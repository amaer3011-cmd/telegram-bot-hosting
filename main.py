# -*- coding: utf-8 -*-
"""
بوت استضافة بوتات تيليجرام
============================
بوت رئيسي يسمح للمستخدمين برفع بوتات تيليجرام أخرى (ملف .py أو أرشيف .zip)
واستضافتها وتشغيلها/إيقافها/مراقبتها من داخل تيليجرام مباشرة.

يعمل عبر مكتبة pyTelegramBotAPI (telebot).
"""

import os
import re
import sys
import time
import html
import signal
import threading
import uuid
import http.server

import psutil
import telebot
from telebot import types

import config
import database as db
from process_manager import ProcessManager
from utils import (
    extract_zip, find_entry_file,
    tail_file, delete_folder, UnsafeZipError,
    make_temp_upload_folder, finalize_bot_folder, safe_filename,
    safe_display_filename, check_syntax, export_bot_zip,
)

# نمط اسم صالح لمتغير بيئة بصيغة POSIX (حروف/أرقام/شرطة سفلية، ولا يبدأ برقم)
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# خطوة الزيادة/الإنقاص لحد الذاكرة المخصَّص لكل بوت (ميغابايت)
MEMORY_STEP_MB = 128
MEMORY_MIN_MB = 128
MEMORY_MAX_MB = 4096

bot = telebot.TeleBot(config.BOT_TOKEN, parse_mode="HTML")
db.init_db()

# حالة المحادثات المؤقتة (لكل مستخدم خطوة ينتظرها البوت منه)
pending = {}  # user_id -> {"action": "...", ...}


def notify_owner(user_id, text):
    try:
        bot.send_message(user_id, text)
    except Exception:
        pass


pm = ProcessManager(bot_notifier=notify_owner)

# آخر وقت بدأ فيه كل مستخدم محادثة "رفع بوت جديد" (uid -> timestamp)، لتطبيق
# تبريد بسيط يمنع إطلاق عدة عمليات venv/pip install ثقيلة بسرعة متتالية
_last_upload_at = {}


def _set_pending(uid, data: dict):
    """يضبط حالة محادثة معلّقة لمستخدم مع ختم زمني، حتى يتمكّن خيط التنظيف
    الدوري من حذف الحالات المنسية (مستخدم بدأ محادثة ولم يكملها) بدل تراكمها
    في الذاكرة للأبد على سيرفر طويل التشغيل."""
    data["_ts"] = time.time()
    pending[uid] = data


def _cleanup_stale_state_loop():
    """خيط دوري ينظّف حالات pending المنتهية الصلاحية، وكذلك مدخلات
    _last_upload_at القديمة جدًا التي لم تعد مفيدة لحساب التبريد."""
    while True:
        time.sleep(config.CLEANUP_INTERVAL_SECONDS)
        now = time.time()
        try:
            stale_uids = [
                uid for uid, data in list(pending.items())
                if now - data.get("_ts", now) > config.PENDING_STATE_TTL_SECONDS
            ]
            for uid in stale_uids:
                pending.pop(uid, None)
        except Exception:
            pass
        try:
            stale_upload_uids = [
                uid for uid, ts in list(_last_upload_at.items())
                if now - ts > config.PENDING_STATE_TTL_SECONDS
            ]
            for uid in stale_upload_uids:
                _last_upload_at.pop(uid, None)
        except Exception:
            pass


# ============================================================
#                        أدوات مساعدة
# ============================================================

def is_admin(uid):
    return uid in config.ADMIN_IDS or db.is_admin_db(uid)


def ensure_user(message):
    db.create_user_if_missing(message.from_user.id, message.from_user.username)


def main_menu(uid):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(types.KeyboardButton("🤖 بوتاتي"), types.KeyboardButton("➕ رفع بوت جديد"))
    kb.add(types.KeyboardButton("ℹ️ المساعدة"))
    if is_admin(uid):
        kb.add(types.KeyboardButton("👑 لوحة التحكم"))
    return kb


def status_emoji(status):
    return {"running": "🟢", "stopped": "🔴", "crashed": "⚠️"}.get(status, "⚪️")


def bot_list_kb(rows):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for r in rows:
        emo = status_emoji("running" if pm.is_running(r["bot_id"]) else r["status"])
        kb.add(types.InlineKeyboardButton(f"{emo} {r['name']}", callback_data=f"bot:{r['bot_id']}"))
    return kb


def bot_control_kb(bot_row):
    bid = bot_row["bot_id"]
    kb = types.InlineKeyboardMarkup(row_width=2)
    if pm.is_running(bid):
        kb.add(
            types.InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop:{bid}"),
            types.InlineKeyboardButton("🔁 إعادة تشغيل", callback_data=f"restart:{bid}"),
        )
    else:
        kb.add(types.InlineKeyboardButton("▶️ تشغيل", callback_data=f"start:{bid}"))
    kb.add(
        types.InlineKeyboardButton("📄 السجلات", callback_data=f"logs:{bid}"),
        types.InlineKeyboardButton("📊 الموارد", callback_data=f"usage:{bid}"),
    )
    kb.add(types.InlineKeyboardButton("⚙️ متغيرات البيئة", callback_data=f"env:{bid}"))
    kb.add(
        types.InlineKeyboardButton(f"➖ ذاكرة ({MEMORY_STEP_MB}MB)", callback_data=f"memdown:{bid}"),
        types.InlineKeyboardButton(f"➕ ذاكرة ({MEMORY_STEP_MB}MB)", callback_data=f"memup:{bid}"),
    )
    ar_label = "🔕 إيقاف الإعادة التلقائية" if bot_row["auto_restart"] else "🔔 تفعيل الإعادة التلقائية"
    kb.add(types.InlineKeyboardButton(ar_label, callback_data=f"toggleauto:{bid}"))
    kb.add(types.InlineKeyboardButton("⏰ إعادة تشغيل دورية", callback_data=f"restartsched:{bid}"))
    kb.add(
        types.InlineKeyboardButton("🔄 تحديث الكود", callback_data=f"updatecode:{bid}"),
        types.InlineKeyboardButton("📁 تنزيل الملفات", callback_data=f"export:{bid}"),
    )
    kb.add(types.InlineKeyboardButton("📈 سجل الاستخدام", callback_data=f"usagehist:{bid}"))
    kb.add(types.InlineKeyboardButton("🗑 حذف البوت", callback_data=f"delask:{bid}"))
    kb.add(types.InlineKeyboardButton("⬅️ رجوع لبوتاتي", callback_data="mybots"))
    return kb


def effective_memory_mb(bot_row):
    """يرجع حد الذاكرة الفعلي لبوت معيّن: القيمة المخصَّصة له إن وُجدت، وإلا القيمة
    الافتراضية العامة من config.py."""
    try:
        custom = bot_row["max_memory_mb"]
    except (KeyError, IndexError):
        custom = None
    return custom or config.MAX_BOT_MEMORY_MB


