import hashlib
import os
import py_compile
import re
import shutil
import subprocess
import sys
import zipfile

from config import BOTS_DIR, MAX_EXTRACTED_SIZE_MB, MAX_ZIP_FILE_COUNT, VENV_SETUP_TIMEOUT_SECONDS


def make_bot_folder(owner_id: int, bot_id: int) -> str:
    folder = os.path.join(BOTS_DIR, str(owner_id), f"bot_{bot_id}")
    os.makedirs(folder, exist_ok=True)
    return folder


def make_temp_upload_folder(owner_id: int, temp_token: str) -> str:
    """مجلد مؤقت لاستقبال ملف/أرشيف البوت قبل إنشاء صف البوت في قاعدة البيانات
    (نؤجل الإدراج في القاعدة حتى نتأكد من نجاح الاستخراج لتفادي "بوتات شبح" فارغة)."""
    folder = os.path.join(BOTS_DIR, str(owner_id), f"tmp_{temp_token}")
    os.makedirs(folder, exist_ok=True)
    return folder


def finalize_bot_folder(temp_folder: str, owner_id: int, bot_id: int) -> str:
    """ينقل مجلد الرفع المؤقت إلى مساره النهائي (bot_<bot_id>) بعد نجاح إنشاء
    صف البوت في قاعدة البيانات."""
    final_folder = os.path.join(BOTS_DIR, str(owner_id), f"bot_{bot_id}")
    if os.path.exists(final_folder):
        shutil.rmtree(final_folder, ignore_errors=True)
    shutil.move(temp_folder, final_folder)
    return final_folder


def safe_filename(fname: str, default: str = "bot_file.py") -> str:
    """يعقّم اسم ملف قادم مباشرة من المستخدم عبر تيليجرام: يأخذ اسم الملف فقط
    (بدون أي جزء من المسار) عبر os.path.basename، ويرفض أي محاولة اجتياز مسار
    (Path Traversal) أو اسمًا فارغًا بعد التعقيم، لأن اسم الملف قد يحتوي أجزاء
    مسار مثل ../../../etc/x تكتب خارج مجلد البوت المخصَّص لو استُخدمت كما هي."""
    name = os.path.basename((fname or "").strip())
    if not name or name in (".", "..") or ".." in name:
        return default
    return name


# نمط اسم ملف آمن للاستخدام في مسارات (مثل ملفات السجل المؤقتة المُرسَلة عبر
# تيليجرام) عندما يكون مصدر الاسم نصًا حرًا أدخله المستخدم (مثل اسم البوت)
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9\u0600-\u06FF _.-]+")


def safe_display_filename(raw_name: str, suffix: str = "", default: str = "file") -> str:
    """يحوّل نصًا حرًا (مثل اسم بوت أدخله المستخدم) إلى اسم ملف آمن للاستخدام
    داخل مسار على القرص: يزيل فواصل المسار وأي محارف قد تُستغل لاجتياز مسار
    (Path Traversal) أو للتضارب مع مسارات أخرى، ويحتفظ بالحروف العربية/اللاتينية
    والأرقام والمسافات والشرطات فقط."""
    name = os.path.basename((raw_name or "").strip())
    name = name.replace("..", "")
    name = _UNSAFE_FILENAME_CHARS.sub("", name).strip()
    if not name:
        name = default
    return f"{name}{suffix}"[:120]


class UnsafeZipError(Exception):
    """يُرفع عند اكتشاف أرشيف يحاول الخروج من مجلد الوجهة أو يتجاوز الحجم المسموح."""


def _safe_member_path(dest_folder: str, member_name: str) -> str:
    """يتحقق أن مسار العضو داخل الأرشيف لا يخرج عن dest_folder (حماية من Zip Slip)."""
    dest_root = os.path.realpath(dest_folder)
    target = os.path.realpath(os.path.join(dest_root, member_name))
    if target != dest_root and not target.startswith(dest_root + os.sep):
        raise UnsafeZipError(f"مسار غير آمن داخل الأرشيف: {member_name}")
    return target


