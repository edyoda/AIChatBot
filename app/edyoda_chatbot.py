"""
Edyoda Course Chatbot (RAG)
Multi-session RAG chatbot: Pinecone (edyoda-course-search) + Claude LLM.

- Session history is stored in Redis (shared, bounded, survives restarts).
- Query is embedded using OpenAI embeddings and retrieved from Pinecone.
- Claude generates a response grounded in retrieved course context.
"""

from __future__ import annotations

import json
import logging as _logging
import os
import random
import re
import threading
import time
from difflib import SequenceMatcher
from typing import Optional

import anthropic
from openai import OpenAI
from pinecone import Pinecone

_rag_logger = _logging.getLogger("edyoda_rag")

from app.chat_history import append_messages, get_recent_messages
from app.mcp_client import get_anthropic_tools as get_mcp_tools, call_tool as mcp_call_tool
from app import name_capture
from pathlib import Path

COURSE_INDEX = os.getenv("PINECONE_COURSE_INDEX", "edyoda-course-search")
COURSE_NAMESPACE = os.getenv("PINECONE_COURSE_NAMESPACE", "courses")
FAQ_INDEX = os.getenv("PINECONE_FAQ_INDEX", "edyoda-faqs")
FAQ_NAMESPACE = os.getenv("PINECONE_FAQ_NAMESPACE", "faqs")
EMBED_MODEL = "text-embedding-3-large"
# ── Model selection is deployment config (env), not code ──────────────────────
# Change a model via .env + restart (per-environment, instant rollback) instead
# of editing/redeploying code. Guarded: an unknown/incompatible id falls back to
# the current default and logs an ERROR, so a typo can't 400 every call.
#   Responder passes thinking=adaptive -> must be a 4.6+ model (Haiku 4.5 would 400).
#   Validator/condense pass no thinking -> any current model is fine.
_RESPONDER_MODELS = {
    "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
    "claude-opus-5", "claude-sonnet-5", "claude-sonnet-4-6",
}
_UTILITY_MODELS = _RESPONDER_MODELS | {
    "claude-haiku-4-5", "claude-haiku-4-5-20251001",
}


def _resolve_model(env_var: str, default: str, allowed: set) -> str:
    m = os.getenv(env_var, "").strip()
    if m and m not in allowed:
        _rag_logger.error(
            "%s=%r is not a recognized/compatible model id; falling back to %s",
            env_var, m, default,
        )
        return default
    return m or default


CLAUDE_MODEL = _resolve_model("CHATBOT_RESPONDER_MODEL", "claude-opus-4-6", _RESPONDER_MODELS)
VALIDATOR_MODEL = _resolve_model("CHATBOT_VALIDATOR_MODEL", "claude-haiku-4-5-20251001", _UTILITY_MODELS)
CONDENSE_MODEL = _resolve_model("CHATBOT_CONDENSE_MODEL", "claude-haiku-4-5-20251001", _UTILITY_MODELS)
TOP_K = 3
MAX_TOKENS = 2048

# Retrieval query construction. Legacy behavior concatenates the last few
# assistant replies onto the user message before embedding — which pollutes
# retrieval with OTHER courses' names/dates from the bot's own prose (see the
# course:50 vs course:100 batch-date bug). When RAG_CONDENSE_QUERY is enabled,
# a cheap LLM rewrites the follow-up into a standalone search query using history
# for reference-resolution ONLY (never carrying assistant marketing text into the
# embedding). Default off so this is a clean A/B and prod behavior is unchanged
# until explicitly enabled.
CONDENSE_QUERY_ENABLED = os.getenv("RAG_CONDENSE_QUERY", "false").strip().lower() in ("1", "true", "yes", "on")

# Routing bias: most traffic is course-related, FAQs are rare, and wrongly
# routing a course question to FAQ (→ "I don't have details" → human handoff)
# is a bad outcome. So default to courses; FAQ must win decisively on the RAW
# (un-enriched) query: a high absolute score AND a large margin over course.
FAQ_ABS_MIN = 0.5   # FAQ top score must clear this floor to be considered
FAQ_MARGIN = 0.3    # ...and beat the course score by at least this much

# Phrases that indicate the agent falsely believes a course doesn't exist
_NO_COURSE_PHRASES = [
    "don't have", "do not have", "doesn't exist", "does not exist",
    "not available", "can't find", "cannot find", "no course",
    "not in our", "isn't available", "is not available",
    "let me confirm", "i'll get you the link", "i'll check",
    "unable to find", "don't see",
]

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompt.txt"
VALIDATOR_PROMPT_PATH = Path(__file__).resolve().parent / "validator_prompt.txt"


def get_system_prompt() -> str:
    """
    Load the system prompt from file each time.
    This allows live edits without restarting the service.
    """
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def get_validator_prompt() -> str:
    """Load the validator prompt template from file each time."""
    return VALIDATOR_PROMPT_PATH.read_text(encoding="utf-8")


_clients_lock = threading.Lock()
_anthropic_client: Optional[anthropic.Anthropic] = None
_openai_client: Optional[OpenAI] = None
_pinecone_course_index = None
_pinecone_faq_index = None


def get_clients():
    global _anthropic_client, _openai_client, _pinecone_course_index, _pinecone_faq_index
    with _clients_lock:
        if _anthropic_client is None:
            _anthropic_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        if _openai_client is None:
            _openai_client = OpenAI()  # reads OPENAI_API_KEY
        if _pinecone_course_index is None or _pinecone_faq_index is None:
            pinecone_key = os.getenv("PINECONE_API_KEY")
            if not pinecone_key:
                raise RuntimeError("Missing PINECONE_API_KEY")
            pc = Pinecone(api_key=pinecone_key)
            _pinecone_course_index = pc.Index(COURSE_INDEX)
            _pinecone_faq_index = pc.Index(FAQ_INDEX)
    return _anthropic_client, _openai_client, _pinecone_course_index, _pinecone_faq_index


