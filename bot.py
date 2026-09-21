import os
import re
import asyncio
import logging
import uuid
import glob
from typing import Dict, Any, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import yt_dlp

# تنظیمات ثبت لاگ برای عیب‌یابی دقیق
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# دریافت توکن ربات از متغیرهای محیطی
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")

# ایجاد پوشه موقت برای ذخیره‌سازی فایل‌های دانلود
DOWNLOAD_DIR = "temp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# حافظه موقت برای ذخیره متاداده‌های پست
MEDIA_CACHE: Dict[str, Dict[str, Any]] = {}


def setup_cookies_file() -> Optional[str]:
    """
    در صورت وجود کوکی در متغیرهای محیطی (INSTAGRAM_COOKIES) یا فایل cookies.txt،
    مسیر فایل کوکی را جهت دور زدن محدودیت‌های اینستاگرام برمی‌گرداند.
    """
    cookies_env = os.getenv("INSTAGRAM_COOKIES")
    if cookies_env:
        cookie_path = os.path.join(DOWNLOAD_DIR, "ig_cookies.txt")
        try:
            with open(cookie_path, "w", encoding="utf-8") as f:
                f.write(cookies_env)
            return cookie_path
        except Exception as e:
            logger.error(f"خطا در ایجاد فایل کوکی: {e}")
            
    if os.path.exists("cookies.txt"):
        return "cookies.txt"
        
    return None


def format_size(bytes_size: Optional[int]) -> str:
    """تبدیل حجم بایت به فرمت قابل فهم (MB/KB)"""
    if not bytes_size or bytes_size <= 0:
        return "نامشخص"
    mb = bytes_size / (1024 * 1024)
    if mb >= 1.0:
        return f"{mb:.1f} MB"
    kb = bytes_size / 1024
    return f"{int(kb)} KB"


def is_instagram_url(url: str) -> bool:
    """بررسی اعتبار لینک ورودی اینستاگرام"""
    ig_pattern = r"(https?://)?(www\.)?(instagram\.com|instagr\.am)/(p|reel|tv|reels|stories)/[A-Za-z0-9_-]+"
    return bool(re.search(ig_pattern, url))


def clean_instagram_url(url: str) -> str:
    """حذف پارامترهای اضافی از لینک اینستاگرام برای پردازش دقیق‌تر"""
    match = re.search(r"(https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reel|tv|reels|stories)/[A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1) + "/"
    return url


async def extract_instagram_info(url: str) -> Optional[Dict[str, Any]]:
    """
    استخراج متاداده پست اینستاگرام با تنظیمات پیشرفته جهت دور زدن Bot Detection
    """
    clean_url = clean_instagram_url(url)
    cookie_file = setup_cookies_file()

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }

    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

    loop = asyncio.get_event_loop()
    try:
        def _fetch():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(clean_url, download=False)

        info = await loop.run_in_executor(None, _fetch)
        return info
    except Exception as e:
        logger.error(f"خطا در دریافت اطلاعات از لینک {clean_url}: {e}")
        return None


async def download_media_file(url: str, format_id: str, output_prefix: str) -> Optional[str]:
    """دانلود فایل ویدیو با فرمت مشخص شده"""
    cookie_file = setup_cookies_file()
    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    fmt_spec = format_id if format_id != "best" else "bestvideo[vcodec^=avc]+bestaudio/best"

    ydl_opts = {
        'format': fmt_spec,
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'merge_output_format': 'mp4',
    }

    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

    loop = asyncio.get_event_loop()
    try:
        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info)

        await loop.run_in_executor(None, _download)

        matching_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{output_prefix}.*"))
        if matching_files:
            return matching_files[0]
        return None
    except Exception as e:
        logger.error(f"خطا در دانلود ویدیو {format_id}: {e}")
        return None


async def download_audio_file(url: str, bitrate: str, output_prefix: str) -> Optional[str]:
    """استخراج فایل MP3 از ویدیو اینستاگرام"""
    cookie_file = setup_cookies_file()
    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': bitrate,
        }],
        'quiet': True,
        'no_warnings': True,
    }

    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

    loop = asyncio.get_event_loop()
    try:
        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
                return os.path.join(DOWNLOAD_DIR, f"{output_prefix}.mp3")

        await loop.run_in_executor(None, _download)
        final_mp3 = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.mp3")
        return final_mp3 if os.path.exists(final_mp3) else None
    except Exception as e:
        logger.error(f"خطا در استخراج فایل صوتی {bitrate}: {e}")
        return None


