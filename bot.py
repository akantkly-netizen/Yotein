"""
ربات تلگرام دانلود اینستاگرام + یوتیوب
----------------------------------------
کاربر لینک پست/ریلز/استوری اینستاگرام، ویدیو/شورتز یوتیوب، یا ویدیوی
تیک‌تاک رو می‌فرسته،
ربات کیفیت‌های موجود رو به صورت دکمه (با حجم دقیق) نشون می‌ده. کاربر یا
یه کیفیت ویدیو انتخاب می‌کنه یا گزینه‌ی "فقط صدا" رو می‌زنه که صدا جدا
استخراج و ارسال میشه. حین دانلود، یه نوار پیشرفت زنده (درصد + حجم
دانلودشده/کل) نمایش داده می‌شه.

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
    - دانلود از پست‌های خصوصی اینستاگرام یا ویدیوهای محدود یوتیوب بدون
      لاگین ممکن نیست. اگر لازم شد، فایل کوکی (cookies.txt) رو به
      yt-dlp بدید (پایین‌تر توضیح داده شده).
    - این ابزار فقط برای محتوای خودتون یا محتوایی که اجازه‌ی استفاده
      دارید به کار ببرید؛ رعایت قوانین پلتفرم‌ها و کپی‌رایت به عهده‌ی
      خودتونه.
"""

import asyncio
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
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
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

# اگر پست‌های خصوصی/محدود دارید، مسیر فایل کوکی رو اینجا بدید
# (با اکستنشن‌هایی مثل "Get cookies.txt" از مرورگر خودتون export بگیرید)
COOKIES_FILE = os.environ.get("INSTAGRAM_COOKIES_FILE", "")  # مثلا "cookies.txt"

MAX_TELEGRAM_UPLOAD_MB = 50  # محدودیت سرور رسمی بات تلگرام
PROGRESS_UPDATE_INTERVAL = 2.0  # ثانیه، فاصله‌ی به‌روزرسانی نوار پیشرفت
PROGRESS_BAR_LENGTH = 18  # تعداد بلوک‌های نوار پیشرفت

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

INSTAGRAM_URL_RE = re.compile(
    r"(https?://)?(www\.)?instagram\.com/(p|reel|reels|stories|tv)/[\w\-/.]+"
)
YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.|m\.)?youtube\.com/(watch\?v=|shorts/|embed/|live/)[\w\-]+[\w\-?&=%.]*"
    r"|(https?://)?youtu\.be/[\w\-]+[\w\-?&=%.]*"
)
TIKTOK_URL_RE = re.compile(
    r"(https?://)?(www\.|m\.)?tiktok\.com/@[\w.\-]+/video/\d+[\w\-?&=%.]*"
    r"|(https?://)?(vm|vt)\.tiktok\.com/[\w\-]+/?[\w\-?&=%.]*"
    r"|(https?://)?(www\.)?tiktok\.com/t/[\w\-]+/?[\w\-?&=%.]*"
)
SUPPORTED_URL_RE = re.compile(
    f"({INSTAGRAM_URL_RE.pattern})|({YOUTUBE_URL_RE.pattern})|({TIKTOK_URL_RE.pattern})"
)

# ------------------------------------------------------------------
# کیبورد پایین صفحه (Reply Keyboard)
# ------------------------------------------------------------------
BTN_HELP = "📖 راهنما"
BTN_ABOUT = "ℹ️ درباره ربات"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_HELP, BTN_ABOUT]],
    resize_keyboard=True,   # دکمه‌ها رو کوچیک و جمع‌وجور نشون می‌ده
    is_persistent=True,     # همیشه روی صفحه بمونه
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


def format_size(num_bytes) -> str:
    """بایت رو به MB/KB خوانا تبدیل می‌کنه."""
    if not num_bytes:
        return ""
    mb = num_bytes / 1024 / 1024
    if mb >= 1:
        return f"{mb:.1f}MB"
    kb = num_bytes / 1024
    return f"{kb:.0f}KB"


