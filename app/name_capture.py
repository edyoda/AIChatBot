"""Customer-name capture for the WhatsApp bot.

On each conversation we check once whether the CRM already has the caller's name
(a direct Zoho lookup by phone — no LLM). If it does, we hand the name to the
responder so it can address the person naturally. If it doesn't, we tell the
responder to ask for the name at a natural moment and expose a `save_customer_name`
tool; when the user shares it we write it back to the CRM (update the existing
lead, else create one) and stop asking.

Cost: the lookup and the write are plain Zoho calls (0 model tokens). The only
model cost is the small tool schema + one tool-loop turn at capture time, and it
rides *only* while the name is unknown — once known, the tool disappears and the
context line is a short "you know their name" note.

Storage rule (per product decision): a 2-word (or more) name is split into
First_Name (first token) + Last_Name (the rest); a single word is stored as
Last_Name only. Phone is written WITHOUT a leading '+' (CRM convention).

We ask at most once per conversation: the moment we decide to ask, name_status is
set to "asked" so no later turn re-prompts — if the customer never gives it, the
bot simply continues without it.

Meta fields owned here, in chat:meta:<waId>:
  name_status   : "known" | "asked"  — "asked" = already prompted once (or legacy)
  customer_name : "<full name>"       — set when known or just captured
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

from app import chat_history
from app.crm_notes import config as crm_config
from app.crm_notes.crm.base import LeadInput, get_provider

log = logging.getLogger("name_capture")

TOOL_NAME = "save_customer_name"

_PLACEHOLDER_NAMES = {"", "tbd", "na", "n/a", "none", "unknown", "customer",
                      "whatsapp", "lead", "test", "user"}

# lazy, process-wide provider (find_leads / update_lead / create_lead are stateless)
_provider = None
_provider_failed = False


def enabled() -> bool:
    return os.getenv("NAME_CAPTURE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _get_provider():
    global _provider, _provider_failed
    if _provider is None and not _provider_failed:
        try:
            _provider = get_provider(crm_config.load().crm_provider)
        except Exception as e:  # pragma: no cover - config/credential issues
            _provider_failed = True
            log.warning("name_capture: CRM provider unavailable: %s", e)
    return _provider


def _is_real_name(name: Optional[str]) -> bool:
    """True only for something that looks like an actual personal name."""
    if not name:
        return False
    n = " ".join(str(name).split()).strip()
    if not n or n.lower() in _PLACEHOLDER_NAMES:
        return False
    if not any(c.isalpha() for c in n):     # pure digits / symbols (e.g. a phone)
        return False
    if sum(c.isdigit() for c in n) >= 4:    # phone-ish blobs
        return False
    return True


def _split_name(name: str) -> Tuple[Optional[str], str]:
    """('Rahul Kumar Sharma') -> ('Rahul', 'Kumar Sharma'); ('Priya') -> (None, 'Priya')."""
    parts = " ".join(name.split()).split(" ")
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return None, parts[0]


def resolve(waid: str) -> Tuple[str, Optional[str]]:
    """Return (status, name). status in {"known", "ask", "asked", "off"}.

    "ask"   -> name not on file and not asked yet: prompt for it THIS turn (once).
    "asked" -> already asked once: never ask again; only capture if volunteered.
    "off"   -> feature off, or CRM unreachable (inject nothing; fail open so we
               never nag when we can't verify).

    The CRM is queried at most once per conversation, and we ask at most once:
    the moment we decide to ask, we persist name_status="asked" so no later turn
    re-prompts, regardless of whether the customer ever provides the name.
    """
    if not enabled():
        return ("off", None)

    meta = chat_history.get_session_meta(waid) or {}
    if _is_real_name(meta.get("customer_name")):
        return ("known", meta["customer_name"])

    # Already asked once (or a legacy "unknown" session that was being re-nagged):
    # do not ask again — just keep the tool live for a volunteered name.
    if meta.get("name_status") in ("asked", "unknown"):
        return ("asked", None)

    # First encounter this conversation: one CRM lookup.
    prov = _get_provider()
    if prov is None:
        return ("off", None)
    try:
        leads = prov.find_leads(phone=waid)
    except Exception as e:
        log.warning("name_capture: find_leads failed for %s: %s", waid, e)
        return ("off", None)

    for lead in leads:                      # newest-first
        if _is_real_name(lead.name):
            chat_history.set_meta_fields(waid, {"name_status": "known",
                                                "customer_name": lead.name})
            return ("known", lead.name)

    # Not on file -> ask this turn, and immediately mark asked so we never re-prompt.
    chat_history.set_meta_fields(waid, {"name_status": "asked"})
    return ("ask", None)


def known_first_name(waid: str) -> Optional[str]:
    """First name to greet a new session with IF the CRM already has one, else None.

    Read-only with respect to the ask flow: it never marks the session "asked", so
    a genuine ask-once still happens on the first real message when no name is on
    file. A positive hit is cached as "known" (safe — caching a known name never
    suppresses asking). Used by the WhatsApp welcome line, before chat() runs.
    """
    if not enabled():
        return None
    meta = chat_history.get_session_meta(waid) or {}
    name = meta.get("customer_name")
    if not _is_real_name(name):
        prov = _get_provider()
        if prov is None:
            return None
        try:
            leads = prov.find_leads(phone=waid)
        except Exception as e:
            log.warning("name_capture: greeting lookup failed for %s: %s", waid, e)
            return None
        name = next((l.name for l in leads if _is_real_name(l.name)), None)
        if _is_real_name(name):
            chat_history.set_meta_fields(waid, {"name_status": "known",
                                                "customer_name": name})
    if not _is_real_name(name):
        return None
    return name.split()[0]


def context_line(status: str, name: Optional[str]) -> str:
    """The line injected into the responder's context for this turn."""
    if status == "known" and name:
        return (f"Customer's name (from CRM): {name}. Address them by their first "
                "name naturally where it fits; do NOT ask for their name.")
    if status == "ask":
        return ("You do not have the customer's name yet. Ask for it just ONCE, "
                "briefly and politely, worked naturally into your reply (not as the "
                f"whole message). If they give it, call the {TOOL_NAME} tool with "
                "the name exactly as given. If they decline, deflect, or don't "
                "answer, do NOT ask again — simply continue helping without it.")
    if status == "asked":
        return ("You already asked for the customer's name once and still don't "
                "have it. Do NOT ask again — continue helping without it. Only if "
                f"the customer offers their name on their own, call the {TOOL_NAME} "
                "tool with it.")
    return ""


def tool_schema(status: str) -> list[dict]:
    """The save_customer_name tool — live while asking, and after (for a
    volunteered name), but not once the name is known or the feature is off."""
    if status not in ("ask", "asked"):
        return []
    return [{
        "name": TOOL_NAME,
        "description": (
            "Save the customer's own name to the CRM. Call this only after the "
            "customer has told you their name in the conversation, and only with a "
            "real personal name they gave for themselves. Never guess a name, and "
            "never pass a company, course, city, or product name."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The customer's name exactly as they gave it, "
                                   "e.g. 'Rahul Sharma' or 'Priya'.",
                },
            },
            "required": ["name"],
        },
    }]


