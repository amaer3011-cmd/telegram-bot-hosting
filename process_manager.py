import os
import subprocess
import threading
import time
import urllib.request

import psutil

import config
import database as db
from utils import (
    venv_python, ensure_venv, human_size, human_uptime,
    make_resource_limiter, rotate_log_if_needed,
)


class ProcessManager:
    def __init__(self, bot_notifier=None):
        self.processes = {}  # bot_id -> subprocess.Popen
        self.lock = threading.Lock()
        self.notifier = bot_notifier  # دالة لإرسال إشعار لصاحب البوت: notifier(user_id, text)
        self._recovering = set()  # bot_ids قيد محاولة إعادة تشغيل تلقائي حاليًا (لمنع التكرار المتوازي)
        self._recovering_lock = threading.Lock()
        # يحدّ من عدد عمليات تجهيز venv/pip install المتزامنة على مستوى السيرفر
        # كله، حتى لا يُنهك عدة مستخدمين يضغطون "تشغيل" في نفس اللحظة المعالج/الشبكة
        self._venv_semaphore = threading.Semaphore(config.MAX_CONCURRENT_VENV_SETUPS)

    @staticmethod
    def _log_path(folder):
        return os.path.join(folder, "run.log")

    # ---------------- تشغيل / إيقاف ----------------

    def start_bot(self, bot_row):
        bot_id = bot_row["bot_id"]
        folder = bot_row["folder"]
        entry = bot_row["entry_file"]
        log_path = self._log_path(folder)

        if not folder or not entry or not os.path.exists(entry):
            return False, "ملفات البوت غير موجودة."

        with self.lock:
            existing = self.processes.get(bot_id)
            if existing and existing.poll() is None:
                return False, "البوت يعمل بالفعل."

        # نحصر عدد عمليات تجهيز البيئة الافتراضية المتزامنة عبر Semaphore عام؛
        # طلبات إضافية تنتظر دورها بدل إطلاق pip install كثيرة معًا
        with self._venv_semaphore:
            try:
                ensure_venv(folder, log_path)
            except Exception as e:
                db.set_last_error(bot_id, str(e))
                return False, f"فشل تجهيز البيئة الافتراضية: {e}"

        py = venv_python(folder)
        env = os.environ.copy()
        env.update(db.get_env_vars(bot_id))
        # نعيد فرض القيم الأصلية الآمنة لمتغيرات البيئة المحمية بعد دمج متغيرات
        # المستخدم، لأن السماح للمستخدم بالكتابة فوق مفاتيح مثل PATH أو
        # LD_PRELOAD قد يغيّر سلوك تشغيل بايثون نفسه أو يُستغل للتحايل على العزل
        for protected_key in config.PROTECTED_ENV_KEYS:
            original_value = os.environ.get(protected_key)
            if original_value is None:
                env.pop(protected_key, None)
            else:
                env[protected_key] = original_value
        env["PYTHONUNBUFFERED"] = "1"

        # اقتطاع السجل إن تجاوز الحد الأقصى للحجم قبل فتحه للإلحاق، لمنعه من
        # النمو بلا حدود واستهلاك مساحة القرص مع الوقت
        rotate_log_if_needed(log_path, config.MAX_LOG_SIZE_MB)

        log_file = open(log_path, "a", encoding="utf-8")
        log_file.write(f"\n\n===== بدء التشغيل {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        log_file.flush()

        # حد الذاكرة: نستخدم الحد المخصَّص لهذا البوت إن وُجد، وإلا القيمة الافتراضية العامة
        try:
            bot_memory_mb = bot_row["max_memory_mb"]
        except (KeyError, IndexError):
            bot_memory_mb = None
        memory_limit_mb = bot_memory_mb or config.MAX_BOT_MEMORY_MB
        limiter = make_resource_limiter(memory_limit_mb, config.MAX_BOT_CPU_SECONDS)

        try:
            proc = subprocess.Popen(
                [py, entry],
                cwd=folder,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                preexec_fn=limiter,
                # جلسة جديدة مستقلة حتى لا ترث العملية الفرعية إشارات الإنهاء
                # (SIGTERM/SIGINT) الموجَّهة لعملية الخادم الرئيسية عن طريق الخطأ؛
                # stop_bot/shutdown_all ما زالا يوقفانها يدويًا بشكل صريح دائمًا
                start_new_session=True,
            )
        except Exception as e:
            db.set_last_error(bot_id, str(e))
            return False, f"فشل تشغيل البوت: {e}"

        with self.lock:
            self.processes[bot_id] = proc

        db.update_status(bot_id, "running")
        db.update_pid(bot_id, proc.pid)
        db.set_last_error(bot_id, None)
        db.update_last_started(bot_id)
        return True, "تم تشغيل البوت بنجاح ✅"

    def stop_bot(self, bot_id, mark_stopped=True):
        with self.lock:
            proc = self.processes.get(bot_id)

        if proc and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass

        with self.lock:
            self.processes.pop(bot_id, None)

        if mark_stopped:
            db.update_status(bot_id, "stopped")
            db.update_pid(bot_id, None)
        return True, "تم إيقاف البوت ⏹"

    def restart_bot(self, bot_row):
        self.stop_bot(bot_row["bot_id"], mark_stopped=False)
        time.sleep(1)
        return self.start_bot(bot_row)

    def is_running(self, bot_id) -> bool:
        with self.lock:
            proc = self.processes.get(bot_id)
        return bool(proc and proc.poll() is None)

    def get_usage(self, bot_id):
        with self.lock:
            proc = self.processes.get(bot_id)
        if not proc or proc.poll() is not None:
            return None
        try:
            p = psutil.Process(proc.pid)
            cpu = p.cpu_percent(interval=0.3)
            mem = p.memory_info().rss
            uptime = time.time() - p.create_time()
            return {
                "cpu": f"{cpu:.1f}%",
                "mem": human_size(mem),
                "uptime": human_uptime(uptime),
            }
        except Exception:
            return None

    def check_health(self, bot_id):
        """يستدعي getMe بتوكن البوت الفرعي (إن وُجد كمتغير بيئة BOT_TOKEN_ENV_KEY)
        للتأكد أنه متصل فعليًا بتيليجرام، وليس فقط أن عمليته حيّة على مستوى نظام
        التشغيل. يرجع True/False، أو None إن لم يوجد توكن معرَّف فنتخطى الفحص."""
        if not self.is_running(bot_id):
            return None
        token = db.get_env_vars(bot_id).get(config.BOT_TOKEN_ENV_KEY)
        if not token:
            return None
        try:
            url = f"https://api.telegram.org/bot{token}/getMe"
            with urllib.request.urlopen(url, timeout=config.HEALTH_CHECK_TIMEOUT_SECONDS) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ---------------- الحارس التلقائي (Watchdog) ----------------

    def watchdog_loop(self, interval):
        while True:
            time.sleep(interval)
            try:
                self._check_all()
            except Exception:
                pass
            try:
                self._record_usage_snapshots()
            except Exception:
                pass
            try:
                self._apply_scheduled_restarts()
            except Exception:
                pass

    def _record_usage_snapshots(self):
        """يأخذ لقطة من استهلاك CPU/RAM لكل بوت شغّال ويحفظها في سجل تاريخي
        بسيط (مع الاحتفاظ بآخر USAGE_HISTORY_MAX_POINTS نقطة فقط لكل بوت)،
        لتمكين عرض اتجاه الاستهلاك بمرور الوقت بدل رقم لحظي واحد فقط."""
        with self.lock:
            bot_ids = list(self.processes.keys())
        for bot_id in bot_ids:
            usage = self.get_usage(bot_id)
            if not usage:
                continue
            try:
                cpu_val = float(usage["cpu"].replace("%", ""))
            except (ValueError, KeyError):
                continue
            with self.lock:
                proc = self.processes.get(bot_id)
            if not proc:
                continue
            try:
                mem_mb = psutil.Process(proc.pid).memory_info().rss / (1024 * 1024)
            except Exception:
                continue
            db.add_usage_point(bot_id, cpu_val, mem_mb, config.USAGE_HISTORY_MAX_POINTS)

    def _apply_scheduled_restarts(self):
        """يعيد تشغيل أي بوت مفعّل له فاصل إعادة تشغيل دوري (restart_interval_hours)
        متى ما تجاوز وقت تشغيله الحالي هذا الفاصل — مفيد لبوتات تحتاج تنظيف
        دوري لحالتها الداخلية أو تجنّب تراكم تسريبات ذاكرة بطيئة بمرور الوقت."""
        now = int(time.time())
        for bot_row in db.get_bots_with_scheduled_restart():
            bot_id = bot_row["bot_id"]
            if not self.is_running(bot_id):
                continue
            last_started = bot_row["last_started_at"] or now
            interval_seconds = bot_row["restart_interval_hours"] * 3600
            if now - last_started < interval_seconds:
                continue
            ok, _ = self.restart_bot(bot_row)
            if ok and self.notifier:
                self.notifier(
                    bot_row["owner_id"],
                    f"⏰ تمت إعادة تشغيل بوتك «{bot_row['name']}» تلقائيًا حسب الجدولة "
                    f"({bot_row['restart_interval_hours']} ساعة).",
                )

    def _check_all(self):
        running_in_db = db.get_bots_by_status("running")
        for bot_row in running_in_db:
            bot_id = bot_row["bot_id"]
            if self.is_running(bot_id):
                continue  # البوت يعمل فعليًا، لا شيء لفعله

            with self._recovering_lock:
                if bot_id in self._recovering:
                    continue  # محاولة إعادة تشغيل لهذا البوت جارية بالفعل
                self._recovering.add(bot_id)

            # البوت متوقف فعليًا رغم أنه مسجَّل "running" في قاعدة البيانات => توقف غير متوقع
            if not bot_row["auto_restart"]:
                db.update_status(bot_id, "crashed")
                if self.notifier:
                    self.notifier(
                        bot_row["owner_id"],
                        f"❌ توقف بوتك «{bot_row['name']}» بشكل غير متوقع، "
                        f"وخاصية إعادة التشغيل التلقائي معطّلة له.",
                    )
                with self._recovering_lock:
                    self._recovering.discard(bot_id)
                continue

            # ننفّذ اكتشاف/تسجيل التعطل ومحاولة الإصلاح في خيط منفصل حتى لا نحجب
            # فحص بقية البوتات في نفس دورة الـ watchdog أثناء انتظار التأخير التصاعدي
            threading.Thread(target=self._handle_crash, args=(bot_row,), daemon=True).start()

    def _handle_crash(self, bot_row):
        bot_id = bot_row["bot_id"]
        try:
            crash_count = db.register_crash(bot_id, config.CRASH_LOOP_RESET_SECONDS)

            if crash_count > config.MAX_AUTO_RESTART_ATTEMPTS:
                # حلقة تعطل متكررة: نوقف إعادة التشغيل التلقائي تلقائيًا لحماية السيرفر
                db.update_status(bot_id, "crashed")
                db.set_auto_restart(bot_id, False)
                db.set_last_error(
                    bot_id,
                    f"تم تعطيل إعادة التشغيل التلقائي بعد {crash_count} تعطلات متتالية "
                    f"خلال {config.CRASH_LOOP_RESET_SECONDS} ثانية (حلقة تعطل).",
                )
                if self.notifier:
                    self.notifier(
                        bot_row["owner_id"],
                        f"🚫 بوتك «{bot_row['name']}» يدخل في حلقة تعطل متكررة "
                        f"({crash_count} مرات متتالية). تم إيقاف إعادة التشغيل التلقائي تلقائيًا "
                        f"لحماية السيرفر. راجع السجلات وأعد التشغيل يدويًا بعد الإصلاح.",
                    )
                return

            # تأخير تصاعدي بسيط قبل إعادة المحاولة (يزداد مع تكرار التعطل)، بحد أقصى 120 ثانية
            backoff = min(config.RESTART_BACKOFF_BASE_SECONDS * (2 ** (crash_count - 1)), 120)
            time.sleep(backoff)

            # إعادة قراءة الصف: قد يكون المستخدم تدخّل يدويًا أثناء الانتظار (أوقف/شغّل/حذف)
            fresh_row = db.get_bot(bot_id)
            if not fresh_row or self.is_running(bot_id) or not fresh_row["auto_restart"]:
                return

            ok, msg = self.start_bot(fresh_row)
            db.increment_restart_count(bot_id)
            if self.notifier:
                status = "تمت إعادة تشغيله تلقائيًا 🔁" if ok else f"فشلت إعادة التشغيل: {msg}"
                self.notifier(
                    fresh_row["owner_id"],
                    f"⚠️ توقف بوتك «{fresh_row['name']}» بشكل غير متوقع "
                    f"(المحاولة {crash_count}/{config.MAX_AUTO_RESTART_ATTEMPTS}).\n{status}",
                )
        finally:
            with self._recovering_lock:
                self._recovering.discard(bot_id)

    def shutdown_all(self):
        with self.lock:
            ids = list(self.processes.keys())
        for bid in ids:
            self.stop_bot(bid, mark_stopped=False)
