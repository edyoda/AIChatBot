"""Orchestrator — CRM-agnostic per-conversation logic.

For one qualifying conversation: summarize -> find-or-create lead (latest on
duplicates) -> add note. Uses ONLY the CRMProvider interface, so this policy is
shared across every CRM. Phone is written WITHOUT a leading '+'.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from typing import Optional

from .crm.base import CRMProvider, LeadInput, LeadRef
from .source import Conversation
from .summarize import Summarizer, guess_topic

log = logging.getLogger("crm_notes.orchestrator")


def phone_no_plus(wa_id: str) -> str:
    """Digits only, no leading '+' (EdYoda stripPlusForCRM convention)."""
    return "".join(ch for ch in wa_id if ch.isdigit())


@dataclass
class Result:
    wa_id: str
    action: str          # "matched" | "created" | "skipped" | "error"
    lead_id: Optional[str] = None
    note_id: Optional[str] = None
    topic: str = ""
    summary: str = ""
    detail: str = ""


def process(conv: Conversation, crm: CRMProvider, summarizer: Summarizer,
            *, dry_run: bool = True) -> Result:
    wa = conv.wa_id
    phone = phone_no_plus(wa)
    topic = guess_topic(conv.transcript())  # kept for the run log only, not the CRM record
    title = f"[WhatsApp Call-Prep] {_dt.date.today().isoformat()}"

    try:
        summary = summarizer.summarize(conv.transcript())
    except Exception as e:
        log.warning("summarize failed for %s: %s", wa, e)
        return Result(wa, "error", topic=topic, detail=f"summarize: {type(e).__name__}: {e}")

    try:
        leads = crm.find_leads(phone=phone)
    except Exception as e:
        return Result(wa, "error", topic=topic, summary=summary, detail=f"find: {type(e).__name__}: {e}")

    if leads:
        lead: LeadRef = leads[0]          # newest first -> "latest on duplicates"
        action = "matched"
    elif dry_run:
        return Result(wa, "created", lead_id="(dry-run: would create)", topic=topic,
                      summary=summary, detail=f"phone={phone}")
    else:
        try:
            lead = crm.create_lead(LeadInput(
                last_name="TBD", phone=phone, email=None, source="WAChat",
                description=f"Created from WhatsApp bot chat {_dt.date.today().isoformat()}. See the call-prep note.",
            ))
            action = "created"
        except Exception as e:
            return Result(wa, "error", topic=topic, summary=summary, detail=f"create: {type(e).__name__}: {e}")

    body = f"WhatsApp bot chat, {_dt.date.today().isoformat()} (Llama-summarized):\n\n{summary}"
    if dry_run:
        return Result(wa, action, lead_id=lead.id, note_id="(dry-run: would add note)",
                      topic=topic, summary=body, detail=f"lead={lead.name or ''} {lead.phone or ''}")
    try:
        note_id = crm.add_note(lead.id, title, body)
    except Exception as e:
        return Result(wa, "error", lead_id=lead.id, topic=topic, summary=summary,
                      detail=f"note: {type(e).__name__}: {e}")

    return Result(wa, action, lead_id=lead.id, note_id=note_id, topic=topic, summary=body)
