# ------------------------------------------------------------------
# مرحله ۱: ساخت telegram-bot-api از سورس رسمی (برای Local Bot API
# Server که سقف آپلود رو از ۵۰ مگ به ۲ گیگ می‌بره بالا)
# ------------------------------------------------------------------
FROM debian:bookworm-slim AS tbapi-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake g++ make zlib1g-dev libssl-dev gperf ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --recursive https://github.com/tdlib/telegram-bot-api.git .

RUN mkdir build && cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX:PATH=/usr/local .. && \
    cmake --build . --target install -- -j"$(nproc)"

# ------------------------------------------------------------------
# مرحله ۲: ایمیج نهایی ربات
# ------------------------------------------------------------------
FROM python:3.11-slim

# نصب ffmpeg از طریق apt (روش مطمئن‌تر از nixpacks)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libssl3 zlib1g && \
    rm -rf /var/lib/apt/lists/*

# باینری telegram-bot-api از مرحله‌ی قبل
COPY --from=tbapi-builder /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
# شامل نصب yt-dlp[default] (که yt-dlp-ejs رو هم میاره) و pip-پکیج deno،
# چون از اواخر ۲۰۲۵ یوتیوب یه چالش جاوااسکریپتی گذاشته که yt-dlp بدون
# یه جاوااسکریپت‌رانتایم (Deno) نمی‌تونه حلش کنه.
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY start.sh .
RUN chmod +x start.sh

CMD ["./start.sh"]
