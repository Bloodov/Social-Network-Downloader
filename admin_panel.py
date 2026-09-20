"""
Небольшая веб-панель администратора.

Запускается в отдельном потоке внутри того же процесса, что и бот
(см. run_admin_panel_in_thread в bot.py), и слушает $PORT — это нужно,
чтобы Railway видел открытый порт и считал сервис "живым".

Доступ закрыт HTTP Basic Auth (логин/пароль из переменных окружения
ADMIN_USER / ADMIN_PASS) — обязательно смените пароль по умолчанию перед деплоем.
"""

import os
import time
import threading
from functools import wraps

import requests
from flask import Flask, request, redirect, url_for, Response

import database as db
from config import BOT_TOKEN

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "change_me")
PORT = int(os.environ.get("PORT", "8080"))

app = Flask(__name__)

# ---------------- Рассылка сообщений всем пользователям ----------------
# Отправка идёт напрямую через Telegram Bot HTTP API (без python-telegram-bot),
# чтобы не зависеть от asyncio-цикла бота, который крутится в другом потоке.

_broadcast_status = {"running": False, "sent": 0, "failed": 0, "total": 0, "last_text": ""}
_broadcast_lock = threading.Lock()

# Пауза между сообщениями — держим ниже общего лимита Telegram (~30 сообщений/сек),
# чтобы не словить 429 Too Many Requests при большой базе пользователей.
BROADCAST_DELAY_SECONDS = float(os.environ.get("BROADCAST_DELAY_SECONDS", "0.05"))


def _send_broadcast(text: str):
    users = [u for u in db.list_users(limit=1_000_000) if not u["is_banned"]]

    with _broadcast_lock:
        _broadcast_status.update(running=True, sent=0, failed=0, total=len(users), last_text=text)

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for u in users:
        try:
            resp = requests.post(
                url,
                data={"chat_id": u["user_id"], "text": text},
                timeout=10,
            )
            ok = resp.ok and resp.json().get("ok")
        except Exception:
            ok = False

        with _broadcast_lock:
            if ok:
                _broadcast_status["sent"] += 1
            else:
                _broadcast_status["failed"] += 1

        time.sleep(BROADCAST_DELAY_SECONDS)

    with _broadcast_lock:
        _broadcast_status["running"] = False


def _check_auth(username: str, password: str) -> bool:
    return username == ADMIN_USER and password == ADMIN_PASS


def _authenticate():
    return Response(
        "Требуется авторизация", 401,
        {"WWW-Authenticate": 'Basic realm="Admin Panel"'},
    )


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return _authenticate()
        return f(*args, **kwargs)
    return decorated


