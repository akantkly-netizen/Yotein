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
# httpx هر درخواست رو با URL کامل (شامل توکن ربات) لاگ می‌کنه؛ پس فقط هشدار/خطاش رو نگه می‌داریم
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

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
# ------------------------------------------------------------------
# چندزبانه (i18n): متن‌ها داخل locales/<کد>.json هستن؛ T("کلید", متغیر=...) متن زبانِ کاربرِ فعلی رو برمی‌گردونه
# ------------------------------------------------------------------
import contextvars
from contextlib import contextmanager

LANGS = [  # (کد، اسم بومی، پرچم). اگه بیشتر از ۱۰ زبان بشه، خودکار صفحه‌ی بعدی با دکمه‌ی «بعدی» ساخته می‌شه
    ("fa", "فارسی", "🇮🇷"), ("en", "English", "🇬🇧"), ("tr", "Türkçe", "🇹🇷"),
    ("ru", "Русский", "🇷🇺"), ("es", "Español", "🇪🇸"), ("fr", "Français", "🇫🇷"),
]
LANG_CODES = [c for c, _, _ in LANGS]
DEFAULT_LANG = "fa"
LANG_PAGE_SIZE = 10
LOCALES_DIR = Path(__file__).resolve().parent / "locales"
LANG_PROMPT = "🌐 Please choose your language\n🌐 زبان خود را انتخاب کنید"


