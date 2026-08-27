# ============================================================
# Dockerfile — بوت استضافة بوتات تيليجرام
# مُجهَّز للنشر على منصات الحاويات (مثل JustRunMy.App) للعمل 24 ساعة بدون توقف
# ============================================================

FROM python:3.11-slim

# مخرجات بايثون تُطبع فورًا بدون تخزين مؤقت (buffering)، حتى تظهر السجلات
# في لوحة "Logs" الخاصة بالمنصة لحظيًا بدل تأخرها أو ضياعها عند إعادة تشغيل
# الحاوية
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# أدوات بناء أساسية: بعض البوتات المستضافة قد تحتاج تجميع حزم بايثون من
# المصدر (لا تتوفر لها عجلات wheel جاهزة)، بالإضافة لـ tzdata لضبط منطقة
# زمنية صحيحة في السجلات، و procps توفر أدوات فحص عمليات مفيدة عند التصحيح
# عبر الطرفية المدمجة في لوحة المنصة
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        tzdata \
        procps \
        curl \
    && rm -rf /var/lib/apt/lists/*

# تثبيت متطلبات بوت الاستضافة نفسه أولًا (طبقة منفصلة قابلة للتخزين المؤقت،
# لا تُعاد إلا عند تغيّر requirements.txt فعليًا)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي كود المشروع
COPY . .

# مستخدم غير جذر (Best Practice أمنيًا) — مع منح ملكية كاملة لمجلد التطبيق
# لأن البوت ينشئ بيئات venv ويكتب ملفات وقاعدة بيانات SQLite أثناء التشغيل
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/uploaded_bots \
    && chown -R appuser:appuser /app
USER appuser

# اختياري: تُستخدم فقط إن عرّفت لوحة المنصة متغير بيئة PORT (مثلًا عند
# التحويل لاحقًا لوضع Webhook بدل Polling، أو كنقطة فحص صحة بسيطة).
# وضع Polling الافتراضي لا يحتاج فتح أي منفذ إطلاقًا.
EXPOSE 8080

# صيغة exec (وليست shell) حتى تصل إشارات SIGTERM/SIGINT مباشرة لعملية
# بايثون (PID 1) ليُنفَّذ معالج الإغلاق النظيف في main.py الذي يوقف كل
# البوتات المستضافة بأمان قبل إعادة تشغيل الحاوية
CMD ["python", "main.py"]
