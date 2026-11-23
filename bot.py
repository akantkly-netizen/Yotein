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
    python-telegram-bot[rate-limiter]==21.*
    yt-dlp
    shazamio>=0.9
    (اختیاری) aria2 روی سیستم + متغیر USE_ARIA2C=1

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
import hashlib
import hmac
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import shutil
import time
import html
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yt_dlp
import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
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

MAX_TELEGRAM_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))  # با Local Bot API Server می‌شه تا 2000
PROGRESS_UPDATE_INTERVAL = 2.0  # ثانیه، فاصله‌ی به‌روزرسانی نوار پیشرفت
PROGRESS_BAR_LENGTH = 18  # تعداد بلوک‌های نوار پیشرفت

# اگه یه Local Bot API Server (سقف آپلود ۲ گیگ به‌جای ۵۰ مگ) راه‌اندازی
# کردید، آدرسش رو اینجا بدید (پیش‌فرض همون آدرسیه که start.sh باهاش
# سرور رو بالا میاره). اگه ست نشه، از سرور رسمی تلگرام استفاده می‌شه.
LOCAL_API_URL = os.environ.get("LOCAL_API_URL", "")
if not LOCAL_API_URL and os.environ.get("TELEGRAM_API_ID") and os.environ.get("TELEGRAM_API_HASH"):
    # start.sh با همین آدرس سرور محلی رو بالا میاره؛ نیازی به ست کردن
    # دستی LOCAL_API_URL نیست، فقط کافیه TELEGRAM_API_ID/HASH رو بدید.
    LOCAL_API_URL = "http://localhost:8081"
if LOCAL_API_URL and "MAX_UPLOAD_MB" not in os.environ:
    MAX_TELEGRAM_UPLOAD_MB = 2000

# برای تشخیص آهنگ از روی صدای ویدیو (وقتی متادیتای پلتفرم اسم آهنگ رو
# نداره). یه توکن رایگان از https://dashboard.audd.io بگیر.
AUDD_API_KEY = os.environ.get("AUDD_API_KEY", "")
SONG_SNIPPET_SECONDS = 20  # طول قطعه‌ی صوتی که برای تشخیص فرستاده می‌شه

# Shazam از طریق کتابخونه‌ی shazamio: رایگان، بدون ثبت‌نام و کلید، کاتالوگ بزرگ.
# API غیررسمیه (ممکنه گاهی تغییر کنه یا محدود بشه)؛ برای خاموش کردنش SHAZAM_ENABLED=0
SHAZAM_ENABLED = os.environ.get("SHAZAM_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
# فقط با shazamio>=0.9: اگه مثلاً 0.001 ست بشه، جواب‌هایی که با اختلاف زمانی/فرکانسی زیاد پیدا شدن رد می‌شن
# (جواب غلط کمتر می‌شه). ولی آهنگ‌های اسپیدشده/پیچ‌شده‌ی تیک‌توک و ریلز هم رد می‌شن؛ پس پیش‌فرض خاموشه.
try:
    SHAZAM_MAX_SKEW = float(os.environ.get("SHAZAM_MAX_SKEW", "") or 0) or None
except ValueError:
    SHAZAM_MAX_SKEW = None
if SHAZAM_MAX_SKEW is not None and SHAZAM_MAX_SKEW <= 0:
    SHAZAM_MAX_SKEW = None

# سرعت دانلود yt-dlp: تعداد قطعه‌های موازی برای HLS/DASH (۱ = خاموش)
YTDLP_CONCURRENT_FRAGMENTS = max(1, int(os.environ.get("YTDLP_CONCURRENT_FRAGMENTS", "4")))
# aria2c به‌عنوان دانلودر خارجی (اختیاری): هم باید نصب باشه (apt install aria2) هم USE_ARIA2C=1 ست بشه
USE_ARIA2C = (
    os.environ.get("USE_ARIA2C", "0").strip().lower() in ("1", "true", "yes", "on")
    and shutil.which("aria2c") is not None
)
# کاور (تامبنیل) + متادیتا داخل فایل mp3 جاسازی می‌شه؛ اگه جاسازی خطا داد، یه‌بار بدون کاور دوباره می‌سازه
EMBED_AUDIO_COVER = os.environ.get("EMBED_AUDIO_COVER", "1").strip().lower() not in ("0", "false", "no", "off")

# سرویس دوم تشخیص آهنگ (اختیاری) - وقتی AudD جواب نده امتحان می‌شه.
# host/access_key/access_secret رو از پنل پروژه‌ی ACRCloud بگیر.
ACR_ACCESS_KEY = os.environ.get("ACR_ACCESS_KEY", "")
ACR_ACCESS_SECRET = os.environ.get("ACR_ACCESS_SECRET", "")
ACR_HOST = os.environ.get("ACR_HOST", "")  # مثلا identify-eu-west-1.acrcloud.com

# برای بهتر شدن دقت تشخیص آهنگ اصلی: قبل از تشخیص از روی صدا (که کندتر
# و محدودتره)، Gemini عنوان/کپشن ویدیو رو می‌خونه چون خیلی وقتا خود
# کپشن اسم آهنگ رو داره. کلید رایگان از https://aistudio.google.com بگیر.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

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
PINTEREST_URL_RE = re.compile(
    r"(https?://)?(www\.)?pinterest\.[a-z.]+/pin/[\w\-]+/?[\w\-?&=%.]*"
    r"|(https?://)?pin\.it/[\w\-]+"
)
SUPPORTED_URL_RE = re.compile(
    f"({INSTAGRAM_URL_RE.pattern})|({YOUTUBE_URL_RE.pattern})|({TIKTOK_URL_RE.pattern})"
)
# لینک پلی‌لیست یوتیوب (همونی که /playlist?list=... داره). لینک ویدیو که
# &list=... داره هنوز مثل قبل فقط همون یه ویدیو حساب می‌شه.
PLAYLIST_URL_RE = re.compile(
    r"(https?://)?(www\.|m\.|music\.)?youtube\.com/playlist\?[^\s]*list=[\w\-]+[^\s]*"
)
PLAYLIST_PAGE_SIZE = 5      # تعداد ویدیو تو هر صفحه
PLAYLIST_MAX_ITEMS = 300    # سقف تعداد ویدیوهایی که از یه پلی‌لیست خونده می‌شه

# ------------------------------------------------------------------
# کیبورد پایین صفحه (Reply Keyboard)
# ------------------------------------------------------------------
BTN_HELP = "📖 راهنما"
BTN_ABOUT = "ℹ️ درباره ربات"
BTN_MUSIC = "🎵 جستجوی موزیک"
BTN_ACCOUNT = "👤 حساب من"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_MUSIC, BTN_ACCOUNT], [BTN_HELP, BTN_ABOUT]],
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

# نتایج پلی‌لیست‌های بازشده: key (chat_id_msgid) -> {"entries", "title", ...}
PLAYLISTS = {}
MUSIC_PAGE_SIZE = 10


