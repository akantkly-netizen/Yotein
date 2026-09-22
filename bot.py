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
import subprocess
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import urllib.request
from typing import Dict, Any, Optional, List, Tuple

import yt_dlp
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Optional ShazamIO Integration
try:
    from shazamio import Shazam, Serialize
    HAS_SHAZAMIO = True
except ImportError:
    HAS_SHAZAMIO = False

# ---------------------------------------------------------------------------
# CONFIGURATION, HARDENING CONSTANTS & LOGGING
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("MediaDownloaderBot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DOWNLOAD_DIR = "temp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
COOKIES_FILE_ENV = os.getenv("COOKIES_FILE", "cookies.txt")

MAX_CONCURRENT_JOBS = 3
JOB_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
executor = ThreadPoolExecutor(max_workers=8)

MEDIA_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_EXPIRATION_SECONDS = 3600  # 1 hour TTL cache

_spotify_access_token: Optional[str] = None
_spotify_token_expires_at: float = 0.0

VALID_MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4a", ".mp3", ".ogg", ".flac", ".wav"}

# Music Search & Validation Hardening Threshold Constants
MIN_TITLE_SIMILARITY = 0.40
MIN_ARTIST_SIMILARITY = 0.30
MAX_DURATION_DIFF_SECONDS = 90.0
MIN_TOTAL_MATCH_SCORE = 0.45

# ---------------------------------------------------------------------------
# UTILITIES & HOST / URL HANDLING
# ---------------------------------------------------------------------------

def check_ffmpeg_installed() -> bool:
    """Verifies ffmpeg and ffprobe presence in the system PATH."""
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    ffprobe_ok = shutil.which("ffprobe") is not None
    if not ffmpeg_ok or not ffprobe_ok:
        logger.warning("FFmpeg or FFprobe is NOT found in system PATH. Media transcoding might fail.")
        return False
    return True

def html_escape(text: str) -> str:
    """Escapes user input strings for safe Telegram HTML formatting."""
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def normalize_host(host: str) -> str:
    """Strips port and www prefix, converting to clean lowercase hostname."""
    if not host:
        return ""
    host = host.lower().strip()
    if ":" in host:
        host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host

YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
INSTAGRAM_HOSTS = {"instagram.com", "instagr.am"}
TIKTOK_HOSTS = {"tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}
SPOTIFY_HOSTS = {"spotify.com", "open.spotify.com"}

def is_youtube_url(url: str) -> bool:
    if not url: return False
    try:
        host = normalize_host(urllib.parse.urlparse(url).netloc)
        return host in YOUTUBE_HOSTS
    except Exception:
        return False

def is_instagram_url(url: str) -> bool:
    if not url: return False
    try:
        host = normalize_host(urllib.parse.urlparse(url).netloc)
        return host in INSTAGRAM_HOSTS
    except Exception:
        return False

def is_tiktok_url(url: str) -> bool:
    if not url: return False
    try:
        host = normalize_host(urllib.parse.urlparse(url).netloc)
        return host in TIKTOK_HOSTS
    except Exception:
        return False

def is_spotify_url(url: str) -> bool:
    if not url: return False
    try:
        host = normalize_host(urllib.parse.urlparse(url).netloc)
        return host in SPOTIFY_HOSTS
    except Exception:
        return False

def is_supported_url(url: str) -> bool:
    if not url: return False
    try:
        host = normalize_host(urllib.parse.urlparse(url).netloc)
        all_supported = YOUTUBE_HOSTS | INSTAGRAM_HOSTS | TIKTOK_HOSTS | SPOTIFY_HOSTS
        return host in all_supported
    except Exception:
        return False

def extract_urls_from_text(text: str) -> List[str]:
    """Extracts all valid HTTP/HTTPS URLs embedded inside a user message and strips trailing punctuation."""
    if not text:
        return []
    url_pattern = r'https?://[^\s<>"{}|\\^`]+'
    raw_urls = re.findall(url_pattern, text)
    cleaned_urls = []
    punctuation_to_strip = ".,!?:;)]}"

    for u in raw_urls:
        while u and u[-1] in punctuation_to_strip:
            u = u[:-1]
        if u:
            cleaned_urls.append(u)
    return cleaned_urls

def clean_media_url(url: str) -> str:
    """Removes tracking query parameters & fragments while strictly preserving essential video IDs & structure."""
    if not url:
        return ""
    url = url.strip()
    try:
        parsed = urllib.parse.urlparse(url)
        host = normalize_host(parsed.netloc)

        if host in YOUTUBE_HOSTS:
            if host == "youtu.be":
                video_id = parsed.path.lstrip("/")
                query_params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                allowed_yt = {"list", "index", "t", "start"}
                cleaned_query = {k: v for k, v in query_params.items() if k in allowed_yt}
                if video_id:
                    cleaned_query["v"] = [video_id]
                new_query_str = urllib.parse.urlencode(cleaned_query, doseq=True)
                return urllib.parse.urlunparse(('https', 'www.youtube.com', '/watch', '', new_query_str, ''))
            else:
                query_params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                allowed_yt = {"v", "list", "index", "t", "start"}
                cleaned_query = {k: v for k, v in query_params.items() if k in allowed_yt}
                new_query_str = urllib.parse.urlencode(cleaned_query, doseq=True)
                return urllib.parse.urlunparse((parsed.scheme or 'https', parsed.netloc, parsed.path, parsed.params, new_query_str, ''))

        query_params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        tracking_keys = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "si", "feature", "igsh", "share_id", "is_copy_url"}
        cleaned_query = {k: v for k, v in query_params.items() if k.lower() not in tracking_keys}
        new_query_str = urllib.parse.urlencode(cleaned_query, doseq=True)

        return urllib.parse.urlunparse((parsed.scheme or 'https', parsed.netloc, parsed.path, parsed.params, new_query_str, ''))
    except Exception as e:
        logger.debug(f"URL cleaning error: {e}")
        return url

def setup_cookies_file() -> Optional[str]:
    """Checks and returns active cookies file path if available."""
    if os.path.exists(COOKIES_FILE_ENV) and os.path.getsize(COOKIES_FILE_ENV) > 10:
        return COOKIES_FILE_ENV
    return None

def cleanup_job_files(prefix: str):
    """Deletes temporary files matching the job prefix."""
    if not prefix:
        return
    try:
        for filepath in glob.glob(os.path.join(DOWNLOAD_DIR, f"{prefix}*")):
            if os.path.isfile(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"Error cleaning files for prefix {prefix}: {e}")

def get_cached_slide(cache_id: str, slide_idx: int) -> Optional[Dict[str, Any]]:
    """Safely fetches a slide from cache by ID and index."""
    cached = MEDIA_CACHE.get(cache_id)
    if not cached:
        return None
    slides = cached.get("slides", [])
    if 0 <= slide_idx < len(slides):
        return slides[slide_idx]
    return None

def clean_expired_cache():
    """Removes expired items from MEDIA_CACHE safely."""
    now = time.time()
    expired_keys = []
    for k, v in list(MEDIA_CACHE.items()):
        try:
            created_at = v.get("created_at") or v.get("timestamp", 0)
            if now - created_at > CACHE_EXPIRATION_SECONDS:
                expired_keys.append(k)
        except Exception:
            expired_keys.append(k)
    for k in expired_keys:
        MEDIA_CACHE.pop(k, None)

def extract_resolution_height(format_dict: Dict[str, Any]) -> int:
    """Extracts format resolution height safely."""
    h = format_dict.get('height')
    if isinstance(h, int) and h > 0:
        return h
    res_str = str(format_dict.get('resolution', ''))
    match = re.search(r'(\d{3,4})p?', res_str)
    return int(match.group(1)) if match else 0

def format_quality_label(height: int) -> str:
    """Returns resolution display string."""
    if height >= 2160: return "🎬 4K (2160p)"
    if height >= 1440: return "🎬 2K (1440p)"
    if height >= 1080: return "📹 Full HD (1080p)"
    if height >= 720:  return "📹 HD (720p)"
    if height >= 480:  return "📱 SD (480p)"
    if height >= 360:  return "📱 Low (360p)"
    return f"📱 {height}p"

def format_size(bytes_val: float) -> str:
    """Formats bytes to MB/GB string or returns 'حجم نامشخص' if size is unknown."""
    if not bytes_val or bytes_val <= 0:
        return "حجم نامشخص"
    mb = bytes_val / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.1f} MB"

def estimate_format_filesize(format_dict: Dict[str, Any], duration: float) -> float:
    """Calculates estimated filesize in bytes strictly without inventing values."""
    filesize = format_dict.get('filesize')
    if filesize and isinstance(filesize, (int, float)) and filesize > 0:
        return float(filesize)

    filesize_approx = format_dict.get('filesize_approx')
    if filesize_approx and isinstance(filesize_approx, (int, float)) and filesize_approx > 0:
        return float(filesize_approx)

    tbr = format_dict.get('tbr')
    if not tbr or not isinstance(tbr, (int, float)) or tbr <= 0:
        vbr = format_dict.get('vbr') or 0
        abr = format_dict.get('abr') or 0
        if isinstance(vbr, (int, float)) and isinstance(abr, (int, float)):
            tbr = vbr + abr
        else:
            tbr = 0

    if tbr and isinstance(tbr, (int, float)) and tbr > 0 and duration and duration > 0:
        return (float(tbr) * 1024 / 8) * float(duration)

    return 0.0

