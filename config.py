"""
Общий конфиг для бота и админ-панели.

По просьбе — токен и ID админа зашиты прямо в код как значения по умолчанию.
Переменные окружения (BOT_TOKEN / ADMIN_IDS), если заданы на хостинге,
всё равно имеют приоритет — так проще менять токен на Railway без правки кода.
"""

import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8976296219:AAGJWQukQxywcUXsrRfXuJxhOx7r-vFbO60")

ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "7043349079").split(",")
    if x.strip().lstrip("-").isdigit()
}
