"""Qualification — decide whether a conversation should be logged to the CRM.

Three gates (all must pass): settled, worth-a-call, and new-since-last-log.
Returns (ok, reason) so dry-run can explain every skip.
"""
from __future__ import annotations

import time
from typing import Optional

from .config import Config
from .source import Conversation

# Signals that a conversation is a real prospect worth a call (not just a greeting).
INTENT_KEYWORDS = (
    "price", "cost", "fee", "fees", "emi", "discount", "offer",
    "batch", "start", "date", "cohort", "schedule", "timing", "timings",
    "enroll", "enrol", "admission", "join", "register",
    "course", "program", "curriculum", "syllabus", "certificate", "placement", "job",
    "experience", "years", "background", "eligibility", "prerequisite",
    "demo", "recorded", "live", "duration", "refund",
)


def _last10(wa_id: str) -> str:
    d = "".join(ch for ch in wa_id if ch.isdigit())
    return d[-10:] if len(d) >= 10 else d


def qualify(conv: Conversation, cfg: Config, last_logged_ts: int = 0,
            now: Optional[int] = None) -> tuple[bool, str]:
    now = int(now if now is not None else time.time())

    # skip internal / test numbers
    if _last10(conv.wa_id) in {n[-10:] for n in cfg.skip_numbers}:
        return False, "skip-number"

    # Gate 1: settled (idle long enough that the exchange is complete)
    if conv.last_user_ts and (now - conv.last_user_ts) < cfg.idle_seconds:
        return False, f"not-settled ({now - conv.last_user_ts}s < {cfg.idle_seconds}s)"

    # Gate 3: new since last log (append a fresh note only if the user spoke again)
    if last_logged_ts and conv.last_user_ts and conv.last_user_ts <= last_logged_ts:
        return False, "already-logged"

    # Gate 2: worth a call
    users = conv.user_messages()
    substantive = [u for u in users if u.strip()]
    if len(substantive) < cfg.min_user_msgs:
        return False, f"too-few-user-msgs ({len(substantive)} < {cfg.min_user_msgs})"
    blob = " ".join(users).lower()
    if not any(kw in blob for kw in INTENT_KEYWORDS):
        return False, "no-intent-signal"

    return True, "ok"