PAGE_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Админ-панель бота</title>
  <style>
    body {{ font-family: -apple-system, Arial, sans-serif; background:#0f1115; color:#e6e6e6;
            margin:0; padding:24px; }}
    h1 {{ font-size: 20px; }}
    h2 {{ font-size: 16px; color:#c7cbd1; margin-top:32px; }}
    .cards {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:8px; }}
    .card {{ background:#1a1d24; border-radius:10px; padding:16px 20px; min-width:140px; }}
    .card .num {{ font-size:28px; font-weight:700; }}
    .card .label {{ color:#9aa0aa; font-size:13px; }}
    table {{ width:100%; border-collapse: collapse; margin-bottom:16px; font-size:14px; }}
    th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #262a33; }}
    th {{ color:#9aa0aa; font-weight:600; }}
    .ok {{ color:#4caf7d; }}
    .error {{ color:#e5636b; }}
    form.inline {{ display:inline-block; margin-right:12px; }}
    input[type=text] {{ background:#1a1d24; border:1px solid #333; color:#eee;
                         padding:6px 8px; border-radius:6px; }}
    button {{ background:#2f6fed; color:white; border:none; padding:6px 12px;
              border-radius:6px; cursor:pointer; }}
    button.danger {{ background:#b3313b; }}
  </style>
</head>
<body>
  <h1>📊 Админ-панель Telegram-бота</h1>
  <div class="cards">
    <div class="card"><div class="num">{total_users}</div><div class="label">Пользователей</div></div>
    <div class="card"><div class="num">{total_downloads}</div><div class="label">Скачано успешно</div></div>
    <div class="card"><div class="num">{failed_downloads}</div><div class="label">Ошибок/отказов</div></div>
    <div class="card"><div class="num">{banned_users}</div><div class="label">В бане</div></div>
  </div>

  <h2>По платформам</h2>
  <table>
    <tr><th>Платформа</th><th>Скачано</th></tr>
    {platform_rows}
  </table>

  <h2>📢 Рассылка всем пользователям (не забаненным)</h2>
  <form method="post" action="/broadcast">
    <textarea name="message" rows="4" style="width:100%; max-width:520px; background:#1a1d24;
      border:1px solid #333; color:#eee; padding:8px; border-radius:6px; font-family:inherit;"
      placeholder="Текст сообщения..." {disabled_attr}>{last_text}</textarea><br><br>
    <button type="submit" {disabled_attr}>Отправить всем</button>
  </form>
  <p style="color:#9aa0aa; font-size:13px;">{broadcast_status_line}</p>

  <h2>🔒 Обязательная подписка на канал</h2>
  <p style="color:#9aa0aa; font-size:13px; max-width:520px;">
    Если поле заполнено — бот не даст скачивать, пока пользователь не подпишется
    на указанный канал. Укажи юзернейм вида <code>@channel</code> или пусто, чтобы
    отключить проверку. Бота нужно добавить в канал администратором, иначе
    проверка подписки не сработает.
  </p>
  <form method="post" action="/settings">
    <input type="text" name="required_channel" value="{required_channel}" placeholder="@channel">
    <button type="submit">Сохранить</button>
  </form>

  <h2>Забанить / разбанить пользователя</h2>
  <form class="inline" method="post" action="/ban">
    <input type="text" name="user_id" placeholder="ID пользователя">
    <button class="danger" type="submit">Забанить</button>
  </form>
  <form class="inline" method="post" action="/unban">
    <input type="text" name="user_id" placeholder="ID пользователя">
    <button type="submit">Разбанить</button>
  </form>

  <h2>Последние загрузки</h2>
  <table>
    <tr><th>Время (UTC)</th><th>Пользователь</th><th>Платформа</th><th>Качество</th><th>Статус</th></tr>
    {download_rows}
  </table>
</body>
</html>
"""


@app.route("/health")
def health():
    # Публичный эндпоинт для healthcheck на Railway — без авторизации.
    return {"status": "ok"}


@app.route("/", methods=["GET"])
@requires_auth
def dashboard():
    stats = db.get_stats()
    platform_rows = "".join(
        f"<tr><td>{p}</td><td>{c}</td></tr>" for p, c in stats["by_platform"]
    ) or "<tr><td colspan=2>Пока нет данных</td></tr>"

    downloads = db.recent_downloads(50)
    rows_html = []
    for d in downloads:
        ts = d["created_at"].replace("T", " ")[:19]
        user_label = d.get("username") or d["user_id"]
        status_cls = "ok" if d["status"] == "ok" else "error"
        rows_html.append(
            f"<tr><td>{ts}</td><td>{user_label}</td><td>{d['platform']}</td>"
            f"<td>{d['quality']}</td><td class='{status_cls}'>{d['status']}</td></tr>"
        )
    download_rows = "".join(rows_html) or "<tr><td colspan=5>Пока нет данных</td></tr>"

    with _broadcast_lock:
        bs = dict(_broadcast_status)

    if bs["running"]:
        status_line = f"⏳ Рассылка идёт: отправлено {bs['sent']} из {bs['total']}, ошибок {bs['failed']}."
        disabled_attr = "disabled"
    elif bs["total"]:
        status_line = f"Последняя рассылка: отправлено {bs['sent']} из {bs['total']}, ошибок {bs['failed']}."
        disabled_attr = ""
    else:
        status_line = "Рассылок ещё не было."
        disabled_attr = ""

    required_channel = db.get_setting("required_channel", "") or ""

    html = PAGE_TEMPLATE.format(
        total_users=stats["total_users"],
        total_downloads=stats["total_downloads"],
        failed_downloads=stats["failed_downloads"],
        banned_users=stats["banned_users"],
        platform_rows=platform_rows,
        download_rows=download_rows,
        broadcast_status_line=status_line,
        disabled_attr=disabled_attr,
        last_text="",
        required_channel=required_channel,
    )
    return html


@app.route("/broadcast", methods=["POST"])
@requires_auth
def broadcast():
    text = request.form.get("message", "").strip()
    with _broadcast_lock:
        already_running = _broadcast_status["running"]
    if text and not already_running:
        threading.Thread(target=_send_broadcast, args=(text,), daemon=True).start()
    return redirect(url_for("dashboard"))


@app.route("/broadcast/status")
@requires_auth
def broadcast_status():
    with _broadcast_lock:
        return dict(_broadcast_status)


@app.route("/settings", methods=["POST"])
@requires_auth
def settings():
    channel = request.form.get("required_channel", "").strip()
    db.set_setting("required_channel", channel)
    return redirect(url_for("dashboard"))


@app.route("/ban", methods=["POST"])
@requires_auth
def ban():
    user_id = request.form.get("user_id", "").strip()
    if user_id.lstrip("-").isdigit():
        db.set_banned(int(user_id), True)
    return redirect(url_for("dashboard"))


@app.route("/unban", methods=["POST"])
@requires_auth
def unban():
    user_id = request.form.get("user_id", "").strip()
    if user_id.lstrip("-").isdigit():
        db.set_banned(int(user_id), False)
    return redirect(url_for("dashboard"))


def _run():
    app.run(host="0.0.0.0", port=PORT)


def run_admin_panel_in_thread():
    t = threading.Thread(target=_run, daemon=True, name="admin-panel")
    t.start()
