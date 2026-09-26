"""CLI runner for CRM-notes. Defaults to DRY-RUN (no writes).

    python -m app.crm_notes.runner --fixture fixtures/conversations_sample.json --provider fake
    python -m app.crm_notes.runner --redis --provider zoho            # dry-run over live Redis
    python -m app.crm_notes.runner --redis --provider zoho --write    # real writes (gated)
    python -m app.crm_notes.runner --redis --number 919603770001 --write
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import config as _config
from . import service as _service
from . import source as _source
from . import state as _state
from .crm.base import get_provider
from .summarize import OpenRouterSummarizer


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="CRM-notes runner (dry-run by default)")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--fixture", help="load conversations from a JSON fixture")
    src.add_argument("--redis", action="store_true", help="load from live Redis")
    p.add_argument("--provider", default=None, help="crm provider (fake|zoho); default from CRM_PROVIDER")
    p.add_argument("--number", help="only process this waId (suffix match)")
    p.add_argument("--limit", type=int, default=None, help="max conversations to log this run")
    p.add_argument("--write", action="store_true", help="DISABLE dry-run and actually write to the CRM")
    args = p.parse_args(argv)

    cfg = _config.load()
    dry = not args.write
    provider_name = args.provider or cfg.crm_provider
    print(f"# provider={provider_name}  dry_run={dry}  idle>={cfg.idle_seconds}s  min_msgs={cfg.min_user_msgs}")

    try:
        summarizer = OpenRouterSummarizer(cfg.openrouter_key, cfg.summary_model)
    except ValueError as e:
        print(f"ERROR: {e} (set OPENROUTER_API_KEY)", file=sys.stderr)
        return 2
    provider = get_provider(provider_name)

    redis_client = None
    if args.redis:
        convs = list(_source.from_redis(cfg.redis_url, limit=500))
        redis_client = _state.connect(cfg.redis_url)      # for idempotency state
    else:
        convs = _source.from_fixture(args.fixture or "fixtures/conversations_sample.json")
    if args.number:
        convs = [c for c in convs if c.wa_id.endswith(args.number[-10:])]

    _service.process_conversations(
        convs, cfg=cfg, provider=provider, summarizer=summarizer,
        dry_run=dry, redis_client=redis_client, limit=args.limit or cfg.max_per_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
