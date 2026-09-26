"""
EdYoda LMS MCP server (stdio transport).

A *catalog* of LMS capability tools the chatbot (and, later, other EdYoda agents)
can call over MCP. Tool #1 is the public, Tier-1 batch-start-date lookup.

Tier-2 (user-private tools: certificate, attendance, progress) is intentionally
NOT implemented here yet — but the seams are in place so those tools are an
"add-a-function" later, with no re-architecture:

  * Tool registry: FastMCP's @mcp.tool() — add a function to grow the catalog.
  * _lms_get(path, authed=False): attaches a service-auth header from
    EDYODA_LMS_SERVICE_SECRET ONLY when authed=True (Tier-2). Tier-1 calls
    authed=False against public endpoints; the secret is unused for now.
  * Trusted-identity seam: user-private tools will read a verified WhatsApp id
    that the CLIENT injects out-of-band (never part of the LLM-facing schema).
    See app/mcp_client.py -> call_tool(..., context=...) and _IDENTITY_TOOLS.

Config (env):
  EDYODA_LMS_BASE_URL       default https://backend.edyoda.com
  EDYODA_LMS_SERVICE_SECRET Tier-2 placeholder (unused today)
  EDYODA_LMS_HTTP_TIMEOUT   default 10 (seconds)
"""
from __future__ import annotations

import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

EDYODA_LMS_BASE_URL = os.getenv("EDYODA_LMS_BASE_URL", "https://backend.edyoda.com").rstrip("/")
EDYODA_LMS_SERVICE_SECRET = os.getenv("EDYODA_LMS_SERVICE_SECRET", "")  # Tier-2 seam (unused)
HTTP_TIMEOUT = float(os.getenv("EDYODA_LMS_HTTP_TIMEOUT", "10"))

mcp = FastMCP("edyoda-lms")


def _lms_get(path: str, params: Optional[dict] = None, authed: bool = False) -> dict:
    """GET an LMS endpoint and return parsed JSON.

    Tier-1 (public): authed=False -> no credentials.
    Tier-2 (user-private, later): authed=True -> attach the service secret. This
    branch is the seam; no tool uses it yet.
    """
    url = f"{EDYODA_LMS_BASE_URL}{path}"
    headers = {}
    if authed:
        if not EDYODA_LMS_SERVICE_SECRET:
            raise RuntimeError("EDYODA_LMS_SERVICE_SECRET is not configured for an authenticated call")
        headers["X-Service-Secret"] = EDYODA_LMS_SERVICE_SECRET
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.get(url, params=params or {}, headers=headers)
        resp.raise_for_status()
        return resp.json()


# ── Tool #1 (Tier-1, public): next batch start date ──────────────────────────
@mcp.tool()
def get_batch_start_date(course_query: str) -> str:
    """Get the next upcoming batch (cohort) start date for an EdYoda course.

    Use this whenever the user asks when a course or batch starts, about the next
    batch, the upcoming cohort, or a start date.

    `course_query` MUST be the course's URL slug (course_url), e.g.
    'grc-micro-degree' or 'grc-manager-micro-degree' — take it from the Course URL
    shown in the course context (the part after the last '/'). A full program URL
    is also accepted. Do NOT pass any personal or user identifier here.
    """
    q = (course_query or "").strip()
    if not q:
        return "I need to know which course to check the batch start date for."
    try:
        data = _lms_get("/api/v1/next-cohort-start", params={"course_url": q}, authed=False)
    except Exception as e:  # network/HTTP/parse — degrade gracefully
        return f"Batch date lookup is temporarily unavailable ({type(e).__name__})."

    if not data.get("found"):
        # LOOKUP FAILURE (wrong/guessed course reference) — NOT a no-batch result.
        return (
            f"Could not resolve an EdYoda course for '{q}'. This is a lookup failure, "
            f"NOT a 'no batch' result. Do NOT tell the user the batch dates aren't out. "
            f"Instead, say you'll have an EdYoda advisor confirm the schedule and give "
            f"them customer support +91-8045682485."
        )

    name = data.get("course_name") or q
    start = data.get("next_batch_start_date")
    batch = data.get("batch_code")
    if not start:
        return (
            f"{name}: no upcoming batch is scheduled yet. "
            f"You MUST tell the user the batch dates aren't out yet and ask them to "
            f"please call EdYoda customer support at +91-8045682485 to know the "
            f"earliest available date. Do not omit the customer support number."
        )
    tail = f" (batch {batch})." if batch else "."
    return f"{name}: the next batch starts on {start}{tail}"


if __name__ == "__main__":
    # stdio transport: the chatbot (MCP client) spawns this as a child process.
    mcp.run()