def bot_info_text(bot_row):
    running = pm.is_running(bot_row["bot_id"])
    status = "🟢 يعمل الآن" if running else status_emoji(bot_row["status"]) + " " + {
        "stopped": "متوقف",
        "crashed": "متعطل (توقف بشكل غير متوقع)",
    }.get(bot_row["status"], bot_row["status"])
    auto = "مفعّلة ✅" if bot_row["auto_restart"] else "معطّلة ❌"
    err = (
        f"\n\n⚠️ آخر خطأ:\n<code>{html.escape(str(bot_row['last_error']))}</code>"
        if bot_row["last_error"] else ""
    )
    # ⚠️ اسم البوت يدخله المستخدم بحرية — يجب تعقيمه دائمًا قبل إدراجه في نص بصيغة HTML
    # لمنع حقن وسوم (مثل روابط تصيّد) تظهر في محادثة صاحب البوت أو الأدمن عند العرض.
    safe_name = html.escape(str(bot_row["name"]))
    interval = bot_row["restart_interval_hours"] or 0
    sched_line = f"إعادة تشغيل دورية: كل {interval} ساعة\n" if interval else "إعادة تشغيل دورية: معطّلة\n"

    health_line = ""
    if running:
        health = pm.check_health(bot_row["bot_id"])
        if health is True:
            health_line = "الفحص الصحي (getMe): متصل بتيليجرام ✅\n"
        elif health is False:
            health_line = "الفحص الصحي (getMe): فشل الاتصال بتيليجرام ⚠️\n"
        # health is None → لا يوجد توكن BOT_TOKEN معرَّف، لا نعرض شيئًا

    return (
        f"🤖 <b>{safe_name}</b>\n"
        f"الحالة: {status}\n"
        f"{health_line}"
        f"إعادة التشغيل التلقائي: {auto}\n"
        f"{sched_line}"
        f"عدد مرات إعادة التشغيل: {bot_row['restart_count']}\n"
        f"حد الذاكرة المخصَّص: {effective_memory_mb(bot_row)}MB"
        f"{err}"
    )


def send_long(chat_id, text, filename="log.txt"):
    """يرسل نصًا طويلًا كملف إن تجاوز حد رسائل تيليجرام.

    ⚠️ filename قد يُبنى جزئيًا من نص حر أدخله المستخدم (مثل اسم بوته)، لذلك
    نمرّره دائمًا عبر safe_display_filename() قبل استخدامه كجزء من مسار على
    القرص، لمنع اجتياز مسار (Path Traversal) أو الكتابة خارج /tmp لو احتوى
    الاسم على "/" أو "..". كما نستخدم اسم ملف مؤقت فريد (uuid) للكتابة الفعلية
    على القرص، بينما visible_file_name (الاسم المعروض للمستخدم في تيليجرام)
    يبقى الاسم المعقَّم المقروء."""
    if len(text) <= 3500:
        bot.send_message(chat_id, f"<pre>{html.escape(text)}</pre>")
        return
    display_name = safe_display_filename(filename, default="log.txt")
    tmp_path = f"/tmp/{uuid.uuid4().hex}.txt"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        with open(tmp_path, "rb") as f:
            bot.send_document(chat_id, f, visible_file_name=display_name)
    finally:
        os.remove(tmp_path)


# ============================================================
#                         /start والقوائم
# ============================================================

@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    ensure_user(message)
    uid = message.from_user.id
    if db.is_banned(uid):
        bot.send_message(uid, "🚫 تم حظرك من استخدام هذه الخدمة.")
        return
    text = (
        "👋 أهلًا بك في <b>بوت استضافة بوتات تيليجرام</b>!\n\n"
        "يمكنك من هنا رفع بوتات تيليجرام خاصة بك (ملف .py أو أرشيف .zip) "
        "وتشغيلها ومراقبتها والتحكم بها بالكامل.\n\n"
        "استخدم الأزرار بالأسفل للبدء 👇"
    )
    bot.send_message(uid, text, reply_markup=main_menu(uid))


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    uid = message.from_user.id
    if pending.pop(uid, None) is not None:
        bot.send_message(uid, "✅ تم إلغاء العملية الحالية.", reply_markup=main_menu(uid))
    else:
        bot.send_message(uid, "لا توجد عملية معلّقة لإلغائها.", reply_markup=main_menu(uid))


@bot.message_handler(func=lambda m: m.text == "ℹ️ المساعدة")
def help_menu(message):
    text = (
        "📌 <b>طريقة الاستخدام:</b>\n\n"
        "1️⃣ اضغط «➕ رفع بوت جديد»\n"
        "2️⃣ أرسل اسمًا للبوت\n"
        "3️⃣ أرسل ملف <code>.py</code> واحد، أو أرشيف <code>.zip</code> يحتوي مشروع البوت كاملًا "
        "(يُفضّل تضمين <code>requirements.txt</code> إن كان بوتك يحتاج مكتبات خاصة)\n"
        "4️⃣ أضف متغيرات البيئة اللازمة لبوتك (مثل توكن بوته الخاص عبر متغير مثلاً "
        "<code>BOT_TOKEN</code>) — بوتك يجب أن يقرأ توكنه من متغيرات البيئة "
        "عبر <code>os.environ</code>\n"
        "5️⃣ اضغط ▶️ تشغيل\n\n"
        "⚙️ <b>مزايا متقدمة:</b>\n"
        "• إعادة تشغيل تلقائي عند تعطّل البوت + إشعار فوري لك\n"
        "• عرض السجلات (Logs) اللحظية لأي بوت\n"
        "• مراقبة استهلاك المعالج والذاكرة لكل بوت + سجل تاريخي بسيط\n"
        "• بيئة بايثون افتراضية معزولة لكل بوت على حدة\n"
        "• إدارة متغيرات البيئة (Environment Variables) لكل بوت\n"
        "• 🔄 تحديث كود بوت موجود دون فقدان إعداداته\n"
        "• 📁 تنزيل ملفات بوتك كأرشيف zip في أي وقت\n"
        "• ⏰ جدولة إعادة تشغيل دورية اختيارية لأي بوت\n\n"
        "🚫 استخدم <code>/cancel</code> في أي وقت لإلغاء عملية معلّقة (مثل رفع بوت لم تكمله)."
    )
    bot.send_message(message.chat.id, text)


# ============================================================
#                      رفع بوت جديد (محادثة)
# ============================================================

@bot.message_handler(func=lambda m: m.text == "➕ رفع بوت جديد")
def new_bot_start(message):
    uid = message.from_user.id
    ensure_user(message)
    if db.is_banned(uid):
        return
    if db.count_user_bots(uid) >= db.get_max_bots(uid):
        bot.send_message(uid, f"⚠️ وصلت للحد الأقصى المسموح به ({db.get_max_bots(uid)} بوتات).")
        return

    # تبريد بسيط بين محاولات رفع بوت جديد المتتالية لنفس المستخدم
    now = time.time()
    last = _last_upload_at.get(uid, 0)
    remaining = config.UPLOAD_COOLDOWN_SECONDS - (now - last)
    if remaining > 0:
        bot.send_message(uid, f"⏳ الرجاء الانتظار {int(remaining) + 1} ثانية قبل محاولة رفع بوت جديد.")
        return
    _last_upload_at[uid] = now

    _set_pending(uid, {"action": "await_name"})
    bot.send_message(uid, "✏️ أرسل اسمًا مميزًا لبوتك الجديد:")


@bot.message_handler(func=lambda m: pending.get(m.from_user.id, {}).get("action") == "await_name")
def new_bot_name(message):
    uid = message.from_user.id
    name = message.text.strip()[:64]
    if not name:
        bot.send_message(uid, "الاسم غير صالح، حاول مرة أخرى:")
        return
    # نؤجل إنشاء صف البوت في قاعدة البيانات حتى يكتمل رفع الملف بنجاح فعليًا،
    # لتفادي تراكم "بوتات شبح" فارغة (folder="") تُحتسب من الحد الأقصى للمستخدم
    # للأبد لو توقف عن إكمال الخطوات. نحتفظ بالاسم مؤقتًا في pending[uid] فقط.
    temp_token = uuid.uuid4().hex[:12]
    _set_pending(uid, {"action": "await_file", "name": name, "temp_token": temp_token})
    bot.send_message(
        uid,
        "📎 الآن أرسل ملف بوتك:\n"
        "• ملف <code>.py</code> واحد، أو\n"
        "• أرشيف <code>.zip</code> يحتوي على كامل مشروع البوت "
        "(ويُفضّل وجود <code>requirements.txt</code> بداخله)",
    )


