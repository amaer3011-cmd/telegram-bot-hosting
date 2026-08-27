# ============================================================
# Dockerfile — بوت استضافة بوتات تيليجرام
# مُجهَّز للنشر على منصات الحاويات مثل Railway للعمل باستمرار
# ============================================================

FROM python:3.11-slim

# مخرجات بايثون تُطبع فورًا حتى تظهر السجلات لحظيًا في لوحة المنصة
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data

WORKDIR /app

# أدوات البناء مطلوبة لبعض الحزم التي قد ترفعها البوتات المستضافة.
# gosu يسمح ببدء الحاوية كجذر لضبط Volume ثم تشغيل التطبيق كمستخدم غير جذر.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        tzdata \
        procps \
        curl \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# تثبيت متطلبات بوت الاستضافة نفسه في طبقة قابلة للتخزين المؤقت.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ كود المشروع. بيانات التشغيل تُحفظ في /app/data خارج صورة البناء.
COPY . .
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# مستخدم غير جذر لتقليل أثر أي كود مرفوع من المستخدمين.
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/data /app/uploaded_bots \
    && chmod 755 /usr/local/bin/entrypoint.sh \
    && chown -R appuser:appuser /app

# Railway يحقن PORT تلقائيًا، والتطبيق يستمع عليه عند وجوده.
EXPOSE 8080

# يبدأ السكربت كجذر لضبط Volume ثم يستبدل نفسه بعملية Python تحت appuser.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "main.py"]