def make_progress_bar(percent: float, length: int = PROGRESS_BAR_LENGTH) -> str:
    """یه نوار پیشرفت متنی می‌سازه: [▓▓▓▓░░░░] """
    percent = max(0, min(100, percent))
    filled = int(length * percent / 100)
    return "▓" * filled + "░" * (length - filled)


class ProgressState:
    """وضعیت زنده‌ی دانلود که بین ترد دانلود و تسک آپدیت پیام مشترکه."""

    def __init__(self):
        self.status = "starting"  # starting | downloading | processing | done | error
        self.downloaded = 0
        self.total = 0
        self.percent = 0.0
        self.speed = 0
        self.eta = 0

    def render(self) -> str:
        if self.status == "processing":
            return "⚙️ در حال پردازش نهایی (تبدیل/ترکیب فایل)..."
        bar = make_progress_bar(self.percent)
        downloaded_txt = format_size(self.downloaded) or "0KB"
        total_txt = format_size(self.total) if self.total else "؟"
        line2 = f"{downloaded_txt} / {total_txt}"
        if self.speed:
            line2 += f"  •  {format_size(self.speed)}/s"
        if self.eta:
            line2 += f"  •  {int(self.eta)}s مونده"
        return f"⬇️ در حال دانلود...\n[{bar}] {self.percent:.0f}%\n{line2}"


def make_progress_hook(state: ProgressState):
    """هوک yt-dlp که توی ترد دانلود صدا زده می‌شه و state رو آپدیت می‌کنه."""

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            state.status = "downloading"
            state.total = total
            state.downloaded = downloaded
            state.percent = (downloaded / total * 100) if total else 0
            state.speed = d.get("speed") or 0
            state.eta = d.get("eta") or 0
        elif d.get("status") == "finished":
            # این فرمت تموم شد؛ اگه merge/convert لازم باشه بعدش میاد
            state.percent = 100
            state.status = "processing"

    return hook


async def set_status(query, text: str):
    """چه پیام اصلی عکس (تامبنیل) باشه چه متن ساده، وضعیت رو به‌روز می‌کنه."""
    try:
        if query.message.photo:
            await query.edit_message_caption(caption=text)
        else:
            await query.edit_message_text(text)
    except BadRequest as e:
        # "Message is not modified" وقتی متن عوض نشده - بی‌خطره، نادیده بگیر
        if "not modified" not in str(e).lower():
            logger.warning("set_status failed: %s", e)
    except Exception:
        logger.exception("set_status unexpected failure")


async def run_progress_updates(query, state: ProgressState, stop_event: asyncio.Event):
    """هر چند ثانیه یک‌بار پیام رو با وضعیت جدید آپدیت می‌کنه تا دانلود تموم بشه."""
    last_shown = -100
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=PROGRESS_UPDATE_INTERVAL)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        if state.status == "downloading" and abs(state.percent - last_shown) < 4:
            continue  # تغییر خیلی کمه، از ادیت اضافه صرف‌نظر کن (جلوگیری از فلود)
        last_shown = state.percent
        await set_status(query, state.render())


def best_audio_format(formats):
    """بهترین فرمت فقط-صدا رو برای تخمین حجم mp3 خروجی برمی‌گردونه."""
    audio_only = [
        f for f in formats
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
    ]
    if not audio_only:
        return None
    return max(audio_only, key=lambda f: f.get("abr") or f.get("tbr") or 0)


