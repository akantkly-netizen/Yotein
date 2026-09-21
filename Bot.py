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

# تنظیمات ثبت لاگ‌ها برای اشکال‌زدایی
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# توکن ربات تلگرام (از متغیرهای محیطی Railway خوانده می‌شود)
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")

# پوشه موقت برای ذخیره فایل‌های دانلود شده
DOWNLOAD_DIR = "temp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# حافظه موقت برای نگهداری اطلاعات پست‌های پردازش شده (برای جلوگیری از حجم بالای callback_data)
# ساختار: {cache_id: {"url": str, "caption": str, "thumbnail": str}}
MEDIA_CACHE: Dict[str, Dict[str, Any]] = {}


def format_size(bytes_size: Optional[int]) -> str:
    """تبدیل بایت به فرمت قابل خواندن برای کاربر (MB, KB)"""
    if not bytes_size or bytes_size <= 0:
        return "نامشخص"
    
    mb = bytes_size / (1024 * 1024)
    if mb >= 1.0:
        return f"{mb:.1f} MB"
    
    kb = bytes_size / 1024
    return f"{int(kb)} KB"


def is_instagram_url(url: str) -> bool:
    """بررسی validity لینک ورودی اینستاگرام"""
    ig_pattern = r"(https?://)?(www\.)?(instagram\.com|instagr\.am)/(p|reel|tv)/[A-Za-z0-9_-]+"
    return bool(re.search(ig_pattern, url))


async def extract_instagram_info(url: str) -> Optional[Dict[str, Any]]:
    """
    استخراج متاداده‌های پست اینستاگرام شامل کیفیت‌ها، تامنیل و کپشن
    با اجرا در اجرای پس‌زمینه (Async Executor)
    """
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
    }
    
    loop = asyncio.get_event_loop()
    try:
        def _fetch():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
                
        info = await loop.run_in_executor(None, _fetch)
        return info
    except Exception as e:
        logger.error(f"Error extracting info from {url}: {e}")
        return None


async def download_media_file(url: str, format_id: str, output_prefix: str) -> Optional[str]:
    """دانلود ویدیو بر اساس format_id خاص"""
    output_template = os.path.join(DOWNLOAD_DIR, f"{output_prefix}.%(ext)s")
    
    ydl_opts = {
        'format': format_id,
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
    }
    
    loop = asyncio.get_event_loop()
    try:
        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                return filename

        filepath = await loop.run_in_executor(None, _download)
        # ممکن است پسوند فایل تغییر کرده باشد، فایل دانلود شده را بررسی می‌کنیم
        matching_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{output_prefix}.*"))
        if matching_files:
            return matching_files[0]
        return filepath if os.path.exists(filepath) else None
    except Exception as e:
        logger.error(f"Error downloading video format {format_id}: {e}")
        return None


