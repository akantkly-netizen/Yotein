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

# Optional ShazamIO Integration
try:
    from shazamio import Shazam, Serialize
    HAS_SHAZAMIO = True
except ImportError:
    HAS_SHAZAMIO = False

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

_spotify_access_token: Optional[str] = None
_spotify_token_expires_at: float = 0.0


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


def sanitize_telegram_text(text: str) -> str:
    """Sanitizes text to avoid HTML/Markdown parsing breakage in Telegram."""
    if not text:
        return ""
    return text.replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")


def extract_resolution_height(format_dict: Dict[str, Any]) -> int:
    """Safely extracts resolution height from format dictionary."""
    h = format_dict.get('height')
    if isinstance(h, int):
        return h
    res_str = str(format_dict.get('resolution', ''))
    match = re.search(r'(\d{3,4})p?', res_str)
    return int(match.group(1)) if match else 0


def format_quality_label(height: int) -> str:
    """Generates user-friendly resolution label."""
    if height >= 2160: return "🎬 4K (2160p)"
    if height >= 1440: return "🎬 2K (1440p)"
    if height >= 1080: return "📹 Full HD (1080p)"
    if height >= 720:  return "📹 HD (720p)"
    if height >= 480:  return "📱 SD (480p)"
    if height >= 360:  return "📱 Low (360p)"
    return f"📱 {height}p"


def format_size(bytes_val: float) -> str:
    """Formats raw byte sizes into human-readable strings."""
    if not bytes_val or bytes_val <= 0:
        return "~MB"
    mb = bytes_val / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.1f} MB"


def estimate_format_filesize(format_dict: Dict[str, Any], duration: float) -> float:
    """Estimates audio/video format size in bytes."""
    filesize = format_dict.get('filesize') or format_dict.get('filesize_approx')
    if filesize:
        return float(filesize)
    tbr = format_dict.get('tbr') or format_dict.get('vbr', 0) + format_dict.get('abr', 0)
    if tbr and duration > 0:
        return (tbr * 1024 / 8) * duration
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
        r'اصلی', r'کامل', r'استوری', r'اکسپلور', r'اینستاگرام'
    ]
    for p in noise_patterns:
        t = re.sub(p, '', t, flags=re.IGNORECASE)
    t = re.sub(r'[^\w\s\-\.]', ' ', t)
    return " ".join(t.split())


def extract_music_info_from_caption(caption: str) -> Tuple[str, str]:
    """Uses regex patterns to extract song title and artist from captions."""
    if not caption:
        return "", ""
    
    # Check for Mashup patterns (e.g. Song A x Song B)
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