def extract_zip(zip_path: str, dest_folder: str):
    """يستخرج أرشيف zip بأمان: يمنع Zip Slip (الخروج عن المجلد) و Zip Bomb (تضخم الحجم)."""
    max_bytes = MAX_EXTRACTED_SIZE_MB * 1024 * 1024
    with zipfile.ZipFile(zip_path, "r") as z:
        infos = z.infolist()

        # حماية إضافية من Zip Bomb عبر عدد هائل من الملفات الصغيرة/الفارغة —
        # الحجم الكلي وحده لا يكفي كمقياس لأن ملايين الملفات الفارغة تستنزف
        # الـ inodes وتُبطئ الاستخراج حتى لو كان الحجم الإجمالي صغيرًا
        if len(infos) > MAX_ZIP_FILE_COUNT:
            raise UnsafeZipError(
                f"عدد الملفات داخل الأرشيف ({len(infos)}) يتجاوز الحد المسموح ({MAX_ZIP_FILE_COUNT})."
            )

        # تحقق مسبق من كل المسارات + الحجم الكلي قبل استخراج أي شيء
        total_size = 0
        for info in infos:
            _safe_member_path(dest_folder, info.filename)
            total_size += info.file_size
            if total_size > max_bytes:
                raise UnsafeZipError(
                    f"حجم الأرشيف بعد فك الضغط يتجاوز الحد المسموح ({MAX_EXTRACTED_SIZE_MB}MB)."
                )

        for info in infos:
            target = _safe_member_path(dest_folder, info.filename)
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

    os.remove(zip_path)


def find_entry_file(folder: str):
    """يحاول إيجاد ملف التشغيل الرئيسي للبوت داخل المجلد"""
    preferred = ["main.py", "bot.py", "app.py", "run.py", "start.py"]
    all_py = []
    for root, dirs, files in os.walk(folder):
        # تجاهل مجلد البيئة الافتراضية إن وجد مسبقًا
        dirs[:] = [d for d in dirs if d != ".venv"]
        for f in files:
            if f.endswith(".py"):
                full = os.path.join(root, f)
                all_py.append(full)
                if f in preferred:
                    return full
    # لا يوجد اسم مفضّل: نفرز أبجديًا لضمان اختيار حتمي وقابل لإعادة الإنتاج
    # بدل الاعتماد على ترتيب os.walk غير المضمون عبر أنظمة الملفات المختلفة
    all_py.sort()
    return all_py[0] if all_py else None


def check_syntax(entry_path: str):
    """يتحقق من صحة صياغة ملف بايثون قبل أول تشغيل، عبر py_compile (بدون
    تنفيذ الكود فعليًا). يرجع (True, None) إن كانت الصياغة سليمة، أو
    (False, رسالة الخطأ) إن كان هناك خطأ صياغي — لإظهاره للمستخدم فورًا بدل
    اكتشافه بعد محاولة تشغيل فاشلة."""
    try:
        # ⚠️ ملاحظة: quiet=2 في py_compile.compile له سلوك غير بديهي — يمنع رفع
        # الاستثناء تمامًا حتى مع doraise=True (وليس مجرد كتم رسالة الطباعة كما
        # قد يُتوقَّع)، لذلك نستخدم quiet=1 (يكتم الطباعة على stderr لكن يحافظ
        # على رفع PyCompileError بشكل طبيعي عبر doraise)
        py_compile.compile(entry_path, doraise=True, quiet=1)
        return True, None
    except py_compile.PyCompileError as e:
        return False, str(e.exc_value)
    except (SyntaxError, ValueError) as e:
        return False, str(e)
    except Exception:
        # أي خطأ غير متوقع في الفحص نفسه لا يجب أن يمنع محاولة التشغيل العادية
        return True, None


def venv_path(folder: str) -> str:
    return os.path.join(folder, ".venv")


def venv_python(folder: str) -> str:
    v = venv_path(folder)
    if os.name == "nt":
        return os.path.join(v, "Scripts", "python.exe")
    return os.path.join(v, "bin", "python")