# ------------------------------------------------------------------
# توابع کمکی yt-dlp
# ------------------------------------------------------------------
def build_ydl_opts(use_cookies: bool = False, player_client=None, **overrides):
    """
    use_cookies=False (پیش‌فرض): بدون کوکی درخواست می‌ده، مثل یه کاربر
    عادی. use_cookies=True: فقط وقتی سایت صریحاً خطای "sign in / not a
    bot" داده، به‌عنوان تلاش دوم کوکی رو اضافه می‌کنه.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": YTDLP_CONCURRENT_FRAGMENTS,
    }
    if USE_ARIA2C:
        opts["external_downloader"] = {"default": "aria2c"}
    if use_cookies and COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE
    if player_client:
        # فقط روی یوتیوب اثر داره؛ برای سایت‌های دیگه نادیده گرفته می‌شه
        opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
    opts.update(overrides)
    return opts


BOT_CHECK_SIGNS = (
    "sign in to confirm",
    "not a bot",
    "confirm you're not a bot",
    "confirm you are not a bot",
    "login required",
    "this video is only available",
    # اینستاگرام (مخصوصاً استوری‌ها) همیشه لاگین می‌خواد
    "you need to log in",
    "log in to access",
    "use --cookies",
)

# خطای ۴۰۳ بعد از اینکه اطلاعات ویدیو با موفقیت گرفته شد: یوتیوب خود فایل
# رو نمی‌ده (IP سرور، کوکی، یا کلاینتی که PO Token می‌خواد)
FORBIDDEN_SIGNS = (
    "http error 403",
    "403: forbidden",
    "unable to download video data",
)

# اینستاگرام: پست برای بعضی مخاطب‌ها (محدودیت سن/مخاطب) در دسترس نیست.
# با نشست واردشده یک‌بار دوباره امتحان می‌شه؛ اگه اون حساب هم اجازه نداشت،
# دسترسی دور زده نمی‌شه و پیام روشن به کاربر داده می‌شه.
AUDIENCE_RESTRICTED_SIGNS = (
    "isn't available to everyone",
    "can't be seen by certain audiences",
)


class InstagramAudienceRestricted(Exception):
    """محتوا برای حسابِ (واردشده یا بدون ورود) قابل‌مشاهده نیست؛ نه خصوصی، نه لینک خراب."""

    def __init__(self, had_session: bool):
        self.had_session = had_session
        if had_session:
            msg = (
                "🔒 این محتوا برای حسابی که ربات با آن وارد شده قابل‌مشاهده نیست "
                "(اینستاگرام آن را فقط برای مخاطبان خاص یا با محدودیت سنی نشان می‌دهد).\n"
                "پست خصوصی یا لینک خراب نیست، اما ربات امکان دانلودش را ندارد."
            )
        else:
            msg = (
                "🔒 این محتوا فقط برای مخاطبان خاصی (مثلاً با محدودیت سنی) در "
                "اینستاگرام قابل‌مشاهده است و ربات حساب واردشده‌ای برای امتحان "
                "دوباره ندارد.\nپست خصوصی یا لینک خراب نیست، اما ربات امکان دانلودش را ندارد."
            )
        super().__init__(msg)


ALT_YOUTUBE_CLIENT = "android_vr"  # کلاینت جایگزین که معمولاً PO Token نمی‌خواد


def _looks_like_bot_check(err_text: str) -> bool:
    t = err_text.lower()
    return any(s in t for s in BOT_CHECK_SIGNS)


def _is_retryable(err_text: str) -> bool:
    t = err_text.lower()
    return _looks_like_bot_check(t) or any(s in t for s in FORBIDDEN_SIGNS)


def _is_audience_restricted(err_text: str) -> bool:
    t = err_text.lower().replace("\u2019", "'")  # apostrophe تایپوگرافیک
    return any(s in t for s in AUDIENCE_RESTRICTED_SIGNS)


def _retry_audience_restricted_once(func, first_error, *args, **kwargs):
    """
    فقط یک‌بار و فقط با نشست واردشده (کوکی) دوباره امتحان می‌کنه؛ بدون حلقه.
    محتوای کوکی/متغیر هیچ‌جا لاگ نمی‌شه.
    """
    if not (COOKIES_FILE and Path(COOKIES_FILE).exists()):
        raise InstagramAudienceRestricted(had_session=False) from first_error
    logger.info("audience-restricted content, retrying once with the logged-in session")
    try:
        return func(*args, use_cookies=True, player_client=None, **kwargs)
    except Exception as e:
        if _is_audience_restricted(str(e)):
            raise InstagramAudienceRestricted(had_session=True) from e
        raise


def _run_with_cookie_fallback(func, *args, **kwargs):
    """
    اول بدون کوکی و با کلاینت پیش‌فرض امتحان می‌کنه. اگه خطا «ثابت کن ربات
    نیستی» یا ۴۰۳ بود، به ترتیب تلاش‌های بعدی رو انجام می‌ده:
      ۱) با کوکی (اگه داریم)
      ۲) بدون کوکی با کلاینت جایگزین یوتیوب (android_vr)
    برای هر خطای دیگه (لینک اشتباه، پست خصوصی و ...) مستقیم پرتاب می‌شه.
    """
    try:
        return func(*args, use_cookies=False, player_client=None, **kwargs)
    except Exception as first_error:
        if _is_audience_restricted(str(first_error)):
            return _retry_audience_restricted_once(func, first_error, *args, **kwargs)
        if not _is_retryable(str(first_error)):
            raise
        last_error = first_error

    steps = []
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        steps.append((True, None))
    steps.append((False, ALT_YOUTUBE_CLIENT))

    for use_cookies, client in steps:
        logger.info("retrying (cookies=%s, client=%s) after: %s", use_cookies, client, str(last_error)[:120])
        try:
            return func(*args, use_cookies=use_cookies, player_client=client, **kwargs)
        except Exception as e:
            if _is_audience_restricted(str(e)):
                raise InstagramAudienceRestricted(had_session=use_cookies) from e
            last_error = e
            if not _is_retryable(str(e)):
                raise
    raise last_error


def _extract_info_impl(url: str, use_cookies: bool = False, player_client=None) -> dict:
    with yt_dlp.YoutubeDL(build_ydl_opts(use_cookies=use_cookies, player_client=player_client)) as ydl:
        return ydl.extract_info(url, download=False)


def extract_info(url: str) -> dict:
    """اطلاعات لینک (فرمت‌های موجود و ...) رو بدون دانلود می‌گیره."""
    return _run_with_cookie_fallback(_extract_info_impl, url)


def search_music(query: str, limit: int = 8):
    """
    اول تو یوتیوب‌میوزیک می‌گرده (کاتالوگ رسمی گوگل)، اگه چیزی پیدا نشد
    (مثلاً برای خیلی از آهنگ‌های فارسی) به یوتیوب عادی و بعد به
    ساندکلاود (برای ریمیکس/تولیدات مستقل) فال‌بک می‌کنه.
    امتیازدهی طوریه که نسخه‌ی رسمی/آفیشیال رو در اولویت می‌ذاره و
    ریمیکس/کاور/لایو/اسپیدآپ رو عقب می‌زنه مگه خود متن جستجو دنبال
    اونا باشه.
    """
    entries = []
    for prefix in (f"ytmsearch{limit}:", f"ytsearch{limit}:", f"scsearch{limit}:"):
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


SOURCE_PREFIXES = {
    "ytmusic": "ytmsearch",
    "youtube": "ytsearch",
    "soundcloud": "scsearch",
}


def search_music_list(query: str, limit: int = 50, source: str = "ytmusic"):
    """
    فقط تو همون منبع مشخص‌شده می‌گرده (پیش‌فرض یوتیوب‌میوزیک که کاتالوگ
    رسمیه، بدون کاور/ریمیکس الکی). برای خواننده‌های خارجی معمولاً همه‌ی
    آهنگ‌های شناخته‌شده‌شون از همینجا درمیاد. اگه کاربر دستی خواست
    (source='youtube')، جستجوی عادی یوتیوب انجام می‌شه.
    """
    prefix = SOURCE_PREFIXES.get(source, "ytmsearch")
    try:
        with yt_dlp.YoutubeDL(build_ydl_opts(extract_flat="in_playlist")) as ydl:
            result = ydl.extract_info(f"{prefix}{limit}:{query}", download=False)
        return [e for e in (result.get("entries") or []) if e]
    except Exception:
        logger.exception("search_music_list failed for source %s", source)
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


def build_music_page(key: str, entries: list, page: int, source: str = "ytmusic"):
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

    # فقط وقتی هنوز رو یوتیوب‌میوزیک هستیم این گزینه رو نشون بده؛ وقتی
    # کاربر خودش زده رو یوتیوب عادی، دیگه نیازی به این دکمه نیست
    if source != "youtube":
        buttons.append([InlineKeyboardButton("🔍 جستجو در یوتیوب", callback_data=f"myoutube|{key}")])

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


def _download_full_audio_impl(url: str, out_dir: str, use_cookies: bool = False, player_client=None):
    """کل صدای ویدیو رو دانلود می‌کنه (یه‌بار) تا از روش چند تیکه‌ی مختلفش
    برای تشخیص استفاده کنیم، بدون نیاز به دانلود دوباره."""
    out_tmpl = os.path.join(out_dir, "full.%(ext)s")
    opts = build_ydl_opts(
        use_cookies=use_cookies,
        player_client=player_client,
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

    return mp3_path, (info.get("duration") or 0)


def download_full_audio(url: str, out_dir: str):
    """(مسیر فایل mp3، طول ویدیو به ثانیه) رو برمی‌گردونه."""
    return _run_with_cookie_fallback(_download_full_audio_impl, url, out_dir)


def make_snippet(full_audio_path: str, out_dir: str, start: float = 0, duration: int = SONG_SNIPPET_SECONDS, name: str = "snippet") -> str:
    """از یه فایل صوتی کامل، یه تیکه‌ی مشخص رو با ffmpeg می‌بره (بدون دانلود دوباره)."""
    out_path = os.path.join(out_dir, f"{name}.mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(max(0, start)), "-i", full_audio_path, "-t", str(duration), "-vn", out_path],
            check=True,
            capture_output=True,
            timeout=60,
        )
        if os.path.exists(out_path):
            return out_path
    except Exception:
        logger.exception("ffmpeg snippet failed, using full audio")
    return full_audio_path


def call_gemini(prompt: str, timeout: int = 20):
    """یه پرامپت به Gemini می‌فرسته و متن خام پاسخ رو برمی‌گردونه، یا None اگه شکست خورد."""
    if not GEMINI_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    try:
        resp = requests.post(
            url,
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
            },
            timeout=timeout,
        )
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        logger.exception("Gemini call failed")
        return None


def ai_identify_song(video_title: str, description: str, uploader: str):
    """
    با کمک Gemini از روی عنوان/کپشن/آپلودکننده‌ی ویدیو، اسم آهنگ و
    خواننده‌ی واقعی استفاده‌شده رو حدس می‌زنه (وقتی متادیتای صریح
    پلتفرم اسم آهنگ رو نداشت). خیلی وقتا خود کپشن اسم آهنگ رو داره،
    مخصوصاً برای آهنگ‌های فارسی/گمنام که تو یوتیوب‌میوزیک نیستن. ریمیکس
    و کاور و امثالش رو هم تشخیص می‌ده.
    برمی‌گردونه: (title, artist, variant_label) یا None.
    """
    if not GEMINI_API_KEY:
        return None

    prompt = (
        "You are helping identify the background music/song used in a short "
        "social-media video (Instagram Reel, TikTok, or YouTube Short). Given "
        "the video's title, caption/description, and uploader name below, "
        "figure out the actual song name and artist IF it is explicitly "
        "mentioned or clearly implied in the text (e.g. 'audio used: X by Y', "
        "hashtags naming a song, or the caption itself naming a track). Pay "
        "special attention to Persian/Farsi text: captions are often written "
        "in Finglish (Persian words spelled with Latin letters, e.g. "
        "'ahangesh Sirvan Khosravi bood') or in Persian script, and may name "
        "underground/regional artists not on YouTube Music — normalize any "
        "Persian artist/song name you recognize to its standard Persian-script "
        "or common transliterated form. Do not guess randomly — if the text "
        "gives no real clue, respond with title=null.\n\n"
        f"Video title: {video_title}\n"
        f"Description/caption: {description[:1500]}\n"
        f"Uploader: {uploader}\n\n"
        "Respond ONLY with compact JSON in exactly this shape, no extra text:\n"
        '{"title": string|null, "artist": string|null, '
        '"is_remix_or_cover": boolean, "variant_label": string|null}\n'
        'variant_label should be a short tag like "Remix", "Cover", '
        '"Funk Version", "Slowed + Reverb" if the audio is clearly a variant '
        "of the original, otherwise null."
    )
    raw = call_gemini(prompt)
    if not raw:
        return None

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]

    try:
        data = json.loads(cleaned)
    except Exception:
        logger.warning("could not parse Gemini JSON response: %s", raw[:200])
        return None

    title = data.get("title")
    if not title:
        return None
    artist = data.get("artist")
    variant = data.get("variant_label")
    return title, artist, variant


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


def recognize_song_via_acrcloud(audio_path: str):
    """
    سرویس دوم تشخیص آهنگ (اختیاری). فقط وقتی AudD جواب نده امتحان می‌شه.
    برمی‌گردونه: (title, artist) یا None.
    """
    if not (ACR_ACCESS_KEY and ACR_ACCESS_SECRET and ACR_HOST):
        return None

    http_method = "POST"
    http_uri = "/v1/identify"
    data_type = "audio"
    signature_version = "1"
    timestamp = str(time.time())
    string_to_sign = "\n".join(
        [http_method, http_uri, ACR_ACCESS_KEY, data_type, signature_version, timestamp]
    )
    sign = base64.b64encode(
        hmac.new(
            ACR_ACCESS_SECRET.encode("ascii"), string_to_sign.encode("ascii"), digestmod=hashlib.sha1
        ).digest()
    ).decode("ascii")

    try:
        sample_bytes = os.path.getsize(audio_path)
        with open(audio_path, "rb") as f:
            files = {"sample": (os.path.basename(audio_path), f, "audio/mpeg")}
            data = {
                "access_key": ACR_ACCESS_KEY,
                "sample_bytes": sample_bytes,
                "timestamp": timestamp,
                "signature": sign,
                "data_type": data_type,
                "signature_version": signature_version,
            }
            resp = requests.post(f"https://{ACR_HOST}/v1/identify", files=files, data=data, timeout=30)
        result = resp.json()
    except Exception:
        logger.exception("ACRCloud request failed")
        return None

    try:
        music = result["metadata"]["music"][0]
    except Exception:
        return None

    title = music.get("title")
    if not title:
        return None
    artists = music.get("artists") or []
    artist = artists[0].get("name") if artists else None
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
                "size": total or None,
            }
        )

    # اگر هیچ فرمت مجزایی پیدا نشد (مثلا فقط یه فایل ترکیبی هست)
    if not options and formats:
        best = max(formats, key=lambda f: f.get("height") or 0)
        size = best.get("filesize") or best.get("filesize_approx")
        size_txt = f" - {format_size(size)}" if size else ""
        options.append({"label": f"🎬 کیفیت پیش‌فرض{size_txt}", "format_id": "best", "kind": "video", "size": size or None})

    # گزینه‌ی فقط صدا همیشه اضافه میشه
    audio_label = "🎵 فقط صدا (MP3)"
    if audio_size:
        audio_label += f" - {format_size(audio_size)}"
    options.append({"label": audio_label, "format_id": "bestaudio", "kind": "audio", "size": audio_size or None})
    return options


def _download_media_impl(
    url: str, format_id: str, kind: str, out_dir: str, progress_state=None, use_cookies: bool = False, player_client=None
) -> str:
    out_tmpl = os.path.join(out_dir, "%(id)s.%(ext)s")
    hooks = [make_progress_hook(progress_state)] if progress_state is not None else []

    def _audio_opts(with_cover: bool):
        pps = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        extra = {}
        if with_cover:
            # تامبنیل رو به jpg تبدیل می‌کنه و همراه متادیتا داخل mp3 می‌ذاره (کاور آهنگ تو پلیر تلگرام)
            extra["writethumbnail"] = True
            pps = (
                [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}]
                + pps
                + [{"key": "FFmpegMetadata", "add_metadata": True}, {"key": "EmbedThumbnail"}]
            )
        return build_ydl_opts(
            use_cookies=use_cookies,
            player_client=player_client,
            format="bestaudio/best",
            outtmpl=out_tmpl,
            progress_hooks=hooks,
            postprocessors=pps,
            **extra,
        )

    if kind == "audio":
        opts = _audio_opts(EMBED_AUDIO_COVER)
    else:
        # یوتیوب دیگه معمولاً فرمت تکی (صدا+تصویر با هم) نمی‌ده، پس "best"
        # ساده خطای "Requested format is not available" می‌ده. همیشه
        # بهترین تصویر + بهترین صدا رو می‌گیریم و با ffmpeg ترکیب می‌کنیم؛
        # فقط اگه جدا نبودن، فرمت تکی رو برمی‌داریم.
        if format_id == "best720":
            # برای ارسال آهنگ (حجم کمتر): حداکثر ۷۲۰p
            fmt = "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"
        elif format_id == "best":
            fmt = "bv*+ba/b"
        else:
            fmt = f"{format_id}+ba/{format_id}/bv*+ba/b"
        opts = build_ydl_opts(
            use_cookies=use_cookies,
            player_client=player_client,
            format=fmt,
            outtmpl=out_tmpl,
            merge_output_format="mp4",
            progress_hooks=hooks,
        )

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
    except yt_dlp.utils.DownloadError as e:
        # فقط اگه خطا از مرحله‌ی پردازش (جاسازی کاور) بود، یه‌بار بدون کاور دوباره می‌سازیم
        if not (kind == "audio" and EMBED_AUDIO_COVER and "postprocessing" in str(e).lower()):
            raise
        logger.warning("cover embedding failed; retrying the audio once without a cover")
        with yt_dlp.YoutubeDL(_audio_opts(False)) as ydl:
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
        "۲. اول تو یوتیوب‌میوزیک (کاتالوگ رسمی) می‌گردم؛ اگه اسم خواننده"
        " بفرستی، همه‌ی آهنگ‌های شناخته‌شده‌اش از همونجا میاد (برای"
        " خواننده‌های خارجی معمولاً کامله)؛ نتیجه‌ها ۱۰تا-۱۰تا"
        " صفحه‌بندی شدن با دکمه‌های «◀️ صفحه قبل» / «▶️ صفحه بعد»\n"
        "۳. روی اسم هر آهنگ که بزنی، همون دکمه‌های کیفیت/فقط‌صدای بالا"
        " براش میاد تا انتخاب کنی\n"
        "۴. اگه چیزی پیدا نشد یا کم بود (مثلاً خیلی از آهنگ‌های فارسی"
        " تو یوتیوب‌میوزیک نیستن)، پایین لیست دکمه‌ی «🔍 جستجو در"
        " یوتیوب» هست؛ بزن تا همون جستجو رو تو یوتیوب عادی هم انجام"
        " بدم\n\n"
        "📃 پلی‌لیست یوتیوب\n"
        "لینک یه پلی‌لیست (همونی که /playlist?list=... داره) رو بفرست؛ اطلاعات"
        " پلی‌لیست و ویدیوهاش ۵تا-۵تا صفحه‌بندی می‌شن. روی هر ویدیو بزنی،"
        " اطلاعات و تصویرش میاد و می‌تونی کیفیت رو انتخاب و دانلودش کنی."
        " با «▶️ صفحه بعد» و «◀️ صفحه قبل» بین صفحه‌ها برو و آخرش"
        " «✅ کارم تموم شد» رو بزن تا دکمه‌ها بسته بشن (اطلاعات پلی‌لیست"
        " می‌مونه)\n\n"
        "🎞 ارسال خود فایل ویدیو (نه لینک)\n"
        "اگه خود کلیپ/ویدیو رو برام بفرستی (مثلاً فایل یه ریلز)، می‌پرسم"
        " «چیکار کنم؟» با دو دکمه: «🎵 استخراج صدا» (صدای ویدیو رو"
        " mp3 می‌کنم و می‌فرستم) یا «🎧 پیدا کردن آهنگ اصلی» (آهنگ"
        " ویدیو رو تشخیص می‌دم و نسخه‌ی رسمیش رو با ویس می‌فرستم)\n"
        "• اگه برای ویدیو کپشن بنویسی، تشخیص دقیق‌تر می‌شه\n"
        "• دریافت فایل بزرگ‌تر از ۲۰ مگابایت فقط با سرور محلی تلگرام"
        " (TELEGRAM_API_ID/HASH) ممکنه\n\n"
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
        "👤 حساب، سهمیه و VIP\n"
        "• دکمه‌ی «👤 حساب من»: اطلاعات حسابت، تعداد دانلودها، حجم مصرف‌شده‌ی"
        " امروز و وضعیت اشتراک\n"
        f"• هر کاربر روزانه تا {format_volume(DAILY_QUOTA_MB * MB)} حجم دانلود داره که"
        " هر شب نیمه‌شب (به وقت ایران) ریست می‌شه\n"
        "• با نوشتن «آیدی» یه آیدی ۸ رقمی اختصاصی می‌گیری؛ اون رو به مدیر بده"
        " تا اشتراک VIP (دانلود نامحدود برای چند روز) برات فعال کنه\n\n"
        "⚠️ نکات مهم\n"
        f"• برای جلوگیری از اسپم، هر {REQUEST_COOLDOWN_SECONDS} ثانیه فقط یه درخواست جدید"
        " می‌تونی بدی؛ اگه زود بفرستی، می‌گم چند ثانیه صبر کنی\n"
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

    status_msg = await update.message.reply_text(f"🔎 دارم تو یوتیوب‌میوزیک دنبال «{query_text}» می‌گردم...")
    chat_id = update.effective_chat.id

    try:
        entries = await asyncio.to_thread(search_music_list, query_text, 50, "ytmusic")
    except Exception as e:
        logger.exception("search_music_list failed")
        await status_msg.edit_text("❌ جستجو با خطا مواجه شد:\n" + str(e)[:300])
        return

    key = f"{chat_id}_{status_msg.message_id}"
    MUSIC_SESSIONS[key] = {"entries": entries, "query": query_text, "source": "ytmusic"}

    if not entries:
        markup, _ = build_music_page(key, [], 0, "ytmusic")
        await status_msg.edit_text(
            f"تو یوتیوب‌میوزیک چیزی برای «{query_text}» پیدا نشد. می‌تونی از دکمه‌ی"
            " پایین تو یوتیوب عادی هم جستجو کنی:",
            reply_markup=markup,
        )
        return

    markup, total_pages = build_music_page(key, entries, 0, "ytmusic")
    text = (
        f"🎵 نتایج یوتیوب‌میوزیک برای «{query_text}» ({len(entries)} مورد) — صفحه ۱ از {total_pages}:\n"
        "یکی رو انتخاب کن، یا اگه کافی نبود پایین «جستجو در یوتیوب» رو بزن:"
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
    source = session.get("source", "ytmusic")
    source_label = "یوتیوب‌میوزیک" if source == "ytmusic" else "یوتیوب"
    markup, total_pages = build_music_page(key, entries, page, source)
    text = (
        f"🎵 نتایج {source_label} برای «{session['query']}» ({len(entries)} مورد) — صفحه {page + 1} از {total_pages}:\n"
        "یکی رو انتخاب کن:"
    )
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("music page edit failed: %s", e)


async def handle_music_youtube_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کاربر دکمه‌ی «جستجو در یوتیوب» رو زده (چون نتایج یوتیوب‌میوزیک کم یا هیچی نبود)."""
    query = update.callback_query
    await query.answer()

    try:
        _, key = query.data.split("|", 1)
    except ValueError:
        return

    session = MUSIC_SESSIONS.get(key)
    if not session:
        await set_status(query, "⌛ این جستجو منقضی شده، دوباره از دکمه‌ی جستجوی موزیک استفاده کن.")
        return

    query_text = session["query"]
    await set_status(query, f"🔎 دارم تو یوتیوب دنبال «{query_text}» می‌گردم...")

    try:
        entries = await asyncio.to_thread(search_music_list, query_text, 50, "youtube")
    except Exception as e:
        logger.exception("youtube fallback search failed")
        await set_status(query, "❌ جستجو با خطا مواجه شد:\n" + str(e)[:300])
        return

    session["entries"] = entries
    session["source"] = "youtube"

    if not entries:
        await set_status(query, f"تو یوتیوب هم چیزی برای «{query_text}» پیدا نشد.")
        return

    markup, total_pages = build_music_page(key, entries, 0, "youtube")
    text = (
        f"🎵 نتایج یوتیوب برای «{query_text}» ({len(entries)} مورد) — صفحه ۱ از {total_pages}:\n"
        "یکی رو انتخاب کن:"
    )
    await set_status(query, text)
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("music youtube fallback markup edit failed: %s", e)


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