# ترجمه‌ها (فارسی + ۵ زبان) داخل همین فایل هستن تا فقط با یه فایل کار کنه. اگه پوشه‌ی locales/ کنار فایل
# باشه و <کد>.json داشته باشه، اون فایل جای ترجمه‌ی داخلی رو می‌گیره (برای اضافه‌کردن زبان یا ویرایش آسون).
_EMBEDDED_LOCALES = json.loads(r'''{"fa":{"__init__.1":"🔒 این محتوا برای حسابی که ربات با آن وارد شده قابل‌مشاهده نیست (اینستاگرام آن را فقط برای مخاطبان خاص یا با محدودیت سنی نشان می‌دهد).\nپست خصوصی یا لینک خراب نیست، اما ربات امکان دانلودش را ندارد.","__init__.2":"🔒 این محتوا فقط برای مخاطبان خاصی (مثلاً با محدودیت سنی) در اینستاگرام قابل‌مشاهده است و ربات حساب واردشده‌ای برای امتحان دوباره ندارد.\nپست خصوصی یا لینک خراب نیست، اما ربات امکان دانلودش را ندارد.","format_music_entry_label.1":"بدون‌نام","build_music_page.1":"◀️ صفحه قبل","build_music_page.2":"▶️ صفحه بعد","build_music_page.3":"🔍 جستجو در یوتیوب","render.1":"⚙️ در حال پردازش نهایی (تبدیل/ترکیب فایل)...","render.2":"؟","render.3":"  •  {n}s مونده","render.4":"⬇️ در حال دانلود...\n[{bar}] {percent:.0f}%\n{line2}","pick_quality_options.1":" (تقریبی)","pick_quality_options.2":"🎬 کیفیت پیش‌فرض{size_txt}","pick_quality_options.3":"🎵 فقط صدا (MP3)","start.1":"درود به روی ماهت 🧘🏾🌚\nمن ربات دانلودرم 🧸\n\nبا من می‌تونی ویدئو ها، موزیک ها و پست های هر پلتفرمی رو که بخوای بدون محدودیت دانلود کنی 🧘🏾✨️\n\nو همچنین میتونی موزیک پست و ویدئو دلخواهت رو با استفاده از من پیدا و دانلود کنی 🧘🏾🎧\n\nکافیه فقط لینک پستی دلخواهت رو برام بفرستی 🧸","help_button.1":"📖 راهنمای کامل ربات\n\n🔗 دانلود از لینک (اینستاگرام / یوتیوب / تیک‌تاک)\n۱. لینک پست، ریلز، استوری اینستاگرام، ویدیو یا شورتز یوتیوب، یا ویدیوی تیک‌تاک رو برام بفرست\n۲. تامبنیل و توضیحات محتوا رو نشونت می‌دم\n۳. زیرش چند دکمه‌ی کیفیت می‌بینی، هرکدوم با حجم دقیق فایل (مثلاً «1080p - 24.3MB»)؛ هرکدوم رو بخوای انتخاب کن\n۴. اگه فقط صدای ویدیو رو می‌خوای (نه خود ویدیو)، دکمه‌ی «🎵 فقط صدا» رو بزن\n۵. حین دانلود یه نوار پیشرفت زنده می‌بینی (درصد، حجم دانلودشده از کل، سرعت)؛ در آخر فایل برات ارسال می‌شه\n\n🎵 جستجوی موزیک (دکمه‌ی «{BTN_MUSIC}»)\n۱. روی دکمه بزن و اسم آهنگ یا فقط اسم خواننده رو بفرست\n۲. اول تو یوتیوب‌میوزیک (کاتالوگ رسمی) می‌گردم؛ اگه اسم خواننده بفرستی، همه‌ی آهنگ‌های شناخته‌شده‌اش از همونجا میاد (برای خواننده‌های خارجی معمولاً کامله)؛ نتیجه‌ها ۱۰تا-۱۰تا صفحه‌بندی شدن با دکمه‌های «◀️ صفحه قبل» / «▶️ صفحه بعد»\n۳. روی اسم هر آهنگ که بزنی، همون دکمه‌های کیفیت/فقط‌صدای بالا براش میاد تا انتخاب کنی\n۴. اگه چیزی پیدا نشد یا کم بود (مثلاً خیلی از آهنگ‌های فارسی تو یوتیوب‌میوزیک نیستن)، پایین لیست دکمه‌ی «🔍 جستجو در یوتیوب» هست؛ بزن تا همون جستجو رو تو یوتیوب عادی هم انجام بدم\n\n📃 پلی‌لیست یوتیوب\nلینک یه پلی‌لیست (همونی که /playlist?list=... داره) رو بفرست؛ اطلاعات پلی‌لیست و ویدیوهاش ۵تا-۵تا صفحه‌بندی می‌شن. روی هر ویدیو بزنی، اطلاعات و تصویرش میاد و می‌تونی کیفیت رو انتخاب و دانلودش کنی. با «▶️ صفحه بعد» و «◀️ صفحه قبل» بین صفحه‌ها برو و آخرش «✅ کارم تموم شد» رو بزن تا دکمه‌ها بسته بشن (اطلاعات پلی‌لیست می‌مونه)\n\n🎞 ارسال خود فایل ویدیو (نه لینک)\nاگه خود کلیپ/ویدیو رو برام بفرستی (مثلاً فایل یه ریلز)، می‌پرسم «چیکار کنم؟» با دو دکمه: «🎵 استخراج صدا» (صدای ویدیو رو mp3 می‌کنم و می‌فرستم) یا «🎧 پیدا کردن آهنگ اصلی» (آهنگ ویدیو رو تشخیص می‌دم و نسخه‌ی رسمیش رو با ویس می‌فرستم)\n• اگه برای ویدیو کپشن بنویسی، تشخیص دقیق‌تر می‌شه\n• دریافت فایل بزرگ‌تر از ۲۰ مگابایت فقط با سرور محلی تلگرام (TELEGRAM_API_ID/HASH) ممکنه\n\n🎧 پیدا کردن آهنگ اصلی یه ویدیو\nزیر دکمه‌های کیفیت هر ویدیو، یه دکمه‌ی «🎵 پیدا کردن آهنگ اصلی ویدیو» هم هست. با زدنش:\n۱. اول چک می‌کنم خود پلتفرم (بیشتر تیک‌تاک) اسم آهنگ رو تو متادیتاش داده یا نه\n۲. اگه نداشت، یه تیکه از صدای خود ویدیو رو می‌گیرم و با سرویس تشخیص آهنگ شناساییش می‌کنم\n۳. بعد از پیدا کردن نسخه‌ی رسمی، خودم هم ویدیو هم نسخه‌ی صوتیش رو با اسم درست و تمیز آهنگ برات می‌فرستم (نیازی به انتخاب دستی نیست)\n۴. اگه پیدا نشد، لااقل اسم خواننده رو (اگه شناسایی شده باشه) بهت می‌گم تا خودت دستی جستجو کنی\n\n👤 حساب، سهمیه و VIP\n• دکمه‌ی «👤 حساب من»: اطلاعات حسابت، تعداد دانلودها، حجم مصرف‌شده‌ی امروز و وضعیت اشتراک\n• هر کاربر روزانه تا {volume} حجم دانلود داره که هر شب نیمه‌شب (به وقت ایران) ریست می‌شه\n• با نوشتن «آیدی» یه آیدی ۸ رقمی اختصاصی می‌گیری؛ اون رو به مدیر بده تا اشتراک VIP (دانلود نامحدود برای چند روز) برات فعال کنه\n\n⚠️ نکات مهم\n• برای جلوگیری از اسپم، هر {REQUEST_COOLDOWN_SECONDS} ثانیه فقط یه درخواست جدید می‌تونی بدی؛ اگه زود بفرستی، می‌گم چند ثانیه صبر کنی\n• پست‌های خصوصی یا ویدیوهای محدود بدون تنظیم کوکی قابل دانلود نیستن\n• فقط برای محتوای خودتون یا محتوایی که اجازه‌ی استفاده دارید ازم استفاده کنید؛ مسئولیت رعایت کپی‌رایت با کاربره","about_button.1":"ℹ️ درباره ربات:\n\nاین ربات با کمک yt-dlp از اینستاگرام، یوتیوب و تیک‌تاک دانلود می‌کنه.\nلطفاً فقط برای محتوای خودتون یا محتوایی که اجازه‌ی استفاده دارید ازش استفاده کنید؛ رعایت کپی‌رایت و قوانین پلتفرم‌ها به عهده‌ی خودتونه.","music_button.1":"🎵 اسم آهنگ و خواننده رو بفرست (مثلاً: «Shadmehr Aghili - Bahar»).\nنسخه‌ی رسمیش رو پیدا می‌کنم و برات می‌فرستم.","handle_music_query.1":"یه اسم آهنگ یا خواننده بفرست تا جستجو کنم.","handle_music_query.2":"🔎 دارم تو یوتیوب‌میوزیک دنبال «{query_text}» می‌گردم...","handle_music_query.3":"❌ جستجو با خطا مواجه شد:\n","handle_music_query.4":"تو یوتیوب‌میوزیک چیزی برای «{query_text}» پیدا نشد. می‌تونی از دکمه‌ی پایین تو یوتیوب عادی هم جستجو کنی:","handle_music_query.5":"🎵 نتایج یوتیوب‌میوزیک برای «{query_text}» ({count} مورد) — صفحه ۱ از {total_pages}:\nیکی رو انتخاب کن، یا اگه کافی نبود پایین «جستجو در یوتیوب» رو بزن:","handle_music_page.1":"⌛ این جستجو منقضی شده، دوباره از دکمه‌ی جستجوی موزیک استفاده کن.","handle_music_page.2":"یوتیوب‌میوزیک","handle_music_page.3":"یوتیوب","handle_music_page.4":"🎵 نتایج {source_label} برای «{query}» ({count} مورد) — صفحه {page} از {total_pages}:\nیکی رو انتخاب کن:","handle_music_youtube_fallback.1":"🔎 دارم تو یوتیوب دنبال «{query_text}» می‌گردم...","handle_music_youtube_fallback.2":"تو یوتیوب هم چیزی برای «{query_text}» پیدا نشد.","handle_music_youtube_fallback.3":"🎵 نتایج یوتیوب برای «{query_text}» ({count} مورد) — صفحه ۱ از {total_pages}:\nیکی رو انتخاب کن:","handle_music_pick.1":"⏳ در حال گرفتن اطلاعات...","show_quality_picker.1":"❌ نتونستم اطلاعات این لینک رو بگیرم. ممکنه پست خصوصی باشه یا لینک اشتباه باشه.\nجزئیات خطا: ","show_quality_picker.2":"\nکیفیت مورد نظرت رو انتخاب کن:","show_quality_picker.3":"کیفیت مورد نظرت رو انتخاب کن:","show_quality_picker.4":"…\n\nکیفیت مورد نظرت رو انتخاب کن:","show_quality_picker.5":"🎵 پیدا کردن آهنگ اصلی ویدیو","playlist_info_text.1":"🎞 {count} ویدیو","playlist_info_text.2":" ({skipped} مورد خصوصی/حذف‌شده نادیده گرفته شد)","playlist_page_text.1":"{playlist_info_text}\n\nصفحه {page} از {playlist_total_pages} — یکی رو انتخاب کن:","build_playlist_markup.1":"✅ کارم تموم شد","show_playlist.1":"❌ نتونستم پلی‌لیست رو بخونم. ممکنه خصوصی باشه یا لینک اشتباه باشه.\nجزئیات خطا: ","show_playlist.2":"این پلی‌لیست ویدیوی قابل‌دسترسی نداره (خالیه یا همه‌ی ویدیوهاش خصوصی/حذف‌شده‌ان).","show_playlist.3":"پلی‌لیست","handle_playlist_page.1":"⌛ این پلی‌لیست منقضی شده، لینکش رو دوباره بفرست.","handle_playlist_pick.1":"⏳ دارم اطلاعات ویدیو رو می‌گیرم...","handle_playlist_done.1":"{base}\n\n✅ کارم تموم شد.","handle_message.1":"⏳ دارم پلی‌لیست رو می‌خونم...","handle_message.2":"این یه لینک معتبر نیست. لینک پست/ریلز/استوری اینستاگرام، ویدیو/شورتز یوتیوب یا ویدیوی تیک‌تاک رو بفرست، یا از دکمه‌ی «{BTN_MUSIC}» برای جستجوی آهنگ استفاده کن.","handle_message.3":"⏳ دارم اطلاعات لینک رو می‌گیرم...","warn_slow_down.1":"⏳ برای جلوگیری از اسپم، هر {REQUEST_COOLDOWN_SECONDS} ثانیه فقط یه درخواست می‌تونی بدی.\nلطفاً {remaining} ثانیه دیگه دوباره بفرست.","quota_problem.1":"📦 سهمیه‌ی امروزت برای این فایل کافی نیست.\nحجم فایل: {volume}\nباقی‌مونده‌ی امروز: {volume2} از {volume3}\n\nسهمیه هر شب نیمه‌شب (به وقت ایران) ریست می‌شه. می‌تونی کیفیت پایین‌تری انتخاب کنی، یا با اشتراک VIP بدون محدودیت دانلود کنی (با نوشتن «آیدی» آیدی ۸ رقمی‌ت رو بگیر و به مدیر بده).","account_button.1":"👤 <b>حساب من</b>","account_button.2":"🏷 نام: {text}","account_button.3":"🔖 آیدی حروفی: {text}","account_button.4":"🔢 آیدی تلگرام: <code>{user_id}</code>","account_button.5":"🆔 آیدی اختصاصی: <code>{public_id}</code>","account_button.6":"📅 تاریخ عضویت: {date}","account_button.7":"⬇️ تعداد دانلود: {downloads_total}","account_button.8":"👑 وضعیت: مدیر (بدون محدودیت)","account_button.9":"💎 اشتراک: VIP فعال تا {date} ({days_left} روز مونده)","account_button.10":"💎 اشتراک: ندارد","account_button.11":"📦 مصرف امروز: {volume} (نامحدود ♾)","account_button.12":"📦 مصرف امروز: {volume} از {volume2} (باقی‌مونده {volume3})","id_command.1":"🆔 آیدی اختصاصی شما: <code>{public_id}</code>\n🔢 آیدی تلگرام: <code>{user_id}</code>\n\nآیدی اختصاصی هویت حساب شماست؛ برای فعال‌سازی اشتراک VIP همین عدد ۸ رقمی رو به مدیر بده.","build_receipt.1":"🧾 <b>فیش فعال‌سازی اشتراک VIP</b>","build_receipt.2":"شماره فیش: <code>#{receipt_id:05d}</code>","build_receipt.3":"👤 کاربر: {text} {text2}","build_receipt.4":"🆔 آیدی: <code>{public_id}</code>","build_receipt.5":"⏳ مدت: {days} روز","build_receipt.6":" (تمدید)","build_receipt.7":"🎁 نوع: اشتراک ماهانه‌ی خودکار (بعد از {AUTO_VIP_AFTER_DAYS} روز عضویت)","build_receipt.8":"👑 نوع: فعال‌سازی توسط مدیر","build_receipt.9":"📅 شروع: {date}","build_receipt.10":"📅 پایان: {date}","build_receipt.11":"💵 ارزش اشتراک: <b>{usd}</b> (هر روز {usd2})","build_receipt.12":"✅ اشتراک فعال شد.","run_auto_vip_check.1":"🎁 اشتراک ماهانه‌ات فعال شد!\nچون بیشتر از {AUTO_VIP_AFTER_DAYS} روزه که با ما هستی، هر {AUTO_VIP_DAYS} روز یه اشتراک {AUTO_VIP_DAYS} روزه‌ی VIP خودکار برات فعال می‌شه.\n\n","run_auto_vip_check.2":"\n\nتا {date} می‌تونی بدون محدودیت حجم دانلود کنی 💎","job_allowed.1":"📦 سهمیه‌ی امروزت تموم شده. نیمه‌شب ریست می‌شه؛ یا با VIP نامحدود دانلود کن («آیدی» بنویس و به مدیر بده).","job_allowed.2":"⏳ برای جلوگیری از اسپم، {remaining} ثانیه دیگه صبر کن و دوباره بزن.","link_markup.1":"🔗 لینک","cleanup_after_send.1":"✅ ارسال شد!","handle_quality_choice.1":"❌ خطای داخلی، دوباره لینک رو بفرست.","handle_quality_choice.2":"⌛ این درخواست منقضی شده، لطفاً لینک رو دوباره بفرست.","handle_quality_choice.3":"⏳ در حال آماده‌سازی دانلود...","handle_quality_choice.4":"❌ حجم فایل {size_mb:.1f}MB هست و از سقف {MAX_TELEGRAM_UPLOAD_MB}MB ربات‌های تلگرام رسمی بیشتره. برای فایل‌های بزرگ‌تر باید از Local Bot API Server استفاده کنی (توضیح در README).","handle_quality_choice.5":"📤 در حال ارسال فایل ({size_mb:.0f}MB)...\nفایل‌های بزرگ ممکنه چند دقیقه طول بکشه، صبور باش.","handle_quality_choice.6":"❌ دانلود یا ارسال فایل با خطا مواجه شد:\n","deliver_song.1":"⚠️ حجم ویدیو {video_size_mb:.1f}MB هست و بیشتر از سقف {MAX_TELEGRAM_UPLOAD_MB}MB تلگرامه.","deliver_song.2":"📤 در حال ارسال ویدیو ({video_size_mb:.0f}MB)...","deliver_song.3":"⬇️ در حال دانلود نسخه‌ی صوتی «{label}»...","deliver_song.4":"⚠️ حجم فایل صوتی {audio_size_mb:.1f}MB هست و بیشتر از سقف {MAX_TELEGRAM_UPLOAD_MB}MB تلگرامه.","deliver_song.5":"❌ ارسال آهنگ با خطا مواجه شد:\n","recognize_from_audio_file.1":"🎧 در حال تشخیص آهنگ (تیکه‌ی اول)...","recognize_from_audio_file.2":"🎧 در حال تشخیص آهنگ (تیکه‌ی دوم)...","find_and_deliver_song.1":"🎵 آهنگ اصلی: {label}{variant_txt}\n🔎 در حال پیدا کردن نسخه‌ی رسمی...","find_and_deliver_song.2":"\nخواننده: {artist}","find_and_deliver_song.3":"❌ نسخه‌ی رسمی «{title}» رو پیدا نکردم.{artist_txt}\nمی‌تونی از دکمه‌ی «🎵 جستجوی موزیک» با اسم دقیق‌تر امتحان کنی.","handle_find_song.1":"🤖 در حال بررسی کپشن ویدیو با هوش مصنوعی...","handle_find_song.2":" یا یه GEMINI_API_KEY رایگان از aistudio.google.com بگیر تا از روی کپشن ویدیو حدس بزنم","handle_find_song.3":"ℹ️ نتونستم از متادیتا یا کپشن ویدیو آهنگ رو تشخیص بدم. برای تشخیص از روی خود صدا باید یه AUDD_API_KEY (یا ACRCloud) تنظیم بشه{hint}.","handle_find_song.4":"⬇️ در حال دانلود صدای ویدیو برای تشخیص...","handle_find_song.5":"❌ تشخیص آهنگ با خطا مواجه شد:\n","handle_find_song.6":"❌ نتونستم آهنگ این ویدیو رو تشخیص بدم. شاید صدای پس‌زمینه واضح نبود یا آهنگ تو دیتابیس سرویس تشخیص نیست.","handle_uploaded_media.1":"🎵 استخراج صدا","handle_uploaded_media.2":"🎧 پیدا کردن آهنگ اصلی","handle_uploaded_media.3":"🎞 فایلت رو گرفتم. چیکار کنم؟","download_uploaded_file.1":"❌ حجم فایل {size_mb:.1f}MB هست و ربات با سرور رسمی تلگرام فقط تا {TELEGRAM_CLOUD_DOWNLOAD_LIMIT_MB}MB رو می‌تونه دریافت کنه. برای فایل‌های بزرگ‌تر باید TELEGRAM_API_ID و TELEGRAM_API_HASH ست بشن (سرور محلی).","download_uploaded_file.2":"⬇️ در حال دریافت فایل از تلگرام...","download_uploaded_file.3":"❌ این فایل برای دریافت توسط ربات خیلی بزرگه (سقف ۲۰ مگابایت بدون سرور محلی).","handle_upload_extract_audio.1":"⌛ این درخواست منقضی شده، فایل رو دوباره بفرست.","handle_upload_extract_audio.2":"🎶 در حال استخراج صدا...","handle_upload_extract_audio.3":"❌ حجم فایل صوتی {size_mb:.1f}MB هست و از سقف {MAX_TELEGRAM_UPLOAD_MB}MB بیشتره.","handle_upload_extract_audio.4":"❌ استخراج صدا با خطا مواجه شد:\n","handle_upload_find_song.1":"🤖 در حال بررسی کپشن با هوش مصنوعی...","handle_upload_find_song.2":"ℹ️ برای تشخیص آهنگ از روی صدای فایل باید یه AUDD_API_KEY (یا ACRCloud) تنظیم بشه.","handle_upload_find_song.3":"❌ نتونستم آهنگ این فایل رو تشخیص بدم. شاید صدای پس‌زمینه واضح نبود یا آهنگ تو دیتابیس سرویس تشخیص نیست.","btn.help":"📖 راهنما","btn.about":"ℹ️ درباره ربات","btn.music":"🎵 جستجوی موزیک","btn.account":"👤 حساب من","vip_granted.user":"🎉 اشتراک VIP شما فعال شد!\n\n{receipt}\n\nاز این به بعد تا {date} می‌تونی بدون محدودیت حجم دانلود کنی 💎","vip_cancelled.user":"ℹ️ اشتراک VIP شما توسط مدیر لغو شد.\nاز این به بعد سهمیه‌ی روزانه‌ی عادی ({volume}) برای شما اعمال می‌شه."},"en":{"__init__.1":"🔒 This content isn't visible to the account the bot is logged in with (Instagram only shows it to certain audiences or with an age restriction).\nIt's not a private post or a broken link, but the bot can't download it.","__init__.2":"🔒 This content is only visible to certain audiences (for example with an age restriction) on Instagram, and the bot has no logged-in account to try again.\nIt's not a private post or a broken link, but the bot can't download it.","format_music_entry_label.1":"Untitled","build_music_page.1":"◀️ Previous page","build_music_page.2":"▶️ Next page","build_music_page.3":"🔍 Search on YouTube","render.1":"⚙️ Final processing (converting/merging the file)...","render.2":"?","render.3":"  •  {n}s left","render.4":"⬇️ Downloading...\n[{bar}] {percent:.0f}%\n{line2}","pick_quality_options.1":" (approx.)","pick_quality_options.2":"🎬 Default quality{size_txt}","pick_quality_options.3":"🎵 Audio only (MP3)","start.1":"Hey there, friend 🧘🏾🌚\nI'm your downloader bot 🧸\n\nWith me you can download videos, music and posts from almost any platform, with no limits 🧘🏾✨️\n\nYou can also find and download the music, posts and videos you like through me 🧘🏾🎧\n\nJust send me the link to the post you want 🧸","help_button.1":"📖 Complete bot guide\n\n🔗 Download from a link (Instagram / YouTube / TikTok)\n1. Send me the link to an Instagram post, reel or story, a YouTube video or short, or a TikTok video\n2. I'll show you the thumbnail and description\n3. Below it you'll see several quality buttons, each with the exact file size (e.g. “1080p - 24.3MB”); pick whichever you like\n4. If you only want the audio of the video (not the video itself), tap «🎵 Audio only»\n5. While downloading you'll see a live progress bar (percentage, downloaded size of total, speed); when it's done the file is sent to you\n\n🎵 Music search («{BTN_MUSIC}» button)\n1. Tap the button and send a song name or just an artist name\n2. I search YouTube Music (the official catalog) first; if you send an artist name, all their known songs come from there (usually complete for international artists); results are paginated 10 at a time with the «◀️ Previous page» / «▶️ Next page» buttons\n3. Tap any song and you get the same quality / audio-only buttons as above\n4. If nothing is found or there are too few results (for example many Persian songs aren't on YouTube Music), there's a «🔍 Search on YouTube» button below the list; tap it and I'll run the same search on regular YouTube\n\n📃 YouTube playlists\nSend the link of a playlist (the kind that contains /playlist?list=...); the playlist info and its videos are paginated 5 at a time. Tap a video to see its info and thumbnail, then choose a quality and download it. Use «▶️ Next page» and «◀️ Previous page» to move between pages and finally tap «✅ I'm done» to close the buttons (the playlist info stays)\n\n🎞 Sending the video file itself (not a link)\nIf you send me a clip/video file itself (for example a reel file), I'll ask “What should I do?” with two buttons: «🎵 Extract audio» (I convert the video's sound to mp3 and send it) or «🎧 Find the original song» (I identify the song in the video and send you its official version as a voice/audio file)\n• If you write a caption for the video, identification becomes more accurate\n• Receiving files larger than 20 MB is only possible with a local Telegram server (TELEGRAM_API_ID/HASH)\n\n🎧 Finding a video's original song\nUnder each video's quality buttons there is also a «🎵 Find the video's original song» button. When you tap it:\n1. First I check whether the platform itself (mostly TikTok) provides the song name in its metadata\n2. If not, I take a piece of the video's audio and identify it with a song-recognition service\n3. Once the official version is found, I send you both the video and its audio version with the proper, clean song name (no manual selection needed)\n4. If it isn't found, I'll at least tell you the artist's name (if identified) so you can search manually\n\n👤 Account, quota and VIP\n• The «👤 My account» button: your account info, number of downloads, today's used volume and subscription status\n• Every user can download up to {volume} per day, which resets every night at midnight (Iran time)\n• By writing «id» you get a unique 8-digit ID; give it to the admin to activate a VIP subscription (unlimited downloads for some days)\n\n⚠️ Important notes\n• To prevent spam, you can only send one new request every {REQUEST_COOLDOWN_SECONDS} seconds; if you send too fast, I'll tell you how many seconds to wait\n• Private posts or restricted videos can't be downloaded without cookie settings\n• Please use me only for your own content or content you're allowed to use; copyright compliance is the user's responsibility","about_button.1":"ℹ️ About the bot:\n\nThis bot downloads from Instagram, YouTube and TikTok using yt-dlp.\nPlease use it only for your own content or content you have permission to use; complying with copyright and platform rules is your own responsibility.","music_button.1":"🎵 Send the song name and artist (for example: «Shadmehr Aghili - Bahar»).\nI'll find the official version and send it to you.","handle_music_query.1":"Send a song or artist name and I'll search for it.","handle_music_query.2":"🔎 Searching YouTube Music for «{query_text}»...","handle_music_query.3":"❌ The search failed:\n","handle_music_query.4":"Nothing found on YouTube Music for «{query_text}». You can also search regular YouTube with the button below:","handle_music_query.5":"🎵 YouTube Music results for «{query_text}» ({count} items) — page 1 of {total_pages}:\nPick one, or if that's not enough, tap «Search on YouTube» below:","handle_music_page.1":"⌛ This search has expired. Please use the music search button again.","handle_music_page.2":"YouTube Music","handle_music_page.3":"YouTube","handle_music_page.4":"🎵 {source_label} results for «{query}» ({count} items) — page {page} of {total_pages}:\nPick one:","handle_music_youtube_fallback.1":"🔎 Searching YouTube for «{query_text}»...","handle_music_youtube_fallback.2":"Nothing found on regular YouTube either for «{query_text}».","handle_music_youtube_fallback.3":"🎵 YouTube results for «{query_text}» ({count} items) — page 1 of {total_pages}:\nPick one:","handle_music_pick.1":"⏳ Getting the info...","show_quality_picker.1":"❌ I couldn't get the info for this link. The post may be private or the link may be wrong.\nError details: ","show_quality_picker.2":"\nChoose the quality you want:","show_quality_picker.3":"Choose the quality you want:","show_quality_picker.4":"…\n\nChoose the quality you want:","show_quality_picker.5":"🎵 Find the video's original song","playlist_info_text.1":"🎞 {count} videos","playlist_info_text.2":" ({skipped} private/deleted items were skipped)","playlist_page_text.1":"{playlist_info_text}\n\nPage {page} of {playlist_total_pages} — pick one:","build_playlist_markup.1":"✅ I'm done","show_playlist.1":"❌ I couldn't read the playlist. It may be private or the link may be wrong.\nError details: ","show_playlist.2":"This playlist has no accessible videos (it's empty or all of its videos are private/deleted).","show_playlist.3":"Playlist","handle_playlist_page.1":"⌛ This playlist has expired, please send its link again.","handle_playlist_pick.1":"⏳ Getting the video info...","handle_playlist_done.1":"{base}\n\n✅ Done.","handle_message.1":"⏳ Reading the playlist...","handle_message.2":"That isn't a valid link. Send a link to an Instagram post/reel/story, a YouTube video/short or a TikTok video, or use the «{BTN_MUSIC}» button to search for a song.","handle_message.3":"⏳ Getting the link info...","warn_slow_down.1":"⏳ To prevent spam, you can only send one new request every {REQUEST_COOLDOWN_SECONDS} seconds.\nPlease send it again in {remaining} seconds.","quota_problem.1":"📦 Today's quota isn't enough for this file.\nFile size: {volume}\nRemaining today: {volume2} of {volume3}\n\nThe quota resets every night at midnight (Iran time). You can choose a lower quality, or download without limits with a VIP subscription (write «id» to get your 8-digit ID and give it to the admin).","account_button.1":"👤 <b>My account</b>","account_button.2":"🏷 Name: {text}","account_button.3":"🔖 Username: {text}","account_button.4":"🔢 Telegram ID: <code>{user_id}</code>","account_button.5":"🆔 Unique ID: <code>{public_id}</code>","account_button.6":"📅 Member since: {date}","account_button.7":"⬇️ Downloads: {downloads_total}","account_button.8":"👑 Status: Admin (no limits)","account_button.9":"💎 Subscription: VIP active until {date} ({days_left} days left)","account_button.10":"💎 Subscription: none","account_button.11":"📦 Used today: {volume} (unlimited ♾)","account_button.12":"📦 Used today: {volume} of {volume2} ({volume3} left)","id_command.1":"🆔 Your unique ID: <code>{public_id}</code>\n🔢 Telegram ID: <code>{user_id}</code>\n\nThe unique ID identifies your account; give this 8-digit number to the admin to activate a VIP subscription.","build_receipt.1":"🧾 <b>VIP subscription activation receipt</b>","build_receipt.2":"Receipt no.: <code>#{receipt_id:05d}</code>","build_receipt.3":"👤 User: {text} {text2}","build_receipt.4":"🆔 ID: <code>{public_id}</code>","build_receipt.5":"⏳ Duration: {days} days","build_receipt.6":" (renewal)","build_receipt.7":"🎁 Type: automatic monthly subscription (after {AUTO_VIP_AFTER_DAYS} days of membership)","build_receipt.8":"👑 Type: activated by the admin","build_receipt.9":"📅 Start: {date}","build_receipt.10":"📅 End: {date}","build_receipt.11":"💵 Subscription value: <b>{usd}</b> ({usd2} per day)","build_receipt.12":"✅ Subscription activated.","run_auto_vip_check.1":"🎁 Your monthly subscription is active!\nSince you've been with us for more than {AUTO_VIP_AFTER_DAYS} days, a {AUTO_VIP_DAYS}-day VIP subscription is activated for you automatically every {AUTO_VIP_DAYS} days.\n\n","run_auto_vip_check.2":"\n\nUntil {date} you can download with no volume limit 💎","job_allowed.1":"📦 Today's quota is used up. It resets at midnight; or download without limits with VIP (write «id» and give it to the admin).","job_allowed.2":"⏳ To prevent spam, wait {remaining} more seconds and try again.","link_markup.1":"🔗 Link","cleanup_after_send.1":"✅ Sent!","handle_quality_choice.1":"❌ Internal error, please send the link again.","handle_quality_choice.2":"⌛ This request has expired, please send the link again.","handle_quality_choice.3":"⏳ Preparing the download...","handle_quality_choice.4":"❌ The file is {size_mb:.1f}MB, which is above the {MAX_TELEGRAM_UPLOAD_MB}MB limit of official Telegram bots. For larger files you need to use a Local Bot API Server (see the README).","handle_quality_choice.5":"📤 Sending the file ({size_mb:.0f}MB)...\nLarge files may take a few minutes, please be patient.","handle_quality_choice.6":"❌ The download or upload failed:\n","deliver_song.1":"⚠️ The video is {video_size_mb:.1f}MB, which is above Telegram's {MAX_TELEGRAM_UPLOAD_MB}MB limit.","deliver_song.2":"📤 Sending the video ({video_size_mb:.0f}MB)...","deliver_song.3":"⬇️ Downloading the audio version of «{label}»...","deliver_song.4":"⚠️ The audio file is {audio_size_mb:.1f}MB, which is above Telegram's {MAX_TELEGRAM_UPLOAD_MB}MB limit.","deliver_song.5":"❌ Sending the song failed:\n","recognize_from_audio_file.1":"🎧 Identifying the song (first clip)...","recognize_from_audio_file.2":"🎧 Identifying the song (second clip)...","find_and_deliver_song.1":"🎵 Original song: {label}{variant_txt}\n🔎 Looking for the official version...","find_and_deliver_song.2":"\nArtist: {artist}","find_and_deliver_song.3":"❌ I couldn't find the official version of «{title}».{artist_txt}\nYou can try the «🎵 Music search» button with a more exact name.","handle_find_song.1":"🤖 Checking the video's caption with AI...","handle_find_song.2":" or get a free GEMINI_API_KEY from aistudio.google.com so I can guess from the video's caption","handle_find_song.3":"ℹ️ I couldn't identify the song from the video's metadata or caption. To identify it from the audio itself, an AUDD_API_KEY (or ACRCloud) must be configured{hint}.","handle_find_song.4":"⬇️ Downloading the video's audio for identification...","handle_find_song.5":"❌ Song identification failed:\n","handle_find_song.6":"❌ I couldn't identify this video's song. The background sound may not have been clear, or the song isn't in the recognition service's database.","handle_uploaded_media.1":"🎵 Extract audio","handle_uploaded_media.2":"🎧 Find the original song","handle_uploaded_media.3":"🎞 I got your file. What should I do?","download_uploaded_file.1":"❌ The file is {size_mb:.1f}MB and, with the official Telegram server, the bot can only receive files up to {TELEGRAM_CLOUD_DOWNLOAD_LIMIT_MB}MB. For larger files, TELEGRAM_API_ID and TELEGRAM_API_HASH must be set (local server).","download_uploaded_file.2":"⬇️ Receiving the file from Telegram...","download_uploaded_file.3":"❌ This file is too large for the bot to receive (the limit is 20 MB without a local server).","handle_upload_extract_audio.1":"⌛ This request has expired, please send the file again.","handle_upload_extract_audio.2":"🎶 Extracting the audio...","handle_upload_extract_audio.3":"❌ The audio file is {size_mb:.1f}MB, which is above the {MAX_TELEGRAM_UPLOAD_MB}MB limit.","handle_upload_extract_audio.4":"❌ Audio extraction failed:\n","handle_upload_find_song.1":"🤖 Checking the caption with AI...","handle_upload_find_song.2":"ℹ️ To identify a song from the file's audio, an AUDD_API_KEY (or ACRCloud) must be configured.","handle_upload_find_song.3":"❌ I couldn't identify this file's song. The background sound may not have been clear, or the song isn't in the recognition service's database.","btn.help":"📖 Help","btn.about":"ℹ️ About","btn.music":"🎵 Music search","btn.account":"👤 My account","vip_granted.user":"🎉 Your VIP subscription is active!\n\n{receipt}\n\nFrom now on, until {date}, you can download with no volume limit 💎","vip_cancelled.user":"ℹ️ Your VIP subscription was cancelled by the admin.\nFrom now on, the normal daily quota ({volume}) applies to you."},"tr":{"__init__.1":"🔒 Bu içerik, botun giriş yaptığı hesap tarafından görüntülenemiyor (Instagram bunu yalnızca belirli kitlelere veya yaş sınırıyla gösteriyor).\nÖzel bir gönderi ya da bozuk bir bağlantı değil, ancak bot bunu indiremiyor.","__init__.2":"🔒 Bu içerik Instagram'da yalnızca belirli kitleler için (örneğin yaş sınırıyla) görüntülenebiliyor ve botun yeniden denemek için giriş yapmış bir hesabı yok.\nÖzel bir gönderi ya da bozuk bir bağlantı değil, ancak bot bunu indiremiyor.","format_music_entry_label.1":"Adsız","build_music_page.1":"◀️ Önceki sayfa","build_music_page.2":"▶️ Sonraki sayfa","build_music_page.3":"🔍 YouTube'da ara","render.1":"⚙️ Son işlemler yapılıyor (dosya dönüştürme/birleştirme)...","render.2":"?","render.3":"  •  {n}sn kaldı","render.4":"⬇️ İndiriliyor...\n[{bar}] {percent:.0f}%\n{line2}","pick_quality_options.1":" (yaklaşık)","pick_quality_options.2":"🎬 Varsayılan kalite{size_txt}","pick_quality_options.3":"🎵 Sadece ses (MP3)","start.1":"Selam dostum 🧘🏾🌚\nBen senin indirme botunum 🧸\n\nBenimle hemen her platformdan video, müzik ve gönderileri sınırsız indirebilirsin 🧘🏾✨️\n\nAyrıca istediğin müziği, gönderiyi ve videoyu benimle bulup indirebilirsin 🧘🏾🎧\n\nSadece istediğin gönderinin bağlantısını bana göndermen yeterli 🧸","help_button.1":"📖 Botun tam rehberi\n\n🔗 Bağlantıdan indirme (Instagram / YouTube / TikTok)\n1. Bana bir Instagram gönderisi, reels ya da hikaye, bir YouTube videosu veya shorts, ya da bir TikTok videosunun bağlantısını gönder\n2. Sana küçük resmi ve açıklamayı gösteririm\n3. Altında birkaç kalite düğmesi görürsün, her biri dosyanın tam boyutuyla (örneğin “1080p - 24.3MB”); istediğini seç\n4. Videonun sadece sesini istiyorsan (videonun kendisini değil), «🎵 Sadece ses» düğmesine bas\n5. İndirirken canlı bir ilerleme çubuğu görürsün (yüzde, toplamdan indirilen boyut, hız); sonunda dosya sana gönderilir\n\n🎵 Müzik arama («{BTN_MUSIC}» düğmesi)\n1. Düğmeye bas ve bir şarkı adı ya da sadece sanatçı adı gönder\n2. Önce YouTube Music'te (resmi katalog) ararım; sanatçı adı gönderirsen bilinen tüm şarkıları oradan gelir (yabancı sanatçılar için genelde eksiksizdir); sonuçlar 10'ar 10'ar «◀️ Önceki sayfa» / «▶️ Sonraki sayfa» düğmeleriyle sayfalanır\n3. Herhangi bir şarkıya dokunursan yukarıdaki kalite / sadece ses düğmeleri onun için de çıkar\n4. Hiçbir şey bulunamazsa ya da az çıkarsa (örneğin birçok Farsça şarkı YouTube Music'te yok), listenin altında «🔍 YouTube'da ara» düğmesi var; bas, aynı aramayı normal YouTube'da da yapayım\n\n📃 YouTube oynatma listeleri\nBir oynatma listesinin bağlantısını gönder (/playlist?list=... içeren); liste bilgisi ve videoları 5'er 5'er sayfalanır. Bir videoya dokunursan bilgisi ve görseli gelir, kaliteyi seçip indirebilirsin. «▶️ Sonraki sayfa» ve «◀️ Önceki sayfa» ile sayfalar arasında gez, sonunda düğmeleri kapatmak için «✅ İşim bitti» düğmesine bas (liste bilgisi kalır)\n\n🎞 Video dosyasının kendisini gönderme (bağlantı değil)\nKlibin/videonun kendisini gönderirsen (örneğin bir reels dosyası), iki düğmeyle “Ne yapayım?” diye sorarım: «🎵 Sesi çıkar» (videonun sesini mp3 yapıp gönderirim) veya «🎧 Orijinal şarkıyı bul» (videodaki şarkıyı tanır ve resmi sürümünü ses olarak gönderirim)\n• Videoya bir açıklama yazarsan tanıma daha doğru olur\n• 20 MB'dan büyük dosyaları almak yalnızca yerel Telegram sunucusuyla (TELEGRAM_API_ID/HASH) mümkün\n\n🎧 Bir videonun orijinal şarkısını bulma\nHer videonun kalite düğmelerinin altında bir de «🎵 Videonun orijinal şarkısını bul» düğmesi var. Ona basınca:\n1. Önce platformun (çoğunlukla TikTok) şarkı adını meta verisinde verip vermediğine bakarım\n2. Vermemişse videonun sesinden bir parça alıp şarkı tanıma servisiyle tanırım\n3. Resmi sürüm bulunduktan sonra hem videoyu hem ses sürümünü doğru ve temiz şarkı adıyla sana gönderirim (elle seçmeye gerek yok)\n4. Bulunamazsa, en azından sanatçının adını (tanınmışsa) söylerim ki kendin arayabilesin\n\n👤 Hesap, kota ve VIP\n• «👤 Hesabım» düğmesi: hesap bilgilerin, indirme sayın, bugün kullanılan hacim ve abonelik durumu\n• Her kullanıcı günde {volume} indirme hacmine sahiptir; bu her gece yarısı (İran saatiyle) sıfırlanır\n• «id» yazarak 8 haneli özel bir kimlik alırsın; VIP aboneliğini (birkaç gün sınırsız indirme) açması için bunu yöneticiye ver\n\n⚠️ Önemli notlar\n• Spam'i önlemek için her {REQUEST_COOLDOWN_SECONDS} saniyede yalnızca bir yeni istek gönderebilirsin; çok hızlı gönderirsen kaç saniye beklemen gerektiğini söylerim\n• Özel gönderiler veya kısıtlı videolar, çerez ayarı olmadan indirilemez\n• Lütfen beni yalnızca kendi içeriğiniz veya kullanma izniniz olan içerikler için kullanın; telif hakkına uymak kullanıcının sorumluluğundadır","about_button.1":"ℹ️ Bot hakkında:\n\nBu bot, yt-dlp yardımıyla Instagram, YouTube ve TikTok'tan indirme yapar.\nLütfen yalnızca kendi içeriğiniz veya kullanma izniniz olan içerikler için kullanın; telif haklarına ve platform kurallarına uymak size aittir.","music_button.1":"🎵 Şarkı adını ve sanatçıyı gönder (örneğin: «Shadmehr Aghili - Bahar»).\nResmi sürümünü bulup sana göndereceğim.","handle_music_query.1":"Aramam için bir şarkı ya da sanatçı adı gönder.","handle_music_query.2":"🔎 YouTube Music'te «{query_text}» aranıyor...","handle_music_query.3":"❌ Arama başarısız oldu:\n","handle_music_query.4":"YouTube Music'te «{query_text}» için bir şey bulunamadı. Aşağıdaki düğmeyle normal YouTube'da da arayabilirsin:","handle_music_query.5":"🎵 «{query_text}» için YouTube Music sonuçları ({count} sonuç) — sayfa 1 / {total_pages}:\nBirini seç, yetmezse aşağıdaki «YouTube'da ara» düğmesine bas:","handle_music_page.1":"⌛ Bu aramanın süresi doldu, lütfen müzik arama düğmesini tekrar kullan.","handle_music_page.2":"YouTube Music","handle_music_page.3":"YouTube","handle_music_page.4":"🎵 «{query}» için {source_label} sonuçları ({count} sonuç) — sayfa {page} / {total_pages}:\nBirini seç:","handle_music_youtube_fallback.1":"🔎 YouTube'da «{query_text}» aranıyor...","handle_music_youtube_fallback.2":"Normal YouTube'da da «{query_text}» için bir şey bulunamadı.","handle_music_youtube_fallback.3":"🎵 «{query_text}» için YouTube sonuçları ({count} sonuç) — sayfa 1 / {total_pages}:\nBirini seç:","handle_music_pick.1":"⏳ Bilgiler alınıyor...","show_quality_picker.1":"❌ Bu bağlantının bilgilerini alamadım. Gönderi özel olabilir ya da bağlantı yanlış olabilir.\nHata ayrıntısı: ","show_quality_picker.2":"\nİstediğin kaliteyi seç:","show_quality_picker.3":"İstediğin kaliteyi seç:","show_quality_picker.4":"…\n\nİstediğin kaliteyi seç:","show_quality_picker.5":"🎵 Videonun orijinal şarkısını bul","playlist_info_text.1":"🎞 {count} video","playlist_info_text.2":" ({skipped} özel/silinmiş öğe atlandı)","playlist_page_text.1":"{playlist_info_text}\n\nSayfa {page} / {playlist_total_pages} — birini seç:","build_playlist_markup.1":"✅ İşim bitti","show_playlist.1":"❌ Oynatma listesini okuyamadım. Özel olabilir ya da bağlantı yanlış olabilir.\nHata ayrıntısı: ","show_playlist.2":"Bu oynatma listesinde erişilebilir video yok (boş ya da tüm videoları özel/silinmiş).","show_playlist.3":"Oynatma listesi","handle_playlist_page.1":"⌛ Bu oynatma listesinin süresi doldu, lütfen bağlantısını tekrar gönder.","handle_playlist_pick.1":"⏳ Video bilgileri alınıyor...","handle_playlist_done.1":"{base}\n\n✅ İşim bitti.","handle_message.1":"⏳ Oynatma listesi okunuyor...","handle_message.2":"Bu geçerli bir bağlantı değil. Bir Instagram gönderisi/reels/hikaye, bir YouTube videosu/shorts ya da TikTok videosu bağlantısı gönder, veya şarkı aramak için «{BTN_MUSIC}» düğmesini kullan.","handle_message.3":"⏳ Bağlantı bilgileri alınıyor...","warn_slow_down.1":"⏳ Spam'i önlemek için her {REQUEST_COOLDOWN_SECONDS} saniyede yalnızca bir yeni istek gönderebilirsin.\nLütfen {remaining} saniye sonra tekrar gönder.","quota_problem.1":"📦 Bugünkü kotan bu dosya için yetmiyor.\nDosya boyutu: {volume}\nBugün kalan: {volume3} üzerinden {volume2}\n\nKota her gece yarısı (İran saatiyle) sıfırlanır. Daha düşük bir kalite seçebilir veya VIP aboneliğiyle sınırsız indirebilirsin («id» yazıp 8 haneli kimliğini al ve yöneticiye ver).","account_button.1":"👤 <b>Hesabım</b>","account_button.2":"🏷 Ad: {text}","account_button.3":"🔖 Kullanıcı adı: {text}","account_button.4":"🔢 Telegram kimliği: <code>{user_id}</code>","account_button.5":"🆔 Özel kimlik: <code>{public_id}</code>","account_button.6":"📅 Üyelik tarihi: {date}","account_button.7":"⬇️ İndirme sayısı: {downloads_total}","account_button.8":"👑 Durum: Yönetici (sınırsız)","account_button.9":"💎 Abonelik: {date} tarihine kadar VIP aktif ({days_left} gün kaldı)","account_button.10":"💎 Abonelik: yok","account_button.11":"📦 Bugünkü kullanım: {volume} (sınırsız ♾)","account_button.12":"📦 Bugünkü kullanım: {volume} / {volume2} ({volume3} kaldı)","id_command.1":"🆔 Özel kimliğin: <code>{public_id}</code>\n🔢 Telegram kimliği: <code>{user_id}</code>\n\nÖzel kimlik hesabının kimliğidir; VIP aboneliğini açtırmak için bu 8 haneli sayıyı yöneticiye ver.","build_receipt.1":"🧾 <b>VIP abonelik etkinleştirme makbuzu</b>","build_receipt.2":"Makbuz no: <code>#{receipt_id:05d}</code>","build_receipt.3":"👤 Kullanıcı: {text} {text2}","build_receipt.4":"🆔 Kimlik: <code>{public_id}</code>","build_receipt.5":"⏳ Süre: {days} gün","build_receipt.6":" (uzatma)","build_receipt.7":"🎁 Tür: otomatik aylık abonelik ({AUTO_VIP_AFTER_DAYS} gün üyelikten sonra)","build_receipt.8":"👑 Tür: yönetici tarafından etkinleştirildi","build_receipt.9":"📅 Başlangıç: {date}","build_receipt.10":"📅 Bitiş: {date}","build_receipt.11":"💵 Abonelik değeri: <b>{usd}</b> (günlük {usd2})","build_receipt.12":"✅ Abonelik etkinleştirildi.","run_auto_vip_check.1":"🎁 Aylık aboneliğin etkinleşti!\n{AUTO_VIP_AFTER_DAYS} günden fazladır bizimle olduğun için, her {AUTO_VIP_DAYS} günde bir senin için {AUTO_VIP_DAYS} günlük VIP abonelik otomatik olarak etkinleştirilir.\n\n","run_auto_vip_check.2":"\n\n{date} tarihine kadar hacim sınırı olmadan indirebilirsin 💎","job_allowed.1":"📦 Bugünkü kotan bitti. Gece yarısı sıfırlanır; ya da VIP ile sınırsız indir («id» yaz ve yöneticiye ver).","job_allowed.2":"⏳ Spam'i önlemek için {remaining} saniye daha bekle ve tekrar dene.","link_markup.1":"🔗 Bağlantı","cleanup_after_send.1":"✅ Gönderildi!","handle_quality_choice.1":"❌ Dahili hata, lütfen bağlantıyı tekrar gönder.","handle_quality_choice.2":"⌛ Bu isteğin süresi doldu, lütfen bağlantıyı tekrar gönder.","handle_quality_choice.3":"⏳ İndirme hazırlanıyor...","handle_quality_choice.4":"❌ Dosya {size_mb:.1f}MB ve resmi Telegram botlarının {MAX_TELEGRAM_UPLOAD_MB}MB sınırını aşıyor. Daha büyük dosyalar için bir Yerel Bot API Sunucusu kullanman gerekir (README'ye bak).","handle_quality_choice.5":"📤 Dosya gönderiliyor ({size_mb:.0f}MB)...\nBüyük dosyalar birkaç dakika sürebilir, lütfen sabırlı ol.","handle_quality_choice.6":"❌ İndirme veya gönderme başarısız oldu:\n","deliver_song.1":"⚠️ Videonun boyutu {video_size_mb:.1f}MB ve Telegram'ın {MAX_TELEGRAM_UPLOAD_MB}MB sınırını aşıyor.","deliver_song.2":"📤 Video gönderiliyor ({video_size_mb:.0f}MB)...","deliver_song.3":"⬇️ «{label}» şarkısının ses sürümü indiriliyor...","deliver_song.4":"⚠️ Ses dosyasının boyutu {audio_size_mb:.1f}MB ve Telegram'ın {MAX_TELEGRAM_UPLOAD_MB}MB sınırını aşıyor.","deliver_song.5":"❌ Şarkı gönderilemedi:\n","recognize_from_audio_file.1":"🎧 Şarkı tanınıyor (ilk parça)...","recognize_from_audio_file.2":"🎧 Şarkı tanınıyor (ikinci parça)...","find_and_deliver_song.1":"🎵 Orijinal şarkı: {label}{variant_txt}\n🔎 Resmi sürüm aranıyor...","find_and_deliver_song.2":"\nSanatçı: {artist}","find_and_deliver_song.3":"❌ «{title}» şarkısının resmi sürümünü bulamadım.{artist_txt}\nDaha doğru bir adla «🎵 Müzik ara» düğmesini deneyebilirsin.","handle_find_song.1":"🤖 Videonun açıklaması yapay zekâ ile inceleniyor...","handle_find_song.2":" ya da aistudio.google.com'dan ücretsiz bir GEMINI_API_KEY al, böylece videonun açıklamasından tahmin edebileyim","handle_find_song.3":"ℹ️ Şarkıyı videonun meta verisinden veya açıklamasından tanıyamadım. Sesin kendisinden tanımak için bir AUDD_API_KEY (veya ACRCloud) ayarlanmalı{hint}.","handle_find_song.4":"⬇️ Tanıma için videonun sesi indiriliyor...","handle_find_song.5":"❌ Şarkı tanıma başarısız oldu:\n","handle_find_song.6":"❌ Bu videonun şarkısını tanıyamadım. Arka plan sesi net olmayabilir veya şarkı tanıma servisinin veritabanında yok.","handle_uploaded_media.1":"🎵 Sesi çıkar","handle_uploaded_media.2":"🎧 Orijinal şarkıyı bul","handle_uploaded_media.3":"🎞 Dosyanı aldım. Ne yapayım?","download_uploaded_file.1":"❌ Dosya {size_mb:.1f}MB ve resmi Telegram sunucusuyla bot yalnızca {TELEGRAM_CLOUD_DOWNLOAD_LIMIT_MB}MB'a kadar dosya alabiliyor. Daha büyük dosyalar için TELEGRAM_API_ID ve TELEGRAM_API_HASH ayarlanmalı (yerel sunucu).","download_uploaded_file.2":"⬇️ Dosya Telegram'dan alınıyor...","download_uploaded_file.3":"❌ Bu dosya botun alması için çok büyük (yerel sunucu olmadan sınır 20 MB).","handle_upload_extract_audio.1":"⌛ Bu isteğin süresi doldu, lütfen dosyayı tekrar gönder.","handle_upload_extract_audio.2":"🎶 Ses çıkarılıyor...","handle_upload_extract_audio.3":"❌ Ses dosyası {size_mb:.1f}MB ve {MAX_TELEGRAM_UPLOAD_MB}MB sınırını aşıyor.","handle_upload_extract_audio.4":"❌ Ses çıkarma başarısız oldu:\n","handle_upload_find_song.1":"🤖 Açıklama yapay zekâ ile inceleniyor...","handle_upload_find_song.2":"ℹ️ Dosyanın sesinden şarkı tanımak için bir AUDD_API_KEY (veya ACRCloud) ayarlanmalı.","handle_upload_find_song.3":"❌ Bu dosyanın şarkısını tanıyamadım. Arka plan sesi net olmayabilir veya şarkı tanıma servisinin veritabanında yok.","btn.help":"📖 Yardım","btn.about":"ℹ️ Hakkında","btn.music":"🎵 Müzik ara","btn.account":"👤 Hesabım","vip_granted.user":"🎉 VIP aboneliğin etkinleşti!\n\n{receipt}\n\nBundan sonra {date} tarihine kadar hacim sınırı olmadan indirebilirsin 💎","vip_cancelled.user":"ℹ️ VIP aboneliğin yönetici tarafından iptal edildi.\nBundan sonra normal günlük kotan ({volume}) geçerli."},"ru":{"__init__.1":"🔒 Этот контент недоступен для аккаунта, под которым вошёл бот (Instagram показывает его только определённой аудитории или с возрастным ограничением).\nЭто не приватный пост и не битая ссылка, но бот не может его скачать.","__init__.2":"🔒 Этот контент в Instagram виден только определённой аудитории (например, с возрастным ограничением), а у бота нет авторизованного аккаунта для повторной попытки.\nЭто не приватный пост и не битая ссылка, но бот не может его скачать.","format_music_entry_label.1":"Без названия","build_music_page.1":"◀️ Предыдущая страница","build_music_page.2":"▶️ Следующая страница","build_music_page.3":"🔍 Искать на YouTube","render.1":"⚙️ Финальная обработка (конвертация/объединение файла)...","render.2":"?","render.3":"  •  осталось {n} с","render.4":"⬇️ Скачивание...\n[{bar}] {percent:.0f}%\n{line2}","pick_quality_options.1":" (примерно)","pick_quality_options.2":"🎬 Качество по умолчанию{size_txt}","pick_quality_options.3":"🎵 Только звук (MP3)","start.1":"Привет, друг 🧘🏾🌚\nЯ твой бот-загрузчик 🧸\n\nСо мной ты можешь без ограничений скачивать видео, музыку и посты почти с любой платформы 🧘🏾✨️\n\nА ещё через меня можно найти и скачать нужную музыку, пост или видео 🧘🏾🎧\n\nПросто отправь мне ссылку на нужный пост 🧸","help_button.1":"📖 Полное руководство по боту\n\n🔗 Скачивание по ссылке (Instagram / YouTube / TikTok)\n1. Отправь мне ссылку на пост, рилс или сторис Instagram, видео или шортс YouTube, либо видео TikTok\n2. Я покажу тебе миниатюру и описание\n3. Под ними появится несколько кнопок качества, каждая с точным размером файла (например, «1080p - 24.3MB»); выбери любую\n4. Если тебе нужен только звук видео (а не само видео), нажми «🎵 Только звук»\n5. Во время загрузки ты увидишь живой индикатор прогресса (проценты, скачанный объём из общего, скорость); в конце файл будет отправлен тебе\n\n🎵 Поиск музыки (кнопка «{BTN_MUSIC}»)\n1. Нажми на кнопку и отправь название песни или просто имя исполнителя\n2. Сначала я ищу в YouTube Music (официальный каталог); если отправишь имя исполнителя, все его известные песни придут оттуда (для зарубежных исполнителей обычно полностью); результаты разбиты по 10 на страницу с кнопками «◀️ Предыдущая страница» / «▶️ Следующая страница»\n3. Нажми на любую песню, и для неё появятся те же кнопки качества / только звук\n4. Если ничего не найдено или результатов мало (например, многих персидских песен нет в YouTube Music), под списком есть кнопка «🔍 Искать на YouTube»; нажми, и я выполню тот же поиск на обычном YouTube\n\n📃 Плейлисты YouTube\nОтправь ссылку на плейлист (содержащую /playlist?list=...); информация о плейлисте и его видео разбиваются по 5 на страницу. Нажми на видео, и появятся его информация и картинка, а ты сможешь выбрать качество и скачать. Листай страницы кнопками «▶️ Следующая страница» и «◀️ Предыдущая страница», а в конце нажми «✅ Я закончил», чтобы убрать кнопки (информация о плейлисте останется)\n\n🎞 Отправка самого видеофайла (не ссылки)\nЕсли ты отправишь сам клип/видео (например, файл рилса), я спрошу «Что делать?» с двумя кнопками: «🎵 Извлечь звук» (превращу звук видео в mp3 и отправлю) или «🎧 Найти оригинальную песню» (распознаю песню из видео и отправлю её официальную версию аудиофайлом)\n• Если напишешь подпись к видео, распознавание будет точнее\n• Приём файлов больше 20 МБ возможен только с локальным сервером Telegram (TELEGRAM_API_ID/HASH)\n\n🎧 Поиск оригинальной песни из видео\nПод кнопками качества каждого видео есть ещё кнопка «🎵 Найти оригинальную песню видео». После нажатия:\n1. Сначала я проверяю, указала ли сама платформа (чаще всего TikTok) название песни в метаданных\n2. Если нет, я беру фрагмент звука из видео и распознаю его сервисом распознавания музыки\n3. Найдя официальную версию, я сам отправлю тебе и видео, и его аудиоверсию с правильным чистым названием песни (выбирать вручную не нужно)\n4. Если не найдено, я хотя бы назову имя исполнителя (если он распознан), чтобы ты поискал сам\n\n👤 Аккаунт, квота и VIP\n• Кнопка «👤 Мой аккаунт»: данные аккаунта, число загрузок, объём, использованный сегодня, и статус подписки\n• Каждый пользователь может скачивать до {volume} в день; квота сбрасывается каждую ночь в полночь (по времени Ирана)\n• Написав «id», ты получишь уникальный 8-значный идентификатор; передай его администратору, чтобы он активировал VIP-подписку (безлимитные загрузки на несколько дней)\n\n⚠️ Важные замечания\n• Для защиты от спама можно отправлять только один новый запрос раз в {REQUEST_COOLDOWN_SECONDS} секунд; если отправишь слишком быстро, я скажу, сколько секунд подождать\n• Приватные посты и видео с ограничениями нельзя скачать без настройки cookie\n• Пожалуйста, используйте меня только для своего контента или контента, который вам разрешено использовать; ответственность за соблюдение авторских прав лежит на пользователе","about_button.1":"ℹ️ О боте:\n\nЭтот бот скачивает из Instagram, YouTube и TikTok с помощью yt-dlp.\nПожалуйста, используйте его только для своего контента или контента, который вам разрешено использовать; соблюдение авторских прав и правил платформ лежит на вас.","music_button.1":"🎵 Отправь название песни и исполнителя (например: «Shadmehr Aghili - Bahar»).\nЯ найду официальную версию и пришлю её тебе.","handle_music_query.1":"Отправь название песни или имя исполнителя, и я поищу.","handle_music_query.2":"🔎 Ищу «{query_text}» в YouTube Music...","handle_music_query.3":"❌ Поиск завершился ошибкой:\n","handle_music_query.4":"В YouTube Music по запросу «{query_text}» ничего не найдено. Можно поискать и на обычном YouTube кнопкой ниже:","handle_music_query.5":"🎵 Результаты YouTube Music по запросу «{query_text}» ({count} шт.) — страница 1 из {total_pages}:\nВыбери один или, если этого мало, нажми ниже «Искать на YouTube»:","handle_music_page.1":"⌛ Этот поиск устарел, снова воспользуйся кнопкой поиска музыки.","handle_music_page.2":"YouTube Music","handle_music_page.3":"YouTube","handle_music_page.4":"🎵 Результаты {source_label} по запросу «{query}» ({count} шт.) — страница {page} из {total_pages}:\nВыбери один:","handle_music_youtube_fallback.1":"🔎 Ищу «{query_text}» на YouTube...","handle_music_youtube_fallback.2":"На обычном YouTube по запросу «{query_text}» тоже ничего не найдено.","handle_music_youtube_fallback.3":"🎵 Результаты YouTube по запросу «{query_text}» ({count} шт.) — страница 1 из {total_pages}:\nВыбери один:","handle_music_pick.1":"⏳ Получаю информацию...","show_quality_picker.1":"❌ Не удалось получить информацию по этой ссылке. Возможно, пост приватный или ссылка неверная.\nПодробности ошибки: ","show_quality_picker.2":"\nВыбери нужное качество:","show_quality_picker.3":"Выбери нужное качество:","show_quality_picker.4":"…\n\nВыбери нужное качество:","show_quality_picker.5":"🎵 Найти оригинальную песню видео","playlist_info_text.1":"🎞 Видео: {count}","playlist_info_text.2":" (пропущено приватных/удалённых: {skipped})","playlist_page_text.1":"{playlist_info_text}\n\nСтраница {page} из {playlist_total_pages} — выбери одно:","build_playlist_markup.1":"✅ Я закончил","show_playlist.1":"❌ Не удалось прочитать плейлист. Возможно, он приватный или ссылка неверная.\nПодробности ошибки: ","show_playlist.2":"В этом плейлисте нет доступных видео (он пуст или все видео приватные/удалены).","show_playlist.3":"Плейлист","handle_playlist_page.1":"⌛ Этот плейлист устарел, отправь ссылку на него снова.","handle_playlist_pick.1":"⏳ Получаю информацию о видео...","handle_playlist_done.1":"{base}\n\n✅ Готово.","handle_message.1":"⏳ Читаю плейлист...","handle_message.2":"Это недействительная ссылка. Отправь ссылку на пост/рилс/сторис Instagram, видео/шортс YouTube или видео TikTok, либо используй кнопку «{BTN_MUSIC}» для поиска песни.","handle_message.3":"⏳ Получаю информацию по ссылке...","warn_slow_down.1":"⏳ Для защиты от спама можно отправлять только один новый запрос раз в {REQUEST_COOLDOWN_SECONDS} секунд.\nПожалуйста, отправь снова через {remaining} с.","quota_problem.1":"📦 Сегодняшней квоты не хватает для этого файла.\nРазмер файла: {volume}\nОсталось сегодня: {volume2} из {volume3}\n\nКвота сбрасывается каждую ночь в полночь (по времени Ирана). Можно выбрать качество пониже или скачивать без ограничений с VIP-подпиской (напиши «id», получи свой 8-значный идентификатор и передай его администратору).","account_button.1":"👤 <b>Мой аккаунт</b>","account_button.2":"🏷 Имя: {text}","account_button.3":"🔖 Имя пользователя: {text}","account_button.4":"🔢 Telegram ID: <code>{user_id}</code>","account_button.5":"🆔 Уникальный ID: <code>{public_id}</code>","account_button.6":"📅 Дата регистрации: {date}","account_button.7":"⬇️ Количество загрузок: {downloads_total}","account_button.8":"👑 Статус: администратор (без ограничений)","account_button.9":"💎 Подписка: VIP активна до {date} (осталось дней: {days_left})","account_button.10":"💎 Подписка: нет","account_button.11":"📦 Использовано сегодня: {volume} (безлимит ♾)","account_button.12":"📦 Использовано сегодня: {volume} из {volume2} (осталось {volume3})","id_command.1":"🆔 Твой уникальный ID: <code>{public_id}</code>\n🔢 Telegram ID: <code>{user_id}</code>\n\nУникальный ID — это идентификатор твоего аккаунта; передай этот 8-значный номер администратору для активации VIP-подписки.","build_receipt.1":"🧾 <b>Квитанция об активации VIP-подписки</b>","build_receipt.2":"Номер квитанции: <code>#{receipt_id:05d}</code>","build_receipt.3":"👤 Пользователь: {text} {text2}","build_receipt.4":"🆔 ID: <code>{public_id}</code>","build_receipt.5":"⏳ Срок: {days} дн.","build_receipt.6":" (продление)","build_receipt.7":"🎁 Тип: автоматическая ежемесячная подписка (после {AUTO_VIP_AFTER_DAYS} дн. членства)","build_receipt.8":"👑 Тип: активирована администратором","build_receipt.9":"📅 Начало: {date}","build_receipt.10":"📅 Окончание: {date}","build_receipt.11":"💵 Стоимость подписки: <b>{usd}</b> ({usd2} в день)","build_receipt.12":"✅ Подписка активирована.","run_auto_vip_check.1":"🎁 Твоя ежемесячная подписка активирована!\nПоскольку ты с нами больше {AUTO_VIP_AFTER_DAYS} дн., каждые {AUTO_VIP_DAYS} дн. тебе автоматически активируется VIP-подписка на {AUTO_VIP_DAYS} дн.\n\n","run_auto_vip_check.2":"\n\nДо {date} ты можешь скачивать без ограничения объёма 💎","job_allowed.1":"📦 Сегодняшняя квота исчерпана. Она сбрасывается в полночь; или скачивай без ограничений с VIP (напиши «id» и передай администратору).","job_allowed.2":"⏳ Для защиты от спама подожди ещё {remaining} с и попробуй снова.","link_markup.1":"🔗 Ссылка","cleanup_after_send.1":"✅ Отправлено!","handle_quality_choice.1":"❌ Внутренняя ошибка, отправь ссылку снова.","handle_quality_choice.2":"⌛ Этот запрос устарел, отправь ссылку снова.","handle_quality_choice.3":"⏳ Готовлю загрузку...","handle_quality_choice.4":"❌ Размер файла {size_mb:.1f}MB превышает лимит {MAX_TELEGRAM_UPLOAD_MB}MB для официальных ботов Telegram. Для файлов большего размера нужно использовать локальный сервер Bot API (см. README).","handle_quality_choice.5":"📤 Отправляю файл ({size_mb:.0f}MB)...\nБольшие файлы могут отправляться несколько минут, наберись терпения.","handle_quality_choice.6":"❌ Не удалось скачать или отправить файл:\n","deliver_song.1":"⚠️ Размер видео {video_size_mb:.1f}MB превышает лимит Telegram в {MAX_TELEGRAM_UPLOAD_MB}MB.","deliver_song.2":"📤 Отправляю видео ({video_size_mb:.0f}MB)...","deliver_song.3":"⬇️ Скачиваю аудиоверсию «{label}»...","deliver_song.4":"⚠️ Размер аудиофайла {audio_size_mb:.1f}MB превышает лимит Telegram в {MAX_TELEGRAM_UPLOAD_MB}MB.","deliver_song.5":"❌ Не удалось отправить песню:\n","recognize_from_audio_file.1":"🎧 Распознаю песню (первый фрагмент)...","recognize_from_audio_file.2":"🎧 Распознаю песню (второй фрагмент)...","find_and_deliver_song.1":"🎵 Оригинальная песня: {label}{variant_txt}\n🔎 Ищу официальную версию...","find_and_deliver_song.2":"\nИсполнитель: {artist}","find_and_deliver_song.3":"❌ Не нашёл официальную версию «{title}».{artist_txt}\nМожешь попробовать кнопку «🎵 Поиск музыки» с более точным названием.","handle_find_song.1":"🤖 Анализирую подпись к видео с помощью ИИ...","handle_find_song.2":" или получи бесплатный GEMINI_API_KEY на aistudio.google.com, чтобы я мог угадывать по подписи к видео","handle_find_song.3":"ℹ️ Не удалось определить песню по метаданным или подписи видео. Чтобы распознавать по самому звуку, нужно настроить AUDD_API_KEY (или ACRCloud){hint}.","handle_find_song.4":"⬇️ Скачиваю звук видео для распознавания...","handle_find_song.5":"❌ Не удалось распознать песню:\n","handle_find_song.6":"❌ Не удалось распознать песню из этого видео. Возможно, фоновый звук был нечётким или песни нет в базе сервиса распознавания.","handle_uploaded_media.1":"🎵 Извлечь звук","handle_uploaded_media.2":"🎧 Найти оригинальную песню","handle_uploaded_media.3":"🎞 Я получил твой файл. Что сделать?","download_uploaded_file.1":"❌ Размер файла {size_mb:.1f}MB, а через официальный сервер Telegram бот может принимать файлы только до {TELEGRAM_CLOUD_DOWNLOAD_LIMIT_MB}MB. Для файлов большего размера нужно задать TELEGRAM_API_ID и TELEGRAM_API_HASH (локальный сервер).","download_uploaded_file.2":"⬇️ Получаю файл из Telegram...","download_uploaded_file.3":"❌ Этот файл слишком большой для получения ботом (лимит 20 МБ без локального сервера).","handle_upload_extract_audio.1":"⌛ Этот запрос устарел, отправь файл снова.","handle_upload_extract_audio.2":"🎶 Извлекаю звук...","handle_upload_extract_audio.3":"❌ Размер аудиофайла {size_mb:.1f}MB превышает лимит {MAX_TELEGRAM_UPLOAD_MB}MB.","handle_upload_extract_audio.4":"❌ Не удалось извлечь звук:\n","handle_upload_find_song.1":"🤖 Анализирую подпись с помощью ИИ...","handle_upload_find_song.2":"ℹ️ Чтобы распознавать песню по звуку файла, нужно настроить AUDD_API_KEY (или ACRCloud).","handle_upload_find_song.3":"❌ Не удалось распознать песню из этого файла. Возможно, фоновый звук был нечётким или песни нет в базе сервиса распознавания.","btn.help":"📖 Помощь","btn.about":"ℹ️ О боте","btn.music":"🎵 Поиск музыки","btn.account":"👤 Мой аккаунт","vip_granted.user":"🎉 Твоя VIP-подписка активирована!\n\n{receipt}\n\nТеперь до {date} ты можешь скачивать без ограничения объёма 💎","vip_cancelled.user":"ℹ️ Твоя VIP-подписка была отменена администратором.\nТеперь действует обычная дневная квота ({volume})."},"es":{"__init__.1":"🔒 Este contenido no es visible para la cuenta con la que el bot ha iniciado sesión (Instagram solo lo muestra a ciertas audiencias o con restricción de edad).\nNo es una publicación privada ni un enlace roto, pero el bot no puede descargarlo.","__init__.2":"🔒 Este contenido solo es visible en Instagram para ciertas audiencias (por ejemplo, con restricción de edad) y el bot no tiene una cuenta con sesión iniciada para volver a intentarlo.\nNo es una publicación privada ni un enlace roto, pero el bot no puede descargarlo.","format_music_entry_label.1":"Sin título","build_music_page.1":"◀️ Página anterior","build_music_page.2":"▶️ Página siguiente","build_music_page.3":"🔍 Buscar en YouTube","render.1":"⚙️ Procesamiento final (convirtiendo/uniendo el archivo)...","render.2":"?","render.3":"  •  quedan {n}s","render.4":"⬇️ Descargando...\n[{bar}] {percent:.0f}%\n{line2}","pick_quality_options.1":" (aprox.)","pick_quality_options.2":"🎬 Calidad predeterminada{size_txt}","pick_quality_options.3":"🎵 Solo audio (MP3)","start.1":"Hola, amigo 🧘🏾🌚\nSoy tu bot descargador 🧸\n\nConmigo puedes descargar vídeos, música y publicaciones de casi cualquier plataforma, sin límites 🧘🏾✨️\n\nTambién puedes encontrar y descargar la música, publicaciones y vídeos que quieras a través de mí 🧘🏾🎧\n\nSolo envíame el enlace de la publicación que quieras 🧸","help_button.1":"📖 Guía completa del bot\n\n🔗 Descargar desde un enlace (Instagram / YouTube / TikTok)\n1. Envíame el enlace de una publicación, reel o historia de Instagram, un vídeo o short de YouTube, o un vídeo de TikTok\n2. Te muestro la miniatura y la descripción\n3. Debajo verás varios botones de calidad, cada uno con el tamaño exacto del archivo (p. ej. «1080p - 24.3MB»); elige el que quieras\n4. Si solo quieres el audio del vídeo (no el vídeo), pulsa «🎵 Solo audio»\n5. Mientras descarga verás una barra de progreso en vivo (porcentaje, tamaño descargado del total, velocidad); al final se te envía el archivo\n\n🎵 Búsqueda de música (botón «{BTN_MUSIC}»)\n1. Pulsa el botón y envía el nombre de una canción o solo el nombre de un artista\n2. Primero busco en YouTube Music (el catálogo oficial); si envías el nombre de un artista, todas sus canciones conocidas vienen de ahí (para artistas internacionales suele estar completo); los resultados se paginan de 10 en 10 con los botones «◀️ Página anterior» / «▶️ Página siguiente»\n3. Si pulsas en cualquier canción, le salen los mismos botones de calidad / solo audio\n4. Si no se encuentra nada o hay pocos resultados (por ejemplo, muchas canciones persas no están en YouTube Music), debajo de la lista hay un botón «🔍 Buscar en YouTube»; púlsalo y haré la misma búsqueda en YouTube normal\n\n📃 Listas de reproducción de YouTube\nEnvía el enlace de una lista de reproducción (el que contiene /playlist?list=...); la información de la lista y sus vídeos se paginan de 5 en 5. Si pulsas un vídeo, aparecen su información y su imagen, y puedes elegir la calidad y descargarlo. Usa «▶️ Página siguiente» y «◀️ Página anterior» para moverte entre páginas y al final pulsa «✅ Terminé» para cerrar los botones (la información de la lista se mantiene)\n\n🎞 Enviar el archivo de vídeo (no un enlace)\nSi me envías el propio clip/vídeo (por ejemplo, el archivo de un reel), te pregunto «¿Qué hago?» con dos botones: «🎵 Extraer audio» (convierto el sonido del vídeo a mp3 y te lo envío) o «🎧 Encontrar la canción original» (reconozco la canción del vídeo y te envío su versión oficial como audio)\n• Si escribes un pie de foto para el vídeo, el reconocimiento será más preciso\n• Recibir archivos de más de 20 MB solo es posible con un servidor local de Telegram (TELEGRAM_API_ID/HASH)\n\n🎧 Encontrar la canción original de un vídeo\nDebajo de los botones de calidad de cada vídeo hay también un botón «🎵 Encontrar la canción original del vídeo». Al pulsarlo:\n1. Primero compruebo si la propia plataforma (sobre todo TikTok) da el nombre de la canción en sus metadatos\n2. Si no, tomo un fragmento del audio del vídeo y lo identifico con un servicio de reconocimiento de música\n3. Una vez encontrada la versión oficial, te envío yo mismo tanto el vídeo como su versión de audio con el nombre correcto y limpio de la canción (no hace falta elegir a mano)\n4. Si no se encuentra, al menos te diré el nombre del artista (si se identificó) para que busques tú\n\n👤 Cuenta, cuota y VIP\n• El botón «👤 Mi cuenta»: los datos de tu cuenta, el número de descargas, el volumen usado hoy y el estado de la suscripción\n• Cada usuario puede descargar hasta {volume} al día, y se reinicia cada noche a medianoche (hora de Irán)\n• Escribiendo «id» obtienes un ID único de 8 dígitos; entrégaselo al administrador para activar una suscripción VIP (descargas ilimitadas durante algunos días)\n\n⚠️ Notas importantes\n• Para evitar el spam, solo puedes enviar una solicitud nueva cada {REQUEST_COOLDOWN_SECONDS} segundos; si envías demasiado rápido, te digo cuántos segundos esperar\n• Las publicaciones privadas o los vídeos restringidos no se pueden descargar sin configurar cookies\n• Usa el bot solo para tu propio contenido o contenido que tengas permiso de usar; el cumplimiento de los derechos de autor es responsabilidad del usuario","about_button.1":"ℹ️ Acerca del bot:\n\nEste bot descarga de Instagram, YouTube y TikTok con ayuda de yt-dlp.\nÚsalo solo para tu propio contenido o contenido que tengas permiso de usar; cumplir los derechos de autor y las normas de las plataformas es responsabilidad tuya.","music_button.1":"🎵 Envía el nombre de la canción y del artista (por ejemplo: «Shadmehr Aghili - Bahar»).\nEncontraré la versión oficial y te la enviaré.","handle_music_query.1":"Envía el nombre de una canción o de un artista y lo busco.","handle_music_query.2":"🔎 Buscando «{query_text}» en YouTube Music...","handle_music_query.3":"❌ La búsqueda falló:\n","handle_music_query.4":"No se encontró nada en YouTube Music para «{query_text}». También puedes buscar en YouTube normal con el botón de abajo:","handle_music_query.5":"🎵 Resultados de YouTube Music para «{query_text}» ({count} elementos) — página 1 de {total_pages}:\nElige uno o, si no es suficiente, pulsa abajo «Buscar en YouTube»:","handle_music_page.1":"⌛ Esta búsqueda ha caducado, usa de nuevo el botón de búsqueda de música.","handle_music_page.2":"YouTube Music","handle_music_page.3":"YouTube","handle_music_page.4":"🎵 Resultados de {source_label} para «{query}» ({count} elementos) — página {page} de {total_pages}:\nElige uno:","handle_music_youtube_fallback.1":"🔎 Buscando «{query_text}» en YouTube...","handle_music_youtube_fallback.2":"Tampoco se encontró nada en YouTube normal para «{query_text}».","handle_music_youtube_fallback.3":"🎵 Resultados de YouTube para «{query_text}» ({count} elementos) — página 1 de {total_pages}:\nElige uno:","handle_music_pick.1":"⏳ Obteniendo la información...","show_quality_picker.1":"❌ No pude obtener la información de este enlace. La publicación puede ser privada o el enlace puede ser incorrecto.\nDetalles del error: ","show_quality_picker.2":"\nElige la calidad que quieras:","show_quality_picker.3":"Elige la calidad que quieras:","show_quality_picker.4":"…\n\nElige la calidad que quieras:","show_quality_picker.5":"🎵 Encontrar la canción original del vídeo","playlist_info_text.1":"🎞 {count} vídeos","playlist_info_text.2":" (se omitieron {skipped} elementos privados/eliminados)","playlist_page_text.1":"{playlist_info_text}\n\nPágina {page} de {playlist_total_pages} — elige uno:","build_playlist_markup.1":"✅ Terminé","show_playlist.1":"❌ No pude leer la lista de reproducción. Puede ser privada o el enlace puede ser incorrecto.\nDetalles del error: ","show_playlist.2":"Esta lista de reproducción no tiene vídeos accesibles (está vacía o todos sus vídeos son privados/eliminados).","show_playlist.3":"Lista de reproducción","handle_playlist_page.1":"⌛ Esta lista de reproducción ha caducado, envía su enlace de nuevo.","handle_playlist_pick.1":"⏳ Obteniendo la información del vídeo...","handle_playlist_done.1":"{base}\n\n✅ Terminé.","handle_message.1":"⏳ Leyendo la lista de reproducción...","handle_message.2":"Ese no es un enlace válido. Envía el enlace de una publicación/reel/historia de Instagram, un vídeo/short de YouTube o un vídeo de TikTok, o usa el botón «{BTN_MUSIC}» para buscar una canción.","handle_message.3":"⏳ Obteniendo la información del enlace...","warn_slow_down.1":"⏳ Para evitar el spam, solo puedes enviar una solicitud nueva cada {REQUEST_COOLDOWN_SECONDS} segundos.\nPor favor, envíala de nuevo en {remaining} segundos.","quota_problem.1":"📦 La cuota de hoy no alcanza para este archivo.\nTamaño del archivo: {volume}\nRestante hoy: {volume2} de {volume3}\n\nLa cuota se reinicia cada noche a medianoche (hora de Irán). Puedes elegir una calidad menor o descargar sin límites con una suscripción VIP (escribe «id» para obtener tu ID de 8 dígitos y entrégaselo al administrador).","account_button.1":"👤 <b>Mi cuenta</b>","account_button.2":"🏷 Nombre: {text}","account_button.3":"🔖 Nombre de usuario: {text}","account_button.4":"🔢 ID de Telegram: <code>{user_id}</code>","account_button.5":"🆔 ID único: <code>{public_id}</code>","account_button.6":"📅 Miembro desde: {date}","account_button.7":"⬇️ Descargas: {downloads_total}","account_button.8":"👑 Estado: administrador (sin límites)","account_button.9":"💎 Suscripción: VIP activa hasta el {date} (quedan {days_left} días)","account_button.10":"💎 Suscripción: ninguna","account_button.11":"📦 Usado hoy: {volume} (ilimitado ♾)","account_button.12":"📦 Usado hoy: {volume} de {volume2} (quedan {volume3})","id_command.1":"🆔 Tu ID único: <code>{public_id}</code>\n🔢 ID de Telegram: <code>{user_id}</code>\n\nEl ID único es la identidad de tu cuenta; entrega este número de 8 dígitos al administrador para activar una suscripción VIP.","build_receipt.1":"🧾 <b>Recibo de activación de la suscripción VIP</b>","build_receipt.2":"N.º de recibo: <code>#{receipt_id:05d}</code>","build_receipt.3":"👤 Usuario: {text} {text2}","build_receipt.4":"🆔 ID: <code>{public_id}</code>","build_receipt.5":"⏳ Duración: {days} días","build_receipt.6":" (renovación)","build_receipt.7":"🎁 Tipo: suscripción mensual automática (tras {AUTO_VIP_AFTER_DAYS} días de membresía)","build_receipt.8":"👑 Tipo: activada por el administrador","build_receipt.9":"📅 Inicio: {date}","build_receipt.10":"📅 Fin: {date}","build_receipt.11":"💵 Valor de la suscripción: <b>{usd}</b> ({usd2} por día)","build_receipt.12":"✅ Suscripción activada.","run_auto_vip_check.1":"🎁 ¡Tu suscripción mensual está activa!\nComo llevas más de {AUTO_VIP_AFTER_DAYS} días con nosotros, cada {AUTO_VIP_DAYS} días se te activa automáticamente una suscripción VIP de {AUTO_VIP_DAYS} días.\n\n","run_auto_vip_check.2":"\n\nHasta el {date} puedes descargar sin límite de volumen 💎","job_allowed.1":"📦 La cuota de hoy se ha agotado. Se reinicia a medianoche; o descarga sin límites con VIP (escribe «id» y entrégaselo al administrador).","job_allowed.2":"⏳ Para evitar el spam, espera {remaining} segundos más e inténtalo de nuevo.","link_markup.1":"🔗 Enlace","cleanup_after_send.1":"✅ ¡Enviado!","handle_quality_choice.1":"❌ Error interno, envía el enlace de nuevo.","handle_quality_choice.2":"⌛ Esta solicitud ha caducado, envía el enlace de nuevo.","handle_quality_choice.3":"⏳ Preparando la descarga...","handle_quality_choice.4":"❌ El archivo pesa {size_mb:.1f}MB y supera el límite de {MAX_TELEGRAM_UPLOAD_MB}MB de los bots oficiales de Telegram. Para archivos más grandes necesitas usar un servidor Local Bot API (consulta el README).","handle_quality_choice.5":"📤 Enviando el archivo ({size_mb:.0f}MB)...\nLos archivos grandes pueden tardar unos minutos, ten paciencia.","handle_quality_choice.6":"❌ La descarga o el envío falló:\n","deliver_song.1":"⚠️ El vídeo pesa {video_size_mb:.1f}MB y supera el límite de {MAX_TELEGRAM_UPLOAD_MB}MB de Telegram.","deliver_song.2":"📤 Enviando el vídeo ({video_size_mb:.0f}MB)...","deliver_song.3":"⬇️ Descargando la versión de audio de «{label}»...","deliver_song.4":"⚠️ El archivo de audio pesa {audio_size_mb:.1f}MB y supera el límite de {MAX_TELEGRAM_UPLOAD_MB}MB de Telegram.","deliver_song.5":"❌ No se pudo enviar la canción:\n","recognize_from_audio_file.1":"🎧 Reconociendo la canción (primer fragmento)...","recognize_from_audio_file.2":"🎧 Reconociendo la canción (segundo fragmento)...","find_and_deliver_song.1":"🎵 Canción original: {label}{variant_txt}\n🔎 Buscando la versión oficial...","find_and_deliver_song.2":"\nArtista: {artist}","find_and_deliver_song.3":"❌ No encontré la versión oficial de «{title}».{artist_txt}\nPuedes probar el botón «🎵 Buscar música» con un nombre más exacto.","handle_find_song.1":"🤖 Revisando el pie del vídeo con IA...","handle_find_song.2":" o consigue una GEMINI_API_KEY gratuita en aistudio.google.com para que pueda deducirlo a partir del pie del vídeo","handle_find_song.3":"ℹ️ No pude reconocer la canción a partir de los metadatos ni del pie del vídeo. Para reconocerla desde el propio audio hay que configurar una AUDD_API_KEY (o ACRCloud){hint}.","handle_find_song.4":"⬇️ Descargando el audio del vídeo para reconocerlo...","handle_find_song.5":"❌ El reconocimiento de la canción falló:\n","handle_find_song.6":"❌ No pude reconocer la canción de este vídeo. Quizá el sonido de fondo no era claro o la canción no está en la base de datos del servicio de reconocimiento.","handle_uploaded_media.1":"🎵 Extraer audio","handle_uploaded_media.2":"🎧 Encontrar la canción original","handle_uploaded_media.3":"🎞 He recibido tu archivo. ¿Qué hago?","download_uploaded_file.1":"❌ El archivo pesa {size_mb:.1f}MB y, con el servidor oficial de Telegram, el bot solo puede recibir archivos de hasta {TELEGRAM_CLOUD_DOWNLOAD_LIMIT_MB}MB. Para archivos más grandes hay que configurar TELEGRAM_API_ID y TELEGRAM_API_HASH (servidor local).","download_uploaded_file.2":"⬇️ Recibiendo el archivo desde Telegram...","download_uploaded_file.3":"❌ Este archivo es demasiado grande para que el bot lo reciba (el límite es de 20 MB sin servidor local).","handle_upload_extract_audio.1":"⌛ Esta solicitud ha caducado, envía el archivo de nuevo.","handle_upload_extract_audio.2":"🎶 Extrayendo el audio...","handle_upload_extract_audio.3":"❌ El archivo de audio pesa {size_mb:.1f}MB y supera el límite de {MAX_TELEGRAM_UPLOAD_MB}MB.","handle_upload_extract_audio.4":"❌ La extracción del audio falló:\n","handle_upload_find_song.1":"🤖 Revisando el pie con IA...","handle_upload_find_song.2":"ℹ️ Para reconocer una canción a partir del audio del archivo hay que configurar una AUDD_API_KEY (o ACRCloud).","handle_upload_find_song.3":"❌ No pude reconocer la canción de este archivo. Quizá el sonido de fondo no era claro o la canción no está en la base de datos del servicio de reconocimiento.","btn.help":"📖 Ayuda","btn.about":"ℹ️ Acerca del bot","btn.music":"🎵 Buscar música","btn.account":"👤 Mi cuenta","vip_granted.user":"🎉 ¡Tu suscripción VIP está activa!\n\n{receipt}\n\nA partir de ahora, hasta el {date}, puedes descargar sin límite de volumen 💎","vip_cancelled.user":"ℹ️ Tu suscripción VIP fue cancelada por el administrador.\nA partir de ahora se aplica la cuota diaria normal ({volume})."},"fr":{"__init__.1":"🔒 Ce contenu n'est pas visible pour le compte avec lequel le bot est connecté (Instagram ne l'affiche qu'à certains publics ou avec une restriction d'âge).\nCe n'est ni une publication privée ni un lien cassé, mais le bot ne peut pas le télécharger.","__init__.2":"🔒 Ce contenu n'est visible sur Instagram que par certains publics (par exemple avec une restriction d'âge), et le bot n'a pas de compte connecté pour réessayer.\nCe n'est ni une publication privée ni un lien cassé, mais le bot ne peut pas le télécharger.","format_music_entry_label.1":"Sans titre","build_music_page.1":"◀️ Page précédente","build_music_page.2":"▶️ Page suivante","build_music_page.3":"🔍 Rechercher sur YouTube","render.1":"⚙️ Traitement final (conversion/fusion du fichier)...","render.2":"?","render.3":"  •  {n}s restantes","render.4":"⬇️ Téléchargement...\n[{bar}] {percent:.0f}%\n{line2}","pick_quality_options.1":" (approx.)","pick_quality_options.2":"🎬 Qualité par défaut{size_txt}","pick_quality_options.3":"🎵 Audio uniquement (MP3)","start.1":"Salut l'ami 🧘🏾🌚\nJe suis ton bot de téléchargement 🧸\n\nAvec moi, tu peux télécharger sans limites des vidéos, de la musique et des publications de presque toutes les plateformes 🧘🏾✨️\n\nTu peux aussi trouver et télécharger grâce à moi la musique, les publications et les vidéos que tu veux 🧘🏾🎧\n\nEnvoie-moi simplement le lien de la publication souhaitée 🧸","help_button.1":"📖 Guide complet du bot\n\n🔗 Télécharger depuis un lien (Instagram / YouTube / TikTok)\n1. Envoie-moi le lien d'une publication, d'un reel ou d'une story Instagram, d'une vidéo ou d'un short YouTube, ou d'une vidéo TikTok\n2. Je te montre la miniature et la description\n3. En dessous, tu vois plusieurs boutons de qualité, chacun avec la taille exacte du fichier (par ex. « 1080p - 24.3MB ») ; choisis celui que tu veux\n4. Si tu ne veux que le son de la vidéo (pas la vidéo elle-même), appuie sur « 🎵 Audio uniquement »\n5. Pendant le téléchargement, tu vois une barre de progression en direct (pourcentage, taille téléchargée sur le total, vitesse) ; à la fin, le fichier t'est envoyé\n\n🎵 Recherche de musique (bouton « {BTN_MUSIC} »)\n1. Appuie sur le bouton et envoie un titre de chanson ou simplement un nom d'artiste\n2. Je cherche d'abord dans YouTube Music (le catalogue officiel) ; si tu envoies un nom d'artiste, toutes ses chansons connues viennent de là (généralement complet pour les artistes internationaux) ; les résultats sont paginés par 10 avec les boutons « ◀️ Page précédente » / « ▶️ Page suivante »\n3. Si tu touches une chanson, tu obtiens pour elle les mêmes boutons de qualité / audio uniquement\n4. Si rien n'est trouvé ou s'il y a peu de résultats (par exemple beaucoup de chansons persanes ne sont pas sur YouTube Music), il y a sous la liste un bouton « 🔍 Rechercher sur YouTube » ; appuie dessus et je ferai la même recherche sur YouTube classique\n\n📃 Playlists YouTube\nEnvoie le lien d'une playlist (celui qui contient /playlist?list=...) ; les infos de la playlist et ses vidéos sont paginées par 5. Touche une vidéo pour voir ses infos et son image, puis choisis la qualité et télécharge-la. Utilise « ▶️ Page suivante » et « ◀️ Page précédente » pour naviguer entre les pages et, à la fin, appuie sur « ✅ J'ai terminé » pour fermer les boutons (les infos de la playlist restent)\n\n🎞 Envoyer le fichier vidéo lui-même (pas un lien)\nSi tu m'envoies le clip/la vidéo elle-même (par exemple le fichier d'un reel), je te demande « Que dois-je faire ? » avec deux boutons : « 🎵 Extraire l'audio » (je convertis le son de la vidéo en mp3 et je te l'envoie) ou « 🎧 Trouver la chanson originale » (je reconnais la chanson de la vidéo et je t'envoie sa version officielle en audio)\n• Si tu écris une légende pour la vidéo, la reconnaissance sera plus précise\n• Recevoir des fichiers de plus de 20 Mo n'est possible qu'avec un serveur local Telegram (TELEGRAM_API_ID/HASH)\n\n🎧 Trouver la chanson originale d'une vidéo\nSous les boutons de qualité de chaque vidéo, il y a aussi un bouton « 🎵 Trouver la chanson originale de la vidéo ». Quand tu appuies dessus :\n1. Je vérifie d'abord si la plateforme elle-même (surtout TikTok) donne le nom de la chanson dans ses métadonnées\n2. Sinon, je prends un extrait du son de la vidéo et je l'identifie avec un service de reconnaissance musicale\n3. Une fois la version officielle trouvée, je t'envoie moi-même la vidéo et sa version audio avec le nom correct et propre de la chanson (pas besoin de choisir à la main)\n4. Si elle n'est pas trouvée, je te donne au moins le nom de l'artiste (s'il est identifié) pour que tu cherches toi-même\n\n👤 Compte, quota et VIP\n• Le bouton « 👤 Mon compte » : les infos de ton compte, le nombre de téléchargements, le volume utilisé aujourd'hui et l'état de l'abonnement\n• Chaque utilisateur peut télécharger jusqu'à {volume} par jour, remis à zéro chaque nuit à minuit (heure d'Iran)\n• En écrivant « id », tu obtiens un identifiant unique à 8 chiffres ; donne-le à l'administrateur pour activer un abonnement VIP (téléchargements illimités pendant quelques jours)\n\n⚠️ Remarques importantes\n• Pour éviter le spam, tu ne peux envoyer qu'une nouvelle demande toutes les {REQUEST_COOLDOWN_SECONDS} secondes ; si tu envoies trop vite, je te dis combien de secondes attendre\n• Les publications privées ou les vidéos restreintes ne sont pas téléchargeables sans configuration des cookies\n• Merci de m'utiliser uniquement pour votre propre contenu ou un contenu que vous avez le droit d'utiliser ; le respect du droit d'auteur incombe à l'utilisateur","about_button.1":"ℹ️ À propos du bot :\n\nCe bot télécharge depuis Instagram, YouTube et TikTok grâce à yt-dlp.\nMerci de l'utiliser uniquement pour votre propre contenu ou un contenu que vous avez le droit d'utiliser ; le respect du droit d'auteur et des règles des plateformes vous incombe.","music_button.1":"🎵 Envoie le titre de la chanson et l'artiste (par exemple : « Shadmehr Aghili - Bahar »).\nJe trouve la version officielle et je te l'envoie.","handle_music_query.1":"Envoie un titre de chanson ou un nom d'artiste et je lance la recherche.","handle_music_query.2":"🔎 Recherche de « {query_text} » dans YouTube Music...","handle_music_query.3":"❌ La recherche a échoué :\n","handle_music_query.4":"Rien trouvé sur YouTube Music pour « {query_text} ». Tu peux aussi chercher sur YouTube classique avec le bouton ci-dessous :","handle_music_query.5":"🎵 Résultats YouTube Music pour « {query_text} » ({count} éléments) — page 1 sur {total_pages} :\nChoisis-en un ou, si ça ne suffit pas, appuie ci-dessous sur « Rechercher sur YouTube » :","handle_music_page.1":"⌛ Cette recherche a expiré, utilise à nouveau le bouton de recherche de musique.","handle_music_page.2":"YouTube Music","handle_music_page.3":"YouTube","handle_music_page.4":"🎵 Résultats {source_label} pour « {query} » ({count} éléments) — page {page} sur {total_pages} :\nChoisis-en un :","handle_music_youtube_fallback.1":"🔎 Recherche de « {query_text} » sur YouTube...","handle_music_youtube_fallback.2":"Rien trouvé non plus sur YouTube classique pour « {query_text} ».","handle_music_youtube_fallback.3":"🎵 Résultats YouTube pour « {query_text} » ({count} éléments) — page 1 sur {total_pages} :\nChoisis-en un :","handle_music_pick.1":"⏳ Récupération des informations...","show_quality_picker.1":"❌ Je n'ai pas pu récupérer les informations de ce lien. La publication est peut-être privée ou le lien est peut-être incorrect.\nDétails de l'erreur : ","show_quality_picker.2":"\nChoisis la qualité souhaitée :","show_quality_picker.3":"Choisis la qualité souhaitée :","show_quality_picker.4":"…\n\nChoisis la qualité souhaitée :","show_quality_picker.5":"🎵 Trouver la chanson originale de la vidéo","playlist_info_text.1":"🎞 {count} vidéos","playlist_info_text.2":" ({skipped} éléments privés/supprimés ignorés)","playlist_page_text.1":"{playlist_info_text}\n\nPage {page} sur {playlist_total_pages} — choisis-en un :","build_playlist_markup.1":"✅ J'ai terminé","show_playlist.1":"❌ Je n'ai pas pu lire la playlist. Elle est peut-être privée ou le lien est peut-être incorrect.\nDétails de l'erreur : ","show_playlist.2":"Cette playlist ne contient aucune vidéo accessible (elle est vide ou toutes ses vidéos sont privées/supprimées).","show_playlist.3":"Playlist","handle_playlist_page.1":"⌛ Cette playlist a expiré, renvoie son lien.","handle_playlist_pick.1":"⏳ Récupération des informations de la vidéo...","handle_playlist_done.1":"{base}\n\n✅ Terminé.","handle_message.1":"⏳ Lecture de la playlist...","handle_message.2":"Ce n'est pas un lien valide. Envoie un lien de publication/reel/story Instagram, de vidéo/short YouTube ou de vidéo TikTok, ou utilise le bouton « {BTN_MUSIC} » pour chercher une chanson.","handle_message.3":"⏳ Récupération des informations du lien...","warn_slow_down.1":"⏳ Pour éviter le spam, tu ne peux envoyer qu'une nouvelle demande toutes les {REQUEST_COOLDOWN_SECONDS} secondes.\nMerci de la renvoyer dans {remaining} secondes.","quota_problem.1":"📦 Ton quota du jour ne suffit pas pour ce fichier.\nTaille du fichier : {volume}\nRestant aujourd'hui : {volume2} sur {volume3}\n\nLe quota est remis à zéro chaque nuit à minuit (heure d'Iran). Tu peux choisir une qualité inférieure ou télécharger sans limites avec un abonnement VIP (écris « id » pour obtenir ton identifiant à 8 chiffres et donne-le à l'administrateur).","account_button.1":"👤 <b>Mon compte</b>","account_button.2":"🏷 Nom : {text}","account_button.3":"🔖 Nom d'utilisateur : {text}","account_button.4":"🔢 ID Telegram : <code>{user_id}</code>","account_button.5":"🆔 ID unique : <code>{public_id}</code>","account_button.6":"📅 Membre depuis : {date}","account_button.7":"⬇️ Téléchargements : {downloads_total}","account_button.8":"👑 Statut : administrateur (sans limites)","account_button.9":"💎 Abonnement : VIP actif jusqu'au {date} ({days_left} jours restants)","account_button.10":"💎 Abonnement : aucun","account_button.11":"📦 Utilisé aujourd'hui : {volume} (illimité ♾)","account_button.12":"📦 Utilisé aujourd'hui : {volume} sur {volume2} ({volume3} restants)","id_command.1":"🆔 Ton ID unique : <code>{public_id}</code>\n🔢 ID Telegram : <code>{user_id}</code>\n\nL'ID unique est l'identité de ton compte ; donne ce nombre à 8 chiffres à l'administrateur pour activer un abonnement VIP.","build_receipt.1":"🧾 <b>Reçu d'activation de l'abonnement VIP</b>","build_receipt.2":"N° de reçu : <code>#{receipt_id:05d}</code>","build_receipt.3":"👤 Utilisateur : {text} {text2}","build_receipt.4":"🆔 ID : <code>{public_id}</code>","build_receipt.5":"⏳ Durée : {days} jours","build_receipt.6":" (renouvellement)","build_receipt.7":"🎁 Type : abonnement mensuel automatique (après {AUTO_VIP_AFTER_DAYS} jours d'adhésion)","build_receipt.8":"👑 Type : activé par l'administrateur","build_receipt.9":"📅 Début : {date}","build_receipt.10":"📅 Fin : {date}","build_receipt.11":"💵 Valeur de l'abonnement : <b>{usd}</b> ({usd2} par jour)","build_receipt.12":"✅ Abonnement activé.","run_auto_vip_check.1":"🎁 Ton abonnement mensuel est activé !\nComme tu es avec nous depuis plus de {AUTO_VIP_AFTER_DAYS} jours, un abonnement VIP de {AUTO_VIP_DAYS} jours t'est activé automatiquement tous les {AUTO_VIP_DAYS} jours.\n\n","run_auto_vip_check.2":"\n\nJusqu'au {date}, tu peux télécharger sans limite de volume 💎","job_allowed.1":"📦 Ton quota du jour est épuisé. Il est remis à zéro à minuit ; ou télécharge sans limites avec le VIP (écris « id » et donne-le à l'administrateur).","job_allowed.2":"⏳ Pour éviter le spam, attends encore {remaining} secondes et réessaie.","link_markup.1":"🔗 Lien","cleanup_after_send.1":"✅ Envoyé !","handle_quality_choice.1":"❌ Erreur interne, renvoie le lien.","handle_quality_choice.2":"⌛ Cette demande a expiré, renvoie le lien.","handle_quality_choice.3":"⏳ Préparation du téléchargement...","handle_quality_choice.4":"❌ Le fichier fait {size_mb:.1f}MB et dépasse la limite de {MAX_TELEGRAM_UPLOAD_MB}MB des bots Telegram officiels. Pour des fichiers plus gros, il faut utiliser un serveur Local Bot API (voir le README).","handle_quality_choice.5":"📤 Envoi du fichier ({size_mb:.0f}MB)...\nLes gros fichiers peuvent prendre quelques minutes, sois patient.","handle_quality_choice.6":"❌ Le téléchargement ou l'envoi a échoué :\n","deliver_song.1":"⚠️ La vidéo fait {video_size_mb:.1f}MB et dépasse la limite de {MAX_TELEGRAM_UPLOAD_MB}MB de Telegram.","deliver_song.2":"📤 Envoi de la vidéo ({video_size_mb:.0f}MB)...","deliver_song.3":"⬇️ Téléchargement de la version audio de « {label} »...","deliver_song.4":"⚠️ Le fichier audio fait {audio_size_mb:.1f}MB et dépasse la limite de {MAX_TELEGRAM_UPLOAD_MB}MB de Telegram.","deliver_song.5":"❌ L'envoi de la chanson a échoué :\n","recognize_from_audio_file.1":"🎧 Reconnaissance de la chanson (premier extrait)...","recognize_from_audio_file.2":"🎧 Reconnaissance de la chanson (deuxième extrait)...","find_and_deliver_song.1":"🎵 Chanson originale : {label}{variant_txt}\n🔎 Recherche de la version officielle...","find_and_deliver_song.2":"\nArtiste : {artist}","find_and_deliver_song.3":"❌ Je n'ai pas trouvé la version officielle de « {title} ».{artist_txt}\nTu peux essayer le bouton « 🎵 Recherche musicale » avec un nom plus précis.","handle_find_song.1":"🤖 Analyse de la légende de la vidéo par l'IA...","handle_find_song.2":" ou obtiens une GEMINI_API_KEY gratuite sur aistudio.google.com pour que je puisse deviner à partir de la légende de la vidéo","handle_find_song.3":"ℹ️ Je n'ai pas pu identifier la chanson à partir des métadonnées ou de la légende de la vidéo. Pour l'identifier à partir du son lui-même, il faut configurer une AUDD_API_KEY (ou ACRCloud){hint}.","handle_find_song.4":"⬇️ Téléchargement du son de la vidéo pour la reconnaissance...","handle_find_song.5":"❌ La reconnaissance de la chanson a échoué :\n","handle_find_song.6":"❌ Je n'ai pas pu identifier la chanson de cette vidéo. Le son de fond n'était peut-être pas clair, ou la chanson n'est pas dans la base du service de reconnaissance.","handle_uploaded_media.1":"🎵 Extraire l'audio","handle_uploaded_media.2":"🎧 Trouver la chanson originale","handle_uploaded_media.3":"🎞 J'ai reçu ton fichier. Que dois-je faire ?","download_uploaded_file.1":"❌ Le fichier fait {size_mb:.1f}MB et, avec le serveur officiel de Telegram, le bot ne peut recevoir que des fichiers jusqu'à {TELEGRAM_CLOUD_DOWNLOAD_LIMIT_MB}MB. Pour des fichiers plus gros, il faut définir TELEGRAM_API_ID et TELEGRAM_API_HASH (serveur local).","download_uploaded_file.2":"⬇️ Réception du fichier depuis Telegram...","download_uploaded_file.3":"❌ Ce fichier est trop gros pour être reçu par le bot (limite de 20 Mo sans serveur local).","handle_upload_extract_audio.1":"⌛ Cette demande a expiré, renvoie le fichier.","handle_upload_extract_audio.2":"🎶 Extraction de l'audio...","handle_upload_extract_audio.3":"❌ Le fichier audio fait {size_mb:.1f}MB et dépasse la limite de {MAX_TELEGRAM_UPLOAD_MB}MB.","handle_upload_extract_audio.4":"❌ L'extraction de l'audio a échoué :\n","handle_upload_find_song.1":"🤖 Analyse de la légende par l'IA...","handle_upload_find_song.2":"ℹ️ Pour identifier une chanson à partir du son du fichier, il faut configurer une AUDD_API_KEY (ou ACRCloud).","handle_upload_find_song.3":"❌ Je n'ai pas pu identifier la chanson de ce fichier. Le son de fond n'était peut-être pas clair, ou la chanson n'est pas dans la base du service de reconnaissance.","btn.help":"📖 Aide","btn.about":"ℹ️ À propos","btn.music":"🎵 Recherche musicale","btn.account":"👤 Mon compte","vip_granted.user":"🎉 Ton abonnement VIP est activé !\n\n{receipt}\n\nÀ partir de maintenant, jusqu'au {date}, tu peux télécharger sans limite de volume 💎","vip_cancelled.user":"ℹ️ Ton abonnement VIP a été annulé par l'administrateur.\nÀ partir de maintenant, le quota quotidien normal ({volume}) s'applique."}}''')