@bot.message_handler(
    content_types=["document"],
    func=lambda m: pending.get(m.from_user.id, {}).get("action") == "await_file",
)
def new_bot_file(message):
    uid = message.from_user.id
    doc = message.document
    size_mb = doc.file_size / (1024 * 1024)
    if size_mb > config.MAX_FILE_SIZE_MB:
        bot.send_message(uid, f"⚠️ الملف كبير جدًا (الحد الأقصى {config.MAX_FILE_SIZE_MB}MB).")
        return

    name = pending[uid]["name"]
    temp_token = pending[uid]["temp_token"]
    folder = make_temp_upload_folder(uid, temp_token)

    file_info = bot.get_file(doc.file_id)
    data = bot.download_file(file_info.file_path)

    # ⚠️ اسم الملف قادم بالكامل من المستخدم عبر تيليجرام، وقد يحتوي أجزاء مسار
    # (مثل ../../../etc/x) لو استُخدم كما هو مباشرة في os.path.join — نعقّمه أولًا
    # عبر safe_filename() (تأخذ os.path.basename فقط وترفض أي ".." متبقٍّ)
    fname = safe_filename(doc.file_name or "bot_file")
    save_path = os.path.join(folder, fname)
    with open(save_path, "wb") as f:
        f.write(data)

    if fname.lower().endswith(".zip"):
        try:
            extract_zip(save_path, folder)
        except UnsafeZipError as e:
            # أرشيف يحاول الخروج من مجلد الوجهة أو يتجاوز الحجم المسموح — نحذف كل ما استُلم
            # (لا يوجد صف بوت في قاعدة البيانات بعد، لأننا لم ننشئه إلا بعد نجاح الاستخراج)
            delete_folder(folder)
            pending.pop(uid, None)
            bot.send_message(uid, f"🚫 تم رفض الأرشيف لأسباب أمنية: {e}")
            return
        except Exception as e:
            bot.send_message(uid, f"❌ تعذر استخراج الأرشيف: {e}")
            return
        entry = find_entry_file(folder)
    elif fname.lower().endswith(".py"):
        entry = save_path
    else:
        bot.send_message(uid, "⚠️ الملف يجب أن يكون .py أو .zip فقط. حاول مرة أخرى:")
        return

    if not entry:
        delete_folder(folder)
        pending.pop(uid, None)
        bot.send_message(uid, "❌ لم يتم العثور على ملف بايثون قابل للتشغيل داخل ما أرسلته.")
        return

    # الآن فقط، بعد التأكد من نجاح الاستخراج ووجود ملف تشغيل صالح، ننشئ صف
    # البوت فعليًا في قاعدة البيانات وننقل الملفات من المجلد المؤقت لمجلدها النهائي.
    # نستخدم insert_bot_if_room بدل count_user_bots + insert منفصلين، لأن ذلك
    # كان يسمح نظريًا لرفعين متزامنين من نفس المستخدم بتجاوز الحد الأقصى معًا؛
    # الآن التحقق والإدراج يحصلان بشكل ذرّي تحت نفس القفل
    entry_rel = os.path.relpath(entry, folder)
    bot_id = db.insert_bot_if_room(uid, name, db.get_max_bots(uid))
    if bot_id is None:
        delete_folder(folder)
        pending.pop(uid, None)
        bot.send_message(uid, f"⚠️ وصلت للحد الأقصى المسموح به ({db.get_max_bots(uid)} بوتات).")
        return

    final_folder = finalize_bot_folder(folder, uid, bot_id)
    final_entry = os.path.join(final_folder, entry_rel)

    db.set_bot_files(bot_id, final_folder, final_entry)
    pending.pop(uid, None)

    # فحص صيغة بايثون مبكرًا (دون تنفيذ الكود) لإظهار أي خطأ صياغي للمستخدم
    # فورًا بدل اكتشافه لاحقًا بعد محاولة تشغيل فاشلة
    syntax_ok, syntax_err = check_syntax(final_entry)
    warn = ""
    if not syntax_ok:
        warn = (
            f"\n\n⚠️ <b>تحذير:</b> يبدو أن ملف التشغيل الرئيسي يحتوي خطأ في الصياغة:\n"
            f"<code>{html.escape(syntax_err[:400])}</code>\nراجعه قبل التشغيل."
        )

    bot.send_message(
        uid,
        "✅ تم استلام بوتك بنجاح!\n"
        "يمكنك الآن (اختياريًا) إضافة متغيرات بيئة كتوكن البوت، ثم تشغيله من لوحة التحكم بالأسفل."
        + warn,
    )
    row = db.get_bot(bot_id)
    bot.send_message(uid, bot_info_text(row), reply_markup=bot_control_kb(row))


@bot.message_handler(
    content_types=["text"],
    func=lambda m: pending.get(m.from_user.id, {}).get("action") == "await_file",
)
def new_bot_file_wrong_type(message):
    bot.send_message(message.from_user.id, "⚠️ الرجاء إرسال ملف (.py أو .zip) وليس نصًا.")


# ============================================================
#                          بوتاتي
# ============================================================

@bot.message_handler(func=lambda m: m.text == "🤖 بوتاتي")
def my_bots(message):
    uid = message.from_user.id
    ensure_user(message)
    rows = db.get_user_bots(uid)
    if not rows:
        bot.send_message(uid, "لا تملك أي بوتات بعد. اضغط «➕ رفع بوت جديد» للبدء.")
        return
    bot.send_message(uid, f"🤖 بوتاتك ({len(rows)}/{db.get_max_bots(uid)}):", reply_markup=bot_list_kb(rows))


@bot.callback_query_handler(func=lambda c: c.data == "mybots")
def cb_mybots(call):
    uid = call.from_user.id
    rows = db.get_user_bots(uid)
    try:
        if not rows:
            bot.edit_message_text("لا تملك أي بوتات بعد.", uid, call.message.message_id)
        else:
            bot.edit_message_text(
                f"🤖 بوتاتك ({len(rows)}/{db.get_max_bots(uid)}):",
                uid, call.message.message_id, reply_markup=bot_list_kb(rows),
            )
    except Exception:
        pass
    bot.answer_callback_query(call.id)


def _owned_bot_or_none(call):
    """يتحقق أن البوت المطلوب يخص صاحب الطلب (أو أنه أدمن) ويرجع الصف"""
    bid = int(call.data.split(":")[1])
    row = db.get_bot(bid)
    if not row:
        bot.answer_callback_query(call.id, "❌ هذا البوت غير موجود.")
        return None
    if row["owner_id"] != call.from_user.id and not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "🚫 هذا البوت ليس ملكك.")
        return None
    return row


@bot.callback_query_handler(func=lambda c: c.data.startswith("bot:"))
def cb_bot_panel(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    try:
        bot.edit_message_text(
            bot_info_text(row), call.from_user.id, call.message.message_id,
            reply_markup=bot_control_kb(row),
        )
    except Exception:
        bot.send_message(call.from_user.id, bot_info_text(row), reply_markup=bot_control_kb(row))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("start:"))
