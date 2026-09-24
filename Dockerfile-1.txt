FROM python:3.11-slim

# نصب ffmpeg از طریق apt (روش مطمئن‌تر از nixpacks)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# شامل نصب yt-dlp[default] (که yt-dlp-ejs رو هم میاره) و pip-پکیج deno،
# چون از اواخر ۲۰۲۵ یوتیوب یه چالش جاوااسکریپتی گذاشته که yt-dlp بدون
# یه جاوااسکریپت‌رانتایم (Deno) نمی‌تونه حلش کنه.
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
