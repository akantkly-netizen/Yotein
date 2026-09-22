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
import base64
import concurrent.futures
import urllib.parse
import urllib.request
from typing import Dict, Any, Optional, List, Tuple

import yt_dlp
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
logger = logging.getLogger("MediaDownloaderBot")

# Global Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
DOWNLOAD_DIR = "temp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

executor = concurrent.futures.ThreadPoolExecutor(max_workers=16)

MEDIA_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_EXPIRATION_SECONDS = 3600  # 1 hour TTL cache

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")


def setup_cookies_file() -> Optional[str]:
    """Prepares cookie file ONLY if valid cookies are explicitly provided in environment variables."""
    cookies_env = os.getenv("YOUTUBE_COOKIES") or os.getenv("INSTAGRAM_COOKIES") or os.getenv("COOKIES_TEXT")
    cookie_path = "cookies.txt"

    if cookies_env and len(cookies_env.strip()) > 20:
        try:
            with open(cookie_path, "w", encoding="utf-8") as f:
                f.write(cookies_env.strip())
            return cookie_path
        except Exception as e:
            logger.error(f"Error writing cookies env: {e}")

    # Clean up invalid cookie files
    if os.path.exists(cookie_path):
        try:
            os.remove(cookie_path)
        except Exception:
            pass

    return None


def cleanup_temp_files():
    """Removes old temporary downloaded files to prevent disk overload."""
    now = time.time()
    try:
        for filepath in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
            if os.path.isfile(filepath):
                if now - os.path.getmtime(filepath) > 900:
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
    """Validates if input text is an Instagram reel/post/tv/story/share link."""
    pattern = r"(https?://)?(www\.)?(instagram\.com|instagr\.am)/(p|reel|reels|tv|stories|share)/[A-Za-z0-9_\-\.]+"
    return bool(re.search(pattern, url))


def is_youtube_url(url: str) -> bool:
    """Validates if input text is a YouTube video/shorts/music link."""
    pattern = r"(https?://)?(www\.|m\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/(watch\?v=|shorts/|live/|embed/|v/|[A-Za-z0-9_-]+)"
    return bool(re.search(pattern, url))


def is_supported_url(url: str) -> bool:
    """Checks if the provided text contains an Instagram or YouTube URL."""
    return is_instagram_url(url) or is_youtube_url(url)


def clean_media_url(url: str) -> str:
    """Extracts clean URL without tracking query parameters."""
    if is_instagram_url(url):
        match = re.search(r"(https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reel|tv|reels|stories|share)/[A-Za-z0-9_\-\.]+)", url)
        if match:
            return match.group(1) + "/"
    elif is_youtube_url(url):
        url = url.strip()
        if "youtube.com/shorts/" in url:
            shorts_id = url.split("shorts/")[1].split("?")[0].split("/")[0]
            return f"https://www.youtube.com/shorts/{shorts_id}"
        elif "youtu.be/" in url:
            yt_id = url.split("youtu.be/")[1].split("?")[0].split("/")[0]
            return f"https://www.youtube.com/watch?v={yt_id}"
        elif "watch?v=" in url:
            yt_id = url.split("watch?v=")[1].split("&")[0]
            return f"https://www.youtube.com/watch?v={yt_id}"
    return url.strip()


def clean_music_query(raw_text: str) -> str:
    """Cleans hashtags, mentions, URLs, and control characters while preserving global Unicode characters."""
    if not raw_text:
        return ""
    text = re.sub(r'https?://\S+|www\.\S+', '', raw_text)
    text = re.sub(r'[@#]\w+', '', text)
    text = re.sub(r'[^\w\s\d]', ' ', text, flags=re.UNICODE)
    return ' '.join(text.split()).strip()


