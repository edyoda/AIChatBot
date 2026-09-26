"""Summarizer — turns a transcript into a call-prep note.

Pluggable like the CRM: OpenRouterSummarizer (Llama by default) is one impl.
The prompt is guardrailed: summarize the CUSTOMER's intent, treat bot claims
(price, seats, offers) as NOT facts, flag repeated objections, mark unknowns.
"""
from __future__ import annotations

import logging
from typing import Optional, Protocol

log = logging.getLogger("crm_notes.summarize")

_SYSTEM = (
    "You are preparing a call-prep note for an EdYoda sales advisor who is about to "
    "make an OUTBOUND call to this prospect. Summarize the WhatsApp conversation into a "
    "concise, factual note the advisor can skim before dialing.\n"
    "RULES:\n"
    "- Use ONLY facts the CUSTOMER stated. If something is not stated (name, experience, "
    "budget, timeline), write 'not stated' — never invent it.\n"
    "- Treat anything the BOT said (prices, seats/scarcity, offers, mentors, availability) "
    "as NOT fact — do not repeat it.\n"
    "- Do NOT put any phone numbers, links, or contact details in the note.\n"
    "- Call out every OBJECTION or concern the customer raised; if they repeated one, say so.\n"
    "- No preamble and no sign-off; start directly with the headings below.\n"
    "Sections (use these exact headings):\n"
    "Interest — which course/topic and what they want.\n"
    "Background — role, experience, situation (only what the customer stated).\n"
    "Objections/concerns — hesitations and any repeated points.\n"
    "Recommended approach for the call — concrete things the ADVISOR should do on the "
    "call (what to lead with, what to confirm, how to position). Phrase these as advisor "
    "actions, never as things the customer should do."
)


class Summarizer(Protocol):
    def summarize(self, transcript: str) -> str: ...


class OpenRouterSummarizer:
    def __init__(self, api_key: str, model: str, timeout: float = 60.0) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is not set")
        self._key = api_key
        self._model = model
        self._timeout = timeout

    def summarize(self, transcript: str) -> str:
        import httpx
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            json={
                "model": self._model, "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": transcript},
                ],
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


def guess_topic(transcript: str) -> str:
    """Cheap topic label for the note title (no LLM) — best-effort keyword scan."""
    t = transcript.lower()
    for label, keys in (
        ("GRC", ("grc", "governance", "compliance")),
        ("Data/Cloud", ("data architect", "multi-cloud", "data engineer", "cloud")),
        ("AI/GenAI", ("generative ai", "genai", "ai agent", "machine learning")),
        ("Cybersecurity", ("cybersecurity", "soc", "splunk", "ethical hacking")),
        ("DevOps", ("devops", "kubernetes", "docker", "terraform")),
        ("Marketing", ("digital marketing", "performance marketing")),
        ("Testing", ("test automation", "selenium")),
    ):
        if any(k in t for k in keys):
            return label
    return "General"
