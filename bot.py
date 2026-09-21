import os
import sys
import re
import asyncio
import logging
import uuid
import glob
import time
import shutil
import concurrent.futures
from typing import Dict, Any, Optional, List, Tuple

import yt_dlp
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("InstagramDownloaderBot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
DOWNLOAD_DIR = "temp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

executor = concurrent.futures.ThreadPoolExecutor(max_workers=12)

MEDIA_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_EXPIRATION_SECONDS = 3600  # 1 hour TTL cache


def setup_cookies_file() -> Optional[str]:
    """Prepares and validates Instagram cookie file to bypass IP bans."""
    cookies_env = os.getenv("INSTAGRAM_COOKIES")
    cookie_path = "cookies.txt"

    if cookies_env:
        try:
            with open(cookie_path, "w", encoding="utf-8") as f:
                f.write(cookies_env.strip())
            return cookie_path
        except Exception as e:
            logger.error(f"Error writing INSTAGRAM_COOKIES env: {e}")

    if os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 0:
        return cookie_path

    return None


def cleanup_temp_files():
    """Removes old temporary downloaded files to prevent disk overload."""
    now = time.time()
    try:
        for filepath in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
            if os.path.isfile(filepath):
                # Delete files older than 30 minutes
                if now - os.path.getmtime(filepath) > 1800:
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
    except Exception as e:
        logger.warning(f"Error during temp file cleanup: {e}")


def clean_expired_cache():
    """Purges expired items from in-memory MEDIA_CACHE."""
    now = time.time()
    expired_keys = [
        k for k, v in MEDIA_CACHE.items()
        if now - v.get("timestamp", 0) > CACHE_EXPIRATION_SECONDS
    ]
    for k in expired_keys:
        MEDIA_CACHE.pop(k, None)


def is_instagram_url(url: str) -> bool:
    """Validates if input text is an Instagram reel/post/tv/story link."""
    pattern = r"(https?://)?(www\.)?(instagram\.com|instagr\.am)/(p|reel|tv|reels|stories)/[A-Za-z0-9_-]+"
    return bool(re.search(pattern, url))


def clean_instagram_url(url: str) -> str:
    """Extracts the base clean Instagram post URL without tracking query parameters."""
    match = re.search(r"(https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reel|tv|reels|stories)/[A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1) + "/"
    return url


def clean_music_query(raw_text: str) -> str:
    """Cleans hashtags, mentions, links, and non-alphanumeric noise for accurate music queries."""
    if not raw_text:
        return ""
    text = re.sub(r'https?://\S+|www\.\S+', '', raw_text)
    text = re.sub(r'[@#]\w+', '', text)
    # Remove emojis and unwanted symbols
    text = re.sub(r'[^\w\s\d]', ' ', text)
    return ' '.join(text.split()).strip()


def extract_music_info_from_caption(caption: str) -> Tuple[str, str]:
    """Extracts song title and artist using keyword patterns in multi-language captions."""
    if not caption:
        return "", ""

    patterns = [
        r'(?:موزیک|آهنگ|ترانه|خواننده|موسیقی|Song|Music|Track|Singer|By)[\s:-]+([^\n#@]+)',
        r'🎵\s*([^\n#@]+)',
        r'🎧\s*([^\n#@]+)',
        r'🎼\s*([^\n#@]+)',
        r'🎶\s*([^\n#@]+)'
    ]

    for pat in patterns:
        match = re.search(pat, caption, re.IGNORECASE)
        if match:
            found = match.group(1).strip()
            if len(found) > 2:
                # Splitting title and artist if dash exists
                if "-" in found:
                    parts = found.split("-", 1)
                    return parts[0].strip(), parts[1].strip()
                elif "–" in found:
                    parts = found.split("–", 1)
                    return parts[0].strip(), parts[1].strip()
                return found, ""

    # Fallback to first non-empty line of caption
    lines = [line.strip() for line in caption.split('\n') if line.strip() and not line.startswith('#') and not line.startswith('@')]
    if lines:
        first_line = lines[0]
        first_line = re.sub(r'https?://\S+', '', first_line).strip()
        if 3 <= len(first_line) <= 80:
            return first_line, ""

    return "", ""


def format_size(bytes_size: Optional[int]) -> str:
    """Formats raw bytes size into MB / KB readable strings."""
    if not bytes_size or bytes_size <= 0:
        return "تخپینی"
    mb = bytes_size / (1024 * 1024)
    if mb >= 1.0:
        return f"{mb:.1f} MB"
    kb = bytes_size / 1024
    return f"{int(kb)} KB"


async def extract_instagram_info_robust(url: str) -> Optional[Dict[str, Any]]:
    """Fetches Instagram media metadata using multiple user-agent fallback strategies."""
    clean_url = clean_instagram_url(url)
    cookie_file = setup_cookies_file()

    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Mobile/15E148 Safari/604.1',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    ]

    loop = asyncio.get_event_loop()

    for agent in user_agents:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'nocheckcertificate': True,
            'concurrent_fragment_downloads': 10,
            'http_headers': {
                'User-Agent': agent,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Sec-Fetch-Mode': 'navigate',
            },
        }

        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file

        def _fetch():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(clean_url, download=False)
            except Exception as ex:
                logger.debug(f"Strategy failed with UA {agent[:30]}: {ex}")
                return None

        result = await loop.run_in_executor(executor, _fetch)
        if result:
            return result

    return None


async def download_instagram_video(url: str, format_id: str, output_prefix: str) -> Optional[str]:
    """Downloads requested video quality with multi-threaded fragment downloads."""
    cookie_file = setup_cookies_file()
    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    fmt_spec = format_id if (format_id and format_id != "best") else "bestvideo[vcodec^=avc]+bestaudio/best"

    ydl_opts = {
        'format': fmt_spec,
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'merge_output_format': 'mp4',
        'nocheckcertificate': True,
        'concurrent_fragment_downloads': 10,
        'buffersize': 2048 * 1024,
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

        matching = glob.glob(os.path.join(DOWNLOAD_DIR, f"{output_prefix}.*"))
        if matching:
            return matching[0]
        return None
    except Exception as e:
        logger.error(f"Error downloading video format {format_id}: {e}")
        return None


async def download_instagram_audio(url: str, bitrate: str, output_prefix: str) -> Optional[str]:
    """Extracts MP3 audio track from Instagram video with high-speed FFmpeg conversion."""
    cookie_file = setup_cookies_file()
    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'concurrent_fragment_downloads': 10,
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
        logger.error(f"Error extracting audio bitrate {bitrate}: {e}")
        return None


async def search_and_download_full_track(track_title: str, artist_name: str, caption: str, output_prefix: str) -> Optional[Dict[str, Any]]:
    """Performs deep 6-stage search on YouTube Music & Google to find full original HQ track."""
    queries_to_try = []

    clean_t = clean_music_query(track_title)
    clean_a = clean_music_query(artist_name)
    extracted_title, extracted_artist = extract_music_info_from_caption(caption)

    if clean_a and clean_t:
        queries_to_try.append(f"{clean_a} {clean_t} full audio song")
        queries_to_try.append(f"{clean_a} {clean_t}")
    if clean_t:
        queries_to_try.append(f"{clean_t} official audio song")
        queries_to_try.append(f"{clean_t} audio")

    if extracted_title:
        q_ext = clean_music_query(f"{extracted_artist} {extracted_title}")
        if q_ext and q_ext not in queries_to_try:
            queries_to_try.append(f"{q_ext} official audio")

    if not queries_to_try and caption:
        clean_cap = clean_music_query(caption[:100])
        if clean_cap:
            queries_to_try.append(f"{clean_cap} song")

    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'concurrent_fragment_downloads': 10,
        'nocheckcertificate': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch5:',
    }

    loop = asyncio.get_event_loop()

    for search_q in queries_to_try:
        try:
            def _search():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"ytsearch5:{search_q}", download=False)
                    if not info or 'entries' not in info or not info['entries']:
                        return None

                    for entry in info['entries']:
                        if not entry:
                            continue
                        duration = entry.get('duration', 0)
                        # Filter out short reels (<45s) and too long mixes (>15m)
                        if 45 <= duration <= 900:
                            video_url = entry.get('webpage_url') or entry.get('url')
                            if video_url:
                                ydl.download([video_url])
                                return {
                                    'filepath': os.path.join(DOWNLOAD_DIR, f"{output_prefix}.mp3"),
                                    'title': entry.get('title', track_title or "Original Song"),
                                    'uploader': entry.get('uploader') or entry.get('artist') or "Unknown Artist",
                                    'duration': duration
                                }
                    return None

            res = await loop.run_in_executor(executor, _search)
            if res and os.path.exists(res['filepath']):
                return res
        except Exception as ex:
            logger.debug(f"Search query failed '{search_q}': {ex}")
            continue

    return None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends greeting message on /start command."""
    welcome_text = (
        "⚡️ **سلام! به ربات دانلود پیشرفته اینستاگرام خوش آمدید.**\n\n"
        "من می‌تونم تمام ویدیوها، ریلزها، وپست‌های چندتایی (آلبوم) اینستاگرام رو با **کیفیت‌های مختلف** و **حجم دقیق** برات دانلود کنم.\n\n"
        "🎵 همچنین می‌تونم **نسخه کامل موزیک اورجینال** ترانه رو برات از یوتیوب موزیک پیدا و استخراج کنم!\n\n"
        "👇 **کافیه فقط لینک پست یا ریلز اینستاگرام رو برام بفرستی:**"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def handle_instagram_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming links, extracts qualities, thumbnails, carousel slides, and formats keyboard."""
    url = update.message.text.strip()

    if not is_instagram_url(url):
        return

    cleanup_temp_files()
    clean_expired_cache()

    status_msg = await update.message.reply_text("🔎 در حال بررسی لینک و استخراج کیفیت‌ها...")

    info = await extract_instagram_info_robust(url)

    if not info:
        await status_msg.edit_text("❌ متأسفانه دریافت اطلاعات پست با خطا مواجه شد. از عمومی (Public) بودن پیج اطمینان حاصل کنید.")
        return

    cache_id = str(uuid.uuid4())[:8]

    # Carousel / Album parsing
    entries = info.get('entries')
    is_album = bool(entries and len(entries) > 1)
    total_slides = len(entries) if is_album else 1
    current_slide = 0

    main_item = entries[current_slide] if is_album else info

    formats = main_item.get('formats', [])
    thumbnail = main_item.get('thumbnail') or info.get('thumbnail', '')
    raw_caption = info.get('description') or info.get('title') or "ویدیو اینستاگرام"

    track_title = main_item.get('track') or main_item.get('alt_title') or ""
    artist_name = main_item.get('artist') or main_item.get('creator') or main_item.get('uploader') or ""

    if not track_title:
        extracted_t, extracted_a = extract_music_info_from_caption(raw_caption)
        track_title = extracted_t
        if extracted_a and not artist_name:
            artist_name = extracted_a

    short_caption = raw_caption[:500] + "..." if len(raw_caption) > 500 else raw_caption

    # Extract distinct video qualities
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

    valid_formats.sort(key=lambda x: x['height'], reverse=True)

    keyboard = []

    # Quality buttons
    for fmt in valid_formats:
        btn_text = f"📹 کیفیت {fmt['height']}p - ({fmt['size_str']})"
        callback_data = f"vid:{cache_id}:{current_slide}:{fmt['format_id']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

    if not keyboard:
        keyboard.append([
            InlineKeyboardButton("📹 دانلود ویدیو (بهترین کیفیت)", callback_data=f"vid:{cache_id}:{current_slide}:best")
        ])

    # Carousel slide navigation if album
    if is_album:
        nav_row = []
        if current_slide > 0:
            nav_row.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"slide:{cache_id}:{current_slide - 1}"))
        nav_row.append(InlineKeyboardButton(f"اسلاید {current_slide + 1} از {total_slides}", callback_data="noop"))
        if current_slide < total_slides - 1:
            nav_row.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"slide:{cache_id}:{current_slide + 1}"))
        keyboard.append(nav_row)

    # Voice / Audio extraction buttons
    keyboard.append([
        InlineKeyboardButton("🎵 ویس ویدیو 128", callback_data=f"aud:{cache_id}:{current_slide}:128"),
        InlineKeyboardButton("🎵 ویس ویدیو 320", callback_data=f"aud:{cache_id}:{current_slide}:320"),
    ])

    # Full HQ original track button
    keyboard.append([
        InlineKeyboardButton("🎧 دانلود موزیک اصلی کامل (YouTube / Google)", callback_data=f"fullm:{cache_id}:{current_slide}:hq")
    ])

    # Extra action buttons
    extra_row = []
    if thumbnail:
        extra_row.append(InlineKeyboardButton("🖼 کاور اصلی", callback_data=f"img:{cache_id}:{current_slide}"))
    if len(raw_caption) > 500:
        extra_row.append(InlineKeyboardButton("📜 کپشن کامل", callback_data=f"cap:{cache_id}:{current_slide}"))

    if extra_row:
        keyboard.append(extra_row)

    # Store media in cache
    MEDIA_CACHE[cache_id] = {
        "url": url,
        "info": info,
        "caption": raw_caption,
        "thumbnail": thumbnail,
        "track_title": track_title,
        "artist_name": artist_name,
        "is_album": is_album,
        "total_slides": total_slides,
        "timestamp": time.time()
    }

    reply_markup = InlineKeyboardMarkup(keyboard)
    await status_msg.delete()

    caption_text = f"📝 **کپشن:**\n{short_caption}\n\nکیفیت یا گزینه مورد نظر را انتخاب کنید:"

    if thumbnail:
        try:
            await update.message.reply_photo(
                photo=thumbnail,
                caption=caption_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            return
        except Exception as ex:
            logger.warning(f"Could not send photo thumbnail: {ex}")

    await update.message.reply_text(
        text=caption_text,
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button clicks for video, audio, full track, slides, and caption."""
    query = update.callback_query
    await query.answer()

    data_parts = query.data.split(":")
    if len(data_parts) < 3:
        return

    action_type = data_parts[0]
    cache_id = data_parts[1]
    slide_idx = int(data_parts[2]) if data_parts[2].isdigit() else 0
    param = data_parts[3] if len(data_parts) > 3 else ""

    if action_type == "noop":
        return

    cached_item = MEDIA_CACHE.get(cache_id)
    if not cached_item:
        await query.message.reply_text("❌ نشست این پست منقضی شده است. لطفاً دوباره لینک را ارسال کنید.")
        return

    url = cached_item["url"]
    file_prefix = f"file_{uuid.uuid4().hex[:6]}"

    if action_type == "vid":
        status_msg = await query.message.reply_text("⏳ در حال دانلود ویدیو... لطفاً صبور باشید.")
        filepath = await download_instagram_video(url, param, file_prefix)

        try:
            if filepath and os.path.exists(filepath):
                await status_msg.edit_text("⬆️ در حال آپلود ویدیو به تلگرام...")
                with open(filepath, 'rb') as video_file:
                    await query.message.reply_video(
                        video=video_file,
                        caption="✨ دانلود شده توسط ربات اینستاگرام",
                        supports_streaming=True
                    )
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ خطا در دانلود ویدیو. ممکن است لینک منقضی شده باشد.")
        finally:
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass

    elif action_type == "aud":
        status_msg = await query.message.reply_text(f"⏳ در حال استخراج ویس با کیفیت {param}kbps...")
        filepath = await download_instagram_audio(url, param, file_prefix)

        try:
            if filepath and os.path.exists(filepath):
                await status_msg.edit_text("⬆️ در حال ارسال فایل صوتی...")
                with open(filepath, 'rb') as audio_file:
                    await query.message.reply_audio(
                        audio=audio_file,
                        caption=f"🎵 ویس استخراج شده با کیفیت {param}kbps"
                    )
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ خطا در استخراج فایل صوتی.")
        finally:
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass

    elif action_type == "fullm":
        status_msg = await query.message.reply_text("🔎 در حال جستجوی هوشمند موزیک اصلی در Google و YouTube...")

        track_title = cached_item.get("track_title", "")
        artist_name = cached_item.get("artist_name", "")
        caption = cached_item.get("caption", "")

        res = await search_and_download_full_track(track_title, artist_name, caption, file_prefix)

        try:
            if res and os.path.exists(res['filepath']):
                await status_msg.edit_text("⬆️ در حال ارسال موزیک کامل با کیفیت 320kbps...")
                with open(res['filepath'], 'rb') as audio_file:
                    await query.message.reply_audio(
                        audio=audio_file,
                        title=res['title'],
                        performer=res['uploader'],
                        caption=f"🎧 **موزیک کامل اورجینال:**\n🎵 **عنوان:** {res['title']}\n👤 **خواننده:** {res['uploader']}\n✨ **کیفیت:** 320kbps (HQ)",
                        parse_mode="Markdown"
                    )
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ متأسفانه نسخه کامل این موزیک یافت نشد.")
        finally:
            if res and os.path.exists(res['filepath']):
                try:
                    os.remove(res['filepath'])
                except Exception:
                    pass

    elif action_type == "img":
        thumb_url = cached_item.get("thumbnail")
        if thumb_url:
            await query.message.reply_photo(photo=thumb_url, caption="🖼 کاور با کیفیت اصلی")

    elif action_type == "cap":
        full_cap = cached_item.get("caption", "بدون کپشن")
        await query.message.reply_text(f"📜 **کپشن کامل:**\n\n{full_cap}")


def main():
    """Main entry point for starting Telegram bot application."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.error("BOT_TOKEN variable is not set. Please specify BOT_TOKEN in environment.")
        sys.exit(1)

    logger.info("Starting Telegram Instagram Downloader Bot...")

    app = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_instagram_link))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Run bot polling loop
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