async def show_quality_picker(message, chat_id: int, url: str, status_msg=None, offer_find_song: bool = True, user_msg_id=None):
    """
    اطلاعات لینک رو می‌گیره، تامبنیل+توضیحات رو نشون می‌ده و دکمه‌های
    کیفیت رو می‌سازه. هم برای لینک مستقیم استفاده می‌شه هم برای نتیجه‌ی
    جستجوی موزیک. اگه status_msg داده بشه (پیام "در حال جستجو/گرفتن
    اطلاعات")، در پایان حذف می‌شه. offer_find_song=False برای وقتی که
    خودش نتیجه‌ی جستجوی آهنگه (نیازی به دکمه‌ی پیدا کردن آهنگ نیست).
    """
    try:
        info = await asyncio.to_thread(extract_info, url)
    except Exception as e:
        logger.exception("extract_info failed")
        if isinstance(e, InstagramAudienceRestricted):
            err_text = str(e)
        else:
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
        "user_msg_id": user_msg_id,   # پیام لینکِ خود کاربر (بعد از ارسال پاک می‌شه)
        "link": normalize_link(url),
        "video_meta": {
            "title": info.get("title") or "",
            "description": info.get("description") or "",
            "uploader": uploader,
        },
    }

    buttons = []
    for o in options:
        cb_data = f"dl|{key}|{o['format_id']}|{o['kind']}"
        buttons.append([InlineKeyboardButton(o["label"], callback_data=cb_data)])

    if offer_find_song:
        buttons.append([InlineKeyboardButton("🎵 پیدا کردن آهنگ اصلی ویدیو", callback_data=f"song|{key}")])

    await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))