def extract_music_info_from_caption(caption: str) -> Tuple[str, str]:
    """Extracts song title and artist using global multi-language keyword patterns."""
    if not caption:
        return "", ""

    patterns = [
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


def estimate_format_filesize(fmt: Dict[str, Any], duration: float) -> Optional[int]:
    """Calculates filesize from direct byte fields or estimates it from bitrate and duration."""
    filesize = fmt.get('filesize') or fmt.get('filesize_approx')
    if filesize and filesize > 0:
        return filesize

    tbr = fmt.get('tbr') or ((fmt.get('vbr') or 0) + (fmt.get('abr') or 0))
    if tbr and tbr > 0 and duration and duration > 0:
        estimated_bytes = int((tbr * 1000 / 8) * duration)
        return estimated_bytes

    return None


def format_size(bytes_size: Optional[int]) -> str:
    """Formats raw bytes size into GB / MB / KB readable strings."""
    if not bytes_size or bytes_size <= 0:
        return "تخمینی"
    gb = bytes_size / (1024 * 1024 * 1024)
    if gb >= 1.0:
        return f"{gb:.2f} GB"
    mb = bytes_size / (1024 * 1024)
    if mb >= 1.0:
        return f"{mb:.1f} MB"
    kb = bytes_size / 1024
    return f"{int(kb)} KB"


def extract_resolution_height(fmt: Dict[str, Any]) -> int:
    """Extracts resolution height integer from height, width, resolution string, or format_note."""
    height = fmt.get('height')
    if height and isinstance(height, int) and height > 0:
        return height

    resolution = fmt.get('resolution') or ""
    if "x" in resolution:
        try:
            parts = resolution.lower().split("x")
            return min(int(parts[0]), int(parts[1]))
        except Exception:
            pass

    format_note = fmt.get('format_note') or ""
    match = re.search(r'(\d{3,4})p?', format_note)
    if match:
        return int(match.group(1))

    width = fmt.get('width')
    if width and isinstance(width, int):
        if width >= 3840:
            return 2160
        elif width >= 2560:
            return 1440
        elif width >= 1920:
            return 1080
        elif width >= 1280:
            return 720
        elif width >= 854:
            return 480
        elif width >= 640:
            return 360
        elif width >= 426:
            return 240

    return 0


def format_quality_label(height: int) -> str:
    """Generates detailed user-friendly quality labels for all heights."""
    if height >= 2160:
        return f"📹 {height}p (4K Ultra HD)"
    elif height >= 1440:
        return f"📹 {height}p (2K QHD)"
    elif height >= 1080:
        return f"📹 {height}p (Full HD)"
    elif height >= 720:
        return f"📹 {height}p (HD)"
    elif height >= 480:
        return f"📹 {height}p (SD)"
    elif height > 0:
        return f"📹 {height}p"
    return "📹 کیفیت استاندارد"


def sanitize_telegram_text(text: str) -> str:
    """Strips risky formatting characters to prevent Telegram API Markdown parsing errors."""
    if not text:
        return ""
    for char in ['_', '*', '`', '[', ']', '(', ')', '~']:
        text = text.replace(char, ' ')
    return text.strip()


def get_spotify_token() -> Optional[str]:
    """Retrieves access token for Spotify API."""
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        try:
            url = "https://accounts.spotify.com/api/token"
            data = urllib.parse.urlencode({'grant_type': 'client_credentials'}).encode('utf-8')
            req = urllib.request.Request(url, data=data, method='POST')
            auth_header = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode('utf-8')).decode('utf-8')
            req.add_header("Authorization", f"Basic {auth_header}")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=5) as response:
                res_data = json.loads(response.read().decode())
                return res_data.get('access_token')
        except Exception as ex:
            logger.debug(f"Failed Spotify credentials auth: {ex}")

    try:
        req = urllib.request.Request("https://open.spotify.com/get_access_token", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode())
            return res_data.get('accessToken')
    except Exception as ex:
        logger.debug(f"Failed open Spotify token fetch: {ex}")
        return None


def search_spotify_track(query: str) -> Optional[Dict[str, str]]:
    """Searches Spotify database to match official track title, artist, and album details."""
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

                return {
                    'title': track_name,
                    'artist': artist_name,
                    'album': album_name,
                    'spotify_url': spotify_url
                }
    except Exception as e:
        logger.debug(f"Spotify search failed for '{clean_q}': {e}")

    return None


