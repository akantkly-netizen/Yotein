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
import base64
import logging
import math
import os
import re
import subprocess
import tempfile
import shutil
from pathlib import Path

import yt_dlp
import requests
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

# اگر پست‌های خصوصی/محدود دارید یا یوتیوب می‌گه "Sign in to confirm
# you're not a bot"، باید کوکی بدید. دو راه:
#  ۱) مسیر یه فایل cookies.txt رو تو COOKIES_FILE بدید (اجرای لوکال)
#  ۲) محتوای همون فایل رو با base64 اینکود کنید و تو متغیر محیطی
#     COOKIES_FILE_B64 بذارید (برای سرورهایی مثل Railway که نمی‌شه
#     مستقیم فایل آپلود کرد) - ربات موقع استارت خودش دیکودش می‌کنه.
# فایل cookies.txt رو با اکستنشنی مثل "Get cookies.txt LOCALLY" از
# مرورگری که توش لاگین یوتیوب/اینستاگرام هستید export بگیرید.
COOKIES_FILE = os.environ.get("COOKIES_FILE") or os.environ.get("INSTAGRAM_COOKIES_FILE", "")
COOKIES_FILE_B64 = os.environ.get("COOKIES_FILE_B64", "")

if COOKIES_FILE_B64 and not (COOKIES_FILE and Path(COOKIES_FILE).exists()):
    try:
        _cookies_path = os.path.join(tempfile.gettempdir(), "ytdlp_cookies.txt")
        with open(_cookies_path, "wb") as _f:
            _f.write(base64.b64decode(COOKIES_FILE_B64))
        COOKIES_FILE = _cookies_path
    except Exception:
        logging.getLogger(__name__).exception("failed to decode COOKIES_FILE_B64")

MAX_TELEGRAM_UPLOAD_MB = 50  # محدودیت سرور رسمی بات تلگرام
PROGRESS_UPDATE_INTERVAL = 2.0  # ثانیه، فاصله‌ی به‌روزرسانی نوار پیشرفت
PROGRESS_BAR_LENGTH = 18  # تعداد بلوک‌های نوار پیشرفت

# برای تشخیص آهنگ از روی صدای ویدیو (وقتی متادیتای پلتفرم اسم آهنگ رو
# نداره). یه توکن رایگان از https://dashboard.audd.io بگیر.
AUDD_API_KEY = os.environ.get("AUDD_API_KEY", "")
SONG_SNIPPET_SECONDS = 20  # طول قطعه‌ی صوتی که برای تشخیص فرستاده می‌شه

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
BTN_MUSIC = "🎵 جستجوی موزیک"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_MUSIC], [BTN_HELP, BTN_ABOUT]],
    resize_keyboard=True,   # دکمه‌ها رو کوچیک و جمع‌وجور نشون می‌ده
    is_persistent=True,     # همیشه روی صفحه بمونه
)


# نگهداری موقت اطلاعات هر لینک تا زمانی که کاربر کیفیت رو انتخاب کنه
# key: کد کوتاه (chat_id_msgid) -> {"url":..., "formats": [...]}
PENDING = {}

# مجموعه‌ی chat_id هایی که منتظر فرستادن اسم آهنگ هستن (بعد از زدن دکمه‌ی جستجو)
WAITING_MUSIC_QUERY = set()

# نگهداری موقت نتایج جستجوی موزیک برای صفحه‌بندی
# key: کد کوتاه (chat_id_msgid) -> {"entries": [...], "query": "..."}
MUSIC_SESSIONS = {}
MUSIC_PAGE_SIZE = 10