# ------------------------------------------------------------------
# پلی‌لیست یوتیوب: ۵تا-۵تا صفحه‌بندی، انتخاب ویدیو، «کارم تموم شد»
# ------------------------------------------------------------------
def _extract_playlist_impl(url: str, use_cookies: bool = False, player_client=None) -> dict:
    opts = build_ydl_opts(
        use_cookies=use_cookies,
        player_client=player_client,
        extract_flat="in_playlist",   # فقط فهرست (سریع)، بدون باز کردن تک‌تک ویدیوها
        noplaylist=False,
        playlistend=PLAYLIST_MAX_ITEMS,
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def extract_playlist(url: str) -> dict:
    return _run_with_cookie_fallback(_extract_playlist_impl, url)


def parse_playlist_entries(info: dict):
    """(فهرست ویدیوهای قابل‌دانلود، تعداد موارد خصوصی/حذف‌شده) رو برمی‌گردونه."""
    entries, skipped = [], 0
    for e in info.get("entries") or []:
        if not e:
            continue
        vid = e.get("id")
        title = (e.get("title") or "").strip()
        if not vid or title.startswith("[Private video]") or title.startswith("[Deleted video]"):
            skipped += 1
            continue
        entries.append({
            "id": vid,
            "title": title or vid,
            "duration": e.get("duration"),
        })
    return entries, skipped


def playlist_info_text(session: dict) -> str:
    """بلوک اطلاعات پلی‌لیست که همیشه تو پیام می‌مونه (حتی بعد از «کارم تموم شد»)."""
    title = session["title"]
    if len(title) > 200:
        title = title[:200] + "…"
    lines = [f"📃 {title}"]
    if session["uploader"]:
        lines.append(f"👤 {session['uploader']}")
    count_line = f"🎞 {len(session['entries'])} ویدیو"
    if session["skipped"]:
        count_line += f" ({session['skipped']} مورد خصوصی/حذف‌شده نادیده گرفته شد)"
    lines.append(count_line)
    desc = (session["description"] or "").strip()
    if desc:
        lines.append(desc[:250] + ("…" if len(desc) > 250 else ""))
    return "\n".join(lines)


def playlist_total_pages(session: dict) -> int:
    return max(1, math.ceil(len(session["entries"]) / PLAYLIST_PAGE_SIZE))


def playlist_page_text(session: dict, page: int) -> str:
    return (
        f"{playlist_info_text(session)}\n\n"
        f"صفحه {page + 1} از {playlist_total_pages(session)} — یکی رو انتخاب کن:"
    )


def build_playlist_markup(key: str, session: dict, page: int) -> InlineKeyboardMarkup:
    """۵ ویدیو + (صفحه قبل/بعد) + «کارم تموم شد»."""
    entries = session["entries"]
    total_pages = playlist_total_pages(session)
    start = page * PLAYLIST_PAGE_SIZE

    buttons = []
    for i, e in enumerate(entries[start:start + PLAYLIST_PAGE_SIZE]):
        idx = start + i
        label = f"{idx + 1}. {e['title']}"
        if e.get("duration"):
            m, sec = divmod(int(e["duration"]), 60)
            h, m = divmod(m, 60)
            label += f" ({h}:{m:02d}:{sec:02d})" if h else f" ({m}:{sec:02d})"
        if len(label) > 62:
            label = label[:59] + "…"
        buttons.append([InlineKeyboardButton(label, callback_data=f"plpick|{key}|{idx}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ صفحه قبل", callback_data=f"plpage|{key}|{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️ صفحه بعد", callback_data=f"plpage|{key}|{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("✅ کارم تموم شد", callback_data=f"pldone|{key}")])
    return InlineKeyboardMarkup(buttons)


async def edit_message_with_markup(query, text: str, markup):
    """متن/کپشن و دکمه‌های یه پیام رو یه‌جا عوض می‌کنه؛ markup=None دکمه‌ها رو می‌بنده."""
    try:
        if query.message.photo:
            await query.edit_message_caption(caption=text, reply_markup=markup)
        else:
            await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("edit_message_with_markup failed: %s", e)


async def show_playlist(message, chat_id: int, url: str, status_msg):
    try:
        info = await asyncio.to_thread(extract_playlist, url)
    except Exception as e:
        logger.exception("extract_playlist failed")
        await status_msg.edit_text(
            "❌ نتونستم پلی‌لیست رو بخونم. ممکنه خصوصی باشه یا لینک اشتباه باشه.\n"
            "جزئیات خطا: " + str(e)[:300]
        )
        return

    entries, skipped = parse_playlist_entries(info)
    if not entries:
        await status_msg.edit_text("این پلی‌لیست ویدیوی قابل‌دسترسی نداره (خالیه یا همه‌ی ویدیوهاش خصوصی/حذف‌شده‌ان).")
        return

    session = {
        "entries": entries,
        "skipped": skipped,
        "title": info.get("title") or "پلی‌لیست",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "description": info.get("description") or "",
    }

    thumb = None
    thumbs = info.get("thumbnails") or []
    if thumbs and thumbs[-1].get("url"):
        thumb = thumbs[-1]["url"]
    if not thumb:
        thumb = f"https://i.ytimg.com/vi/{entries[0]['id']}/hqdefault.jpg"

    caption = playlist_page_text(session, 0)
    await status_msg.delete()
    try:
        sent = await message.reply_photo(photo=thumb, caption=caption)
    except Exception:
        logger.exception("playlist thumbnail failed, falling back to text")
        sent = await message.reply_text(caption)

    key = f"{chat_id}_{sent.message_id}"
    PLAYLISTS[key] = session
    await sent.edit_reply_markup(reply_markup=build_playlist_markup(key, session, 0))


async def handle_playlist_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, key, page_str = query.data.split("|", 2)
        page = int(page_str)
    except ValueError:
        return
    session = PLAYLISTS.get(key)
    if not session:
        await set_status(query, "⌛ این پلی‌لیست منقضی شده، لینکش رو دوباره بفرست.")
        return
    page = max(0, min(page, playlist_total_pages(session) - 1))
    await edit_message_with_markup(
        query, playlist_page_text(session, page), build_playlist_markup(key, session, page)
    )


async def handle_playlist_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """روی یکی از ویدیوهای پلی‌لیست زده شد -> اطلاعات و گرافیک و دکمه‌های دانلودش میاد."""
    query = update.callback_query
    await query.answer()
    try:
        _, key, idx_str = query.data.split("|", 2)
        idx = int(idx_str)
    except ValueError:
        return
    session = PLAYLISTS.get(key)
    if not session:
        await set_status(query, "⌛ این پلی‌لیست منقضی شده، لینکش رو دوباره بفرست.")
        return
    if idx < 0 or idx >= len(session["entries"]):
        return

    entry = session["entries"][idx]
    url = f"https://www.youtube.com/watch?v={entry['id']}"
    chat_id = query.message.chat_id
    status_msg = await query.message.reply_text("⏳ دارم اطلاعات ویدیو رو می‌گیرم...")
    await show_quality_picker(query.message, chat_id, url, status_msg)


async def handle_playlist_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«کارم تموم شد»: دکمه‌های شیشه‌ای بسته می‌شن ولی اطلاعات پلی‌لیست می‌مونه."""
    query = update.callback_query
    await query.answer()
    try:
        _, key = query.data.split("|", 1)
    except ValueError:
        return
    session = PLAYLISTS.pop(key, None)
    if session:
        base = playlist_info_text(session)
    else:
        base = query.message.caption or query.message.text or ""
    await edit_message_with_markup(query, f"{base}\n\n✅ کارم تموم شد.", None)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else chat_id

    # مدیر وسط مراحل پنل (آیدی کاربر / تعداد روز) -> بدون تایمر ضداسپم
    if is_admin(user_id) and user_id in ADMIN_STATE:
        await admin_handle_text(update, context, text)
        return

    remaining = cooldown_remaining(_last_message_at, user_id, record=False)
    if remaining:
        await warn_slow_down(update.message, user_id, remaining)
        return

    if chat_id in WAITING_MUSIC_QUERY:
        WAITING_MUSIC_QUERY.discard(chat_id)
        if text:
            mark_request(_last_message_at, user_id)
        await handle_music_query(update, context, text)
        return

    pl_match = PLAYLIST_URL_RE.search(text)
    if pl_match:
        mark_request(_last_message_at, user_id)
        status_msg = await update.message.reply_text("⏳ دارم پلی‌لیست رو می‌خونم...")
        await show_playlist(update.message, chat_id, pl_match.group(0), status_msg)
        return

    match = SUPPORTED_URL_RE.search(text)
    if not match:
        await update.message.reply_text(
            "این یه لینک معتبر نیست. لینک پست/ریلز/استوری اینستاگرام،"
            " ویدیو/شورتز یوتیوب یا ویدیوی تیک‌تاک رو بفرست، یا از دکمه‌ی"
            f" «{BTN_MUSIC}» برای جستجوی آهنگ استفاده کن."
        )
        return

    mark_request(_last_message_at, user_id)
    url = match.group(0)
    status_msg = await update.message.reply_text("⏳ دارم اطلاعات لینک رو می‌گیرم...")
    await show_quality_picker(
        update.message, chat_id, url, status_msg, user_msg_id=update.message.message_id
    )


# ------------------------------------------------------------------
# ضد اسپم: هر کاربر هر REQUEST_COOLDOWN_SECONDS ثانیه فقط یه درخواست جدید بده
# (دو شمارنده‌ی جدا: «پیام درخواست» مثل لینک/جستجو/فایل، و «شروع کار» مثل
# زدن دکمه‌ی دانلود؛ پس لینک فرستادن و بعدش زدن دکمه‌ی دانلود به هم گیر نمی‌دن)
# ------------------------------------------------------------------
REQUEST_COOLDOWN_SECONDS = int(os.environ.get("REQUEST_COOLDOWN_SECONDS", "20"))
_last_message_at = {}
_last_job_at = {}
_last_warn_at = {}


def _prune_cooldowns(store: dict):
    if len(store) > 5000:
        cutoff = time.monotonic() - REQUEST_COOLDOWN_SECONDS * 2
        for k in [k for k, v in store.items() if v < cutoff]:
            store.pop(k, None)


def cooldown_remaining(store: dict, user_id, record: bool = True) -> int:
    """0 یعنی مجازه (و اگه record=True ثبت می‌شه)؛ وگرنه چند ثانیه‌ی باقی‌مونده."""
    if REQUEST_COOLDOWN_SECONDS <= 0:
        return 0
    now = time.monotonic()
    last = store.get(user_id)
    if last is not None:
        remaining = REQUEST_COOLDOWN_SECONDS - (now - last)
        if remaining > 0:
            return math.ceil(remaining)
    if record:
        store[user_id] = now
        _prune_cooldowns(store)
    return 0


def mark_request(store: dict, user_id):
    store[user_id] = time.monotonic()


async def warn_slow_down(message, user_id, remaining: int):
    """پیام «صبر کن» (حداکثر هر ۵ ثانیه یه بار، که خودش اسپم نشه)."""
    now = time.monotonic()
    last = _last_warn_at.get(user_id)
    if last is not None and now - last < 5:
        return
    _last_warn_at[user_id] = now
    await message.reply_text(
        f"⏳ برای جلوگیری از اسپم، هر {REQUEST_COOLDOWN_SECONDS} ثانیه فقط یه درخواست"
        f" می‌تونی بدی.\nلطفاً {remaining} ثانیه دیگه دوباره بفرست."
    )


# ------------------------------------------------------------------
# دیتابیس کاربران، سهمیه‌ی روزانه، اشتراک VIP و پنل ادمین
# ------------------------------------------------------------------
# ⚠️ روی Railway باید یه Volume رو روی مسیر /app/data وصل کنی، وگرنه با هر
# دیپلوی دیتابیس پاک می‌شه (دیسک کانتینر موقتیه).
DB_PATH = os.environ.get("DB_PATH", "/app/data/bot.db")
# اگه ست باشه (مثلاً از Postgres خود Railway)، به‌جای فایل SQLite از PostgreSQL استفاده می‌شه
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# اشتراک خودکار: هر کاربر بعد از AUTO_VIP_AFTER_DAYS روز عضویت، هر AUTO_VIP_DAYS روز یه اشتراک AUTO_VIP_DAYS روزه می‌گیره
AUTO_VIP_AFTER_DAYS = int(os.environ.get("AUTO_VIP_AFTER_DAYS", "10"))  # 0 = خاموش
AUTO_VIP_DAYS = int(os.environ.get("AUTO_VIP_DAYS", "30"))
AUTO_VIP_CHECK_SECONDS = 600
# ارزش اشتراک روی فیش: هر VIP_PRICE_PERIOD_DAYS روز = VIP_PRICE_USD دلار (قیمت هر روز خودکار محاسبه می‌شه)
VIP_PRICE_USD = float(os.environ.get("VIP_PRICE_USD", "5"))
VIP_PRICE_PERIOD_DAYS = int(os.environ.get("VIP_PRICE_PERIOD_DAYS", "30"))
DAILY_QUOTA_MB = int(os.environ.get("DAILY_QUOTA_MB", "1536"))  # ۱.۵ گیگ (۱ گیگ و ۵۰۰ مگ)
TZ_OFFSET_MINUTES = int(os.environ.get("TZ_OFFSET_MINUTES", "210"))  # ایران = UTC+3:30
MB = 1024 * 1024

_admin_raw = os.environ.get("ADMIN_IDS") or os.environ.get("ADMIN_ID") or ""
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", _admin_raw.strip()) if x.isdigit()}


def utcnow() -> datetime:
    """زمان فعلی UTC (بدون tzinfo)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def from_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")


def to_local(dt: datetime) -> datetime:
    return dt + timedelta(minutes=TZ_OFFSET_MINUTES)


def today_key() -> str:
    """کلید «امروز» به وقت محلی؛ با عوض شدن روز، سهمیه خودکار ریست می‌شه."""
    return to_local(utcnow()).strftime("%Y-%m-%d")


def gregorian_to_jalali(gy: int, gm: int, gd: int):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) - 80 + gd + g_d_m[gm - 1]
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def fmt_date(dt: datetime, with_time: bool = False) -> str:
    """تاریخ شمسی (به وقت محلی)، مثلاً 1405/08/14"""
    local = to_local(dt)
    jy, jm, jd = gregorian_to_jalali(local.year, local.month, local.day)
    text = f"{jy}/{jm:02d}/{jd:02d}"
    if with_time:
        text += f" {local.hour:02d}:{local.minute:02d}"
    return text


def vip_value_usd(days: int) -> float:
    """ارزش اشتراک به دلار: روزی VIP_PRICE_USD / VIP_PRICE_PERIOD_DAYS (مثلاً ۵ دلار / ۳۰ روز)."""
    return days * VIP_PRICE_USD / VIP_PRICE_PERIOD_DAYS


def fmt_usd(amount: float) -> str:
    return f"${amount:,.2f}"


SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    first_name TEXT, last_name TEXT, username TEXT,
    joined_at TEXT NOT NULL,
    downloads_total INTEGER NOT NULL DEFAULT 0,
    vip_until TEXT,
    auto_vip_granted INTEGER NOT NULL DEFAULT 0,
    auto_vip_blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage_daily (
    user_id INTEGER NOT NULL, day TEXT NOT NULL,
    bytes_used INTEGER NOT NULL DEFAULT 0,
    downloads INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS vip_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, days INTEGER NOT NULL, granted_by INTEGER,
    granted_at TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'admin'
)
"""

# آیدی‌های تلگرام از ۳۲ بیت بزرگ‌ترن؛ تو Postgres حتماً BIGINT
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    first_name TEXT, last_name TEXT, username TEXT,
    joined_at TEXT NOT NULL,
    downloads_total INTEGER NOT NULL DEFAULT 0,
    vip_until TEXT,
    auto_vip_granted INTEGER NOT NULL DEFAULT 0,
    auto_vip_blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage_daily (
    user_id BIGINT NOT NULL, day TEXT NOT NULL,
    bytes_used BIGINT NOT NULL DEFAULT 0,
    downloads INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS vip_log (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL, days INTEGER NOT NULL, granted_by BIGINT,
    granted_at TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'admin'
)
"""


class _Tx:
    """یه تراکنش؛ ? رو برای Postgres خودکار به %s تبدیل می‌کنه."""

    def __init__(self, db, cur):
        self.db = db
        self.cur = cur

    def exec(self, sql: str, params=()):
        if self.db.is_pg:
            sql = sql.replace("?", "%s")
        self.cur.execute(sql, params)
        return self.cur


class Database:
    """
    دیتابیس کاربران. اگه url (DATABASE_URL) داده بشه از PostgreSQL (مثلاً
    Postgres خود Railway) استفاده می‌کنه، وگرنه از یه فایل SQLite.
    """

    def __init__(self, path: str = None, url: str = None):
        self.url = url or None
        self.is_pg = bool(self.url)
        self.path = path
        self.lock = threading.Lock()
        self.conn = None
        if self.is_pg:
            import psycopg2
            import psycopg2.extras

            self._pg = psycopg2
            self._extras = psycopg2.extras
            self.conn_errors = (psycopg2.OperationalError, psycopg2.InterfaceError)
        else:
            self.conn_errors = ()
            folder = os.path.dirname(path or "")
            if folder:
                os.makedirs(folder, exist_ok=True)
        self._connect()
        self._init_schema()

    @property
    def backend(self) -> str:
        return "PostgreSQL" if self.is_pg else f"SQLite ({self.path})"

    def _connect(self):
        if self.is_pg:
            self.conn = self._pg.connect(self.url, connect_timeout=10)
            self.conn.autocommit = False
        else:
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")

    def _cursor(self):
        if self.is_pg:
            return self.conn.cursor(cursor_factory=self._extras.RealDictCursor)
        return self.conn.cursor()

    def run(self, fn):
        """fn(tx) رو تو یه تراکنش اجرا می‌کنه؛ اگه اتصال Postgres قطع شده بود، یه بار دوباره وصل می‌شه."""
        last = None
        for _ in range(2):
            with self.lock:
                try:
                    if self.conn is None or (self.is_pg and self.conn.closed):
                        self._connect()
                    cur = self._cursor()
                    try:
                        result = fn(_Tx(self, cur))
                        self.conn.commit()
                        return result
                    except Exception:
                        try:
                            self.conn.rollback()
                        except Exception:
                            pass
                        raise
                    finally:
                        try:
                            cur.close()
                        except Exception:
                            pass
                except self.conn_errors as e:
                    last = e
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                    self.conn = None
        raise last

    def _init_schema(self):
        def fn(t):
            for stmt in (SCHEMA_PG if self.is_pg else SCHEMA_SQLITE).split(";"):
                if stmt.strip():
                    t.exec(stmt)
            # مهاجرت برای دیتابیس‌هایی که با نسخه‌ی قدیمی‌تر ساخته شدن
            if self.is_pg:
                t.exec("ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_vip_granted INTEGER NOT NULL DEFAULT 0")
                t.exec("ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_vip_blocked INTEGER NOT NULL DEFAULT 0")
                t.exec("ALTER TABLE vip_log ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'admin'")
            else:
                cols = {r["name"] for r in t.exec("PRAGMA table_info(users)").fetchall()}
                if "auto_vip_granted" not in cols:
                    t.exec("ALTER TABLE users ADD COLUMN auto_vip_granted INTEGER NOT NULL DEFAULT 0")
                if "auto_vip_blocked" not in cols:
                    t.exec("ALTER TABLE users ADD COLUMN auto_vip_blocked INTEGER NOT NULL DEFAULT 0")
                cols = {r["name"] for r in t.exec("PRAGMA table_info(vip_log)").fetchall()}
                if "source" not in cols:
                    t.exec("ALTER TABLE vip_log ADD COLUMN source TEXT NOT NULL DEFAULT 'admin'")

        self.run(fn)

    def upsert_user(self, user_id: int, first_name, last_name, username):
        """کاربر رو ثبت (اگه جدیده، آیدی ۸ رقمی اختصاصی می‌گیره) یا اسمش رو به‌روز می‌کنه."""

        def fn(t):
            row = t.exec("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                for _ in range(30):
                    public_id = str(secrets.randbelow(90_000_000) + 10_000_000)
                    if t.exec("SELECT 1 FROM users WHERE public_id=?", (public_id,)).fetchone():
                        continue
                    t.exec(
                        "INSERT INTO users(user_id, public_id, first_name, last_name, username, joined_at)"
                        " VALUES (?,?,?,?,?,?) ON CONFLICT (user_id) DO NOTHING",
                        (user_id, public_id, first_name, last_name, username, to_iso(utcnow())),
                    )
                    break
                else:
                    raise RuntimeError("could not allocate a unique public id")
            elif (row["first_name"], row["last_name"], row["username"]) != (first_name, last_name, username):
                t.exec(
                    "UPDATE users SET first_name=?, last_name=?, username=? WHERE user_id=?",
                    (first_name, last_name, username, user_id),
                )
            return t.exec("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

        return self.run(fn)

    def get_user(self, user_id: int):
        return self.run(lambda t: t.exec("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())

    def get_user_by_public_id(self, public_id: str):
        return self.run(lambda t: t.exec("SELECT * FROM users WHERE public_id=?", (public_id,)).fetchone())

    def usage_today(self, user_id: int):
        """(بایت مصرف‌شده‌ی امروز، تعداد دانلود امروز)"""
        row = self.run(
            lambda t: t.exec(
                "SELECT bytes_used, downloads FROM usage_daily WHERE user_id=? AND day=?",
                (user_id, today_key()),
            ).fetchone()
        )
        return (row["bytes_used"], row["downloads"]) if row else (0, 0)

    def add_bytes(self, user_id: int, nbytes: int):
        self.run(
            lambda t: t.exec(
                "INSERT INTO usage_daily(user_id, day, bytes_used) VALUES (?,?,?)"
                " ON CONFLICT (user_id, day) DO UPDATE SET bytes_used = usage_daily.bytes_used + excluded.bytes_used",
                (user_id, today_key(), int(nbytes)),
            )
        )

    def add_download(self, user_id: int):
        def fn(t):
            t.exec("UPDATE users SET downloads_total = downloads_total + 1 WHERE user_id=?", (user_id,))
            t.exec(
                "INSERT INTO usage_daily(user_id, day, downloads) VALUES (?,?,1)"
                " ON CONFLICT (user_id, day) DO UPDATE SET downloads = usage_daily.downloads + 1",
                (user_id, today_key()),
            )

        self.run(fn)

    def vip_until(self, user_id: int):
        """تاریخ پایان VIP اگه هنوز فعاله، وگرنه None."""
        row = self.run(lambda t: t.exec("SELECT vip_until FROM users WHERE user_id=?", (user_id,)).fetchone())
        if row and row["vip_until"]:
            end = from_iso(row["vip_until"])
            if end > utcnow():
                return end
        return None

    def grant_vip(self, user_id: int, days: int, granted_by: int, auto: bool = False) -> dict:
        """
        اگه VIP فعال داشته، از پایانش تمدید می‌شه؛ وگرنه از همین الان شروع می‌شه.
        تمدید خودکار اگه تازه (حداکثر ۱ روز) تموم شده باشه، از همون لحظه‌ی پایان ادامه پیدا می‌کنه
        تا دوره‌های ماهانه فاصله نیفتن. فعال‌سازی دستی مدیر، لغوِ قبلی (توقف تمدید خودکار) رو برمی‌داره.
        """

        def fn(t):
            now = utcnow()
            row = t.exec("SELECT vip_until FROM users WHERE user_id=?", (user_id,)).fetchone()
            current = None
            end_old = None
            if row and row["vip_until"]:
                end_old = from_iso(row["vip_until"])
                if end_old > now:
                    current = end_old
            if current:
                start = current
            elif auto and end_old and now - end_old <= timedelta(days=1):
                start = end_old
            else:
                start = now
            end = start + timedelta(days=days)
            receipt_id = t.exec(
                "INSERT INTO vip_log(user_id, days, granted_by, granted_at, start_at, end_at, source)"
                " VALUES (?,?,?,?,?,?,?) RETURNING id",
                (user_id, days, granted_by, to_iso(now), to_iso(start), to_iso(end), "auto" if auto else "admin"),
            ).fetchone()["id"]
            t.exec(
                "UPDATE users SET vip_until=?" + (", auto_vip_granted=1" if auto else ", auto_vip_blocked=0") + " WHERE user_id=?",
                (to_iso(end), user_id),
            )
            return {
                "receipt_id": receipt_id, "days": days, "start": start, "end": end,
                "extended": current is not None, "auto": auto,
            }

        return self.run(fn)

    def cancel_vip(self, user_id: int) -> dict:
        """اشتراک فعال رو همین الان تموم می‌کنه و تمدید خودکار رو برای این کاربر متوقف می‌کنه."""

        def fn(t):
            now = utcnow()
            row = t.exec("SELECT vip_until FROM users WHERE user_id=?", (user_id,)).fetchone()
            old_end = None
            if row and row["vip_until"]:
                end = from_iso(row["vip_until"])
                if end > now:
                    old_end = end
            if old_end:
                t.exec("UPDATE users SET vip_until=?, auto_vip_blocked=1 WHERE user_id=?", (to_iso(now), user_id))
            else:
                t.exec("UPDATE users SET auto_vip_blocked=1 WHERE user_id=?", (user_id,))
            return {"was_active": old_end is not None, "old_end": old_end}

        return self.run(fn)

    def users_due_for_auto_vip(self, cutoff_iso: str, now_iso: str, exclude_ids=(), limit: int = 200):
        """کاربرهایی که قبل از cutoff عضو شدن، الان اشتراک فعال ندارن و تمدید خودکارشون لغو نشده."""

        def fn(t):
            sql = (
                "SELECT * FROM users WHERE joined_at <= ? AND auto_vip_blocked = 0"
                " AND (vip_until IS NULL OR vip_until <= ?)"
            )
            params = [cutoff_iso, now_iso]
            ids = [int(i) for i in exclude_ids]
            if ids:
                sql += " AND user_id NOT IN (" + ",".join("?" * len(ids)) + ")"
                params += ids
            sql += " ORDER BY joined_at LIMIT ?"
            params.append(limit)
            return t.exec(sql, tuple(params)).fetchall()

        return self.run(fn)

    def total_bytes(self, user_id: int) -> int:
        """مجموع حجم دانلودِ کاربر از ابتدا."""
        row = self.run(
            lambda t: t.exec(
                "SELECT COALESCE(SUM(bytes_used), 0) AS s FROM usage_daily WHERE user_id=?", (user_id,)
            ).fetchone()
        )
        return int(row["s"]) if row else 0


_db = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(DB_PATH, DATABASE_URL or None)
    return _db


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def user_is_vip(user_id: int) -> bool:
    return is_admin(user_id) or get_db().vip_until(user_id) is not None


def quota_remaining_bytes(user_id: int):
    """بایت باقی‌مونده‌ی امروز؛ None یعنی نامحدود (VIP/مدیر). اگه دیتابیس خراب بود، ربات رو نمی‌خوابونیم."""
    try:
        if user_is_vip(user_id):
            return None
        used, _ = get_db().usage_today(user_id)
        return max(0, DAILY_QUOTA_MB * MB - used)
    except Exception:
        logger.exception("quota check failed; allowing")
        return None


def format_volume(nbytes: int) -> str:
    mb = nbytes / MB
    if mb >= 1024:
        return f"{mb / 1024:.2f}".rstrip("0").rstrip(".") + "GB"
    return f"{mb:.0f}MB"


def quota_problem(user_id: int, size_bytes: int):
    """اگه حجم فایل از سهمیه‌ی باقی‌مونده بیشتره، متن توضیح؛ وگرنه None."""
    remaining = quota_remaining_bytes(user_id)
    if remaining is None or size_bytes <= remaining:
        return None
    return (
        "📦 سهمیه‌ی امروزت برای این فایل کافی نیست.\n"
        f"حجم فایل: {format_volume(size_bytes)}\n"
        f"باقی‌مونده‌ی امروز: {format_volume(remaining)} از {format_volume(DAILY_QUOTA_MB * MB)}\n\n"
        "سهمیه هر شب نیمه‌شب (به وقت ایران) ریست می‌شه. می‌تونی کیفیت پایین‌تری"
        " انتخاب کنی، یا با اشتراک VIP بدون محدودیت دانلود کنی (با نوشتن «آیدی»"
        " آیدی ۸ رقمی‌ت رو بگیر و به مدیر بده)."
    )


def record_sent(user_id: int, nbytes: int):
    try:
        get_db().add_bytes(user_id, nbytes)
    except Exception:
        logger.exception("record_sent failed")


def record_download(user_id: int):
    try:
        get_db().add_download(user_id)
    except Exception:
        logger.exception("record_download failed")


def to_ascii_digits(text: str) -> str:
    return text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def display_name(row) -> str:
    return " ".join(p for p in (row["first_name"], row["last_name"]) if p) or "—"


def username_text(row) -> str:
    return f"@{row['username']}" if row["username"] else "—"


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """هر کاربری که با ربات کار می‌کنه خودکار ثبت‌نام می‌شه (و اسمش به‌روز می‌مونه)."""
    user = update.effective_user
    if not user or user.is_bot:
        return
    try:
        await asyncio.to_thread(get_db().upsert_user, user.id, user.first_name, user.last_name, user.username)
    except Exception:
        logger.exception("track_user failed")


async def account_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_ = get_db()
    row = await asyncio.to_thread(db_.upsert_user, user.id, user.first_name, user.last_name, user.username)
    used, downloads_today = db_.usage_today(user.id)
    vip_end = db_.vip_until(user.id)

    lines = [
        "👤 <b>حساب من</b>",
        "━━━━━━━━━━━━━━",
        f"🏷 نام: {html.escape(display_name(row))}",
        f"🔖 آیدی حروفی: {html.escape(username_text(row))}",
        f"🔢 آیدی تلگرام: <code>{user.id}</code>",
        f"🆔 آیدی اختصاصی: <code>{row['public_id']}</code>",
        f"📅 تاریخ عضویت: {fmt_date(from_iso(row['joined_at']))}",
        f"⬇️ تعداد دانلود: {row['downloads_total']}",
        "━━━━━━━━━━━━━━",
    ]
    if is_admin(user.id):
        lines.append("👑 وضعیت: مدیر (بدون محدودیت)")
    elif vip_end:
        days_left = max(1, math.ceil((vip_end - utcnow()).total_seconds() / 86400))
        lines.append(f"💎 اشتراک: VIP فعال تا {fmt_date(vip_end)} ({days_left} روز مونده)")
    else:
        lines.append("💎 اشتراک: ندارد")

    if user_is_vip(user.id):
        lines.append(f"📦 مصرف امروز: {format_volume(used)} (نامحدود ♾)")
    else:
        total = DAILY_QUOTA_MB * MB
        lines.append(
            f"📦 مصرف امروز: {format_volume(used)} از {format_volume(total)}"
            f" (باقی‌مونده {format_volume(max(0, total - used))})"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کاربر «آیدی» می‌نویسه -> آیدی ۸ رقمی اختصاصیش رو می‌گیره."""
    user = update.effective_user
    row = await asyncio.to_thread(get_db().upsert_user, user.id, user.first_name, user.last_name, user.username)
    await update.message.reply_text(
        f"🆔 آیدی اختصاصی شما: <code>{row['public_id']}</code>\n"
        f"🔢 آیدی تلگرام: <code>{user.id}</code>\n\n"
        "آیدی اختصاصی هویت حساب شماست؛ برای فعال‌سازی اشتراک VIP همین عدد ۸ رقمی"
        " رو به مدیر بده.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )


# ------------------------- پنل ادمین -------------------------
ADMIN_STATE = {}  # admin_id -> {"step": ..., "target_user_id": ..., "public_id": ...}


class AdminOnly(filters.MessageFilter):
    """فقط پیام‌های مدیر رد می‌شن؛ برای بقیه، پیام عادی پردازش می‌شه."""

    def filter(self, message) -> bool:
        return bool(message.from_user and is_admin(message.from_user.id))


ADMIN_CANCEL_MARKUP = InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="adm|cancel")]])


def admin_panel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 VIP کردن کاربر", callback_data="adm|vip")],
        [InlineKeyboardButton("🚫 لغو اشتراک VIP", callback_data="adm|cxl")],
        [InlineKeyboardButton("👤 مشخصات کاربر", callback_data="adm|info")],
        [InlineKeyboardButton("❌ بستن پنل", callback_data="adm|close")],
    ])


def admin_days_markup() -> InlineKeyboardMarkup:
    def b(days):
        return InlineKeyboardButton(
            f"{days} روز · {fmt_usd(vip_value_usd(days))}", callback_data=f"admd|{days}"
        )

    return InlineKeyboardMarkup([
        [b(7), b(15), b(30)],
        [b(60), b(90), b(180)],
        [InlineKeyboardButton("✏️ عدد دلخواه", callback_data="admd|custom")],
        [InlineKeyboardButton("❌ لغو", callback_data="adm|cancel")],
    ])


async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیر «ادمین» می‌نویسه (یا /admin) -> پنل با دکمه‌های شیشه‌ای باز می‌شه."""
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    ADMIN_STATE.pop(user.id, None)
    await update.message.reply_text("👑 پنل مدیریت\nیکی از گزینه‌ها رو انتخاب کن:", reply_markup=admin_panel_markup())


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("دسترسی نداری.", show_alert=True)
        return
    await query.answer()
    action = query.data.split("|", 1)[1]
    admin_id = query.from_user.id

    if action == "vip":
        ADMIN_STATE[admin_id] = {"step": "await_id", "action": "vip"}
        await query.edit_message_text(
            "💎 VIP کردن کاربر\n\nآیدی ۸ رقمی کاربر رو بفرست.\n"
            "(کاربر با نوشتن «آیدی» تو ربات اون رو می‌گیره)",
            reply_markup=ADMIN_CANCEL_MARKUP,
        )
    elif action == "cxl":
        ADMIN_STATE[admin_id] = {"step": "await_id", "action": "cxl"}
        await query.edit_message_text(
            "🚫 لغو اشتراک VIP\n\nآیدی ۸ رقمی کاربر رو بفرست.",
            reply_markup=ADMIN_CANCEL_MARKUP,
        )
    elif action == "info":
        ADMIN_STATE[admin_id] = {"step": "await_id", "action": "info"}
        await query.edit_message_text(
            "👤 مشخصات کاربر\n\nآیدی ۸ رقمی کاربر رو بفرست.",
            reply_markup=ADMIN_CANCEL_MARKUP,
        )
    elif action == "cxl_yes":
        await admin_confirm_cancel(query, context, admin_id)
    elif action == "cancel":
        ADMIN_STATE.pop(admin_id, None)
        await query.edit_message_text("👑 پنل مدیریت\nیکی از گزینه‌ها رو انتخاب کن:", reply_markup=admin_panel_markup())
    elif action == "close":
        ADMIN_STATE.pop(admin_id, None)
        await query.edit_message_text("پنل بسته شد.")


def build_receipt(row, info: dict) -> str:
    days = info["days"]
    lines = [
        "🧾 <b>فیش فعال‌سازی اشتراک VIP</b>",
        "━━━━━━━━━━━━━━",
        f"شماره فیش: <code>#{info['receipt_id']:05d}</code>",
        f"👤 کاربر: {html.escape(display_name(row))} {html.escape(username_text(row))}",
        f"🆔 آیدی: <code>{row['public_id']}</code>",
        f"⏳ مدت: {days} روز" + (" (تمدید)" if info["extended"] else ""),
        (
            f"🎁 نوع: اشتراک ماهانه‌ی خودکار (بعد از {AUTO_VIP_AFTER_DAYS} روز عضویت)"
            if info.get("auto")
            else "👑 نوع: فعال‌سازی توسط مدیر"
        ),
        f"📅 شروع: {fmt_date(info['start'], with_time=True)}",
        f"📅 پایان: {fmt_date(info['end'], with_time=True)}",
        "━━━━━━━━━━━━━━",
        f"💵 ارزش اشتراک: <b>{fmt_usd(vip_value_usd(days))}</b>"
        f" (هر روز {fmt_usd(vip_value_usd(1))})",
        "✅ اشتراک فعال شد.",
    ]
    return "\n".join(lines)


def build_user_info(row) -> str:
    """مشخصات کاربر برای پنل مدیر؛ مدت عضویت به‌صورت عدد (روز) نشون داده می‌شه."""
    db_ = get_db()
    uid = row["user_id"]
    used, _ = db_.usage_today(uid)
    total_bytes = db_.total_bytes(uid)
    vip_end = db_.vip_until(uid)
    member_days = max(0, (utcnow() - from_iso(row["joined_at"])).days)

    lines = [
        "👤 <b>مشخصات کاربر</b>",
        "━━━━━━━━━━━━━━",
        f"🏷 نام: {html.escape(display_name(row))} {html.escape(username_text(row))}",
        f"🆔 آیدی اختصاصی: <code>{row['public_id']}</code>",
        f"📅 مدت عضویت: {member_days} روز",
        "━━━━━━━━━━━━━━",
        f"⬇️ دانلود امروز: {format_volume(used)}",
    ]
    if is_admin(uid) or vip_end:
        lines.append("📦 حجم باقی‌مونده‌ی امروز: نامحدود ♾")
    else:
        total = DAILY_QUOTA_MB * MB
        lines.append(f"📦 حجم باقی‌مونده‌ی امروز: {format_volume(max(0, total - used))} از {format_volume(total)}")
    lines.append(f"📊 مجموع حجم دانلود: {format_volume(total_bytes)} ({row['downloads_total']} دانلود)")
    if is_admin(uid):
        lines.append("👑 وضعیت: مدیر (بدون محدودیت)")
    elif vip_end:
        days_left = max(1, math.ceil((vip_end - utcnow()).total_seconds() / 86400))
        lines.append(f"💎 اشتراک: VIP فعال ({days_left} روز مونده)")
    else:
        lines.append("💎 اشتراک: ندارد")
    if row["auto_vip_blocked"]:
        lines.append("🔁 تمدید خودکار: متوقف (لغو شده توسط مدیر)")
    return "\n".join(lines)


async def admin_confirm_cancel(query, context: ContextTypes.DEFAULT_TYPE, admin_id: int):
    """بعد از تأیید مدیر: اشتراک رو لغو و تمدید خودکار رو متوقف می‌کنه."""
    state = ADMIN_STATE.get(admin_id)
    if not state or state.get("step") != "confirm_cancel":
        await query.edit_message_text("⌛ این مرحله منقضی شده؛ دوباره «ادمین» رو بنویس.")
        return
    target = state["target_user_id"]
    ADMIN_STATE.pop(admin_id, None)
    db_ = get_db()
    result = await asyncio.to_thread(db_.cancel_vip, target)
    row = db_.get_user(target)

    delivered = True
    try:
        await context.bot.send_message(
            chat_id=target,
            text=(
                "ℹ️ اشتراک VIP شما توسط مدیر لغو شد.\n"
                f"از این به بعد سهمیه‌ی روزانه‌ی عادی ({format_volume(DAILY_QUOTA_MB * MB)}) برای شما اعمال می‌شه."
            ),
        )
    except Exception:
        delivered = False
        logger.info("could not notify the user about VIP cancellation")

    head = "✅ اشتراک VIP لغو شد." if result["was_active"] else "✅ این کاربر اشتراک فعالی نداشت؛ تمدید خودکارش متوقف شد."
    note = "📨 به کاربر هم اطلاع داده شد." if delivered else "⚠️ نتونستم به کاربر پیام بدم."
    await query.edit_message_text(
        f"{head}\n👤 {display_name(row)} {username_text(row)}\n🆔 {row['public_id']}\n"
        "🔁 تمدید خودکار برای این کاربر متوقف شد (با «VIP کردن کاربر» دوباره فعال می‌شه).\n\n" + note,
        reply_markup=admin_panel_markup(),
    )


async def admin_grant_vip(message, context: ContextTypes.DEFAULT_TYPE, admin_id: int, target_user_id: int, days: int):
    """VIP رو فعال می‌کنه، فیش رو برای مدیر و کاربر می‌فرسته."""
    db_ = get_db()
    info = await asyncio.to_thread(db_.grant_vip, target_user_id, days, admin_id)
    row = db_.get_user(target_user_id)
    receipt = build_receipt(row, info)
    ADMIN_STATE.pop(admin_id, None)

    delivered = True
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                "🎉 اشتراک VIP شما فعال شد!\n\n" + receipt +
                f"\n\nاز این به بعد تا {fmt_date(info['end'])} می‌تونی بدون محدودیت حجم دانلود کنی 💎"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        delivered = False
        logger.exception("could not notify the VIP user")

    note = (
        "📨 همین فیش برای کاربر هم ارسال شد."
        if delivered
        else "⚠️ نتونستم به کاربر پیام بدم (احتمالاً ربات رو استارت نکرده یا بلاک کرده)."
    )
    await message.reply_text(receipt + "\n\n" + note, parse_mode=ParseMode.HTML, reply_markup=admin_panel_markup())


async def run_auto_vip_check(bot_) -> int:
    """
    هر کاربری که AUTO_VIP_AFTER_DAYS روز از عضویتش گذشته، الان اشتراک فعال نداره و تمدید خودکارش
    لغو نشده، یه اشتراک AUTO_VIP_DAYS روزه می‌گیره و فیش (با ارزش دلاری) براش ارسال می‌شه.
    وقتی اشتراکش تموم بشه، دوباره همین چرخه تکرار می‌شه (یعنی هر ۳۰ روز یه اشتراک ۳۰ روزه).
    """
    if AUTO_VIP_AFTER_DAYS <= 0 or AUTO_VIP_DAYS <= 0:
        return 0
    db_ = get_db()
    granted = 0
    skip = set(ADMIN_IDS)  # مدیر که از قبل نامحدوده؛ کاربرهایی که گرنت‌شون خطا داد هم تو این دور رد می‌شن
    for _ in range(20):  # دسته‌ای (هر دسته ۲۰۰ نفر)
        now = utcnow()
        rows = await asyncio.to_thread(
            db_.users_due_for_auto_vip, to_iso(now - timedelta(days=AUTO_VIP_AFTER_DAYS)), to_iso(now), skip
        )
        if not rows:
            break
        for row in rows:
            uid = row["user_id"]
            try:
                info = await asyncio.to_thread(db_.grant_vip, uid, AUTO_VIP_DAYS, 0, True)
            except Exception:
                logger.exception("auto VIP grant failed for %s", uid)
                skip.add(uid)
                continue
            granted += 1
            try:
                fresh = db_.get_user(uid)
                await bot_.send_message(
                    chat_id=uid,
                    text=(
                        "🎁 اشتراک ماهانه‌ات فعال شد!\n"
                        f"چون بیشتر از {AUTO_VIP_AFTER_DAYS} روزه که با ما هستی، هر {AUTO_VIP_DAYS} روز یه اشتراک"
                        f" {AUTO_VIP_DAYS} روزه‌ی VIP خودکار برات فعال می‌شه.\n\n"
                        + build_receipt(fresh, info)
                        + f"\n\nتا {fmt_date(info['end'])} می‌تونی بدون محدودیت حجم دانلود کنی 💎"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.info("could not send the auto-VIP receipt to %s", uid)
            await asyncio.sleep(0.05)
        if len(rows) < 200:
            break
    return granted


async def auto_vip_loop(application):
    await asyncio.sleep(20)
    while True:
        try:
            n = await run_auto_vip_check(application.bot)
            if n:
                logger.info("auto VIP granted to %s users", n)
        except Exception:
            logger.exception("auto VIP check failed")
        await asyncio.sleep(AUTO_VIP_CHECK_SECONDS)


async def post_init(application):
    """بعد از بالا اومدن ربات اجرا می‌شه: چک دوره‌ای VIP خودکار رو راه می‌اندازه."""
    application.bot_data["auto_vip_task"] = asyncio.create_task(auto_vip_loop(application))


async def admin_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """متنی که مدیر وسط مراحل پنل می‌فرسته (آیدی کاربر، تعداد روز دلخواه)."""
    admin_id = update.effective_user.id
    state = ADMIN_STATE.get(admin_id)
    if not state:
        return
    value = to_ascii_digits(text.strip())

    if value in ("لغو", "/cancel"):
        ADMIN_STATE.pop(admin_id, None)
        await update.message.reply_text("لغو شد.", reply_markup=admin_panel_markup())
        return

    if state["step"] == "await_id":
        if not re.fullmatch(r"\d{8}", value):
            await update.message.reply_text("❌ آیدی باید یه عدد ۸ رقمی باشه. دوباره بفرست (یا «لغو» بنویس).")
            return
        row = get_db().get_user_by_public_id(value)
        if not row:
            await update.message.reply_text("❌ کاربری با این آیدی پیدا نشد. آیدی رو چک کن و دوباره بفرست (یا «لغو»).")
            return
        action = state.get("action", "vip")

        if action == "info":
            ADMIN_STATE.pop(admin_id, None)
            info_text = await asyncio.to_thread(build_user_info, row)
            await update.message.reply_text(info_text, parse_mode=ParseMode.HTML, reply_markup=admin_panel_markup())
            return

        if action == "cxl":
            if is_admin(row["user_id"]):
                await update.message.reply_text(
                    "❌ این کاربر مدیره و اشتراکی نداره (دسترسی نامحدودش از ADMIN_IDS میاد). آیدی دیگه‌ای بفرست (یا «لغو»)."
                )
                return
            vip_end = get_db().vip_until(row["user_id"])
            if vip_end:
                days_left = max(1, math.ceil((vip_end - utcnow()).total_seconds() / 86400))
                status = f"💎 VIP فعال تا {fmt_date(vip_end)} ({days_left} روز مونده)"
            else:
                status = "💎 اشتراک فعالی نداره"
            state.update(step="confirm_cancel", target_user_id=row["user_id"], public_id=value)
            await update.message.reply_text(
                f"🚫 لغو اشتراک VIP\n\n👤 {display_name(row)} {username_text(row)}\n🆔 {value}\n{status}\n\n"
                "مطمئنی؟ اشتراک لغو و تمدید خودکار برای این کاربر متوقف می‌شه.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ بله، لغو کن", callback_data="adm|cxl_yes")],
                    [InlineKeyboardButton("❌ انصراف", callback_data="adm|cancel")],
                ]),
            )
            return

        state.update(step="choose_days", target_user_id=row["user_id"], public_id=value)
        vip_end = get_db().vip_until(row["user_id"])
        vip_line = f"\n💎 VIP فعلی تا {fmt_date(vip_end)} (روزهای جدید بهش اضافه می‌شه)" if vip_end else ""
        await update.message.reply_text(
            f"👤 {html.escape(display_name(row))} {html.escape(username_text(row))}\n"
            f"🆔 {value}{vip_line}\n\nچند روز VIP بشه؟",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_days_markup(),
        )
    elif state["step"] == "await_days":
        if not re.fullmatch(r"\d{1,4}", value) or not (1 <= int(value) <= 3650):
            await update.message.reply_text("❌ تعداد روز باید یه عدد بین ۱ تا ۳۶۵۰ باشه. دوباره بفرست (یا «لغو»).")
            return
        await admin_grant_vip(update.message, context, admin_id, state["target_user_id"], int(value))
    else:
        await update.message.reply_text("اول از دکمه‌های بالا یکی رو انتخاب کن (یا «لغو»).")


async def admin_days_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = query.from_user.id
    if not is_admin(admin_id):
        await query.answer("دسترسی نداری.", show_alert=True)
        return
    await query.answer()
    state = ADMIN_STATE.get(admin_id)
    if not state or state.get("step") != "choose_days":
        await query.edit_message_text("⌛ این مرحله منقضی شده؛ دوباره «ادمین» رو بنویس.")
        return

    choice = query.data.split("|", 1)[1]
    if choice == "custom":
        state["step"] = "await_days"
        await query.edit_message_text("✏️ تعداد روز VIP رو (بین ۱ تا ۳۶۵۰) بفرست — یا «لغو» بنویس.")
        return

    await query.edit_message_reply_markup(reply_markup=None)
    await admin_grant_vip(query.message, context, admin_id, state["target_user_id"], int(choice))


async def job_allowed(query) -> bool:
    """برای دکمه‌هایی که کار سنگین شروع می‌کنن (دانلود، تشخیص آهنگ، ...)."""
    quota_left = quota_remaining_bytes(query.from_user.id)
    if quota_left is not None and quota_left <= 0:
        await query.answer(
            "📦 سهمیه‌ی امروزت تموم شده. نیمه‌شب ریست می‌شه؛ یا با VIP نامحدود دانلود"
            " کن («آیدی» بنویس و به مدیر بده).",
            show_alert=True,
        )
        return False
    remaining = cooldown_remaining(_last_job_at, query.from_user.id)
    if remaining:
        await query.answer(
            f"⏳ برای جلوگیری از اسپم، {remaining} ثانیه دیگه صبر کن و دوباره بزن.",
            show_alert=True,
        )
        return False
    await query.answer()
    return True


# ------------------------------------------------------------------
# تمیزکاری بعد از ارسال + دکمه‌ی «لینک» زیر فایل
# ------------------------------------------------------------------
def normalize_link(url: str) -> str:
    return url if url.startswith(("http://", "https://")) else "https://" + url


def link_markup(url: str) -> InlineKeyboardMarkup:
    """یه دکمه‌ی شیشه‌ای «🔗 لینک» که با زدنش لینک اصلی باز می‌شه."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔗 لینک", url=normalize_link(url))]])


async def delete_quietly(message) -> bool:
    try:
        await message.delete()
        return True
    except Exception:
        return False


async def cleanup_after_send(context, query, user_msg_id=None):
    """
    بعد از ارسال موفق: پیام «در حال ارسال» پاک می‌شه، و اگه پیام لینکِ خود
    کاربر هم مشخصه، اون هم پاک می‌شه (لینک زیر فایل تو دکمه‌ی «🔗 لینک» می‌مونه).
    اگه پاک کردن ممکن نبود، فقط یه ✅ نشون می‌ده.
    """
    if not await delete_quietly(query.message):
        await set_status(query, "✅ ارسال شد!")
    if user_msg_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=user_msg_id)
        except Exception:
            logger.info("could not delete the user's link message (no permission?)")


async def send_media_file(bot, kind: str, chat_id: int, path: str, **kwargs):
    """
    فایل (video یا audio) رو به تلگرام می‌فرسته.
    اگه سرور محلی فعاله، فقط «مسیر فایل» رو می‌دیم (file://) تا خود سرور فایل رو
    از دیسک بخونه و آپلود کنه؛ این‌جوری کل فایل (که ممکنه چند صد مگ باشه)
    تو رمِ ربات لود نمی‌شه و از طریق HTTP هم رد و بدل نمی‌شه.
    اگه این روش با BadRequest شکست خورد، آپلود معمولی انجام می‌شه.
    """
    send = bot.send_video if kind == "video" else bot.send_audio
    size_mb = os.path.getsize(path) / 1024 / 1024
    started = time.monotonic()
    logger.info("sending %s (%.1fMB) to chat %s, local_mode=%s", kind, size_mb, chat_id, bool(LOCAL_API_URL))
    try:
        if LOCAL_API_URL:
            try:
                result = await send(chat_id=chat_id, **{kind: path}, **kwargs)
                logger.info("sent %s in %.0fs (by local path)", kind, time.monotonic() - started)
                return result
            except BadRequest:
                logger.exception("sending by local path failed; falling back to normal upload")
        with open(path, "rb") as f:
            result = await send(chat_id=chat_id, **{kind: f}, **kwargs)
        logger.info("sent %s in %.0fs (normal upload)", kind, time.monotonic() - started)
        return result
    except Exception:
        logger.exception("sending %s failed after %.0fs", kind, time.monotonic() - started)
        raise


async def with_heartbeat(query, text: str, coro, interval: int = 20):
    """
    coro (مثلاً ارسال فایل بزرگ) رو اجرا می‌کنه و تا وقتی تموم نشده، هر چند
    ثانیه پیام وضعیت رو با زمان سپری‌شده آپدیت می‌کنه. اگه ربات وسط کار
    بمیره/ری‌استارت بشه، شمارنده از حرکت می‌ایسته و کاربر می‌فهمه.
    """
    stop = asyncio.Event()
    started = time.monotonic()

    async def beat():
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                elapsed = int(time.monotonic() - started)
                await set_status(query, f"{text}\n⏱ {elapsed // 60}:{elapsed % 60:02d}")

    task = asyncio.create_task(beat())
    try:
        return await coro
    finally:
        stop.set()
        await task


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await job_allowed(query):
        return

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

    # پیش‌بررسی سهمیه با حجم تخمینی کیفیت انتخابی (قبل از دانلود)
    est = (entry.get("options", {}).get(f"{format_id}_{kind}") or {}).get("size")
    if est:
        problem = quota_problem(query.from_user.id, int(est))
        if problem:
            _last_job_at.pop(query.from_user.id, None)  # بدون صبر ۲۰ ثانیه‌ای بتونه کیفیت پایین‌تر بزنه
            await query.message.reply_text(problem)
            return

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

        file_bytes = os.path.getsize(file_path)
        problem = quota_problem(query.from_user.id, file_bytes)
        if problem:
            await set_status(query, problem)
            return

        sending_text = (
            f"📤 در حال ارسال فایل ({size_mb:.0f}MB)...\n"
            "فایل‌های بزرگ ممکنه چند دقیقه طول بکشه، صبور باش."
        )
        await set_status(query, sending_text)
        chat_id = query.message.chat_id
        markup = link_markup(entry.get("link") or url)
        if kind == "audio":
            await with_heartbeat(
                query, sending_text,
                send_media_file(context.bot, "audio", chat_id, file_path, reply_markup=markup),
            )
        else:
            await with_heartbeat(
                query, sending_text,
                send_media_file(
                    context.bot, "video", chat_id, file_path,
                    supports_streaming=True, reply_markup=markup,
                ),
            )

        record_sent(query.from_user.id, file_bytes)
        record_download(query.from_user.id)
        await cleanup_after_send(context, query, entry.get("user_msg_id"))
    except Exception as e:
        logger.exception("download/send failed")
        stop_event.set()
        await set_status(query, "❌ دانلود یا ارسال فایل با خطا مواجه شد:\n" + str(e)[:300])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        PENDING.pop(key, None)


async def deliver_song(query, context, chat_id: int, url: str, title: str, artist: str = None, user_msg_id=None):
    """
    برای فیچر «پیدا کردن آهنگ اصلی»: خودکار هم ویدیو هم نسخه‌ی صوتی رو
    دانلود و می‌فرسته، و نسخه‌ی صوتی رو با اسم درست و تمیز آهنگ (نه
    اسم خام فایل یوتیوب) تگ‌گذاری می‌کنه تا تو تلگرام درست نشون داده بشه.
    """
    label = f"{title} - {artist}" if artist else title
    markup = link_markup(url)
    tmp_dir = tempfile.mkdtemp(prefix="song_dl_")
    try:
        # --- ویدیو ---
        state = ProgressState()
        stop_event = asyncio.Event()
        progress_task = asyncio.create_task(run_progress_updates(query, state, stop_event))
        try:
            video_path = await asyncio.to_thread(download_media, url, "best720", "video", tmp_dir, state)
        finally:
            stop_event.set()
            await progress_task

        uid = query.from_user.id
        sent_any = False

        v_bytes = os.path.getsize(video_path)
        video_size_mb = v_bytes / 1024 / 1024
        v_problem = quota_problem(uid, v_bytes)
        if video_size_mb > MAX_TELEGRAM_UPLOAD_MB:
            await query.message.reply_text(
                f"⚠️ حجم ویدیو {video_size_mb:.1f}MB هست و بیشتر از سقف {MAX_TELEGRAM_UPLOAD_MB}MB تلگرامه."
            )
        elif v_problem:
            await query.message.reply_text(v_problem)
        else:
            video_text = f"📤 در حال ارسال ویدیو ({video_size_mb:.0f}MB)..."
            await set_status(query, video_text)
            await with_heartbeat(
                query, video_text,
                send_media_file(
                    context.bot, "video", chat_id, video_path,
                    supports_streaming=True, caption=f"🎬 {label}", reply_markup=markup,
                ),
            )
            record_sent(uid, v_bytes)
            sent_any = True

        # --- نسخه‌ی صوتی ---
        await set_status(query, f"⬇️ در حال دانلود نسخه‌ی صوتی «{label}»...")
        audio_path = await asyncio.to_thread(download_media, url, "bestaudio", "audio", tmp_dir)
        a_bytes = os.path.getsize(audio_path)
        audio_size_mb = a_bytes / 1024 / 1024
        a_problem = quota_problem(uid, a_bytes)
        if audio_size_mb > MAX_TELEGRAM_UPLOAD_MB:
            await query.message.reply_text(
                f"⚠️ حجم فایل صوتی {audio_size_mb:.1f}MB هست و بیشتر از سقف {MAX_TELEGRAM_UPLOAD_MB}MB تلگرامه."
            )
        elif a_problem:
            await query.message.reply_text(a_problem)
        else:
            await send_media_file(
                context.bot, "audio", chat_id, audio_path,
                title=title, performer=artist or None, reply_markup=markup,
            )
            record_sent(uid, a_bytes)
            sent_any = True

        if sent_any:
            record_download(uid)
        await cleanup_after_send(context, query, user_msg_id if sent_any else None)
        return sent_any
    except Exception as e:
        logger.exception("deliver_song failed")
        await set_status(query, "❌ ارسال آهنگ با خطا مواجه شد:\n" + str(e)[:300])
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def shazam_available() -> bool:
    if not SHAZAM_ENABLED:
        return False
    import importlib.util

    try:
        return importlib.util.find_spec("shazamio") is not None
    except (ImportError, ValueError):
        return "shazamio" in sys.modules


async def recognize_song_via_shazam(audio_path: str):
    """
    تشخیص آهنگ با Shazam (shazamio). کلید و ثبت‌نام نمی‌خواد.
    برمی‌گردونه: (title, artist) یا None (پیدا نشد / خطا / نصب نبود).
    """
    if not shazam_available():
        return None
    try:
        from shazamio import Shazam

        kwargs = {"max_skew": SHAZAM_MAX_SKEW} if SHAZAM_MAX_SKEW else {}
        shazam = Shazam()
        try:
            out = await asyncio.wait_for(shazam.recognize(audio_path, **kwargs), timeout=40)
        finally:
            # shazamio>=0.9 برای هر کلاینت یه نشست HTTP باز می‌کنه؛ بستنش جلوی نشتی رو می‌گیره
            close = getattr(shazam, "close", None)
            if close:
                try:
                    await close()
                except Exception:
                    pass
    except Exception as e:
        if type(e).__name__ == "RateLimited":
            logger.warning("Shazam rate limit (429); falling back to the next recognizer")
        else:
            logger.warning("Shazam recognition failed: %s", repr(e)[:200])
        return None
    track = (out or {}).get("track") if isinstance(out, dict) else None
    if not track:
        return None
    title = track.get("title")
    if not title:
        return None
    return title, track.get("subtitle")


def has_audio_recognition() -> bool:
    return bool(
        shazam_available()
        or AUDD_API_KEY
        or (ACR_ACCESS_KEY and ACR_ACCESS_SECRET and ACR_HOST)
    )


async def recognize_snippet(snippet_path: str):
    """یه قطعه‌ی صوتی رو به ترتیب با Shazam، AudD و (اگه تنظیم باشه) ACRCloud امتحان می‌کنه."""
    result = await recognize_song_via_shazam(snippet_path)
    if not result:
        result = await asyncio.to_thread(recognize_song_via_audd, snippet_path)
    if not result:
        result = await asyncio.to_thread(recognize_song_via_acrcloud, snippet_path)
    return result


async def recognize_from_audio_file(query, audio_path: str, duration: float, tmp_dir: str):
    """تشخیص آهنگ از روی یه فایل صوتی/ویدیویی (Shazam، AudD، ACRCloud؛ تیکه‌ی اول و وسط)."""
    await set_status(query, "🎧 در حال تشخیص آهنگ (تیکه‌ی اول)...")
    snippet1 = await asyncio.to_thread(
        make_snippet, audio_path, tmp_dir, 0, SONG_SNIPPET_SECONDS, "snip1"
    )
    result = await recognize_snippet(snippet1)

    # اگه تیکه‌ی اول (که ممکنه فقط حرف زدن باشه) جواب نداد و فایل
    # به‌اندازه‌ی کافی طولانیه، یه تیکه‌ی دیگه از وسطش رو هم امتحان کن
    if not result and duration and duration > 40:
        await set_status(query, "🎧 در حال تشخیص آهنگ (تیکه‌ی دوم)...")
        mid_start = max(0, (duration / 2) - (SONG_SNIPPET_SECONDS / 2))
        snippet2 = await asyncio.to_thread(
            make_snippet, audio_path, tmp_dir, mid_start, SONG_SNIPPET_SECONDS, "snip2"
        )
        result = await recognize_snippet(snippet2)
    return result


async def find_and_deliver_song(query, context, chat_id: int, title: str, artist, variant=None, user_msg_id=None):
    """بعد از شناسایی اسم آهنگ: نسخه‌ی رسمیش رو پیدا و ویدیو + ویس رو می‌فرسته."""
    label = f"{title} - {artist}" if artist else title
    variant_txt = f" ({variant})" if variant else ""
    await set_status(
        query, f"🎵 آهنگ اصلی: {label}{variant_txt}\n🔎 در حال پیدا کردن نسخه‌ی رسمی..."
    )

    search_query = label if not variant else f"{label} {variant}"
    try:
        best = await asyncio.to_thread(search_music, search_query)
    except Exception as e:
        logger.exception("search_music failed in find_and_deliver_song")
        await set_status(query, "❌ جستجو با خطا مواجه شد:\n" + str(e)[:300])
        return False

    if not best:
        artist_txt = f"\nخواننده: {artist}" if artist else ""
        await set_status(
            query,
            f"❌ نسخه‌ی رسمی «{title}» رو پیدا نکردم.{artist_txt}\n"
            "می‌تونی از دکمه‌ی «🎵 جستجوی موزیک» با اسم دقیق‌تر امتحان کنی.",
        )
        return False

    best_url = best.get("url") or best.get("webpage_url") or best.get("id")
    if best.get("id") and not str(best_url).startswith("http"):
        best_url = f"https://www.youtube.com/watch?v={best['id']}"

    return await deliver_song(query, context, chat_id, best_url, title, artist, user_msg_id=user_msg_id)


async def handle_find_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کاربر دکمه‌ی «پیدا کردن آهنگ اصلی ویدیو» رو زده."""
    query = update.callback_query
    if not await job_allowed(query):
        return

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
    video_meta = entry.get("video_meta") or {}
    chat_id = query.message.chat_id

    title = artist = variant = None

    # حالت ۱: خود پلتفرم اسم آهنگ رو تو متادیتا داده بود (مثلاً تیک‌تاک)
    if known_track:
        title, artist = known_track

    # حالت ۲: با کمک Gemini از روی عنوان/کپشن ویدیو حدس می‌زنیم (سریع‌تر
    # و رایگان‌تر از تشخیص از روی صدا، و برای آهنگ‌های فارسی/گمنام هم
    # بهتر جواب می‌ده)
    if not title and GEMINI_API_KEY:
        await set_status(query, "🤖 در حال بررسی کپشن ویدیو با هوش مصنوعی...")
        try:
            ai_result = await asyncio.to_thread(
                ai_identify_song,
                video_meta.get("title", ""),
                video_meta.get("description", ""),
                video_meta.get("uploader", ""),
            )
        except Exception:
            logger.exception("ai_identify_song failed")
            ai_result = None
        if ai_result:
            title, artist, variant = ai_result

    # حالت ۳: هیچ‌کدوم بالا جواب نداد -> از روی خود صدای ویدیو تشخیص بدیم
    if not title:
        if not has_audio_recognition():
            hint = (
                " یا یه GEMINI_API_KEY رایگان از aistudio.google.com بگیر تا"
                " از روی کپشن ویدیو حدس بزنم"
                if not GEMINI_API_KEY
                else ""
            )
            await set_status(
                query,
                "ℹ️ نتونستم از متادیتا یا کپشن ویدیو آهنگ رو تشخیص بدم. برای"
                " تشخیص از روی خود صدا باید یه AUDD_API_KEY (یا ACRCloud)"
                f" تنظیم بشه{hint}.",
            )
            return

        await set_status(query, "⬇️ در حال دانلود صدای ویدیو برای تشخیص...")
        tmp_dir = tempfile.mkdtemp(prefix="song_id_")
        try:
            full_audio_path, duration = await asyncio.to_thread(download_full_audio, url, tmp_dir)
            result = await recognize_from_audio_file(query, full_audio_path, duration, tmp_dir)
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

    delivered = await find_and_deliver_song(
        query, context, chat_id, title, artist, variant, user_msg_id=entry.get("user_msg_id")
    )
    if delivered:
        PENDING.pop(key, None)


# ------------------------------------------------------------------
# فایل ویدیوی ارسالی (نه لینک): کاربر خود کلیپ رو می‌فرسته
# ------------------------------------------------------------------
UPLOADS = {}  # key -> {"file_id", "file_size", "name", "caption"}
TELEGRAM_CLOUD_DOWNLOAD_LIMIT_MB = 20  # سقف دریافت فایل با سرور رسمی تلگرام


def probe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def extract_mp3(src: str, out_dir: str, name: str = "audio") -> str:
    out_path = os.path.join(out_dir, f"{name}.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-vn", "-acodec", "libmp3lame", "-q:a", "2", out_path],
        check=True, capture_output=True, timeout=600,
    )
    return out_path


async def handle_uploaded_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کاربر خود فایل ویدیو/صدا رو فرستاده -> می‌پرسیم چیکار کنم."""
    message = update.message
    media = message.video or message.video_note or message.audio or message.document
    if not media:
        return

    user_id = update.effective_user.id if update.effective_user else message.chat_id
    remaining = cooldown_remaining(_last_message_at, user_id)
    if remaining:
        await warn_slow_down(message, user_id, remaining)
        return

    key = f"{message.chat_id}_{message.message_id}"
    UPLOADS[key] = {
        "file_id": media.file_id,
        "file_size": media.file_size or 0,
        "name": getattr(media, "file_name", None) or "",
        "caption": message.caption or "",
    }
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵 استخراج صدا", callback_data=f"fa|{key}")],
        [InlineKeyboardButton("🎧 پیدا کردن آهنگ اصلی", callback_data=f"fs|{key}")],
    ])
    await message.reply_text("🎞 فایلت رو گرفتم. چیکار کنم؟", reply_markup=markup, reply_to_message_id=message.message_id)


async def download_uploaded_file(query, context, entry: dict, tmp_dir: str):
    """فایل ارسالی کاربر رو از تلگرام دانلود می‌کنه؛ اگه نشد None برمی‌گردونه (و پیام خطا می‌ده)."""
    size_mb = (entry["file_size"] or 0) / 1024 / 1024
    if not LOCAL_API_URL and size_mb > TELEGRAM_CLOUD_DOWNLOAD_LIMIT_MB:
        await set_status(
            query,
            f"❌ حجم فایل {size_mb:.1f}MB هست و ربات با سرور رسمی تلگرام فقط تا"
            f" {TELEGRAM_CLOUD_DOWNLOAD_LIMIT_MB}MB رو می‌تونه دریافت کنه. برای فایل‌های"
            " بزرگ‌تر باید TELEGRAM_API_ID و TELEGRAM_API_HASH ست بشن (سرور محلی).",
        )
        return None

    await set_status(query, "⬇️ در حال دریافت فایل از تلگرام...")
    try:
        tg_file = await context.bot.get_file(entry["file_id"])
        ext = Path(entry["name"]).suffix or ".mp4"
        path = os.path.join(tmp_dir, "input" + ext)
        await tg_file.download_to_drive(path)
    except BadRequest as e:
        if "too big" in str(e).lower():
            await set_status(query, "❌ این فایل برای دریافت توسط ربات خیلی بزرگه (سقف ۲۰ مگابایت بدون سرور محلی).")
            return None
        raise
    return path


async def handle_upload_extract_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await job_allowed(query):
        return
    try:
        _, key = query.data.split("|", 1)
    except ValueError:
        return
    entry = UPLOADS.get(key)
    if not entry:
        await set_status(query, "⌛ این درخواست منقضی شده، فایل رو دوباره بفرست.")
        return

    tmp_dir = tempfile.mkdtemp(prefix="up_audio_")
    try:
        path = await download_uploaded_file(query, context, entry, tmp_dir)
        if not path:
            return
        await set_status(query, "🎶 در حال استخراج صدا...")
        mp3_path = await asyncio.to_thread(extract_mp3, path, tmp_dir)

        size_mb = os.path.getsize(mp3_path) / 1024 / 1024
        if size_mb > MAX_TELEGRAM_UPLOAD_MB:
            await set_status(query, f"❌ حجم فایل صوتی {size_mb:.1f}MB هست و از سقف {MAX_TELEGRAM_UPLOAD_MB}MB بیشتره.")
            return

        mp3_bytes = os.path.getsize(mp3_path)
        problem = quota_problem(query.from_user.id, mp3_bytes)
        if problem:
            await set_status(query, problem)
            return

        title = Path(entry["name"]).stem or None
        await send_media_file(
            context.bot, "audio", query.message.chat_id, mp3_path, title=title
        )
        record_sent(query.from_user.id, mp3_bytes)
        record_download(query.from_user.id)
        await cleanup_after_send(context, query)
    except Exception as e:
        logger.exception("upload extract audio failed")
        await set_status(query, "❌ استخراج صدا با خطا مواجه شد:\n" + str(e)[:300])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        UPLOADS.pop(key, None)


async def handle_upload_find_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await job_allowed(query):
        return
    try:
        _, key = query.data.split("|", 1)
    except ValueError:
        return
    entry = UPLOADS.get(key)
    if not entry:
        await set_status(query, "⌛ این درخواست منقضی شده، فایل رو دوباره بفرست.")
        return

    chat_id = query.message.chat_id
    title = artist = variant = None

    # اگه کاربر کپشن یا اسم فایل گذاشته، اول Gemini اون رو بررسی کنه
    if GEMINI_API_KEY and (entry["caption"] or entry["name"]):
        await set_status(query, "🤖 در حال بررسی کپشن با هوش مصنوعی...")
        try:
            ai_result = await asyncio.to_thread(
                ai_identify_song, entry["name"], entry["caption"], ""
            )
        except Exception:
            logger.exception("ai_identify_song failed for upload")
            ai_result = None
        if ai_result:
            title, artist, variant = ai_result

    if not title:
        if not has_audio_recognition():
            await set_status(
                query,
                "ℹ️ برای تشخیص آهنگ از روی صدای فایل باید یه AUDD_API_KEY (یا"
                " ACRCloud) تنظیم بشه.",
            )
            UPLOADS.pop(key, None)
            return

        tmp_dir = tempfile.mkdtemp(prefix="up_song_")
        try:
            path = await download_uploaded_file(query, context, entry, tmp_dir)
            if not path:
                return
            duration = await asyncio.to_thread(probe_duration, path)
            result = await recognize_from_audio_file(query, path, duration, tmp_dir)
        except Exception as e:
            logger.exception("upload find song failed")
            await set_status(query, "❌ تشخیص آهنگ با خطا مواجه شد:\n" + str(e)[:300])
            return
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if not result:
            await set_status(
                query,
                "❌ نتونستم آهنگ این فایل رو تشخیص بدم. شاید صدای پس‌زمینه"
                " واضح نبود یا آهنگ تو دیتابیس سرویس تشخیص نیست.",
            )
            UPLOADS.pop(key, None)
            return
        title, artist = result

    UPLOADS.pop(key, None)
    await find_and_deliver_song(query, context, chat_id, title, artist, variant)


def log_ytdlp_environment():
    """نسخه‌ی yt-dlp، پیدا شدن Deno و وجود کوکی رو تو لاگ می‌نویسه (برای عیب‌یابی خطای ۴۰۳)."""
    try:
        import yt_dlp.version as ytv

        deno_path = shutil.which("deno")
        if not deno_path:
            try:
                import deno  # پکیج pip که باینری Deno رو میاره

                deno_path = deno.find_deno_exe()
            except Exception:
                deno_path = None
        logger.info(
            "yt-dlp %s | deno: %s | cookies: %s",
            ytv.__version__,
            deno_path or "NOT FOUND",
            bool(COOKIES_FILE and Path(COOKIES_FILE).exists()),
        )
    except Exception:
        logger.exception("could not log yt-dlp environment")


def main():
    if BOT_TOKEN == "PUT-YOUR-TOKEN-HERE":
        raise SystemExit(
            "توکن ربات رو ست نکردی! متغیر محیطی TELEGRAM_BOT_TOKEN رو بذار یا"
            " مستقیم توی کد جایگزین کن."
        )

    log_ytdlp_environment()
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        # آپلود فایل‌های بزرگ دقیقه‌ها طول می‌کشه؛ تایم‌اوت پیش‌فرض (۵ تا ۲۰
        # ثانیه) وسط ارسال قطعش می‌کرد
        .connect_timeout(30)
        .read_timeout(1800)
        .write_timeout(1800)
        .pool_timeout(30)
        # تا وقتی یه فایل بزرگ داره ارسال می‌شه، ربات به بقیه (و بقیه‌ی
        # کاربرها) هم جواب بده
        .concurrent_updates(True)
        .post_init(post_init)
    )
    if LOCAL_API_URL:
        base = LOCAL_API_URL.rstrip("/")
        builder = (
            builder.base_url(f"{base}/bot")
            .base_file_url(f"{base}/file/bot")
            .local_mode(True)  # فایل‌ها با مسیر (file://) رد و بدل می‌شن
        )
        logger.info("using local Bot API server at %s (local mode)", base)
    # محدودکننده‌ی نرخ ارسال (جلوگیری از فلود تلگرام، مخصوصاً برای ارسال‌های پشت‌سرهم و پیام همگانی)
    try:
        from telegram.ext import AIORateLimiter

        builder = builder.rate_limiter(AIORateLimiter(max_retries=3))
        logger.info("rate limiter: enabled")
    except (ImportError, RuntimeError):
        logger.warning('rate limiter: disabled (pip install "python-telegram-bot[rate-limiter]==21.*" to enable)')
    app = builder.build()

    try:
        database = get_db()
        logger.info(
            "database: %s | admins: %s | daily quota: %sMB | auto-VIP: %s",
            database.backend,
            sorted(ADMIN_IDS) or "NONE (ADMIN_IDS رو ست کن)",
            DAILY_QUOTA_MB,
            f"after {AUTO_VIP_AFTER_DAYS}d -> {AUTO_VIP_DAYS}d" if AUTO_VIP_AFTER_DAYS > 0 else "off",
        )
        logger.info("song recognition: shazam=%s audd=%s acrcloud=%s", shazam_available(), bool(AUDD_API_KEY), bool(ACR_HOST))
    except Exception:
        logger.exception("database init failed")
    # هر کاربری که با ربات کار کنه خودکار ثبت‌نام می‌شه
    app.add_handler(TypeHandler(Update, track_user), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_HELP)}$"), help_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_ABOUT)}$"), about_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_MUSIC)}$"), music_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_ACCOUNT)}$"), account_button))
    app.add_handler(CommandHandler("me", account_button))
    app.add_handler(MessageHandler(
        filters.Regex(re.compile(r"^\s*(آیدی|ایدی|id)\s*$", re.IGNORECASE)), id_command
    ))
    app.add_handler(CommandHandler(["id", "myid"], id_command))
    app.add_handler(CommandHandler("admin", admin_entry))
    app.add_handler(MessageHandler(
        filters.Regex(re.compile(r"^\s*(ادمین|پنل|پنل ادمین|admin)\s*$", re.IGNORECASE)) & AdminOnly(),
        admin_entry,
    ))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.VIDEO_NOTE | filters.AUDIO
        | filters.Document.VIDEO | filters.Document.AUDIO,
        handle_uploaded_media,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm\|"))
    app.add_handler(CallbackQueryHandler(admin_days_callback, pattern=r"^admd\|"))
    app.add_handler(CallbackQueryHandler(handle_find_song, pattern=r"^song\|"))
    app.add_handler(CallbackQueryHandler(handle_upload_extract_audio, pattern=r"^fa\|"))
    app.add_handler(CallbackQueryHandler(handle_upload_find_song, pattern=r"^fs\|"))
    app.add_handler(CallbackQueryHandler(handle_music_page, pattern=r"^mpage\|"))
    app.add_handler(CallbackQueryHandler(handle_music_youtube_fallback, pattern=r"^myoutube\|"))
    app.add_handler(CallbackQueryHandler(handle_music_pick, pattern=r"^mpick\|"))
    app.add_handler(CallbackQueryHandler(handle_playlist_page, pattern=r"^plpage\|"))
    app.add_handler(CallbackQueryHandler(handle_playlist_pick, pattern=r"^plpick\|"))
    app.add_handler(CallbackQueryHandler(handle_playlist_done, pattern=r"^pldone\|"))

    logger.info("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
