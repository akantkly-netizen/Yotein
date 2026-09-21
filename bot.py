import os
import sys
import re
import json
import asyncio
import logging
import uuid
import glob
import time
import shutil
import concurrent.futures
import urllib.parse
import urllib.request
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

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("InstagramDownloaderBot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
DOWNLOAD_DIR = "temp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

executor = concurrent.futures.ThreadPoolExecutor(max_workers=16)

MEDIA_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_EXPIRATION_SECONDS = 3600  # 1 hour TTL cache

# Spotify Client Credentials (Optional via env or fallback to public token generator)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")


def setup_cookies_file() -> Optional[str]:
    """Prepares and validates Instagram cookie file to bypass IP bans and rate limits."""
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
    """Removes old temporary downloaded files to prevent disk overload on Railway."""
    now = time.time()
    try:
        for filepath in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
            if os.path.isfile(filepath):
                # Delete files older than 20 minutes
                if now - os.path.getmtime(filepath) > 1200:
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
    """Extracts clean Instagram post URL without tracking query parameters."""
    match = re.search(r"(https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reel|tv|reels|stories)/[A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1) + "/"
    return url


def clean_music_query(raw_text: str) -> str:
    """Cleans hashtags, mentions, URLs, and control characters while preserving global Unicode characters (German, Russian, French, Turkish, Asian, Arabic, Persian, etc.)."""
    if not raw_text:
        return ""
    text = re.sub(r'https?://\S+|www\.\S+', '', raw_text)
    text = re.sub(r'[@#]\w+', '', text)
    # Retain all Unicode word characters (\w in Python 3 covers all languages & scripts)
    text = re.sub(r'[^\w\s\d]', ' ', text, flags=re.UNICODE)
    return ' '.join(text.split()).strip()


def extract_music_info_from_caption(caption: str) -> Tuple[str, str]:
    """Extracts song title and artist using global multi-language keyword patterns (German, French, Spanish, Russian, Turkish, Italian, Persian, Arabic, English)."""
    if not caption:
        return "", ""

    patterns = [
        # Multi-lingual keywords for song / music / track / artist / singer
        r'(?:موزیک|آهنگ|ترانه|خواننده|موسیقی|Song|Music|Track|Singer|Artist|By|Musik|Lied|Sänger|Musique|Chanson|Música|Canción|Песня|Музыка|Исполнитель|Müzik|Şarkı|Sanatçı|Musica|Canzone|Группа|Soundtrack|OST)[\s:-]+([^\n#@]+)',
        r'[🎵🎧🎼🎶🔊💿📻]\s*([^\n#@]+)',
    ]

    for pat in patterns:
        match = re.search(pat, caption, re.IGNORECASE)
        if match:
            found = match.group(1).strip()
            if len(found) > 2:
                for sep in ["-", "–", "—", "|", "~"]:
                    if sep in found:
                        parts = found.split(sep, 1)
                        return parts[0].strip(), parts[1].strip()
                return found, ""

    # Check for direct "Artist - Track" or "Track - Artist" anywhere in caption
    delimiters = [r'\s+[-–—|~]\s+']
    for line in caption.split('\n'):
        line_clean = re.sub(r'https?://\S+|[@#]\w+', '', line).strip()
        if 5 <= len(line_clean) <= 100:
            for sep in delimiters:
                parts = re.split(sep, line_clean, maxsplit=1)
                if len(parts) == 2 and len(parts[0]) > 2 and len(parts[1]) > 2:
                    return parts[0].strip(), parts[1].strip()

    lines = [line.strip() for line in caption.split('\n') if line.strip() and not line.startswith('#') and not line.startswith('@')]
    if lines:
        first_line = lines[0]
        first_line = re.sub(r'https?://\S+', '', first_line).strip()
        if 3 <= len(first_line) <= 90:
            return first_line, ""

    return "", ""


def format_size(bytes_size: Optional[int]) -> str:
    """Formats raw bytes size into MB / KB readable strings."""
    if not bytes_size or bytes_size <= 0:
        return "تخمینی"
    mb = bytes_size / (1024 * 1024)
    if mb >= 1.0:
        return f"{mb:.1f} MB"
    kb = bytes_size / 1024
    return f"{int(kb)} KB"


# --- SPOTIFY SEARCH ENGINE MODULE ---

def get_spotify_token() -> Optional[str]:
    """Retrieves access token for Spotify API."""
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        try:
            url = "https://accounts.spotify.com/api/token"
            data = urllib.parse.urlencode({'grant_type': 'client_credentials'}).encode('utf-8')
            req = urllib.request.Request(url, data=data, method='POST')
            import base64
            auth_header = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode('utf-8')).decode('utf-8')
            req.add_header("Authorization", f"Basic {auth_header}")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=5) as response:
                res_data = json.loads(response.read().decode())
                return res_data.get('access_token')
        except Exception as ex:
            logger.debug(f"Failed Spotify credentials auth: {ex}")

    # Fallback to Spotify open web token generator
    try:
        req = urllib.request.Request("https://open.spotify.com/get_access_token", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode())
            return res_data.get('accessToken')
    except Exception as ex:
        logger.debug(f"Failed open Spotify token fetch: {ex}")
        return None


