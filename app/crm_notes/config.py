"""Config for the CRM-notes feature — all env-driven, safe defaults.

Nothing here enables writes on its own: the background loop is gated by
CRM_NOTES_ENABLED (default off), and the runner defaults to dry-run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "").strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


@dataclass
class Config:
    # feature gate for the in-process background loop (main.py startup)
    enabled: bool = field(default_factory=lambda: _bool("CRM_NOTES_ENABLED", False))
    # data source
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    # summarizer (OpenRouter)
    openrouter_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    summary_model: str = field(default_factory=lambda: os.getenv("CRM_SUMMARY_MODEL", "meta-llama/llama-3.1-8b-instruct"))
    # CRM provider selection
    crm_provider: str = field(default_factory=lambda: os.getenv("CRM_PROVIDER", "zoho"))
    # qualification gates
    idle_seconds: int = field(default_factory=lambda: _int("CRM_IDLE_SECONDS", 20 * 60))
    min_user_msgs: int = field(default_factory=lambda: _int("CRM_MIN_USER_MSGS", 2))
    # run bounds / safety
    max_per_run: int = field(default_factory=lambda: _int("CRM_MAX_PER_RUN", 50))
    loop_interval_seconds: int = field(default_factory=lambda: _int("CRM_LOOP_INTERVAL", 60 * 60))
    # numbers to never log (EdYoda's own lines / test numbers), comma-separated
    skip_numbers: frozenset = field(default_factory=lambda: frozenset(
        n.strip() for n in os.getenv("CRM_SKIP_NUMBERS", "8045682485,8904512659,9663357054").split(",") if n.strip()
    ))
    # ── dedup job (dedup.py) ──────────────────────────────────────────────────
    dedup_enabled: bool = field(default_factory=lambda: _bool("CRM_DEDUP_ENABLED", False))
    dedup_window_hours: int = field(default_factory=lambda: _int("CRM_DEDUP_WINDOW_HOURS", 24))
    # daily run times in IST (Asia/Kolkata), comma-separated HH:MM
    dedup_times: tuple = field(default_factory=lambda: tuple(
        t.strip() for t in os.getenv("CRM_DEDUP_TIMES", "08:00,17:00").split(",") if t.strip()))
    dedup_rollback_dir: str = field(default_factory=lambda: os.getenv("CRM_DEDUP_ROLLBACK_DIR", "dedup_rollback"))
    # Safety cap: skip (and log for human review) any cluster larger than this — a
    # backstop against an over-match (bad phone/shared email) retiring many leads.
    dedup_max_cluster: int = field(default_factory=lambda: _int("CRM_DEDUP_MAX_CLUSTER", 25))


def load() -> Config:
    return Config()