def clean_music_query(text: str) -> str:
    """Cleans captions and query strings by stripping noise, tags, and emojis."""
    if not text:
        return ""
    t = re.sub(r'https?://\S+', '', text)
    t = re.sub(r'#\w+', '', t)
    t = re.sub(r'@\w+', '', t)
    noise_patterns = [
        r'دانلود\s+موزیک', r'دانلود\s+آهنگ', r'اهنگ\s+جدید', r'آهنگ\s+جدید',
        r'پست\s+جدید', r'ریمیکس\s+جدید', r'فول\s+آلبوم', r'لینک\s+بیو',
        r'اصلی', r'کامل', r'استوری', r'اکسپلور', r'اینستاگرام', r'تیک\s*تاک', r'تیکتاک'
    ]
    for p in noise_patterns:
        t = re.sub(p, '', t, flags=re.IGNORECASE)
    t = re.sub(r'[^\w\s\-\.]', ' ', t)
    return " ".join(t.split())

def extract_music_info_from_caption(caption: str) -> Tuple[str, str]:
    """Extracts song title and artist from post caption using regex."""
    if not caption:
        return "", ""
    mashup_match = re.search(r"([A-Za-z0-9\s]{2,25})\s*(?:[x×X]|vs|VS|\/)\s*([A-Za-z0-9\s]{2,25})", caption)
    if mashup_match:
        g1, g2 = clean_music_query(mashup_match.group(1)), clean_music_query(mashup_match.group(2))
        return f"{g1} x {g2}", "Mashup Mix"

    patterns = [
        r"(?:song|music|track|آهنگ|موزیک)\s*:\s*([^-\n]+)\s*-\s*([^\n]+)",
        r"([A-Za-z0-9\s]{2,30})\s*[-–—]\s*([A-Za-z0-9\s]{2,30})",
        r"♬\s*([^-\n]+)\s*-\s*([^\n]+)",
        r"♫\s*([^-\n]+)\s*-\s*([^\n]+)"
    ]
    for p in patterns:
        match = re.search(p, caption, re.IGNORECASE)
        if match:
            g1, g2 = match.group(1).strip(), match.group(2).strip()
            return clean_music_query(g1), clean_music_query(g2)
    clean_cap = clean_music_query(caption)
    return clean_cap[:60], ""

# ---------------------------------------------------------------------------
# MEDIA FILE FINDING, STALE FILE PROTECTION & HEIGHT VALIDATION
# ---------------------------------------------------------------------------

def validate_downloaded_video_height(filepath: str, max_height: Optional[int]) -> bool:
    """
    Validates that the downloaded video file exists, has a valid video stream,
    and its height does not exceed max_height.
    If max_height is None or <= 0, validation passes (unrestricted 'best').
    """
    if not filepath or not os.path.exists(filepath):
        return False
    try:
        if os.path.getsize(filepath) <= 0:
            return False
    except Exception:
        return False

    if max_height is None or max_height <= 0:
        return True

    meta = sync_get_video_metadata(filepath)
    actual_height = meta.get("height", 0)
    if actual_height <= 0:
        logger.warning(f"Failed to probe video height or no video stream found for {filepath}")
        return False

    if actual_height > max_height:
        logger.warning(f"Video height validation failed: actual height {actual_height}p exceeds max_height {max_height}p")
        return False

    return True

def find_downloaded_media(job_prefix: str, min_mtime: float = 0.0) -> Optional[str]:
    """
    Locates downloaded video/media file for a given job_prefix.
    Rejects stale files created prior to min_mtime (with tolerance).
    """
    if not job_prefix:
        return None

    pattern = os.path.join(DOWNLOAD_DIR, f"{job_prefix}.*")
    candidates = glob.glob(pattern)

    valid_files = []
    tolerance = 2.0  # seconds filesystem timestamp tolerance
    effective_min_mtime = min_mtime - tolerance if min_mtime > 0 else 0.0

    for filepath in candidates:
        if not os.path.isfile(filepath):
            continue

        filename = os.path.basename(filepath)

        if (filename.startswith("thumb_") or 
            filename.startswith("prep_") or 
            filename.startswith("slice_") or 
            "_slice_" in filename):
            continue

        ext = os.path.splitext(filename)[1].lower()
        if ext in VALID_MEDIA_EXTENSIONS:
            try:
                st_mtime = os.path.getmtime(filepath)
                if os.path.getsize(filepath) > 0 and st_mtime >= effective_min_mtime:
                    valid_files.append((filepath, st_mtime))
            except Exception:
                pass

    if not valid_files:
        return None

    valid_files.sort(key=lambda x: x[1], reverse=True)
    return valid_files[0][0]

def find_downloaded_audio(job_prefix: str, min_mtime: float = 0.0) -> Optional[str]:
    """
    Locates extracted audio file for a given job_prefix.
    Rejects stale files created prior to min_mtime (with tolerance).
    """
    if not job_prefix:
        return None

    pattern = os.path.join(DOWNLOAD_DIR, f"{job_prefix}.*")
    candidates = glob.glob(pattern)

    audio_extensions = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac"}
    valid_files = []
    tolerance = 2.0
    effective_min_mtime = min_mtime - tolerance if min_mtime > 0 else 0.0

    for filepath in candidates:
        if not os.path.isfile(filepath):
            continue

        filename = os.path.basename(filepath)
        if filename.startswith("thumb_") or filename.startswith("prep_") or "_slice_" in filename:
            continue

        ext = os.path.splitext(filename)[1].lower()
        if ext in audio_extensions:
            try:
                st_mtime = os.path.getmtime(filepath)
                if os.path.getsize(filepath) > 0 and st_mtime >= effective_min_mtime:
                    is_mp3 = 1 if ext == ".mp3" else 0
                    valid_files.append((is_mp3, st_mtime, filepath))
            except Exception:
                pass

    if not valid_files:
        return None

    valid_files.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return valid_files[0][2]

# ---------------------------------------------------------------------------
# FFMPEG PROCESSING & TRANSCODING PIPELINE (HARDENED)
# ---------------------------------------------------------------------------

def sync_get_video_metadata(filepath: str) -> Dict[str, Any]:
    """Runs ffprobe synchronously to fetch media metadata."""
    meta = {"duration": 0, "width": 0, "height": 0, "vcodec": "", "acodec": ""}
    if not filepath or not os.path.exists(filepath):
        return meta

    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", filepath
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=12)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            fmt = data.get("format", {})
            meta["duration"] = int(float(fmt.get("duration", 0)))
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video" and not meta["vcodec"]:
                    meta["vcodec"] = stream.get("codec_name", "")
                    meta["width"] = int(stream.get("width", 0))
                    meta["height"] = int(stream.get("height", 0))
                elif stream.get("codec_type") == "audio" and not meta["acodec"]:
                    meta["acodec"] = stream.get("codec_name", "")
    except Exception as e:
        logger.debug(f"ffprobe metadata extraction error: {e}")
    return meta

async def prepare_telegram_video(filepath: str, job_prefix: str) -> Dict[str, Any]:
    """
    Ensures Telegram compatibility:
    - Normalizes video to H.264 + AAC (MP4 container).
    - Compresses video if file exceeds 49MB (Telegram Bot API limit).
    - Hardened: If FFmpeg fails, operations strictly fail (no return of broken files).
    - Validates output using FFprobe.
    """
    if not filepath or not os.path.exists(filepath):
        return {"filepath": None, "error": "File missing"}

    loop = asyncio.get_running_loop()
    meta = await loop.run_in_executor(executor, lambda: sync_get_video_metadata(filepath))
    filesize_mb = os.path.getsize(filepath) / (1024 * 1024)
    vcodec = meta.get("vcodec", "").lower()
    acodec = meta.get("acodec", "").lower()

    needs_transcode = (
        vcodec not in ["h264", "avc1"] or
        acodec not in ["aac", "mp4a"] or
        filesize_mb > 48.0 or
        not filepath.lower().endswith(".mp4")
    )

    processed_path = filepath

    if needs_transcode:
        out_path = os.path.join(DOWNLOAD_DIR, f"prep_{job_prefix}.mp4")
        duration = meta.get("duration", 0) or 60

        def _do_transcode() -> bool:
            ffmpeg_cmd = ["ffmpeg", "-y", "-i", filepath]
            if filesize_mb > 48.0 and duration > 0:
                target_size_bits = 45 * 8 * 1024 * 1024  # ~45MB target
                target_total_bitrate = int(target_size_bits / duration)
                audio_bitrate = 128000
                video_bitrate = max(200000, target_total_bitrate - audio_bitrate)
                ffmpeg_cmd.extend([
                    "-c:v", "libx264", "-b:v", f"{video_bitrate}", "-preset", "fast",
                    "-c:a", "aac", "-b:a", "128k", "-vf", "scale='min(1280,iw)':-2"
                ])
            else:
                ffmpeg_cmd.extend([
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k"
                ])

            ffmpeg_cmd.extend([
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                out_path
            ])

            res = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            if res.returncode != 0:
                logger.error(f"FFmpeg transcode failed: {res.stderr.decode('utf-8', errors='ignore')}")
                return False
            
            if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                logger.error("FFmpeg transcode output file is missing or zero size.")
                return False

            return True

        success = await loop.run_in_executor(executor, _do_transcode)

        if not success:
            cleanup_job_files(f"prep_{job_prefix}")
            return {"filepath": None, "error": "FFmpeg transcoding failed"}

        if filepath != out_path and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass
        processed_path = out_path
        meta = await loop.run_in_executor(executor, lambda: sync_get_video_metadata(processed_path))
        if meta.get("width", 0) == 0 and meta.get("height", 0) == 0:
            logger.error("FFprobe validation failed on transcoded file.")
            return {"filepath": None, "error": "Transcoded file validation failed"}

    # Thumbnail Generation
    thumb_path = None
    if meta.get("width") and meta.get("height"):
        t_path = os.path.join(DOWNLOAD_DIR, f"thumb_{job_prefix}.jpg")
        def _gen_thumb():
            cmd_thumb = [
                "ffmpeg", "-y", "-ss", "00:00:01", "-i", processed_path,
                "-vframes", "1", "-vf", "scale=320:-1", t_path
            ]
            subprocess.run(cmd_thumb, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)

        await loop.run_in_executor(executor, _gen_thumb)
        if os.path.exists(t_path) and os.path.getsize(t_path) > 0:
            thumb_path = t_path

    return {
        "filepath": processed_path,
        "width": meta.get("width", 0),
        "height": meta.get("height", 0),
        "duration": meta.get("duration", 0),
        "thumb_path": thumb_path,
        "filesize": os.path.getsize(processed_path) if os.path.exists(processed_path) else 0
    }

