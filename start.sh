#!/bin/sh
set -e

# اگه نمی‌تونی TELEGRAM_API_ID / TELEGRAM_API_HASH رو تو Variables سرور ست کنی،
# همین دو مقدار رو تو فایل local_secrets.sh بذار (کنار همین فایل):
#   export TELEGRAM_API_ID="123456"
#   export TELEGRAM_API_HASH="abcdef..."
if [ -f ./local_secrets.sh ]; then
    . ./local_secrets.sh
fi

# یه آدرس رو باز می‌کنه و اگه جواب JSON با ok=true بود موفق (0) برمی‌گردونه
http_get_ok() {
    python - "$1" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=15) as r:
        sys.exit(0 if json.load(r).get("ok") else 1)
except Exception:
    sys.exit(1)
PY
}

if [ -n "$TELEGRAM_API_ID" ] && [ -n "$TELEGRAM_API_HASH" ]; then
    echo "[start.sh] starting local telegram-bot-api server..."
    mkdir -p /data/telegram-bot-api
    telegram-bot-api \
        --api-id="$TELEGRAM_API_ID" \
        --api-hash="$TELEGRAM_API_HASH" \
        --http-port=8081 \
        --dir=/data/telegram-bot-api \
        --verbosity=1 &
    SERVER_PID=$!

    # صبر می‌کنیم سرور بالا بیاد و واقعاً به تلگرام وصل بشه (حداکثر ~۴۰ ثانیه)
    LOCAL_OK=0
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if http_get_ok "http://127.0.0.1:8081/bot${TELEGRAM_BOT_TOKEN}/getMe"; then
            LOCAL_OK=1
            break
        fi
        sleep 4
    done

    if [ "$LOCAL_OK" = "1" ]; then
        # طبق مستندات تلگرام، قبل از استفاده از سرور محلی باید ربات از سرور
        # ابری (api.telegram.org) logOut بشه، وگرنه تضمینی نیست آپدیت‌ها
        # برسن. بعدش تا ۱۰ دقیقه نمی‌شه دوباره به سرور ابری برگشت.
        echo "[start.sh] local server is healthy; logging the bot out of the cloud Bot API..."
        http_get_ok "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/logOut" \
            || echo "[start.sh] logOut returned not-ok (ignored; maybe already logged out)"
    else
        echo "[start.sh] local server did not become healthy (wrong api_id/api_hash?);"
        echo "[start.sh] falling back to the official Telegram server (50MB limit)."
        kill "$SERVER_PID" 2>/dev/null || true
        unset TELEGRAM_API_ID TELEGRAM_API_HASH
    fi
else
    echo "[start.sh] TELEGRAM_API_ID/TELEGRAM_API_HASH ست نشدن؛"
    echo "[start.sh] از سرور رسمی تلگرام (سقف ۵۰ مگ) استفاده می‌شه."
fi

echo "[start.sh] starting bot..."
exec python bot.py
