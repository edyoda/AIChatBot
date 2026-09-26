"""Conversation source — reads from the bot's Redis or a JSON fixture.

Redis layout (written by app/chat_history.py):
  chat:session:<waId>  -> list of JSON messages ({"role","content"})
  chat:meta:<waId>     -> hash: last_user_ts, last_assistant_ts, channel_phone_number, ...
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterator, Optional

log = logging.getLogger("crm_notes.source")


@dataclass
class Conversation:
    wa_id: str
    meta: dict
    messages: list[dict]  # [{"role": "user"|"assistant", "content": str}]

    @property
    def last_user_ts(self) -> int:
        try:
            return int(self.meta.get("last_user_ts") or 0)
        except (TypeError, ValueError):
            return 0

    def user_messages(self) -> list[str]:
        return [m["content"] for m in self.messages if m.get("role") == "user" and m.get("content")]

    def transcript(self, max_msgs: int = 40) -> str:
        lines = []
        for m in self.messages[-max_msgs:]:
            who = "Customer" if m.get("role") == "user" else "Bot"
            lines.append(f"{who}: {str(m.get('content', '')).strip()}")
        return "\n".join(lines)


def _parse_message(raw: str) -> Optional[dict]:
    try:
        d = json.loads(raw)
    except Exception:
        return {"role": "?", "content": raw}
    content = d.get("content", "")
    if isinstance(content, list):  # rich content blocks -> flatten to text
        content = " ".join(str(c.get("text", c)) for c in content if isinstance(c, dict)) or str(content)
    return {"role": d.get("role", "?"), "content": content}


def from_fixture(path: str) -> list[Conversation]:
    """Load conversations exported to JSON (see fixtures/conversations_sample.json)."""
    data = json.load(open(path, encoding="utf-8"))
    out = []
    for c in data:
        msgs = [_parse_message(m) for m in c.get("messages", [])]
        out.append(Conversation(wa_id=c["waId"], meta=c.get("meta", {}), messages=[m for m in msgs if m]))
    return out


def from_redis(redis_url: str, limit: int = 500) -> Iterator[Conversation]:
    """Iterate conversations from live Redis, most-recently-active first."""
    import redis
    r = redis.Redis.from_url(redis_url, decode_responses=True)
    metas: list[tuple[int, str]] = []
    for k in r.scan_iter(match="chat:meta:*", count=2000):
        ts = r.hget(k, "last_user_ts")
        if ts:
            try:
                metas.append((int(ts), k.split(":")[-1]))
            except (TypeError, ValueError):
                pass
    metas.sort(reverse=True)
    for _, wa in metas[:limit]:
        meta = r.hgetall(f"chat:meta:{wa}")
        raw = r.lrange(f"chat:session:{wa}", 0, -1)
        msgs = [m for m in (_parse_message(x) for x in raw) if m]
        yield Conversation(wa_id=wa, meta=meta, messages=msgs)