# ---------------------------------------------------------------------------
# MUSIC SEARCH ENGINES (Spotify, iTunes, Deezer) WITH HARDENED MULTI-ENGINE SELECTION
# ---------------------------------------------------------------------------

def get_spotify_token() -> Optional[str]:
    """Retrieves or refreshes Spotify API access token."""
    global _spotify_access_token, _spotify_token_expires_at
    if _spotify_access_token and time.time() < _spotify_token_expires_at:
        return _spotify_access_token
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    try:
        auth_bytes = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode('utf-8')
        auth_header = base64.b64encode(auth_bytes).decode('utf-8')
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=urllib.parse.urlencode({'grant_type': 'client_credentials'}).encode('utf-8'),
            headers={
                'Authorization': f'Basic {auth_header}',
                'Content-Type': 'application/x-www-form-urlencoded'
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=6) as response:
            data = json.loads(response.read().decode())
            _spotify_access_token = data.get('access_token')
            expires_in = data.get('expires_in', 3600)
            _spotify_token_expires_at = time.time() + expires_in - 60
            return _spotify_access_token
    except Exception as e:
        logger.debug(f"Spotify token retrieval failed: {e}")
        return None

def get_spotify_candidates(query: str) -> List[Dict[str, Any]]:
    """Fetches candidate tracks from Spotify Web API."""
    token = get_spotify_token()
    if not token or not query:
        return []
    try:
        q_enc = urllib.parse.quote(query)
        req_url = f"https://api.spotify.com/v1/search?q={q_enc}&type=track&limit=5"
        req = urllib.request.Request(req_url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req, timeout=6) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode('utf-8'))
                items = data.get('tracks', {}).get('items', [])
                candidates = []
                for tr in items:
                    artists = ", ".join([a.get('name', '') for a in tr.get('artists', [])])
                    album_art = tr.get('album', {}).get('images', [{}])[0].get('url', '')
                    duration = float(tr.get('duration_ms', 0)) / 1000.0
                    candidates.append({
                        'title': tr.get('name', ''),
                        'artist': artists,
                        'album': tr.get('album', {}).get('name', ''),
                        'duration': duration,
                        'cover': album_art,
                        'spotify_url': tr.get('external_urls', {}).get('spotify', ''),
                        'source': 'Spotify Engine'
                    })
                return candidates
    except Exception as e:
        logger.debug(f"Spotify candidates fetch error: {e}")
    return []

def get_itunes_candidates(query: str) -> List[Dict[str, Any]]:
    """Fetches candidate tracks from iTunes API."""
    if not query:
        return []
    try:
        q_enc = urllib.parse.quote(query)
        req_url = f"https://itunes.apple.com/search?term={q_enc}&entity=song&limit=5"
        req = urllib.request.Request(req_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=6) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode('utf-8'))
                results = data.get('results', [])
                candidates = []
                for tr in results:
                    cover_hq = tr.get('artworkUrl100', '').replace('100x100bb', '1000x1000bb')
                    duration = float(tr.get('trackTimeMillis', 0)) / 1000.0
                    candidates.append({
                        'title': tr.get('trackName', ''),
                        'artist': tr.get('artistName', ''),
                        'album': tr.get('collectionName', ''),
                        'genre': tr.get('primaryGenreName', ''),
                        'duration': duration,
                        'cover': cover_hq,
                        'source': 'Apple Music Engine'
                    })
                return candidates
    except Exception as e:
        logger.debug(f"iTunes candidates fetch error: {e}")
    return []

def get_deezer_candidates(query: str) -> List[Dict[str, Any]]:
    """Fetches candidate tracks from Deezer API."""
    if not query:
        return []
    try:
        q_enc = urllib.parse.quote(query)
        req_url = f"https://api.deezer.com/search?q={q_enc}&limit=5"
        req = urllib.request.Request(req_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=6) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode('utf-8'))
                items = data.get('data', [])
                candidates = []
                for tr in items:
                    duration = float(tr.get('duration', 0))
                    candidates.append({
                        'title': tr.get('title', ''),
                        'artist': tr.get('artist', {}).get('name', ''),
                        'album': tr.get('album', {}).get('title', ''),
                        'duration': duration,
                        'cover': tr.get('album', {}).get('cover_xl') or tr.get('album', {}).get('cover_medium', ''),
                        'source': 'Deezer Engine'
                    })
                return candidates
    except Exception as e:
        logger.debug(f"Deezer candidates fetch error: {e}")
    return []

def select_best_metadata_candidate(candidates: List[Dict[str, Any]], query: str) -> Optional[Dict[str, Any]]:
    """Evaluates candidates against query using similarity scoring and minimum threshold."""
    if not candidates or not query:
        return None

    clean_q = clean_music_query(query)
    best_candidate = None
    best_score = 0.0

    for cand in candidates:
        c_title = cand.get('title', '')
        c_artist = cand.get('artist', '')
        c_str = f"{c_artist} {c_title}".strip()

        title_sim = string_similarity(c_title, clean_q)
        full_sim = string_similarity(c_str, clean_q)
        sim_score = max(title_sim, full_sim)

        if sim_score > best_score:
            best_score = sim_score
            best_candidate = cand

    if best_candidate and best_score >= MIN_TOTAL_MATCH_SCORE:
        best_candidate['confidence_score'] = best_score
        return best_candidate

    return None

def search_spotify_track(query: str) -> Optional[Dict[str, Any]]:
    cands = get_spotify_candidates(query)
    return select_best_metadata_candidate(cands, query)

def search_itunes_track(query: str) -> Optional[Dict[str, Any]]:
    cands = get_itunes_candidates(query)
    return select_best_metadata_candidate(cands, query)

def search_deezer_track(query: str) -> Optional[Dict[str, Any]]:
    cands = get_deezer_candidates(query)
    return select_best_metadata_candidate(cands, query)

def search_multi_engine_track(query: str) -> Optional[Dict[str, Any]]:
    """
    HARDENED MULTI-ENGINE METADATA SEARCH:
    Queries Spotify, iTunes, and Deezer APIs simultaneously.
    DOES NOT stop at first success. Aggregates all valid candidates from all three engines,
    selects the highest confidence canonical metadata, and returns None if confidence is low.
    Spotify remains strictly metadata/search only.
    """
    clean_q = clean_music_query(query)
    if not clean_q or len(clean_q) < 2:
        return None

    all_candidates: List[Dict[str, Any]] = []

    # Query all three engines
    all_candidates.extend(get_spotify_candidates(clean_q))
    all_candidates.extend(get_itunes_candidates(clean_q))
    all_candidates.extend(get_deezer_candidates(clean_q))

    if not all_candidates:
        return None

    return select_best_metadata_candidate(all_candidates, clean_q)

def extract_spotify_info_oembed(url: str) -> Optional[Dict[str, Any]]:
    """Extracts Spotify track details via oEmbed API."""
    try:
        req_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(url)}"
        req = urllib.request.Request(req_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=6) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode('utf-8'))
                full_title = data.get('title', '')
                parts = full_title.split(' - ')
                title = parts[0] if len(parts) > 0 else full_title
                artist = parts[1] if len(parts) > 1 else "Spotify Artist"
                return {
                    'title': title,
                    'artist': artist,
                    'thumbnail': data.get('thumbnail_url', '')
                }
    except Exception as e:
        logger.debug(f"Spotify oEmbed error: {e}")
    return None

# ---------------------------------------------------------------------------
# MUSIC INTELLIGENCE, HARD VALIDATION & CANDIDATE MATCHING ENGINE
# ---------------------------------------------------------------------------