def _load_locales() -> dict:
    data = {code: dict(_EMBEDDED_LOCALES.get(code, {})) for code in LANG_CODES}
    for code, extra in _EXTRA_LOCALES.items():
        data.setdefault(code, {}).update(extra)
    for code in LANG_CODES:
        path = LOCALES_DIR / f"{code}.json"
        try:
            with open(path, encoding="utf-8") as f:
                data[code].update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("could not read locale file %s", path.name)
    return data


_EXTRA_LOCALES = {
    "fa": {
        "yt.bot_check": '❌ یوتیوب الان ربات رو شناسایی کرده و این ویدیو رو نمی\u200cده. چند دقیقه دیگه دوباره امتحان کن؛ مدیر هم مطلع شد.',
        "ig.login_wall": '❌ اینستاگرام الان به ربات اجازه\u200cی دیدن این پست رو نمی\u200cده (ورود لازمه یا محدودیت سرعت). چند دقیقه دیگه دوباره امتحان کن؛ اگه تکرار شد، به مدیر ربات خبر بده.',
        "ext.btn": "🌐 دانلود خارجی (لینک مستقیم)",
        "ext.unavailable": "❌ دانلود خارجی فعلاً فعال نیست.",
        "ext.limit_reached": "⛔ سهمیه‌ی دانلود خارجی امروزت تموم شده ({used} از {limit}). فردا دوباره امتحان کن.",
        "ext.vip_hint": "\n💎 کاربرهای VIP روزی تا {vip_limit} بار و تا {vip_volume} می‌تونن از این قابلیت استفاده کنن.",
        "ext.too_big": "❌ هیچ‌کدوم از کیفیت‌های این ویدیو توی سقف دانلود خارجی تو ({limit}) جا نمی‌شه.",
        "ext.busy": "⏳ الان چند دانلود خارجی در حال انجامه؛ چند دقیقه‌ی دیگه دوباره امتحان کن.",
        "ext.start": "🌐 دانلود خارجی شروع شد.\nکیفیت انتخابی: {quality}\nاول روی سرور دانلود می‌شه و بعد روی گوگل درایو آپلود می‌شه.",
        "ext.uploading": "⬆️ در حال آپلود روی گوگل درایو...\n[{bar}] {percent:.0f}%\n{line2}",
        "ext.too_big_after": "❌ حجم نهایی فایل ({volume}) از سقف مجاز ({limit}) بیشتره. یه کیفیت پایین‌تر رو امتحان کن.",
        "ext.ready": "✅ فایل آماده‌ست!\n📁 {name}\n📦 حجم: {volume}\n\n⏳ این لینک تا {minutes} دقیقه (تا ساعت {time}) معتبره و بعدش فایل از درایو پاک می‌شه.\nروی دکمه‌ی زیر بزن تا دانلود شروع بشه.\n\n📊 دانلود خارجی امروز: {used} از {limit}",
        "ext.link_btn": "⬇️ دانلود",
        "ext.expired": "⌛ مهلت این لینک تموم شد و فایل پاک شد. برای گرفتن دوباره، لینک ویدیو رو دوباره بفرست.",
        "ext.failed": "❌ دانلود خارجی انجام نشد:\n",
    },
    "en": {
        "yt.bot_check": '❌ YouTube is currently blocking the bot from getting this video. Try again in a few minutes; the admin has been notified.',
        "ig.login_wall": "❌ Instagram isn't letting the bot view this post right now (login required or rate limit). Try again in a few minutes; if it keeps happening, let the bot admin know.",
        "ext.btn": "🌐 External download (direct link)",
        "ext.unavailable": "❌ External download isn't available right now.",
        "ext.limit_reached": "⛔ You've used up today's external downloads ({used} of {limit}). Try again tomorrow.",
        "ext.vip_hint": "\n💎 VIP users can use this up to {vip_limit} times a day and up to {vip_volume}.",
        "ext.too_big": "❌ None of this video's qualities fit within your external download limit ({limit}).",
        "ext.busy": "⏳ Several external downloads are running right now; please try again in a few minutes.",
        "ext.start": "🌐 External download started.\nSelected quality: {quality}\nIt's downloaded on the server first, then uploaded to Google Drive.",
        "ext.uploading": "⬆️ Uploading to Google Drive...\n[{bar}] {percent:.0f}%\n{line2}",
        "ext.too_big_after": "❌ The final file size ({volume}) is above the allowed limit ({limit}). Try a lower quality.",
        "ext.ready": "✅ Your file is ready!\n📁 {name}\n📦 Size: {volume}\n\n⏳ This link is valid for {minutes} minutes (until {time}); after that the file is deleted from the drive.\nTap the button below to start the download.\n\n📊 External downloads today: {used} of {limit}",
        "ext.link_btn": "⬇️ Download",
        "ext.expired": "⌛ This link has expired and the file was deleted. To get it again, send the video link again.",
        "ext.failed": "❌ The external download failed:\n",
    },
    "tr": {
        "yt.bot_check": '❌ YouTube şu anda botun bu videoyu almasını engelliyor. Birkaç dakika sonra tekrar dene; yönetici bilgilendirildi.',
        "ig.login_wall": '❌ Instagram şu anda botun bu gönderiyi görmesine izin vermiyor (giriş gerekli ya da hız sınırı). Birkaç dakika sonra tekrar dene; devam ederse bot yöneticisine haber ver.',
        "ext.btn": "🌐 Harici indirme (doğrudan bağlantı)",
        "ext.unavailable": "❌ Harici indirme şu anda kullanılamıyor.",
        "ext.limit_reached": "⛔ Bugünkü harici indirme hakkın doldu ({used}/{limit}). Yarın tekrar dene.",
        "ext.vip_hint": "\n💎 VIP kullanıcılar bunu günde {vip_limit} kereye kadar ve {vip_volume} boyuta kadar kullanabilir.",
        "ext.too_big": "❌ Bu videonun hiçbir kalitesi harici indirme sınırına ({limit}) sığmıyor.",
        "ext.busy": "⏳ Şu anda birkaç harici indirme sürüyor; lütfen birkaç dakika sonra tekrar dene.",
        "ext.start": "🌐 Harici indirme başladı.\nSeçilen kalite: {quality}\nÖnce sunucuya indirilir, sonra Google Drive'a yüklenir.",
        "ext.uploading": "⬆️ Google Drive'a yükleniyor...\n[{bar}] {percent:.0f}%\n{line2}",
        "ext.too_big_after": "❌ Dosyanın son boyutu ({volume}) izin verilen sınırı ({limit}) aşıyor. Daha düşük bir kalite dene.",
        "ext.ready": "✅ Dosyan hazır!\n📁 {name}\n📦 Boyut: {volume}\n\n⏳ Bu bağlantı {minutes} dakika (saat {time}'e kadar) geçerli; sonrasında dosya Drive'dan silinir.\nİndirmeyi başlatmak için aşağıdaki düğmeye bas.\n\n📊 Bugünkü harici indirmeler: {used}/{limit}",
        "ext.link_btn": "⬇️ İndir",
        "ext.expired": "⌛ Bu bağlantının süresi doldu ve dosya silindi. Tekrar almak için video bağlantısını yeniden gönder.",
        "ext.failed": "❌ Harici indirme başarısız oldu:\n",
    },
    "ru": {
        "yt.bot_check": '❌ YouTube сейчас не даёт боту получить это видео. Попробуй снова через несколько минут; администратор уведомлён.',
        "ig.login_wall": '❌ Instagram сейчас не позволяет боту открыть этот пост (нужен вход или превышен лимит запросов). Попробуй снова через несколько минут; если повторится, сообщи администратору бота.',
        "ext.btn": "🌐 Внешняя загрузка (прямая ссылка)",
        "ext.unavailable": "❌ Внешняя загрузка сейчас недоступна.",
        "ext.limit_reached": "⛔ Лимит внешних загрузок на сегодня исчерпан ({used} из {limit}). Попробуй завтра.",
        "ext.vip_hint": "\n💎 VIP-пользователи могут использовать это до {vip_limit} раз в день и для файлов до {vip_volume}.",
        "ext.too_big": "❌ Ни одно из качеств этого видео не укладывается в твой лимит внешней загрузки ({limit}).",
        "ext.busy": "⏳ Сейчас выполняется несколько внешних загрузок; попробуй снова через несколько минут.",
        "ext.start": "🌐 Внешняя загрузка началась.\nВыбранное качество: {quality}\nСначала файл скачивается на сервер, затем загружается в Google Диск.",
        "ext.uploading": "⬆️ Загружаю в Google Диск...\n[{bar}] {percent:.0f}%\n{line2}",
        "ext.too_big_after": "❌ Итоговый размер файла ({volume}) превышает допустимый лимит ({limit}). Попробуй качество пониже.",
        "ext.ready": "✅ Файл готов!\n📁 {name}\n📦 Размер: {volume}\n\n⏳ Ссылка действует {minutes} мин. (до {time}), после этого файл удаляется с диска.\nНажми кнопку ниже, чтобы начать загрузку.\n\n📊 Внешних загрузок сегодня: {used} из {limit}",
        "ext.link_btn": "⬇️ Скачать",
        "ext.expired": "⌛ Срок действия ссылки истёк, файл удалён. Чтобы получить его снова, отправь ссылку на видео ещё раз.",
        "ext.failed": "❌ Внешняя загрузка не удалась:\n",
    },
    "es": {
        "yt.bot_check": '❌ YouTube está bloqueando ahora mismo al bot para obtener este vídeo. Inténtalo de nuevo en unos minutos; el administrador ha sido avisado.',
        "ig.login_wall": '❌ Instagram no deja que el bot vea esta publicación ahora mismo (se requiere inicio de sesión o hay un límite de velocidad). Inténtalo de nuevo en unos minutos; si se repite, avisa al administrador del bot.',
        "ext.btn": "🌐 Descarga externa (enlace directo)",
        "ext.unavailable": "❌ La descarga externa no está disponible ahora mismo.",
        "ext.limit_reached": "⛔ Has agotado las descargas externas de hoy ({used} de {limit}). Inténtalo de nuevo mañana.",
        "ext.vip_hint": "\n💎 Los usuarios VIP pueden usarla hasta {vip_limit} veces al día y hasta {vip_volume}.",
        "ext.too_big": "❌ Ninguna de las calidades de este vídeo cabe en tu límite de descarga externa ({limit}).",
        "ext.busy": "⏳ Hay varias descargas externas en curso; inténtalo de nuevo en unos minutos.",
        "ext.start": "🌐 Descarga externa iniciada.\nCalidad elegida: {quality}\nPrimero se descarga en el servidor y luego se sube a Google Drive.",
        "ext.uploading": "⬆️ Subiendo a Google Drive...\n[{bar}] {percent:.0f}%\n{line2}",
        "ext.too_big_after": "❌ El tamaño final del archivo ({volume}) supera el límite permitido ({limit}). Prueba con una calidad menor.",
        "ext.ready": "✅ ¡Tu archivo está listo!\n📁 {name}\n📦 Tamaño: {volume}\n\n⏳ Este enlace es válido {minutes} minutos (hasta las {time}); después el archivo se elimina de Drive.\nPulsa el botón de abajo para iniciar la descarga.\n\n📊 Descargas externas hoy: {used} de {limit}",
        "ext.link_btn": "⬇️ Descargar",
        "ext.expired": "⌛ Este enlace ha caducado y el archivo se eliminó. Para obtenerlo de nuevo, envía otra vez el enlace del vídeo.",
        "ext.failed": "❌ La descarga externa falló:\n",
    },
    "fr": {
        "yt.bot_check": "❌ YouTube empêche actuellement le bot de récupérer cette vidéo. Réessaie dans quelques minutes ; l'administrateur a été prévenu.",
        "ig.login_wall": "❌ Instagram ne laisse pas le bot consulter cette publication pour le moment (connexion requise ou limite de débit). Réessaie dans quelques minutes ; si cela se répète, préviens l'administrateur du bot.",
        "ext.btn": "🌐 Téléchargement externe (lien direct)",
        "ext.unavailable": "❌ Le téléchargement externe n'est pas disponible pour le moment.",
        "ext.limit_reached": "⛔ Tu as épuisé tes téléchargements externes du jour ({used} sur {limit}). Réessaie demain.",
        "ext.vip_hint": "\n💎 Les utilisateurs VIP peuvent l'utiliser jusqu'à {vip_limit} fois par jour et jusqu'à {vip_volume}.",
        "ext.too_big": "❌ Aucune des qualités de cette vidéo ne rentre dans ta limite de téléchargement externe ({limit}).",
        "ext.busy": "⏳ Plusieurs téléchargements externes sont en cours ; réessaie dans quelques minutes.",
        "ext.start": "🌐 Téléchargement externe lancé.\nQualité choisie : {quality}\nLe fichier est d'abord téléchargé sur le serveur, puis envoyé sur Google Drive.",
        "ext.uploading": "⬆️ Envoi vers Google Drive...\n[{bar}] {percent:.0f}%\n{line2}",
        "ext.too_big_after": "❌ La taille finale du fichier ({volume}) dépasse la limite autorisée ({limit}). Essaie une qualité inférieure.",
        "ext.ready": "✅ Ton fichier est prêt !\n📁 {name}\n📦 Taille : {volume}\n\n⏳ Ce lien est valable {minutes} minutes (jusqu'à {time}) ; ensuite le fichier est supprimé du Drive.\nAppuie sur le bouton ci-dessous pour lancer le téléchargement.\n\n📊 Téléchargements externes aujourd'hui : {used} sur {limit}",
        "ext.link_btn": "⬇️ Télécharger",
        "ext.expired": "⌛ Ce lien a expiré et le fichier a été supprimé. Pour le récupérer, renvoie le lien de la vidéo.",
        "ext.failed": "❌ Le téléchargement externe a échoué :\n",
    },
}


