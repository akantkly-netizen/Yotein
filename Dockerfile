FROM python:3.11-slim

# نصب FFmpeg و ابزارهای مورد نیاز سیستم‌عامل
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# تنظیم پوشه کاری
WORKDIR /app

# نصب پیش‌نیازهای پایتون
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کپی کردن تمام فایل‌های پروژه
COPY . .

# اجرای ربات
CMD ["python", "main.py"]
