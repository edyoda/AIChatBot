"""Idempotency state — remembers when each conversation was last logged to the CRM.

Stored in the bot's own chat:meta:<waId> hash as `last_crm_logged_ts`, so a
conversation is logged once per new activity (a resumed chat logs again with a
fresh note; a re-run within the same activity is a no-op).
"""
from __future__ import annotations

FIELD = "last_crm_logged_ts"


def connect(redis_url: str):
    import redis
    return redis.Redis.from_url(redis_url, decode_responses=True)


def get_last_logged(r, wa_id: str) -> int:
    try:
        return int(r.hget(f"chat:meta:{wa_id}", FIELD) or 0)
    except (TypeError, ValueError):
        return 0


def mark_logged(r, wa_id: str, ts: int) -> None:
    r.hset(f"chat:meta:{wa_id}", FIELD, int(ts))
