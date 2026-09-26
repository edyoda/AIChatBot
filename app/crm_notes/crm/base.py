"""The CRM plug point.

Every CRM implements CRMProvider's three methods. The orchestrator talks ONLY to
this interface, so the find-or-create + latest-lead-on-duplicate policy and note
formatting are shared across all CRMs. Add a CRM: subclass CRMProvider, register
it in PROVIDERS, set CRM_PROVIDER=<name>.

Phone convention: numbers are passed/stored WITHOUT a leading '+'. Providers must
not add one (matches EdYoda's stripPlusForCRM form convention).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("crm_notes.crm")


@dataclass
class LeadRef:
    """A CRM lead we found or created."""
    id: str
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    created: Optional[str] = None   # ISO timestamp, used to pick the latest on dupes
    program: Optional[str] = None   # Select_Program (or provider equivalent)
    status: Optional[str] = None    # Lead_Status (e.g. Fresh / Duplicate)


@dataclass
class NoteRef:
    """A note read from the CRM."""
    id: str
    title: str
    content: str
    created: Optional[str] = None


@dataclass
class LeadInput:
    """Fields to create a new lead with. Phone has NO leading '+'."""
    last_name: str
    first_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    source: str = "WAChat"                  # Zoho Lead_Source for chatbot-created leads
    description: str = ""
    extra: dict = field(default_factory=dict)


class CRMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def find_leads(self, *, phone: Optional[str] = None, email: Optional[str] = None) -> list[LeadRef]:
        """Return matching leads, newest first (so callers can take [0] = latest)."""

    @abstractmethod
    def create_lead(self, lead: LeadInput) -> LeadRef:
        """Create a lead and return its ref."""

    @abstractmethod
    def add_note(self, lead_id: str, title: str, body: str) -> str:
        """Attach a note to a lead; return the note id."""

    # --- ops used by the dedup job (dedup.py) --------------------------------
    @abstractmethod
    def leads_created_since(self, iso_dt: str) -> list["LeadRef"]:
        """Leads created at/after the given ISO datetime, newest first."""

    @abstractmethod
    def list_notes(self, lead_id: str) -> list["NoteRef"]:
        """All notes on a lead, newest first."""

    @abstractmethod
    def set_lead_status(self, lead_id: str, status: str) -> None:
        """Set a lead's status (e.g. 'Duplicate'), without firing workflows."""

    @abstractmethod
    def update_lead(self, lead_id: str, fields: dict) -> None:
        """Patch arbitrary fields on a lead (e.g. First_Name/Last_Name)."""


class FakeCRMProvider(CRMProvider):
    """In-memory provider for dev/testing — records calls, writes nothing external.

    Seed `existing` with LeadRefs to simulate matches (keyed by last-10 phone
    digits) so the find-or-create path can be exercised both ways.
    """
    name = "fake"

    def __init__(self, existing: Optional[list[LeadRef]] = None) -> None:
        self._all: list[LeadRef] = []
        self._by_phone10: dict[str, list[LeadRef]] = {}
        for lr in (existing or []):
            self._index(lr)
        self._seq = 1000
        self.notes: list[dict] = []

    @staticmethod
    def _last10(phone: Optional[str]) -> str:
        # Mirror ZohoProvider: <10 digits -> '' (never a match key).
        d = "".join(ch for ch in (phone or "") if ch.isdigit())
        return d[-10:] if len(d) >= 10 else ""

    def _index(self, lr: LeadRef) -> None:
        self._all.append(lr)
        self._by_phone10.setdefault(self._last10(lr.phone), []).append(lr)

    def find_leads(self, *, phone=None, email=None) -> list[LeadRef]:
        tail = self._last10(phone)                    # '' if <10 digits -> no phone match
        rows = list(self._by_phone10.get(tail, [])) if tail else []
        if email:
            rows += [l for l in self._all if (l.email or "").lower() == email.lower() and l not in rows]
        rows.sort(key=lambda r: r.created or "", reverse=True)
        return rows

    def create_lead(self, lead: LeadInput) -> LeadRef:
        self._seq += 1
        name = " ".join(x for x in [lead.first_name, lead.last_name] if x) or None
        lr = LeadRef(id=f"fake-{self._seq}", name=name, phone=lead.phone,
                     email=lead.email, created="2026-08-26T00:00:00+05:30", status="Fresh")
        self._index(lr)
        log.info("[fake] created lead %s phone=%s name=%s", lr.id, lr.phone, name)
        return lr

    def add_note(self, lead_id: str, title: str, body: str) -> str:
        nid = f"fakenote-{len(self.notes) + 1}"
        self.notes.append({"id": nid, "lead_id": str(lead_id), "title": title, "body": body,
                           "created": "2026-08-26T00:00:00+05:30"})
        log.info("[fake] note %s on lead %s", nid, lead_id)
        return nid

    def leads_created_since(self, iso_dt: str) -> list[LeadRef]:
        rows = [l for l in self._all if (l.created or "") >= iso_dt]
        rows.sort(key=lambda r: r.created or "", reverse=True)
        return rows

    def list_notes(self, lead_id: str) -> list["NoteRef"]:
        rows = [NoteRef(id=n["id"], title=n["title"], content=n["body"], created=n.get("created"))
                for n in self.notes if n["lead_id"] == str(lead_id)]
        rows.sort(key=lambda n: n.created or "", reverse=True)
        return rows

    def set_lead_status(self, lead_id: str, status: str) -> None:
        for l in self._all:
            if l.id == str(lead_id):
                l.status = status
        log.info("[fake] lead %s -> status=%s", lead_id, status)

    def update_lead(self, lead_id: str, fields: dict) -> None:
        for l in self._all:
            if l.id == str(lead_id):
                fn, ln = fields.get("First_Name"), fields.get("Last_Name")
                if fn is not None or ln is not None:
                    l.name = " ".join(x for x in [fn, ln] if x) or l.name
                for k in ("Email", "Mobile", "Phone"):
                    if k in fields:
                        setattr(l, "email" if k == "Email" else "phone", fields[k])
        log.info("[fake] lead %s <- %s", lead_id, fields)


# registry -------------------------------------------------------------------
def get_provider(name: str) -> CRMProvider:
    name = (name or "").strip().lower()
    if name in ("fake", "dryfake", "test"):
        return FakeCRMProvider()
    if name == "zoho":
        from .zoho import ZohoProvider
        return ZohoProvider()
    raise ValueError(f"unknown CRM provider: {name!r} (known: fake, zoho)")