# ------------------------------------------------------------------
# توابع کمکی yt-dlp
# ------------------------------------------------------------------
def build_ydl_opts(use_cookies: bool = False, **overrides):
    """
    use_cookies=False (پیش‌فرض): بدون کوکی درخواست می‌ده، مثل یه کاربر
    عادی. use_cookies=True: فقط وقتی سایت صریحاً خطای "sign in / not a
    bot" داده، به‌عنوان تلاش دوم کوکی رو اضافه می‌کنه.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if use_cookies and COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE
    opts.update(overrides)
    return opts


BOT_CHECK_SIGNS = (
    "sign in to confirm",
    "not a bot",
    "confirm you're not a bot",
    "confirm you are not a bot",
    "login required",
    "this video is only available",
)


def _looks_like_bot_check(err_text: str) -> bool:
    t = err_text.lower()
    return any(s in t for s in BOT_CHECK_SIGNS)


def _run_with_cookie_fallback(func, *args, **kwargs):
    """
    اول بدون کوکی امتحان می‌کنه. اگه خطا مشخصاً «ثابت کن ربات نیستی»
    بود و کوکی داریم، فقط همون یه‌بار با کوکی دوباره امتحان می‌کنه.
    برای هر خطای دیگه (لینک اشتباه، پست خصوصی و ...) مستقیم پرتاب می‌شه.
    """
    try:
        return func(*args, use_cookies=False, **kwargs)
    except Exception as e:
        if COOKIES_FILE and Path(COOKIES_FILE).exists() and _looks_like_bot_check(str(e)):
            logger.info("bot-check detected, retrying with cookies")
            return func(*args, use_cookies=True, **kwargs)
        raise


def _extract_info_impl(url: str, use_cookies: bool = False) -> dict:
    with yt_dlp.YoutubeDL(build_ydl_opts(use_cookies=use_cookies)) as ydl:
        return ydl.extract_info(url, download=False)


def extract_info(url: str) -> dict:
    """اطلاعات لینک (فرمت‌های موجود و ...) رو بدون دانلود می‌گیره."""
    return _run_with_cookie_fallback(_extract_info_impl, url)


def search_music(query: str, limit: int = 8):
    """
    اول تو یوتیوب‌میوزیک می‌گرده (کاتالوگ رسمی گوگل)، اگه چیزی پیدا نشد
    (مثلاً برای خیلی از آهنگ‌های فارسی) به یوتیوب عادی فال‌بک می‌کنه.
    امتیازدهی طوریه که نسخه‌ی رسمی/آفیشیال رو در اولویت می‌ذاره و
    ریمیکس/کاور/لایو/اسپیدآپ رو عقب می‌زنه مگه خود متن جستجو دنبال
    اونا باشه.
    """
    entries = []
    for prefix in (f"ytmsearch{limit}:", f"ytsearch{limit}:"):
        try:
            with yt_dlp.YoutubeDL(build_ydl_opts(extract_flat="in_playlist")) as ydl:
                result = ydl.extract_info(prefix + query, download=False)
            entries = [e for e in (result.get("entries") or []) if e]
            if entries:
                break
        except Exception:
            logger.exception("search_music failed for prefix %s", prefix)

    if not entries:
        return None

    query_lower = query.lower()
    wants_variant = any(w in query_lower for w in ("remix", "cover", "live", "ریمیکس", "کاور"))

    def score(entry):
        title = (entry.get("title") or "").lower()
        s = 0
        if "official audio" in title:
            s += 4
        elif "official music video" in title or "official video" in title:
            s += 3
        elif "official" in title:
            s += 1
        if "lyric" in title:
            s += 1
        if not wants_variant:
            for bad in (
                "remix", "cover", "ریمیکس", "کاور", "live", "sped up",
                "slowed", "nightcore", "8d audio", "karaoke", "instrumental", "reaction",
            ):
                if bad in title:
                    s -= 4
        return s

    best = max(entries, key=score)
    return best


def search_music_list(query: str, limit: int = 50):
    """
    برای جستجوی موزیک با لیست کامل: اول تو یوتیوب‌میوزیک (که کاتالوگ
    رسمی گوگل/یوتیوب‌میوزیکه، بدون کاور و ریمیکس‌های الکی) می‌گرده؛
    اگه چیزی پیدا نشد، به جستجوی عادی یوتیوب فال‌بک می‌کنه. برای یه
    اسم خواننده، معمولاً همه‌ی آهنگ‌های شناخته‌شده‌اش برمی‌گرده.
    """
    for prefix in (f"ytmsearch{limit}:", f"ytsearch{limit}:"):
        try:
            with yt_dlp.YoutubeDL(build_ydl_opts(extract_flat="in_playlist")) as ydl:
                result = ydl.extract_info(prefix + query, download=False)
            entries = [e for e in (result.get("entries") or []) if e]
            if entries:
                return entries
        except Exception:
            logger.exception("search_music_list failed for prefix %s", prefix)
    return []


def format_music_entry_label(entry: dict, number: int) -> str:
    title = entry.get("title") or "بدون‌نام"
    artist = entry.get("artist") or entry.get("uploader") or entry.get("channel") or ""
    duration = entry.get("duration")
    dur_txt = ""
    if duration:
        m, s = divmod(int(duration), 60)
        dur_txt = f" ({m}:{s:02d})"
    label = f"{number}. {title}"
    if artist and artist.lower() not in title.lower():
        label += f" - {artist}"
    label += dur_txt
    if len(label) > 64:
        label = label[:61] + "…"
    return label


def build_music_page(key: str, entries: list, page: int):
    """صفحه‌ی مشخصی از نتایج (۱۰تا-۱۰تا) رو با دکمه‌ها می‌سازه."""
    start = page * MUSIC_PAGE_SIZE
    page_entries = entries[start:start + MUSIC_PAGE_SIZE]
    total_pages = max(1, math.ceil(len(entries) / MUSIC_PAGE_SIZE))

    buttons = []
    for i, e in enumerate(page_entries):
        idx = start + i
        label = format_music_entry_label(e, idx + 1)
        buttons.append([InlineKeyboardButton(label, callback_data=f"mpick|{key}|{idx}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ صفحه قبل", callback_data=f"mpage|{key}|{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶️ صفحه بعد", callback_data=f"mpage|{key}|{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    return InlineKeyboardMarkup(buttons), total_pages


def extract_track_from_metadata(info: dict):
    """
    اگه پلتفرم (مثلاً تیک‌تاک) اسم آهنگ استفاده‌شده رو توی متادیتا داده
    باشه برش می‌گردونه: (title, artist) یا None اگه چیزی پیدا نشد.
    """
    title = info.get("track")
    artist = info.get("artist")
    if not artist:
        artists = info.get("artists")
        if artists:
            artist = artists[0] if isinstance(artists, list) else artists
    # بعضی وقتا تیک‌تاک اسم صدا رو تو "album" می‌ذاره اگه track نباشه
    if not title:
        title = info.get("alt_title")
    if title and title.strip().lower() not in ("original sound", "original audio", ""):
        return title.strip(), (artist.strip() if artist else None)
    return None


def _download_audio_snippet_impl(
    url: str, out_dir: str, duration: int = SONG_SNIPPET_SECONDS, use_cookies: bool = False
) -> str:
    out_tmpl = os.path.join(out_dir, "snippet.%(ext)s")
    opts = build_ydl_opts(
        use_cookies=use_cookies,
        format="bestaudio/best",
        outtmpl=out_tmpl,
        postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}
        ],
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    base, _ = os.path.splitext(filename)
    mp3_path = base + ".mp3"
    if not os.path.exists(mp3_path):
        mp3_path = filename

    trimmed_path = os.path.join(out_dir, "snippet_trim.mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-t", str(duration), "-vn", trimmed_path],
            check=True,
            capture_output=True,
            timeout=60,
        )
        if os.path.exists(trimmed_path):
            return trimmed_path
    except Exception:
        logger.exception("ffmpeg trim failed, using full snippet")

    return mp3_path


def download_audio_snippet(url: str, out_dir: str, duration: int = SONG_SNIPPET_SECONDS) -> str:
    """یه قطعه‌ی کوتاه از صدای ویدیو رو دانلود می‌کنه (برای فرستادن به سرویس تشخیص آهنگ)."""
    return _run_with_cookie_fallback(
        _download_audio_snippet_impl, url, out_dir, duration=duration
    )


def recognize_song_via_audd(audio_path: str):
    """
    قطعه‌ی صوتی رو به سرویس AudD می‌فرسته و اسم آهنگ/خواننده رو
    برمی‌گردونه: (title, artist) یا None. نیاز به AUDD_API_KEY داره.
    """
    if not AUDD_API_KEY:
        return None
    try:
        with open(audio_path, "rb") as f:
            resp = requests.post(
                "https://api.audd.io/",
                data={"api_token": AUDD_API_KEY, "return": "spotify"},
                files={"file": f},
                timeout=30,
            )
        data = resp.json()
    except Exception:
        logger.exception("AudD request failed")
        return None

    result = data.get("result") if isinstance(data, dict) else None
    if not result:
        return None
    title = result.get("title")
    artist = result.get("artist")
    if not title:
        return None
    return title, artist


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


def _download_media_impl(
    url: str, format_id: str, kind: str, out_dir: str, progress_state=None, use_cookies: bool = False
) -> str:
    out_tmpl = os.path.join(out_dir, "%(id)s.%(ext)s")
    hooks = [make_progress_hook(progress_state)] if progress_state is not None else []

    if kind == "audio":
        opts = build_ydl_opts(
            use_cookies=use_cookies,
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
            use_cookies=use_cookies,
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


def download_media(url: str, format_id: str, kind: str, out_dir: str, progress_state=None) -> str:
    """دانلود ویدیو با فرمت انتخابی یا استخراج فقط صدا. مسیر فایل نهایی رو برمی‌گردونه."""
    return _run_with_cookie_fallback(
        _download_media_impl, url, format_id, kind, out_dir, progress_state=progress_state
    )


# ------------------------------------------------------------------
# هندلرهای تلگرام
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "درود به روی ماهت 🧘🏾🌚\n"
        "من ربات دانلودرم 🧸\n\n"
        "با من می‌تونی ویدئو ها، موزیک ها و پست های هر پلتفرمی رو که"
        " بخوای بدون محدودیت دانلود کنی 🧘🏾✨️\n\n"
        "و همچنین میتونی موزیک پست و ویدئو دلخواهت رو با استفاده از من"
        " پیدا و دانلود کنی 🧘🏾🎧\n\n"
        "کافیه فقط لینک پستی دلخواهت رو برام بفرستی 🧸",
        reply_markup=MAIN_KEYBOARD,
    )


async def help_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 راهنمای کامل ربات\n\n"
        "🔗 دانلود از لینک (اینستاگرام / یوتیوب / تیک‌تاک)\n"
        "۱. لینک پست، ریلز، استوری اینستاگرام، ویدیو یا شورتز یوتیوب،"
        " یا ویدیوی تیک‌تاک رو برام بفرست\n"
        "۲. تامبنیل و توضیحات محتوا رو نشونت می‌دم\n"
        "۳. زیرش چند دکمه‌ی کیفیت می‌بینی، هرکدوم با حجم دقیق فایل"
        " (مثلاً «1080p - 24.3MB»)؛ هرکدوم رو بخوای انتخاب کن\n"
        "۴. اگه فقط صدای ویدیو رو می‌خوای (نه خود ویدیو)، دکمه‌ی"
        " «🎵 فقط صدا» رو بزن\n"
        "۵. حین دانلود یه نوار پیشرفت زنده می‌بینی (درصد، حجم دانلودشده"
        " از کل، سرعت)؛ در آخر فایل برات ارسال می‌شه\n\n"
        f"🎵 جستجوی موزیک (دکمه‌ی «{BTN_MUSIC}»)\n"
        "۱. روی دکمه بزن و اسم آهنگ یا فقط اسم خواننده رو بفرست\n"
        "۲. اگه اسم خواننده بفرستی، همه‌ی آهنگ‌های شناخته‌شده‌اش رو"
        " پیدا می‌کنم؛ نتیجه‌ها ۱۰تا-۱۰تا صفحه‌بندی شدن و با دکمه‌های"
        " «◀️ صفحه قبل» و «▶️ صفحه بعد» می‌تونی بین صفحه‌ها بری\n"
        "۳. روی اسم هر آهنگ که بزنی، همون دکمه‌های کیفیت/فقط‌صدای بالا"
        " براش میاد تا انتخاب کنی\n"
        "۴. جستجو اول تو یوتیوب‌میوزیک می‌گرده (کاتالوگ رسمی) و اگه چیزی"
        " پیدا نشد (مثلاً خیلی از آهنگ‌های فارسی) خودکار به یوتیوب عادی"
        " هم سر می‌زنه\n\n"
        "🎧 پیدا کردن آهنگ اصلی یه ویدیو\n"
        "زیر دکمه‌های کیفیت هر ویدیو، یه دکمه‌ی «🎵 پیدا کردن آهنگ اصلی"
        " ویدیو» هم هست. با زدنش:\n"
        "۱. اول چک می‌کنم خود پلتفرم (بیشتر تیک‌تاک) اسم آهنگ رو تو"
        " متادیتاش داده یا نه\n"
        "۲. اگه نداشت، یه تیکه از صدای خود ویدیو رو می‌گیرم و با سرویس"
        " تشخیص آهنگ شناساییش می‌کنم\n"
        "۳. بعد از پیدا کردن نسخه‌ی رسمی، خودم هم ویدیو هم نسخه‌ی صوتیش"
        " رو با اسم درست و تمیز آهنگ برات می‌فرستم (نیازی به انتخاب"
        " دستی نیست)\n"
        "۴. اگه پیدا نشد، لااقل اسم خواننده رو (اگه شناسایی شده باشه)"
        " بهت می‌گم تا خودت دستی جستجو کنی\n\n"
        "⚠️ نکات مهم\n"
        "• پست‌های خصوصی یا ویدیوهای محدود بدون تنظیم کوکی قابل دانلود"
        " نیستن\n"
        "• فقط برای محتوای خودتون یا محتوایی که اجازه‌ی استفاده دارید"
        " ازم استفاده کنید؛ مسئولیت رعایت کپی‌رایت با کاربره",
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


async def music_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    WAITING_MUSIC_QUERY.add(update.effective_chat.id)
    await update.message.reply_text(
        "🎵 اسم آهنگ و خواننده رو بفرست (مثلاً: «Shadmehr Aghili - Bahar»).\n"
        "نسخه‌ی رسمیش رو پیدا می‌کنم و برات می‌فرستم.",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_music_query(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str):
    if not query_text:
        await update.message.reply_text("یه اسم آهنگ یا خواننده بفرست تا جستجو کنم.")
        return

    status_msg = await update.message.reply_text(f"🔎 دارم دنبال «{query_text}» می‌گردم...")
    chat_id = update.effective_chat.id

    try:
        entries = await asyncio.to_thread(search_music_list, query_text)
    except Exception as e:
        logger.exception("search_music_list failed")
        await status_msg.edit_text("❌ جستجو با خطا مواجه شد:\n" + str(e)[:300])
        return

    if not entries:
        await status_msg.edit_text(
            "چیزی پیدا نکردم. اسم دقیق‌تر یا اسم درست خواننده رو امتحان کن."
        )
        return

    key = f"{chat_id}_{status_msg.message_id}"
    MUSIC_SESSIONS[key] = {"entries": entries, "query": query_text}

    markup, total_pages = build_music_page(key, entries, 0)
    text = (
        f"🎵 نتایج برای «{query_text}» ({len(entries)} مورد) — صفحه ۱ از {total_pages}:\n"
        "یکی رو انتخاب کن:"
    )
    await status_msg.edit_text(text, reply_markup=markup)


async def handle_music_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کاربر دکمه‌ی صفحه‌ی بعد/قبل رو زده."""
    query = update.callback_query
    await query.answer()

    try:
        _, key, page_str = query.data.split("|", 2)
        page = int(page_str)
    except ValueError:
        return

    session = MUSIC_SESSIONS.get(key)
    if not session:
        await set_status(query, "⌛ این جستجو منقضی شده، دوباره از دکمه‌ی جستجوی موزیک استفاده کن.")
        return

    entries = session["entries"]
    markup, total_pages = build_music_page(key, entries, page)
    text = (
        f"🎵 نتایج برای «{session['query']}» ({len(entries)} مورد) — صفحه {page + 1} از {total_pages}:\n"
        "یکی رو انتخاب کن:"
    )
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("music page edit failed: %s", e)