def extract_instagram_dd_info(url: str) -> Optional[Dict[str, Any]]:
    """Bypasses Instagram blocks using DDInstagram / VxInstagram proxy scraper."""
    try:
        match = re.search(r"instagram\.com/(?:p|reel|reels|tv|stories|share)/([A-Za-z0-9_\-]+)", url)
        if not match:
            return None
        shortcode = match.group(1)

        dd_url = f"https://ddinstagram.com/reel/{shortcode}/"

        req = urllib.request.Request(dd_url, headers={
            'User-Agent': 'TelegramBot (like TwitterBot/1.0)',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        })

        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode('utf-8', errors='ignore')

        video_match = re.search(r'<meta\s+(?:property|name)=["\']og:video(?::secure_url)?["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not video_match:
            video_match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:video(?::secure_url)?["\']', html, re.IGNORECASE)

        image_match = re.search(r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not image_match:
            image_match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:image["\']', html, re.IGNORECASE)

        desc_match = re.search(r'<meta\s+(?:property|name)=["\']og:description["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not desc_match:
            desc_match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:description["\']', html, re.IGNORECASE)

        video_url = video_match.group(1).replace("&amp;", "&") if video_match else None
        image_url = image_match.group(1).replace("&amp;", "&") if image_match else None
        caption = desc_match.group(1) if desc_match else "ویدیوی اینستاگرام"

        if video_url:
            logger.info("Successfully extracted via DDInstagram Proxy!")
            return {
                "direct_url": video_url,
                "title": caption[:100] if caption else "ویدیوی اینستاگرام",
                "description": caption,
                "formats": [
                    {"url": video_url, "ext": "mp4", "height": 1080, "vcodec": "h264"},
                    {"url": video_url, "ext": "mp4", "height": 720, "vcodec": "h264"},
                    {"url": video_url, "ext": "mp4", "height": 480, "vcodec": "h264"}
                ],
                "duration": 60,
                "thumbnail": image_url
            }
        elif image_url:
            return {
                "direct_url": image_url,
                "title": caption[:100] if caption else "تصویر اینستاگرام",
                "description": caption,
                "formats": [
                    {"url": image_url, "ext": "jpg", "height": 1080, "vcodec": "none"}
                ],
                "duration": 0,
                "thumbnail": image_url
            }
    except Exception as e:
        logger.debug(f"DDInstagram scraper error: {e}")

    return None


def extract_instagram_embed_info(url: str) -> Optional[Dict[str, Any]]:
    """Scrapes Instagram embed endpoint for media URLs."""
    try:
        match = re.search(r"instagram\.com/(?:p|reel|reels|tv|stories|share)/([A-Za-z0-9_\-]+)", url)
        if not match:
            return None
        shortcode = match.group(1)
        embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"

        req = urllib.request.Request(embed_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode('utf-8', errors='ignore')

        video_urls = re.findall(r'"video_url"\s*:\s*"([^"]+)"', html)
        if not video_urls:
            video_urls = re.findall(r'<meta\s+property="og:video"\s+content="([^"]+)"', html)

        display_urls = re.findall(r'"display_url"\s*:\s*"([^"]+)"', html)
        if not display_urls:
            display_urls = re.findall(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)

        caption_match = re.search(r'<div\s+class="Caption"[^>]*>(.*?)</div>', html, re.DOTALL)
        caption = ""
        if caption_match:
            caption = re.sub(r'<[^>]+>', '', caption_match.group(1)).strip()

        clean_video_urls = [v.replace('\\/', '/').replace('\\u0026', '&') for v in video_urls]
        clean_display_urls = [d.replace('\\/', '/').replace('\\u0026', '&') for d in display_urls]

        if clean_video_urls:
            direct_v_url = clean_video_urls[0]
            thumb = clean_display_urls[0] if clean_display_urls else None
            return {
                "direct_url": direct_v_url,
                "title": caption[:100] if caption else "ویدیوی اینستاگرام",
                "description": caption or "ویدیو استخراج شده از اینستاگرام",
                "formats": [
                    {"url": direct_v_url, "ext": "mp4", "height": 1080, "vcodec": "h264"},
                    {"url": direct_v_url, "ext": "mp4", "height": 720, "vcodec": "h264"},
                    {"url": direct_v_url, "ext": "mp4", "height": 480, "vcodec": "h264"}
                ],
                "duration": 60,
                "thumbnail": thumb
            }
        elif clean_display_urls:
            direct_img_url = clean_display_urls[0]
            return {
                "direct_url": direct_img_url,
                "title": caption[:100] if caption else "تصویر اینستاگرام",
                "description": caption or "پست تصویری اینستاگرام",
                "formats": [
                    {"url": direct_img_url, "ext": "jpg", "height": 1080, "vcodec": "none"}
                ],
                "duration": 0,
                "thumbnail": direct_img_url
            }
    except Exception as e:
        logger.debug(f"Instagram embed extraction error: {e}")
    return None


async def fetch_cobalt_fallback_info(url: str) -> Optional[Dict[str, Any]]:
    """Fallback extractor using multi-instance Cobalt nodes."""
    instances = [
        "https://api.cobalt.tools",
        "https://cobalt-api.kwiatekm.pl",
        "https://api.cobalt.redna.dev",
        "https://co.wuk.sh"
    ]

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    payload = json.dumps({"url": url}).encode('utf-8')
    loop = asyncio.get_event_loop()

    def _call_instance(api_url: str):
        try:
            req = urllib.request.Request(f"{api_url}/", data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode('utf-8'))
        except Exception:
            try:
                req = urllib.request.Request(f"{api_url}/api/json", data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=8) as resp:
                    if resp.status == 200:
                        return json.loads(resp.read().decode('utf-8'))
            except Exception as e:
                logger.debug(f"Cobalt node '{api_url}' request error: {e}")
        return None

    for inst in instances:
        res = await loop.run_in_executor(executor, lambda: _call_instance(inst))
        if res:
            status = res.get("status")
            if status in ["stream", "redirect", "tunnel"]:
                media_url = res.get("url")
                if media_url:
                    return {
                        "direct_url": media_url,
                        "title": "ویدیوی استخراج شده",
                        "description": "استخراج شده با موتور کلود پشتیبان",
                        "formats": [{"url": media_url, "ext": "mp4", "height": 720, "vcodec": "h264"}],
                        "duration": 60,
                        "thumbnail": None
                    }
            elif status == "picker":
                picker = res.get("picker", [])
                entries = []
                for p in picker:
                    p_url = p.get("url")
                    if p_url:
                        entries.append({
                            "direct_url": p_url,
                            "formats": [{"url": p_url, "ext": "mp4" if p.get("type") == "video" else "jpg", "height": 720, "vcodec": "h264" if p.get("type") == "video" else "none"}]
                        })
                if entries:
                    return {
                        "entries": entries,
                        "title": "آلبوم استخراج شده",
                        "description": "پست چندتایی"
                    }

    return None


async def extract_media_info_robust(url: str) -> Optional[Dict[str, Any]]:
    """Robust multi-layer extraction pipeline."""
    clean_url = clean_media_url(url)
    cookie_file = setup_cookies_file()

    loop = asyncio.get_event_loop()

    # Layer 1: yt-dlp Extraction
    strategies = [
        {
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            'extractor_args': {
                'youtube': {'player_client': ['android', 'ios', 'mweb', 'web']},
            }
        },
        {
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Mobile/15E148 Safari/604.1',
                'Accept': '*/*',
            },
            'extractor_args': {
                'youtube': {'player_client': ['ios', 'web']},
            }
        }
    ]

    for strat in strategies:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'nocheckcertificate': True,
            'concurrent_fragment_downloads': 16,
            'http_headers': strat.get('http_headers', {}),
        }

        if 'extractor_args' in strat:
            ydl_opts['extractor_args'] = strat['extractor_args']

        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file

        def _fetch():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(clean_url, download=False)
            except Exception as ex:
                logger.debug(f"yt-dlp extraction strategy failed: {ex}")
                return None

        result = await loop.run_in_executor(executor, _fetch)
        if result:
            return result

    # Layer 2: DDInstagram Proxy Scraper (For Instagram URLs)
    if is_instagram_url(clean_url):
        logger.info("Attempting DDInstagram Proxy Scraper...")
        dd_res = await loop.run_in_executor(executor, lambda: extract_instagram_dd_info(clean_url))
        if dd_res:
            return dd_res

        # Layer 3: Instagram Embed Scraper
        logger.info("Attempting Instagram Embed Scraper...")
        embed_res = await loop.run_in_executor(executor, lambda: extract_instagram_embed_info(clean_url))
        if embed_res:
            return embed_res

    # Layer 4: Cobalt Multi-Node Extractor Fallback
    logger.info("Attempting Cobalt API Node fallback...")
    cobalt_res = await fetch_cobalt_fallback_info(clean_url)
    if cobalt_res:
        return cobalt_res

    return None


async def download_media_video(url: str, param: str, output_prefix: str, direct_url: Optional[str] = None) -> Optional[str]:
    """Downloads requested video quality using multi-stage format specs or direct stream."""
    cookie_file = setup_cookies_file()
    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    # Direct CDN Stream handling if extracted from proxy/embed
    if direct_url:
        target_path = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.mp4")
        try:
            req = urllib.request.Request(direct_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            })
            with urllib.request.urlopen(req, timeout=180) as resp, open(target_path, 'wb') as out_file:
                shutil.copyfileobj(resp, out_file)
            if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                return target_path
        except Exception as e:
            logger.error(f"Failed direct CDN download: {e}")

    format_specs = []
    if param and param.isdigit():
        height = param
        format_specs.append(f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={height}]+bestaudio/best[height<={height}]/best")
        format_specs.append(f"b[height<={height}]/best")
    elif param and param != "best":
        format_specs.append(f"{param}+bestaudio/best")
        format_specs.append(param)

    format_specs.append("bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best")
    format_specs.append("best")

    loop = asyncio.get_event_loop()

    for fmt in format_specs:
        ydl_opts = {
            'format': fmt,
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'merge_output_format': 'mp4',
            'nocheckcertificate': True,
            'concurrent_fragment_downloads': 16,
            'buffersize': 4096 * 1024,
            'extractor_args': {
                'youtube': {'player_client': ['android', 'ios', 'mweb']},
            },
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36',
            }
        }

        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file

        def _download():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return ydl.prepare_filename(info)
            except Exception as ex:
                logger.debug(f"Video download failed with format '{fmt}': {ex}")
                return None

        await loop.run_in_executor(executor, _download)

        matching = glob.glob(os.path.join(DOWNLOAD_DIR, f"{output_prefix}.*"))
        if matching:
            return matching[0]

    return None


async def download_media_audio(url: str, bitrate: str, output_prefix: str) -> Optional[str]:
    """Extracts MP3 audio track from video with high-speed FFmpeg conversion."""
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
    """Performs deep multi-stage Spotify + YouTube Music search pipeline."""
    queries_to_try = []

    clean_t = clean_music_query(track_title)
    clean_a = clean_music_query(artist_name)
    extracted_title, extracted_artist = extract_music_info_from_caption(caption)

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
    cookie_file = setup_cookies_file()

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

    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

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
    """Sends custom greeting message on /start command."""
    welcome_text = (
        "درود به روی ماهت 🧘🏾🌚\n"
        "من ربات دانلودرم 🧸\n\n"
        "با من می‌تونی ویدئو ها، موزیک ها و پست های هر پلتفرمی رو که بخوای بدون محدودیت دانلود کنی 🧘🏾✨️\n\n"
        "و همچنین میتونی موزیک پست دلخواهت را با استفاده از من پیدا و دانلود کنی 🧘🏾🎧\n\n"
        "کافیه فقط لینک پستی دلخواهت رو برام بفرستی 🧸"
    )
    await update.message.reply_text(welcome_text)


async def handle_media_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming Instagram and YouTube links safely without crashing."""
    url = update.message.text.strip()

    if not is_supported_url(url):
        return

    cleanup_temp_files()
    clean_expired_cache()

    status_msg = await update.message.reply_text("🔎 در حال بررسی لینک و استخراج تمامی کیفیت‌ها...")

    try:
        info = await extract_media_info_robust(url)
    except Exception as e:
        logger.error(f"Error during media extraction: {e}")
        info = None

    if not info:
        await status_msg.edit_text("❌ متأسفانه دریافت اطلاعات پست/ویدیو با خطا مواجه شد. از عمومی (Public) بودن لینک اطمینان حاصل کنید.")
        return

    cache_id = str(uuid.uuid4())[:8]

    entries = info.get('entries')
    is_album = bool(entries and len(entries) > 1)
    total_slides = len(entries) if is_album else 1
    current_slide = 0

    main_item = entries[current_slide] if is_album else info

    formats = main_item.get('formats', [])
    duration = float(main_item.get('duration') or info.get('duration') or 0)
    thumbnail = main_item.get('thumbnail') or info.get('thumbnail', '')
    raw_caption = info.get('description') or info.get('title') or "ویدیو / پست"
    direct_url = main_item.get('direct_url') or info.get('direct_url')

    track_title = main_item.get('track') or main_item.get('alt_title') or ""
    artist_name = main_item.get('artist') or main_item.get('creator') or main_item.get('uploader') or ""

    if not track_title:
        extracted_t, extracted_a = extract_music_info_from_caption(raw_caption)
        track_title = extracted_t
        if extracted_a and not artist_name:
            artist_name = extracted_a

    safe_caption = sanitize_telegram_text(raw_caption[:400])
    short_caption = safe_caption + "..." if len(raw_caption) > 400 else safe_caption

    valid_formats = []
    seen_heights = set()

    for f in formats:
        vcodec = f.get('vcodec', 'none')
        if vcodec == 'none':
            continue

        height = extract_resolution_height(f)
        if height > 0 and height not in seen_heights:
            seen_heights.add(height)
            size_bytes = estimate_format_filesize(f, duration)
            valid_formats.append({
                'height': height,
                'label': format_quality_label(height),
                'size_str': format_size(size_bytes)
            })

    valid_formats.sort(key=lambda x: x['height'], reverse=True)

    keyboard = []

    for fmt in valid_formats:
        btn_text = f"{fmt['label']} - ({fmt['size_str']})"
        callback_data = f"vid:{cache_id}:{current_slide}:{fmt['height']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

    if not keyboard:
        keyboard.append([
            InlineKeyboardButton("📹 دانلود ویدیو (بهترین کیفیت)", callback_data=f"vid:{cache_id}:{current_slide}:best")
        ])

    if is_album:
        nav_row = []
        if current_slide > 0:
            nav_row.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"slide:{cache_id}:{current_slide - 1}"))
        nav_row.append(InlineKeyboardButton(f"اسلاید {current_slide + 1} از {total_slides}", callback_data="noop"))
        if current_slide < total_slides - 1:
            nav_row.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"slide:{cache_id}:{current_slide + 1}"))
        keyboard.append(nav_row)

    keyboard.append([
        InlineKeyboardButton("🎵 ویس ویدیو 128", callback_data=f"aud:{cache_id}:{current_slide}:128"),
        InlineKeyboardButton("🎵 ویس ویدیو 320", callback_data=f"aud:{cache_id}:{current_slide}:320"),
    ])

    keyboard.append([
        InlineKeyboardButton("🎧 دانلود کامل موزیک اصلی (Spotify / YouTube)", callback_data=f"fullm:{cache_id}:{current_slide}:hq")
    ])

    extra_row = []
    if thumbnail:
        extra_row.append(InlineKeyboardButton("🖼 کاور اصلی", callback_data=f"img:{cache_id}:{current_slide}"))
    if len(raw_caption) > 400:
        extra_row.append(InlineKeyboardButton("📜 کپشن کامل", callback_data=f"cap:{cache_id}:{current_slide}"))

    if extra_row:
        keyboard.append(extra_row)

    MEDIA_CACHE[cache_id] = {
        "url": url,
        "direct_url": direct_url,
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

    caption_text = f"📝 کپشن:\n{short_caption}\n\nکیفیت یا گزینه مورد نظر را انتخاب کنید:"

    sent_successfully = False

    if thumbnail:
        try:
            await update.message.reply_photo(
                photo=thumbnail,
                caption=caption_text,
                reply_markup=reply_markup
            )
            sent_successfully = True
        except Exception as ex:
            logger.warning(f"Could not send photo thumbnail: {ex}")

    if not sent_successfully:
        try:
            await update.message.reply_text(
                text=caption_text,
                reply_markup=reply_markup
            )
            sent_successfully = True
        except Exception as ex:
            logger.error(f"Failed to send menu message: {ex}")
            await status_msg.edit_text("❌ خطا در نمایش منوی دانلود. لطفاً مجدداً تلاش کنید.")
            return

    # Delete checking message AFTER menu is safely sent
    try:
        await status_msg.delete()
    except Exception:
        pass


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button clicks."""
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
    direct_url = cached_item.get("direct_url")
    file_prefix = f"file_{uuid.uuid4().hex[:6]}"

    if action_type == "vid":
        status_msg = await query.message.reply_text("⏳ در حال دانلود ویدیو... لطفاً صبور باشید.")
        filepath = await download_media_video(url, param, file_prefix, direct_url=direct_url)

        try:
            if filepath and os.path.exists(filepath):
                filesize = os.path.getsize(filepath)
                if filesize > 50 * 1024 * 1024:
                    await status_msg.edit_text("❌ حجم ویدیو بیشتر از محدودیّت ۵۰ مگابایت تلگرام است.")
                    return

                await status_msg.edit_text("⬆️ در حال آپلود ویدیو به تلگرام...")
                with open(filepath, 'rb') as video_file:
                    await query.message.reply_video(
                        video=video_file,
                        caption="✨ دانلود شده توسط ربات دانلودر",
                        supports_streaming=True,
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=300
                    )
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ خطا در دانلود ویدیو. ممکن است لینک منقضی شده یا دسترسی ویدیو محدود باشد.")
        except Exception as e:
            logger.error(f"Error uploading video: {e}")
            await status_msg.edit_text("❌ خطا در ارسال ویدیو به تلگرام.")
        finally:
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass

    elif action_type == "aud":
        status_msg = await query.message.reply_text(f"⏳ در حال استخراج ویس با کیفیت {param}kbps...")
        filepath = await download_media_audio(url, param, file_prefix)

        try:
            if filepath and os.path.exists(filepath):
                await status_msg.edit_text("⬆️ در حال ارسال فایل صوتی...")
                with open(filepath, 'rb') as audio_file:
                    await query.message.reply_audio(
                        audio=audio_file,
                        caption=f"🎵 ویس استخراج شده با کیفیت {param}kbps",
                        read_timeout=300,
                        write_timeout=300
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
                sp_note = f"\n🌐 لینک اسپاتیفای: {res['spotify_url']}" if res.get('spotify_url') else ""
                caption_audio = f"🎧 موزیک کامل اورجینال:\n🎵 عنوان: {res['title']}\n👤 خواننده: {res['uploader']}\n✨ کیفیت: 320kbps (HQ){sp_note}"

                with open(res['filepath'], 'rb') as audio_file:
                    await query.message.reply_audio(
                        audio=audio_file,
                        title=res['title'],
                        performer=res['uploader'],
                        caption=caption_audio,
                        read_timeout=300,
                        write_timeout=300
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
        clean_full_cap = sanitize_telegram_text(full_cap)
        await query.message.reply_text(f"📜 کپشن کامل:\n\n{clean_full_cap}")


async def post_init(application: Application):
    """Executes on startup to drop pending updates and release active webhooks from old bot instances."""
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Successfully dropped pending updates and cleared old active connections.")
    except Exception as ex:
        logger.warning(f"Failed to clear old connections on startup: {ex}")


def main():
    """Main entry point for starting Telegram bot application."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.error("BOT_TOKEN variable is not set. Please specify BOT_TOKEN in environment.")
        sys.exit(1)

    logger.info("Starting Telegram Media Downloader Bot...")

    setup_cookies_file()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_media_link))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
