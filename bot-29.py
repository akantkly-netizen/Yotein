"""
ربات تلگرام دانلود اینستاگرام
------------------------------
کاربر لینک پست/ریلز/استوری اینستاگرام رو می‌فرسته، ربات کیفیت‌های
موجود رو به صورت دکمه نشون می‌ده. کاربر یا یه کیفیت ویدیو انتخاب
می‌کنه یا گزینه‌ی "فقط صدا" رو می‌زنه که صدا جدا استخراج و ارسال میشه.

نیازمندی‌ها (requirements.txt):
    python-telegram-bot==21.*
    yt-dlp

همچنین باید ffmpeg روی سیستم نصب باشه (برای استخراج صدا و مرج فرمت‌ها).

راه‌اندازی:
    1. یه ربات از @BotFather بسازید و توکنش رو بگیرید.
    2. متغیر محیطی TELEGRAM_BOT_TOKEN رو ست کنید یا مستقیم پایین بذارید.
    3. pip install -r requirements.txt
    4. python bot.py

محدودیت مهم:
    - سرور رسمی تلگرام برای بات‌ها آپلود فایل رو تا ۵۰ مگابایت اجازه میده.
      برای فایل‌های بزرگ‌تر باید از Local Bot API Server استفاده کنید
      (خودتون سرور تلگرام رو لوکال اجرا کنید) که سقف تا ۲ گیگابایت میشه.
    - دانلود از پست‌های خصوصی اینستاگرام بدون لاگین ممکن نیست. اگر لازم
      شد، فایل کوکی اینستاگرام (cookies.txt) رو به yt-dlp بدید
      (پایین‌تر توضیح داده شده).
    - این ابزار فقط برای محتوای خودتون یا محتوایی که اجازه‌ی استفاده
      دارید به کار ببرید؛ رعایت قوانین اینستاگرام و کپی‌رایت به عهده‌ی
      خودتونه.
"""

import logging
import os
import re
import tempfile
import shutil
from pathlib import Path

import yt_dlp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ------------------------------------------------------------------
# تنظیمات
# ------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT-YOUR-TOKEN-HERE")

# اگر پست‌های خصوصی/محدود دارید، مسیر فایل کوکی اینستاگرام رو اینجا بدید
# (با اکستنشن‌هایی مثل "Get cookies.txt" از مرورگر خودتون export بگیرید)
COOKIES_FILE = os.environ.get("INSTAGRAM_COOKIES_FILE", "")  # مثلا "cookies.txt"

MAX_TELEGRAM_UPLOAD_MB = 50  # محدودیت سرور رسمی بات تلگرام

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

INSTAGRAM_URL_RE = re.compile(
    r"(https?://)?(www\.)?instagram\.com/(p|reel|reels|stories|tv)/[\w\-/.]+"
)

# نگهداری موقت اطلاعات هر لینک تا زمانی که کاربر کیفیت رو انتخاب کنه
# key: کد کوتاه (chat_id_msgid) -> {"url":..., "formats": [...]}
PENDING = {}


# ------------------------------------------------------------------
# توابع کمکی yt-dlp
# ------------------------------------------------------------------
def build_ydl_opts(**overrides):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE
    opts.update(overrides)
    return opts