async def download_audio_file(url: str, bitrate: str, output_prefix: str) -> Optional[str]:
    """استخراج موزیک از ویدیو با کیفیت تعیین شده (128k یا 320k)"""
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
    
    loop = asyncio.get_event_loop()
    try:
        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
                return os.path.join(DOWNLOAD_DIR, f"{output_prefix}.mp3")

        filepath = await loop.run_in_executor(None, _download)
        return filepath if os.path.exists(filepath) else None
    except Exception as e:
        logger.error(f"Error extracting audio with bitrate {bitrate}: {e}")
        return None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پاسخ به دستور /start طبق پیام درخواستی کاربر"""
    welcome_message = (
        "هی سلام من ربات دانلود اینستا هستم "
        "با من میتونی ویدیو های اینستا رو بدون محدودیت دانلود کنی ⚡️\n\n"
        "فقط کافیه لینک پست یا ریلز مورد نظرت رو برام بفرستی!"
    )
    await update.message.reply_text(welcome_message)


async def handle_instagram_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش لینک‌های ورودی و نمایش دکمه‌های دانلود کیفیت‌های مختلف"""
    url = update.message.text.strip()
    
    if not is_instagram_url(url):
        return

    status_msg = await update.message.reply_text("🔍 در حال بررسی و پردازش لینک اینستاگرام...")
    
    info = await extract_instagram_info(url)
    
    if not info:
        await status_msg.edit_text("❌ متأسفانه مشکلی در دریافت اطلاعات ویدیو پیش آمد. لطفا از عمومی (Public) بودن پیج اطمینان حاصل کنید.")
        return

    # ذخیره در کش جهت کوتاه کردن callback_data
    cache_id = str(uuid.uuid4())[:8]
    
    # استخراج کیفیت‌های ویدیو
    formats = info.get('formats', [])
    thumbnail = info.get('thumbnail', '')
    raw_caption = info.get('description') or info.get('title') or "ویدیو اینستاگرام"
    
    # خلاصه‌سازی کپشن برای نمایش بهتر در تلگرام
    caption = raw_caption[:800] + "..." if len(raw_caption) > 800 else raw_caption

    keyboard = []
    
    # گروه‌بندی یا جداسازی کیفیت‌ها بر اساس ارتفاع تصویر (height)
    valid_formats = []
    seen_heights = set()

    for f in formats:
        # فیلتر کردن فرمت‌هایی که شامل ویدیو هستند
        if f.get('vcodec') != 'none' and f.get('height'):
            height = f.get('height')
            if height not in seen_heights:
                seen_heights.add(height)
                filesize = f.get('filesize') or f.get('filesize_approx')
                valid_formats.append({
                    'format_id': f.get('format_id'),
                    'height': height,
                    'size_str': format_size(filesize)
                })

    # مرتب‌سازی کیفیت‌ها از کم به زیاد
    valid_formats.sort(key=lambda x: x['height'])

    # ساخت دکمه‌ها برای کیفیت ویدیو
    for fmt in valid_formats:
        btn_text = f"📹 دانلود کیفیت {fmt['height']}p - ({fmt['size_str']})"
        callback_data = f"vid:{cache_id}:{fmt['format_id']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

    # اگر کیفیت‌های مختلف جدا نشدند، دکمه دانلود پیش‌فرض قرار می‌دهیم
    if not keyboard:
        keyboard.append([
            InlineKeyboardButton("📹 دانلود ویدیو با بهترین کیفیت", callback_data=f"vid:{cache_id}:best")
        ])

    # دکمه‌های استخراج صوتی (MP3 128 & 320)
    keyboard.append([
        InlineKeyboardButton("🎵 دانلود آهنگ 128", callback_data=f"aud:{cache_id}:128"),
        InlineKeyboardButton("🎵 دانلود آهنگ 320", callback_data=f"aud:{cache_id}:320"),
    ])

    MEDIA_CACHE[cache_id] = {
        "url": url,
        "caption": caption,
        "thumbnail": thumbnail
    }

    reply_markup = InlineKeyboardMarkup(keyboard)

    # پاک کردن پیام اولیه پردازش
    await status_msg.delete()

    # ارسال تامنیل همراه با کپشن و دکمه‌های شیشه‌ای
    if thumbnail:
        try:
            await update.message.reply_photo(
                photo=thumbnail,
                caption=f"📝 **کپشن:**\n{caption}",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            return
        except Exception:
            pass  # در صورت خطا در ارسال تامنیل، به ارسال متن سوییچ می‌کند

    await update.message.reply_text(
        text=f"📝 **کپشن:**\n{caption}\n\nکیفیت مورد نظر خود را انتخاب کنید:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت کلیک روی دکمه‌های شیشه‌ای دانلود ویدیو یا آهنگ"""
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    if len(data) < 3:
        return

    action_type = data[0]   # 'vid' یا 'aud'
    cache_id = data[1]      # کلید شناسه متاداده
    param = data[2]         # format_id برای ویدیو یا bitrate برای صوت

    cached_data = MEDIA_CACHE.get(cache_id)
    if not cached_data:
        await query.edit_message_caption(caption="❌ اطلاعات این پست منقضی شده است. لطفاً دوباره لینک را ارسال کنید.")
        return

    url = cached_data["url"]
    file_prefix = f"dl_{cache_id}_{uuid.uuid4().hex[:4]}"

    if action_type == "vid":
        # دانلود و ارسال ویدیو
        await query.edit_message_caption(caption="⏳ در حال دانلود ویدیو، لطفاً شکیبا باشید...")
        
        filepath = await download_media_file(url, param, file_prefix)
        
        if filepath and os.path.exists(filepath):
            await query.edit_message_caption(caption="⬆️ در حال آپلود و ارسال ویدیو به تلگرام...")
            with open(filepath, 'rb') as video_file:
                await query.message.reply_video(
                    video=video_file,
                    caption="✅ **ویدیو شما با موفقیت دانلود شد.**",
                    parse_mode="Markdown"
                )
            # حذف فایل پس از ارسال
            os.remove(filepath)
        else:
            await query.edit_message_caption(caption="❌ خطایی هنگام دانلود ویدیو رخ داد.")

    elif action_type == "aud":
        # استخراج و ارسال موزیک
        await query.edit_message_caption(caption=f"⏳ در حال استخراج و ساخت فایل MP3 با کیفیت {param}kbps...")
        
        filepath = await download_audio_file(url, param, file_prefix)
        
        if filepath and os.path.exists(filepath):
            await query.edit_message_caption(caption="⬆️ در حال ارسال فایل صوتی به تلگرام...")
            with open(filepath, 'rb') as audio_file:
                await query.message.reply_audio(
                    audio=audio_file,
                    title=f"Instagram Audio ({param}k)",
                    caption=f"🎵 **آهنگ استخراج شده با کیفیت {param}kbps**",
                    parse_mode="Markdown"
                )
            # حذف فایل پس از ارسال
            os.remove(filepath)
        else:
            await query.edit_message_caption(caption="❌ خطایی هنگام استخراج فایل صوتی رخ داد.")


def main():
    """شروع به کار ربات"""
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("🔴 لطفاً ابتدا توکن ربات تلگرام خود را در متغیر BOT_TOKEN قرار دهید.")
        return

    print("🟢 ربات با موفقیت روشن شد و در حال گوش به زنگ پیام‌ها است...")
    
    app = Application.builder().token(BOT_TOKEN).build()

    # ثبت هندرها
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_instagram_link))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # شروع Polling
    app.run_polling()


if __name__ == "__main__":
    main()
