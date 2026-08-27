import sqlite3
import threading
import time

from config import DB_PATH, DEFAULT_MAX_BOTS

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA foreign_keys=ON")


def _add_column_if_missing(table, column_def):
    """إضافة عمود لجدول موجود مسبقًا دون كسر قواعد بيانات قديمة (ترقية بسيطة)."""
    col_name = column_def.split()[0]
    cur = _conn.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in cur.fetchall()}
    if col_name not in existing:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")


def init_db():
    with _lock:
        cur = _conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                is_admin INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                max_bots INTEGER DEFAULT 3,
                joined_at INTEGER
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                bot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                name TEXT,
                folder TEXT,
                entry_file TEXT,
                status TEXT DEFAULT 'stopped',
                pid INTEGER,
                auto_restart INTEGER DEFAULT 1,
                created_at INTEGER,
                last_error TEXT,
                restart_count INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS env_vars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                key TEXT,
                value TEXT,
                UNIQUE(bot_id, key)
            )
        """)
        # أعمدة إضافية لحماية Crash Loop (تُضاف بأمان لقواعد بيانات موجودة مسبقًا)
        _add_column_if_missing("bots", "consecutive_crashes INTEGER DEFAULT 0")
        _add_column_if_missing("bots", "last_crash_at INTEGER")
        # حد ذاكرة مخصَّص لكل بوت (NULL = استخدام القيمة الافتراضية من config.py)
        _add_column_if_missing("bots", "max_memory_mb INTEGER")
        # فاصل إعادة التشغيل الدورية المجدولة بالساعات (0 = معطّلة)
        _add_column_if_missing("bots", "restart_interval_hours INTEGER DEFAULT 0")
        # آخر وقت تشغيل فعلي للبوت (يُستخدم لحساب موعد إعادة التشغيل الدورية القادمة)
        _add_column_if_missing("bots", "last_started_at INTEGER")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS usage_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                ts INTEGER,
                cpu REAL,
                mem_mb REAL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_history_bot ON usage_history(bot_id, ts)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                action TEXT,
                target TEXT,
                details TEXT,
                ts INTEGER
            )
        """)
        _conn.commit()


# ================= المستخدمون =================

def get_user(user_id):
    with _lock:
        cur = _conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return cur.fetchone()


def create_user_if_missing(user_id, username):
    if get_user(user_id):
        return
    with _lock:
        _conn.execute(
            "INSERT INTO users (user_id, username, max_bots, joined_at) VALUES (?,?,?,?)",
            (user_id, username or "", DEFAULT_MAX_BOTS, int(time.time())),
        )
        _conn.commit()


def is_admin_db(user_id):
    row = get_user(user_id)
    return bool(row and row["is_admin"])


def set_admin(user_id, value=True):
    with _lock:
        _conn.execute("UPDATE users SET is_admin=? WHERE user_id=?", (1 if value else 0, user_id))
        _conn.commit()


def is_banned(user_id):
    row = get_user(user_id)
    return bool(row and row["is_banned"])


def ban_user(user_id):
    with _lock:
        _conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
        _conn.commit()


def unban_user(user_id):
    with _lock:
        _conn.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
        _conn.commit()


def set_max_bots(user_id, value):
    with _lock:
        _conn.execute("UPDATE users SET max_bots=? WHERE user_id=?", (value, user_id))
        _conn.commit()


def get_max_bots(user_id):
    row = get_user(user_id)
    return row["max_bots"] if row else DEFAULT_MAX_BOTS


def get_all_users():
    with _lock:
        cur = _conn.execute("SELECT * FROM users ORDER BY joined_at DESC")
        return cur.fetchall()


def count_users():
    with _lock:
        cur = _conn.execute("SELECT COUNT(*) AS c FROM users")
        return cur.fetchone()["c"]


# ================= البوتات =================

def insert_bot(owner_id, name):
    with _lock:
        cur = _conn.execute(
            "INSERT INTO bots (owner_id, name, folder, entry_file, created_at) VALUES (?,?,?,?,?)",
            (owner_id, name, "", "", int(time.time())),
        )
        _conn.commit()
        return cur.lastrowid


