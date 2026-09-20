"""
Telegram-бот для скачивания видео/аудио с YouTube, TikTok, Instagram и ещё
~1800 сайтов, которые поддерживает yt-dlp (Twitter/X, VK, Facebook, Pinterest,
Reddit, Twitch, SoundCloud, Vimeo и т.д.).

Возможности:
- Автоопределение платформы, скачивание через yt-dlp.
- Для TikTok — без водяного знака (yt-dlp берёт исходник через API площадки).
- Выбор качества видео (1080p/720p/480p) или скачивание только аудио (mp3).
- Антиспам: cooldown между запросами, лимит скачиваний в час на пользователя,
  ограничение числа параллельных загрузок, лимит длительности видео.
- Бан/разбан пользователей (через веб-панель или команды /ban, /unban).
- Встроенная веб-панель админа (admin_panel.py), которая слушает $PORT —
  это нужно для деплоя на Railway (см. README.md).
"""

import os
import re
import time
import uuid
import logging
import tempfile
import asyncio
from pathlib import Path
from urllib.parse import urlparse
from collections import defaultdict, deque

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import database as db
from admin_panel import run_admin_panel_in_thread
from config import BOT_TOKEN, ADMIN_IDS
from locales import t

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # лимит обычного Bot API
MAX_VIDEO_DURATION = int(os.environ.get("MAX_VIDEO_DURATION", "1800"))  # сек, по умолчанию 30 мин

# ---------------- Антиспам ----------------
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "15"))
MAX_DOWNLOADS_PER_HOUR = int(os.environ.get("MAX_DOWNLOADS_PER_HOUR", "10"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))

_last_request_at: dict[int, float] = {}
_hourly_requests: dict[int, deque] = defaultdict(deque)
_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

URL_RE = re.compile(r"https?://\S+")

# Список только для красивой подписи платформы в статистике.
# yt-dlp поддерживает намного больше сайтов — они попадут в категорию "other",
# но скачиваться будут точно так же.
KNOWN_PLATFORMS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "tiktok.com": "TikTok",
    "instagram.com": "Instagram",
    "twitter.com": "Twitter/X", "x.com": "Twitter/X",
    "vk.com": "VK",
    "facebook.com": "Facebook", "fb.watch": "Facebook",
    "pinterest.com": "Pinterest", "pin.it": "Pinterest",
    "reddit.com": "Reddit",
    "twitch.tv": "Twitch",
    "soundcloud.com": "SoundCloud",
    "vimeo.com": "Vimeo",
    "likee.video": "Likee",
    "ok.ru": "Одноклассники",
}

# request_id -> метаданные запроса, ожидающего выбора качества пользователем
pending_requests: dict[str, dict] = {}


def detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    for domain, label in KNOWN_PLATFORMS.items():
        if domain in host:
            return label
    return "other"


def get_user_lang(user_id: int, telegram_language_code: str | None = None) -> str:
    """Язык пользователя: сохранённый выбор, иначе эвристика по языку Telegram-клиента."""
    saved = db.get_language(user_id)
    if saved:
        return saved
    if telegram_language_code and telegram_language_code.lower().startswith("ru"):
        return "ru"
    return "en"


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇷🇺 Русский", callback_data="setlang:ru"),
            InlineKeyboardButton("🇬🇧 English", callback_data="setlang:en"),
        ]
    ])


# ---------------- Обязательная подписка на канал ----------------

REQUIRED_CHANNEL_SETTING_KEY = "required_channel"


def get_required_channel() -> str | None:
    """Юзернейм/ID канала (например, @mychannel) из настроек админ-панели, либо None, если отключено."""
    value = db.get_setting(REQUIRED_CHANNEL_SETTING_KEY, "")
    value = (value or "").strip()
    return value or None


async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int, channel: str) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        # Если проверить не получилось (например, бот не добавлен админом в канал),
        # не блокируем всех пользователей из-за неправильной настройки — пропускаем,
        # но громко логируем, чтобы админ заметил и поправил канал в панели.
        logger.warning("Subscription check failed for user %s, channel %s: %s", user_id, channel, e)
        return True


def subscribe_keyboard(lang: str, channel: str) -> InlineKeyboardMarkup:
    channel_username = channel.lstrip("@")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "subscribe_button"), url=f"https://t.me/{channel_username}")],
        [InlineKeyboardButton(t(lang, "subscribe_check_button"), callback_data="checksub")],
    ])