LOCALES = _load_locales()
_current_lang = contextvars.ContextVar("current_lang", default=DEFAULT_LANG)
_LANG_CHOSEN = {}  # user_id -> کد زبانِ انتخاب‌شده (None = هنوز انتخاب نکرده)


def tr(lang: str, key: str, **kw) -> str:
    """ترجمه‌ی کلید به زبان lang؛ اگه نبود به انگلیسی و بعد فارسی برمی‌گرده."""
    text = None
    for code in (lang, "en", DEFAULT_LANG):
        text = LOCALES.get(code, {}).get(key)
        if text:
            break
    if text is None:
        return key
    try:
        return text.format(**kw) if kw else text
    except (KeyError, IndexError, ValueError):
        fallback = LOCALES.get(DEFAULT_LANG, {}).get(key, key)
        try:
            return fallback.format(**kw)
        except Exception:
            return fallback


def T(key: str, **kw) -> str:
    return tr(_current_lang.get(), key, **kw)


def btn(name: str) -> str:
    return T(f"btn.{name}")


def all_btn_labels(name: str) -> list:
    """برچسب یه دکمه‌ی کیبورد به همه‌ی زبان‌ها (برای تشخیص فشردن دکمه فارغ از زبان)."""
    return sorted({tr(code, f"btn.{name}") for code in LANG_CODES})