def insert_bot_if_room(owner_id, name, max_bots):
    """يتحقق من عدد بوتات المستخدم الحالي وينشئ صفًا جديدًا بشكل ذرّي تحت نفس
    القفل، بدل التحقق ثم الإدراج في استدعاءين منفصلين (كان يسمح نظريًا لرفعين
    متزامنين من نفس المستخدم بتجاوز الحد الأقصى معًا). يرجع bot_id عند النجاح،
    أو None إن كان المستخدم قد وصل للحد الأقصى فعلًا لحظة الإدراج."""
    with _lock:
        cur = _conn.execute("SELECT COUNT(*) AS c FROM bots WHERE owner_id=?", (owner_id,))
        current_count = cur.fetchone()["c"]
        if current_count >= max_bots:
            return None
        cur = _conn.execute(
            "INSERT INTO bots (owner_id, name, folder, entry_file, created_at) VALUES (?,?,?,?,?)",
            (owner_id, name, "", "", int(time.time())),
        )
        _conn.commit()
        return cur.lastrowid


def set_bot_files(bot_id, folder, entry_file):
    with _lock:
        _conn.execute("UPDATE bots SET folder=?, entry_file=? WHERE bot_id=?", (folder, entry_file, bot_id))
        _conn.commit()


def get_bot(bot_id):
    with _lock:
        cur = _conn.execute("SELECT * FROM bots WHERE bot_id=?", (bot_id,))
        return cur.fetchone()


def get_user_bots(owner_id):
    with _lock:
        cur = _conn.execute("SELECT * FROM bots WHERE owner_id=? ORDER BY created_at DESC", (owner_id,))
        return cur.fetchall()


def count_user_bots(owner_id):
    with _lock:
        cur = _conn.execute("SELECT COUNT(*) AS c FROM bots WHERE owner_id=?", (owner_id,))
        return cur.fetchone()["c"]


def get_all_bots():
    with _lock:
        cur = _conn.execute("SELECT * FROM bots ORDER BY created_at DESC")
        return cur.fetchall()


def get_bots_by_status(status):
    with _lock:
        cur = _conn.execute("SELECT * FROM bots WHERE status=?", (status,))
        return cur.fetchall()


def update_status(bot_id, status):
    with _lock:
        _conn.execute("UPDATE bots SET status=? WHERE bot_id=?", (status, bot_id))
        _conn.commit()


def update_pid(bot_id, pid):
    with _lock:
        _conn.execute("UPDATE bots SET pid=? WHERE bot_id=?", (pid, bot_id))
        _conn.commit()


def set_last_error(bot_id, err):
    with _lock:
        _conn.execute("UPDATE bots SET last_error=? WHERE bot_id=?", (err, bot_id))
        _conn.commit()


def increment_restart_count(bot_id):
    with _lock:
        _conn.execute("UPDATE bots SET restart_count = restart_count + 1 WHERE bot_id=?", (bot_id,))
        _conn.commit()


def register_crash(bot_id, reset_window_seconds):
    """يسجّل تعطلًا جديدًا ويرجع عدد التعطلات المتتالية (يُعاد العدّاد للصفر إن مرّت
    نافذة reset_window_seconds منذ آخر تعطل)."""
    with _lock:
        row = _conn.execute(
            "SELECT consecutive_crashes, last_crash_at FROM bots WHERE bot_id=?", (bot_id,)
        ).fetchone()
        now = int(time.time())
        prev_count = row["consecutive_crashes"] or 0
        last_at = row["last_crash_at"] or 0
        if now - last_at > reset_window_seconds:
            new_count = 1
        else:
            new_count = prev_count + 1
        _conn.execute(
            "UPDATE bots SET consecutive_crashes=?, last_crash_at=? WHERE bot_id=?",
            (new_count, now, bot_id),
        )
        _conn.commit()
        return new_count


def reset_crash_count(bot_id):
    with _lock:
        _conn.execute(
            "UPDATE bots SET consecutive_crashes=0, last_crash_at=NULL WHERE bot_id=?", (bot_id,)
        )
        _conn.commit()