def search_spotify_track(query: str) -> Optional[Dict[str, str]]:
    """Searches Spotify database (global multi-lingual support) to match official track title, artist, and album details."""
    clean_q = clean_music_query(query)
    if not clean_q or len(clean_q) < 2:
        return None

    token = get_spotify_token()
    if not token:
        return None

    try:
        encoded_q = urllib.parse.quote(clean_q)
        url = f"https://api.spotify.com/v1/search?q={encoded_q}&type=track&limit=1"
        req = urllib.request.Request(url, headers={
            'Authorization': f'Bearer {token}',
            'User-Agent': 'Mozilla/5.0'
        })
        with urllib.request.urlopen(req, timeout=6) as response:
            data = json.loads(response.read().decode())
            items = data.get('tracks', {}).get('items', [])
            if items:
                track = items[0]
                track_name = track.get('name', '')
                artists = [a.get('name', '') for a in track.get('artists', [])]
                artist_name = ", ".join(artists) if artists else ""
                album_name = track.get('album', {}).get('name', '')
                spotify_url = track.get('external_urls', {}).get('spotify', '')
                
                logger.info(f"Spotify Matched: {artist_name} - {track_name}")
                return {
                    'title': track_name,
                    'artist': artist_name,
                    'album': album_name,
                    'spotify_url': spotify_url
                }
    except Exception as e:
        logger.debug(f"Spotify search failed for '{clean_q}': {e}")

    return None


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
            'concurrent_fragment_downloads': 12,
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
        'concurrent_fragment_downloads': 12,
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
        'concurrent_fragment_downloads': 12,
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
    """Performs deep multi-stage Spotify + YouTube Music + Google search pipeline across all global languages."""
    queries_to_try = []

    clean_t = clean_music_query(track_title)
    clean_a = clean_music_query(artist_name)
    extracted_title, extracted_artist = extract_music_info_from_caption(caption)

    # STEP 1: Query Spotify Database for exact official Track & Artist match (Universal Multi-lingual Search)
    spotify_match = None
    search_seed = f"{clean_a} {clean_t}".strip() or f"{extracted_artist} {extracted_title}".strip() or clean_music_query(caption[:100])

    if search_seed:
        spotify_match = search_spotify_track(search_seed)

    if spotify_match:
        sp_title = spotify_match['title']
        sp_artist = spotify_match['artist']
        queries_to_try.append(f"{sp_artist} {sp_title} official audio")
        queries_to_try.append(f"{sp_artist} {sp_title}")

    if clean_a and clean_t:
        queries_to_try.append(f"{clean_a} {clean_t} full song audio")
        queries_to_try.append(f"{clean_a} {clean_t}")
    if clean_t:
        queries_to_try.append(f"{clean_t} official song")
        queries_to_try.append(f"{clean_t}")

    if extracted_title:
        q_ext = clean_music_query(f"{extracted_artist} {extracted_title}")
        if q_ext and q_ext not in queries_to_try:
            queries_to_try.append(f"{q_ext} official")

    if not queries_to_try and caption:
        clean_cap = clean_music_query(caption[:120])
        if clean_cap:
            queries_to_try.append(f"{clean_cap} song")

    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'concurrent_fragment_downloads': 12,
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
                                
                                final_title = (spotify_match.get('title') if spotify_match else None) or entry.get('title', track_title or "Original Song")
                                final_artist = (spotify_match.get('artist') if spotify_match else None) or entry.get('uploader') or entry.get('artist') or "Unknown Artist"

                                return {
                                    'filepath': os.path.join(DOWNLOAD_DIR, f"{output_prefix}.mp3"),
                                    'title': final_title,
                                    'uploader': final_artist,
                                    'duration': duration,
                                    'spotify_url': spotify_match.get('spotify_url') if spotify_match else None
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
        "⚡️ **سلام! من ربات دانلود اینستا هستم.**\n\n"
        "با من می‌تونی ویدیوها و پست‌های اینستاگرام رو **بدون محدودیت** و با **کیفیت‌های مختلف** دانلود کنی.\n\n"
        "🎵 همچنین سیستم هوشمند متصل به **Spotify & YouTube Music** نسخه کامل موزیک اصلی پست رو برات استخراج می‌کنه!\n\n"
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
        btn_text = f"📹 {fmt['height']}p - ({fmt['size_str']})"
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

    # Spotify & YouTube Music HQ Full Track download button
    keyboard.append([
        InlineKeyboardButton("🎧 دانلود کامل موزیک اصلی (Spotify / YouTube)", callback_data=f"fullm:{cache_id}:{current_slide}:hq")
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
        status_msg = await query.message.reply_text("🟢 🔎 در حال آنالیز اثر در Spotify و جستجوی نسخه کامل...")

        track_title = cached_item.get("track_title", "")
        artist_name = cached_item.get("artist_name", "")
        caption = cached_item.get("caption", "")

        res = await search_and_download_full_track(track_title, artist_name, caption, file_prefix)

        try:
            if res and os.path.exists(res['filepath']):
                await status_msg.edit_text("⬆️ در حال ارسال موزیک کامل با کیفیت 320kbps...")
                sp_note = f"\n🌐 **لینک اسپاتیفای:** {res['spotify_url']}" if res.get('spotify_url') else ""
                caption_audio = f"🎧 **موزیک کامل اورجینال:**\n🎵 **عنوان:** {res['title']}\n👤 **خواننده:** {res['uploader']}\n✨ **کیفیت:** 320kbps (HQ){sp_note}"
                
                with open(res['filepath'], 'rb') as audio_file:
                    await query.message.reply_audio(
                        audio=audio_file,
                        title=res['title'],
                        performer=res['uploader'],
                        caption=caption_audio,
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