@contextmanager
def lang_scope(lang):
    token = _current_lang.set(lang if lang in LANG_CODES else DEFAULT_LANG)
    try:
        yield
    finally:
        _current_lang.reset(token)


def btn_regex(name: str):
    return re.compile("^(?:" + "|".join(re.escape(x) for x in all_btn_labels(name)) + ")$")


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[btn("music"), btn("account")], [btn("help"), btn("about")]],
        resize_keyboard=True,   # دکمه‌ها رو کوچیک و جمع‌وجور نشون می‌ده
        is_persistent=True,     # همیشه روی صفحه بمونه
    )


def lang_picker_markup(page: int) -> InlineKeyboardMarkup:
    """پنل شیشه‌ای انتخاب زبان: صفحه‌ی اول ۱۰ زبان، با دکمه‌ی «بعدی» ۵ زبان بعدی."""
    pages = (len(LANGS) + LANG_PAGE_SIZE - 1) // LANG_PAGE_SIZE
    page = max(0, min(page, pages - 1))
    rows, row = [], []
    for code, name, flag in LANGS[page * LANG_PAGE_SIZE:(page + 1) * LANG_PAGE_SIZE]:
        row.append(InlineKeyboardButton(f"{flag} {name}", callback_data=f"lang|set|{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if page < pages - 1:
        rows.append([InlineKeyboardButton("Next ▶️ | بعدی", callback_data=f"lang|p|{page + 1}")])
    if page > 0:
        rows.append([InlineKeyboardButton("◀️ Back | قبلی", callback_data=f"lang|p|{page - 1}")])
    return InlineKeyboardMarkup(rows)


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
                T("__init__.1")
            )
        else:
            msg = (
                T("__init__.2")
            )
        super().__init__(msg)


class InstagramLoginWall(Exception):
    """اینستاگرام درخواست رو به صفحه‌ی ورود فرستاده (محدودیت سرعت ناشناس، یا کوکی منقضی/باطل)."""

    def __init__(self, had_cookies: bool):
        self.had_cookies = had_cookies
        super().__init__(T("ig.login_wall"))


class YouTubeBotCheck(Exception):
    """یوتیوب حتی با کوکی (اگه داشتیم) و کلاینت جایگزین هم «ثابت کن ربات نیستی» داد."""

    def __init__(self):
        super().__init__(T("yt.bot_check"))


ALT_YOUTUBE_CLIENT = "android_vr"  # کلاینت جایگزین که معمولاً PO Token نمی‌خواد
_YT_PROBLEM = {"count": 0, "at": 0.0, "last_alert": 0.0}

IG_LOGIN_WALL_SIGNS = (
    "redirected to the login page", "anonymously", "login required", "you need to log in",
    "log in to access", "use --cookies", "rate-limit", "rate limit",
    "http error 401", "unauthorized",  # کوکی هست ولی اینستاگرام نشستش رو قبول نمی‌کنه (منقضی/باطل)
)
_IG_WALL = {"count": 0, "had_cookies": False, "last_alert": 0.0, "at": 0.0}


def _cookies_available() -> bool:
    return bool(COOKIES_FILE and Path(COOKIES_FILE).exists())


def _is_instagram_url(args) -> bool:
    return bool(args) and isinstance(args[0], str) and "instagram.com" in args[0].lower()


def _ig_login_wall(err_text: str) -> bool:
    t = err_text.lower()
    return "instagram" in t and any(sg in t for sg in IG_LOGIN_WALL_SIGNS)


def _note_ig_login_wall(had_cookies: bool) -> None:
    _IG_WALL["count"] += 1
    _IG_WALL["had_cookies"] = had_cookies
    _IG_WALL["at"] = time.time()


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
    یوتیوب و بقیه: اول بدون کوکی و با کلاینت پیش‌فرض؛ اگه خطا «ثابت کن ربات نیستی» یا ۴۰۳ بود، به ترتیب
    با کوکی (اگه داریم) و بعد بدون کوکی با کلاینت جایگزین (android_vr).
    اینستاگرام: از سرور بدون ورود تقریباً همیشه محدود می‌شه، پس اگه کوکی داریم از همون اول با کوکی می‌ریم.
    اگه اینستاگرام باز هم به صفحه‌ی ورود فرستاد (کوکی نیست/منقضی شده)، بدون تلاش بی‌فایده خطای روشن می‌دیم.
    برای هر خطای دیگه (لینک اشتباه، پست خصوصی و ...) مستقیم پرتاب می‌شه.
    """
    ig = _is_instagram_url(args)
    ig_cookies = ig and _cookies_available()
    try:
        return func(*args, use_cookies=ig_cookies, player_client=None, **kwargs)
    except Exception as first_error:
        text = str(first_error)
        if _is_audience_restricted(text):
            if ig_cookies:  # همین الان با حساب واردشده بودیم؛ اون حساب هم اجازه نداره
                raise InstagramAudienceRestricted(had_session=True) from first_error
            return _retry_audience_restricted_once(func, first_error, *args, **kwargs)
        if ig and _ig_login_wall(text):
            _note_ig_login_wall(ig_cookies)
            raise InstagramLoginWall(had_cookies=ig_cookies) from first_error
        if not _is_retryable(text):
            raise
        last_error = first_error

    steps = []
    if _cookies_available() and not ig_cookies:
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
    if _looks_like_bot_check(str(last_error).lower()):
        _YT_PROBLEM["count"] += 1
        _YT_PROBLEM["at"] = time.time()
        raise YouTubeBotCheck() from last_error
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
    title = entry.get("title") or T("format_music_entry_label.1")
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
        nav_row.append(InlineKeyboardButton(T("build_music_page.1"), callback_data=f"mpage|{key}|{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(T("build_music_page.2"), callback_data=f"mpage|{key}|{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    # فقط وقتی هنوز رو یوتیوب‌میوزیک هستیم این گزینه رو نشون بده؛ وقتی
    # کاربر خودش زده رو یوتیوب عادی، دیگه نیازی به این دکمه نیست
    if source != "youtube":
        buttons.append([InlineKeyboardButton(T("build_music_page.3"), callback_data=f"myoutube|{key}")])

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
            return T("render.1")
        bar = make_progress_bar(self.percent)
        downloaded_txt = format_size(self.downloaded) or "0KB"
        total_txt = format_size(self.total) if self.total else T("render.2")
        line2 = f"{downloaded_txt} / {total_txt}"
        if self.speed:
            line2 += f"  •  {format_size(self.speed)}/s"
        if self.eta:
            line2 += T("render.3", n=int(self.eta))
        return T("render.4", bar=bar, percent=self.percent, line2=line2)


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
            size_txt = f" - {format_size(total)}" + ("" if exact else T("pick_quality_options.1"))
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
        options.append({"label": T("pick_quality_options.2", size_txt=size_txt), "format_id": "best", "kind": "video", "size": size or None})

    # گزینه‌ی فقط صدا همیشه اضافه میشه
    audio_label = T("pick_quality_options.3")
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
    user = update.effective_user
    if _LANG_CHOSEN.get(user.id) is None:
        # اولین بار (یا هنوز زبان انتخاب نکرده): اول زبان رو بپرس
        await update.message.reply_text(LANG_PROMPT, reply_markup=lang_picker_markup(0))
        return
    await update.message.reply_text(T("start.1"), reply_markup=main_keyboard())


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(LANG_PROMPT, reply_markup=lang_picker_markup(0))


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import BadRequest

    query = update.callback_query
    parts = (query.data or "").split("|")
    if len(parts) == 3 and parts[1] == "p":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=lang_picker_markup(int(parts[2])))
        except (ValueError, BadRequest):
            pass
        return
    if len(parts) == 3 and parts[1] == "set" and parts[2] in LANG_CODES:
        code = parts[2]
        uid = query.from_user.id
        await asyncio.to_thread(get_db().set_lang, uid, code)
        _LANG_CHOSEN[uid] = code
        _current_lang.set(code)
        await query.answer()
        name, flag = next((n, f) for c, n, f in LANGS if c == code)
        try:
            await query.edit_message_text(f"✅ {flag} {name}")
        except BadRequest:
            pass
        await context.bot.send_message(chat_id=query.message.chat_id, text=T("start.1"), reply_markup=main_keyboard())
        return
    await query.answer()


async def help_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        T("help_button.1", BTN_MUSIC=btn("music"), volume=format_volume(DAILY_QUOTA_MB * MB), REQUEST_COOLDOWN_SECONDS=REQUEST_COOLDOWN_SECONDS),
        reply_markup=main_keyboard(),
    )


async def about_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        T("about_button.1"),
        reply_markup=main_keyboard(),
    )


async def music_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    WAITING_MUSIC_QUERY.add(update.effective_chat.id)
    await update.message.reply_text(
        T("music_button.1"),
        reply_markup=main_keyboard(),
    )


async def handle_music_query(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str):
    if not query_text:
        await update.message.reply_text(T("handle_music_query.1"))
        return

    status_msg = await update.message.reply_text(T("handle_music_query.2", query_text=query_text))
    chat_id = update.effective_chat.id

    try:
        entries = await asyncio.to_thread(search_music_list, query_text, 50, "ytmusic")
    except Exception as e:
        logger.exception("search_music_list failed")
        await status_msg.edit_text(T("handle_music_query.3") + str(e)[:300])
        return

    key = f"{chat_id}_{status_msg.message_id}"
    MUSIC_SESSIONS[key] = {"entries": entries, "query": query_text, "source": "ytmusic"}

    if not entries:
        markup, _ = build_music_page(key, [], 0, "ytmusic")
        await status_msg.edit_text(
            T("handle_music_query.4", query_text=query_text),
            reply_markup=markup,
        )
        return

    markup, total_pages = build_music_page(key, entries, 0, "ytmusic")
    text = (
        T("handle_music_query.5", query_text=query_text, count=len(entries), total_pages=total_pages)
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
        await set_status(query, T("handle_music_page.1"))
        return

    entries = session["entries"]
    source = session.get("source", "ytmusic")
    source_label = T("handle_music_page.2") if source == "ytmusic" else T("handle_music_page.3")
    markup, total_pages = build_music_page(key, entries, page, source)
    text = (
        T("handle_music_page.4", source_label=source_label, query=session['query'], count=len(entries), page=page + 1, total_pages=total_pages)
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
        await set_status(query, T("handle_music_page.1"))
        return

    query_text = session["query"]
    await set_status(query, T("handle_music_youtube_fallback.1", query_text=query_text))

    try:
        entries = await asyncio.to_thread(search_music_list, query_text, 50, "youtube")
    except Exception as e:
        logger.exception("youtube fallback search failed")
        await set_status(query, T("handle_music_query.3") + str(e)[:300])
        return

    session["entries"] = entries
    session["source"] = "youtube"

    if not entries:
        await set_status(query, T("handle_music_youtube_fallback.2", query_text=query_text))
        return

    markup, total_pages = build_music_page(key, entries, 0, "youtube")
    text = (
        T("handle_music_youtube_fallback.3", query_text=query_text, count=len(entries), total_pages=total_pages)
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
        await set_status(query, T("handle_music_page.1"))
        return

    entries = session["entries"]
    if idx < 0 or idx >= len(entries):
        return

    entry = entries[idx]
    video_url = entry.get("url") or entry.get("webpage_url") or entry.get("id")
    if entry.get("id") and not str(video_url).startswith("http"):
        video_url = f"https://www.youtube.com/watch?v={entry['id']}"

    chat_id = query.message.chat_id
    status_msg = await query.message.reply_text(T("handle_music_pick.1"))
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
        if isinstance(e, InstagramLoginWall):
            err_text = str(e)
            try:
                asyncio.create_task(alert_admins_ig_problem(message.get_bot()))
            except Exception:
                logger.exception("could not alert admins about the Instagram login wall")
        elif isinstance(e, YouTubeBotCheck):
            err_text = str(e)
            try:
                asyncio.create_task(alert_admins_yt_problem(message.get_bot()))
            except Exception:
                logger.exception("could not alert admins about the YouTube bot check")
        elif isinstance(e, InstagramAudienceRestricted):
            err_text = str(e)
        else:
            err_text = (
                T("show_quality_picker.1") + str(e)[:300]
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
    caption_lines.append(T("show_quality_picker.2"))
    caption = "\n\n".join(caption_lines) if caption_lines else T("show_quality_picker.3")
    if len(caption) > 1024:  # سقف کپشن عکس در تلگرام
        caption = caption[:1000] + T("show_quality_picker.4")

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

    if await ext_offer(info, options):
        buttons.append([InlineKeyboardButton(T("ext.btn"), callback_data=f"ext|{key}")])

    if offer_find_song:
        buttons.append([InlineKeyboardButton(T("show_quality_picker.5"), callback_data=f"song|{key}")])

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
    count_line = T("playlist_info_text.1", count=len(session['entries']))
    if session["skipped"]:
        count_line += T("playlist_info_text.2", skipped=session['skipped'])
    lines.append(count_line)
    desc = (session["description"] or "").strip()
    if desc:
        lines.append(desc[:250] + ("…" if len(desc) > 250 else ""))
    return "\n".join(lines)


def playlist_total_pages(session: dict) -> int:
    return max(1, math.ceil(len(session["entries"]) / PLAYLIST_PAGE_SIZE))


def playlist_page_text(session: dict, page: int) -> str:
    return (
        T("playlist_page_text.1", playlist_info_text=playlist_info_text(session), page=page + 1, playlist_total_pages=playlist_total_pages(session))
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
        nav.append(InlineKeyboardButton(T("build_music_page.1"), callback_data=f"plpage|{key}|{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(T("build_music_page.2"), callback_data=f"plpage|{key}|{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(T("build_playlist_markup.1"), callback_data=f"pldone|{key}")])
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
            T("show_playlist.1") + str(e)[:300]
        )
        return

    entries, skipped = parse_playlist_entries(info)
    if not entries:
        await status_msg.edit_text(T("show_playlist.2"))
        return

    session = {
        "entries": entries,
        "skipped": skipped,
        "title": info.get("title") or T("show_playlist.3"),
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
        await set_status(query, T("handle_playlist_page.1"))
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
        await set_status(query, T("handle_playlist_page.1"))
        return
    if idx < 0 or idx >= len(session["entries"]):
        return

    entry = session["entries"][idx]
    url = f"https://www.youtube.com/watch?v={entry['id']}"
    chat_id = query.message.chat_id
    status_msg = await query.message.reply_text(T("handle_playlist_pick.1"))
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
    await edit_message_with_markup(query, T("handle_playlist_done.1", base=base), None)


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
        status_msg = await update.message.reply_text(T("handle_message.1"))
        await show_playlist(update.message, chat_id, pl_match.group(0), status_msg)
        return

    match = SUPPORTED_URL_RE.search(text)
    if not match:
        await update.message.reply_text(
            T("handle_message.2", BTN_MUSIC=btn("music"))
        )
        return

    mark_request(_last_message_at, user_id)
    url = match.group(0)
    status_msg = await update.message.reply_text(T("handle_message.3"))
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
        T("warn_slow_down.1", REQUEST_COOLDOWN_SECONDS=REQUEST_COOLDOWN_SECONDS, remaining=remaining)
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
    auto_vip_blocked INTEGER NOT NULL DEFAULT 0,
    lang TEXT
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
);
CREATE TABLE IF NOT EXISTS external_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, day TEXT NOT NULL,
    drive_id TEXT NOT NULL, name TEXT, size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    chat_id INTEGER, message_id INTEGER
);
CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY, value TEXT
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
    auto_vip_blocked INTEGER NOT NULL DEFAULT 0,
    lang TEXT
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
);
CREATE TABLE IF NOT EXISTS external_files (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL, day TEXT NOT NULL,
    drive_id TEXT NOT NULL, name TEXT, size_bytes BIGINT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    chat_id BIGINT, message_id BIGINT
);
CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY, value TEXT
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
                t.exec("ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT")
                t.exec("ALTER TABLE vip_log ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'admin'")
            else:
                cols = {r["name"] for r in t.exec("PRAGMA table_info(users)").fetchall()}
                if "auto_vip_granted" not in cols:
                    t.exec("ALTER TABLE users ADD COLUMN auto_vip_granted INTEGER NOT NULL DEFAULT 0")
                if "auto_vip_blocked" not in cols:
                    t.exec("ALTER TABLE users ADD COLUMN auto_vip_blocked INTEGER NOT NULL DEFAULT 0")
                if "lang" not in cols:
                    t.exec("ALTER TABLE users ADD COLUMN lang TEXT")
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

    def set_lang(self, user_id: int, lang: str):
        self.run(lambda t: t.exec("UPDATE users SET lang=? WHERE user_id=?", (lang, user_id)))

    def count_users(self) -> int:
        row = self.run(lambda t: t.exec("SELECT COUNT(*) AS c FROM users").fetchone())
        return int(row["c"]) if row else 0

    def list_users(self, offset: int, limit: int):
        """کاربرها از جدیدترین به قدیمی‌ترین (برای لیست صفحه‌بندی‌شده‌ی پنل مدیر)."""
        return self.run(
            lambda t: t.exec(
                "SELECT * FROM users ORDER BY joined_at DESC, user_id DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        )

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

    def count_external_today(self, user_id: int) -> int:
        row = self.run(
            lambda t: t.exec(
                "SELECT COUNT(*) AS c FROM external_files WHERE user_id=? AND day=?", (user_id, today_key())
            ).fetchone()
        )
        return int(row["c"]) if row else 0

    def add_external_file(self, user_id: int, drive_id: str, name: str, size_bytes: int, expires_at_iso: str) -> int:
        def fn(t):
            return t.exec(
                "INSERT INTO external_files(user_id, day, drive_id, name, size_bytes, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?) RETURNING id",
                (user_id, today_key(), drive_id, name, size_bytes, to_iso(utcnow()), expires_at_iso),
            ).fetchone()["id"]

        return self.run(fn)

    def set_external_message(self, ext_id: int, chat_id: int, message_id: int):
        self.run(lambda t: t.exec(
            "UPDATE external_files SET chat_id=?, message_id=? WHERE id=?", (chat_id, message_id, ext_id)))

    def due_external_files(self, now_iso: str, limit: int = 50):
        return self.run(lambda t: t.exec(
            "SELECT * FROM external_files WHERE status='active' AND expires_at <= ? ORDER BY expires_at LIMIT ?",
            (now_iso, limit)).fetchall())

    def mark_external_deleted(self, ext_id: int):
        self.run(lambda t: t.exec("UPDATE external_files SET status='deleted' WHERE id=?", (ext_id,)))

    def kv_get(self, key: str):
        row = self.run(lambda t: t.exec("SELECT value FROM kv_store WHERE key=?", (key,)).fetchone())
        return row["value"] if row else None

    def kv_set(self, key: str, value: str):
        self.run(lambda t: t.exec(
            "INSERT INTO kv_store(key, value) VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (key, value)))

    def kv_delete(self, key: str):
        self.run(lambda t: t.exec("DELETE FROM kv_store WHERE key=?", (key,)))

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
        T("quota_problem.1", volume=format_volume(size_bytes), volume2=format_volume(remaining), volume3=format_volume(DAILY_QUOTA_MB * MB))
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
        row = await asyncio.to_thread(get_db().upsert_user, user.id, user.first_name, user.last_name, user.username)
        try:
            lang = row["lang"] if row is not None else None
        except (KeyError, IndexError):
            lang = None
        lang = lang if lang in LANG_CODES else None
        _LANG_CHOSEN[user.id] = lang
        _current_lang.set(lang or DEFAULT_LANG)  # بقیه‌ی هندلرهای همین آپدیت به همین زبان جواب می‌دن
    except Exception:
        logger.exception("track_user failed")


async def account_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_ = get_db()
    row = await asyncio.to_thread(db_.upsert_user, user.id, user.first_name, user.last_name, user.username)
    used, downloads_today = db_.usage_today(user.id)
    vip_end = db_.vip_until(user.id)

    lines = [
        T("account_button.1"),
        "━━━━━━━━━━━━━━",
        T("account_button.2", text=html.escape(display_name(row))),
        T("account_button.3", text=html.escape(username_text(row))),
        T("account_button.4", user_id=user.id),
        T("account_button.5", public_id=row['public_id']),
        T("account_button.6", date=fmt_date(from_iso(row['joined_at']))),
        T("account_button.7", downloads_total=row['downloads_total']),
        "━━━━━━━━━━━━━━",
    ]
    if is_admin(user.id):
        lines.append(T("account_button.8"))
    elif vip_end:
        days_left = max(1, math.ceil((vip_end - utcnow()).total_seconds() / 86400))
        lines.append(T("account_button.9", date=fmt_date(vip_end), days_left=days_left))
    else:
        lines.append(T("account_button.10"))

    if user_is_vip(user.id):
        lines.append(T("account_button.11", volume=format_volume(used)))
    else:
        total = DAILY_QUOTA_MB * MB
        lines.append(
            T("account_button.12", volume=format_volume(used), volume2=format_volume(total), volume3=format_volume(max(0, total - used)))
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=main_keyboard())


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کاربر «آیدی» می‌نویسه -> آیدی ۸ رقمی اختصاصیش رو می‌گیره."""
    user = update.effective_user
    row = await asyncio.to_thread(get_db().upsert_user, user.id, user.first_name, user.last_name, user.username)
    await update.message.reply_text(
        T("id_command.1", public_id=row['public_id'], user_id=user.id),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
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
        ADMIN_STATE.pop(admin_id, None)
        await admin_show_user_list(query, 0)
    elif action.startswith("ul|"):
        try:
            await admin_show_user_list(query, int(action.split("|")[1]))
        except ValueError:
            pass
    elif action.startswith("u|"):
        try:
            _, uid_s, page_s = action.split("|")
            await admin_show_user(query, int(uid_s), int(page_s))
        except ValueError:
            pass
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
        T("build_receipt.1"),
        "━━━━━━━━━━━━━━",
        T("build_receipt.2", receipt_id=info['receipt_id']),
        T("build_receipt.3", text=html.escape(display_name(row)), text2=html.escape(username_text(row))),
        T("build_receipt.4", public_id=row['public_id']),
        T("build_receipt.5", days=days) + (T("build_receipt.6") if info["extended"] else ""),
        (
            T("build_receipt.7", AUTO_VIP_AFTER_DAYS=AUTO_VIP_AFTER_DAYS)
            if info.get("auto")
            else T("build_receipt.8")
        ),
        T("build_receipt.9", date=fmt_date(info['start'], with_time=True)),
        T("build_receipt.10", date=fmt_date(info['end'], with_time=True)),
        "━━━━━━━━━━━━━━",
        T("build_receipt.11", usd=fmt_usd(vip_value_usd(days)), usd2=fmt_usd(vip_value_usd(1))),
        T("build_receipt.12"),
    ]
    return "\n".join(lines)


ADMIN_USERS_PER_PAGE = 5


async def admin_show_user_list(query, page: int):
    """لیست کاربرها، هر صفحه ۵ نفر؛ با دکمه‌ی «بعدی» / «قبلی» بین صفحه‌ها می‌گرده."""
    from telegram.error import BadRequest

    db_ = get_db()
    total = await asyncio.to_thread(db_.count_users)
    pages = max(1, math.ceil(total / ADMIN_USERS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    users = await asyncio.to_thread(db_.list_users, page * ADMIN_USERS_PER_PAGE, ADMIN_USERS_PER_PAGE)
    now = utcnow()
    rows = []
    for u in users:
        days = max(0, (now - from_iso(u["joined_at"])).days)
        label = f"👤 {display_name(u)} {username_text(u)} · {days} روز"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"adm|u|{u['user_id']}|{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"adm|ul|{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"adm|ul|{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 پنل مدیریت", callback_data="adm|cancel")])
    text = (
        f"👥 لیست کاربران (کل: {total})\n"
        f"صفحه {page + 1} از {pages} — برای دیدن مشخصات، روی کاربر بزن:"
        if users
        else "👥 هنوز کاربری ثبت نشده."
    )
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except BadRequest:
        pass


async def admin_show_user(query, user_id: int, page: int):
    """مشخصات یه کاربر (از روی لیست)، با دکمه‌ی برگشت به همون صفحه‌ی لیست."""
    from telegram.error import BadRequest

    row = await asyncio.to_thread(get_db().get_user, user_id)
    back = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 بازگشت به لیست", callback_data=f"adm|ul|{page}")],
        [InlineKeyboardButton("🏠 پنل مدیریت", callback_data="adm|cancel")],
    ])
    if not row:
        text, mode = "❌ این کاربر پیدا نشد.", None
    else:
        text, mode = await asyncio.to_thread(build_user_info, row), ParseMode.HTML
    try:
        await query.edit_message_text(text, parse_mode=mode, reply_markup=back)
    except BadRequest:
        pass


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
        "🌐 زبان: " + next((n for c, n, _ in LANGS if c == row["lang"]), "انتخاب نشده"),
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
        with lang_scope(row["lang"] if row else None):
            notice = T("vip_cancelled.user", volume=format_volume(DAILY_QUOTA_MB * MB))
        await context.bot.send_message(chat_id=target, text=notice)
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
    with lang_scope("fa"):  # فیش مدیر همیشه فارسیه
        receipt = build_receipt(row, info)
    ADMIN_STATE.pop(admin_id, None)

    delivered = True
    try:
        with lang_scope(row["lang"] if row else None):  # فیش کاربر به زبان خودش
            user_text = T("vip_granted.user", receipt=build_receipt(row, info), date=fmt_date(info["end"]))
        await context.bot.send_message(chat_id=target_user_id, text=user_text, parse_mode=ParseMode.HTML)
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
                with lang_scope(fresh["lang"] if fresh else None):  # به زبان خود کاربر
                    auto_text = (
                        T("run_auto_vip_check.1", AUTO_VIP_AFTER_DAYS=AUTO_VIP_AFTER_DAYS, AUTO_VIP_DAYS=AUTO_VIP_DAYS)
                        + build_receipt(fresh, info)
                        + T("run_auto_vip_check.2", date=fmt_date(info['end']))
                    )
                await bot_.send_message(chat_id=uid, text=auto_text, parse_mode=ParseMode.HTML)
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
    application.bot_data["ext_cleanup_task"] = asyncio.create_task(external_cleanup_loop(application))
    try:
        if await asyncio.to_thread(load_cookies_from_db):
            logger.info("cookies: loaded the cookie file stored by an admin")
    except Exception:
        logger.exception("loading stored cookies failed")


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
            T("job_allowed.1"),
            show_alert=True,
        )
        return False
    remaining = cooldown_remaining(_last_job_at, query.from_user.id)
    if remaining:
        await query.answer(
            T("job_allowed.2", remaining=remaining),
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
    return InlineKeyboardMarkup([[InlineKeyboardButton(T("link_markup.1"), url=normalize_link(url))]])


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
        await set_status(query, T("cleanup_after_send.1"))
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
        await set_status(query, T("handle_quality_choice.1"))
        return

    entry = PENDING.get(key)
    if not entry:
        await set_status(query, T("handle_quality_choice.2"))
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
    await set_status(query, T("handle_quality_choice.3"))

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
                T("handle_quality_choice.4", size_mb=size_mb, MAX_TELEGRAM_UPLOAD_MB=MAX_TELEGRAM_UPLOAD_MB),
            )
            return

        file_bytes = os.path.getsize(file_path)
        problem = quota_problem(query.from_user.id, file_bytes)
        if problem:
            await set_status(query, problem)
            return

        sending_text = (
            T("handle_quality_choice.5", size_mb=size_mb)
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
        await set_status(query, T("handle_quality_choice.6") + str(e)[:300])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        PENDING.pop(key, None)


# --------------------------------------------------------------------------------------
# کوکی‌ها: ربات فایل کوکی رو می‌خونه و می‌فهمه مال اینستاگرامه یا یوتیوب/گوگل؛ هر کدوم جدا ذخیره می‌شه،
# پس با فرستادن کوکی یوتیوب، کوکی اینستاگرام پاک نمی‌شه (و برعکس). فایل ترکیبی برای yt-dlp ساخته می‌شه.
# --------------------------------------------------------------------------------------
COOKIE_LABELS = {"instagram": "📸 اینستاگرام", "youtube": "▶️ یوتیوب/گوگل", "other": "🌐 بقیه‌ی سایت‌ها"}
YT_LOGIN_COOKIES = ("SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "__Secure-1PAPISID", "LOGIN_INFO")
_COOKIE_STORE = {"instagram": [], "youtube": [], "other": []}  # خط‌های فرمت Netscape
_COOKIE_META = {}  # platform -> {"updated": iso یا None, "source": "admin"|"env"}
COOKIE_TIP_IG = (
    "نکته: بعد از export از اینستاگرام «Logout» نزن؛ خروج نشست رو باطل می‌کنه. بهترین راه: توی یه پنجره‌ی "
    "ناشناس (Incognito) لاگین کن، کوکی رو export بگیر و پنجره رو بدون Logout ببند. از حساب جداگانه استفاده کن."
)
COOKIE_TIP_YT = (
    "نکته: توی پنجره‌ی ناشناس (Incognito) لاگین یوتیوب کن، تب رو به youtube.com/robots.txt ببر، کوکی "
    "youtube.com رو export بگیر و پنجره رو ببند (Logout نزن)؛ این‌طوری یوتیوب کوکی رو عوض نمی‌کنه."
)
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _cookie_platform(domain: str) -> str:
    d = domain.lstrip(".").lower()
    if d.endswith("instagram.com"):
        return "instagram"
    if d.endswith(("youtube.com", "google.com")):
        return "youtube"
    return "other"


def _parse_cookies(text: str) -> list:
    """فایل Netscape رو می‌خونه؛ هر کوکی: platform, domain, name, value, expiry, line (خط اصلی)."""
    out = []
    for raw in text.splitlines():
        body = raw.strip()
        if not body:
            continue
        if body.startswith("#HttpOnly_"):
            fields = body[len("#HttpOnly_"):]
        elif body.startswith("#"):
            continue
        else:
            fields = body
        parts = fields.split("\t")
        if len(parts) < 7:
            continue
        try:
            expiry = int(float(parts[4] or 0))
        except ValueError:
            expiry = 0
        out.append({"platform": _cookie_platform(parts[0]), "domain": parts[0], "name": parts[5],
                    "value": parts[6], "expiry": expiry, "line": body})
    return out


def _login_cookies(platform: str) -> list:
    names = ("sessionid",) if platform == "instagram" else YT_LOGIN_COOKIES
    return [c for c in _parse_cookies("\n".join(_COOKIE_STORE[platform])) if c["name"] in names]


def _expiry_state(found: list):
    """('none'|'expired'|'ok'|'session', روزهای مونده)."""
    if not found:
        return "none", None
    exps = [c["expiry"] for c in found if c["expiry"]]
    if exps and min(exps) < time.time():
        return "expired", None
    if exps:
        return "ok", int((min(exps) - time.time()) // 86400)
    return "session", None


def _jar(platform: str) -> dict:
    return {c["name"]: c["value"] for c in _parse_cookies("\n".join(_COOKIE_STORE[platform]))}


def _write_merged_cookies() -> bool:
    global COOKIES_FILE
    lines = [ln for p in ("instagram", "youtube", "other") for ln in _COOKIE_STORE[p]]
    if not lines:
        return False
    path = os.path.join(tempfile.gettempdir(), "ytdlp_cookies.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n")
    COOKIES_FILE = path
    return True


def load_cookies_from_db() -> bool:
    """موقع بالا اومدن: کوکی‌های متغیر محیطی (پایه) + کوکی‌هایی که مدیر فرستاده (جایگزین هر پلتفرم)."""
    for p in _COOKIE_STORE:
        _COOKIE_STORE[p] = []
    _COOKIE_META.clear()
    if _cookies_available():
        try:
            with open(COOKIES_FILE, encoding="utf-8", errors="ignore") as f:
                for c in _parse_cookies(f.read()):
                    _COOKIE_STORE[c["platform"]].append(c["line"])
            for p, lines in _COOKIE_STORE.items():
                if lines:
                    _COOKIE_META[p] = {"updated": None, "source": "env"}
        except Exception:
            logger.exception("could not read the base cookie file")
    db_ = get_db()
    try:
        legacy = db_.kv_get("cookies_txt")  # نسخه‌ی قبلی که یه فایل واحد نگه می‌داشت
        if legacy:
            by = {}
            for c in _parse_cookies(legacy):
                by.setdefault(c["platform"], []).append(c["line"])
            for p, lines in by.items():
                db_.kv_set(f"cookies_{p}", "\n".join(lines))
                db_.kv_set(f"cookies_ts_{p}", to_iso(utcnow()))
            db_.kv_delete("cookies_txt")
        for p in _COOKIE_STORE:
            text = db_.kv_get(f"cookies_{p}")
            if text:
                _COOKIE_STORE[p] = [ln for ln in text.splitlines() if ln.strip()]
                _COOKIE_META[p] = {"updated": db_.kv_get(f"cookies_ts_{p}"), "source": "admin"}
    except Exception:
        logger.exception("could not read stored cookies")
    return _write_merged_cookies()


def instagram_session_check():
    """از خود اینستاگرام می‌پرسه با این کوکی لاگینیم یا نه: ("ok"|"invalid"|"limited"|"unknown"|"none", توضیح)."""
    jar = _jar("instagram")
    if not _COOKIE_STORE["instagram"]:
        return "none", ""
    if "sessionid" not in jar:
        return "invalid", "no sessionid"
    headers = {
        "User-Agent": _UA, "X-IG-App-ID": "936619743392459", "X-CSRFToken": jar.get("csrftoken", ""),
        "X-Requested-With": "XMLHttpRequest", "Referer": "https://www.instagram.com/",
    }
    try:
        r = requests.get("https://www.instagram.com/api/v1/accounts/current_user/?edit=true",
                         cookies=jar, headers=headers, timeout=20, allow_redirects=False)
    except requests.RequestException as e:
        return "unknown", type(e).__name__
    if r.status_code == 200:
        try:
            return "ok", r.json()["user"]["username"]
        except Exception:
            return "ok", ""
    if r.status_code in (301, 302, 303, 307, 308, 400, 401, 403):
        return "invalid", f"HTTP {r.status_code}"
    if r.status_code == 429:
        return "limited", "HTTP 429"
    return "unknown", f"HTTP {r.status_code}"


def youtube_session_check():
    """صفحه‌ی اصلی یوتیوب رو با کوکی می‌گیره و نشون «LOGGED_IN» رو می‌خونه."""
    if not _COOKIE_STORE["youtube"]:
        return "none", ""
    jar = _jar("youtube")
    if not any(n in jar for n in YT_LOGIN_COOKIES):
        return "invalid", "no login cookie"
    jar.setdefault("SOCS", "CAI")  # صفحه‌ی رضایت اروپا رو رد می‌کنه
    try:
        r = requests.get("https://www.youtube.com/", cookies=jar,
                         headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=25)
    except requests.RequestException as e:
        return "unknown", type(e).__name__
    if '"LOGGED_IN":true' in r.text:
        return "ok", ""
    if '"LOGGED_IN":false' in r.text:
        return "invalid", "not logged in"
    if r.status_code == 429:
        return "limited", "HTTP 429"
    return "unknown", f"HTTP {r.status_code}"


def session_check_text(platform: str, state: str, detail: str) -> str:
    who = "اینستاگرام" if platform == "instagram" else "یوتیوب"
    if state == "none":
        return ""
    if state == "ok":
        return f"تست زنده: ✅ {who} این نشست رو قبول می‌کنه" + (f" (@{detail})" if detail else "")
    if state == "invalid":
        return f"تست زنده: ❌ {who} این نشست رو قبول نمی‌کنه ({detail})"
    if state == "limited":
        return f"تست زنده: ⚠️ {who} موقتاً محدودیت سرعت گذاشته ({detail})"
    return f"تست زنده: نامشخص ({detail})"


def _ago(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    if d < 90:
        return "همین الان"
    if d < 5400:
        return f"{d // 60} دقیقه پیش"
    if d < 172800:
        return f"{d // 3600} ساعت پیش"
    return f"{d // 86400} روز پیش"


async def cookie_report() -> str:
    """گزارش وضعیت کوکی اینستاگرام و یوتیوب (با تست زنده)."""
    ig_live, yt_live = await asyncio.gather(
        asyncio.to_thread(instagram_session_check), asyncio.to_thread(youtube_session_check)
    )
    lives = {"instagram": ig_live, "youtube": yt_live}
    problems = {"instagram": _IG_WALL, "youtube": _YT_PROBLEM}
    out = ["🍪 وضعیت کوکی‌ها"]
    bad_platforms = []
    for p in ("instagram", "youtube"):
        out.append("")
        if not _COOKIE_STORE[p]:
            out.append(COOKIE_LABELS[p] + "\n• کوکی ندارم ❌ — فایل کوکی بفرست")
            bad_platforms.append(p)
            continue
        lines, bad = [COOKIE_LABELS[p]], False
        state, days = _expiry_state(_login_cookies(p))
        if state == "none":
            lines.append("• کوکیِ ورود توش نیست ❌")
            bad = True
        elif state == "expired":
            lines.append("• کوکی‌های ورود منقضی شدن ❌")
            bad = True
        elif state == "ok":
            lines.append(f"• کوکی ورود هست (تا حدود {days} روز دیگه)")
        else:
            lines.append("• کوکی ورود هست (نشست مرورگر)")
        meta = _COOKIE_META.get(p, {})
        if meta.get("updated"):
            try:
                lines.append("• آخرین بروزرسانی: " + _ago(from_iso(meta["updated"]).timestamp()))
            except Exception:
                pass
        elif meta.get("source") == "env":
            lines.append("• منبع: تنظیمات سرور (COOKIES_FILE_B64)")
        lv_state, lv_detail = lives[p]
        live = session_check_text(p, lv_state, lv_detail)
        if live:
            lines.append("• " + live)
        if lv_state == "invalid":
            bad = True
        prob = problems[p]
        if prob["count"]:
            lines.append(f"• خطای ثبت‌شده از زمان بالا اومدن ربات: {prob['count']} بار؛ آخرینش {_ago(prob['at'])}")
            if time.time() - prob["at"] < 1800 and lv_state != "ok":
                bad = True
        lines.append("➡️ نتیجه: " + ("❌ باید کوکی عوض بشه" if bad else "✅ سالم به نظر می‌رسه"))
        if bad:
            bad_platforms.append(p)
        out.extend(lines)
    if _COOKIE_STORE["other"]:
        out.append(f"\n🌐 کوکی سایت‌های دیگه: {len(_COOKIE_STORE['other'])} تا")
    for p in bad_platforms:
        out.append("\n" + (COOKIE_TIP_IG if p == "instagram" else COOKIE_TIP_YT))
    return "\n".join(out)


async def _alert_admins(bot_, state: dict, headline: str):
    if time.time() - state["last_alert"] < 3600:  # حداکثر ساعتی یک بار
        return
    state["last_alert"] = time.time()
    text = headline + "\n\n" + await cookie_report() + "\n\nفایل cookies.txt تازه رو همین‌جا برای ربات بفرست."
    for admin_id in ADMIN_IDS:
        try:
            await bot_.send_message(chat_id=admin_id, text=text)
        except Exception:
            pass


async def alert_admins_ig_problem(bot_):
    await _alert_admins(bot_, _IG_WALL, "⚠️ اینستاگرام لینک‌ها رو برای ربات رد می‌کنه (ورود لازمه / نشست باطله).")


async def alert_admins_yt_problem(bot_):
    await _alert_admins(bot_, _YT_PROBLEM, "⚠️ یوتیوب ربات رو شناسایی کرده («ثابت کن ربات نیستی»).")


async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cookies (فقط مدیر): وضعیت کوکی اینستاگرام و یوتیوب."""
    if not is_admin(update.effective_user.id):
        return
    wait = await update.message.reply_text("⏳ دارم کوکی‌ها رو تست می‌کنم...")
    text = await cookie_report() + "\n\nبرای عوض کردن کوکی، فایل cookies.txt رو همین‌جا برای ربات بفرست."
    try:
        await wait.edit_text(text)
    except Exception:
        await update.message.reply_text(text)


async def cookies_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فایل کوکی مدیر رو می‌خونه، پلتفرم‌ها رو تشخیص می‌ده و هر کدوم رو جدا بررسی/ذخیره می‌کنه."""
    user = update.effective_user
    doc = update.message.document
    if not user or not is_admin(user.id) or not doc or (doc.file_size or 0) > 1_000_000:
        return
    try:
        tg_file = await doc.get_file()
        text = bytes(await tg_file.download_as_bytearray()).decode("utf-8", errors="ignore")
    except Exception:
        return
    parsed = _parse_cookies(text)
    if not parsed:
        if "# Netscape HTTP Cookie File" in text:
            await update.message.reply_text("❌ توی این فایل هیچ کوکی‌ای پیدا نکردم.")
        return  # فایل دیگه‌ای بوده؛ کاری باهاش نداریم
    by = {}
    for c in parsed:
        by.setdefault(c["platform"], []).append(c)
    results, saved = [], []
    for p in ("instagram", "youtube", "other"):
        items = by.get(p)
        if not items:
            continue
        label = COOKIE_LABELS[p]
        if p != "other":
            names = ("sessionid",) if p == "instagram" else YT_LOGIN_COOKIES
            found = [c for c in items if c["name"] in names]
            if not found:
                results.append(f"{label}: ❌ کوکیِ ورود توش نیست (موقع export لاگین نبودی). کوکی قبلی دست‌نخورده موند؛ لاگین کن و دوباره export بگیر.")
                continue
            exps = [c["expiry"] for c in found if c["expiry"]]
            if exps and min(exps) < time.time():
                results.append(f"{label}: ❌ کوکی‌های این فایل منقضی شدن. کوکی قبلی دست‌نخورده موند؛ تازه export بگیر.")
                continue
        _COOKIE_STORE[p] = [c["line"] for c in items]
        _COOKIE_META[p] = {"updated": to_iso(utcnow()), "source": "admin"}
        await asyncio.to_thread(get_db().kv_set, f"cookies_{p}", "\n".join(_COOKIE_STORE[p]))
        await asyncio.to_thread(get_db().kv_set, f"cookies_ts_{p}", _COOKIE_META[p]["updated"])
        saved.append(p)
    if saved:
        await asyncio.to_thread(_write_merged_cookies)
        _IG_WALL["last_alert"] = 0.0
        _YT_PROBLEM["last_alert"] = 0.0
    tips = []
    for p in saved:
        if p == "other":
            results.append(f"{COOKIE_LABELS[p]}: ✅ {len(_COOKIE_STORE[p])} کوکی ذخیره شد.")
            continue
        state, detail = await asyncio.to_thread(instagram_session_check if p == "instagram" else youtube_session_check)
        live = session_check_text(p, state, detail)
        results.append(f"{COOKIE_LABELS[p]}: ✅ ذخیره و فعال شد.\n   {live}")
        if state == "invalid":
            results[-1] += "\n   ⚠️ ذخیره شد ولی به نظر کار نمی‌کنه؛ کوکی تازه‌ای بده."
            tips.append(COOKIE_TIP_IG if p == "instagram" else COOKIE_TIP_YT)
    await update.message.reply_text("🍪 نتیجه‌ی فایل کوکی:\n\n" + "\n\n".join(results) + ("\n\n" + "\n".join(tips) if tips else ""))
    try:
        await update.message.delete()  # فایل کوکی توی چت نمونه
    except Exception:
        pass


# ------------------------------------------------------------------
# دانلود خارجی (گوگل درایو): ویدیوهای بلند/سنگین رو آپلود می‌کنه و یه لینک مستقیمِ موقت می‌ده
# ------------------------------------------------------------------
import mimetypes

GDRIVE_CLIENT_ID = os.environ.get("GDRIVE_CLIENT_ID", "").strip()
GDRIVE_CLIENT_SECRET = os.environ.get("GDRIVE_CLIENT_SECRET", "").strip()
GDRIVE_ENV_REFRESH_TOKEN = os.environ.get("GDRIVE_REFRESH_TOKEN", "").strip()  # اختیاری؛ وگرنه با /gdrive تو دیتابیس ذخیره می‌شه
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "").strip()  # اختیاری: پوشه‌ی مقصد
GDRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"  # فقط فایل‌هایی که خود ربات می‌سازه
GDRIVE_TOKEN_URL = os.environ.get("GDRIVE_TOKEN_URL", "https://oauth2.googleapis.com/token")
GDRIVE_DEVICE_URL = os.environ.get("GDRIVE_DEVICE_URL", "https://oauth2.googleapis.com/device/code")
GDRIVE_API = os.environ.get("GDRIVE_API_BASE", "https://www.googleapis.com")

# قانون‌ها (همه با متغیر محیطی قابل تغییرن)
EXT_MIN_DURATION_SECONDS = int(os.environ.get("EXT_MIN_DURATION_SECONDS", "600"))  # دکمه فقط برای ویدیوی بیشتر از ۱۰ دقیقه
EXT_MIN_SIZE_MB = int(os.environ.get("EXT_MIN_SIZE_MB", "100"))                    # ... و حجم زیاد
EXT_NORMAL_MB = int(os.environ.get("EXT_NORMAL_MB", "800"))        # کاربر عادی: تا ۸۰۰ مگ
EXT_NORMAL_TOL_MB = int(os.environ.get("EXT_NORMAL_TOL_MB", "10"))  # ... و تا ۸۱۰ مگ اشکالی نداره
EXT_VIP_MB = int(os.environ.get("EXT_VIP_MB", "1500"))             # VIP: ۱ گیگ و ۵۰۰ مگ
EXT_VIP_TOL_MB = int(os.environ.get("EXT_VIP_TOL_MB", "100"))      # ... و تا ۱۶۰۰ مگ اشکالی نداره
EXT_NORMAL_MINUTES = int(os.environ.get("EXT_NORMAL_MINUTES", "15"))  # مهلت دانلود لینک
EXT_VIP_MINUTES = int(os.environ.get("EXT_VIP_MINUTES", "30"))
EXT_NORMAL_DAILY = int(os.environ.get("EXT_NORMAL_DAILY", "2"))    # تعداد در روز
EXT_VIP_DAILY = int(os.environ.get("EXT_VIP_DAILY", "10"))
EXT_MAX_CONCURRENT = int(os.environ.get("EXT_MAX_CONCURRENT", "2"))  # همزمانی کل (حفاظت از دیسک/CPU سرور)
EXT_CLEANUP_SECONDS = 30


class GDriveError(Exception):
    pass


_gd_lock = threading.Lock()
_gd_token = {"value": None, "exp": 0.0}
_ext_active = set()
_gdrive_flow_running = False


def _gd_err(resp) -> str:
    """متن کوتاهِ خطا؛ هیچ توکنی داخلش نیست."""
    try:
        j = resp.json()
        err = j.get("error")
        if isinstance(err, dict):
            err = err.get("message") or err.get("status")
        return f"HTTP {resp.status_code} {str(err or j.get('error_description') or '')[:100]}".strip()
    except Exception:
        return f"HTTP {resp.status_code}"


def gdrive_refresh_token() -> str:
    if GDRIVE_ENV_REFRESH_TOKEN:
        return GDRIVE_ENV_REFRESH_TOKEN
    try:
        return get_db().kv_get("gdrive_refresh_token") or ""
    except Exception:
        return ""


def gdrive_ready() -> bool:
    return bool(GDRIVE_CLIENT_ID and GDRIVE_CLIENT_SECRET and gdrive_refresh_token())


def gdrive_access_token(force: bool = False) -> str:
    with _gd_lock:
        if not force and _gd_token["value"] and time.time() < _gd_token["exp"] - 60:
            return _gd_token["value"]
        refresh = gdrive_refresh_token()
        if not refresh:
            raise GDriveError("Google Drive is not connected")
        r = requests.post(
            GDRIVE_TOKEN_URL,
            data={
                "client_id": GDRIVE_CLIENT_ID, "client_secret": GDRIVE_CLIENT_SECRET,
                "refresh_token": refresh, "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise GDriveError("token refresh failed: " + _gd_err(r))
        j = r.json()
        _gd_token["value"] = j["access_token"]
        _gd_token["exp"] = time.time() + int(j.get("expires_in", 3600))
        return _gd_token["value"]


def _gd_call(method: str, url: str, **kw):
    timeout = kw.pop("timeout", 60)
    base_headers = kw.pop("headers", None) or {}
    r = None
    for attempt in (1, 2):
        headers = dict(base_headers)
        headers["Authorization"] = "Bearer " + gdrive_access_token(force=(attempt == 2))
        r = requests.request(method, url, headers=headers, timeout=timeout, **kw)
        if r.status_code != 401:
            break
    return r


def _next_offset(resp) -> int:
    rng = resp.headers.get("Range") or ""
    m = re.search(r"bytes=0-(\d+)", rng)
    return int(m.group(1)) + 1 if m else 0


def gdrive_upload(path: str, name: str, progress=None) -> str:
    """آپلود قابل‌ازسرگیری (resumable) تکه‌تکه؛ شناسه‌ی فایل رو برمی‌گردونه."""
    size = os.path.getsize(path)
    if size <= 0:
        raise GDriveError("empty file")
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    meta = {"name": name}
    if GDRIVE_FOLDER_ID:
        meta["parents"] = [GDRIVE_FOLDER_ID]
    r = _gd_call(
        "POST", f"{GDRIVE_API}/upload/drive/v3/files?uploadType=resumable&fields=id", json=meta,
        headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
    )
    if r.status_code != 200 or not r.headers.get("Location"):
        raise GDriveError("could not start upload: " + _gd_err(r))
    session = r.headers["Location"]
    chunk = 16 * 1024 * 1024  # مضرب ۲۵۶ کیلوبایت
    offset, fails = 0, 0
    with open(path, "rb") as fh:
        while True:
            fh.seek(offset)
            data = fh.read(min(chunk, size - offset))
            end = offset + len(data) - 1
            try:
                resp = requests.put(
                    session, data=data, timeout=300,
                    headers={"Content-Range": f"bytes {offset}-{end}/{size}"},
                )
            except requests.RequestException:
                resp = None
            if resp is not None and resp.status_code in (200, 201):
                if progress:
                    progress(size, size)
                return resp.json()["id"]
            if resp is not None and resp.status_code == 308:
                offset, fails = _next_offset(resp), 0
                if progress:
                    progress(offset, size)
                continue
            fails += 1
            retryable = resp is None or resp.status_code in (408, 429, 500, 502, 503, 504)
            if fails > 5 or not retryable:
                raise GDriveError("upload failed: " + (_gd_err(resp) if resp is not None else "network error"))
            time.sleep(min(2 ** fails, 30))
            try:  # از سرور بپرس تا کجا رسیده
                st = requests.put(session, headers={"Content-Range": f"bytes */{size}"}, data=b"", timeout=60)
                if st.status_code in (200, 201):
                    return st.json()["id"]
                if st.status_code == 308:
                    offset = _next_offset(st)
            except requests.RequestException:
                pass


def gdrive_share_public(file_id: str) -> None:
    r = _gd_call("POST", f"{GDRIVE_API}/drive/v3/files/{file_id}/permissions", json={"role": "reader", "type": "anyone"})
    if r.status_code not in (200, 201):
        raise GDriveError("could not share file: " + _gd_err(r))


def gdrive_delete(file_id: str) -> bool:
    """حذف دائمی (بدون سطل زباله)؛ ۴۰۴ یعنی قبلاً پاک شده."""
    r = _gd_call("DELETE", f"{GDRIVE_API}/drive/v3/files/{file_id}")
    if r.status_code in (200, 204, 404):
        return True
    raise GDriveError("could not delete file: " + _gd_err(r))


def gdrive_direct_link(file_id: str) -> str:
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


def ext_tier(user_id: int) -> dict:
    """سقف‌ها و مهلت‌ها بر اساس نوع کاربر (مدیر مثل VIP ولی بدون سقف روزانه)."""
    if user_is_vip(user_id):
        return {
            "vip": True, "limit": EXT_VIP_MB * MB, "hard": (EXT_VIP_MB + EXT_VIP_TOL_MB) * MB,
            "minutes": EXT_VIP_MINUTES, "daily": None if is_admin(user_id) else EXT_VIP_DAILY,
        }
    return {
        "vip": False, "limit": EXT_NORMAL_MB * MB, "hard": (EXT_NORMAL_MB + EXT_NORMAL_TOL_MB) * MB,
        "minutes": EXT_NORMAL_MINUTES, "daily": EXT_NORMAL_DAILY,
    }


def ext_pick_option(options, hard_bytes: int):
    """بهترین کیفیتی که تو سقف جا می‌شه؛ اگه حجم هیچ‌کدوم معلوم نیست، کمترین کیفیت (بعداً حجم واقعی چک می‌شه)."""
    video = [o for o in options if o.get("kind") == "video"]
    fits = [o for o in video if o.get("size") and o["size"] <= hard_bytes]
    if fits:
        return fits[0]
    unknown = [o for o in video if not o.get("size")]
    return unknown[-1] if unknown else None


async def ext_offer(info: dict, options) -> bool:
    """دکمه‌ی «دانلود خارجی» فقط برای ویدیوی بلند و سنگین، و فقط وقتی درایو وصله."""
    if (info.get("duration") or 0) < EXT_MIN_DURATION_SECONDS:
        return False
    sizes = [o["size"] for o in options if o.get("kind") == "video" and o.get("size")]
    if sizes and max(sizes) < EXT_MIN_SIZE_MB * MB:
        return False
    return await asyncio.to_thread(gdrive_ready)


class UploadState:
    def __init__(self, total: int):
        self.total = total
        self.sent = 0
        self.started = time.monotonic()

    def update(self, sent: int, total: int):
        self.sent, self.total = sent, total

    def percent(self) -> float:
        return self.sent / self.total * 100 if self.total else 0.0

    def render(self) -> str:
        elapsed = max(time.monotonic() - self.started, 0.001)
        speed = self.sent / elapsed
        line2 = f"{format_size(self.sent) or '0KB'} / {format_size(self.total)}"
        if self.sent and speed:
            line2 += f"  •  {format_size(speed)}/s"
        return T("ext.uploading", bar=make_progress_bar(self.percent()), percent=self.percent(), line2=line2)


async def run_upload_progress(query, state: UploadState, stop_event: asyncio.Event):
    last = -100.0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=PROGRESS_UPDATE_INTERVAL * 2)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        if abs(state.percent() - last) < 4:
            continue
        last = state.percent()
        await set_status(query, state.render())


async def handle_external_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    try:
        _, key = query.data.split("|", 1)
    except ValueError:
        await query.answer()
        return
    entry = PENDING.get(key)
    if not entry:
        await query.answer(T("handle_quality_choice.2"), show_alert=True)
        return
    if not await asyncio.to_thread(gdrive_ready):
        await query.answer(T("ext.unavailable"), show_alert=True)
        return

    db_ = get_db()
    tier = ext_tier(uid)
    used = await asyncio.to_thread(db_.count_external_today, uid)
    vip_hint = "" if tier["vip"] else T(
        "ext.vip_hint", vip_limit=EXT_VIP_DAILY, vip_volume=format_volume(EXT_VIP_MB * MB)
    )
    if tier["daily"] is not None and used >= tier["daily"]:
        await query.answer()
        await query.message.reply_text(T("ext.limit_reached", used=used, limit=tier["daily"]) + vip_hint)
        return
    if uid in _ext_active or len(_ext_active) >= EXT_MAX_CONCURRENT:
        await query.answer(T("ext.busy"), show_alert=True)
        return
    remaining = cooldown_remaining(_last_job_at, uid)
    if remaining:
        await query.answer(T("job_allowed.2", remaining=remaining), show_alert=True)
        return
    opt = ext_pick_option(list(entry.get("options", {}).values()), tier["hard"])
    if not opt:
        _last_job_at.pop(uid, None)
        await query.answer()
        await query.message.reply_text(T("ext.too_big", limit=format_volume(tier["limit"])) + vip_hint)
        return

    await query.answer()
    _ext_active.add(uid)
    tmp_dir = tempfile.mkdtemp(prefix="ext_")
    drive_id = None
    ok = False
    try:
        state = ProgressState()
        await set_status(query, T("ext.start", quality=opt["label"]))
        stop_event = asyncio.Event()
        progress_task = asyncio.create_task(run_progress_updates(query, state, stop_event))
        try:
            file_path = await asyncio.to_thread(download_media, entry["url"], opt["format_id"], "video", tmp_dir, state)
        finally:
            stop_event.set()
            await progress_task

        size = os.path.getsize(file_path)
        if size > tier["hard"]:
            await set_status(query, T("ext.too_big_after", volume=format_volume(size), limit=format_volume(tier["limit"])))
            _last_job_at.pop(uid, None)
            return

        name = os.path.basename(file_path)
        up = UploadState(size)
        stop_event = asyncio.Event()
        progress_task = asyncio.create_task(run_upload_progress(query, up, stop_event))
        try:
            drive_id = await asyncio.to_thread(gdrive_upload, file_path, name, up.update)
        finally:
            stop_event.set()
            await progress_task
        await asyncio.to_thread(gdrive_share_public, drive_id)

        expires = utcnow() + timedelta(minutes=tier["minutes"])
        ext_id = await asyncio.to_thread(db_.add_external_file, uid, drive_id, name, size, to_iso(expires))
        ok = True  # از اینجا به بعد پاک‌کردن فایل با کار دوره‌ای انجام می‌شه
        text = T(
            "ext.ready", name=name[:80], volume=format_volume(size), minutes=tier["minutes"],
            time=to_local(expires).strftime("%H:%M"), used=used + 1,
            limit=tier["daily"] if tier["daily"] is not None else "∞",
        )
        sent = await query.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(T("ext.link_btn"), url=gdrive_direct_link(drive_id))]])
        )
        try:
            await asyncio.to_thread(db_.set_external_message, ext_id, sent.chat_id, sent.message_id)
        except Exception:
            logger.exception("could not store external message id")
        record_download(uid)
        await cleanup_after_send(context, query, entry.get("user_msg_id"))
        PENDING.pop(key, None)
    except Exception as e:
        logger.exception("external download failed")
        _last_job_at.pop(uid, None)
        if drive_id and not ok:
            try:
                await asyncio.to_thread(gdrive_delete, drive_id)
            except Exception:
                logger.warning("could not remove a partially shared drive file")
        await set_status(query, T("ext.failed") + str(e)[:300])
    finally:
        _ext_active.discard(uid)
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def run_external_cleanup(bot_) -> int:
    """فایل‌های منقضی‌شده رو از درایو پاک می‌کنه و پیام لینک رو به «منقضی شد» تبدیل می‌کنه."""
    if not await asyncio.to_thread(gdrive_ready):
        return 0
    db_ = get_db()
    rows = await asyncio.to_thread(db_.due_external_files, to_iso(utcnow()))
    done = 0
    for row in rows:
        try:
            await asyncio.to_thread(gdrive_delete, row["drive_id"])
        except Exception as e:
            logger.warning("drive cleanup failed for #%s (%s); will retry", row["id"], str(e)[:120])
            continue
        await asyncio.to_thread(db_.mark_external_deleted, row["id"])
        done += 1
        if row["chat_id"] and row["message_id"]:
            try:
                urow = await asyncio.to_thread(db_.get_user, row["user_id"])
                with lang_scope(urow["lang"] if urow else None):
                    text = T("ext.expired")
                await bot_.edit_message_text(chat_id=row["chat_id"], message_id=row["message_id"], text=text)
            except Exception:
                pass
    return done