def normalize_music_text(text: str) -> str:
    """Normalizes music title and artist strings for comparison."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'#\w+', '', text)
    text = re.sub(r'@\w+', '', text)

    noise_parentheses = [
        r'[\(\[\{]\s*(official\s+audio|official\s+video|music\s+video|lyric\s+video|lyrics|hd|4k|topic|full\s+song)\s*[\)\]\}]'
    ]
    for np in noise_parentheses:
        text = re.sub(np, ' ', text, flags=re.IGNORECASE)

    text = re.sub(r'\b(featuring|feat|ft)\b', ' ', text)
    text = re.sub(r'\b(official\s+audio|official\s+video|official\s+music\s+video|music\s+video|lyric\s+video|lyrics|hd|4k|topic)\b', ' ', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    return " ".join(text.split())

def string_similarity(s1: str, s2: str) -> float:
    """Calculates composite string similarity score (0.0 to 1.0)."""
    norm1 = normalize_music_text(s1)
    norm2 = normalize_music_text(s2)
    if not norm1 or not norm2:
        return 0.0
    if norm1 == norm2:
        return 1.0

    tokens1 = set(norm1.split())
    tokens2 = set(norm2.split())
    if not tokens1 or not tokens2:
        return 0.0

    intersection = tokens1.intersection(tokens2)
    union = tokens1.union(tokens2)
    jaccard = len(intersection) / len(union) if union else 0.0

    containment_bonus = 0.2 if (norm1 in norm2 or norm2 in norm1) else 0.0
    char_ratio = SequenceMatcher(None, norm1, norm2).ratio()

    return min(1.0, max(jaccard, char_ratio) * 0.7 + containment_bonus + (jaccard * 0.1))

def score_music_candidate(
    candidate: Dict[str, Any],
    target_title: str,
    target_artist: str,
    target_duration: float
) -> float:
    """Evaluates candidate song track against target criteria with penalties."""
    c_title = candidate.get('title', '')
    c_uploader = candidate.get('uploader', '') or candidate.get('channel', '')
    c_duration = float(candidate.get('duration') or 0)

    title_score = string_similarity(c_title, target_title)

    artist_title_sim = string_similarity(c_title, target_artist) if target_artist else 0.0
    artist_uploader_sim = string_similarity(c_uploader, target_artist) if target_artist else 0.0
    artist_score = max(artist_title_sim, artist_uploader_sim)

    duration_score = 0.5
    if target_duration > 0 and c_duration > 0:
        diff = abs(c_duration - target_duration)
        if diff <= 5:
            duration_score = 1.0
        elif diff <= 15:
            duration_score = 0.85
        elif diff <= 30:
            duration_score = 0.6
        elif diff <= 60:
            duration_score = 0.3
        else:
            duration_score = 0.0
    elif c_duration > 0:
        if 60 <= c_duration <= 600:
            duration_score = 0.7
        else:
            duration_score = 0.1

    base_score = (title_score * 0.45) + (artist_score * 0.35) + (duration_score * 0.20)

    c_title_lower = c_title.lower()
    t_title_lower = target_title.lower()

    if "remix" in c_title_lower and "remix" not in t_title_lower:
        base_score -= 0.25
    if "live" in c_title_lower and "live" not in t_title_lower:
        base_score -= 0.25
    if "cover" in c_title_lower and "cover" not in t_title_lower:
        base_score -= 0.35
    if "karaoke" in c_title_lower or "instrumental" in c_title_lower:
        base_score -= 0.40
    if "sped up" in c_title_lower or "slowed" in c_title_lower or "nightcore" in c_title_lower:
        base_score -= 0.35

    c_uploader_lower = c_uploader.lower()
    if "topic" in c_uploader_lower or "vevo" in c_uploader_lower:
        base_score += 0.15
    if "official audio" in c_title_lower or "official video" in c_title_lower:
        base_score += 0.10

    return max(0.0, min(1.0, base_score))

def validate_music_candidate(
    candidate: Dict[str, Any],
    target_title: str,
    target_artist: str,
    target_duration: float,
    score: float
) -> Tuple[bool, str]:
    """
    HARD VALIDATION: Strictly checks title similarity, artist similarity, duration difference, and composite score.
    Score alone CANNOT bypass hard validation criteria.
    """
    if score < MIN_TOTAL_MATCH_SCORE:
        return False, f"Score {score:.2f} below minimum threshold {MIN_TOTAL_MATCH_SCORE}"

    c_title = candidate.get('title', '')
    c_uploader = candidate.get('uploader', '') or candidate.get('channel', '')
    c_duration = float(candidate.get('duration') or 0)

    title_sim = string_similarity(c_title, target_title)
    if title_sim < MIN_TITLE_SIMILARITY:
        return False, f"Title similarity {title_sim:.2f} below threshold {MIN_TITLE_SIMILARITY}"

    if target_artist:
        artist_title_sim = string_similarity(c_title, target_artist)
        artist_uploader_sim = string_similarity(c_uploader, target_artist)
        artist_sim = max(artist_title_sim, artist_uploader_sim)
        if artist_sim < MIN_ARTIST_SIMILARITY:
            return False, f"Artist similarity {artist_sim:.2f} below threshold {MIN_ARTIST_SIMILARITY}"

    if target_duration > 0 and c_duration > 0:
        dur_diff = abs(c_duration - target_duration)
        if dur_diff > MAX_DURATION_DIFF_SECONDS:
            return False, f"Duration difference {dur_diff:.1f}s exceeds max allowed {MAX_DURATION_DIFF_SECONDS}s"

    return True, "Valid Candidate"

def collect_youtube_candidates(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Collects YouTube & YouTube Music candidates without downloading audio files upfront."""
    candidates = []
    if not query:
        return candidates

    cookie_file = setup_cookies_file()
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
        'nocheckcertificate': True,
    }
    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

    targets = [f"ytmsearch{limit}:{query}", f"ytsearch{limit}:{query}"]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for tgt in targets:
            try:
                info = ydl.extract_info(tgt, download=False)
                if info and info.get('entries'):
                    for entry in info.get('entries', []):
                        if not entry:
                            continue
                        url = entry.get('webpage_url') or entry.get('url')
                        if not url:
                            vid = entry.get('id')
                            if vid:
                                url = f"https://www.youtube.com/watch?v={vid}"
                        if url:
                            candidates.append({
                                'title': entry.get('title') or "Audio Candidate",
                                'uploader': entry.get('uploader') or entry.get('channel') or "YouTube Artist",
                                'duration': float(entry.get('duration') or 0),
                                'webpage_url': url,
                                'id': entry.get('id', '')
                            })
            except Exception as ex:
                logger.debug(f"Candidate search failed for {tgt}: {ex}")

    seen_urls = set()
    unique_candidates = []
    for c in candidates:
        if c['webpage_url'] not in seen_urls:
            seen_urls.add(c['webpage_url'])
            unique_candidates.append(c)

    return unique_candidates

# ---------------------------------------------------------------------------
# SHAZAM AUDIO RECOGNITION
# ---------------------------------------------------------------------------

def generate_audio_chunks(input_path: str, job_prefix: str) -> List[Tuple[str, str]]:
    """Slices audio into chunk samples for mix detection."""
    if not os.path.exists(input_path):
        return []

    generated_files: List[Tuple[str, str]] = []
    try:
        cmd_duration = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", input_path
        ]
        res = subprocess.run(cmd_duration, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8)
        duration = float(res.stdout.strip()) if res.returncode == 0 and res.stdout.strip() else 30.0

        offsets = [0.0]
        if duration > 15: offsets.append(duration * 0.35)
        if duration > 35: offsets.append(duration * 0.70)

        for idx, start_t in enumerate(offsets):
            crop_dur = 12.0
            chunk_path = os.path.join(DOWNLOAD_DIR, f"{job_prefix}_slice_{idx}.wav")
            cmd = [
                "ffmpeg", "-y", "-ss", str(start_t), "-t", str(crop_dur),
                "-i", input_path, "-vn", "-ac", "1", "-ar", "44100", "-f", "wav", chunk_path
            ]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 1000:
                generated_files.append((chunk_path, f"Chunk {idx+1}"))
    except Exception as e:
        logger.debug(f"Audio chunk generation error: {e}")

    return generated_files

async def recognize_audio_shazam(filepath: str, job_prefix: str) -> Optional[Dict[str, Any]]:
    """Recognizes track metadata using ShazamIO or raw API fallback."""
    loop = asyncio.get_running_loop()
    chunk_items = await loop.run_in_executor(executor, lambda: generate_audio_chunks(filepath, job_prefix))
    if not chunk_items:
        chunk_items = [(filepath, "Full Audio")]

    detected_tracks: List[Dict[str, Any]] = []
    seen_track_keys = set()

    for chunk_path, label in chunk_items:
        try:
            track_info = None
            if HAS_SHAZAMIO:
                try:
                    shazam = Shazam()
                    out = await shazam.recognize(chunk_path)
                    track = out.get('track', {})
                    if track and track.get('title'):
                        track_info = {
                            'title': track.get('title', ''),
                            'artist': track.get('subtitle', ''),
                            'genre': track.get('genres', {}).get('primary', 'نامشخص'),
                        }
                except Exception as e:
                    logger.debug(f"ShazamIO error: {e}")

            if not track_info:
                def _raw_shazam_request(c_path):
                    try:
                        with open(c_path, 'rb') as f:
                            audio_data = f.read()
                        encoded_b64 = base64.b64encode(audio_data[:500000]).decode('utf-8')
                        req_url = "https://amp.shazam.com/discovery/v5/en/US/mweb/-/tag"
                        payload = json.dumps({"sample": encoded_b64}).encode('utf-8')
                        req = urllib.request.Request(req_url, data=payload, headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}, method="POST")
                        with urllib.request.urlopen(req, timeout=8) as resp:
                            if resp.status == 200:
                                data = json.loads(resp.read().decode('utf-8'))
                                track = data.get('track') or data.get('matches', [{}])[0]
                                if track and 'title' in track:
                                    return {
                                        'title': track.get('title'),
                                        'artist': track.get('subtitle', 'Unknown'),
                                        'genre': 'General'
                                    }
                    except Exception:
                        pass
                    return None

                track_info = await loop.run_in_executor(executor, lambda: _raw_shazam_request(chunk_path))

            if track_info and track_info.get('title'):
                key = f"{track_info['title'].lower()}_{track_info['artist'].lower()}"
                if key not in seen_track_keys:
                    seen_track_keys.add(key)
                    detected_tracks.append(track_info)

        finally:
            if chunk_path != filepath and os.path.exists(chunk_path):
                try:
                    os.remove(chunk_path)
                except Exception:
                    pass

    if detected_tracks:
        main_track = detected_tracks[0]
        return {
            'title': main_track['title'],
            'artist': main_track['artist'],
            'genre': main_track.get('genre', 'نامشخص'),
            'all_tracks': detected_tracks,
            'is_mix': len(detected_tracks) > 1
        }

    return None