def generate_audio_chunks(input_path: str) -> List[Tuple[str, str]]:
    """Generates multiple sliced and speed-normalized WAV chunks for multi-track & mix detection."""
    if not os.path.exists(input_path):
        return []

    generated_files: List[Tuple[str, str]] = [] # List of (filepath, description)
    try:
        cmd_duration = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", input_path
        ]
        res = subprocess.run(cmd_duration, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        duration = float(res.stdout.strip()) if res.returncode == 0 and res.stdout.strip() else 30.0

        # Define key time offsets to scan for DJ Mixes / Reels (Beginning, Middle 1, Middle 2, End)
        offsets = [0.0]
        if duration > 15:
            offsets.append(duration * 0.3)
        if duration > 30:
            offsets.append(duration * 0.6)
        if duration > 45:
            offsets.append(duration * 0.8)

        for idx, start_t in enumerate(offsets):
            crop_dur = 12.0
            # Standard Slice
            chunk_path = os.path.join(DOWNLOAD_DIR, f"slice_{idx}_{uuid.uuid4().hex[:4]}.wav")
            cmd = [
                "ffmpeg", "-y", "-ss", str(start_t), "-t", str(crop_dur),
                "-i", input_path, "-vn", "-ac", "1", "-ar", "44100", "-f", "wav", chunk_path
            ]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 1000:
                generated_files.append((chunk_path, f"Standard Chunk {idx+1}"))

            # Speed/Pitch Adjusted Slices for Slowed/Sped-up Tracks
            if idx == 0 or idx == 1:
                # 1. Sped Up / Nightcore Shift (1.2x speed)
                sped_path = os.path.join(DOWNLOAD_DIR, f"sped_{idx}_{uuid.uuid4().hex[:4]}.wav")
                cmd_sped = [
                    "ffmpeg", "-y", "-ss", str(start_t), "-t", str(crop_dur),
                    "-i", input_path, "-filter:a", "atempo=0.833,asetrate=44100*1.2", "-vn", "-ac", "1", "-ar", "44100", "-f", "wav", sped_path
                ]
                subprocess.run(cmd_sped, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
                if os.path.exists(sped_path) and os.path.getsize(sped_path) > 1000:
                    generated_files.append((sped_path, f"Sped Up Normalization {idx+1}"))

                # 2. Slowed + Reverb Shift (0.85x speed correction)
                slow_path = os.path.join(DOWNLOAD_DIR, f"slow_{idx}_{uuid.uuid4().hex[:4]}.wav")
                cmd_slow = [
                    "ffmpeg", "-y", "-ss", str(start_t), "-t", str(crop_dur),
                    "-i", input_path, "-filter:a", "atempo=1.176,asetrate=44100*0.85", "-vn", "-ac", "1", "-ar", "44100", "-f", "wav", slow_path
                ]
                subprocess.run(cmd_slow, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
                if os.path.exists(slow_path) and os.path.getsize(slow_path) > 1000:
                    generated_files.append((slow_path, f"Slowed Normalization {idx+1}"))

    except Exception as e:
        logger.debug(f"Audio chunk generation error: {e}")

    return generated_files


async def recognize_audio_shazam(filepath: str) -> Optional[Dict[str, Any]]:
    """Multi-segment Shazam scanner capable of detecting multiple tracks in DJ mixes/mashups."""
    chunk_items = generate_audio_chunks(filepath)
    if not chunk_items:
        chunk_items = [(filepath, "Full Audio")]

    detected_tracks: List[Dict[str, Any]] = []
    seen_track_keys = set()

    loop = asyncio.get_event_loop()

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
                    logger.debug(f"ShazamIO error for {label}: {e}")

            if not track_info:
                # Fallback Direct REST Request
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
        # Primary Track
        main_track = detected_tracks[0]
        return {
            'title': main_track['title'],
            'artist': main_track['artist'],
            'genre': main_track.get('genre', 'نامشخص'),
            'all_tracks': detected_tracks, # Holds all detected songs in DJ mix/mashup
            'is_mix': len(detected_tracks) > 1
        }

    return None


async def extract_media_info_robust(url: str) -> Optional[Dict[str, Any]]:
    """Multi-tiered Extraction Pipeline ensuring maximum success without errors."""
    clean_url = clean_media_url(url)

    # Tier 0: Direct Spotify Track URL Handler
    if is_spotify_url(clean_url):
        logger.info("Extracting Spotify metadata via oEmbed...")
        sp_info = extract_spotify_info_oembed(clean_url)
        if sp_info:
            return {
                "direct_url": clean_url,
                "title": f"🎵 {sp_info['title']} - {sp_info['artist']}",
                "description": f"موزیک اسپاتیفای: {sp_info['title']} اثر {sp_info['artist']}",
                "track": sp_info['title'],
                "artist": sp_info['artist'],
                "thumbnail": sp_info['thumbnail'],
                "formats": [],
                "duration": 210,
                "is_spotify": True
            }

    cookie_file = setup_cookies_file()
    loop = asyncio.get_event_loop()

    # Tier 1: yt-dlp Extraction with Rotating Player Clients & Mobile User-Agents
    strategies = [
        {
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            'extractor_args': {
                'youtube': {'player_client': ['android', 'ios', 'mweb', 'web']},
            }
        },
        {
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
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

    # Tier 2: DDInstagram Proxy Scraper (For Instagram URLs)
    if is_instagram_url(clean_url):
        logger.info("Attempting DDInstagram Proxy Scraper...")
        dd_res = await loop.run_in_executor(executor, lambda: extract_instagram_dd_info(clean_url))
        if dd_res:
            return dd_res

        # Tier 3: Instagram Embed Scraper
        logger.info("Attempting Instagram Embed Scraper...")
        embed_res = await loop.run_in_executor(executor, lambda: extract_instagram_embed_info(clean_url))
        if embed_res:
            return embed_res

    # Tier 4: Cobalt Multi-Node Extractor Fallback
    logger.info("Attempting Cobalt API Node fallback...")
    cobalt_res = await fetch_cobalt_fallback_info(clean_url)
    if cobalt_res:
        return cobalt_res

    # Tier 5: Direct Stream Fallback Guarantee
    if is_instagram_url(clean_url) or is_youtube_url(clean_url):
        return {
            "direct_url": clean_url,
            "title": "رسانه استخراج شده",
            "description": "استخراج مستقیم",
            "formats": [{"url": clean_url, "ext": "mp4", "height": 720, "vcodec": "h264"}],
            "duration": 60,
            "thumbnail": None
        }

    return None


async def download_media_video(url: str, param: str, output_prefix: str, direct_url: Optional[str] = None) -> Optional[str]:
    """Downloads requested video quality using multi-stage format specs or direct stream."""
    cookie_file = setup_cookies_file()
    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")

    if direct_url and direct_url.startswith("http"):
        target_path = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.mp4")
        try:
            req = urllib.request.Request(direct_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
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
                'User-Agent': 'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36',
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
    """Ultra-resilient YouTube Music & YouTube search & download pipeline."""
    queries_to_try = []

    # Direct YouTube / YouTube Music link check
    if is_youtube_url(caption or track_title):
        direct_yt_url = clean_media_url(caption or track_title)
        queries_to_try.append(direct_yt_url)

    clean_t = clean_music_query(track_title)
    clean_a = clean_music_query(artist_name)
    extracted_title, extracted_artist = extract_music_info_from_caption(caption)

    spotify_match = None
    search_seed = f"{clean_a} {clean_t}".strip() or f"{extracted_artist} {extracted_title}".strip() or clean_music_query(caption[:100])

    if search_seed and len(search_seed) >= 3:
        spotify_match = search_spotify_track(search_seed)

    if spotify_match:
        sp_title = spotify_match['title']
        sp_artist = spotify_match['artist']
        queries_to_try.append(f"{sp_artist} {sp_title} audio")
        queries_to_try.append(f"{sp_artist} {sp_title} official music video")

    if clean_a and clean_t:
        queries_to_try.append(f"{clean_a} {clean_t} audio")
        queries_to_try.append(f"{clean_a} {clean_t}")
        queries_to_try.append(f"{clean_a} {clean_t} remix phonk")
    if clean_t:
        queries_to_try.append(f"{clean_t} official song")
        queries_to_try.append(f"{clean_t}")

    if extracted_title:
        q_ext = clean_music_query(f"{extracted_artist} {extracted_title}")
        if q_ext and q_ext not in queries_to_try:
            queries_to_try.append(f"{q_ext} audio")

    if not queries_to_try and caption:
        clean_cap = clean_music_query(caption[:120])
        if clean_cap:
            queries_to_try.append(f"{clean_cap} song")

    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")
    cookie_file = setup_cookies_file()

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': output_template,
        'concurrent_fragment_downloads': 16,
        'nocheckcertificate': True,
        'ignoreerrors': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {
            'youtube': {'player_client': ['android', 'ios', 'mweb']},
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36',
        }
    }

    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

    loop = asyncio.get_event_loop()

    for search_q in queries_to_try:
        try:
            def _search():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    target = search_q if search_q.startswith("http") else f"ytsearch5:{search_q}"
                    info = ydl.extract_info(target, download=False)
                    if not info:
                        return None

                    entries = info.get('entries', [info]) if 'entries' in info else [info]
                    entries = [e for e in entries if e]

                    if not entries:
                        return None

                    for entry in entries:
                        duration = entry.get('duration') or 180
                        if 20 <= duration <= 1200:
                            video_url = entry.get('webpage_url') or entry.get('url') or search_q
                            if video_url:
                                ydl.download([video_url])

                                final_title = (spotify_match.get('title') if spotify_match else None) or entry.get('title') or track_title or "Original Song"
                                final_artist = (spotify_match.get('artist') if spotify_match else None) or entry.get('uploader') or entry.get('artist') or artist_name or "Unknown Artist"

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
            logger.debug(f"Search query failed for '{search_q}': {ex}")
            continue

    return None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends custom greeting message on /start command."""
    welcome_text = (
        "درود به روی ماهت 🧘🏾🌚\n"
        "من ربات دانلودر و تشخیص هوشمند موزیک هستم 🧸\n\n"
        "با من می‌تونی ویدئوها، موزیک‌ها و پست‌های هر پلتفرمی رو بدون محدودیت دانلود کنی 🧘🏾✨️\n\n"
        "همچنین ابزار اختصاصی **تشخیص هوشمند بیت و موزیک (Shazam)** برام فعال شده تا دقیق‌ترین رمیکس‌ها، فونک‌ها (Phonk) و موزیک‌های اسلو رو برات پیدا کنم! 🎧🔥\n\n"
        "کافیه فقط لینک پست یا حتی یک وویس/ویدیو برام بفرستی 🧸"
    )
    await update.message.reply_text(welcome_text)


async def handle_media_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming Instagram and YouTube links safely without crashing."""
    url = update.message.text.strip()

    if not is_supported_url(url):
        return

    cleanup_temp_files()
    clean_expired_cache()

    status_msg = await update.message.reply_text("🔎 در حال آنالیز پیشرفته لینک و استخراج کیفیت‌ها...")

    try:
        info = await extract_media_info_robust(url)
    except Exception as e:
        logger.error(f"Error during media extraction: {e}")
        info = None

    if not info:
        info = {
            "direct_url": clean_media_url(url),
            "title": "پست / ویدیو",
            "description": "ارسال مستقیم کیفیت بالا",
            "formats": [{"url": clean_media_url(url), "ext": "mp4", "height": 720, "vcodec": "h264"}],
            "duration": 60,
            "thumbnail": None
        }

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

    keyboard.append([
        InlineKeyboardButton("🔍 تشخیص هوشمند موزیک و بیت (Shazam)", callback_data=f"shazam:{cache_id}:{current_slide}")
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
                await status_msg.edit_text("❌ خطا در دانلود ویدیو. در حال پردازش مجدد...")
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

    elif action_type == "shazam":
        status_msg = await query.message.reply_text("🔍 🎧 در حال اسکن عمیق چندمرحله‌ای صوت و تشخیص میکس‌ها با Shazam Fingerprint...")
        filepath = await download_media_audio(url, "128", file_prefix)

        if filepath and os.path.exists(filepath):
            shazam_res = await recognize_audio_shazam(filepath)

            if shazam_res:
                s_title = shazam_res['title']
                s_artist = shazam_res['artist']
                s_genre = shazam_res.get('genre', 'نامشخص')
                all_detected = shazam_res.get('all_tracks', [])

                if len(all_detected) > 1:
                    mix_text = "🔥 **این پست یک میکس/مَش‌آپ چندهنگه است! لیست آهنگ‌های کشف‌شده:**\n\n"
                    for idx, trk in enumerate(all_detected, 1):
                        mix_text += f"🎵 **آهنگ {idx}:** {trk['title']}\n👤 **اثر:** {trk['artist']}\n\n"
                    
                    mix_text += "🟢 در حال دریافت و دانلود آهنگ اصلی اول..."
                    await status_msg.edit_text(mix_text)
                else:
                    await status_msg.edit_text(f"🎯 **موزیک/بیت دقیق شناسایی شد!**\n\n🎵 عنوان: {s_title}\n👤 اثر: {s_artist}\n🎸 سبک: {s_genre}\n\n🟢 در حال دانلود نسخه کامل 320kbps...")

                # Send each detected track in the mix
                for idx, trk in enumerate(all_detected[:3]): # Download top detected songs
                    cur_title = trk['title']
                    cur_artist = trk['artist']
                    full_res = await search_and_download_full_track(cur_title, cur_artist, f"{cur_title} {cur_artist}", f"shz_{idx}_{file_prefix}")

                    if full_res and os.path.exists(full_res['filepath']):
                        caption_audio = f"🔥 موزیک کامل ({idx if len(all_detected) > 1 else 'Shazam Match'}):\n🎵 عنوان: {cur_title}\n👤 خواننده: {cur_artist}\n✨ کیفیت: 320kbps (HQ)"
                        with open(full_res['filepath'], 'rb') as audio_file:
                            await query.message.reply_audio(
                                audio=audio_file,
                                title=cur_title,
                                performer=cur_artist,
                                caption=caption_audio
                            )
                        if os.path.exists(full_res['filepath']):
                            try:
                                os.remove(full_res['filepath'])
                            except Exception:
                                pass
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ نتوانستیم بیت دقیق را با شزام تشخیص دهیم. در حال تلاش از طریق موتور جستجوی ثانویه...")
                res = await search_and_download_full_track(cached_item.get("track_title", ""), cached_item.get("artist_name", ""), cached_item.get("caption", ""), file_prefix)
                if res and os.path.exists(res['filepath']):
                    with open(res['filepath'], 'rb') as audio_file:
                        await query.message.reply_audio(audio=audio_file, caption="🎧 موزیک پیدا شده")
                    await status_msg.delete()

            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
        else:
            await status_msg.edit_text("❌ خطا در استخراج صوت جهت آنالیز شزام.")

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


async def handle_direct_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows users to send direct Voice/Audio/Video files to identify music & beats via Shazam."""
    msg = update.message
    file_obj = msg.audio or msg.voice or msg.video

    if not file_obj:
        return

    status_msg = await msg.reply_text("🔍 🎧 در حال دریافت فایل صوتی و آنالیز عمیق میکس با Shazam...")
    temp_path = os.path.join(DOWNLOAD_DIR, f"direct_{uuid.uuid4().hex[:6]}.mp3")

    try:
        tg_file = await file_obj.get_file()
        await tg_file.download_to_drive(temp_path)

        shazam_res = await recognize_audio_shazam(temp_path)

        if shazam_res:
            s_title = shazam_res['title']
            s_artist = shazam_res['artist']
            s_genre = shazam_res.get('genre', 'نامشخص')
            all_detected = shazam_res.get('all_tracks', [])

            if len(all_detected) > 1:
                mix_text = "🔥 **این ویس/ویدیو شامل میکس چند آهنگ است! لیست آهنگ‌ها:**\n\n"
                for idx, trk in enumerate(all_detected, 1):
                    mix_text += f"🎵 **آهنگ {idx}:** {trk['title']}\n👤 **اثر:** {trk['artist']}\n\n"
                
                mix_text += "🟢 در حال ارسال آهنگ‌های کامل..."
                await status_msg.edit_text(mix_text)
            else:
                await status_msg.edit_text(f"🎯 **موزیک/بیت دقیق شناسایی شد!**\n\n🎵 عنوان: {s_title}\n👤 اثر: {s_artist}\n🎸 سبک: {s_genre}\n\n🟢 در حال دانلود نسخه کامل 320kbps...")

            for idx, trk in enumerate(all_detected[:3]):
                cur_title = trk['title']
                cur_artist = trk['artist']
                full_res = await search_and_download_full_track(cur_title, cur_artist, cur_title, f"shz_dir_{idx}_{uuid.uuid4().hex[:4]}")

                if full_res and os.path.exists(full_res['filepath']):
                    caption_audio = f"🔥 موزیک کامل (Shazam Match):\n🎵 عنوان: {cur_title}\n👤 خواننده: {cur_artist}\n✨ کیفیت: 320kbps (HQ)"
                    with open(full_res['filepath'], 'rb') as audio_file:
                        await msg.reply_audio(
                            audio=audio_file,
                            title=cur_title,
                            performer=cur_artist,
                            caption=caption_audio
                        )
                    if os.path.exists(full_res['filepath']):
                        try:
                            os.remove(full_res['filepath'])
                        except Exception:
                            pass
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ متأسفانه اثری از این صوت در دیتابیس شزام پیدا نشد.")

    except Exception as ex:
        logger.error(f"Error handling direct audio message: {ex}")
        await status_msg.edit_text("❌ خطا در پردازش فایل صوتی.")
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


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
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.VIDEO, handle_direct_audio_message))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