async def download_full_track_from_youtube(query: str, output_prefix: str) -> Optional[Dict[str, Any]]:
    """
    جستجوی هوشمند و دانلود نسخه کامل و اورجینال موزیک از سرویس‌های یوتیوب/یوتیوب موزیک
    """
    search_query = f"ytsearch1:{query} full audio song"
    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
    }

    loop = asyncio.get_event_loop()
    try:
        def _search_and_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_query, download=True)
                if 'entries' in info and len(info['entries']) > 0:
                    entry = info['entries'][0]
                    return {
                        'filepath': os.path.join(DOWNLOAD_DIR, f"{output_prefix}.mp3"),
                        'title': entry.get('title', query),
                        'uploader': entry.get('uploader', 'Unknown Artist'),
                    }
                return None

        result = await loop.run_in_executor(None, _search_and_download)
        if result and os.path.exists(result['filepath']):
            return result
        return None
    except Exception as e:
        logger.error(f"خطا در پیدا کردن موزیک کامل برای {query}: {e}")
        return None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پاسخ به دستور /start"""
    welcome_message = (
        "هی سلام من ربات دانلود اینستا هستم "
        "با من میتونی ویدیو های اینستا رو بدون محدودیت دانلود کنی ⚡️\n\n"
        "فقط کافیه لینک پست یا ریلز مورد نظرت رو برام بفرستی!"
    )
    await update.message.reply_text(welcome_message)


async def handle_instagram_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش لینک‌های ورودی و ساخت منوی شیشه‌ای کیفیت‌ها"""
    url = update.message.text.strip()

    if not is_instagram_url(url):
        return

    status_msg = await update.message.reply_text("🔎 در حال بررسی لینک و استخراج کیفیت‌های ویدیو...")

    info = await extract_instagram_info(url)

    if not info:
        await status_msg.edit_text("❌ متأسفانه مشکلی در دریافت اطلاعات پیش آمد. از عمومی (Public) بودن پیج اطمینان حاصل کنید.")
        return

    cache_id = str(uuid.uuid4())[:8]

    entries = info.get('entries')
    main_item = entries[0] if entries else info

    formats = main_item.get('formats', [])
    thumbnail = main_item.get('thumbnail') or info.get('thumbnail', '')
    raw_caption = info.get('description') or info.get('title') or "ویدیو اینستاگرام"

    # استخراج نام تراک یا موزیک استفاده شده
    track_title = main_item.get('track') or main_item.get('alt_title') or ""
    artist_name = main_item.get('artist') or main_item.get('creator') or ""
    
    if track_title:
        full_music_query = f"{artist_name} {track_title}".strip()
    else:
        # در صورت عدم وجود نام تراک مستقیم، خط اول کپشن را استفاده می‌کنیم
        first_line = raw_caption.split('\n')[0][:50]
        full_music_query = re.sub(r'#\w+|[^\w\s]', '', first_line).strip()

    short_caption = raw_caption[:600] + "..." if len(raw_caption) > 600 else raw_caption

    valid_formats = []
    seen_heights = set()

    for f in formats:
        height = f.get('height')
        vcodec = f.get('vcodec', 'none')

        if height and vcodec != 'none' and height not in seen_heights:
            seen_heights.add(height)
            filesize = f.get('filesize') or f.get('filesize_approx')
            valid_formats.append({
                'format_id': f.get('format_id'),
                'height': height,
                'size_str': format_size(filesize)
            })

    valid_formats.sort(key=lambda x: x['height'])

    keyboard = []

    # دکمه‌های ویدیو
    for fmt in valid_formats:
        btn_text = f"📹 دانلود کیفیت {fmt['height']}p - ({fmt['size_str']})"
        callback_data = f"vid:{cache_id}:{fmt['format_id']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

    if not keyboard:
        keyboard.append([
            InlineKeyboardButton("📹 دانلود ویدیو با بهترین کیفیت", callback_data=f"vid:{cache_id}:best")
        ])

    # دکمه‌های استخراج صوتی ویدیو
    keyboard.append([
        InlineKeyboardButton("🎵 ویس ویدیو 128", callback_data=f"aud:{cache_id}:128"),
        InlineKeyboardButton("🎵 ویس ویدیو 320", callback_data=f"aud:{cache_id}:320"),
    ])

    # دکمه اختصاصی دانلود کامل موزیک اصلی از Spotify / YouTube Music
    if full_music_query:
        keyboard.append([
            InlineKeyboardButton("🎧 دانلود موزیک اصلی کامل (Spotify/YouTube)", callback_data=f"fullm:{cache_id}:hq")
        ])

    # دکمه‌های کمکی
    extra_buttons = []
    if thumbnail:
        extra_buttons.append(InlineKeyboardButton("🖼 کاور ویدیو", callback_data=f"img:{cache_id}:thumb"))
    if len(raw_caption) > 600:
        extra_buttons.append(InlineKeyboardButton("📜 کپشن کامل", callback_data=f"cap:{cache_id}:full"))

    if extra_buttons:
        keyboard.append(extra_buttons)

    # ذخیره در کش
    MEDIA_CACHE[cache_id] = {
        "url": url,
        "caption": raw_caption,
        "thumbnail": thumbnail,
        "music_query": full_music_query
    }

    reply_markup = InlineKeyboardMarkup(keyboard)

    await status_msg.delete()

    caption_text = f"📝 **کپشن:**\n{short_caption}\n\nکیفیت مورد نظر خود را انتخاب کنید:"

    if thumbnail:
        try:
            await update.message.reply_photo(
                photo=thumbnail,
                caption=caption_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            return
        except Exception as e:
            logger.warning(f"عدم موفقیت در ارسال کاور: {e}")

    await update.message.reply_text(
        text=caption_text,
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت کلیک روی دکمه‌ها"""
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    if len(data) < 3:
        return

    action_type = data[0]   # 'vid', 'aud', 'fullm', 'img', 'cap'
    cache_id = data[1]      
    param = data[2]         

    cached_data = MEDIA_CACHE.get(cache_id)
    if not cached_data:
        await query.message.reply_text("❌ اطلاعات این پست منقضی شده است. لطفاً دوباره لینک را ارسال کنید.")
        return

    url = cached_data["url"]
    file_prefix = f"dl_{cache_id}_{uuid.uuid4().hex[:4]}"

    # نمایش کپشن کامل
    if action_type == "cap":
        await query.message.reply_text(f"📝 **کپشن کامل:**\n\n{cached_data['caption']}")
        return

    # ارسال کاور
    if action_type == "img":
        if cached_data.get("thumbnail"):
            await query.message.reply_photo(
                photo=cached_data["thumbnail"],
                caption="🖼 **کاور کیفیت اصلی ویدیو**"
            )
        return

    # دانلود ویدیو
    if action_type == "vid":
        status_msg = await query.message.reply_text("⏳ در حال دانلود ویدیو، لطفاً شکیبا باشید...")
        filepath = await download_media_file(url, param, file_prefix)

        try:
            if filepath and os.path.exists(filepath):
                await status_msg.edit_text("⬆️ در حال آپلود ویدیو به تلگرام...")
                with open(filepath, 'rb') as video_file:
                    await query.message.reply_video(
                        video=video_file,
                        caption="✅ **ویدیو شما با موفقیت دانلود شد.**",
                        parse_mode="Markdown"
                    )
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ خطایی هنگام دریافت فایل ویدیو رخ داد.")
        finally:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)

    # دانلود ویس/آهنگ ویدیو
    elif action_type == "aud":
        status_msg = await query.message.reply_text(f"⏳ در حال استخراج فایل صوتی با کیفیت {param}kbps...")
        filepath = await download_audio_file(url, param, file_prefix)

        try:
            if filepath and os.path.exists(filepath):
                await status_msg.edit_text("⬆️ در حال ارسال فایل صوتی...")
                with open(filepath, 'rb') as audio_file:
                    await query.message.reply_audio(
                        audio=audio_file,
                        title=f"Instagram Audio ({param}k)",
                        caption=f"🎵 **آهنگ استخراج شده با کیفیت {param}kbps**",
                        parse_mode="Markdown"
                    )
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ خطایی هنگام استخراج فایل صوتی رخ داد.")
        finally:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)

    # دانلود نسخه کامل آهنگ اصلی از یوتیوب/اسپاتیفای
    elif action_type == "fullm":
        music_query = cached_data.get("music_query")
        if not music_query:
            await query.message.reply_text("❌ نام آهنگ قابل تشخیص نبود.")
            return

        status_msg = await query.message.reply_text(f"🔎 در حال جستجو و دانلود نسخه کامل موزیک `{music_query}` از دیتابیس Spotify/YouTube...")
        
        result = await download_full_track_from_youtube(music_query, file_prefix)

        try:
            if result and os.path.exists(result['filepath']):
                await status_msg.edit_text("⬆️ در حال ارسال فایل کامل موزیک با کیفیت 320kbps...")
                with open(result['filepath'], 'rb') as audio_file:
                    await query.message.reply_audio(
                        audio=audio_file,
                        title=result['title'],
                        performer=result['uploader'],
                        caption=f"🎧 **موزیک کامل اورجینال یافت شد:**\n🎵 {result['title']}\n👤 {result['uploader']}\n✨ کیفیت: 320kbps (HQ)",
                        parse_mode="Markdown"
                    )
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ متأسفانه نسخه کامل این موزیک در دیتابیس یافت نشد.")
        finally:
            if result and os.path.exists(result['filepath']):
                os.remove(result['filepath'])


def main():
    """شروع به کار ربات تلگرام"""
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("🔴 خطا: توکن ربات تنظیم نشده است. لطفاً متغیر BOT_TOKEN را مقداردهی کنید.")
        return

    print("🟢 ربات با قابلیت‌های پیشرفته آماده به کار است...")

    app = Application.builder().token(BOT_TOKEN).build()

    # ثبت هندرها
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_instagram_link))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # اجرا
    app.run_polling()


if __name__ == "__main__":
    main()