def is_tool(name: str) -> bool:
    return name == TOOL_NAME


def handle(waid: str, args: dict) -> str:
    """Execute the save_customer_name tool call. Returns a short tool_result string."""
    raw = (args or {}).get("name", "")
    name = " ".join(str(raw).split()).strip()
    if not _is_real_name(name):
        return "That did not look like a valid personal name, so nothing was saved."

    first, last = _split_name(name)
    prov = _get_provider()

    # Always cache locally so we stop asking this conversation, even if the write fails.
    chat_history.set_meta_fields(waid, {"name_status": "known", "customer_name": name})

    if prov is None:
        return f"Noted the name {name} for this chat."
    try:
        leads = prov.find_leads(phone=waid)
        if leads:
            fields = {"Last_Name": last}
            if first:
                fields["First_Name"] = first
            prov.update_lead(leads[0].id, fields)
            log.info("name_capture: updated lead %s for %s -> first=%r last=%r",
                     leads[0].id, waid, first, last)
        else:
            prov.create_lead(LeadInput(last_name=last, first_name=first, phone=waid,
                                       source="WAChat"))
            log.info("name_capture: created lead for %s -> first=%r last=%r", waid, first, last)
    except Exception as e:
        log.warning("name_capture: save failed for %s: %s", waid, e)
        return f"Recorded the name {name}."
    return f"Saved. The customer's name is {name}."
