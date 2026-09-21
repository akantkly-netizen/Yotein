import os
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import yt_dlp

# بارگذاری متغیرهای محیطی از فایل .env (در صورت وجود)
load_dotenv()

# تنظیم لاگ‌ها
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# دریافت توکن ربات از متغیرهای محیطی (Railway / .env)
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ساختار ایموجی‌های پرمیوم (انیمیشنی) تلگرام
# می‌توانید emoji-idها را با ID استیکرها یا ایموجی‌های اختصاصی خود جایگزین کنید
PREMIUM_EMOJIS = {
    "DOWNLOAD": '<tg-emoji emoji-id="5368324170671202286">⚡</tg-emoji>',
    "MUSIC": '<tg-emoji emoji-id="5368324170671202286">🎵</tg-emoji>',
    "CHECK": '<tg-emoji emoji-id="5368324170671202286">✅</tg-emoji>',
    "VIDEO": '<tg-emoji emoji-id="5368324170671202286">🎬</tg-emoji>',
    "WARN": '<tg-emoji emoji-id="5368324170671202286">⚠️</tg-emoji>',
}

# حافظه موقت جهت نگهداری اطلاعات لینک‌های کاربران
user_data_store = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فرمان /start"""
    welcome_text = (
        f"{PREMIUM_EMOJIS['DOWNLOAD']} **سلام! من ربات دانلودر حرفه‌ای هستم.**\n\n"
        f"لینک ویدیو اینستاگرام یا یوتیوب رو برام بفرست تا با کیفیت‌های مختلف بگیریش.\n\n"
        f"همچنین می‌تونی اسم یا لینک یک آهنگ رو بفرستی تا توی {PREMIUM_EMOJIS['MUSIC']} **Spotify** و **YouTube Music** برات پیداش کنم!"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")


async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش متن ورودی کاربر (لینک ویدیو یا نام موزیک)"""
    text = update.message.text.strip()
    user_id = update.message.from_user.id

    # تشخیص لینک‌های یوتیوب یا اینستاگرام
    if any(domain in text for domain in ["youtube.com", "youtu.be", "instagram.com"]):
        msg = await update.message.reply_text(
            f"{PREMIUM_EMOJIS['DOWNLOAD']} در حال استخراج اطلاعات و کیفیت‌های ویدیو... لطفا شکیبا باشید."
        )

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'age_limit': 99,  # دور زدن محدودیت سنی
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(text, download=False)
                title = info.get('title', 'بدون عنوان')
                description = (
                    info.get('description', '') or info.get('caption', 'بدون کپشن')
                )
                caption_cut = (
                    (description[:250] + '...') if len(description) > 250 else description
                )

                formats = info.get('formats', [])
                keyboard = []
                seen_resolutions = set()

                # استخراج کیفیت‌های ویدیویی اصلی
                for f in formats:
                    res = f.get('format_note') or f.get('resolution') or (f"{f.get('height')}p" if f.get('height') else None)
                    format_id = f.get('format_id')

                    if f.get('vcodec') != 'none' and res and res not in seen_resolutions:
                        seen_resolutions.add(res)
                        keyboard.append([
                            InlineKeyboardButton(f"🎬 کیفیت {res}", callback_data=f"dl|{format_id}")
                        ])

                # گزینه‌های عمومی
                keyboard.append([
                    InlineKeyboardButton("🎧 دانلود فقط فایل صوتی (MP3)", callback_data="dl|bestaudio")
                ])
                keyboard.append([
                    InlineKeyboardButton("✨ دانلود بهترین کیفیت ممکن", callback_data="dl|best")
                ])

                user_data_store[user_id] = {'url': text, 'title': title}

                reply_markup = InlineKeyboardMarkup(keyboard)
                await msg.edit_text(
                    f"{PREMIUM_EMOJIS['VIDEO']} <b>{title}</b>\n\n"
                    f"📝 <b>کپشن / توضیحات:</b>\n{caption_cut}\n\n"
                    f"لطفاً کیفیت مورد نظر خود را انتخاب کنید:",
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )

        except Exception as e:
            logger.error(f"Error extracting video info: {e}")
            await msg.edit_text(
                f"❌ خطایی در دریافت اطلاعات ویدیو رخ داد:\n`{str(e)}`",
                parse_mode="Markdown"
            )

    else:
        # جستجوی موزیک در صورت ارسال متن عادی (اسم آهنگ یا خواننده)
        await search_music(update, text)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت دکمه‌های شیشه‌ای انتخاب کیفیت"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data.split("|")

    if data[0] == "dl":
        format_id = data[1]
        user_info = user_data_store.get(user_id)

        if not user_info:
            await query.edit_message_text(
                f"{PREMIUM_EMOJIS['WARN']} نشست شما منقضی شده، لطفاً لینک را دوباره ارسال کنید."
            )
            return

        url = user_info['url']
        await query.edit_message_text(
            f"{PREMIUM_EMOJIS['DOWNLOAD']} در حال دانلود و ارسال فایل (تا سقف ۲ گیگابایت)... لطفاً منتظر بمانید."
        )

        file_prefix = f"download_{user_id}"
        file_path = f"{file_prefix}.mp4"

        ydl_opts = {
            'format': f"{format_id}+bestaudio/best" if format_id != "best" else "bestvideo+bestaudio/best",
            'outtmpl': file_path,
            'max_filesize': 2000 * 1024 * 1024,  # سقف ۲ گیگابایت برای تلگرام
            'quiet': True,
            'age_limit': 99,
        }

        # دانلود فقط صوت (MP3)
        if format_id == "bestaudio":
            file_path = f"{file_prefix}.mp3"
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
            ydl_opts['outtmpl'] = file_prefix

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            actual_file = file_path if os.path.exists(file_path) else f"{file_prefix}.mp3"

            if not os.path.exists(actual_file):
                # جستجو برای پیدا کردن پسوند واقعی پس از کانورت
                for f in os.listdir("."):
                    if f.startswith(file_prefix):
                        actual_file = f
                        break

            with open(actual_file, 'rb') as f:
                if format_id == "bestaudio" or actual_file.endswith(".mp3"):
                    await context.bot.send_audio(
                        chat_id=user_id,
                        audio=f,
                        caption=f"{PREMIUM_EMOJIS['CHECK']} با موفقیت دانلود شد!"
                    )
                else:
                    await context.bot.send_video(
                        chat_id=user_id,
                        video=f,
                        caption=f"{PREMIUM_EMOJIS['CHECK']} با موفقیت دانلود شد!"
                    )

            # پاکسازی فایل پس از ارسال
            if os.path.exists(actual_file):
                os.remove(actual_file)

        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ خطا در دانلود یا ارسال فایل:\n{str(e)}"
            )