def extract_info(url: str) -> dict:
    """اطلاعات لینک (فرمت‌های موجود و ...) رو بدون دانلود می‌گیره."""
    with yt_dlp.YoutubeDL(build_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    return info


def pick_quality_options(info: dict):
    """
    از بین فرمت‌های موجود، چند گزینه‌ی معنادار برای کاربر می‌سازه:
    بهترین کیفیت، کیفیت متوسط، کیفیت پایین (اگر موجود باشه) + فقط صدا.
    """
    formats = info.get("formats") or []
    video_formats = [
        f for f in formats
        if f.get("vcodec") not in (None, "none") and f.get("height")
    ]
    # حذف تکراری‌ها بر اساس ارتفاع (height) و نگه‌داشتن بهترین بیت‌ریت هر رزولوشن
    by_height = {}
    for f in video_formats:
        h = f["height"]
        if h not in by_height or (f.get("tbr") or 0) > (by_height[h].get("tbr") or 0):
            by_height[h] = f

    sorted_heights = sorted(by_height.keys(), reverse=True)
    options = []
    for h in sorted_heights:
        f = by_height[h]
        size = f.get("filesize") or f.get("filesize_approx")
        size_txt = f" (~{size / 1024 / 1024:.1f}MB)" if size else ""
        options.append(
            {
                "label": f"🎬 {h}p{size_txt}",
                "format_id": f["format_id"],
                "kind": "video",
            }
        )

    # اگر هیچ فرمت مجزایی پیدا نشد (مثلا فقط یه فایل ترکیبی هست)
    if not options and formats:
        options.append({"label": "🎬 کیفیت پیش‌فرض", "format_id": "best", "kind": "video"})

    # گزینه‌ی فقط صدا همیشه اضافه میشه
    options.append({"label": "🎵 فقط صدا (MP3)", "format_id": "bestaudio", "kind": "audio"})
    return options


def download_media(url: str, format_id: str, kind: str, out_dir: str) -> str:
    """دانلود ویدیو با فرمت انتخابی یا استخراج فقط صدا. مسیر فایل نهایی رو برمی‌گردونه."""
    out_tmpl = os.path.join(out_dir, "%(id)s.%(ext)s")

    if kind == "audio":
        opts = build_ydl_opts(
            format="bestaudio/best",
            outtmpl=out_tmpl,
            postprocessors=[
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        )
    else:
        fmt = format_id if format_id == "best" else f"{format_id}+bestaudio/best"
        opts = build_ydl_opts(
            format=fmt,
            outtmpl=out_tmpl,
            merge_output_format="mp4",
        )

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    if kind == "audio":
        # بعد از پردازش FFmpegExtractAudio، پسوند به mp3 تغییر می‌کنه
        base, _ = os.path.splitext(filename)
        mp3_path = base + ".mp3"
        if os.path.exists(mp3_path):
            return mp3_path

    return filename


# ------------------------------------------------------------------
# هندلرهای تلگرام
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! لینک پست، ریلز یا استوری اینستاگرام رو برام بفرست 🙂\n"
        "بعدش کیفیت مورد نظرت رو از بین دکمه‌ها انتخاب کن، یا اگه فقط"
        " آهنگش رو می‌خوای، گزینه‌ی «فقط صدا» رو بزن."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    match = INSTAGRAM_URL_RE.search(text)
    if not match:
        await update.message.reply_text(
            "این یه لینک معتبر اینستاگرام نیست. لطفاً لینک پست/ریلز/استوری رو بفرست."
        )
        return

    url = match.group(0)
    status_msg = await update.message.reply_text("⏳ دارم اطلاعات لینک رو می‌گیرم...")

    try:
        info = extract_info(url)
    except Exception as e:
        logger.exception("extract_info failed")
        await status_msg.edit_text(
            "❌ نتونستم اطلاعات این لینک رو بگیرم. ممکنه پست خصوصی باشه یا"
            " لینک اشتباه باشه.\nجزئیات خطا: " + str(e)[:300]
        )
        return

    options = pick_quality_options(info)
    key = f"{update.effective_chat.id}_{status_msg.message_id}"
    PENDING[key] = {"url": url, "options": {o["format_id"] + "_" + o["kind"]: o for o in options}}

    buttons = []
    for o in options:
        cb_data = f"dl|{key}|{o['format_id']}|{o['kind']}"
        buttons.append([InlineKeyboardButton(o["label"], callback_data=cb_data)])

    title = info.get("title") or info.get("description") or "محتوای اینستاگرام"
    title = (title[:150] + "…") if len(title) > 150 else title

    await status_msg.edit_text(
        f"✅ پیدا شد: {title}\n\nکیفیت مورد نظرت رو انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, key, format_id, kind = query.data.split("|", 3)
    except ValueError:
        await query.edit_message_text("❌ خطای داخلی، دوباره لینک رو بفرست.")
        return

    entry = PENDING.get(key)
    if not entry:
        await query.edit_message_text("⌛ این درخواست منقضی شده، لطفاً لینک رو دوباره بفرست.")
        return

    url = entry["url"]
    await query.edit_message_text("⬇️ در حال دانلود... (بسته به حجم فایل ممکنه کمی طول بکشه)")

    tmp_dir = tempfile.mkdtemp(prefix="insta_dl_")
    try:
        file_path = download_media(url, format_id, kind, tmp_dir)
        size_mb = os.path.getsize(file_path) / 1024 / 1024

        if size_mb > MAX_TELEGRAM_UPLOAD_MB:
            await query.edit_message_text(
                f"❌ حجم فایل {size_mb:.1f}MB هست و از سقف {MAX_TELEGRAM_UPLOAD_MB}MB"
                " ربات‌های تلگرام رسمی بیشتره. برای فایل‌های بزرگ‌تر باید از"
                " Local Bot API Server استفاده کنی (توضیح در README)."
            )
            return

        chat_id = query.message.chat_id
        with open(file_path, "rb") as f:
            if kind == "audio":
                await context.bot.send_audio(chat_id=chat_id, audio=f)
            else:
                await context.bot.send_video(chat_id=chat_id, video=f, supports_streaming=True)

        await query.edit_message_text("✅ ارسال شد!")
    except Exception as e:
        logger.exception("download/send failed")
        await query.edit_message_text("❌ دانلود یا ارسال فایل با خطا مواجه شد:\n" + str(e)[:300])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        PENDING.pop(key, None)


def main():
    if BOT_TOKEN == "PUT-YOUR-TOKEN-HERE":
        raise SystemExit(
            "توکن ربات رو ست نکردی! متغیر محیطی TELEGRAM_BOT_TOKEN رو بذار یا"
            " مستقیم توی کد جایگزین کن."
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^dl\|"))

    logger.info("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
