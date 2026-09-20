"""
Тексты бота на русском и английском. Всё, что видит пользователь,
берётся отсюда — так проще держать оба языка в актуальном состоянии.
"""

TEXTS = {
    "ru": {
        "welcome": (
            "Привет! Я скачиваю фото и видео из соцсетей — YouTube, TikTok, "
            "Instagram и многих других. Просто пришли мне ссылку 🙂\n\n"
            "Сменить язык можно в любой момент командой /language."
        ),
        "choose_language": "Выбери язык:",
        "language_set": "Готово! Теперь я говорю по-русски 🇷🇺",
        "send_link": "Пришли, пожалуйста, ссылку на видео или фото.",
        "banned": "У тебя нет доступа к этому боту.",
        "checking_link": "🔎 Проверяю ссылку...",
        "unsupported_link": "Не получилось загрузить это. Возможно, ссылка недоступна или пока не поддерживается.",
        "too_long": "Это видео слишком длинное — попробуй, пожалуйста, что-нибудь покороче.",
        "choose_quality": "{title}\n\nВыбери качество:",
        "request_expired": "Эта ссылка устарела, пришли её ещё раз.",
        "not_your_request": "Это не твой запрос",
        "downloading": "⏳ Скачиваю...",
        "sending": "📤 Отправляю...",
        "too_large": "Файл получился слишком большим для отправки. Попробуй выбрать качество пониже.",
        "download_error": "Не получилось скачать. Попробуй другую ссылку или качество.",
        "cooldown": "Подожди немного перед следующим запросом 🙂",
        "hourly_limit": "Ты израсходовал лимит скачиваний на этот час — попробуй позже.",
        "myid": "Твой Telegram ID: `{id}`",
        "quality_audio_marker": "аудио",
        "subscribe_required": "Чтобы пользоваться ботом, подпишись на канал {channel}, а потом нажми «Я подписался».",
        "subscribe_button": "📢 Перейти в канал",
        "subscribe_check_button": "✅ Я подписался",
        "subscribe_thanks": "Спасибо за подписку! Теперь пришли ссылку 🙂",
        "subscribe_still_missing": "Пока не вижу подписку 🙁 Попробуй ещё раз.",
    },
    "en": {
        "welcome": (
            "Hi! I download photos and videos from social media — YouTube, TikTok, "
            "Instagram and many more. Just send me a link 🙂\n\n"
            "You can change the language anytime with /language."
        ),
        "choose_language": "Choose your language:",
        "language_set": "Done! I'll speak English from now on 🇬🇧",
        "send_link": "Please send me a video or photo link.",
        "banned": "You don't have access to this bot.",
        "checking_link": "🔎 Checking the link...",
        "unsupported_link": "Couldn't load this. The link might be unavailable or not supported yet.",
        "too_long": "This video is too long — please try something shorter.",
        "choose_quality": "{title}\n\nChoose quality:",
        "request_expired": "This request has expired, please send the link again.",
        "not_your_request": "This isn't your request",
        "downloading": "⏳ Downloading...",
        "sending": "📤 Sending...",
        "too_large": "The file turned out too large to send. Try picking a lower quality.",
        "download_error": "Couldn't download this. Try another link or quality.",
        "cooldown": "Please wait a bit before your next request 🙂",
        "hourly_limit": "You've reached the hourly download limit — try again later.",
        "myid": "Your Telegram ID: `{id}`",
        "quality_audio_marker": "audio",
        "subscribe_required": "To use the bot, please subscribe to {channel}, then tap 'I subscribed'.",
        "subscribe_button": "📢 Open channel",
        "subscribe_check_button": "✅ I subscribed",
        "subscribe_thanks": "Thanks for subscribing! Now send me a link 🙂",
        "subscribe_still_missing": "I still don't see a subscription 🙁 Try again.",
    },
}

DEFAULT_LANG = "ru"


def t(lang: str | None, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else DEFAULT_LANG
    text = TEXTS[lang].get(key) or TEXTS[DEFAULT_LANG].get(key) or key
    return text.format(**kwargs) if kwargs else text