def _file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def ensure_venv(folder: str, log_path: str):
    """ينشئ بيئة افتراضية خاصة بالبوت ويثبّت المتطلبات إن وُجد requirements.txt.
    يتخطى إعادة التثبيت إذا لم يتغيّر محتوى requirements.txt منذ آخر تشغيل ناجح."""
    v = venv_path(folder)
    timeout = VENV_SETUP_TIMEOUT_SECONDS
    with open(log_path, "a", encoding="utf-8") as log:
        try:
            if not os.path.exists(v):
                log.write(">>> إنشاء بيئة افتراضية جديدة...\n")
                log.flush()
                subprocess.run(
                    [sys.executable, "-m", "venv", v],
                    check=True, stdout=log, stderr=log, timeout=timeout,
                )

            py = venv_python(folder)
            req = os.path.join(folder, "requirements.txt")
            hash_marker = os.path.join(v, ".requirements.sha256")

            if not os.path.exists(req):
                log.write(">>> لا يوجد requirements.txt — سيتم التشغيل بدون تثبيت مكتبات إضافية.\n")
                return

            current_hash = _file_hash(req)
            previous_hash = None
            if os.path.exists(hash_marker):
                with open(hash_marker, "r", encoding="utf-8") as f:
                    previous_hash = f.read().strip()

            if current_hash == previous_hash:
                log.write(">>> requirements.txt لم يتغيّر — تخطي إعادة التثبيت.\n")
                return

            log.write(">>> تثبيت المتطلبات من requirements.txt ...\n")
            log.flush()
            subprocess.run(
                [py, "-m", "pip", "install", "--upgrade", "pip"],
                stdout=log, stderr=log, timeout=timeout,
            )
            result = subprocess.run(
                [py, "-m", "pip", "install", "-r", req],
                stdout=log, stderr=log, timeout=timeout,
            )
            if result.returncode == 0:
                with open(hash_marker, "w", encoding="utf-8") as f:
                    f.write(current_hash)
        except subprocess.TimeoutExpired as e:
            log.write(f">>> ⏱ انتهت المهلة أثناء تجهيز البيئة: {e}\n")
            raise


def tail_file(path: str, n: int = 60) -> str:
    if not os.path.exists(path):
        return "لا يوجد سجل بعد."
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        content = "".join(lines[-n:])
        return content or "السجل فارغ."
    except Exception as e:
        return f"تعذر قراءة السجل: {e}"


def human_size(num_bytes: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def human_uptime(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} س {m} د"
    if m:
        return f"{m} د {s} ث"
    return f"{s} ث"


def rotate_log_if_needed(log_path: str, max_size_mb: int):
    """إن تجاوز حجم ملف السجل الحد الأقصى، يقتطعه ويحتفظ بآخر نصف الحجم المسموح
    فقط (بدل حذفه بالكامل أو تركه ينمو بلا حدود)، حتى لا يفقد المستخدم كل
    تاريخ سجله دفعة واحدة عند كل تجاوز."""
    try:
        if not os.path.exists(log_path):
            return
        max_bytes = max_size_mb * 1024 * 1024
        size = os.path.getsize(log_path)
        if size <= max_bytes:
            return
        keep_bytes = max(max_bytes // 2, 1024)
        with open(log_path, "rb") as f:
            if size > keep_bytes:
                f.seek(-keep_bytes, os.SEEK_END)
            tail_data = f.read()
        with open(log_path, "wb") as f:
            f.write(
                "…\n[⚠️ تم اقتطاع بداية السجل تلقائيًا لتجاوزه الحد الأقصى للحجم]\n…\n\n"
                .encode("utf-8")
            )
            f.write(tail_data)
    except Exception:
        # لا نُفشل تشغيل البوت بسبب مشكلة في تدوير السجل
        pass


def delete_folder(folder: str):
    shutil.rmtree(folder, ignore_errors=True)


def export_bot_zip(folder: str, dest_zip_path_no_ext: str) -> str:
    """يضغط مجلد بوت (باستثناء .venv وملف السجل) إلى أرشيف zip للتنزيل.
    يرجع مسار الملف الناتج."""
    tmp_export_dir = dest_zip_path_no_ext + "_export_src"
    if os.path.exists(tmp_export_dir):
        shutil.rmtree(tmp_export_dir, ignore_errors=True)
    shutil.copytree(
        folder, tmp_export_dir,
        ignore=shutil.ignore_patterns(".venv", "run.log", "__pycache__", "*.pyc"),
    )
    archive_path = shutil.make_archive(dest_zip_path_no_ext, "zip", tmp_export_dir)
    shutil.rmtree(tmp_export_dir, ignore_errors=True)
    return archive_path


def make_resource_limiter(max_memory_mb: int, max_cpu_seconds: int):
    """يرجع دالة preexec_fn لتمريرها إلى subprocess.Popen لتقييد ذاكرة/معالج
    العملية الفرعية (تعمل على Linux فقط عبر وحدة resource)."""
    if os.name == "nt":
        return None
    try:
        import resource
    except ImportError:
        return None

    def _limit():
        try:
            mem_bytes = max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            resource.setrlimit(resource.RLIMIT_CPU, (max_cpu_seconds, max_cpu_seconds))
        except Exception:
            # إن فشل ضبط الحد (صلاحيات مثلاً)، نكمل بدون تقييد بدل تعطيل التشغيل بالكامل
            pass

    return _limit