def pick_quality_options(info: dict):
    """
    از بین فرمت‌های موجود، چند گزینه‌ی معنادار برای کاربر می‌سازه:
    هر رزولوشن موجود با حجم دقیق (یا تقریبی اگه سرور اعلام نکرده باشه) + فقط صدا.
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

    # بهترین فرمت صدا که موقع مرج به ویدیو اضافه می‌شه، برای تخمین حجم نهایی
    audio_f = best_audio_format(formats)
    audio_size = 0
    if audio_f:
        audio_size = audio_f.get("filesize") or audio_f.get("filesize_approx") or 0

    sorted_heights = sorted(by_height.keys(), reverse=True)
    options = []
    for h in sorted_heights:
        f = by_height[h]
        video_size = f.get("filesize") or f.get("filesize_approx") or 0
        exact = f.get("filesize") is not None
        total = video_size + audio_size if video_size else 0
        if total:
            size_txt = f" - {format_size(total)}" + ("" if exact else " (تقریبی)")
        else:
            size_txt = ""
        options.append(
            {
                "label": f"🎬 {h}p{size_txt}",
                "format_id": f["format_id"],
                "kind": "video",
            }
        )

    # اگر هیچ فرمت مجزایی پیدا نشد (مثلا فقط یه فایل ترکیبی هست)
    if not options and formats:
        best = max(formats, key=lambda f: f.get("height") or 0)
        size = best.get("filesize") or best.get("filesize_approx")
        size_txt = f" - {format_size(size)}" if size else ""
        options.append({"label": f"🎬 کیفیت پیش‌فرض{size_txt}", "format_id": "best", "kind": "video"})

    # گزینه‌ی فقط صدا همیشه اضافه میشه
    audio_label = "🎵 فقط صدا (MP3)"
    if audio_size:
        audio_label += f" - {format_size(audio_size)}"
    options.append({"label": audio_label, "format_id": "bestaudio", "kind": "audio"})
    return options


def download_media(url: str, format_id: str, kind: str, out_dir: str, progress_state=None) -> str:
    """دانلود ویدیو با فرمت انتخابی یا استخراج فقط صدا. مسیر فایل نهایی رو برمی‌گردونه."""
    out_tmpl = os.path.join(out_dir, "%(id)s.%(ext)s")
    hooks = [make_progress_hook(progress_state)] if progress_state is not None else []

    if kind == "audio":
        opts = build_ydl_opts(
            format="bestaudio/best",
            outtmpl=out_tmpl,
            progress_hooks=hooks,
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
            progress_hooks=hooks,
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
        "سلام! لینک پست/ریلز/استوری اینستاگرام، ویدیو/شورتز یوتیوب یا"
        " ویدیوی تیک‌تاک رو"
        " برام بفرست 🙂\nبعدش کیفیت مورد نظرت رو از بین دکمه‌ها انتخاب کن"
        " (با حجم دقیق هر کدوم)، یا اگه فقط آهنگش رو می‌خوای، گزینه‌ی"
        " «فقط صدا» رو بزن.",
        reply_markup=MAIN_KEYBOARD,
    )


async def help_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 راهنمای استفاده:\n\n"
        "۱. لینک پست/ریلز/استوری اینستاگرام، ویدیو/شورتز یوتیوب یا"
        " ویدیوی تیک‌تاک"
        " رو بفرست\n"
        "۲. تامبنیل و توضیحات محتوا نشون داده می‌شه\n"
        "۳. از بین دکمه‌های کیفیت (هرکدوم با حجم دقیق) یکی رو انتخاب کن\n"
        "۴. اگه فقط صدا می‌خوای، دکمه‌ی «🎵 فقط صدا» رو بزن\n"
        "۵. یه نوار پیشرفت زنده می‌بینی تا دانلود و ارسال تموم بشه\n\n"
        "نکته: پست‌های خصوصی یا ویدیوهای محدود بدون تنظیم کوکی قابل"
        " دانلود نیستن.",
        reply_markup=MAIN_KEYBOARD,
    )


async def about_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ درباره ربات:\n\n"
        "این ربات با کمک yt-dlp از اینستاگرام، یوتیوب و تیک‌تاک دانلود می‌کنه.\n"
        "لطفاً فقط برای محتوای خودتون یا محتوایی که اجازه‌ی استفاده"
        " دارید ازش استفاده کنید؛ رعایت کپی‌رایت و قوانین پلتفرم‌ها به"
        " عهده‌ی خودتونه.",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    match = SUPPORTED_URL_RE.search(text)
    if not match:
        await update.message.reply_text(
            "این یه لینک معتبر نیست. لینک پست/ریلز/استوری اینستاگرام،"
            " ویدیو/شورتز یوتیوب یا ویدیوی تیک‌تاک رو بفرست."
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

    buttons_template = []  # بعد از ساخت key تکمیل میشه
    thumbnail_url = info.get("thumbnail")
    uploader = info.get("uploader") or info.get("uploader_id") or ""
    description = info.get("description") or info.get("title") or ""
    description = description.strip()
    if len(description) > 600:
        description = description[:600] + "…"

    caption_lines = []
    if uploader:
        caption_lines.append(f"👤 {uploader}")
    if description:
        caption_lines.append(description)
    caption_lines.append("\nکیفیت مورد نظرت رو انتخاب کن:")
    caption = "\n\n".join(caption_lines) if caption_lines else "کیفیت مورد نظرت رو انتخاب کن:"
    if len(caption) > 1024:  # سقف کپشن عکس در تلگرام
        caption = caption[:1000] + "…\n\nکیفیت مورد نظرت رو انتخاب کن:"

    await status_msg.delete()

    if thumbnail_url:
        try:
            sent_msg = await update.message.reply_photo(photo=thumbnail_url, caption=caption)
        except Exception:
            logger.exception("sending thumbnail failed, falling back to text")
            sent_msg = await update.message.reply_text(caption)
    else:
        sent_msg = await update.message.reply_text(caption)

    key = f"{update.effective_chat.id}_{sent_msg.message_id}"
    PENDING[key] = {"url": url, "options": {o["format_id"] + "_" + o["kind"]: o for o in options}}

    buttons = []
    for o in options:
        cb_data = f"dl|{key}|{o['format_id']}|{o['kind']}"
        buttons.append([InlineKeyboardButton(o["label"], callback_data=cb_data)])

    await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, key, format_id, kind = query.data.split("|", 3)
    except ValueError:
        await set_status(query, "❌ خطای داخلی، دوباره لینک رو بفرست.")
        return

    entry = PENDING.get(key)
    if not entry:
        await set_status(query, "⌛ این درخواست منقضی شده، لطفاً لینک رو دوباره بفرست.")
        return

    url = entry["url"]
    state = ProgressState()
    await set_status(query, "⏳ در حال آماده‌سازی دانلود...")

    tmp_dir = tempfile.mkdtemp(prefix="dl_")
    stop_event = asyncio.Event()
    progress_task = asyncio.create_task(run_progress_updates(query, state, stop_event))

    try:
        try:
            file_path = await asyncio.to_thread(
                download_media, url, format_id, kind, tmp_dir, state
            )
        finally:
            stop_event.set()
            await progress_task

        size_mb = os.path.getsize(file_path) / 1024 / 1024

        if size_mb > MAX_TELEGRAM_UPLOAD_MB:
            await set_status(
                query,
                f"❌ حجم فایل {size_mb:.1f}MB هست و از سقف {MAX_TELEGRAM_UPLOAD_MB}MB"
                " ربات‌های تلگرام رسمی بیشتره. برای فایل‌های بزرگ‌تر باید از"
                " Local Bot API Server استفاده کنی (توضیح در README).",
            )
            return

        await set_status(query, "📤 در حال ارسال فایل...")
        chat_id = query.message.chat_id
        with open(file_path, "rb") as f:
            if kind == "audio":
                await context.bot.send_audio(chat_id=chat_id, audio=f)
            else:
                await context.bot.send_video(chat_id=chat_id, video=f, supports_streaming=True)

        await set_status(query, "✅ ارسال شد!")
    except Exception as e:
        logger.exception("download/send failed")
        stop_event.set()
        await set_status(query, "❌ دانلود یا ارسال فایل با خطا مواجه شد:\n" + str(e)[:300])
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
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_HELP)}$"), help_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_ABOUT)}$"), about_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^dl\|"))

    logger.info("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
    