async def search_music(update: Update, query_text: str):
    """جستجوی موزیک در YouTube Music و Spotify"""
    msg = await update.message.reply_text(
        f"{PREMIUM_EMOJIS['MUSIC']} در حال جستجو در Spotify و YouTube Music..."
    )

    search_query = f"ytsearch5:{query_text} audio"
    ydl_opts = {'quiet': True, 'extract_flat': True}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            results = ydl.extract_info(search_query, download=False)
            entries = results.get('entries', [])

            if not entries:
                await msg.edit_text("❌ هیچ آهنگی پیدا نشد.")
                return

            keyboard = []
            for entry in entries:
                title = entry.get('title', 'موزیک')[:35]
                video_url = entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
                
                # ذخیره لینک انتخاب شده
                user_data_store[update.message.from_user.id] = {
                    'url': video_url,
                    'title': title
                }
                keyboard.append([
                    InlineKeyboardButton(f"🎵 {title}", callback_data="dl|bestaudio")
                ])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await msg.edit_text(
                f"{PREMIUM_EMOJIS['MUSIC']} نتایج یافت‌شده برای <b>{query_text}</b>:\n\nیک گزینه را برای دانلود MP3 انتخاب کنید:",
                parse_mode="HTML",
                reply_markup=reply_markup
            )

    except Exception as e:
        logger.error(f"Error searching music: {e}")
        await msg.edit_text(f"❌ خطایی در جستجوی موزیک رخ داد:\n{str(e)}")


def main():
    """شروع به کار ربات"""
    if not BOT_TOKEN:
        raise ValueError("خطا: BOT_TOKEN یافت نشد! لطفاً متغیر BOT_TOKEN را در تنظیمات Railway یا فایل .env تنظیم کنید.")

    app = Application.builder().token(BOT_TOKEN).build()

    # هندلرها
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_message))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("✅ ربات دانلودر با موفقیت روشن شد و آماده به کار است...")
    app.run_polling()


if __name__ == '__main__':
    main()
