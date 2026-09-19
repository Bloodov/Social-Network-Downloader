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


def check_rate_limit(user_id: int) -> str | None:
    """None — можно скачивать, иначе возвращает текст с причиной отказа."""
    if user_id in ADMIN_IDS:
        return None

    now = time.time()
    last = _last_request_at.get(user_id, 0)
    if now - last < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last))
        return f"⏳ Подожди {wait} сек. перед следующим запросом."

    q = _hourly_requests[user_id]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= MAX_DOWNLOADS_PER_HOUR:
        return f"🚫 Лимит {MAX_DOWNLOADS_PER_HOUR} скачиваний в час исчерпан. Попробуй позже."

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


def _pick_quality_options(info: dict) -> list[tuple[str, str]]:
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
        options.append(("🎬 Лучшее качество", "bestvideo+bestaudio/best"))

    options.append(("🎵 Только аудио (mp3)", "bestaudio/best"))
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
    db.touch_user(update.effective_user.id, update.effective_user.username)
    await update.message.reply_text(
        "Привет! Пришли ссылку на видео (YouTube, TikTok, Instagram, Twitter/X, VK, "
        "Facebook, Pinterest, Reddit, Twitch, SoundCloud, Vimeo и другие) — предложу "
        "качество и скачаю.\n\n"
        "TikTok — без водяного знака."
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой Telegram ID: `{update.effective_user.id}`", parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.touch_user(user.id, user.username)

    if db.is_banned(user.id):
        await update.message.reply_text("🚫 Тебе закрыт доступ к боту.")
        return

    text = update.message.text or ""
    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text("Пришли, пожалуйста, ссылку на видео.")
        return
    url = match.group(0)

    limit_error = check_rate_limit(user.id)
    if limit_error:
        await update.message.reply_text(limit_error)
        return

    status_msg = await update.message.reply_text("🔎 Проверяю ссылку...")

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, _extract_info_sync, url)
    except Exception as e:
        logger.warning("Extract failed for %s: %s", url, e)
        await status_msg.edit_text(
            "❌ Не удалось распознать ссылку. Либо сайт не поддерживается, "
            "либо видео приватное/удалено."
        )
        return

    duration = info.get("duration") or 0
    if duration and duration > MAX_VIDEO_DURATION:
        await status_msg.edit_text(
            f"⚠️ Видео длиннее {MAX_VIDEO_DURATION // 60} минут — бот не скачивает такие "
            "ролики, чтобы не перегружать сервер."
        )
        return

    options = _pick_quality_options(info)
    request_id = uuid.uuid4().hex[:10]
    pending_requests[request_id] = {
        "url": url,
        "user_id": user.id,
        "chat_id": update.effective_chat.id,
        "platform": detect_platform(url),
        "title": info.get("title") or "Видео",
        "options": options,
    }

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"dl:{request_id}:{i}")]
        for i, (label, _fmt) in enumerate(options)
    ]

    await status_msg.edit_text(
        f"🎞 {pending_requests[request_id]['title']}\n\nВыбери качество:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, request_id, idx_str = query.data.split(":")
        idx = int(idx_str)
    except ValueError:
        await query.edit_message_text("❌ Запрос устарел, пришли ссылку заново.")
        return

    req = pending_requests.get(request_id)
    if not req:
        await query.edit_message_text("❌ Запрос устарел, пришли ссылку заново.")
        return

    if req["user_id"] != update.effective_user.id:
        await query.answer("Это не твой запрос", show_alert=True)
        return

    label, fmt = req["options"][idx]
    is_audio = "аудио" in label.lower()

    limit_error = check_rate_limit(req["user_id"])
    if limit_error:
        await query.edit_message_text(limit_error)
        pending_requests.pop(request_id, None)
        return

    register_request(req["user_id"])
    await query.edit_message_text(f"⏳ Скачиваю ({label})... Если сервер занят — подожди немного.")

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
            await query.edit_message_text(f"❌ Не удалось скачать: {e}")
            pending_requests.pop(request_id, None)
            return

    try:
        size = os.path.getsize(file_path)
        if size > MAX_TELEGRAM_FILE_SIZE:
            db.log_download(req["user_id"], req["platform"], req["url"], label, "too_large")
            await query.edit_message_text(
                "⚠️ Файл больше 50 МБ — обычный Bot API не может его отправить. "
                "Попробуй выбрать качество ниже."
            )
            return

        await query.edit_message_text("📤 Отправляю...")
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
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