def toggle_auto_restart(bot_id):
    with _lock:
        row = _conn.execute("SELECT auto_restart FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
        new_val = 0 if row["auto_restart"] else 1
        _conn.execute("UPDATE bots SET auto_restart=? WHERE bot_id=?", (new_val, bot_id))
        _conn.commit()
        return new_val


def set_auto_restart(bot_id, value: bool):
    with _lock:
        _conn.execute("UPDATE bots SET auto_restart=? WHERE bot_id=?", (1 if value else 0, bot_id))
        _conn.commit()


def delete_bot_db(bot_id):
    with _lock:
        _conn.execute("DELETE FROM bots WHERE bot_id=?", (bot_id,))
        _conn.execute("DELETE FROM env_vars WHERE bot_id=?", (bot_id,))
        _conn.commit()


def rename_bot(bot_id, new_name):
    with _lock:
        _conn.execute("UPDATE bots SET name=? WHERE bot_id=?", (new_name, bot_id))
        _conn.commit()


def set_max_memory(bot_id, value_mb):
    """يخصّص حد ذاكرة (بالميغابايت) لبوت معيّن. مرّر None لإعادته للقيمة الافتراضية العامة."""
    with _lock:
        _conn.execute("UPDATE bots SET max_memory_mb=? WHERE bot_id=?", (value_mb, bot_id))
        _conn.commit()


# ================= متغيرات البيئة =================

def set_env_var(bot_id, key, value):
    with _lock:
        _conn.execute(
            "INSERT INTO env_vars (bot_id, key, value) VALUES (?,?,?) "
            "ON CONFLICT(bot_id, key) DO UPDATE SET value=excluded.value",
            (bot_id, key, value),
        )
        _conn.commit()


def get_env_vars(bot_id):
    """يرجع dict بسيط {KEY: VALUE} لحقنها كمتغيرات بيئة عند التشغيل"""
    with _lock:
        cur = _conn.execute("SELECT key, value FROM env_vars WHERE bot_id=?", (bot_id,))
        return {r["key"]: r["value"] for r in cur.fetchall()}


def list_env_vars(bot_id):
    with _lock:
        cur = _conn.execute("SELECT * FROM env_vars WHERE bot_id=?", (bot_id,))
        return cur.fetchall()


def delete_env_var(env_id):
    with _lock:
        _conn.execute("DELETE FROM env_vars WHERE id=?", (env_id,))
        _conn.commit()


def set_restart_interval(bot_id, hours: int):
    with _lock:
        _conn.execute("UPDATE bots SET restart_interval_hours=? WHERE bot_id=?", (hours, bot_id))
        _conn.commit()


def update_last_started(bot_id, ts=None):
    with _lock:
        _conn.execute(
            "UPDATE bots SET last_started_at=? WHERE bot_id=?", (ts or int(time.time()), bot_id)
        )
        _conn.commit()


def get_bots_with_scheduled_restart():
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM bots WHERE status='running' AND restart_interval_hours > 0"
        )
        return cur.fetchall()


# ================= سجل استخدام الموارد =================

def add_usage_point(bot_id, cpu, mem_mb, max_points):
    with _lock:
        _conn.execute(
            "INSERT INTO usage_history (bot_id, ts, cpu, mem_mb) VALUES (?,?,?,?)",
            (bot_id, int(time.time()), cpu, mem_mb),
        )
        # نحتفظ فقط بآخر max_points نقطة لكل بوت لمنع نمو الجدول بلا حدود
        _conn.execute(
            """DELETE FROM usage_history WHERE bot_id=? AND id NOT IN (
                   SELECT id FROM usage_history WHERE bot_id=? ORDER BY id DESC LIMIT ?
               )""",
            (bot_id, bot_id, max_points),
        )
        _conn.commit()


def get_usage_history(bot_id, limit=50):
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM usage_history WHERE bot_id=? ORDER BY id ASC LIMIT ?",
            (bot_id, limit),
        )
        return cur.fetchall()


# ================= سجل تدقيق الأدمن =================

def log_admin_action(admin_id, action, target="", details=""):
    with _lock:
        _conn.execute(
            "INSERT INTO audit_log (admin_id, action, target, details, ts) VALUES (?,?,?,?,?)",
            (admin_id, action, str(target), details, int(time.time())),
        )
        _conn.commit()


def get_audit_log(limit=20):
    with _lock:
        cur = _conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return cur.fetchall()


# ================= إحصائيات عامة =================

def global_stats():
    with _lock:
        total_bots = _conn.execute("SELECT COUNT(*) AS c FROM bots").fetchone()["c"]
        running = _conn.execute("SELECT COUNT(*) AS c FROM bots WHERE status='running'").fetchone()["c"]
        crashed = _conn.execute("SELECT COUNT(*) AS c FROM bots WHERE status='crashed'").fetchone()["c"]
        total_users = _conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        return {
            "total_bots": total_bots,
            "running": running,
            "crashed": crashed,
            "total_users": total_users,
        }
