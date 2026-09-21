import os
import re
import asyncio
import logging
import uuid
import glob
import concurrent.futures
from typing import Dict, Any, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
def clean_music_query(raw_text: str) -> str:
    """پاک‌سازی پیشرفته متن کپشن یا اسم موزیک برای جستجوی کاملاً دقیق"""
    if not raw_text:
        return ""
    # حذف لینک‌ها، هشتگ‌ها، آیدی‌های تلگرام/اینستاگرام
    text = re.sub(r'https?://\S+|www\.\S+', '', raw_text)
    text = re.sub(r'[@#]\w+', '', text)
    # حذف کلمات غیرالفبایی و ایموجی‌ها
    text = re.sub(r'[^\w\s\d]', ' ', text)
    # حذف فاصله‌های تکراری
    return ' '.join(text.split()).strip()


async def download_full_track_from_youtube(query: str, artist: str, output_prefix: str) -> Optional[Dict[str, Any]]:
    """
    جستجوی هوشمند و چندمرحله‌ای برای پیدا کردن نسخه کامل موزیک اورجینال
    ارزیابی مدت زمان ویدیو برای اطمینان از کامل بودن (عدم دانلود ریلزهای کوتاه)
    """
    cleaned_q = clean_music_query(query)
    cleaned_a = clean_music_query(artist)
    
    # ساخت لیستی از پرومپت‌های جستجو بر اساس اولویت
    search_strategies = []
    if cleaned_a and cleaned_q:
        search_strategies.append(f"ytsearch5:{cleaned_a} {cleaned_q} full audio song")
    if cleaned_q:
        search_strategies.append(f"ytsearch5:{cleaned_q} official audio song")
        search_strategies.append(f"ytsearch5:{cleaned_q}")

    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'concurrent_fragment_downloads': 8,
        'nocheckcertificate': True,
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
    
    for search_query in search_strategies:
        try:
            def _search_and_download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(search_query, download=False)
                    if not info or 'entries' not in info:
                        return None
                    
                    # فیلتر کردن نتایج: یافتن آهنگی که زمان آن بیشتر از ۴۵ ثانیه باشد (تراک کامل)
                    for entry in info['entries']:
                        if not entry:
                            continue
                        duration = entry.get('duration', 0)
                        # زمان آهنگ باید حداقل ۴۵ ثانیه و حداکثر ۱۵ دقیقه باشد
                        if duration >= 45 and duration <= 900:
                            video_url = entry.get('webpage_url') or entry.get('url')
                            if video_url:
                                ydl.download([video_url])
                                return {
                                    'filepath': os.path.join(DOWNLOAD_DIR, f"{output_prefix}.mp3"),
                                    'title': entry.get('title', query),
                                    'uploader': entry.get('uploader') or entry.get('artist') or "Unknown Artist",
                                    'duration': duration
                                }
                    return None

            result = await loop.run_in_executor(executor, _search_and_download)
            if result and os.path.exists(result['filepath']):
                return result
        except Exception as e:
            logger.error(f"خطا در جستجوی {search_query}: {e}")
            continue

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
    استخراج متاداده پست اینستاگرام با تنظیمات سرعت بالا و چندنخی
    """
    clean_url = clean_instagram_url(url)
    cookie_file = setup_cookies_file()

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'nocheckcertificate': True,
        'concurrent_fragment_downloads': 8,
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

        info = await loop.run_in_executor(executor, _fetch)
        return info
    except Exception as e:
        logger.error(f"خطا در دریافت اطلاعات از لینک {clean_url}: {e}")
        return None


async def download_media_file(url: str, format_id: str, output_prefix: str) -> Optional[str]:
    """دانلود فایل ویدیو با فرمت مشخص شده و ۸ اتصال همزمان برای حداکثر سرعت"""
    cookie_file = setup_cookies_file()
    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    fmt_spec = format_id if format_id != "best" else "bestvideo[vcodec^=avc]+bestaudio/best"

    ydl_opts = {
        'format': fmt_spec,
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'merge_output_format': 'mp4',
        'nocheckcertificate': True,
        'concurrent_fragment_downloads': 8,
        'buffersize': 1024 * 1024,
    }

    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

    loop = asyncio.get_event_loop()
    try:
        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info)

        await loop.run_in_executor(executor, _download)

        matching_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{output_prefix}.*"))
        if matching_files:
            return matching_files[0]
        return None
    except Exception as e:
        logger.error(f"خطا در دانلود ویدیو {format_id}: {e}")
        return None


async def download_audio_file(url: str, bitrate: str, output_prefix: str) -> Optional[str]:
    """استخراج سریع فایل MP3 از ویدیو اینستاگرام"""
    cookie_file = setup_cookies_file()
    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'concurrent_fragment_downloads': 8,
        'nocheckcertificate': True,
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

        await loop.run_in_executor(executor, _download)
        final_mp3 = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.mp3")
        return final_mp3 if os.path.exists(final_mp3) else None
    except Exception as e:
        logger.error(f"خطا در استخراج فایل صوتی {bitrate}: {e}")
        return None


async def download_full_track_from_youtube(query: str, output_prefix: str) -> Optional[Dict[str, Any]]:
    """
    جستجوی بسیار سریع و دانلود نسخه کامل موزیک از سرویس‌های موسیقی
    """
    search_query = f"ytsearch1:{query} full audio song"
    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'concurrent_fragment_downloads': 8,
        'nocheckcertificate': True,
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

        result = await loop.run_in_executor(executor, _search_and_download)
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
    artist_name = main_item.get('artist') or main_item.get('creator') or main_item.get('uploader') or ""
    
    if not track_title:
        # در صورت عدم وجود نام تراک مستقیم، خط اول کپشن را تمیزسازی می‌کنیم
        first_line = raw_caption.split('\n')[0][:80]
        track_title = first_line

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
        "music_query": track_title,
        "artist_name": artist_name
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
    # دانلود نسخه کامل آهنگ اصلی از گوگل/یوتیوب موزیک
    elif action_type == "fullm":
        music_query = cached_data.get("music_query")
        artist_name = cached_data.get("artist_name", "")
        if not music_query:
            await query.message.reply_text("❌ نام آهنگ قابل تشخیص نبود.")
            return

        status_msg = await query.message.reply_text(f"🔎 در حال جستجوی هوشمند موزیک اصلی در Google / YouTube Music...")
        
        result = await download_full_track_from_youtube(music_query, artist_name, file_prefix)

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
