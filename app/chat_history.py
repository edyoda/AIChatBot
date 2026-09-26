"""
Redis-backed chat history. Session state lives in Redis (shared, bounded, survives restarts).
Uses REDIS_URL; keeps last N messages per session and optional TTL for inactive sessions.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional
from datetime import datetime, timezone

from pydantic_settings import BaseSettings, SettingsConfigDict

import redis

REDIS_KEY_PREFIX = "chat:session:"
MAX_MESSAGES_PER_SESSION = 20
SESSION_TTL_DAYS = 30  # expire key after N days of no updates (optional)
META_KEY_PREFIX = "chat:meta:"

_redis_client: Optional[redis.Redis] = None

logger = logging.getLogger("chat_history_redis")

class Settings(BaseSettings):
    # Load `.env` reliably regardless of the current working directory.
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[1] / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )
    redis_url: str = ""


settings = Settings()


def _get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        # Prefer runtime env var, fall back to `.env` (pydantic-settings).
        url = os.getenv("REDIS_URL") or settings.redis_url
        if not url:
            raise RuntimeError(
                "REDIS_URL is required (e.g. redis://localhost:6379/0 or redis://:password@host:6379/0)"
            )
        parsed = urlparse(url)
        # redact password in logs
        safe_url = url
        if parsed.password:
            # reconstruct without password
            safe_url = f"{parsed.scheme}://{parsed.username}:***@{parsed.hostname}:{parsed.port or ''}/{parsed.path.lstrip('/')}"
        _redis_client = redis.from_url(url, decode_responses=True)
        try:
            _redis_client.ping()
            logger.info(
                "Redis connected. REDIS_URL(host=%s port=%s db=%s)",
                parsed.hostname,
                parsed.port,
                (parsed.path.lstrip("/") if parsed.path else ""),
            )
            logger.info("Redis key prefix: %s", REDIS_KEY_PREFIX)
        except Exception as e:
            logger.exception("Redis ping failed (url=%s): %s", safe_url, str(e))
            raise
    return _redis_client


def get_recent_messages(session_id: str, limit: int = MAX_MESSAGES_PER_SESSION) -> list[dict]:
    """Return the last `limit` messages for this session, chronological order. Reads from Redis."""
    r = _get_redis()
    key = f"{REDIS_KEY_PREFIX}{session_id}"
    logger.info("Redis get_recent_messages session_id=%s limit=%s key=%s", session_id, limit, key)
    # LIST: we RPUSH (append), so LRANGE 0 -1 is chronological; keep last `limit` by taking from right
    raw = r.lrange(key, -limit, -1) if limit else r.lrange(key, 0, -1)
    out = []
    for s in raw:
        try:
            out.append(json.loads(s))
        except (json.JSONDecodeError, TypeError):
            continue
    logger.info("Redis fetched %s messages for session_id=%s", len(out), session_id)
    return out


def append_messages(
    session_id: str,
    user_content: str,
    assistant_content: str,
    keep_last: int = MAX_MESSAGES_PER_SESSION,
) -> None:
    """Append one user and one assistant message in Redis, then trim to last keep_last. Sets TTL on key."""
    r = _get_redis()
    key = f"{REDIS_KEY_PREFIX}{session_id}"
    meta_key = f"{META_KEY_PREFIX}{session_id}"
    now_ts = int(time.time())
    logger.info(
        "Redis append_messages session_id=%s keep_last=%s key=%s",
        session_id,
        keep_last,
        key,
    )
    pipe = r.pipeline()
    pipe.rpush(key, json.dumps({"role": "user", "content": user_content, "ts": now_ts}))
    pipe.rpush(key, json.dumps({"role": "assistant", "content": assistant_content, "ts": now_ts}))
    pipe.ltrim(key, -keep_last, -1)
    if SESSION_TTL_DAYS > 0:
        pipe.expire(key, SESSION_TTL_DAYS * 24 * 3600)
        pipe.expire(meta_key, SESSION_TTL_DAYS * 24 * 3600)
    # Track last assistant activity in META hash.
    # Important: do NOT overwrite last_user_ts here; that is only set on inbound user activity.
    pipe.hset(meta_key, mapping={"last_assistant_ts": now_ts})
    pipe.llen(key)
    pipe.execute()

    # LLen is last result in pipeline results; fetch it again for safe logging.
    try:
        new_len = r.llen(key)
        logger.info("Redis new length for session_id=%s is %s", session_id, new_len)
    except Exception as e:
        logger.debug("Redis llen failed for session_id=%s: %s", session_id, str(e))


def update_user_activity(session_id: str, channel_phone_number: Optional[str] = None) -> None:
    """
    Update META hash for this session when an inbound user message is received.
    Also clears 'reassigned_to_bot' so future inactivity can trigger reassignment again.
    """
    try:
        r = _get_redis()
        meta_key = f"{META_KEY_PREFIX}{session_id}"
        now_ts = int(time.time())
        mapping: dict[str, str | int] = {
            "last_user_ts": now_ts,
            "reassigned_to_bot": 0,
        }
        if channel_phone_number:
            mapping["channel_phone_number"] = channel_phone_number
        r.hset(meta_key, mapping=mapping)
        if SESSION_TTL_DAYS > 0:
            r.expire(meta_key, SESSION_TTL_DAYS * 24 * 3600)
        iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()
        logger.info(
            "user activity updated session_id=%s ts=%s (%s) channel=%s",
            session_id,
            now_ts,
            iso,
            channel_phone_number,
        )
    except Exception as e:
        logger.debug("update_user_activity failed for session_id=%s: %s", session_id, str(e))


def get_session_meta(session_id: str) -> dict:
    """Read META hash for a session."""
    try:
        r = _get_redis()
        meta_key = f"{META_KEY_PREFIX}{session_id}"
        data = r.hgetall(meta_key) or {}
        return data
    except Exception:
        return {}


def set_meta_fields(session_id: str, mapping: dict) -> None:
    """Patch arbitrary fields into the session META hash (does not touch timestamps)."""
    try:
        r = _get_redis()
        meta_key = f"{META_KEY_PREFIX}{session_id}"
        r.hset(meta_key, mapping=mapping)
        if SESSION_TTL_DAYS > 0:
            r.expire(meta_key, SESSION_TTL_DAYS * 24 * 3600)
    except Exception as e:
        logger.debug("set_meta_fields failed for session_id=%s: %s", session_id, str(e))


def mark_reassigned_to_bot(session_id: str) -> None:
    """Set reassigned_to_bot=1 with timestamp."""
    try:
        r = _get_redis()
        meta_key = f"{META_KEY_PREFIX}{session_id}"
        now_ts = int(time.time())
        r.hset(meta_key, mapping={"reassigned_to_bot": 1, "reassigned_at": now_ts})
        if SESSION_TTL_DAYS > 0:
            r.expire(meta_key, SESSION_TTL_DAYS * 24 * 3600)
    except Exception as e:
        logger.debug("mark_reassigned_to_bot failed for session_id=%s: %s", session_id, str(e))
