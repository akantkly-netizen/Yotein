#STREAMING_CHUNK: Creating Dockerfile for foolproof deployment on Railway...
FROM python:3.11-slim
#نصب ffmpeg و ابزارهای مورد نیاز
RUN apt-get update && apt-get install -y ffmpeg git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
#کپی و نصب پیش‌نیازها
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
#کپی بقیه کدها
COPY . .
#اجرای ربات
CMD ["python", "bot.py"]
