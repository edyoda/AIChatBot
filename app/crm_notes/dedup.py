"""Lead dedup job — merges a person's older leads into their newest one.

Runs twice daily (scheduled in main.py). For each lead created in the last N
hours, it clusters the person's leads (phone last-10 / email), keeps the NEWEST
as the survivor, and for each older, still-active duplicate:
  * copies its notes onto the survivor (notes can't be re-parented in Zoho),
  * marks it Lead_Status='Duplicate' (reversible; trigger suppressed),
and writes one Select_Program history note on the survivor as JSON:
  [{"<lead created date>": "<Select_Program>"}, ...]  (chronological, whole cluster).

Idempotent: only leads NOT already 'Duplicate' are treated as merge sources, so a
re-run finds nothing to do. dry_run=True does zero writes and reports the plan.
A per-run rollback JSON records prior statuses so any run can be reversed.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Optional

from . import config as _config
from .crm.base import CRMProvider, LeadRef, get_provider

log = logging.getLogger("crm_notes.dedup")

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def _date_of(iso: Optional[str]) -> str:
    return (iso or "")[:10]  # YYYY-MM-DD


def _program_history(cluster: list[LeadRef]) -> list[dict]:
    """[{created-date: Select_Program}, ...] over the cluster, oldest→newest."""
    out = []
    for l in sorted(cluster, key=lambda x: x.created or ""):
        if l.program:
            out.append({_date_of(l.created): l.program})
    return out


def run_once(cfg=None, *, dry_run: bool = True, crm: Optional[CRMProvider] = None) -> dict:
    cfg = cfg or _config.load()
    crm = crm or get_provider(cfg.crm_provider)

    since = _dt.datetime.now(_IST) - _dt.timedelta(hours=cfg.dedup_window_hours)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S+05:30")
    recent = crm.leads_created_since(since_iso)
    log.info("dedup: %d leads created since %s (dry_run=%s)", len(recent), since_iso, dry_run)

    totals = {"recent": len(recent), "clusters": 0, "duplicates_marked": 0,
              "notes_copied": 0, "program_notes": 0, "errors": 0, "skipped_singletons": 0,
              "oversized_skipped": 0}
    processed_ids: set[str] = set()
    rollback: list[dict] = []

    for lead in recent:
        if lead.id in processed_ids:
            continue
        try:
            cluster = crm.find_leads(phone=lead.phone, email=lead.email)
        except Exception as e:
            log.warning("dedup: cluster lookup failed for %s: %s", lead.id, e)
            totals["errors"] += 1
            continue
        for l in cluster:
            processed_ids.add(l.id)
        if len(cluster) < 2:
            totals["skipped_singletons"] += 1
            continue

        # Safety cap: an implausibly large cluster means an over-match (bad phone or
        # a shared placeholder email). Never mass-merge — skip and flag for review.
        if len(cluster) > cfg.dedup_max_cluster:
            log.warning("dedup: SKIP oversized cluster of %d (seed=%s phone=%s email=%s) "
                        "— likely over-match; needs human review",
                        len(cluster), lead.id, lead.phone, lead.email)
            totals["oversized_skipped"] += 1
            continue

        survivor = max(cluster, key=lambda x: x.created or "")
        active_olders = [l for l in cluster
                         if l.id != survivor.id and (l.status or "").strip().lower() != "duplicate"]
        if not active_olders:
            continue  # already deduped
        totals["clusters"] += 1

        prog_json = _program_history(cluster)
        log.info("MERGE survivor=%s(%s) <- %d older %s | programs=%s",
                 survivor.id, _date_of(survivor.created), len(active_olders),
                 [o.id for o in active_olders], json.dumps(prog_json, ensure_ascii=False))

        if dry_run:
            continue

        # 1) copy notes + retire each older duplicate
        for older in active_olders:
            try:
                for n in crm.list_notes(older.id):
                    tag = f"[Merged from lead {older.id} · {_date_of(older.created)}] {n.title}".strip()
                    crm.add_note(survivor.id, tag[:250], n.content or "")
                    totals["notes_copied"] += 1
                crm.set_lead_status(older.id, "Duplicate")
                rollback.append({"lead_id": older.id, "prev_status": older.status,
                                 "merged_into": survivor.id})
                totals["duplicates_marked"] += 1
            except Exception as e:
                log.warning("dedup: merge of older %s failed: %s", older.id, e)
                totals["errors"] += 1

        # 2) Select_Program history note on the survivor
        try:
            crm.add_note(survivor.id, "[Merge] Select_Program history",
                         json.dumps(prog_json, ensure_ascii=False))
            totals["program_notes"] += 1
        except Exception as e:
            log.warning("dedup: program-history note failed for %s: %s", survivor.id, e)
            totals["errors"] += 1

    if not dry_run and rollback:
        try:
            os.makedirs(cfg.dedup_rollback_dir, exist_ok=True)
            path = os.path.join(cfg.dedup_rollback_dir,
                                f"dedup_rollback_{_dt.datetime.now(_IST):%Y%m%d_%H%M%S}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rollback, f, indent=2)
            log.info("dedup: rollback written -> %s (%d entries)", path, len(rollback))
        except Exception as e:
            log.warning("dedup: failed to write rollback json: %s", e)

    log.info("dedup totals: %s", totals)
    return totals


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run_once(dry_run="--write" not in sys.argv)