async def external_cleanup_loop(application):
    await asyncio.sleep(5)
    while True:
        try:
            await run_external_cleanup(application.bot)
        except Exception:
            logger.exception("external cleanup failed")
        await asyncio.sleep(EXT_CLEANUP_SECONDS)


def gdrive_device_start() -> dict:
    r = requests.post(GDRIVE_DEVICE_URL, data={"client_id": GDRIVE_CLIENT_ID, "scope": GDRIVE_SCOPE}, timeout=30)
    if r.status_code != 200:
        raise GDriveError("device code request failed: " + _gd_err(r))
    return r.json()


def gdrive_device_poll(device_code: str):
    r = requests.post(
        GDRIVE_TOKEN_URL,
        data={
            "client_id": GDRIVE_CLIENT_ID, "client_secret": GDRIVE_CLIENT_SECRET,
            "device_code": device_code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
        timeout=30,
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


async def _gdrive_device_flow(bot_, chat_id: int):
    global _gdrive_flow_running
    _gdrive_flow_running = True
    try:
        j = await asyncio.to_thread(gdrive_device_start)
        wait_min = max(1, int(j.get("expires_in", 900)) // 60)
        await bot_.send_message(
            chat_id=chat_id,
            text=(
                "🔗 وصل کردن گوگل درایو\n\n"
                f"1) این لینک رو باز کن: {j.get('verification_url') or j.get('verification_uri')}\n"
                f"2) این کد رو وارد کن: {j['user_code']}\n"
                "3) با حساب گوگلی که می‌خوای فایل‌ها توش ذخیره بشه وارد شو و اجازه بده.\n\n"
                f"منتظر تأیید تو هستم (تا {wait_min} دقیقه)..."
            ),
        )
        deadline = time.monotonic() + int(j.get("expires_in", 900))
        interval = int(j.get("interval", 5))
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            status, data = await asyncio.to_thread(gdrive_device_poll, j["device_code"])
            if status == 200 and data.get("refresh_token"):
                await asyncio.to_thread(get_db().kv_set, "gdrive_refresh_token", data["refresh_token"])
                with _gd_lock:
                    _gd_token["value"], _gd_token["exp"] = None, 0.0
                await bot_.send_message(chat_id=chat_id, text="✅ گوگل درایو وصل شد؛ دانلود خارجی الان فعاله.")
                return
            err = data.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            await bot_.send_message(chat_id=chat_id, text=f"❌ وصل شدن انجام نشد ({err or status}). دوباره /gdrive رو بزن.")
            return
        await bot_.send_message(chat_id=chat_id, text="⌛ مهلت وارد کردن کد تموم شد. دوباره /gdrive رو بزن.")
    except Exception as e:
        logger.exception("gdrive device flow failed")
        try:
            await bot_.send_message(chat_id=chat_id, text="❌ خطا در وصل کردن گوگل درایو: " + str(e)[:200])
        except Exception:
            pass
    finally:
        _gdrive_flow_running = False


async def gdrive_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فقط مدیر: /gdrive وضعیت یا شروع وصل کردن؛ /gdrive reset قطع اتصال."""
    if not is_admin(update.effective_user.id):
        return
    msg = update.message
    if not (GDRIVE_CLIENT_ID and GDRIVE_CLIENT_SECRET):
        await msg.reply_text(
            "❌ اول دو متغیر GDRIVE_CLIENT_ID و GDRIVE_CLIENT_SECRET رو توی Variables ریلوی بذار "
            "(از Google Cloud Console، نوع OAuth client: «TVs and Limited Input devices»)، بعد دوباره /gdrive رو بزن."
        )
        return
    if context.args and context.args[0].lower() == "reset":
        await asyncio.to_thread(get_db().kv_delete, "gdrive_refresh_token")
        with _gd_lock:
            _gd_token["value"], _gd_token["exp"] = None, 0.0
        await msg.reply_text("🔌 اتصال گوگل درایو قطع شد. برای وصل دوباره /gdrive رو بزن.")
        return
    if await asyncio.to_thread(gdrive_ready):
        try:
            await asyncio.to_thread(gdrive_access_token, True)
            await msg.reply_text("✅ گوگل درایو وصله و دانلود خارجی فعاله.\nبرای قطع و وصل دوباره: /gdrive reset")
        except Exception as e:
            await msg.reply_text(f"⚠️ توکن درایو کار نمی‌کنه ({str(e)[:150]}). /gdrive reset بزن و دوباره وصل کن.")
        return
    if _gdrive_flow_running:
        await msg.reply_text("⏳ یه وصل‌کردنِ قبلی هنوز منتظر تأییده؛ کدی که فرستادم رو وارد کن.")
        return
    asyncio.create_task(_gdrive_device_flow(context.bot, msg.chat_id))


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
                T("deliver_song.1", video_size_mb=video_size_mb, MAX_TELEGRAM_UPLOAD_MB=MAX_TELEGRAM_UPLOAD_MB)
            )
        elif v_problem:
            await query.message.reply_text(v_problem)
        else:
            video_text = T("deliver_song.2", video_size_mb=video_size_mb)
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
        await set_status(query, T("deliver_song.3", label=label))
        audio_path = await asyncio.to_thread(download_media, url, "bestaudio", "audio", tmp_dir)
        a_bytes = os.path.getsize(audio_path)
        audio_size_mb = a_bytes / 1024 / 1024
        a_problem = quota_problem(uid, a_bytes)
        if audio_size_mb > MAX_TELEGRAM_UPLOAD_MB:
            await query.message.reply_text(
                T("deliver_song.4", audio_size_mb=audio_size_mb, MAX_TELEGRAM_UPLOAD_MB=MAX_TELEGRAM_UPLOAD_MB)
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
        await set_status(query, T("deliver_song.5") + str(e)[:300])
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
    await set_status(query, T("recognize_from_audio_file.1"))
    snippet1 = await asyncio.to_thread(
        make_snippet, audio_path, tmp_dir, 0, SONG_SNIPPET_SECONDS, "snip1"
    )
    result = await recognize_snippet(snippet1)

    # اگه تیکه‌ی اول (که ممکنه فقط حرف زدن باشه) جواب نداد و فایل
    # به‌اندازه‌ی کافی طولانیه، یه تیکه‌ی دیگه از وسطش رو هم امتحان کن
    if not result and duration and duration > 40:
        await set_status(query, T("recognize_from_audio_file.2"))
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
        query, T("find_and_deliver_song.1", label=label, variant_txt=variant_txt)
    )

    search_query = label if not variant else f"{label} {variant}"
    try:
        best = await asyncio.to_thread(search_music, search_query)
    except Exception as e:
        logger.exception("search_music failed in find_and_deliver_song")
        await set_status(query, T("handle_music_query.3") + str(e)[:300])
        return False

    if not best:
        artist_txt = T("find_and_deliver_song.2", artist=artist) if artist else ""
        await set_status(
            query,
            T("find_and_deliver_song.3", title=title, artist_txt=artist_txt),
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
        await set_status(query, T("handle_quality_choice.1"))
        return

    entry = PENDING.get(key)
    if not entry:
        await set_status(query, T("handle_quality_choice.2"))
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
        await set_status(query, T("handle_find_song.1"))
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
                T("handle_find_song.2")
                if not GEMINI_API_KEY
                else ""
            )
            await set_status(
                query,
                T("handle_find_song.3", hint=hint),
            )
            return

        await set_status(query, T("handle_find_song.4"))
        tmp_dir = tempfile.mkdtemp(prefix="song_id_")
        try:
            full_audio_path, duration = await asyncio.to_thread(download_full_audio, url, tmp_dir)
            result = await recognize_from_audio_file(query, full_audio_path, duration, tmp_dir)
        except Exception as e:
            logger.exception("find_song audio recognition failed")
            await set_status(query, T("handle_find_song.5") + str(e)[:300])
            return
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if not result:
            await set_status(
                query,
                T("handle_find_song.6"),
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
        [InlineKeyboardButton(T("handle_uploaded_media.1"), callback_data=f"fa|{key}")],
        [InlineKeyboardButton(T("handle_uploaded_media.2"), callback_data=f"fs|{key}")],
    ])
    await message.reply_text(T("handle_uploaded_media.3"), reply_markup=markup, reply_to_message_id=message.message_id)


async def download_uploaded_file(query, context, entry: dict, tmp_dir: str):
    """فایل ارسالی کاربر رو از تلگرام دانلود می‌کنه؛ اگه نشد None برمی‌گردونه (و پیام خطا می‌ده)."""
    size_mb = (entry["file_size"] or 0) / 1024 / 1024
    if not LOCAL_API_URL and size_mb > TELEGRAM_CLOUD_DOWNLOAD_LIMIT_MB:
        await set_status(
            query,
            T("download_uploaded_file.1", size_mb=size_mb, TELEGRAM_CLOUD_DOWNLOAD_LIMIT_MB=TELEGRAM_CLOUD_DOWNLOAD_LIMIT_MB),
        )
        return None

    await set_status(query, T("download_uploaded_file.2"))
    try:
        tg_file = await context.bot.get_file(entry["file_id"])
        ext = Path(entry["name"]).suffix or ".mp4"
        path = os.path.join(tmp_dir, "input" + ext)
        await tg_file.download_to_drive(path)
    except BadRequest as e:
        if "too big" in str(e).lower():
            await set_status(query, T("download_uploaded_file.3"))
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
        await set_status(query, T("handle_upload_extract_audio.1"))
        return

    tmp_dir = tempfile.mkdtemp(prefix="up_audio_")
    try:
        path = await download_uploaded_file(query, context, entry, tmp_dir)
        if not path:
            return
        await set_status(query, T("handle_upload_extract_audio.2"))
        mp3_path = await asyncio.to_thread(extract_mp3, path, tmp_dir)

        size_mb = os.path.getsize(mp3_path) / 1024 / 1024
        if size_mb > MAX_TELEGRAM_UPLOAD_MB:
            await set_status(query, T("handle_upload_extract_audio.3", size_mb=size_mb, MAX_TELEGRAM_UPLOAD_MB=MAX_TELEGRAM_UPLOAD_MB))
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
        await set_status(query, T("handle_upload_extract_audio.4") + str(e)[:300])
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
        await set_status(query, T("handle_upload_extract_audio.1"))
        return

    chat_id = query.message.chat_id
    title = artist = variant = None

    # اگه کاربر کپشن یا اسم فایل گذاشته، اول Gemini اون رو بررسی کنه
    if GEMINI_API_KEY and (entry["caption"] or entry["name"]):
        await set_status(query, T("handle_upload_find_song.1"))
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
                T("handle_upload_find_song.2"),
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
            await set_status(query, T("handle_find_song.5") + str(e)[:300])
            return
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if not result:
            await set_status(
                query,
                T("handle_upload_find_song.3"),
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


def wait_for_telegram_api(max_wait_seconds: int = 1800) -> None:
    """
    قبل از شروع پولینگ، getMe رو امتحان می‌کنه. اگه تلگرام جواب ۴۲۹ (Flood control) بده، به‌جای کرش و
    ری‌استارت پشت‌سرهم، به اندازه‌ی retry_after صبر می‌کنه و دوباره امتحان می‌کنه.
    (URL و توکن عمداً هیچ‌جا لاگ نمی‌شه.)
    """
    base = LOCAL_API_URL.rstrip("/") if LOCAL_API_URL else "https://api.telegram.org"
    url = f"{base}/bot{BOT_TOKEN}/getMe"
    deadline = time.monotonic() + max_wait_seconds
    backoff = 5
    while time.monotonic() < deadline:
        try:
            resp = requests.post(url, timeout=60)
        except requests.RequestException as e:
            logger.warning("Telegram getMe network error (%s); retrying in %ss", type(e).__name__, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        if resp.status_code == 429:
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 30))
            except Exception:
                wait = 30
            wait = min(max(wait, 1), 300) + 3
            logger.warning("Telegram flood control (429); waiting %ss before retrying", wait)
            time.sleep(wait)
            continue
        return  # موفق (یا خطای دیگه‌ای مثل توکن اشتباه که خود PTB گزارشش می‌کنه)
    logger.error("Telegram API still not answering properly after %ss; continuing anyway", max_wait_seconds)


def main():
    if BOT_TOKEN == "PUT-YOUR-TOKEN-HERE":
        raise SystemExit(
            "توکن ربات رو ست نکردی! متغیر محیطی TELEGRAM_BOT_TOKEN رو بذار یا"
            " مستقیم توی کد جایگزین کن."
        )

    log_ytdlp_environment()
    wait_for_telegram_api()
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
    app.add_handler(CommandHandler("language", language_command))
    app.add_handler(CallbackQueryHandler(language_callback, pattern=r"^lang\|"))
    app.add_handler(MessageHandler(filters.Regex(btn_regex("help")), help_button))
    app.add_handler(MessageHandler(filters.Regex(btn_regex("about")), about_button))
    app.add_handler(MessageHandler(filters.Regex(btn_regex("music")), music_button))
    app.add_handler(MessageHandler(filters.Regex(btn_regex("account")), account_button))
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
    app.add_handler(CallbackQueryHandler(handle_external_download, pattern=r"^ext\|"))
    app.add_handler(CommandHandler("gdrive", gdrive_command))
    app.add_handler(CommandHandler("cookies", cookies_command))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, cookies_document), group=1)
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
