"""Zoho CRM provider — self-contained (vendored so the chatbot owns it).

Talks to Zoho's MCP endpoint over JSON-RPC directly (no LLM in the loop). Auth
reuses the OAuth token that scripts/zoho_mcp_auth.py stored once, refreshing the
short-lived access token from the refresh token when stale.

Config (env):
  ZOHO_MCP_URL          the MCP endpoint (required)
  ZOHO_TOKEN_FILE       path to .zoho_mcp_token.json (default: ./.zoho_mcp_token.json)

Phone convention: writes Mobile WITHOUT a leading '+'.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from .base import CRMProvider, LeadInput, LeadRef, NoteRef

log = logging.getLogger("crm_notes.crm.zoho")


class ZohoError(RuntimeError):
    pass


def _token_file() -> Path:
    return Path(os.getenv("ZOHO_TOKEN_FILE", ".zoho_mcp_token.json"))


def _bearer() -> str:
    """Live access token, refreshed from the stored refresh token if stale."""
    tf = _token_file()
    if not tf.exists():
        raise ZohoError(f"Zoho token not found at {tf} (run scripts/zoho_mcp_auth.py once)")
    tok = json.loads(tf.read_text())
    if time.time() < float(tok["expires_at"]) - 60:
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise ZohoError("Zoho token expired and no refresh_token available")
    fresh = httpx.post(tok["token_endpoint"], timeout=30, data={
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": tok["client_id"],
        "client_secret": tok["client_secret"],
    }).json()
    if "access_token" not in fresh:
        raise ZohoError(f"Zoho token refresh failed: {fresh}")
    tok["access_token"] = fresh["access_token"]
    tok["expires_at"] = time.time() + fresh.get("expires_in", 3600)
    tok["refresh_token"] = fresh.get("refresh_token", tok["refresh_token"])
    tf.write_text(json.dumps(tok, indent=2))
    return tok["access_token"]


class _ZohoMCP:
    """Minimal Zoho MCP JSON-RPC client (vendored from app/crm.py)."""

    def __init__(self, timeout: float = 60.0) -> None:
        url = os.getenv("ZOHO_MCP_URL")
        if not url:
            raise ZohoError("ZOHO_MCP_URL is not set")
        self._url = url
        self._client = httpx.Client(timeout=timeout)
        self._id = 0
        self._headers = {
            "Authorization": f"Bearer {_bearer()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self._handshake()

    def _next(self) -> int:
        self._id += 1
        return self._id

    def _post(self, payload: dict) -> httpx.Response:
        r = self._client.post(self._url, headers=self._headers, json=payload)
        r.raise_for_status()
        return r

    def _handshake(self) -> None:
        r = self._post({"jsonrpc": "2.0", "id": self._next(), "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "edyoda-chatbot-crmnotes", "version": "1"}}})
        if sid := r.headers.get("mcp-session-id"):
            self._headers["mcp-session-id"] = sid
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, arguments: dict) -> Any:
        r = self._post({"jsonrpc": "2.0", "id": self._next(), "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments}})
        body = r.text
        if body.startswith("event:"):
            body = [l[6:] for l in body.splitlines() if l.startswith("data: ")][-1]
        env = json.loads(body)
        if "error" in env:
            raise ZohoError(f"{tool}: {env['error']}")
        result = env["result"]
        if result.get("isError"):
            raise ZohoError(f"{tool}: {result.get('content')}")
        structured = result.get("structuredContent")
        payload = (structured.get("data", structured) if structured is not None
                   else json.loads(result["content"][0]["text"]))
        if isinstance(payload, dict) and set(payload) == {"message"}:
            if "success" in payload["message"].lower():
                return {}
            raise ZohoError(f"{tool}: {payload['message']}")
        return payload

    def coql(self, query: str) -> list[dict]:
        out = self.call("ZohoCRM_executeCOQLQuery", {"body": {"select_query": query}})
        return (out or {}).get("data", [])

    def create(self, module: str, records: list[dict]) -> list[dict]:
        out = self.call("ZohoCRM_createRecords",
                        {"path_variables": {"module": module}, "body": {"data": records}})
        return (out or {}).get("data", [])

    def update(self, module: str, records: list[dict], trigger=None) -> list[dict]:
        body: dict = {"data": records}
        if trigger is not None:
            body["trigger"] = trigger           # trigger=[] suppresses workflows
        out = self.call("ZohoCRM_updateRecords",
                        {"path_variables": {"module": module}, "body": body})
        return (out or {}).get("data", [])

    def related_notes(self, lead_id: str) -> list[dict]:
        out = self.call("ZohoCRM_getRelatedRecords", {
            "path_variables": {"parentRecordModule": "Leads", "parentRecord": str(lead_id),
                               "relatedList": "Notes"},
            "query_params": {"fields": "Note_Title,Note_Content,Created_Time", "per_page": 200},
        })
        return (out or {}).get("data", []) or []


def _last10(phone: Optional[str]) -> str:
    """Last 10 digits of a phone, or '' if it has fewer than 10 digits.

    The <10 gate is deliberate and country-agnostic: a partial/garbage number
    (e.g. '91') must never become a match key, or a substring LIKE would match
    nearly every lead. find_leads also equality-filters on this value, so the
    match is on equal last-10 digits, never a substring coincidence.
    """
    d = "".join(ch for ch in (phone or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else ""


_LEAD_FIELDS = ("id, First_Name, Last_Name, Email, Phone, Mobile, Select_Program, "
                "Lead_Status, Created_Time")


def _to_lead(r: dict) -> LeadRef:
    name = " ".join(x for x in [r.get("First_Name"), r.get("Last_Name")] if x) or None
    return LeadRef(id=str(r["id"]), name=name, phone=r.get("Mobile") or r.get("Phone"),
                   email=r.get("Email"), created=r.get("Created_Time"),
                   program=r.get("Select_Program"), status=r.get("Lead_Status"))


class ZohoProvider(CRMProvider):
    name = "zoho"

    def __init__(self) -> None:
        self._z = _ZohoMCP()

    def find_leads(self, *, phone=None, email=None) -> list[LeadRef]:
        clauses = []
        tail = _last10(phone)                        # '' if <10 digits -> never a key
        norm_email = email.strip().lower() if email else None
        if tail:
            clauses.append(f"(Phone like '%{tail}%' or Mobile like '%{tail}%')")
        if norm_email:
            clauses.append(f"Email = '{norm_email}'")
        if not clauses:
            return []
        rows = self._z.coql(
            f"select {_LEAD_FIELDS} from Leads where ({' or '.join(clauses)}) "
            "order by Created_Time desc limit 100")
        # The LIKE above is only a candidate fetch. Keep a row only if it matches a
        # key EXACTLY — equal last-10 digits, or exact email — so a substring hit
        # (e.g. '%9502678024%' landing inside a longer unrelated number) is dropped.
        out = []
        for r in rows:
            r_tail = _last10(r.get("Mobile") or r.get("Phone"))
            r_email = (r.get("Email") or "").strip().lower()
            if (tail and r_tail == tail) or (norm_email and r_email == norm_email):
                out.append(_to_lead(r))
        return out

    def leads_created_since(self, iso_dt: str) -> list[LeadRef]:
        rows = self._z.coql(
            f"select {_LEAD_FIELDS} from Leads where Created_Time >= '{iso_dt}' "
            "order by Created_Time desc limit 2000")
        return [_to_lead(r) for r in rows]

    def list_notes(self, lead_id: str) -> list[NoteRef]:
        rows = self._z.related_notes(lead_id)
        notes = [NoteRef(id=str(n.get("id")), title=n.get("Note_Title") or "",
                         content=n.get("Note_Content") or "", created=n.get("Created_Time"))
                 for n in rows]
        notes.sort(key=lambda n: n.created or "", reverse=True)
        return notes

    def set_lead_status(self, lead_id: str, status: str) -> None:
        res = self._z.update("Leads", [{"id": str(lead_id), "Lead_Status": status}], trigger=[])
        if not res or res[0].get("status") != "success":
            raise ZohoError(f"set_lead_status failed for {lead_id}: {res}")

    def update_lead(self, lead_id: str, fields: dict) -> None:
        res = self._z.update("Leads", [{"id": str(lead_id), **fields}], trigger=[])
        if not res or res[0].get("status") != "success":
            raise ZohoError(f"update_lead failed for {lead_id}: {res}")

    def create_lead(self, lead: LeadInput) -> LeadRef:
        rec: dict[str, Any] = {"Last_Name": lead.last_name or "TBD", "Lead_Status": "Fresh"}
        if lead.source:
            rec["Lead_Source"] = lead.source     # e.g. "WAChat" for chatbot-created leads
        if lead.first_name:
            rec["First_Name"] = lead.first_name
        if lead.phone:
            rec["Mobile"] = lead.phone            # no '+' — caller already stripped it
        if lead.email:
            rec["Email"] = lead.email
        if lead.description:
            rec["Description"] = lead.description
        res = self._z.create("Leads", [rec])
        if not res or res[0].get("status") != "success":
            raise ZohoError(f"create_lead failed: {res}")
        name = " ".join(x for x in [lead.first_name, lead.last_name] if x) or None
        return LeadRef(id=str(res[0]["details"]["id"]), name=name,
                       phone=lead.phone, email=lead.email,
                       created=res[0]["details"].get("Created_Time"))

    def add_note(self, lead_id: str, title: str, body: str) -> str:
        res = self._z.create("Notes", [{
            "Note_Title": title,
            "Note_Content": body,
            "Parent_Id": {"id": str(lead_id), "module": {"api_name": "Leads"}},
        }])
        if not res or res[0].get("status") != "success":
            raise ZohoError(f"add_note failed: {res}")
        return str(res[0]["details"]["id"])