async def handle_music_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کاربر یکی از آهنگ‌های لیست رو انتخاب کرده."""
    query = update.callback_query
    await query.answer()

    try:
        _, key, idx_str = query.data.split("|", 2)
        idx = int(idx_str)
    except ValueError:
        return

    session = MUSIC_SESSIONS.get(key)
    if not session:
        await set_status(query, "⌛ این جستجو منقضی شده، دوباره از دکمه‌ی جستجوی موزیک استفاده کن.")
        return

    entries = session["entries"]
    if idx < 0 or idx >= len(entries):
        return

    entry = entries[idx]
    video_url = entry.get("url") or entry.get("webpage_url") or entry.get("id")
    if entry.get("id") and not str(video_url).startswith("http"):
        video_url = f"https://www.youtube.com/watch?v={entry['id']}"

    chat_id = query.message.chat_id
    status_msg = await query.message.reply_text("⏳ در حال گرفتن اطلاعات...")
    await show_quality_picker(query.message, chat_id, video_url, status_msg, offer_find_song=False)


async def show_quality_picker(message, chat_id: int, url: str, status_msg=None, offer_find_song: bool = True):
    """
    اطلاعات لینک رو می‌گیره، تامبنیل+توضیحات رو نشون می‌ده و دکمه‌های
    کیفیت رو می‌سازه. هم برای لینک مستقیم استفاده می‌شه هم برای نتیجه‌ی
    جستجوی موزیک. اگه status_msg داده بشه (پیام "در حال جستجو/گرفتن
    اطلاعات")، در پایان حذف می‌شه. offer_find_song=False برای وقتی که
    خودش نتیجه‌ی جستجوی آهنگه (نیازی به دکمه‌ی پیدا کردن آهنگ نیست).
    """
    try:
        info = extract_info(url)
    except Exception as e:
        logger.exception("extract_info failed")
        err_text = (
            "❌ نتونستم اطلاعات این لینک رو بگیرم. ممکنه پست خصوصی باشه یا"
            " لینک اشتباه باشه.\nجزئیات خطا: " + str(e)[:300]
        )
        if status_msg:
            await status_msg.edit_text(err_text)
        else:
            await message.reply_text(err_text)
        return

    options = pick_quality_options(info)
    known_track = extract_track_from_metadata(info) if offer_find_song else None

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

    if status_msg:
        await status_msg.delete()

    if thumbnail_url:
        try:
            sent_msg = await message.reply_photo(photo=thumbnail_url, caption=caption)
        except Exception:
            logger.exception("sending thumbnail failed, falling back to text")
            sent_msg = await message.reply_text(caption)
    else:
        sent_msg = await message.reply_text(caption)

    key = f"{chat_id}_{sent_msg.message_id}"
    PENDING[key] = {
        "url": url,
        "options": {o["format_id"] + "_" + o["kind"]: o for o in options},
        "known_track": known_track,
    }

    buttons = []
    for o in options:
        cb_data = f"dl|{key}|{o['format_id']}|{o['kind']}"
        buttons.append([InlineKeyboardButton(o["label"], callback_data=cb_data)])

    if offer_find_song:
        buttons.append([InlineKeyboardButton("🎵 پیدا کردن آهنگ اصلی ویدیو", callback_data=f"song|{key}")])

    await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    if chat_id in WAITING_MUSIC_QUERY:
        WAITING_MUSIC_QUERY.discard(chat_id)
        await handle_music_query(update, context, text)
        return

    match = SUPPORTED_URL_RE.search(text)
    if not match:
        await update.message.reply_text(
            "این یه لینک معتبر نیست. لینک پست/ریلز/استوری اینستاگرام،"
            " ویدیو/شورتز یوتیوب یا ویدیوی تیک‌تاک رو بفرست، یا از دکمه‌ی"
            f" «{BTN_MUSIC}» برای جستجوی آهنگ استفاده کن."
        )
        return

    url = match.group(0)
    status_msg = await update.message.reply_text("⏳ دارم اطلاعات لینک رو می‌گیرم...")
    await show_quality_picker(update.message, chat_id, url, status_msg)


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


async def deliver_song(query, context, chat_id: int, url: str, title: str, artist: str = None):
    """
    برای فیچر «پیدا کردن آهنگ اصلی»: خودکار هم ویدیو هم نسخه‌ی صوتی رو
    دانلود و می‌فرسته، و نسخه‌ی صوتی رو با اسم درست و تمیز آهنگ (نه
    اسم خام فایل یوتیوب) تگ‌گذاری می‌کنه تا تو تلگرام درست نشون داده بشه.
    """
    label = f"{title} - {artist}" if artist else title
    tmp_dir = tempfile.mkdtemp(prefix="song_dl_")
    try:
        # --- ویدیو ---
        state = ProgressState()
        stop_event = asyncio.Event()
        progress_task = asyncio.create_task(run_progress_updates(query, state, stop_event))
        try:
            video_path = await asyncio.to_thread(download_media, url, "best", "video", tmp_dir, state)
        finally:
            stop_event.set()
            await progress_task

        video_size_mb = os.path.getsize(video_path) / 1024 / 1024
        if video_size_mb <= MAX_TELEGRAM_UPLOAD_MB:
            with open(video_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=chat_id, video=f, supports_streaming=True, caption=f"🎬 {label}"
                )
        else:
            await query.message.reply_text(
                f"⚠️ حجم ویدیو {video_size_mb:.1f}MB هست و بیشتر از سقف {MAX_TELEGRAM_UPLOAD_MB}MB تلگرامه."
            )

        # --- نسخه‌ی صوتی ---
        await set_status(query, f"⬇️ در حال دانلود نسخه‌ی صوتی «{label}»...")
        audio_path = await asyncio.to_thread(download_media, url, "bestaudio", "audio", tmp_dir)
        audio_size_mb = os.path.getsize(audio_path) / 1024 / 1024
        if audio_size_mb <= MAX_TELEGRAM_UPLOAD_MB:
            with open(audio_path, "rb") as f:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=f,
                    title=title,
                    performer=artist or None,
                )
        else:
            await query.message.reply_text(
                f"⚠️ حجم فایل صوتی {audio_size_mb:.1f}MB هست و بیشتر از سقف {MAX_TELEGRAM_UPLOAD_MB}MB تلگرامه."
            )

        await set_status(query, f"✅ «{label}» ارسال شد!")
    except Exception as e:
        logger.exception("deliver_song failed")
        await set_status(query, "❌ ارسال آهنگ با خطا مواجه شد:\n" + str(e)[:300])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def handle_find_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کاربر دکمه‌ی «پیدا کردن آهنگ اصلی ویدیو» رو زده."""
    query = update.callback_query
    await query.answer()

    try:
        _, key = query.data.split("|", 1)
    except ValueError:
        await set_status(query, "❌ خطای داخلی، دوباره لینک رو بفرست.")
        return

    entry = PENDING.get(key)
    if not entry:
        await set_status(query, "⌛ این درخواست منقضی شده، لطفاً لینک رو دوباره بفرست.")
        return

    url = entry["url"]
    known_track = entry.get("known_track")
    chat_id = query.message.chat_id

    # حالت ۱: خود پلتفرم اسم آهنگ رو تو متادیتا داده بود (مثلاً تیک‌تاک)
    if known_track:
        title, artist = known_track
    else:
        # حالت ۲: متادیتا نداشتیم -> باید از روی خود صدای ویدیو تشخیص بدیم
        if not AUDD_API_KEY:
            await set_status(
                query,
                "ℹ️ این ویدیو اسم آهنگ رو تو متادیتاش نداره، و برای تشخیص از"
                " روی خود صدا باید یه AUDD_API_KEY تنظیم بشه (رایگان از"
                " audd.io بگیر و در تنظیمات ربات قرار بده).",
            )
            return

        await set_status(query, "⬇️ در حال دانلود قطعه‌ی صدا برای تشخیص...")
        tmp_dir = tempfile.mkdtemp(prefix="song_id_")
        try:
            snippet_path = await asyncio.to_thread(download_audio_snippet, url, tmp_dir)
            await set_status(query, "🎧 در حال تشخیص آهنگ...")
            result = await asyncio.to_thread(recognize_song_via_audd, snippet_path)
        except Exception as e:
            logger.exception("find_song audio recognition failed")
            await set_status(query, "❌ تشخیص آهنگ با خطا مواجه شد:\n" + str(e)[:300])
            return
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if not result:
            await set_status(
                query,
                "❌ نتونستم آهنگ این ویدیو رو تشخیص بدم. شاید صدای پس‌زمینه"
                " واضح نبود یا آهنگ تو دیتابیس سرویس تشخیص نیست.",
            )
            return
        title, artist = result

    label = f"{title} - {artist}" if artist else title
    await set_status(query, f"🎵 آهنگ اصلی این ویدیو: {label}\n🔎 در حال پیدا کردن نسخه‌ی رسمی...")

    try:
        best = await asyncio.to_thread(search_music, label)
    except Exception as e:
        logger.exception("search_music failed in find_song")
        await set_status(query, "❌ جستجو با خطا مواجه شد:\n" + str(e)[:300])
        return

    if not best:
        artist_txt = f"\nخواننده: {artist}" if artist else ""
        await set_status(
            query,
            f"❌ نسخه‌ی رسمی «{title}» رو پیدا نکردم.{artist_txt}\n"
            "می‌تونی از دکمه‌ی «🎵 جستجوی موزیک» با اسم دقیق‌تر امتحان کنی.",
        )
        return

    best_url = best.get("url") or best.get("webpage_url") or best.get("id")
    if best.get("id") and not str(best_url).startswith("http"):
        best_url = f"https://www.youtube.com/watch?v={best['id']}"

    await deliver_song(query, context, chat_id, best_url, title, artist)


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
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_MUSIC)}$"), music_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(handle_find_song, pattern=r"^song\|"))
    app.add_handler(CallbackQueryHandler(handle_music_page, pattern=r"^mpage\|"))
    app.add_handler(CallbackQueryHandler(handle_music_pick, pattern=r"^mpick\|"))

    logger.info("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
