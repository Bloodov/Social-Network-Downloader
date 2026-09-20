"""
Простое хранилище на SQLite: пользователи, история загрузок, баны.

Используется и ботом (проверка бана, лог загрузок), и веб-панелью админа
(статистика, бан/разбан).
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "bot.db")

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_conn():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen TEXT,
                is_banned INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                platform TEXT,
                url TEXT,
                quality TEXT,
                status TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        # Добавляем колонку language, если база создана более старой версией бота.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "language" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN language TEXT")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def touch_user(user_id: int, username: str | None):
    with get_conn() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (user_id, username, first_seen, is_banned) VALUES (?, ?, ?, 0)",
                (user_id, username, _now()),
            )
        else:
            conn.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))


def is_banned(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row["is_banned"])


def get_language(user_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT language FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["language"] if row else None


def set_language(user_id: int, language: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET language=? WHERE user_id=?", (language, user_id))


def get_setting(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def set_banned(user_id: int, banned: bool):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, user_id))


def log_download(user_id: int, platform: str, url: str, quality: str, status: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO downloads (user_id, platform, url, quality, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, platform, url, quality, status, _now()),
        )


def get_stats() -> dict:
    with get_conn() as conn:
        total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        total_downloads = conn.execute(
            "SELECT COUNT(*) c FROM downloads WHERE status='ok'"
        ).fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) c FROM downloads WHERE status!='ok'"
        ).fetchone()["c"]
        by_platform = conn.execute(
            "SELECT platform, COUNT(*) c FROM downloads WHERE status='ok' "
            "GROUP BY platform ORDER BY c DESC"
        ).fetchall()
        banned = conn.execute("SELECT COUNT(*) c FROM users WHERE is_banned=1").fetchone()["c"]
        return {
            "total_users": total_users,
            "total_downloads": total_downloads,
            "failed_downloads": failed,
            "by_platform": [(r["platform"], r["c"]) for r in by_platform],
            "banned_users": banned,
        }


def recent_downloads(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT d.*, u.username FROM downloads d LEFT JOIN users u ON u.user_id = d.user_id "
            "ORDER BY d.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_users(limit: int = 200) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY first_seen DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
