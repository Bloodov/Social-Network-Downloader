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
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # лимит загрузки фото — 10 МБ

# ---------------- Рассылка сообщений всем пользователям ----------------
# Отправка идёт напрямую через Telegram Bot HTTP API (без python-telegram-bot),
# чтобы не зависеть от asyncio-цикла бота, который крутится в другом потоке.

_broadcast_status = {"running": False, "sent": 0, "failed": 0, "total": 0, "last_text": ""}
_broadcast_lock = threading.Lock()

# Пауза между сообщениями — держим ниже общего лимита Telegram (~30 сообщений/сек),
# чтобы не словить 429 Too Many Requests при большой базе пользователей.
BROADCAST_DELAY_SECONDS = float(os.environ.get("BROADCAST_DELAY_SECONDS", "0.05"))

# Лимит подписи к фото в Telegram
CAPTION_LIMIT = 1024


def _send_broadcast(text: str, photo_bytes: bytes | None, photo_filename: str | None):
    users = [u for u in db.list_users(limit=1_000_000) if not u["is_banned"]]

    with _broadcast_lock:
        _broadcast_status.update(running=True, sent=0, failed=0, total=len(users), last_text=text)

    send_photo_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    send_message_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    # Если текст короткий — он идёт подписью к фото; если длинный — фото без подписи
    # плюс отдельное сообщение с текстом сразу следом (у подписи лимит 1024 символа).
    caption = text if (photo_bytes and text and len(text) <= CAPTION_LIMIT) else None
    trailing_text = text if (photo_bytes and text and len(text) > CAPTION_LIMIT) else None

    # После первой успешной отправки фото Telegram отдаёт file_id — дальше
    # рассылаем по file_id, без повторной загрузки файла на каждого пользователя.
    file_id = None

    for u in users:
        try:
            ok = True
            if photo_bytes:
                data = {"chat_id": u["user_id"]}
                if caption:
                    data["caption"] = caption
                    data["parse_mode"] = "HTML"

                if file_id:
                    data["photo"] = file_id
                    resp = requests.post(send_photo_url, data=data, timeout=15)
                else:
                    resp = requests.post(
                        send_photo_url,
                        data=data,
                        files={"photo": (photo_filename or "photo.jpg", photo_bytes)},
                        timeout=30,
                    )

                payload = resp.json()
                ok = resp.ok and payload.get("ok")
                if ok and not file_id:
                    sizes = (payload.get("result") or {}).get("photo") or []
                    if sizes:
                        file_id = sizes[-1]["file_id"]

                if ok and trailing_text:
                    resp2 = requests.post(
                        send_message_url,
                        data={"chat_id": u["user_id"], "text": trailing_text, "parse_mode": "HTML"},
                        timeout=10,
                    )
                    ok = ok and resp2.ok and resp2.json().get("ok")
            else:
                resp = requests.post(
                    send_message_url,
                    data={"chat_id": u["user_id"], "text": text, "parse_mode": "HTML"},
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
    .toolbar {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:8px; max-width:520px; }}
    .toolbar button {{ background:#1a1d24; border:1px solid #333; color:#eee;
                        min-width:36px; padding:6px 10px; font-size:14px; }}
    .toolbar button:hover {{ background:#262a33; }}
    .rich-editor {{ width:100%; max-width:520px; min-height:120px; background:#1a1d24;
                     border:1px solid #333; color:#eee; padding:10px; border-radius:6px;
                     font-family:inherit; font-size:14px; line-height:1.5; white-space:pre-wrap; }}
    .rich-editor:empty:before {{ content: attr(data-placeholder); color:#666; }}
    .rich-editor b {{ font-weight:700; }}
    .rich-editor .tg-spoiler {{ background:#333; border-radius:3px; padding:0 2px; }}
    .rich-editor code {{ background:#0f1115; padding:1px 4px; border-radius:4px; font-family:monospace; }}
    .rich-editor pre {{ background:#0f1115; padding:8px; border-radius:6px; overflow-x:auto;
                         font-family:monospace; }}
    .rich-editor blockquote {{ border-left:3px solid #2f6fed; margin:4px 0; padding-left:10px;
                                color:#c7cbd1; }}
    .rich-editor a {{ color:#6ea8fe; }}
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
  <form method="post" action="/broadcast" enctype="multipart/form-data" id="broadcastForm">
    <div class="toolbar" id="toolbar">
      <button type="button" data-cmd="bold" title="Жирный"><b>B</b></button>
      <button type="button" data-cmd="italic" title="Курсив"><i>I</i></button>
      <button type="button" data-cmd="underline" title="Подчёркнутый"><u>U</u></button>
      <button type="button" data-cmd="strike" title="Зачёркнутый"><s>S</s></button>
      <button type="button" data-cmd="spoiler" title="Спойлер">🙈</button>
      <button type="button" data-cmd="code" title="Код (строка)">&lt;/&gt;</button>
      <button type="button" data-cmd="pre" title="Блок кода">{{ }}</button>
      <button type="button" data-cmd="quote" title="Цитата">❝</button>
      <button type="button" data-cmd="link" title="Ссылка">🔗</button>
    </div>
    <div id="richEditor" class="rich-editor" contenteditable="true"
         data-placeholder="Текст сообщения... выдели текст и нажми кнопку форматирования"
         {disabled_attr}></div>
    <input type="hidden" name="message" id="messageHidden">
    <br>
    <label style="color:#9aa0aa; font-size:13px;">Фото (необязательно):</label><br>
    <input type="file" name="photo" accept="image/*" {disabled_attr}><br><br>
    <button type="submit" {disabled_attr}>Отправить всем</button>
    <p style="color:#9aa0aa; font-size:12px; max-width:520px; line-height:1.5;">
      Выдели текст в поле выше и нажми кнопку на панели, чтобы применить форматирование
      Telegram (жирный, курсив, подчёркнутый, зачёркнутый, спойлер, код, блок кода, цитата, ссылка).
      Если прикреплено фото — текст станет подписью к нему (лимит подписи — 1024 символа,
      более длинный текст придёт отдельным сообщением сразу после фото).
    </p>
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

  <script>
    (function() {{
      var editor = document.getElementById('richEditor');
      var toolbar = document.getElementById('toolbar');
      var form = document.getElementById('broadcastForm');
      var hidden = document.getElementById('messageHidden');
      if (!editor || !toolbar || !form) return;

      function wrapSelection(tagName, attrs) {{
        var sel = window.getSelection();
        if (!sel.rangeCount || sel.isCollapsed) return;
        var range = sel.getRangeAt(0);
        var el = document.createElement(tagName);
        if (attrs) {{
          Object.keys(attrs).forEach(function(k) {{ el.setAttribute(k, attrs[k]); }});
        }}
        try {{
          range.surroundContents(el);
        }} catch (e) {{
          var content = range.extractContents();
          el.appendChild(content);
          range.insertNode(el);
        }}
        sel.removeAllRanges();
      }}

      toolbar.addEventListener('mousedown', function(e) {{
        // Не даём полю потерять фокус/выделение при клике по кнопке
        e.preventDefault();
      }});

      toolbar.addEventListener('click', function(e) {{
        var btn = e.target.closest('button[data-cmd]');
        if (!btn) return;
        editor.focus();
        var cmd = btn.getAttribute('data-cmd');
        switch (cmd) {{
          case 'bold': document.execCommand('bold'); break;
          case 'italic': document.execCommand('italic'); break;
          case 'underline': document.execCommand('underline'); break;
          case 'strike': document.execCommand('strikeThrough'); break;
          case 'spoiler': wrapSelection('span', {{'class': 'tg-spoiler'}}); break;
          case 'code': wrapSelection('code'); break;
          case 'pre': wrapSelection('pre'); break;
          case 'quote': wrapSelection('blockquote'); break;
          case 'link':
            var url = window.prompt('Ссылка (https://...)');
            if (url) wrapSelection('a', {{'href': url}});
            break;
        }}
      }});

      function escapeHtml(text) {{
        return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      }}

      // Строим результат построчно: инлайновые элементы (b, i, a...) добавляются
      // к текущей строке, а <div>/<p>/<br> всегда начинают новую строку.
      // Это нужно, чтобы не терять перенос строки между первой строкой (она в
      // contenteditable лежит как обычный текстовый узел без обёртки) и
      // следующими строками, которые браузер оборачивает в <div> после Enter.
      function editorToTelegramHtml() {{
        var lines = [''];

        function appendToCurrentLine(str) {{
          lines[lines.length - 1] += str;
        }}

        function newLineIfNeeded() {{
          if (lines[lines.length - 1] !== '') lines.push('');
        }}

        function walk(node) {{
          if (node.nodeType === Node.TEXT_NODE) {{
            appendToCurrentLine(escapeHtml(node.textContent));
            return;
          }}
          if (node.nodeType !== Node.ELEMENT_NODE) return;

          var tag = node.tagName.toLowerCase();

          if (tag === 'br') {{
            lines.push('');
            return;
          }}
          if (tag === 'div' || tag === 'p') {{
            newLineIfNeeded();
            Array.prototype.forEach.call(node.childNodes, walk);
            newLineIfNeeded();
            return;
          }}

          var open = '', close = '';
          switch (tag) {{
            case 'b': case 'strong': open = '<b>'; close = '</b>'; break;
            case 'i': case 'em': open = '<i>'; close = '</i>'; break;
            case 'u': open = '<u>'; close = '</u>'; break;
            case 's': case 'strike': case 'del': open = '<s>'; close = '</s>'; break;
            case 'code': open = '<code>'; close = '</code>'; break;
            case 'pre': open = '<pre>'; close = '</pre>'; break;
            case 'blockquote': open = '<blockquote>'; close = '</blockquote>'; break;
            case 'a':
              var href = node.getAttribute('href') || '';
              open = '<a href="' + escapeHtml(href) + '">';
              close = '</a>';
              break;
            case 'span':
              if (node.classList.contains('tg-spoiler')) {{
                open = '<span class="tg-spoiler">';
                close = '</span>';
              }}
              break;
          }}

          appendToCurrentLine(open);
          Array.prototype.forEach.call(node.childNodes, walk);
          appendToCurrentLine(close);
        }}

        Array.prototype.forEach.call(editor.childNodes, walk);

        while (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
        return lines.join('\\n');
      }}

      form.addEventListener('submit', function() {{
        hidden.value = editorToTelegramHtml();
      }});
    }})();
  </script>
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

    photo_bytes = None
    photo_filename = None
    photo_file = request.files.get("photo")
    if photo_file and photo_file.filename:
        photo_bytes = photo_file.read()
        photo_filename = photo_file.filename

    with _broadcast_lock:
        already_running = _broadcast_status["running"]

    if (text or photo_bytes) and not already_running:
        threading.Thread(
            target=_send_broadcast, args=(text, photo_bytes, photo_filename), daemon=True
        ).start()
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