# ---------------------------------------------------------------------------
# EXTRACTION & FORMAT SELECTION ENGINE (HARDENED)
# ---------------------------------------------------------------------------

def parse_valid_formats_for_item(formats: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    """Filters and deduplicates format resolutions for a slide."""
    if not formats:
        return []

    height_groups: Dict[int, List[Dict[str, Any]]] = {}

    for f in formats:
        vcodec = f.get('vcodec', 'none')
        if not vcodec or vcodec == 'none':
            continue

        height = extract_resolution_height(f)
        if height <= 0:
            continue

        if height not in height_groups:
            height_groups[height] = []
        height_groups[height].append(f)

    valid_formats = []

    for height, fmt_list in height_groups.items():
        def format_rank_key(fmt):
            ext = str(fmt.get('ext', '')).lower()
            vcodec = str(fmt.get('vcodec', '')).lower()
            acodec = str(fmt.get('acodec', '')).lower()

            is_mp4 = 1 if ext == 'mp4' else 0
            is_h264 = 1 if ('h264' in vcodec or 'avc' in vcodec) else 0
            has_audio = 1 if (acodec and acodec != 'none') else 0

            size = estimate_format_filesize(fmt, duration)
            has_size = 1 if size > 0 else 0
            tbr = float(fmt.get('tbr') or fmt.get('vbr') or 0)

            return (has_audio, is_mp4, is_h264, has_size, tbr, size)

        sorted_fmts = sorted(fmt_list, key=format_rank_key, reverse=True)
        best_fmt = sorted_fmts[0]

        acodec = best_fmt.get('acodec', 'none')
        vcodec = best_fmt.get('vcodec', 'none')
        has_audio = bool(acodec and acodec != 'none')
        has_video = bool(vcodec and vcodec != 'none')

        size_bytes = estimate_format_filesize(best_fmt, duration)

        fmt_meta = {
            'height': height,
            'format_id': str(best_fmt.get('format_id', '')),
            'ext': str(best_fmt.get('ext', 'mp4')),
            'vcodec': str(vcodec),
            'acodec': str(acodec),
            'filesize': best_fmt.get('filesize', 0),
            'filesize_approx': best_fmt.get('filesize_approx', 0),
            'tbr': best_fmt.get('tbr', 0),
            'has_audio': has_audio,
            'has_video': has_video,
            'label': format_quality_label(height),
            'size_str': format_size(size_bytes),
            'size_bytes': size_bytes
        }
        valid_formats.append(fmt_meta)

    valid_formats.sort(key=lambda x: x['height'], reverse=True)
    return valid_formats

def build_format_selectors(slide: Dict[str, Any], param: str) -> List[str]:
    """
    QUALITY SAFETY MANDATE:
    For numeric parameters (e.g. 720), format selectors MUST NOT contain unrestricted fallbacks or unconstrained height syntax.
    """
    if not param or param == "best":
        return [
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo[vcodec^=avc]+bestaudio[acodec^=mp4a]",
            "bestvideo+bestaudio",
            "best"
        ]

    try:
        requested_h = int(param)
    except (ValueError, TypeError):
        return [
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio",
            "best"
        ]

    # Strictly restricted to requested_h with NO unconstrained height fallbacks
    selectors = [
        f"bestvideo[height<={requested_h}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={requested_h}][vcodec^=avc]+bestaudio[acodec^=mp4a]",
        f"bestvideo[height<={requested_h}]+bestaudio",
        f"best[height<={requested_h}]"
    ]
    return selectors

def normalize_extraction_entries(info: Dict[str, Any], root_url: str) -> List[Dict[str, Any]]:
    """Normalizes yt-dlp single / playlist / carousel extraction result into independent slide dicts."""
    if not info:
        return []

    root_title = info.get('title') or "ویدیو / پست"
    root_desc = info.get('description') or root_title
    root_thumb = info.get('thumbnail') or ""
    root_duration = float(info.get('duration') or 0)
    root_webpage_url = info.get('webpage_url') or info.get('original_url') or info.get('url') or root_url
    root_track = info.get('track') or info.get('alt_title') or ""
    root_artist = info.get('artist') or info.get('creator') or info.get('uploader') or ""

    raw_entries = info.get('entries')

    entries_list = []
    if raw_entries:
        for entry in raw_entries:
            if not entry:
                continue
            if entry.get('entries'):
                for sub in entry.get('entries', []):
                    if sub:
                        entries_list.append(sub)
            else:
                entries_list.append(entry)

    if not entries_list:
        single_url = info.get('webpage_url') or info.get('original_url') or info.get('url') or root_url
        single_track = root_track
        single_artist = root_artist

        if not single_track:
            ex_t, ex_a = extract_music_info_from_caption(root_desc)
            single_track = ex_t
            if ex_a and not single_artist:
                single_artist = ex_a

        slide_formats = parse_valid_formats_for_item(info.get('formats', []), root_duration)

        return [{
            "index": 0,
            "url": single_url,
            "webpage_url": single_url,
            "title": root_title,
            "description": root_desc,
            "duration": root_duration,
            "thumbnail": root_thumb,
            "formats": slide_formats,
            "track_title": single_track,
            "artist_name": single_artist,
            "is_spotify": info.get('is_spotify', False),
            "raw_info": info
        }]

    slides = []
    for idx, entry in enumerate(entries_list):
        e_url = entry.get('webpage_url') or entry.get('original_url') or entry.get('url') or root_webpage_url
        e_title = entry.get('title') or root_title
        e_desc = entry.get('description') or entry.get('title') or root_desc
        e_thumb = entry.get('thumbnail') or root_thumb
        e_duration = float(entry.get('duration') or root_duration or 0)
        e_track = entry.get('track') or entry.get('alt_title') or root_track
        e_artist = entry.get('artist') or entry.get('creator') or entry.get('uploader') or root_artist

        if not e_track:
            ex_t, ex_a = extract_music_info_from_caption(e_desc)
            e_track = ex_t
            if ex_a and not e_artist:
                e_artist = ex_a

        raw_formats = entry.get('formats') or info.get('formats', [])
        slide_formats = parse_valid_formats_for_item(raw_formats, e_duration)

        slides.append({
            "index": idx,
            "url": e_url,
            "webpage_url": e_url,
            "title": e_title,
            "description": e_desc,
            "duration": e_duration,
            "thumbnail": e_thumb,
            "formats": slide_formats,
            "track_title": e_track,
            "artist_name": e_artist,
            "is_spotify": entry.get('is_spotify', False),
            "raw_info": entry
        })

    return slides

async def extract_media_info_robust(url: str) -> Optional[Dict[str, Any]]:
    """Extracts media metadata using yt-dlp."""
    clean_url = clean_media_url(url)

    if is_spotify_url(clean_url):
        loop = asyncio.get_running_loop()
        sp_info = await loop.run_in_executor(executor, lambda: extract_spotify_info_oembed(clean_url))
        if sp_info:
            return {
                "direct_url": None,
                "title": f"🎵 {sp_info['title']} - {sp_info['artist']}",
                "description": f"موزیک اسپاتیفای: {sp_info['title']} اثر {sp_info['artist']}",
                "track": sp_info['title'],
                "artist": sp_info['artist'],
                "thumbnail": sp_info['thumbnail'],
                "formats": [],
                "duration": 210,
                "is_spotify": True,
                "webpage_url": clean_url
            }

    cookie_file = setup_cookies_file()
    loop = asyncio.get_running_loop()

    strategies = [
        {
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            }
        },
        {
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Mobile/15E148 Safari/604.1',
            }
        }
    ]

    for strat in strategies:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'nocheckcertificate': True,
            'http_headers': strat.get('http_headers', {}),
        }

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

    return None

