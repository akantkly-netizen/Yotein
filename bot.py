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

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
user_data_store = {}


def get_ydl_options():
    """تنظیمات بهینه‌شده برای دور زدن بن و ارورها"""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'age_limit': 99,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'mweb']
            }
        }
    }
    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = 'cookies.txt'
    return opts


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "⚡ **سلام! به ربات دانلودر خوش آمدید.**\n\n"
        "🔗 **دانلود ویدیو:** لینک اینستاگرام یا یوتیوب را بفرستید.\n"
        "🎵 **جستجوی آهنگ:** نام آهنگ یا خواننده را بفرستید."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.message.from_user.id

    if any(domain in text for domain in ["youtube.com", "youtu.be", "instagram.com"]):
        msg = await update.message.reply_text("🔎 در حال دریافت اطلاعات ویدیو و کیفیت‌ها...")

        ydl_opts = get_ydl_options()

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(text, download=False)
                title = info.get('title', 'بدون عنوان')
                thumbnail = info.get('thumbnail')
                description = info.get('description', '') or info.get('caption', '') or 'بدون توضیح'
                caption_cut = (description[:200] + '...') if len(description) > 200 else description

                formats = info.get('formats', [])
                keyboard = []
                seen_resolutions = set()

                for f in formats:
                    res = f.get('format_note') or f.get('resolution') or (f"{f.get('height')}p" if f.get('height') else None)
                    format_id = f.get('format_id')

                    # فقط کیفیت‌های ویدیویی واقعی را لیست می‌کنیم
                    if f.get('vcodec') != 'none' and res and res not in seen_resolutions:
                        seen_resolutions.add(res)
                        keyboard.append([
                            InlineKeyboardButton(f"🎬 کیفیت {res}", callback_data=f"dl|{format_id}")
                        ])

                # دکمه‌های صوتی
                keyboard.append([
                    InlineKeyboardButton("🎧 دانلود آهنگ (128kbps)", callback_data="dl|audio_128"),
                    InlineKeyboardButton("🎵 دانلود آهنگ (320kbps)", callback_data="dl|audio_320")
                ])

                user_data_store[user_id] = {'url': text, 'title': title}
                reply_markup = InlineKeyboardMarkup(keyboard)

                caption_text = (
                    f"🎬 **{title}**\n\n"
                    f"📝 **توضیحات:**\n{caption_cut}\n\n"
                    f"👇 **کیفیت مورد نظر را انتخاب کنید:**"
                )

                await msg.delete()

                if thumbnail:
                    try:
                        await update.message.reply_photo(
                            photo=thumbnail,
                            caption=caption_text,
                            parse_mode="Markdown",
                            reply_markup=reply_markup
                        )
                    except Exception:
                        await update.message.reply_text(
                            caption_text,
                            parse_mode="Markdown",
                            reply_markup=reply_markup
                        )
                else:
                    await update.message.reply_text(
                        caption_text,
                        parse_mode="Markdown",
                        reply_markup=reply_markup
                    )

        except Exception as e:
            logger.error(f"Error extracting info: {e}")
            await msg.edit_text(f"❌ خطایی در استخراج اطلاعات رخ داد:\n`{str(e)}`", parse_mode="Markdown")

    else:
        await search_music(update, text)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data.split("|")

    if data[0] == "dl":
        format_id = data[1]
        user_info = user_data_store.get(user_id)

        if not user_info:
            await query.message.reply_text("⚠️ نشست شما منقضی شده است. لطفاً لینک را دوباره بفرستید.")
            return

        url = user_info['url']
        status_msg = await query.message.reply_text("⚡ در حال دانلود و پردازش فایل... لطفاً منتظر بمانید.")

        file_prefix = f"download_{user_id}"

        ydl_opts = get_ydl_options()
        ydl_opts.update({
            'outtmpl': f"{file_prefix}.%(ext)s",
            'max_filesize': 2000 * 1024 * 1024,
        })

        if format_id == "audio_128":
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '128',
            }]

        elif format_id == "audio_320":
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '320',
            }]

        else:
            # فرمت هوشمند برای جلوگیری از ارور Requested format is not available
            ydl_opts['format'] = f"{format_id}+bestaudio/bestvideo+bestaudio/best"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            actual_file = None
            for f in os.listdir("."):
                if f.startswith(file_prefix):
                    actual_file = f
                    break

            if actual_file and os.path.exists(actual_file):
                with open(actual_file, 'rb') as f:
                    if actual_file.endswith((".mp3", ".m4a", ".webm", ".ogg")):
                        await context.bot.send_audio(
                            chat_id=user_id,
                            audio=f,
                            caption="✅ فایل صوتی با موفقیت دانلود شد!"
                        )
                    else:
                        await context.bot.send_video(
                            chat_id=user_id,
                            video=f,
                            caption="✅ ویدیو با موفقیت دانلود شد!"
                        )

                os.remove(actual_file)
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ فایل یافت نشد.")

        except Exception as e:
            logger.error(f"Error downloading: {e}")
            await status_msg.edit_text(f"❌ خطا در دانلود:\n`{str(e)}`", parse_mode="Markdown")


async def search_music(update: Update, query_text: str):
    msg = await update.message.reply_text("🎵 در حال جستجوی موزیک...")

    search_query = f"ytsearch5:{query_text} audio"
    ydl_opts = get_ydl_options()
    ydl_opts['extract_flat'] = True

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            results = ydl.extract_info(search_query, download=False)
            entries = results.get('entries', [])

            if not entries:
                await msg.edit_text("❌ هیچ آهنگی یافت نشد.")
                return

            keyboard = []
            for entry in entries:
                title = entry.get('title', 'موزیک')[:35]
                video_url = entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}"

                user_data_store[update.message.from_user.id] = {
                    'url': video_url,
                    'title': title
                }

                keyboard.append([
                    InlineKeyboardButton(f"🎧 {title} (128)", callback_data="dl|audio_128"),
                    InlineKeyboardButton(f"🎵 (320)", callback_data="dl|audio_320")
                ])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await msg.edit_text(
                f"🔎 **نتایج جستجو برای:** `{query_text}`\n\nکیفیت مورد نظر برای دانلود را انتخاب کنید:",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )

    except Exception as e:
        logger.error(f"Error searching music: {e}")
        await msg.edit_text(f"❌ خطایی در جستجو پیش آمد:\n`{str(e)}`", parse_mode="Markdown")


def main():
    if not BOT_TOKEN:
        raise ValueError("خطا: BOT_TOKEN تنظیم نشده است.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_message))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("✅ ربات بدون مشکل روشن شد...")
    app.run_polling()


if __name__ == '__main__':
    main()