def cb_start(call):
    row = _owned_bot_or_none(call)
    if not row:
        return

    # فحص صياغة سريع قبل التشغيل (لا يمنع التشغيل، فقط تحذير استباقي — قد يكون
    # المستخدم يستخدم ملف تشغيل ديناميكي أو صيغة غير مدعومة بالكامل بـ py_compile)
    syntax_ok, syntax_err = check_syntax(row["entry_file"]) if row["entry_file"] else (True, None)
    if not syntax_ok:
        bot.answer_callback_query(call.id, "⚠️ تحذير: خطأ محتمل في صياغة الكود، جارٍ المحاولة رغم ذلك...")
        bot.send_message(
            call.from_user.id,
            f"⚠️ تحذير: يبدو أن هناك خطأ في صياغة ملف التشغيل:\n"
            f"<code>{html.escape(syntax_err[:400])}</code>",
        )
    else:
        bot.answer_callback_query(call.id, "⏳ جاري التشغيل...")

    # تشغيل يدوي = فرصة جديدة؛ نصفّر عدّاد التعطلات المتتالية القديم
    db.reset_crash_count(row["bot_id"])
    ok, msg = pm.start_bot(row)
    row = db.get_bot(row["bot_id"])
    bot.send_message(call.from_user.id, msg)
    bot.send_message(call.from_user.id, bot_info_text(row), reply_markup=bot_control_kb(row))