async def download_media_video(url: str, param: str, job_prefix: str, slide: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Downloads requested video quality with post-download FFprobe height validation & stale file protection.
    Rejects any video that exceeds requested max_height.
    """
    if not url:
        return None

    max_height = int(param) if (param and param.isdigit()) else None

    async with JOB_SEMAPHORE:
        cleanup_job_files(job_prefix)
        download_started_at = time.time()

        cookie_file = setup_cookies_file()
        output_template = os.path.join(DOWNLOAD_DIR, f"{job_prefix}.%(ext)s")

        slide_dict = slide or {}
        format_selectors = build_format_selectors(slide_dict, param)

        loop = asyncio.get_running_loop()

        for fmt in format_selectors:
            cleanup_job_files(job_prefix)

            ydl_opts = {
                'format': fmt,
                'outtmpl': output_template,
                'quiet': True,
                'no_warnings': True,
                'merge_output_format': 'mp4',
                'nocheckcertificate': True,
                'concurrent_fragment_downloads': 8,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                }
            }

            if cookie_file:
                ydl_opts['cookiefile'] = cookie_file

            def _download():
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.extract_info(url, download=True)
                except Exception as ex:
                    logger.debug(f"Video download strategy failed ({fmt}): {ex}")

            await loop.run_in_executor(executor, _download)

            found_file = find_downloaded_media(job_prefix, min_mtime=download_started_at)
            if found_file:
                if validate_downloaded_video_height(found_file, max_height):
                    return found_file
                else:
                    logger.warning(f"Rejecting output file {found_file} due to height validation failure (> {max_height}p).")
                    cleanup_job_files(job_prefix)

        return None

async def download_media_audio(url: str, bitrate: str, job_prefix: str) -> Optional[str]:
    """Extracts MP3 audio track from source slide URL with stale file protection and ffprobe validation."""
    if not url:
        return None

    async with JOB_SEMAPHORE:
        cleanup_job_files(job_prefix)
        download_started_at = time.time()

        cookie_file = setup_cookies_file()
        output_template = os.path.join(DOWNLOAD_DIR, f"{job_prefix}.%(ext)s")

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': output_template,
            'nocheckcertificate': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': bitrate if bitrate in ["128", "320"] else "128",
            }],
            'quiet': True,
            'no_warnings': True,
        }

        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file

        loop = asyncio.get_running_loop()
        try:
            def _download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(url, download=True)

            await loop.run_in_executor(executor, _download)
            found_audio = find_downloaded_audio(job_prefix, min_mtime=download_started_at)
            if found_audio and os.path.exists(found_audio):
                meta = await loop.run_in_executor(executor, lambda: sync_get_video_metadata(found_audio))
                if meta.get("duration", 0) > 0 and os.path.getsize(found_audio) > 0:
                    return found_audio
            return None
        except Exception as e:
            logger.error(f"Error downloading audio: {e}")
            return None

async def search_and_download_full_track(track_title: str, artist_name: str, caption: str, job_prefix: str) -> Optional[Dict[str, Any]]:
    """
    FULL TRACK VALIDATION PIPELINE:
    1. Multi-Engine Search (Spotify, iTunes, Deezer) -> Canonical Target
    2. Collect YouTube / YouTube Music Candidates
    3. Normalize & Score Candidates
    4. HARD VALIDATION (validate_music_candidate) BEFORE downloading
    5. Sort valid candidates descending by score
    6. Download best valid candidate
    7. FFprobe Verification
    8. Return result OR explicit failure (None). NO blind/random downloading.
    """
    async with JOB_SEMAPHORE:
        cleanup_job_files(job_prefix)
        download_started_at = time.time()

        clean_t = clean_music_query(track_title)
        clean_a = clean_music_query(artist_name)
        extracted_t, extracted_a = extract_music_info_from_caption(caption)

        search_seed = f"{clean_a} {clean_t}".strip() or f"{extracted_a} {extracted_t}".strip() or clean_music_query(caption[:80])
        if not search_seed or len(search_seed) < 2:
            return None

        loop = asyncio.get_running_loop()

        # Step 1: Multi-Engine Metadata Search -> Canonical Target
        multi_match = await loop.run_in_executor(executor, lambda: search_multi_engine_track(search_seed))

        if multi_match:
            target_t = multi_match.get('title') or clean_t or extracted_t
            target_a = multi_match.get('artist') or clean_a or extracted_a
            target_dur = float(multi_match.get('duration', 0))
            engine_label = multi_match.get('source', 'Multi-Engine')
        else:
            target_t = clean_t or extracted_t
            target_a = clean_a or extracted_a
            target_dur = 0.0
            engine_label = 'Direct Search'

        if not target_t or len(target_t) < 2:
            logger.info("Canonical target title invalid or too short. Aborting search.")
            return None

        # Step 2: Collect Candidates from YouTube / YouTube Music
        search_queries = [f"{target_a} {target_t}".strip(), f"{target_t} audio".strip(), search_seed]

        candidates = []
        for q in search_queries:
            if not q: continue
            q_candidates = await loop.run_in_executor(executor, lambda: collect_youtube_candidates(q, limit=6))
            candidates.extend(q_candidates)

        seen_urls = set()
        unique_candidates = []
        for c in candidates:
            if c['webpage_url'] not in seen_urls:
                seen_urls.add(c['webpage_url'])
                unique_candidates.append(c)

        if not unique_candidates:
            logger.info("No YT candidates collected.")
            return None

        # Step 3 & 4: Score & HARD VALIDATION BEFORE downloading
        valid_candidates = []
        for cand in unique_candidates:
            score = score_music_candidate(cand, target_t, target_a, target_dur)
            is_valid, val_reason = validate_music_candidate(cand, target_t, target_a, target_dur, score)
            if is_valid:
                valid_candidates.append((score, cand))
            else:
                logger.debug(f"Candidate rejected ({cand.get('title')}): {val_reason}")

        if not valid_candidates:
            logger.info("No candidates passed hard validation. Explicit failure returned.")
            return None

        # Step 5: Sort valid candidates by score descending
        valid_candidates.sort(key=lambda x: x[0], reverse=True)

        output_template = os.path.join(DOWNLOAD_DIR, f"{job_prefix}.%(ext)s")
        cookie_file = setup_cookies_file()

        # Step 6 & 7: Download and validate best candidate
        for score, cand in valid_candidates:
            target_url = cand['webpage_url']

            ydl_opts = {
                'format': 'bestaudio[ext=m4a]/bestaudio/best',
                'outtmpl': output_template,
                'nocheckcertificate': True,
                'ignoreerrors': True,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '320',
                }],
                'quiet': True,
                'no_warnings': True,
            }
            if cookie_file:
                ydl_opts['cookiefile'] = cookie_file

            def _dl_cand():
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([target_url])
                except Exception as ex:
                    logger.debug(f"Candidate download failed ({target_url}): {ex}")

            await loop.run_in_executor(executor, _dl_cand)

            found_audio = find_downloaded_audio(job_prefix, min_mtime=download_started_at)
            if found_audio and os.path.exists(found_audio):
                try:
                    meta = await loop.run_in_executor(executor, lambda: sync_get_video_metadata(found_audio))
                    audio_dur = meta.get("duration", 0)
                    if audio_dur > 0 and os.path.getsize(found_audio) > 0:
                        final_title = target_t or cand['title']
                        final_artist = target_a or cand['uploader']
                        return {
                            'filepath': found_audio,
                            'title': final_title,
                            'uploader': final_artist,
                            'duration': audio_dur,
                            'score': score,
                            'engine': f"{engine_label} (Match Score: {int(score * 100)}%)"
                        }
                except Exception as ex:
                    logger.debug(f"Audio ffprobe validation failed: {ex}")

            cleanup_job_files(job_prefix)

        return None

# ---------------------------------------------------------------------------
# TELEGRAM BOT HANDLERS & CALLBACKS
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends start greeting message."""
    welcome_text = (
        "<b>درود به روی ماهت 🧘🏾🌚</b>\n\n"
        "من ربات دانلودر و موتور جستجوی هوشمند موسیقی هستم 🧸\n\n"
        "با من می‌تونی ویدئوها، موزیک‌ها و پست‌های <b>اینستاگرام، یوتیوب، تیک‌تاک و اسپاتیفای</b> رو بدون محدودیت دانلود کنی ⚡️\n\n"
        "کافیه فقط لینک پست، آهنگ یا یک فایل صوتی/ویدیو برام بفرستی!"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML)

def build_media_keyboard(cache_id: str, slide_idx: int, total_slides: int, slide: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard menu based on slide properties."""
    keyboard = []
    valid_formats = slide.get("formats", [])

    for fmt in valid_formats:
        btn_text = f"{fmt['label']} ({fmt['size_str']})"
        callback_data = f"vid:{cache_id}:{slide_idx}:{fmt['height']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

    if not valid_formats and not slide.get("is_spotify"):
        keyboard.append([
            InlineKeyboardButton("📹 دانلود ویدیو (بهترین کیفیت)", callback_data=f"vid:{cache_id}:{slide_idx}:best")
        ])

    if total_slides > 1:
        nav_row = []
        if slide_idx > 0:
            nav_row.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"slide:{cache_id}:{slide_idx - 1}"))
        nav_row.append(InlineKeyboardButton(f"اسلاید {slide_idx + 1} از {total_slides}", callback_data="noop"))
        if slide_idx < total_slides - 1:
            nav_row.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"slide:{cache_id}:{slide_idx + 1}"))
        keyboard.append(nav_row)

    if not slide.get("is_spotify"):
        keyboard.append([
            InlineKeyboardButton("🎵 ویس 128kbps", callback_data=f"aud:{cache_id}:{slide_idx}:128"),
            InlineKeyboardButton("🎵 ویس 320kbps", callback_data=f"aud:{cache_id}:{slide_idx}:320"),
        ])

    keyboard.append([
        InlineKeyboardButton("🎧 دانلود آهنگ اصلی (Multi-Engine)", callback_data=f"fullm:{cache_id}:{slide_idx}:hq")
    ])

    if not slide.get("is_spotify"):
        keyboard.append([
            InlineKeyboardButton("🔍 تشخیص موزیک با شزام (Shazam Pro)", callback_data=f"shazam:{cache_id}:{slide_idx}")
        ])

    extra_row = []
    if slide.get("thumbnail"):
        extra_row.append(InlineKeyboardButton("🖼 کاور اصلی", callback_data=f"img:{cache_id}:{slide_idx}"))
    if slide.get("description"):
        extra_row.append(InlineKeyboardButton("📜 کپشن کامل", callback_data=f"cap:{cache_id}:{slide_idx}"))

    if extra_row:
        keyboard.append(extra_row)

    return InlineKeyboardMarkup(keyboard)

async def handle_media_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming text and extracts media URLs."""
    text = update.message.text.strip() if update.message.text else ""
    urls = extract_urls_from_text(text)

    valid_url = None
    for u in urls:
        if is_supported_url(u):
            valid_url = u
            break

    if not valid_url:
        return

    cleanup_job_files("temp_")
    clean_expired_cache()

    status_msg = await update.message.reply_text("🔎 در حال آنالیز لینک و بررسی کیفیت‌های موجود...")

    clean_url = clean_media_url(valid_url)

    try:
        info = await extract_media_info_robust(clean_url)
    except Exception as e:
        logger.error(f"Error extracting info: {e}")
        info = None

    if not info:
        await status_msg.edit_text("❌ خطا در استخراج اطلاعات لینک. لطفاً از صحت لینک اطمینان حاصل کنید.")
        return

    slides = normalize_extraction_entries(info, clean_url)
    if not slides:
        await status_msg.edit_text("❌ هیچ محتوای قابل دانلودی در این لینک یافت نشد.")
        return

    cache_id = str(uuid.uuid4())[:8]
    total_slides = len(slides)
    current_slide = 0

    MEDIA_CACHE[cache_id] = {
        "created_at": time.time(),
        "source_url": clean_url,
        "current_slide": current_slide,
        "total_slides": total_slides,
        "slides": slides,
        "root_info": info
    }

    slide_0 = slides[0]
    reply_markup = build_media_keyboard(cache_id, current_slide, total_slides, slide_0)

    raw_caption = slide_0.get("description") or slide_0.get("title") or "ویدیو / پست"
    safe_cap = html_escape(raw_caption[:300])
    short_cap = safe_cap + "..." if len(raw_caption) > 300 else safe_cap
    caption_text = f"<b>📝 کپشن:</b>\n{short_cap}\n\n<b>گزینه مورد نظر را انتخاب کنید:</b>"

    thumbnail = slide_0.get("thumbnail")
    sent = False
    if thumbnail:
        try:
            await update.message.reply_photo(
                photo=thumbnail,
                caption=caption_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            sent = True
        except Exception as ex:
            logger.warning(f"Failed sending thumbnail photo: {ex}")

    if not sent:
        try:
            await update.message.reply_text(
                text=caption_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        except Exception as ex:
            logger.error(f"Failed sending menu text: {ex}")

    try:
        await status_msg.delete()
    except Exception:
        pass

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline callbacks with hardened validation & Spotify restrictions."""
    query = update.callback_query
    await query.answer()

    if not query.data:
        return

    data_parts = query.data.split(":")
    action_type = data_parts[0]

    if action_type == "noop":
        return

    # Direct Video Options Callback (vopt)
    if action_type == "vopt":
        if len(data_parts) < 3:
            return
        sub_action = data_parts[1]
        v_cache_id = data_parts[2]
        cached_vid = MEDIA_CACHE.get(v_cache_id)

        if not cached_vid:
            await query.message.reply_text("❌ این درخواست منقضی شده است؛ لطفاً فایل را دوباره ارسال کنید.")
            return

        file_id = cached_vid.get("file_id")
        job_prefix = f"job_vopt_{uuid.uuid4().hex[:6]}"
        temp_v = os.path.join(DOWNLOAD_DIR, f"{job_prefix}.mp4")
        temp_a = os.path.join(DOWNLOAD_DIR, f"{job_prefix}.mp3")

        status_msg = await query.message.reply_text("⏳ در حال پردازش ویدیو...")

        try:
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(temp_v)

            if sub_action == "extract_audio":
                def _ext_audio():
                    cmd = ["ffmpeg", "-y", "-i", temp_v, "-vn", "-acodec", "libmp3lame", "-b:a", "320k", temp_a]
                    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                    return res.returncode == 0 and os.path.exists(temp_a) and os.path.getsize(temp_a) > 0

                loop = asyncio.get_running_loop()
                success = await loop.run_in_executor(executor, _ext_audio)

                found_audio = find_downloaded_audio(job_prefix) if success else None
                if found_audio:
                    with open(found_audio, 'rb') as f:
                        await query.message.reply_audio(audio=f, caption="🎵 فایل صوتی استخراج شده (320kbps)")
                    await status_msg.delete()
                else:
                    await status_msg.edit_text("❌ خطا در استخراج صوت از ویدیو.")

            elif sub_action in ["shazam", "full_music"]:
                def _prep_wav():
                    cmd = ["ffmpeg", "-y", "-i", temp_v, "-vn", "-ac", "1", "-ar", "44100", temp_a]
                    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                    return res.returncode == 0 and os.path.exists(temp_a) and os.path.getsize(temp_a) > 0

                loop = asyncio.get_running_loop()
                success = await loop.run_in_executor(executor, _prep_wav)

                if not success:
                    await status_msg.edit_text("❌ خطا در آماده‌سازی فایل صوتی از ویدیو.")
                    return

                shz_res = await recognize_audio_shazam(temp_a, job_prefix)
                if shz_res:
                    s_t, s_a = shz_res['title'], shz_res['artist']
                    await status_msg.edit_text(f"🎯 <b>موزیک شناسایی شد:</b> {html_escape(s_a)} - {html_escape(s_t)}\n🟢 در حال جستجو و دانلود نسخه مطمئن...", parse_mode=ParseMode.HTML)
                    full_res = await search_and_download_full_track(s_t, s_a, f"{s_t} {s_a}", f"full_{job_prefix}")
                    if full_res and os.path.exists(full_res['filepath']):
                        with open(full_res['filepath'], 'rb') as f:
                            await query.message.reply_audio(audio=f, title=full_res['title'], performer=full_res['uploader'], caption=f"🎧 <b>{html_escape(full_res['uploader'])} - {html_escape(full_res['title'])}</b>\n⚡️ {html_escape(full_res.get('engine','Engine'))}", parse_mode=ParseMode.HTML)
                        await status_msg.delete()
                    else:
                        await status_msg.edit_text("❌ نسخه مطمئن و موثقی از آهنگ پیدا نشد.")
                else:
                    await status_msg.edit_text("❌ موزیک توسط شزام تشخیص داده نشد.")
        except Exception as e:
            logger.error(f"vopt processing error: {e}")
            await status_msg.edit_text("❌ خطا در پردازش درخواست.")
        finally:
            cleanup_job_files(job_prefix)
        return

    # Standard Media Callbacks validation
    if len(data_parts) < 3:
        return

    cache_id = data_parts[1]
    if not data_parts[2].isdigit():
        return
    slide_idx = int(data_parts[2])
    param = data_parts[3] if len(data_parts) > 3 else ""

    cached_entry = MEDIA_CACHE.get(cache_id)
    if not cached_entry:
        await query.message.reply_text("❌ این درخواست منقضی شده است؛ لطفاً لینک را دوباره ارسال کنید.")
        return

    slide = get_cached_slide(cache_id, slide_idx)
    if not slide:
        await query.message.reply_text("❌ اسلاید مورد نظر یافت نشد.")
        return

    # Spotify callback restriction
    if slide.get("is_spotify"):
        if action_type in ["vid", "aud", "shazam"]:
            await query.message.reply_text("❌ امکان دانلود مستقیم فایل ویدیو/صوت برای لینک‌های اسپاتیفای وجود ندارد.\nلطفاً از گزینه «دانلود آهنگ اصلی» استفاده کنید.")
            return

    # Quality callback validation for "vid"
    if action_type == "vid":
        slide_formats = slide.get("formats", [])
        if param != "best" and param.isdigit():
            req_h = int(param)
            valid_hs = [f['height'] for f in slide_formats if isinstance(f.get('height'), int) and f['height'] > 0]
            if slide_formats and req_h not in valid_hs:
                await query.message.reply_text("❌ کیفیت انتخاب‌شده معتبر نیست یا در این اسلاید وجود ندارد.")
                return

    cached_entry["current_slide"] = slide_idx
    url = slide.get("url") or slide.get("webpage_url") or cached_entry.get("source_url")
    job_prefix = f"job_{uuid.uuid4().hex[:6]}"

    if action_type == "slide":
        reply_markup = build_media_keyboard(cache_id, slide_idx, cached_entry.get("total_slides", 1), slide)
        try:
            await query.edit_message_reply_markup(reply_markup=reply_markup)
        except Exception:
            pass

    elif action_type == "vid":
        status_msg = await query.message.reply_text("⏳ در حال دانلود و بهینه‌سازی ویدیو...")
        try:
            raw_path = await download_media_video(url, param, job_prefix, slide=slide)
            if raw_path and os.path.exists(raw_path):
                prep_res = await prepare_telegram_video(raw_path, job_prefix)
                filepath = prep_res.get("filepath")
                thumb_path = prep_res.get("thumb_path")

                if not filepath or not os.path.exists(filepath):
                    await status_msg.edit_text("❌ خطا در پردازش یا تبدیل فرمت ویدیو.")
                    return

                if prep_res.get("filesize", 0) > 49.5 * 1024 * 1024:
                    await status_msg.edit_text("❌ حجم ویدیو بیشتر از حد مجاز تلگرام (۵۰ مگابایت) است.")
                    return

                await status_msg.edit_text("⬆️ در حال آپلود ویدیو...")
                kwargs = {"supports_streaming": True, "caption": "✨ دانلود شده توسط ربات"}
                if prep_res.get("width"): kwargs["width"] = prep_res["width"]
                if prep_res.get("height"): kwargs["height"] = prep_res["height"]
                if prep_res.get("duration"): kwargs["duration"] = prep_res["duration"]

                if thumb_path and os.path.exists(thumb_path):
                    with open(thumb_path, 'rb') as th, open(filepath, 'rb') as vf:
                        kwargs["thumbnail"] = th
                        await query.message.reply_video(video=vf, **kwargs)
                else:
                    with open(filepath, 'rb') as vf:
                        await query.message.reply_video(video=vf, **kwargs)

                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ کیفیت درخواستی موجود نیست یا امکان دانلود امن آن وجود ندارد.")
        except Exception as e:
            logger.error(f"Error uploading video: {e}")
            await status_msg.edit_text("❌ خطا در دانلود یا ارسال ویدیو.")
        finally:
            cleanup_job_files(job_prefix)

    elif action_type == "aud":
        status_msg = await query.message.reply_text(f"⏳ در حال استخراج ویس ({param}kbps)...")
        try:
            filepath = await download_media_audio(url, param, job_prefix)
            if filepath and os.path.exists(filepath):
                with open(filepath, 'rb') as af:
                    await query.message.reply_audio(audio=af, caption=f"🎵 ویس استخراج شده {param}kbps")
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ خطا در استخراج یا تبدیل فایل صوتی.")
        finally:
            cleanup_job_files(job_prefix)

    elif action_type == "shazam":
        status_msg = await query.message.reply_text("🔍 🎧 در حال اسکن صوت با شزام...")
        try:
            filepath = await download_media_audio(url, "128", job_prefix)
            if filepath and os.path.exists(filepath):
                shz_res = await recognize_audio_shazam(filepath, job_prefix)
                if shz_res:
                    s_t, s_a = shz_res['title'], shz_res['artist']
                    await status_msg.edit_text(f"🎯 <b>موزیک شناسایی شد:</b> {html_escape(s_a)} - {html_escape(s_t)}\n🟢 در حال آنالیز و دانلود نسخه مطمئن...", parse_mode=ParseMode.HTML)
                    full_res = await search_and_download_full_track(s_t, s_a, f"{s_t} {s_a}", f"full_{job_prefix}")
                    if full_res and os.path.exists(full_res['filepath']):
                        with open(full_res['filepath'], 'rb') as af:
                            await query.message.reply_audio(audio=af, title=full_res['title'], performer=full_res['uploader'], caption=f"🎧 <b>{html_escape(full_res['uploader'])} - {html_escape(full_res['title'])}</b>\n⚡️ {html_escape(full_res.get('engine','Engine'))}", parse_mode=ParseMode.HTML)
                        await status_msg.delete()
                    else:
                        await status_msg.edit_text("❌ نسخه مطمئن و موثقی از آهنگ پیدا نشد.")
                else:
                    await status_msg.edit_text("❌ اثری از این موزیک در دیتابیس شزام پیدا نشد.")
            else:
                await status_msg.edit_text("❌ خطا در آماده‌سازی فایل برای شزام.")
        finally:
            cleanup_job_files(job_prefix)

    elif action_type == "fullm":
        status_msg = await query.message.reply_text("🟢 🔎 در حال آنالیز و جستجوی چندگانه موزیک اصلی...")
        try:
            track_title = slide.get("track_title", "")
            artist_name = slide.get("artist_name", "")
            caption = slide.get("description", "")

            res = await search_and_download_full_track(track_title, artist_name, caption, job_prefix)
            if res and os.path.exists(res['filepath']):
                eng = html_escape(res.get('engine', 'Multi-Engine'))
                caption_audio = f"🎧 <b>{html_escape(res['uploader'])} - {html_escape(res['title'])}</b>\n⚡️ موتور: {eng}"
                with open(res['filepath'], 'rb') as af:
                    await query.message.reply_audio(audio=af, title=res['title'], performer=res['uploader'], caption=caption_audio, parse_mode=ParseMode.HTML)
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ نسخه مطمئن و موثقی از آهنگ مطابق مشخصات یافت نشد.")
        finally:
            cleanup_job_files(job_prefix)

    elif action_type == "img":
        thumb = slide.get("thumbnail")
        if thumb:
            await query.message.reply_photo(photo=thumb, caption="🖼 کاور اصلی")
        else:
            await query.message.reply_text("❌ کاور برای این اسلاید موجود نیست.")

    elif action_type == "cap":
        full_cap = slide.get("description") or slide.get("title") or "بدون کپشن"
        await query.message.reply_text(f"📜 <b>کپشن کامل:</b>\n\n{html_escape(full_cap)}", parse_mode=ParseMode.HTML)

async def handle_direct_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles directly sent voice or audio messages for Shazam identification & candidate search."""
    msg = update.message
    file_obj = msg.audio or msg.voice
    if not file_obj:
        return

    job_prefix = f"job_dir_aud_{uuid.uuid4().hex[:6]}"
    status_msg = await msg.reply_text("🔍 🎧 در حال دریافت و آنالیز ویس با شزام...")
    temp_path = os.path.join(DOWNLOAD_DIR, f"{job_prefix}.mp3")

    try:
        tg_file = await file_obj.get_file()
        await tg_file.download_to_drive(temp_path)

        shz_res = await recognize_audio_shazam(temp_path, job_prefix)
        if shz_res:
            s_t, s_a = shz_res['title'], shz_res['artist']
            await status_msg.edit_text(f"🎯 <b>شناسایی شد:</b> {html_escape(s_a)} - {html_escape(s_t)}\n🟢 در حال آنالیز و دانلود نسخه مطمئن...", parse_mode=ParseMode.HTML)
            full_res = await search_and_download_full_track(s_t, s_a, f"{s_t} {s_a}", f"full_{job_prefix}")
            if full_res and os.path.exists(full_res['filepath']):
                with open(full_res['filepath'], 'rb') as af:
                    await msg.reply_audio(audio=af, title=full_res['title'], performer=full_res['uploader'], caption=f"🎧 <b>{html_escape(full_res['uploader'])} - {html_escape(full_res['title'])}</b>\n⚡️ {html_escape(full_res.get('engine','Engine'))}", parse_mode=ParseMode.HTML)
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ نسخه مطمئن و موثقی از آهنگ پیدا نشد.")
        else:
            await status_msg.edit_text("❌ متأسفانه این آهنگ در شزام پیدا نشد.")
    except Exception as ex:
        logger.error(f"Direct audio error: {ex}")
        await status_msg.edit_text("❌ خطا در پردازش فایل صوتی.")
    finally:
        cleanup_job_files(job_prefix)

async def handle_direct_video_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles directly sent video messages and provides video utilities."""
    msg = update.message
    video_obj = msg.video or msg.video_note or msg.document
    if not video_obj:
        return

    cache_id = f"vfile_{uuid.uuid4().hex[:8]}"
    MEDIA_CACHE[cache_id] = {
        "file_id": video_obj.file_id,
        "timestamp": time.time()
    }

    keyboard = [
        [
            InlineKeyboardButton("🎵 استخراج صوتی MP3 320", callback_data=f"vopt:extract_audio:{cache_id}"),
            InlineKeyboardButton("🔍 تشخیص با شزام", callback_data=f"vopt:shazam:{cache_id}")
        ],
        [
            InlineKeyboardButton("🎧 دانلود کامل آهنگ 320", callback_data=f"vopt:full_music:{cache_id}")
        ]
    ]

    await msg.reply_text(
        "🎬 <b>ویدیو دریافت شد!</b>\nیکی از گزینه‌های زیر را انتخاب کنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def post_init(application: Application):
    """Cleans up active webhooks and drops old updates on startup."""
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Cleared webhook and pending updates.")
    except Exception as ex:
        logger.warning(f"Error dropping pending updates: {ex}")

# ---------------------------------------------------------------------------
# MAIN EXECUTION ENTRYPOINT
# ---------------------------------------------------------------------------

def main():
    """Starts Telegram Bot application."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is required.")
        sys.exit(1)

    check_ffmpeg_installed()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(180)
        .write_timeout(180)
        .connect_timeout(60)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_media_link))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_direct_audio_message))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE | filters.Document.VIDEO, handle_direct_video_message))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    logger.info("Bot started polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

# FINAL HARDENING COMPLETE