async def handle_subscription_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    lang = get_user_lang(user.id, user.language_code)

    channel = get_required_channel()
    if not channel or await is_subscribed(context, user.id, channel):
        await query.answer(t(lang, "subscribe_thanks"), show_alert=True)
        await query.delete_message()
        return

    await query.answer(t(lang, "subscribe_still_missing"), show_alert=True)


def check_rate_limit(user_id: int, lang: str) -> str | None:
    """None — можно скачивать, иначе возвращает текст с причиной отказа."""
    if user_id in ADMIN_IDS:
        return None

    now = time.time()
    last = _last_request_at.get(user_id, 0)
    if now - last < COOLDOWN_SECONDS:
        return t(lang, "cooldown")

    q = _hourly_requests[user_id]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= MAX_DOWNLOADS_PER_HOUR:
        return t(lang, "hourly_limit")

    return None


def register_request(user_id: int):
    now = time.time()
    _last_request_at[user_id] = now
    _hourly_requests[user_id].append(now)


# ---------------- yt-dlp helpers ----------------

def _extract_info_sync(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _pick_quality_options(info: dict, lang: str) -> list[tuple[str, str]]:
    """Список (подпись_кнопки, format_selector) для показа пользователю."""
    formats = info.get("formats") or []
    heights = sorted({f.get("height") for f in formats if f.get("height")}, reverse=True)

    chosen_heights: list[int] = []
    for target in (1080, 720, 480):
        candidates = [h for h in heights if h <= target]
        pick = max(candidates) if candidates else (min(heights) if heights else None)
        if pick and pick not in chosen_heights:
            chosen_heights.append(pick)
    if not chosen_heights and heights:
        chosen_heights = heights[:3]

    options = [
        (f"🎬 {h}p", f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/best[height<={h}]")
        for h in chosen_heights
    ]
    if not options:
        best_label = "🎬 Лучшее качество" if lang == "ru" else "🎬 Best quality"
        options.append((best_label, "bestvideo+bestaudio/best"))

    audio_label = "🎵 Только аудио (mp3)" if lang == "ru" else "🎵 Audio only (mp3)"
    options.append((audio_label, "bestaudio/best"))
    return options


def _download_sync(url: str, fmt: str, out_template: str, audio_only: bool) -> str:
    opts = {
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "format": fmt,
        "socket_timeout": 30,
    }
    if audio_only:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if audio_only:
            filename = str(Path(filename).with_suffix(".mp3"))
        return filename


# ---------------- Handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.touch_user(user.id, user.username)

    saved_lang = db.get_language(user.id)
    if saved_lang:
        await update.message.reply_text(t(saved_lang, "welcome"))
    else:
        # Первый запуск — просим выбрать язык, приветствие покажем после выбора.
        await update.message.reply_text(
            "Выбери язык / Choose your language:",
            reply_markup=language_keyboard(),
        )


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_user_lang(update.effective_user.id, update.effective_user.language_code)
    await update.message.reply_text(t(lang, "choose_language"), reply_markup=language_keyboard())


async def handle_language_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, lang = query.data.split(":")
    db.set_language(update.effective_user.id, lang)
    await query.edit_message_text(t(lang, "language_set") + "\n\n" + t(lang, "welcome"))


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_user_lang(update.effective_user.id, update.effective_user.language_code)
    await update.message.reply_text(
        t(lang, "myid", id=update.effective_user.id), parse_mode="Markdown"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.touch_user(user.id, user.username)
    lang = get_user_lang(user.id, user.language_code)

    if db.is_banned(user.id):
        await update.message.reply_text(t(lang, "banned"))
        return

    channel = get_required_channel()
    if channel and not await is_subscribed(context, user.id, channel):
        await update.message.reply_text(
            t(lang, "subscribe_required", channel=channel),
            reply_markup=subscribe_keyboard(lang, channel),
        )
        return

    text = update.message.text or ""
    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text(t(lang, "send_link"))
        return
    url = match.group(0)

    limit_error = check_rate_limit(user.id, lang)
    if limit_error:
        await update.message.reply_text(limit_error)
        return

    status_msg = await update.message.reply_text(t(lang, "checking_link"))

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, _extract_info_sync, url)
    except Exception as e:
        logger.warning("Extract failed for %s: %s", url, e)
        await status_msg.edit_text(t(lang, "unsupported_link"))
        return

    duration = info.get("duration") or 0
    if duration and duration > MAX_VIDEO_DURATION:
        await status_msg.edit_text(t(lang, "too_long"))
        return

    options = _pick_quality_options(info, lang)
    request_id = uuid.uuid4().hex[:10]
    title = info.get("title") or ("Видео" if lang == "ru" else "Video")
    pending_requests[request_id] = {
        "url": url,
        "user_id": user.id,
        "chat_id": update.effective_chat.id,
        "platform": detect_platform(url),
        "title": title,
        "options": options,
        "lang": lang,
    }

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"dl:{request_id}:{i}")]
        for i, (label, _fmt) in enumerate(options)
    ]

    await status_msg.edit_text(
        t(lang, "choose_quality", title=title),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    fallback_lang = get_user_lang(update.effective_user.id, update.effective_user.language_code)

    try:
        _, request_id, idx_str = query.data.split(":")
        idx = int(idx_str)
    except ValueError:
        await query.edit_message_text(t(fallback_lang, "request_expired"))
        return

    req = pending_requests.get(request_id)
    if not req:
        await query.edit_message_text(t(fallback_lang, "request_expired"))
        return

    lang = req["lang"]

    if req["user_id"] != update.effective_user.id:
        await query.answer(t(lang, "not_your_request"), show_alert=True)
        return

    label, fmt = req["options"][idx]
    is_audio = t(lang, "quality_audio_marker") in label.lower()

    limit_error = check_rate_limit(req["user_id"], lang)
    if limit_error:
        await query.edit_message_text(limit_error)
        pending_requests.pop(request_id, None)
        return

    register_request(req["user_id"])
    await query.edit_message_text(t(lang, "downloading"))

    file_path = None
    async with _download_semaphore:
        tmp_dir = tempfile.mkdtemp(prefix="tgdl_")
        out_template = str(Path(tmp_dir) / "%(id)s.%(ext)s")
        try:
            loop = asyncio.get_running_loop()
            file_path = await loop.run_in_executor(
                None, _download_sync, req["url"], fmt, out_template, is_audio
            )
        except Exception as e:
            logger.exception("Download failed: %s", req["url"])
            db.log_download(req["user_id"], req["platform"], req["url"], label, "error")
            await query.edit_message_text(t(lang, "download_error"))
            pending_requests.pop(request_id, None)
            return

    try:
        size = os.path.getsize(file_path)
        if size > MAX_TELEGRAM_FILE_SIZE:
            db.log_download(req["user_id"], req["platform"], req["url"], label, "too_large")
            await query.edit_message_text(t(lang, "too_large"))
            return

        await query.edit_message_text(t(lang, "sending"))
        with open(file_path, "rb") as f:
            if is_audio:
                await context.bot.send_audio(chat_id=req["chat_id"], audio=f, title=req["title"])
            else:
                await context.bot.send_video(
                    chat_id=req["chat_id"], video=f, supports_streaming=True, write_timeout=120
                )
        db.log_download(req["user_id"], req["platform"], req["url"], label, "ok")
        await query.delete_message()
    finally:
        pending_requests.pop(request_id, None)
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                os.rmdir(os.path.dirname(file_path))
            except OSError:
                pass


# ---------------- Админ-команды (дублируют веб-панель прямо в чате) ----------------

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("Команда доступна только админам.")
            return
        return await func(update, context)
    return wrapper


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = db.get_stats()
    lines = [
        f"👥 Пользователей: {s['total_users']}",
        f"✅ Скачано успешно: {s['total_downloads']}",
        f"❌ Ошибок/отказов: {s['failed_downloads']}",
        f"🚫 Забанено: {s['banned_users']}",
        "",
        "По платформам:",
    ]
    lines += [f"  {p}: {c}" for p, c in s["by_platform"]] or ["  (пока нет данных)"]
    await update.message.reply_text("\n".join(lines))


@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Использование: /ban <user_id>")
        return
    uid = int(context.args[0])
    db.set_banned(uid, True)
    await update.message.reply_text(f"Пользователь {uid} забанен.")


@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Использование: /unban <user_id>")
        return
    uid = int(context.args[0])
    db.set_banned(uid, False)
    await update.message.reply_text(f"Пользователь {uid} разбанен.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Переменная окружения BOT_TOKEN не установлена. "
            "Получите токен у @BotFather и задайте BOT_TOKEN в переменных окружения."
        )

    db.init_db()
    run_admin_panel_in_thread()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("language", language_command))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CallbackQueryHandler(handle_language_choice, pattern=r"^setlang:"))
    app.add_handler(CallbackQueryHandler(handle_subscription_check, pattern=r"^checksub$"))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