# ── Local course cache for name/description search ───────────────────────────
_course_cache: dict[str, dict] = {}
_course_cache_lock = threading.Lock()
_course_cache_loaded = False
_course_cache_loaded_at: float = 0.0


def _env_positive_int(name: str, default: int) -> int:
    """Read a positive int from env, falling back to default on missing/invalid."""
    try:
        v = int(os.getenv(name, "").strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# How long the in-memory course cache may serve before the next query reloads it
# from Pinecone. Env-configurable (seconds); default 6h. Lower it for faster catalog
# propagation — a reload is light (~2 list + 2 fetch calls). The indexer can also
# push updates instantly via POST /admin/refresh-course-cache?lazy=1.
COURSE_CACHE_TTL_SECONDS = _env_positive_int("CHATBOT_COURSE_CACHE_TTL", 6 * 60 * 60)

STOP_WORDS = {
    # NLTK English stop words
    "i", "me", "my", "myself", "we", "our", "ours", "ourselves",
    "you", "your", "yours", "yourself", "yourselves",
    "he", "him", "his", "himself", "she", "her", "hers", "herself",
    "it", "its", "itself", "they", "them", "their", "theirs", "themselves",
    "what", "which", "who", "whom", "this", "that", "these", "those",
    "am", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "having", "do", "does", "did", "doing",
    "a", "an", "the", "and", "but", "if", "or", "because", "as",
    "until", "while", "of", "at", "by", "for", "with", "about",
    "against", "between", "into", "through", "during", "before", "after",
    "above", "below", "to", "from", "up", "down", "in", "out", "on",
    "off", "over", "under", "again", "further", "then", "once",
    "here", "there", "when", "where", "why", "how",
    "all", "both", "each", "few", "more", "most", "other", "some", "such",
    "no", "nor", "not", "only", "own", "same", "so", "than", "too",
    "very", "can", "will", "just", "should", "now",
    # Common verb forms not in NLTK base list
    "starts", "start", "starting", "started",
    "looking", "look", "looks", "looked",
    "interested", "interest", "need", "needs", "needed",
    "know", "knows", "knowing", "knew",
    "want", "wants", "wanted", "wanting",
    "like", "likes", "liked", "liking",
    "tell", "tells", "told", "give", "gives", "given",
    "get", "gets", "got", "getting",
    "show", "shows", "showed",
    "find", "finds", "found",
    # Domain stop words (EdYoda-specific)
    "course", "courses", "url", "link", "please",
    "details", "info", "information", "list",
    "which", "where", "any", "its", "that", "this", "from",
}


# Live visibility set (source of truth = LMS DB), refreshed on every cache load.
_hidden_course_ids: set = set()
_visibility_is_live = False  # True when _hidden_course_ids came from the LMS this load


def _fetch_hidden_course_ids():
    """Fetch the course_ids currently hidden (visibility_status='no') from the LMS.

    The DB is the ultimate source of truth for visibility, so we read it live at
    each cache load rather than trusting the (possibly stale) Pinecone metadata.
    Returns a set[int], or None if the LMS is unreachable (caller then falls back
    to the indexed metadata flag).
    """
    base = os.getenv("EDYODA_LMS_BASE_URL", "https://backend.edyoda.com").rstrip("/")
    url = f"{base}/api/v1/course-visibility"
    try:
        # httpx (same client the MCP LMS tool uses): urllib's default User-Agent is
        # rejected with 403 by the backend, and httpx bundles up-to-date CA certs.
        import httpx
        resp = httpx.get(url, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        return {int(x) for x in (data.get("hidden_course_ids") or [])}
    except Exception as e:  # network/HTTP/parse — degrade gracefully
        _rag_logger.warning("[RAG] LMS visibility fetch failed (%s); using indexed flag", e)
        return None


def _is_hidden(meta) -> bool:
    """True if a course must be excluded from retrieval.

    Prefers the LIVE LMS visibility set (DB = ultimate truth). If the LMS was
    unreachable this load, falls back to the visibility_status carried in the
    Pinecone metadata (missing -> visible), so the cache is never wrongly emptied.
    """
    m = meta or {}
    if _visibility_is_live:
        return m.get("course_id") in _hidden_course_ids
    return str(m.get("visibility_status", "yes")).strip().lower() == "no"


def _load_course_cache(force: bool = False) -> None:
    global _course_cache, _course_cache_loaded, _course_cache_loaded_at
    global _hidden_course_ids, _visibility_is_live
    with _course_cache_lock:
        cache_age = time.time() - _course_cache_loaded_at
        if _course_cache_loaded and not force and cache_age < COURSE_CACHE_TTL_SECONDS:
            return
        try:
            # DB is the source of truth for visibility — fetch the live hidden set.
            hidden = _fetch_hidden_course_ids()
            _visibility_is_live = hidden is not None
            _hidden_course_ids = hidden or set()
            _, _, course_index, _ = get_clients()
            all_ids: list[str] = []
            for page in course_index.list(namespace=COURSE_NAMESPACE):
                # pinecone>=5 (v9 here) yields ListResponse objects whose
                # .vectors is a list of ListItem(id=...). Older/other shapes
                # yield a plain list of id strings, or bare id strings.
                if hasattr(page, "vectors"):
                    all_ids.extend(item.id for item in page.vectors)
                elif isinstance(page, list):
                    all_ids.extend(page)
                else:
                    all_ids.append(page)
            cache: dict[str, dict] = {}
            for i in range(0, len(all_ids), 100):
                batch = all_ids[i:i + 100]
                resp = course_index.fetch(ids=batch, namespace=COURSE_NAMESPACE)
                for vid, vec in resp.vectors.items():
                    if vec.metadata and not _is_hidden(vec.metadata):
                        cache[vid] = vec.metadata
            _course_cache = cache
            _course_cache_loaded = True
            _course_cache_loaded_at = time.time()
            _rag_logger.info(
                "[RAG] course cache loaded: %d visible courses (visibility source=%s)",
                len(cache), "LMS-DB" if _visibility_is_live else "pinecone-metadata",
            )
        except Exception as e:
            _rag_logger.warning("[RAG] course cache load failed: %s", e)


def _kw_matches(keyword: str, text: str) -> bool:
    text_low = text.lower()
    words = text_low.split()
    # Fuzzy per-token match for typos / minor inflections.
    for word in words:
        if SequenceMatcher(None, keyword, word).ratio() >= 0.82:
            return True
    # Short acronym-like keywords (<=3 chars) must hit a WORD BOUNDARY, so 'ev'
    # matches 'EV battery' and 'system(bms)' but NOT 'dEVops' / 'dEVelopment'.
    # Bare substring matching for such short tokens pollutes name_search badly.
    if len(keyword) <= 3:
        return re.search(r'\b' + re.escape(keyword) + r'\b', text_low) is not None
    # Longer keywords keep the looser substring match (e.g. 'front' -> 'frontend').
    for word in words:
        if keyword in word or word in keyword:
            return True
    return False


def refresh_course_cache() -> int:
    """Force-reload the course cache. Returns number of courses loaded."""
    _load_course_cache(force=True)
    return len(_course_cache)


def invalidate_course_cache() -> None:
    """Mark the course cache stale so the NEXT query reloads it from Pinecone.

    Cheap and idempotent: a burst of calls (e.g. a bulk re-index pinging after each
    course) coalesces into a single reload on the next user message, rather than N
    full reloads. Used by POST /admin/refresh-course-cache?lazy=1.
    """
    global _course_cache_loaded_at
    with _course_cache_lock:
        _course_cache_loaded_at = 0.0


def _extract_keywords(query: str) -> list[str]:
    """
    Extract meaningful keywords from query.
    Splits compound tokens like 'aws+gcp+azure' → ['aws', 'gcp', 'azure'].
    """
    tokens = query.lower().split()
    keywords: list[str] = []
    for token in tokens:
        # Split compound tokens on +, /, &, comma
        parts = re.split(r'[+/&,]', token)
        for part in parts:
            part = part.strip("()")
            if len(part) >= 2 and part not in STOP_WORDS:
                keywords.append(part)
    return keywords


def _name_search(query: str) -> list[str]:
    """Return course IDs sorted by name/description relevance to the query."""
    _load_course_cache()
    keywords = _extract_keywords(query)
    if not keywords:
        return []

    scored: list[tuple[str, float]] = []
    for course_id, meta in _course_cache.items():
        name = meta.get("name", "")
        description = meta.get("description", "")
        score = 0.0
        for kw in keywords:
            if _kw_matches(kw, name):
                score += 2.0
            elif _kw_matches(kw, description):
                score += 1.0
        # phrase similarity bonus
        phrase = " ".join(keywords)
        ps = SequenceMatcher(None, phrase.lower(), name.lower()).ratio()
        if ps >= 0.4:
            score += ps * 2

        # name coverage bonus: fraction of name words explained by query keywords
        # rewards courses where the query covers most of the name (near-exact match)
        # e.g. "multi cloud architect" covers 3/4 words of "Multi Cloud Architect (AWS+AZURE+GCP)"
        # but only 3/6 words of "Multi Cloud Architect for GenAI Infra"
        name_words = name.lower().split()
        if name_words:
            matched = sum(1 for nw in name_words if any(_kw_matches(kw, nw) for kw in keywords))
            coverage = matched / len(name_words)
            score += coverage * 3   # weight=3 so it dominates duration tiebreaker

        # duration tiebreaker (smallest weight — only separates truly equal scores)
        m = re.search(r'(\d+)[\s-]week', description, re.IGNORECASE)
        if m:
            score += int(m.group(1)) / 1000
        if score > 0:
            scored.append((course_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [cid for cid, _ in scored]


def embed_query(text: str) -> list[float]:
    _, openai_client, _, _ = get_clients()
    response = openai_client.embeddings.create(model=EMBED_MODEL, input=text)
    return response.data[0].embedding


def retrieve_courses_vector(vector: list[float]):
    _, _, course_index, _ = get_clients()
    results = course_index.query(
        vector=vector,
        top_k=TOP_K,
        include_metadata=True,
        namespace=COURSE_NAMESPACE,
    )
    return results


def retrieve_faqs_vector(vector: list[float]):
    _, _, _, faq_index = get_clients()
    results = faq_index.query(
        vector=vector,
        top_k=TOP_K,
        include_metadata=True,
        namespace=FAQ_NAMESPACE,
    )
    return results


def route_and_retrieve(query: str, raw_query: str | None = None) -> tuple[str, list[dict], float, float]:
    """
    Route: compare top scores from courses vs faqs and choose index.
    For courses: merges vector results with name/description search so
    courses with weak embeddings still surface when their name matches.
    Returns (mode, items, raw_course_score, raw_faq_score).
    raw_query: if provided, score using raw user input (not enriched) for deflection detection.
    """
    vector = embed_query(query)
    course_results = retrieve_courses_vector(vector)
    faq_results = retrieve_faqs_vector(vector)

    course_score = course_results.matches[0].score if course_results.matches else 0.0
    faq_score = faq_results.matches[0].score if faq_results.matches else 0.0

    # Compute raw scores from original user input (unaffected by history enrichment)
    if raw_query and raw_query != query:
        raw_vector = embed_query(raw_query)
        raw_course_results = retrieve_courses_vector(raw_vector)
        raw_faq_results = retrieve_faqs_vector(raw_vector)
        raw_course_score = raw_course_results.matches[0].score if raw_course_results.matches else 0.0
        raw_faq_score = raw_faq_results.matches[0].score if raw_faq_results.matches else 0.0
    else:
        raw_course_score, raw_faq_score = course_score, faq_score

    # Decide mode on the RAW query scores (undiluted by history enrichment):
    # FAQ only wins if it clears an absolute floor AND beats course by a margin.
    # Otherwise default to courses. Item retrieval below still uses the enriched
    # vector, so multi-turn follow-ups keep surfacing the right specific courses.
    mode = "faqs" if (raw_faq_score >= FAQ_ABS_MIN and
                      raw_faq_score > raw_course_score + FAQ_MARGIN) else "courses"
    _rag_logger.info(
        "[RAG] query='%s' | top_course_score=%.4f | top_faq_score=%.4f | raw_course_score=%.4f | raw_faq_score=%.4f | mode=%s",
        query, course_score, faq_score, raw_course_score, raw_faq_score, mode,
    )

    if mode == "faqs":
        return "faqs", [m.metadata for m in faq_results.matches if m.metadata], raw_course_score, raw_faq_score

    # ── Course mode: merge vector results with name/description search ─────
    # 1. Name/description hits (ordered by relevance score)
    name_hit_ids = _name_search(query)
    _rag_logger.info("[RAG] name_search hits (top 5): %s", name_hit_ids[:5])

    seen: set[str] = set()
    merged: list[dict] = []

    # Name hits first
    for cid in name_hit_ids:
        if cid in _course_cache and cid not in seen:
            seen.add(cid)
            meta = _course_cache[cid]
            merged.append(meta)
            if len(merged) <= TOP_K:
                _rag_logger.info(
                    "[RAG] name_hit id=%s name='%s' course_id=%s",
                    cid, meta.get("name", "?"), meta.get("course_id", "?"),
                )

    # Vector results fill remaining slots (hidden courses filtered out — the cache
    # already omits them, but vector matches come straight from Pinecone).
    for m in course_results.matches:
        if m.id not in seen and m.metadata and not _is_hidden(m.metadata):
            seen.add(m.id)
            merged.append(m.metadata)
            _rag_logger.info(
                "[RAG] vector_hit id=%s score=%.4f name='%s' course_id=%s",
                m.id, m.score, m.metadata.get("name", "?"), m.metadata.get("course_id", "?"),
            )

    return "courses", merged[:TOP_K], raw_course_score, raw_faq_score


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    # simple dot since vectors are normalized by OpenAI embeddings
    return sum(x * y for x, y in zip(a, b))


def select_faq_image_for_query(query: str, min_score: float = 0.32) -> tuple[str, str] | None:
    """
    Query the FAQ index and select the single most relevant image based on
    caption similarity to the query. Returns (image_url, caption) or None.
    """
    vector = embed_query(query)
    faq_results = retrieve_faqs_vector(vector)
    best_url: str | None = None
    best_cap: str = ""
    best_score = 0.0
    for m in faq_results.matches or []:
        meta = m.metadata or {}
        urls = meta.get("image_urls") or []
        caps = meta.get("image_captions") or []
        if not urls or not caps:
            continue
        # evaluate captions
        for idx, cap in enumerate(caps):
            try:
                cap_vec = embed_query(str(cap)[:512])
                score = _cosine(vector, cap_vec)
                if score > best_score and idx < len(urls):
                    best_score = score
                    best_url = urls[idx]
                    best_cap = str(cap)
            except Exception:
                continue
    if best_url and best_score >= min_score:
        return best_url, best_cap
    return None


def select_faq_image_from_items(items: list[dict], query: str, min_score: float = 0.5) -> tuple[str, str] | None:
    """
    Select an image ONLY from the provided FAQ items (already retrieved for this query).
    This ensures we don't pick unrelated images from other FAQs.
    """
    if not items:
        return None
    q_vec = embed_query(query)
    best_url: str | None = None
    best_cap: str = ""
    best_score = 0.0
    for meta in items:
        try:
            urls = meta.get("image_urls") or []
            caps = meta.get("image_captions") or []
            if not urls or not caps:
                continue
            for idx, cap in enumerate(caps):
                cap_vec = embed_query(str(cap)[:512])
                score = _cosine(q_vec, cap_vec)
                if score > best_score and idx < len(urls):
                    best_score = score
                    best_url = urls[idx]
                    best_cap = str(cap)
        except Exception:
            continue
    if best_url and best_score >= min_score:
        return best_url, best_cap
    return None

def _infer_geo_from_waid(waid: str) -> tuple[str, str]:
    """
    Infer ISD and country from WhatsApp waId (e.g., '91890...').
    Returns (isd, country) or ('', '') if unknown.
    """
    digits = "".join(ch for ch in (waid or "") if ch.isdigit())
    if digits.startswith("91"):
        return "91", "India"
    if digits.startswith("44"):
        return "44", "United Kingdom"
    if digits.startswith("1"):
        # Ambiguous (US/Canada); default to US
        return "1", "United States"
    return "", ""


def _maybe_json_list(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def format_context(courses: list[dict]) -> str:
    if not courses:
        return "No relevant courses found."

    parts = []
    for i, c in enumerate(courses, 1):
        lines = [f"--- Course {i}: {c.get('name', 'Unknown')} ---"]

        if c.get("description"):
            lines.append(f"Description: {c['description']}")
        if c.get("course_type"):
            lines.append(f"Type: {c['course_type']}")
        if c.get("course_url"):
            full_url = f"https://www.edyoda.com{c['course_url']}"
            lines.append(f"Course URL: {full_url}")

        mentors = _maybe_json_list(c.get("mentors"))
        if mentors and isinstance(mentors, list):
            lines.append("Mentors: " + " | ".join(mentors))

        cohorts = _maybe_json_list(c.get("cohorts"))
        if cohorts and isinstance(cohorts, list):
            lines.append("Cohorts: " + "; ".join(cohorts))

        prereqs = _maybe_json_list(c.get("prerequisites"))
        if prereqs and isinstance(prereqs, list):
            lines.append("Prerequisites: " + ", ".join(prereqs))

        skills = _maybe_json_list(c.get("curriculum_skills"))
        if skills and isinstance(skills, list):
            lines.append("Skills you'll gain:\n  - " + "\n  - ".join(skills))

        paths = _maybe_json_list(c.get("career_pathways"))
        if paths and isinstance(paths, list):
            lines.append("Career pathways: " + ", ".join(paths))

        projects = _maybe_json_list(c.get("projects"))
        if projects and isinstance(projects, list):
            lines.append("Projects: " + " | ".join(projects))

        faqs = _maybe_json_list(c.get("faqs"))
        if faqs and isinstance(faqs, list) and faqs:
            faq_lines = [f"  Q: {faq}" for faq in faqs[:3]]
            lines.append("Sample FAQs:\n" + "\n".join(faq_lines))

        audience = _maybe_json_list(c.get("target_audiance"))
        if audience and isinstance(audience, list):
            lines.append("Target audience: " + ", ".join(audience))

        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def format_faq_context(faqs: list[dict]) -> str:
    if not faqs:
        return "No relevant FAQs found."
    parts = []
    for i, f in enumerate(faqs, 1):
        q = f.get("question", "") or ""
        a = f.get("answer_snippet", "") or ""
        lines = [f"--- FAQ {i} ---", f"Q: {q}", f"A: {a}"]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _extract_urls(text: str) -> list[str]:
    return re.findall(r'https://www\.edyoda\.com[^\s\)\"\']+', text)


def _url_valid_in_cache(url: str) -> bool:
    """Return True if the URL path matches a known course_url in the cache."""
    for meta in _course_cache.values():
        slug = meta.get("course_url", "")
        if slug and slug in url:
            return True
    return False


_PHONE_RE = re.compile(r'(?<!\d)\+?[\d][\d\s\-\.\(\)]{6,16}[\d](?!\d)')


def _extract_phone_numbers(text: str) -> list[str]:
    """Extract phone-like sequences from text as normalized digit-only strings."""
    seen: set[str] = set()
    result: list[str] = []
    for match in _PHONE_RE.findall(text):
        digits = re.sub(r'\D', '', match)
        if len(digits) < 7 or len(digits) > 15:
            continue
        if len(set(digits)) <= 1:
            continue
        if digits not in seen:
            seen.add(digits)
            result.append(digits)
    return result


def _normalize_phone(digits: str) -> str:
    """Strip leading country code so +91XXXXXXXXXX and XXXXXXXXXX compare equal."""
    if len(digits) == 12 and digits.startswith('91'):
        return digits[2:]
    if len(digits) == 11 and digits.startswith('1'):
        return digits[1:]
    return digits[-10:] if len(digits) > 10 else digits


# EdYoda's authorized customer-care numbers (from the system prompt contact block).
# Seeded into the validator's known set so legitimate contacts — notably the Direct
# Hotline — are never stripped as "unverified" when the FAQ index doesn't return them.
# Stored normalized to 10 digits (see _normalize_phone).
_AUTHORIZED_CONTACT_NUMBERS = {"8045682485", "8904512659", "9663357054"}


def _get_faq_phone_numbers() -> set[str]:
    """Query FAQ index for contact/support entries and return all known phone numbers.

    Seeded with _AUTHORIZED_CONTACT_NUMBERS so EdYoda's real support lines are always
    recognized even if the FAQ lookup fails or omits them.
    """
    known: set[str] = set(_AUTHORIZED_CONTACT_NUMBERS)
    try:
        vec = embed_query("contact phone number support helpline call us")
        faq_results = retrieve_faqs_vector(vec)
        for m in faq_results.matches or []:
            meta = m.metadata or {}
            text = (meta.get("answer_snippet") or "") + " " + (meta.get("question") or "")
            for digits in _extract_phone_numbers(text):
                known.add(_normalize_phone(digits))
        _rag_logger.info("[VALIDATOR] known FAQ phone numbers: %s", known)
    except Exception as e:
        _rag_logger.warning("[VALIDATOR] phone number FAQ lookup failed: %s", e)
    return known


def _build_verified_course_lines(course_ids: list[str]) -> str:
    lines = []
    for cid in course_ids:
        meta = _course_cache.get(cid, {})
        name = meta.get("name", "")
        slug = meta.get("course_url", "")
        url = f"https://www.edyoda.com{slug}" if slug else "(no URL)"
        lines.append(f"- {name}: {url}")
    return "\n".join(lines)


def validate_and_correct(query: str, course_items: list[dict], response: str, conversation_history: list[dict] | None = None) -> str:
    """
    Validator agent (Haiku) — runs after main agent.
    Checks for:
      1. Hallucinated/unverified URLs
      2. False claims that a course doesn't exist
    Rewrites only if issues found.
    """
    _load_course_cache()
    issues: list[str] = []

    # 1. Check every edyoda URL in the response against the cache
    urls_in_response = _extract_urls(response)
    bad_urls = [u for u in urls_in_response if not _url_valid_in_cache(u)]
    if bad_urls:
        issues.append(f"Unverified URLs (not in course database): {bad_urls}")

    # 2. Detect false "course doesn't exist" claims then look up cache
    response_lower = response.lower()
    false_negative = any(phrase in response_lower for phrase in _NO_COURSE_PHRASES)
    cache_hit_ids: list[str] = []
    if false_negative or bad_urls:
        cache_hit_ids = _name_search(query)[:3]
        if cache_hit_ids and false_negative:
            names = [_course_cache[c].get("name", c) for c in cache_hit_ids]
            issues.append(f"Agent said course unavailable but cache found: {names}")

    # 3. Check phone numbers in response against FAQ data, and enforce country code
    phones_in_response = _extract_phone_numbers(response)
    if phones_in_response:
        known_phones = _get_faq_phone_numbers()
        bad_phones = [p for p in phones_in_response if _normalize_phone(p) not in known_phones]
        if bad_phones:
            issues.append(f"Unverified phone numbers (not found in FAQ database): {bad_phones}")
        # Flag verified numbers shared WITHOUT a country code (bare 10-digit).
        # _extract_phone_numbers returns digits only, so a country code shows up as
        # extra leading digits (e.g. 91XXXXXXXXXX); a bare 10-digit number has none.
        missing_cc = [p for p in phones_in_response if _normalize_phone(p) in known_phones and len(p) <= 10]
        if missing_cc:
            issues.append(f"Phone numbers missing country code +91 (all EdYoda customer care is India-based): {missing_cc}")

    if not issues:
        _rag_logger.info("[VALIDATOR] response OK — no issues found")
        return response

    _rag_logger.info("[VALIDATOR] issues found: %s", issues)

    # Build verified course reference from cache hits + original context
    verified_lines: list[str] = []
    if cache_hit_ids:
        verified_lines.append(_build_verified_course_lines(cache_hit_ids))
    for item in course_items:
        slug = item.get("course_url", "")
        name = item.get("name", "")
        if slug and name:
            verified_lines.append(f"- {name}: https://www.edyoda.com{slug}")

    verified_block = "\n".join(verified_lines) or "No verified courses available."

    anthropic_client, _, _, _ = get_clients()
    # Format conversation history for the validator
    history_block = ""
    if conversation_history:
        history_lines = []
        for m in conversation_history[:-1]:  # exclude the current user message (already in query)
            role = m.get("role", "")
            text = m.get("content", "")
            if role == "user":
                history_lines.append(f"User: {text}")
            elif role == "assistant":
                history_lines.append(f"Aman: {text}")
        if history_lines:
            history_block = "Conversation so far:\n" + "\n".join(history_lines[-10:]) + "\n\n"

    issues_block = "\n".join(f"- {i}" for i in issues)
    validator_prompt = get_validator_prompt().format(
        history_block=history_block,
        query=query,
        issues=issues_block,
        verified_block=verified_block,
        draft_response=response,
    )

    try:
        result = anthropic_client.messages.create(
            model=VALIDATOR_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": validator_prompt}],
        )
        corrected = next((b.text for b in result.content if b.type == "text"), "").strip()
        _rag_logger.info("[VALIDATOR] response corrected")
        return corrected if corrected else response
    except Exception as e:
        _rag_logger.warning("[VALIDATOR] correction failed, using original: %s", e)
        return response


DEFLECT_SCORE_THRESHOLD = 0.35


def _condense_query(user_input: str, past_messages: list[dict]) -> str:
    """Rewrite a follow-up into a standalone retrieval query using recent history.

    History is used ONLY to resolve references (pronouns, 'it', 'that course',
    'when does it start') — the OUTPUT is a fresh, self-contained search query, so
    the bot's own marketing prose (other course names/dates) never enters the
    embedding. Falls back to the raw user_input when there's no history or on any
    error, so retrieval never hard-fails.
    """
    if not past_messages:
        return user_input
    # Compact transcript of the last few turns as REWRITE CONTEXT only.
    turns = []
    for m in past_messages[-6:]:
        role = "User" if m.get("role") == "user" else "Assistant"
        text = (m.get("content") or "").strip().replace("\n", " ")
        if text:
            turns.append(f"{role}: {text[:500]}")
    if not turns:
        return user_input
    transcript = "\n".join(turns)
    prompt = (
        "You rewrite a user's latest message into a standalone search query for an "
        "online course catalog. Use the conversation ONLY to resolve references "
        "(pronouns, 'it', 'that course', 'when does it start').\n"
        "Rules:\n"
        "- Output ONLY the query text — no preamble, no explanation, no quotes.\n"
        "- Include the specific course name/topic the user means now plus what they "
        "want to know.\n"
        "- Do NOT introduce any course or detail the user did not refer to.\n"
        "- If the message is a greeting, thanks, goodbye, or is NOT about a specific "
        "course, output it EXACTLY as-is, unchanged.\n"
        "- If the message is already standalone, output it unchanged.\n\n"
        "Examples:\n"
        "Latest: 'when does it start?' (after discussing AI Agent Development Specialist)\n"
        "-> AI Agent Development Specialist course start date\n"
        "Latest: 'bye'\n"
        "-> bye\n"
        "Latest: 'thanks!'\n"
        "-> thanks!\n\n"
        f"Conversation:\n{transcript}\n\n"
        f"Latest user message: {user_input}\n\n"
        "Standalone search query:"
    )
    try:
        anthropic_client, _, _, _ = get_clients()
        result = anthropic_client.messages.create(
            model=CONDENSE_MODEL,
            max_tokens=60,
            messages=[{"role": "user", "content": prompt}],
        )
        q = next((b.text for b in result.content if b.type == "text"), "").strip().strip('"\'')
        # Guard: reject a runaway/meta rewrite (condenser explaining instead of
        # rewriting). If the output balloons well beyond the raw message, fall back.
        if not q or len(q) > max(160, len(user_input) * 6):
            return user_input
        return q
    except Exception as e:
        _rag_logger.warning("[RAG] condense failed, using raw input: %s", e)
        return user_input


def chat(session_id: str, user_input: str) -> tuple[str, bool]:
    anthropic_client, _, _, _ = get_clients()

    # Load history first so we can enrich the RAG query with recent context
    past_messages = get_recent_messages(session_id)

    # Build the retrieval query from recent context so follow-up questions
    # (e.g. 'when does it start?', 'share course content for each') retrieve the
    # right courses. Two strategies:
    #   - condense (RAG_CONDENSE_QUERY on): LLM rewrites the follow-up into a clean
    #     standalone query; assistant prose never enters the embedding.
    #   - legacy (default): concatenate the last 4 assistant replies (pollutes
    #     retrieval with other courses' names/dates — the course:50/100 bug).
    if CONDENSE_QUERY_ENABLED:
        enriched_query = _condense_query(user_input, past_messages)
        if enriched_query != user_input:
            _rag_logger.info("[RAG] condensed query='%s' (raw='%s')", enriched_query, user_input)
    else:
        asst_texts = [m['content'] for m in past_messages if m.get('role') == 'assistant'][-4:]
        enriched_query = (user_input + ' ' + ' '.join(asst_texts)).strip() if asst_texts else user_input

    mode, items, raw_course_score, raw_faq_score = route_and_retrieve(enriched_query, raw_query=user_input)
    isd, country = _infer_geo_from_waid(session_id)
    geo_block = f"<user_geo>isd={isd or 'unknown'}; country={country or 'unknown'}</user_geo>\n"
    if mode == "faqs":
        context_block = format_faq_context(items)
        user_message_with_context = (
            geo_block +
            f"<faq_context>\n{context_block}\n</faq_context>\n\n"
            f"User question: {user_input}"
        )
    else:
        context_block = format_context(items)
        user_message_with_context = (
            geo_block +
            f"<course_context>\n{context_block}\n</course_context>\n\n"
            f"User question: {user_input}"
        )

    # Customer-name context (from CRM, checked once per conversation). Injects a
    # short line telling the responder the name, or to ask for it; and — while the
    # name is unknown — exposes the save_customer_name tool below. No-op when the
    # feature is off or the CRM is unreachable (fail open).
    name_status, _cust_name = name_capture.resolve(session_id)
    _name_ctx = name_capture.context_line(name_status, _cust_name)
    if _name_ctx:
        user_message_with_context = f"<customer>{_name_ctx}</customer>\n" + user_message_with_context

    # History already loaded above for RAG enrichment
    # Sanitize stored messages: Anthropic API only allows role/content keys
    api_messages: list[dict[str, str]] = []
    for m in past_messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if isinstance(role, str) and isinstance(content, str):
            api_messages.append({"role": role, "content": content})
    api_messages.append({"role": "user", "content": user_message_with_context})
    # Snapshot the string-only history for the validator (before any tool blocks).
    history_for_validator = list(api_messages)

    # MCP tools (best-effort): [] if the MCP server is unavailable, in which case
    # chat() behaves exactly as before (no tools passed). While the caller's name
    # is unknown we also expose save_customer_name (local, not MCP).
    tools = list(get_mcp_tools() or []) + name_capture.tool_schema(name_status)

    def _invoke(msgs):
        """One messages.create with retry/backoff. Returns the response or raises."""
        err: Optional[Exception] = None
        for attempt in range(1, 6):
            try:
                kwargs = dict(
                    model=CLAUDE_MODEL,
                    max_tokens=MAX_TOKENS,
                    system=get_system_prompt(),
                    thinking={"type": "adaptive"},
                    messages=msgs,
                )
                if tools:
                    kwargs["tools"] = tools
                return anthropic_client.messages.create(**kwargs)
            except anthropic.BadRequestError:
                raise
            except Exception as e:
                err = e
                sleep_s = min(10.0, (0.5 * (2 ** (attempt - 1))) + random.random() * 0.25)
                time.sleep(sleep_s)
        raise err

    # Tool-use loop: let Claude call MCP tools (e.g. get_batch_start_date) and feed
    # results back until it produces a final answer. Capped to avoid runaway loops.
    # `tool_context` carries the verified caller identity (Tier-2 seam); Tier-1
    # tools ignore it and it is never exposed to the model.
    tool_context = {"waid": session_id}
    no_batch_signal = False  # set when get_batch_start_date reports no upcoming batch
    response = _invoke(api_messages)
    for _ in range(3):
        if getattr(response, "stop_reason", None) != "tool_use":
            break
        api_messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                if name_capture.is_tool(block.name):
                    out = name_capture.handle(session_id, block.input)
                else:
                    out = mcp_call_tool(block.name, block.input, context=tool_context)
                _rag_logger.info("[TOOL] %s(%s) -> %.160s", block.name, block.input, out.replace("\n", " "))
                if block.name == "get_batch_start_date" and (
                    "no upcoming batch" in out.lower() or "no batch is scheduled" in out.lower()
                ):
                    no_batch_signal = True
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": out,
                })
        if not tool_results:
            break
        api_messages.append({"role": "user", "content": tool_results})
        response = _invoke(api_messages)

    reply = next((block.text for block in response.content if getattr(block, "type", None) == "text"), "")

    # Validator agent: check URLs, phone numbers, and false "doesn't exist" claims.
    # Runs for both modes — FAQ responses can contain phone numbers that need verification.
    course_items_for_validator = items if mode == "courses" else []
    reply = validate_and_correct(user_input, course_items_for_validator, reply, conversation_history=history_for_validator)

    # Deterministic guarantee: when the batch tool reported no upcoming batch, the
    # user must be directed to customer support for the earliest date. The model's
    # conversion/gating conditioning otherwise drops this, so we ensure it here
    # (append only if the support number isn't already present). Runs after the
    # validator; +91-8045682485 is an authorized number (see _AUTHORIZED_CONTACT_NUMBERS).
    if no_batch_signal and "8045682485" not in reply:
        reply = reply.rstrip() + (
            "\n\nThe batch dates aren't published yet — please call our customer "
            "support at +91-8045682485 to know the earliest date."
        )

    # Persist to Redis (trim to cap, set TTL)
    append_messages(session_id, user_input, reply)

    # Deflection: low RAG score (out-of-domain) OR known deflection phrase in reply
    score_deflected = max(raw_course_score, raw_faq_score) < DEFLECT_SCORE_THRESHOLD
    phrase_deflected = any(p in reply.lower() for p in [
        "don't have", "do not have", "don't offer", "do not offer",
        "doesn't exist", "not available", "can't find", "cannot find",
        "not in our", "unable to find", "isn't something we",
        "not something we", "we don't cover", "outside our", "beyond our",
        "not part of our", "currently busy", "try again in a minute",
    ])
    is_deflected = score_deflected or phrase_deflected
    _rag_logger.info("[DEFLECT] score_deflected=%s phrase_deflected=%s is_deflected=%s", score_deflected, phrase_deflected, is_deflected)
    return reply, is_deflected


def ask(session_id: str, question: str) -> str:
    reply, _ = chat(session_id, question)
    return reply


def chat_with_image(session_id: str, image_bytes: bytes, media_type: str, caption: str = "") -> str:
    """
    Process an inbound image from the user.
    Skips RAG (no text to embed) — sends image + system prompt + history to Claude vision.
    caption: any text the user included alongside the image (often empty).
    """
    import base64
    anthropic_client, _, _, _ = get_clients()
    isd, country = _infer_geo_from_waid(session_id)
    geo_block = f"<user_geo>isd={isd or 'unknown'}; country={country or 'unknown'}</user_geo>\n"

    user_text = caption.strip() if caption.strip() else "I've shared an image."
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    past_messages = get_recent_messages(session_id)
    api_messages: list[dict] = []
    for m in past_messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if isinstance(role, str) and isinstance(content, str):
            api_messages.append({"role": role, "content": content})

    # Current message: image + optional caption
    api_messages.append({
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_b64,
                },
            },
            {
                "type": "text",
                "text": (
                    geo_block +
                    "The user shared this image. Analyze it in the context of EdYoda programs. "
                    "If it's a resume or profile — suggest the most relevant EdYoda program. "
                    "If it's a certificate or course syllabus — acknowledge it and bridge to EdYoda. "
                    "If it's an error or technical screenshot — help if you can, then connect to a relevant program. "
                    "If it's unrelated — politely redirect to EdYoda programs.\n\n"
                    f"User caption: {user_text}"
                ),
            },
        ],
    })

    last_err: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            response = anthropic_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=MAX_TOKENS,
                system=get_system_prompt(),
                messages=api_messages,
            )
            last_err = None
            break
        except anthropic.BadRequestError as e:
            last_err = e
            break
        except Exception as e:
            last_err = e
            sleep_s = min(10.0, (0.5 * (2 ** (attempt - 1))) + random.random() * 0.25)
            time.sleep(sleep_s)

    if last_err is not None:
        raise last_err

    reply = next((block.text for block in response.content if block.type == "text"), "")
    # Store with a placeholder so history stays coherent
    stored_input = f"[image]{(' ' + caption.strip()) if caption.strip() else ''}"
    append_messages(session_id, stored_input, reply)
    _rag_logger.info("[RAG] image processed session=%s reply_len=%d", session_id, len(reply))
    return reply


def ask_with_image(session_id: str, image_bytes: bytes, media_type: str, caption: str = "") -> str:
    return chat_with_image(session_id, image_bytes, media_type, caption)


def ask_with_media(session_id: str, question: str) -> tuple[str, tuple[str, str] | None, bool]:
    """
    Returns (reply_text, (image_url, caption) | None, is_deflected).
    Image is selected ONLY from the retrieved FAQ items for this query,
    and only when the router chose 'faqs'. This avoids unrelated images.
    """
    reply, is_deflected = chat(session_id, question)
    # Route again to pick media — reuses cached embeddings in practice
    mode, items, _, _ = route_and_retrieve(question)
    media: tuple[str, str] | None = None
    if mode == "faqs" and items:
        media = select_faq_image_from_items(items, question, min_score=0.5)
    return reply, media, is_deflected

