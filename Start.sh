#!/bin/sh
set -e

if [ -n "$TELEGRAM_API_ID" ] && [ -n "$TELEGRAM_API_HASH" ]; then
    echo "[start.sh] starting local telegram-bot-api server..."
    mkdir -p /data/telegram-bot-api
    telegram-bot-api \
        --api-id="$TELEGRAM_API_ID" \
        --api-hash="$TELEGRAM_API_HASH" \
        --http-port=8081 \
        --dir=/data/telegram-bot-api \
        --verbosity=1 &

    # چند ثانیه صبر می‌کنیم سرور بالا بیاد قبل از استارت ربات
    sleep 5
else
    echo "[start.sh] TELEGRAM_API_ID/TELEGRAM_API_HASH ست نشدن؛"
    echo "[start.sh] از سرور رسمی تلگرام (سقف ۵۰ مگ) استفاده می‌شه."
fi

echo "[start.sh] starting bot..."
exec python bot.py
