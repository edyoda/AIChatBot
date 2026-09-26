"""
SQLite-backed conversation log for the EdYoda chatbot.
One row per exchange (user message + bot reply).
"""
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "chatbot_logs.db"

_db_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock:
        conn = _conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chatbot_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                waid TEXT NOT NULL,
                user_message TEXT,
                image_url TEXT,
                bot_reply TEXT,
                is_deflected INTEGER DEFAULT 0,
                timestamp TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_waid ON chatbot_logs(waid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON chatbot_logs(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deflected ON chatbot_logs(is_deflected)")
        conn.commit()
        conn.close()


def log_conversation(
    waid: str,
    user_message: str,
    bot_reply: str,
    is_deflected: bool = False,
    image_url: str | None = None,
) -> None:
    try:
        ts = datetime.now(tz=timezone.utc).isoformat()
        with _db_lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO chatbot_logs (waid, user_message, image_url, bot_reply, is_deflected, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (waid, user_message, image_url, bot_reply, int(is_deflected), ts),
            )
            conn.commit()
            conn.close()
    except Exception:
        pass


def get_all_users(deflected_only: bool = False) -> list[dict]:
    with _db_lock:
        conn = _conn()
        if deflected_only:
            rows = conn.execute("""
                SELECT waid, MAX(timestamp) as last_ts, COUNT(*) as total,
                       SUM(is_deflected) as deflected_count
                FROM chatbot_logs
                WHERE waid IN (SELECT DISTINCT waid FROM chatbot_logs WHERE is_deflected = 1)
                GROUP BY waid
                ORDER BY last_ts DESC
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT waid, MAX(timestamp) as last_ts, COUNT(*) as total,
                       SUM(is_deflected) as deflected_count
                FROM chatbot_logs
                GROUP BY waid
                ORDER BY last_ts DESC
            """).fetchall()
        conn.close()
    return [
        {
            "waid": r["waid"],
            "last_ts": r["last_ts"],
            "total": r["total"],
            "deflected_count": r["deflected_count"] or 0,
        }
        for r in rows
    ]


def get_conversation(waid: str) -> list[dict]:
    with _db_lock:
        conn = _conn()
        rows = conn.execute("""
            SELECT id, user_message, image_url, bot_reply, is_deflected, timestamp
            FROM chatbot_logs
            WHERE waid = ?
            ORDER BY timestamp ASC
        """, (waid,)).fetchall()
        conn.close()
    return [
        {
            "id": r["id"],
            "user_message": r["user_message"],
            "image_url": r["image_url"],
            "bot_reply": r["bot_reply"],
            "is_deflected": bool(r["is_deflected"]),
            "timestamp": r["timestamp"],
        }
        for r in rows
    ]