@bot.callback_query_handler(func=lambda c: c.data.startswith("stop:"))
def cb_stop(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    bot.answer_callback_query(call.id, "⏳ جاري الإيقاف...")
    ok, msg = pm.stop_bot(row["bot_id"])
    row = db.get_bot(row["bot_id"])
    bot.send_message(call.from_user.id, msg)
    bot.send_message(call.from_user.id, bot_info_text(row), reply_markup=bot_control_kb(row))


@bot.callback_query_handler(func=lambda c: c.data.startswith("restart:"))
def cb_restart(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    bot.answer_callback_query(call.id, "⏳ جاري إعادة التشغيل...")
    db.reset_crash_count(row["bot_id"])
    ok, msg = pm.restart_bot(row)
    row = db.get_bot(row["bot_id"])
    bot.send_message(call.from_user.id, msg)
    bot.send_message(call.from_user.id, bot_info_text(row), reply_markup=bot_control_kb(row))


@bot.callback_query_handler(func=lambda c: c.data.startswith("toggleauto:"))
def cb_toggle_auto(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    new_val = db.toggle_auto_restart(row["bot_id"])
    if new_val:
        # عند تفعيل إعادة التشغيل التلقائي يدويًا، نمنح البوت بداية نظيفة
        db.reset_crash_count(row["bot_id"])
    row = db.get_bot(row["bot_id"])
    bot.answer_callback_query(call.id, "تم التحديث ✅")
    try:
        bot.edit_message_text(
            bot_info_text(row), call.from_user.id, call.message.message_id,
            reply_markup=bot_control_kb(row),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("memup:") or c.data.startswith("memdown:"))
def cb_memory_adjust(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    action = call.data.split(":")[0]
    current = effective_memory_mb(row)
    if action == "memup":
        new_val = min(current + MEMORY_STEP_MB, MEMORY_MAX_MB)
    else:
        new_val = max(current - MEMORY_STEP_MB, MEMORY_MIN_MB)
    db.set_max_memory(row["bot_id"], new_val)
    row = db.get_bot(row["bot_id"])
    bot.answer_callback_query(call.id, f"حد الذاكرة الجديد: {new_val}MB (يلزم إعادة تشغيل البوت لتطبيقه)")
    try:
        bot.edit_message_text(
            bot_info_text(row), call.from_user.id, call.message.message_id,
            reply_markup=bot_control_kb(row),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("restartsched:"))
def cb_restart_sched(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    choices = config.RESTART_INTERVAL_CHOICES_HOURS
    current = row["restart_interval_hours"] or 0
    idx = choices.index(current) if current in choices else 0
    new_val = choices[(idx + 1) % len(choices)]
    db.set_restart_interval(row["bot_id"], new_val)
    row = db.get_bot(row["bot_id"])
    label = "معطّلة" if new_val == 0 else f"كل {new_val} ساعة"
    bot.answer_callback_query(call.id, f"إعادة التشغيل الدورية الآن: {label}")
    try:
        bot.edit_message_text(
            bot_info_text(row), call.from_user.id, call.message.message_id,
            reply_markup=bot_control_kb(row),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("updatecode:"))
def cb_update_code(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    _set_pending(call.from_user.id, {"action": "await_update_file", "bot_id": row["bot_id"]})
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.from_user.id,
        f"📎 أرسل ملف الكود الجديد لبوت «{html.escape(row['name'])}» "
        f"(<code>.py</code> واحد أو أرشيف <code>.zip</code> كامل).\n\n"
        "⚠️ سيتم استبدال كل ملفات الكود الحالية بالملفات الجديدة (تبقى متغيرات "
        "البيئة وسجل التشغيل والإحصائيات كما هي)، وسيُوقَف البوت مؤقتًا أثناء التحديث.\n"
        "استخدم /cancel للإلغاء.",
    )


@bot.message_handler(
    content_types=["document"],
    func=lambda m: pending.get(m.from_user.id, {}).get("action") == "await_update_file",
)
def update_code_file(message):
    uid = message.from_user.id
    doc = message.document
    size_mb = doc.file_size / (1024 * 1024)
    if size_mb > config.MAX_FILE_SIZE_MB:
        bot.send_message(uid, f"⚠️ الملف كبير جدًا (الحد الأقصى {config.MAX_FILE_SIZE_MB}MB).")
        return

    bot_id = pending[uid]["bot_id"]
    row = db.get_bot(bot_id)
    if not row or (row["owner_id"] != uid and not is_admin(uid)):
        pending.pop(uid, None)
        bot.send_message(uid, "🚫 غير مسموح.")
        return

    fname = safe_filename(doc.file_name or "bot_file")
    if not (fname.lower().endswith(".py") or fname.lower().endswith(".zip")):
        bot.send_message(uid, "⚠️ الملف يجب أن يكون .py أو .zip فقط. حاول مرة أخرى:")
        return

    # نستقبل الملف الجديد في مجلد مؤقت أولًا (بنفس منطق الرفع الأول)، حتى لو
    # فشل الاستخراج أو لم نجد ملف تشغيل صالح، يبقى كود البوت القديم سليمًا كما هو
    temp_token = uuid.uuid4().hex[:12]
    temp_folder = make_temp_upload_folder(row["owner_id"], f"update_{temp_token}")

    file_info = bot.get_file(doc.file_id)
    data = bot.download_file(file_info.file_path)
    save_path = os.path.join(temp_folder, fname)
    with open(save_path, "wb") as f:
        f.write(data)

    if fname.lower().endswith(".zip"):
        try:
            extract_zip(save_path, temp_folder)
        except UnsafeZipError as e:
            delete_folder(temp_folder)
            pending.pop(uid, None)
            bot.send_message(uid, f"🚫 تم رفض الأرشيف لأسباب أمنية: {e}")
            return
        except Exception as e:
            delete_folder(temp_folder)
            bot.send_message(uid, f"❌ تعذر استخراج الأرشيف: {e}")
            return
        entry = find_entry_file(temp_folder)
    else:
        entry = save_path

    if not entry:
        delete_folder(temp_folder)
        pending.pop(uid, None)
        bot.send_message(uid, "❌ لم يتم العثور على ملف بايثون قابل للتشغيل داخل ما أرسلته.")
        return

    # كل شيء سليم الآن: نوقف البوت (إن كان يعمل)، نحذف مجلده القديم، وننقل
    # المجلد المؤقت الجديد مكانه محتفظين بنفس bot_id (وبالتالي env_vars وbot_id)
    entry_rel = os.path.relpath(entry, temp_folder)
    was_running = pm.is_running(bot_id)
    pm.stop_bot(bot_id, mark_stopped=False)

    old_folder = row["folder"]
    if old_folder and os.path.exists(old_folder):
        delete_folder(old_folder)
    final_folder = finalize_bot_folder(temp_folder, row["owner_id"], bot_id)
    final_entry = os.path.join(final_folder, entry_rel)
    db.set_bot_files(bot_id, final_folder, final_entry)
    db.reset_crash_count(bot_id)
    pending.pop(uid, None)

    syntax_ok, syntax_err = check_syntax(final_entry)
    warn = ""
    if not syntax_ok:
        warn = (
            f"\n\n⚠️ <b>تحذير:</b> خطأ محتمل في صياغة الكود الجديد:\n"
            f"<code>{html.escape(syntax_err[:400])}</code>"
        )

    fresh_row = db.get_bot(bot_id)
    bot.send_message(uid, "✅ تم تحديث كود البوت بنجاح." + warn)
    if was_running:
        ok, msg = pm.start_bot(fresh_row)
        fresh_row = db.get_bot(bot_id)
        bot.send_message(uid, f"🔁 إعادة تشغيل البوت بالكود الجديد: {msg}")
    bot.send_message(uid, bot_info_text(fresh_row), reply_markup=bot_control_kb(fresh_row))


@bot.callback_query_handler(func=lambda c: c.data.startswith("export:"))
def cb_export(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    bot.answer_callback_query(call.id, "⏳ جاري تجهيز الأرشيف...")
    try:
        base_name = safe_display_filename(row["name"], default="bot")
        dest_no_ext = f"/tmp/{uuid.uuid4().hex}"
        archive_path = export_bot_zip(row["folder"], dest_no_ext)
        with open(archive_path, "rb") as f:
            bot.send_document(call.from_user.id, f, visible_file_name=f"{base_name}.zip")
    except Exception as e:
        bot.send_message(call.from_user.id, f"❌ تعذر تجهيز الأرشيف: {e}")
    finally:
        try:
            os.remove(archive_path)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("usagehist:"))
def cb_usage_history(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    bot.answer_callback_query(call.id)
    points = db.get_usage_history(row["bot_id"], limit=config.USAGE_HISTORY_MAX_POINTS)
    if not points:
        bot.send_message(
            call.from_user.id,
            "لا يوجد سجل استخدام كافٍ بعد — يُسجَّل تلقائيًا كل بضع دقائق أثناء عمل البوت.",
        )
        return
    blocks = "▁▂▃▄▅▆▇█"

    def sparkline(values):
        if not values:
            return ""
        lo, hi = min(values), max(values)
        span = (hi - lo) or 1
        return "".join(blocks[min(int((v - lo) / span * (len(blocks) - 1)), len(blocks) - 1)] for v in values)

    cpu_vals = [p["cpu"] for p in points]
    mem_vals = [p["mem_mb"] for p in points]
    text = (
        f"📈 <b>سجل استخدام بوت {html.escape(row['name'])}</b> (آخر {len(points)} لقطة)\n\n"
        f"CPU: {sparkline(cpu_vals)}  (أحدث قيمة: {cpu_vals[-1]:.1f}%)\n"
        f"RAM: {sparkline(mem_vals)}  (أحدث قيمة: {mem_vals[-1]:.0f}MB)"
    )
    bot.send_message(call.from_user.id, text)


@bot.callback_query_handler(func=lambda c: c.data.startswith("logs:"))
def cb_logs(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    bot.answer_callback_query(call.id)
    log_path = os.path.join(row["folder"], "run.log")
    content = tail_file(log_path, config.LOG_TAIL_LINES)
    send_long(call.from_user.id, content, filename=f"{row['name']}_log.txt")


@bot.callback_query_handler(func=lambda c: c.data.startswith("usage:"))
def cb_usage(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    usage = pm.get_usage(row["bot_id"])
    if not usage:
        bot.answer_callback_query(call.id, "البوت غير مُشغّل حاليًا.")
        return
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.from_user.id,
        f"📊 <b>استهلاك بوت {row['name']}</b>\n"
        f"المعالج (CPU): {usage['cpu']}\n"
        f"الذاكرة (RAM): {usage['mem']}\n"
        f"مدة التشغيل: {usage['uptime']}",
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("delask:"))
def cb_delask(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ نعم، احذف نهائيًا", callback_data=f"delconfirm:{row['bot_id']}"),
        types.InlineKeyboardButton("❌ إلغاء", callback_data=f"bot:{row['bot_id']}"),
    )
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"⚠️ هل أنت متأكد من حذف البوت «{row['name']}»؟\nسيتم حذف جميع ملفاته نهائيًا ولا يمكن التراجع.",
        call.from_user.id, call.message.message_id, reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("delconfirm:"))
def cb_delconfirm(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    pm.stop_bot(row["bot_id"], mark_stopped=False)
    delete_folder(row["folder"])
    db.delete_bot_db(row["bot_id"])
    bot.answer_callback_query(call.id, "🗑 تم الحذف")
    bot.edit_message_text("🗑 تم حذف البوت بنجاح.", call.from_user.id, call.message.message_id)


# ---------------- متغيرات البيئة ----------------

@bot.callback_query_handler(func=lambda c: c.data.startswith("env:"))
def cb_env(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    vars_rows = db.list_env_vars(row["bot_id"])
    kb = types.InlineKeyboardMarkup(row_width=1)
    for v in vars_rows:
        kb.add(types.InlineKeyboardButton(f"🗑 {v['key']}", callback_data=f"envdel:{v['id']}:{row['bot_id']}"))
    kb.add(types.InlineKeyboardButton("➕ إضافة متغير جديد", callback_data=f"envadd:{row['bot_id']}"))
    if vars_rows:
        kb.add(types.InlineKeyboardButton("📤 تصدير كملف .env", callback_data=f"envexport:{row['bot_id']}"))
    kb.add(types.InlineKeyboardButton("📥 استيراد من ملف .env", callback_data=f"envimport:{row['bot_id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data=f"bot:{row['bot_id']}"))

    if vars_rows:
        listing = "\n".join(
            f"• <code>{html.escape(v['key'])}</code> = <code>{html.escape(v['value'])}</code>"
            for v in vars_rows
        )
    else:
        listing = "لا توجد متغيرات بيئة مضافة بعد."

    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"⚙️ <b>متغيرات بيئة بوت {row['name']}</b>\n\n{listing}\n\n"
        f"اضغط 🗑 لحذف متغير، أو أضف متغيرًا جديدًا.",
        call.from_user.id, call.message.message_id, reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("envadd:"))
def cb_envadd(call):
    bid = int(call.data.split(":")[1])
    row = db.get_bot(bid)
    if not row or (row["owner_id"] != call.from_user.id and not is_admin(call.from_user.id)):
        bot.answer_callback_query(call.id, "🚫 غير مسموح.")
        return
    _set_pending(call.from_user.id, {"action": "await_env", "bot_id": bid})
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.from_user.id,
        "✏️ أرسل المتغير بصيغة:\n<code>KEY=VALUE</code>\n\n"
        "مثال:\n<code>BOT_TOKEN=123456:ABCdefGhIJ</code>",
    )


@bot.message_handler(func=lambda m: pending.get(m.from_user.id, {}).get("action") == "await_env")
def env_value(message):
    uid = message.from_user.id
    bot_id = pending[uid]["bot_id"]
    text = message.text.strip()
    if "=" not in text:
        bot.send_message(uid, "⚠️ الصيغة غير صحيحة. أرسل بصيغة KEY=VALUE:")
        return
    key, value = text.split("=", 1)
    key, value = key.strip(), value.strip()
    if not key:
        bot.send_message(uid, "⚠️ اسم المتغير غير صالح. حاول مرة أخرى:")
        return
    # نتحقق أن الاسم يطابق صيغة متغير بيئة POSIX صالحة بدل قبول أي نص بصمت
    if not ENV_KEY_PATTERN.match(key):
        bot.send_message(
            uid,
            "⚠️ اسم المتغير يجب أن يطابق صيغة متغيرات البيئة الصحيحة:\n"
            "حروف/أرقام/شرطة سفلية فقط، ولا يبدأ برقم (مثال: <code>BOT_TOKEN</code>).\n"
            "حاول مرة أخرى بصيغة KEY=VALUE:",
        )
        return
    # نرفض مفاتيح البيئة المحمية بدل قبولها بصمت ثم تجاهلها لاحقًا عند التشغيل،
    # لأن قيمها الحقيقية (PATH وغيرها) تُفرض دائمًا من بيئة الخادم للحماية
    if key.upper() in config.PROTECTED_ENV_KEYS:
        bot.send_message(
            uid,
            f"🚫 لا يمكن تعيين المتغير <code>{key}</code> لأنه محمي ويؤثر على بيئة تشغيل بايثون نفسها.\n"
            "اختر اسمًا آخر لبوتك (مثال: <code>MY_BOT_TOKEN</code>):",
        )
        return
    db.set_env_var(bot_id, key, value)
    pending.pop(uid, None)
    bot.send_message(uid, f"✅ تم حفظ المتغير <code>{key}</code>.\nملاحظة: يلزم إعادة تشغيل البوت لتطبيق المتغير.")
    row = db.get_bot(bot_id)
    bot.send_message(uid, bot_info_text(row), reply_markup=bot_control_kb(row))


@bot.callback_query_handler(func=lambda c: c.data.startswith("envexport:"))
def cb_env_export(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    vars_rows = db.list_env_vars(row["bot_id"])
    bot.answer_callback_query(call.id)
    if not vars_rows:
        bot.send_message(call.from_user.id, "لا توجد متغيرات بيئة لتصديرها.")
        return
    content = "\n".join(f"{v['key']}={v['value']}" for v in vars_rows)
    base_name = safe_display_filename(row["name"], default="bot")
    tmp_path = f"/tmp/{uuid.uuid4().hex}.env"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content + "\n")
    try:
        with open(tmp_path, "rb") as f:
            bot.send_document(call.from_user.id, f, visible_file_name=f"{base_name}.env")
    finally:
        os.remove(tmp_path)


@bot.callback_query_handler(func=lambda c: c.data.startswith("envimport:"))
def cb_env_import_start(call):
    row = _owned_bot_or_none(call)
    if not row:
        return
    _set_pending(call.from_user.id, {"action": "await_env_import", "bot_id": row["bot_id"]})
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.from_user.id,
        "📥 أرسل ملف <code>.env</code> (كل سطر بصيغة <code>KEY=VALUE</code>).\n"
        "سيتم استبدال أي متغير موجود بنفس الاسم بالقيمة الجديدة. استخدم /cancel للإلغاء.",
    )


@bot.message_handler(
    content_types=["document"],
    func=lambda m: pending.get(m.from_user.id, {}).get("action") == "await_env_import",
)
def env_import_file(message):
    uid = message.from_user.id
    bot_id = pending[uid]["bot_id"]
    row = db.get_bot(bot_id)
    if not row or (row["owner_id"] != uid and not is_admin(uid)):
        pending.pop(uid, None)
        bot.send_message(uid, "🚫 غير مسموح.")
        return

    doc = message.document
    if doc.file_size > 1024 * 1024:  # 1MB كافٍ جدًا لملف .env
        bot.send_message(uid, "⚠️ الملف كبير جدًا لملف .env.")
        return
    file_info = bot.get_file(doc.file_id)
    data = bot.download_file(file_info.file_path)
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        bot.send_message(uid, "❌ تعذر قراءة الملف كنص.")
        return

    imported, skipped = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().strip('"').strip("'"), value.strip().strip('"').strip("'")
        if not ENV_KEY_PATTERN.match(key) or key.upper() in config.PROTECTED_ENV_KEYS:
            skipped.append(key)
            continue
        db.set_env_var(bot_id, key, value)
        imported.append(key)

    pending.pop(uid, None)
    summary = f"✅ تم استيراد {len(imported)} متغير."
    if skipped:
        summary += f"\n⚠️ تم تخطي {len(skipped)} متغير غير صالح/محمي: {', '.join(skipped[:10])}"
    summary += "\nملاحظة: يلزم إعادة تشغيل البوت لتطبيق المتغيرات."
    bot.send_message(uid, summary)
    fresh_row = db.get_bot(bot_id)
    bot.send_message(uid, bot_info_text(fresh_row), reply_markup=bot_control_kb(fresh_row))


@bot.callback_query_handler(func=lambda c: c.data.startswith("envdel:"))
def cb_envdel(call):
    _, env_id, bot_id = call.data.split(":")
    row = db.get_bot(int(bot_id))
    if not row or (row["owner_id"] != call.from_user.id and not is_admin(call.from_user.id)):
        bot.answer_callback_query(call.id, "🚫 غير مسموح.")
        return
    db.delete_env_var(int(env_id))
    bot.answer_callback_query(call.id, "🗑 تم الحذف")
    # إعادة عرض القائمة المحدثة
    call.data = f"env:{bot_id}"
    cb_env(call)


# ============================================================
#                         لوحة تحكم الأدمن
# ============================================================

@bot.message_handler(func=lambda m: m.text == "👑 لوحة التحكم")
def admin_panel(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 إحصائيات عامة", callback_data="adm:stats"),
        types.InlineKeyboardButton("👥 المستخدمون", callback_data="adm:users:0"),
        types.InlineKeyboardButton("🤖 كل البوتات", callback_data="adm:bots:0"),
        types.InlineKeyboardButton("🟢 البوتات الشغّالة فقط", callback_data="adm:botsf:running:0"),
        types.InlineKeyboardButton("⚠️ البوتات المتعطلة فقط", callback_data="adm:botsf:crashed:0"),
        types.InlineKeyboardButton("📢 إذاعة رسالة", callback_data="adm:broadcast"),
        types.InlineKeyboardButton("📜 سجل تدقيق الأدمن", callback_data="adm:audit"),
    )
    bot.send_message(uid, "👑 لوحة تحكم المشرف:", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "adm:stats")
def cb_admin_stats(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "🚫 غير مسموح.")
    s = db.global_stats()
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    text = (
        "📊 <b>إحصائيات عامة</b>\n\n"
        f"👥 عدد المستخدمين: {s['total_users']}\n"
        f"🤖 إجمالي البوتات: {s['total_bots']}\n"
        f"🟢 البوتات الشغّالة: {s['running']}\n"
        f"⚠️ البوتات المتعطلة: {s['crashed']}\n\n"
        f"🖥 معالج السيرفر: {cpu}%\n"
        f"💾 الذاكرة: {mem.percent}% مستخدمة من {mem.total // (1024**3)}GB"
    )
    bot.answer_callback_query(call.id)
    bot.send_message(call.from_user.id, text)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:users:"))
def cb_admin_users(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "🚫 غير مسموح.")
    page = int(call.data.split(":")[2])
    users = db.get_all_users()
    per_page = 5
    chunk = users[page * per_page:(page + 1) * per_page]

    kb = types.InlineKeyboardMarkup(row_width=1)
    for u in chunk:
        label = f"{'🚫' if u['is_banned'] else '✅'} {u['username'] or u['user_id']} (بوتات: {db.count_user_bots(u['user_id'])}/{u['max_bots']})"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"adm:user:{u['user_id']}"))

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️ السابق", callback_data=f"adm:users:{page-1}"))
    if (page + 1) * per_page < len(users):
        nav.append(types.InlineKeyboardButton("التالي ➡️", callback_data=f"adm:users:{page+1}"))
    if nav:
        kb.row(*nav)

    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(f"👥 المستخدمون ({len(users)}):", call.from_user.id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(call.from_user.id, f"👥 المستخدمون ({len(users)}):", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:user:"))
def cb_admin_user_detail(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "🚫 غير مسموح.")
    target_id = int(call.data.split(":")[2])
    u = db.get_user(target_id)
    if not u:
        return bot.answer_callback_query(call.id, "غير موجود.")

    kb = types.InlineKeyboardMarkup(row_width=2)
    if u["is_banned"]:
        kb.add(types.InlineKeyboardButton("✅ رفع الحظر", callback_data=f"adm:unban:{target_id}"))
    else:
        kb.add(types.InlineKeyboardButton("🚫 حظر", callback_data=f"adm:ban:{target_id}"))
    kb.add(
        types.InlineKeyboardButton("➕ زيادة الحد", callback_data=f"adm:incmax:{target_id}"),
        types.InlineKeyboardButton("➖ إنقاص الحد", callback_data=f"adm:decmax:{target_id}"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="adm:users:0"))

    bot.answer_callback_query(call.id)
    text = (
        f"👤 المستخدم: <code>{target_id}</code> (@{u['username'] or '-'})\n"
        f"الحالة: {'🚫 محظور' if u['is_banned'] else '✅ نشط'}\n"
        f"الحد الأقصى للبوتات: {u['max_bots']}\n"
        f"عدد البوتات الحالية: {db.count_user_bots(target_id)}"
    )
    try:
        bot.edit_message_text(text, call.from_user.id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(call.from_user.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:ban:") or c.data.startswith("adm:unban:"))
def cb_admin_ban(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "🚫 غير مسموح.")
    action, target_id = call.data.split(":")[1], int(call.data.split(":")[2])
    if action == "ban":
        db.ban_user(target_id)
        db.log_admin_action(call.from_user.id, "ban_user", target_id)
        bot.answer_callback_query(call.id, "🚫 تم الحظر")
    else:
        db.unban_user(target_id)
        db.log_admin_action(call.from_user.id, "unban_user", target_id)
        bot.answer_callback_query(call.id, "✅ تم رفع الحظر")
    call.data = f"adm:user:{target_id}"
    cb_admin_user_detail(call)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:incmax:") or c.data.startswith("adm:decmax:"))
def cb_admin_maxbots(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "🚫 غير مسموح.")
    action, target_id = call.data.split(":")[1], int(call.data.split(":")[2])
    current = db.get_max_bots(target_id)
    new_val = current + 1 if action == "incmax" else max(0, current - 1)
    db.set_max_bots(target_id, new_val)
    db.log_admin_action(call.from_user.id, action, target_id, details=f"{current} -> {new_val}")
    bot.answer_callback_query(call.id, f"الحد الجديد: {new_val}")
    call.data = f"adm:user:{target_id}"
    cb_admin_user_detail(call)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:bots:"))
def cb_admin_bots(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "🚫 غير مسموح.")
    page = int(call.data.split(":")[2])
    all_bots = db.get_all_bots()
    per_page = 6
    chunk = all_bots[page * per_page:(page + 1) * per_page]

    kb = types.InlineKeyboardMarkup(row_width=1)
    for r in chunk:
        emo = status_emoji("running" if pm.is_running(r["bot_id"]) else r["status"])
        kb.add(types.InlineKeyboardButton(f"{emo} {r['name']} (👤{r['owner_id']})", callback_data=f"bot:{r['bot_id']}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️ السابق", callback_data=f"adm:bots:{page-1}"))
    if (page + 1) * per_page < len(all_bots):
        nav.append(types.InlineKeyboardButton("التالي ➡️", callback_data=f"adm:bots:{page+1}"))
    if nav:
        kb.row(*nav)

    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(f"🤖 كل البوتات ({len(all_bots)}):", call.from_user.id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(call.from_user.id, f"🤖 كل البوتات ({len(all_bots)}):", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:botsf:"))
def cb_admin_bots_filtered(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "🚫 غير مسموح.")
    _, _, status_filter, page_s = call.data.split(":")
    page = int(page_s)
    if status_filter == "running":
        all_bots = [r for r in db.get_all_bots() if pm.is_running(r["bot_id"])]
        title_label = "🟢 البوتات الشغّالة"
    else:
        all_bots = db.get_bots_by_status("crashed")
        title_label = "⚠️ البوتات المتعطلة"

    per_page = 6
    chunk = all_bots[page * per_page:(page + 1) * per_page]
    kb = types.InlineKeyboardMarkup(row_width=1)
    for r in chunk:
        emo = status_emoji("running" if pm.is_running(r["bot_id"]) else r["status"])
        kb.add(types.InlineKeyboardButton(f"{emo} {r['name']} (👤{r['owner_id']})", callback_data=f"bot:{r['bot_id']}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️ السابق", callback_data=f"adm:botsf:{status_filter}:{page-1}"))
    if (page + 1) * per_page < len(all_bots):
        nav.append(types.InlineKeyboardButton("التالي ➡️", callback_data=f"adm:botsf:{status_filter}:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("⬅️ رجوع للوحة التحكم", callback_data="adm:panel"))

    bot.answer_callback_query(call.id)
    text = f"{title_label} ({len(all_bots)}):"
    try:
        bot.edit_message_text(text, call.from_user.id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(call.from_user.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "adm:panel")
def cb_admin_panel_back(call):
    # ⚠️ ملاحظة: call.message هو رسالة أرسلها البوت نفسه سابقًا، لذا
    # call.message.from_user يكون حساب البوت وليس الأدمن — لا يجوز الاعتماد
    # عليه للتحقق من الصلاحية أو تحديد المستلم؛ نستخدم call.from_user دائمًا
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "🚫 غير مسموح.")
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 إحصائيات عامة", callback_data="adm:stats"),
        types.InlineKeyboardButton("👥 المستخدمون", callback_data="adm:users:0"),
        types.InlineKeyboardButton("🤖 كل البوتات", callback_data="adm:bots:0"),
        types.InlineKeyboardButton("🟢 البوتات الشغّالة فقط", callback_data="adm:botsf:running:0"),
        types.InlineKeyboardButton("⚠️ البوتات المتعطلة فقط", callback_data="adm:botsf:crashed:0"),
        types.InlineKeyboardButton("📢 إذاعة رسالة", callback_data="adm:broadcast"),
        types.InlineKeyboardButton("📜 سجل تدقيق الأدمن", callback_data="adm:audit"),
    )
    bot.answer_callback_query(call.id)
    bot.send_message(call.from_user.id, "👑 لوحة تحكم المشرف:", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "adm:audit")
def cb_admin_audit(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "🚫 غير مسموح.")
    entries = db.get_audit_log(limit=20)
    bot.answer_callback_query(call.id)
    if not entries:
        bot.send_message(call.from_user.id, "لا يوجد سجل تدقيق بعد.")
        return
    lines = []
    for e in entries:
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
        details = f" ({e['details']})" if e["details"] else ""
        lines.append(f"[{t}] admin={e['admin_id']} {e['action']} target={e['target']}{details}")
    send_long(call.from_user.id, "\n".join(lines), filename="audit_log.txt")


@bot.callback_query_handler(func=lambda c: c.data == "adm:broadcast")
def cb_admin_broadcast(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "🚫 غير مسموح.")
    _set_pending(call.from_user.id, {"action": "await_broadcast"})
    bot.answer_callback_query(call.id)
    bot.send_message(call.from_user.id, "📢 أرسل نص الرسالة التي تريد إذاعتها لجميع المستخدمين:")


def _send_broadcast_message(user_id, safe_text, allow_retry=True):
    """يرسل رسالة البث لمستخدم واحد. عند خطأ Flood Control (429) ننتظر المدة
    المطلوبة من تيليجرام ثم نعيد المحاولة فعليًا مرة واحدة إضافية فقط (لتفادي
    حلقة انتظار لا نهائية)، ونحتسب النتيجة بناءً على محاولة الإرسال الفعلية الثانية."""
    try:
        bot.send_message(user_id, f"📢 <b>إعلان</b>\n\n{safe_text}")
        return True
    except telebot.apihelper.ApiTelegramException as e:
        retry_after = getattr(e, "result_json", {}).get("parameters", {}).get("retry_after") \
            if isinstance(getattr(e, "result_json", None), dict) else None
        if retry_after and allow_retry:
            time.sleep(retry_after)
            return _send_broadcast_message(user_id, safe_text, allow_retry=False)
        return False
    except Exception:
        return False


@bot.message_handler(func=lambda m: pending.get(m.from_user.id, {}).get("action") == "await_broadcast")
def do_broadcast(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    pending.pop(uid, None)
    # نستثني المستخدمين المحظورين من البث الجماعي — لا داعٍ لإزعاجهم أو محاولة
    # مراسلتهم بينما هم أساسًا ممنوعون من استخدام الخدمة
    users = [u for u in db.get_all_users() if not u["is_banned"]]
    sent, failed = 0, 0
    # نُعقّم نص الإعلان لأنه سيُرسل بصيغة HTML لكل المستخدمين، ووسوم غير مغلقة
    # قد تكسر التنسيق أو تُستغل لإدراج روابط/وسوم غير مقصودة
    safe_text = html.escape(message.text)
    status_msg = bot.send_message(uid, f"⏳ جاري الإرسال لـ {len(users)} مستخدم (غير المحظورين)...")
    for u in users:
        if _send_broadcast_message(u["user_id"], safe_text):
            sent += 1
        else:
            failed += 1
        time.sleep(0.05)
    db.log_admin_action(uid, "broadcast", target="all_users", details=f"sent={sent} failed={failed}")
    bot.edit_message_text(f"✅ تم الإرسال إلى {sent} مستخدم، وفشل مع {failed}.", uid, status_msg.message_id)


# ============================================================
#                        تشغيل البوت
# ============================================================

def restore_running_bots():
    """عند إعادة تشغيل السيرفر، يعيد تشغيل كل البوتات التي كانت تعمل سابقًا.
    قبل ذلك نتحقق أن العملية القديمة (من قبل إعادة التشغيل) ليست لا تزال حيّة
    فعليًا عبر psutil.pid_exists — لأن تشغيل نسخة جديدة فوق نسخة يتيمة لا تزال
    تعمل يسبب تعارض getUpdates أو استجابات مضاعفة لنفس البوت."""
    for row in db.get_bots_by_status("running"):
        old_pid = row["pid"]
        if old_pid and psutil.pid_exists(old_pid):
            print(
                f"⚠️ العملية القديمة لبوت «{row['name']}» (PID={old_pid}) لا تزال حيّة، "
                "تخطي التشغيل لتفادي تشغيل نسخة مكررة."
            )
            continue
        pm.start_bot(row)


def _start_health_server():
    """خادم HTTP بسيط جدًا (اختياري) لا علاقة له بعمل البوت نفسه (الذي يعتمد
    على Polling ولا يحتاج أي منفذ مفتوح). الغرض الوحيد منه هو التوافق مع
    منصات الاستضافة الحاوياتية (مثل JustRunMy.App) التي قد تستخدم فتح منفذ
    للتأكد أن الحاوية "حيّة"، أو عند رغبة المستخدم لاحقًا باستخدام Webhook
    بدل Polling. يعمل فقط إن كان متغير البيئة PORT معرَّفًا من لوحة المنصة؛
    وإلا لا يتم تشغيله إطلاقًا ولا يؤثر على شيء."""
    port_str = os.environ.get("PORT", "").strip()
    if not port_str.isdigit():
        return

    port = int(port_str)

    class _HealthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, fmt, *args):
            pass  # لا داعي لتسجيل كل طلب فحص صحة في مخرجات السجل الرئيسية

    def _serve():
        try:
            server = http.server.ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
            server.serve_forever()
        except Exception as e:
            print(f"⚠️ تعذّر تشغيل خادم فحص الصحة على المنفذ {port}: {e}")

    threading.Thread(target=_serve, daemon=True).start()
    print(f"🩺 خادم فحص صحة اختياري يعمل على المنفذ {port} (لا يؤثر على وضع Polling).")


def _handle_shutdown_signal(signum, frame):
    """معالج إغلاق نظيف عند SIGTERM/SIGINT: يوقف كل البوتات المستضافة أولًا حتى
    لا تبقى عمليات subprocess يتيمة تعمل بعد إغلاق الخادم بشكل مفاجئ، وهو ما
    كان يؤدي لتشغيل نسخة ثانية فوقها عند الإقلاع التالي."""
    print(f"🛑 تم استقبال إشارة إيقاف ({signum})، جاري إيقاف كل البوتات المستضافة بأمان...")
    try:
        pm.shutdown_all()
    finally:
        sys.exit(0)


def main():
    print("🚀 تشغيل بوت الاستضافة...")

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    _start_health_server()

    restore_running_bots()

    watchdog = threading.Thread(target=pm.watchdog_loop, args=(config.WATCHDOG_INTERVAL,), daemon=True)
    watchdog.start()

    cleanup_thread = threading.Thread(target=_cleanup_stale_state_loop, daemon=True)
    cleanup_thread.start()

    print("✅ البوت يعمل الآن.")

    # حلقة خارجية حول infinity_polling: المكتبة نفسها تعيد المحاولة داخليًا عند
    # أخطاء الشبكة المؤقتة، لكن في حال حدوث استثناء غير متوقع تمامًا يوقف
    # الحلقة بالكامل، لا نريد للحاوية أن تنهار وتنتظر إعادة تشغيل المنصة —
    # نعيد تشغيل Polling فورًا بعد تأخير قصير بدل الخروج، دون التأثير على
    # البوتات المستضافة (تعمل كعمليات مستقلة تمامًا ولا تتأثر بهذه الحلقة).
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
            break  # لا يعود infinity_polling إلا عند طلب إيقاف صريح
        except Exception as e:
            print(f"❌ خطأ غير متوقع أوقف حلقة Polling: {e}\n🔁 إعادة المحاولة خلال 5 ثوانٍ...")
            time.sleep(5)


if __name__ == "__main__":
    main()
