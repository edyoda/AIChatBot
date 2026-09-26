"""Batch service — the reusable core used by the CLI runner, the background loop,
and the admin trigger. Blocking (httpx/Zoho); callers run it in a thread.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from . import config as _config
from . import source as _source
from . import state as _state
from .crm.base import get_provider
from .orchestrator import process
from .qualify import qualify
from .summarize import OpenRouterSummarizer

log = logging.getLogger("crm_notes.service")


def process_conversations(convs: Iterable, *, cfg, provider, summarizer,
                          dry_run: bool, redis_client=None, limit: int) -> dict:
    totals = {"scanned": 0, "qualified": 0, "matched": 0, "created": 0, "error": 0, "skipped": 0}
    logged = 0
    for c in convs:
        totals["scanned"] += 1
        if redis_client is not None:
            last = _state.get_last_logged(redis_client, c.wa_id)
        else:
            try:
                last = int(c.meta.get("last_crm_logged_ts") or 0)
            except (TypeError, ValueError):
                last = 0
        ok, reason = qualify(c, cfg, last_logged_ts=last)
        if not ok:
            totals["skipped"] += 1
            log.info("SKIP  %s (%d msgs) [%s]", c.wa_id, len(c.messages), reason)
            continue
        if logged >= limit:
            log.info("HOLD  %s [limit=%d reached]", c.wa_id, limit)
            break
        totals["qualified"] += 1
        r = process(c, provider, summarizer, dry_run=dry_run)
        totals[r.action] = totals.get(r.action, 0) + 1
        logged += 1
        log.info("%s %s lead=%s note=%s %s", r.action.upper(), c.wa_id, r.lead_id, r.note_id or "", r.detail)
        # mark logged only on a real, successful write
        if (not dry_run) and redis_client is not None and r.action in ("matched", "created") and r.note_id:
            _state.mark_logged(redis_client, c.wa_id, c.last_user_ts)
    log.info("totals: %s", totals)
    return totals


def run_once(cfg=None, *, dry_run: bool = False, limit: Optional[int] = None,
             number: Optional[str] = None) -> dict:
    """Production entry: scan live Redis and log qualifying conversations."""
    cfg = cfg or _config.load()
    provider = get_provider(cfg.crm_provider)
    summarizer = OpenRouterSummarizer(cfg.openrouter_key, cfg.summary_model)
    r = _state.connect(cfg.redis_url)
    convs = list(_source.from_redis(cfg.redis_url, limit=500))
    if number:
        convs = [c for c in convs if c.wa_id.endswith(number[-10:])]
    return process_conversations(
        convs, cfg=cfg, provider=provider, summarizer=summarizer,
        dry_run=dry_run, redis_client=r, limit=limit or cfg.max_per_run,
    )
