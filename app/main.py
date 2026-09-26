from __future__ import annotations

import logging
import os
import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin, parse_qs, urlparse

import httpx
from anyio import to_thread
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.edyoda_chatbot import ask as edyoda_ask
from app.edyoda_chatbot import ask_with_media as edyoda_ask_with_media
from app.edyoda_chatbot import ask_with_image as edyoda_ask_with_image
from app.edyoda_chatbot import embed_query, retrieve_faqs_vector, refresh_course_cache, invalidate_course_cache
from app import name_capture
from pathlib import Path
from app.chat_history import (
    update_user_activity,
    mark_reassigned_to_bot,
    get_session_meta,
)
from app.chat_history import _get_redis, REDIS_KEY_PREFIX  # type: ignore
from app.db import init_db, log_conversation
from app.chat_history import META_KEY_PREFIX  # type: ignore


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    wati_api_endpoint: str = ""
    wati_access_token: str = ""
    # Support both env var spellings (legacy + corrected).
    edyoda_rag_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("EDYODA_RAG_ENABLED", "EDUYODA_RAG_ENABLED"),
    )
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    pinecone_api_key: str = ""
    admin_token: str = ""  # required to edit system prompt via browser
    deflect_alert_numbers: str = ""  # comma-separated WhatsApp numbers to alert on deflected chats


settings = Settings()

app = FastAPI(title="WATI FastAPI Auto-Reply", version="0.1.0")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wati_app")

# Background task controls
_bg_reassign_task: Optional[asyncio.Task] = None
_bg_stop_event: Optional[asyncio.Event] = None
_REASSIGN_LOOP_INTERVAL_SEC = 60  # run every 60s
_DEBOUNCE_WINDOW_SEC = 10.0  # coalesce rapid multi-part user messages

# When running under sudo (for port 80), shell env vars may be dropped.
# Ensure RAG dependencies can still read API keys.
if settings.openai_api_key and not os.getenv("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = settings.openai_api_key
if settings.anthropic_api_key and not os.getenv("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
if settings.pinecone_api_key and not os.getenv("PINECONE_API_KEY"):
    os.environ["PINECONE_API_KEY"] = settings.pinecone_api_key

logger.info(
    "RAG key visibility (openai=%s anthropic=%s pinecone=%s) edyoda_rag_enabled=%s",
    bool(os.getenv("OPENAI_API_KEY")),
    bool(os.getenv("ANTHROPIC_API_KEY")),
    bool(os.getenv("PINECONE_API_KEY")),
    settings.edyoda_rag_enabled,
)

from app.edyoda_chatbot import CLAUDE_MODEL, VALIDATOR_MODEL, CONDENSE_MODEL
logger.info(
    "Chatbot models (env-configurable): responder=%s validator=%s condense=%s",
    CLAUDE_MODEL, VALIDATOR_MODEL, CONDENSE_MODEL,
)


class WatiWebhookMessage(BaseModel):
    eventType: Optional[str] = None
    owner: Optional[bool] = None
    waId: Optional[str] = None
    text: Optional[str] = None
    whatsappMessageId: Optional[str] = None
    channelPhoneNumber: Optional[str] = None

    # allow unknown fields from WATI
    model_config = {"extra": "allow"}


async def send_wati_text(
    *,
    whatsapp_number: str,
    message_text: str,
    reply_context_id: Optional[str],
    channel_phone_number: Optional[str],
) -> None:
    if not settings.wati_api_endpoint or not settings.wati_access_token:
        raise HTTPException(
            status_code=500,
            detail="Missing WATI_API_ENDPOINT or WATI_ACCESS_TOKEN (set them in .env or environment variables).",
        )

    base = settings.wati_api_endpoint.rstrip("/")
    url = urljoin(f"{base}/", f"api/v1/sendSessionMessage/{whatsapp_number}")
    headers = {"Authorization": f"Bearer {settings.wati_access_token}"}
    params: dict[str, Any] = {"messageText": message_text}
    if reply_context_id:
        params["replyContextId"] = reply_context_id
    if channel_phone_number:
        params["channelPhoneNumber"] = channel_phone_number

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, headers=headers, params=params)
        if resp.status_code >= 400:
            # Don't leak token; log enough to debug.
            body_preview = resp.text[:2000] if resp.text else ""
            raise HTTPException(status_code=502, detail={"wati_status": resp.status_code, "wati_body_preview": body_preview})


async def send_wati_image(
    *,
    whatsapp_number: str,
    media_url: str,
    caption: Optional[str],
    channel_phone_number: Optional[str],
) -> None:
    """
    Best-effort image send using WATI v1 endpoints.
    Order:
      1) sendSessionFile (multipart upload)
      2) sendSessionImage with imageLink
      3) sendMediaMessage JSON with mediaLink
    """
    if not settings.wati_api_endpoint or not settings.wati_access_token:
        raise HTTPException(
            status_code=500,
            detail="Missing WATI_API_ENDPOINT or WATI_ACCESS_TOKEN (set them in .env or environment variables).",
        )
    base = settings.wati_api_endpoint.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.wati_access_token}"}
    async with httpx.AsyncClient(timeout=20) as client:
        # Attempt 1: V1 sendSessionFile (multipart upload)
        try:
            url_file = urljoin(f"{base}/", f"api/v1/sendSessionFile/{whatsapp_number}")
            paramsf: dict[str, Any] = {}
            if channel_phone_number:
                paramsf["channelPhoneNumber"] = channel_phone_number
            dl2 = await client.get(media_url)
            if dl2.status_code < 400 and dl2.content:
                files2 = {
                    "file": (
                        "image",
                        dl2.content,
                        dl2.headers.get("content-type") or "application/octet-stream",
                    )
                }
                respf = await client.post(url_file, headers=headers, params=paramsf, files=files2)
                if respf.status_code < 400:
                    logger.info("send_wati_image success: waId=%s endpoint=%s (v1 sendSessionFile)", whatsapp_number, url_file)
                    return
                else:
                    logger.info(
                        "send_wati_image v1 sendSessionFile failed: waId=%s status=%s endpoint=%s body_preview=%s",
                        whatsapp_number,
                        respf.status_code,
                        url_file,
                        (respf.text[:2000] if respf.text else ""),
                    )
            else:
                logger.info(
                    "send_wati_image download failed: url=%s status=%s",
                    media_url,
                    dl2.status_code,
                )
        except Exception as e:
            logger.info("send_wati_image v1 sendSessionFile attempt errored: %s", e)
        # Attempt 2: sendSessionImage with imageLink (some tenants require imageLink, not imageUrl)
        url1 = urljoin(f"{base}/", f"api/v1/sendSessionImage/{whatsapp_number}")
        params1: dict[str, Any] = {"imageLink": media_url}
        if channel_phone_number:
            params1["channelPhoneNumber"] = channel_phone_number
        resp = await client.post(url1, headers=headers, params=params1)
        if resp.status_code < 400:
            logger.info("send_wati_image success: waId=%s endpoint=%s", whatsapp_number, url1)
            return
        body_preview = resp.text[:2000] if resp.text else ""
        logger.info(
            "send_wati_image v1 sendSessionImage failed: waId=%s status=%s endpoint=%s media_url=%s body_preview=%s",
            whatsapp_number,
            resp.status_code,
            url1,
            media_url,
            body_preview,
        )
        # Attempt 3: sendMediaMessage with JSON body (mediaType/ mediaLink)
        url2 = urljoin(f"{base}/", f"api/v1/sendMediaMessage/{whatsapp_number}")
        json2: dict[str, Any] = {"mediaType": "IMAGE", "mediaLink": media_url}
        if channel_phone_number:
            json2["channelPhoneNumber"] = channel_phone_number
        resp2 = await client.post(url2, headers=headers, json=json2)
        if resp2.status_code < 400:
            logger.info("send_wati_image success: waId=%s endpoint=%s", whatsapp_number, url2)
            return
        body_preview2 = resp2.text[:2000] if resp2.text else ""
        logger.info(
            "send_wati_image v1 sendMediaMessage failed: waId=%s status=%s endpoint=%s media_url=%s body_preview=%s",
            whatsapp_number,
            resp2.status_code,
            url2,
            media_url,
            body_preview2,
        )
        raise HTTPException(
            status_code=502,
            detail={"wati_status": resp2.status_code, "wati_body_preview": body_preview2},
        )


async def maybe_send_faq_steps(waid: str, channel_phone_number: Optional[str], user_query: str) -> bool:
    """
    If the top FAQ hit contains multiple images, send up to 3 (image first, then a short text bubble).
    Returns True if any step images were sent.
    """
    try:
        vec = embed_query(user_query)
        results = retrieve_faqs_vector(vec)
        if not results.matches:
            return False
        # Pick the first FAQ with images
        meta = None
        for m in results.matches:
            if m.metadata and m.metadata.get("has_images") and m.metadata.get("image_urls"):
                meta = m.metadata
                break
        if not meta:
            return False
        urls = meta.get("image_urls") or []
        caps = meta.get("image_captions") or []
        if not urls or not caps:
            return False
        sent_any = False
        max_steps = min(3, len(urls), len(caps))
        for i in range(max_steps):
            url = urls[i]
            cap = str(caps[i] or "").strip()
            # 1) send image (with brief caption)
            try:
                await send_wati_image(
                    whatsapp_number=waid,
                    media_url=url,
                    caption=None,
                    channel_phone_number=channel_phone_number,
                )
            except Exception as e:
                logger.info("maybe_send_faq_steps image send failed for step=%s err=%s", i + 1, e)
            # 2) send short text bubble for the step
            text_bubble = (f"{i+1}. {cap}" if cap else f"{i+1}. See the screenshot above.")
            try:
                await send_wati_text(
                    whatsapp_number=waid,
                    message_text=text_bubble[:900],
                    reply_context_id=None,
                    channel_phone_number=channel_phone_number,
                )
            except Exception:
                pass
            # small pause to preserve order
            try:
                await asyncio.sleep(0.5)
            except Exception:
                pass
            sent_any = True
        # If more remain, ask
        if len(urls) > max_steps:
            remaining = len(urls) - max_steps
            prompt_more = f"I have {remaining} more screenshot{'s' if remaining > 1 else ''}. Want me to share them?"
            try:
                await send_wati_text(
                    whatsapp_number=waid,
                    message_text=prompt_more,
                    reply_context_id=None,
                    channel_phone_number=channel_phone_number,
                )
            except Exception:
                pass
        return sent_any
    except Exception as e:
        logger.info("maybe_send_faq_steps errored: %s", e)
        return False
async def fetch_last_user_text(whatsapp_number: Optional[str]) -> Optional[str]:
    """
    Fetch recent messages for this WhatsApp number from WATI and return the latest user (owner=False) text, if any.
    API path per docs: /{tenantId}/api/v1/getMessages/{whatsappNumber}
    """
    if not whatsapp_number or not settings.wati_api_endpoint or not settings.wati_access_token:
        return None
    base = settings.wati_api_endpoint.rstrip("/")
    url = urljoin(f"{base}/", f"api/v1/getMessages/{whatsapp_number}")
    headers = {"Authorization": f"Bearer {settings.wati_access_token}"}
    params: dict[str, Any] = {"pageSize": 50, "pageNumber": 1}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers, params=params)
            #logger.info("getMessages response: %s", resp.json())
            if resp.status_code >= 400:
                logger.warning("getMessages failed for whatsappNumber=%s status=%s", whatsapp_number, resp.status_code)
                return None
            data = resp.json()
            messages = []
            if isinstance(data, list):
                messages = data
            elif isinstance(data, dict):
                if isinstance(data.get("messages"), dict):
                    messages = data["messages"].get("items", [])
                elif isinstance(data.get("messages"), list):
                    messages = data["messages"]
                elif isinstance(data.get("result"), list):
                    messages = data["result"]
                elif isinstance(data.get("result"), list):
                    messages = data["result"]
            # Walk newest-to-oldest for latest user message
            for m in messages:
                try:
                    if m.get("owner") is False:
                        txt = m.get("text") or m.get("message") or ""
                        if isinstance(txt, str) and txt.strip():
                            return txt.strip()
                except Exception:
                    continue
    except Exception as e:
        logger.debug("fetch_last_user_text error for whatsappNumber=%s: %s", whatsapp_number, str(e))
        return None
    return None


async def assign_chat_to_bot(whatsapp_number: str, channel_phone_number: Optional[str]) -> bool:
    """
    Best-effort call to WATI to reassign a chat to the 'Bot'.
    API shapes can vary across tenants/versions; we log failures without raising.
    """
    if not settings.wati_api_endpoint or not settings.wati_access_token:
        logger.error("assign_chat_to_bot missing WATI config")
        return False
    base = settings.wati_api_endpoint.rstrip("/")
    # Common pattern observed in WATI docs: /api/v1/assignChat/{whatsappNumber}
    url = urljoin(f"{base}/", f"api/v1/assignOperator/")
    headers = {"Authorization": f"Bearer {settings.wati_access_token}"}
    params: dict[str, Any] = {"whatsappNumber": whatsapp_number}
    if channel_phone_number:
        params["channelPhoneNumber"] = channel_phone_number
    try:
        logger.info(
            "assign_chat_to_bot attempt waId=%s channel=%s url=%s",
            whatsapp_number,
            channel_phone_number,
            url,
        )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, params=params)
            if resp.status_code >= 400:
                logger.error(
                    "assign_chat_to_bot failed waId=%s status=%s body=%s",
                    whatsapp_number,
                    resp.status_code,
                    (resp.text[:500] if resp.text else ""),
                )
                return False
            logger.info(
                "assign_chat_to_bot success waId=%s status=%s",
                whatsapp_number,
                resp.status_code,
            )
            return True
    except Exception as e:
        logger.exception("assign_chat_to_bot exception waId=%s: %s", whatsapp_number, str(e))
        return False

@app.get("/health")
async def health() -> PlainTextResponse:
    return PlainTextResponse("Hello World")


@app.post("/webhook/wati")
async def wati_webhook(payload: WatiWebhookMessage, request: Request) -> PlainTextResponse:
    # Debug message type and basic context
    try:
        logger.info(
            "WATI webhook received: eventType=%s owner=%s waId=%s channel=%s text_len=%s content_type=%s",
            payload.eventType,
            payload.owner,
            payload.waId,
            payload.channelPhoneNumber,
            len(payload.text or ""),
            request.headers.get("content-type"),
        )
    except Exception:
        pass
    # If chat is newly assigned, fetch historical messages and craft a contextual intro/response
    if payload.eventType == "chatAssigned" and payload.waId:
        if settings.wati_api_endpoint and settings.wati_access_token:
            try:
                last_user_text = await fetch_last_user_text(payload.waId)
                update_user_activity(payload.waId, payload.channelPhoneNumber)
                logger.info("Last user text: %s", last_user_text)
                # Personalize the welcome line if the CRM already has this lead's name.
                # Read-only w.r.t. the ask flow (never marks the session "asked").
                try:
                    _greet_first = await to_thread.run_sync(name_capture.known_first_name, payload.waId)
                except Exception:
                    _greet_first = None
                _hi = f"Hi {_greet_first}," if _greet_first else "Hi."
                greet_grace = f"{_hi} I’m Grace from EdYoda, your learning advisor. Please tell me how can I help you today?"
                greet_plain = f"{_hi} I’m your learning advisor. Please tell me how can I help you today?"
                if settings.edyoda_rag_enabled and last_user_text:
                    try:
                        reply_text, media, _ca_deflected = await to_thread.run_sync(edyoda_ask_with_media, payload.waId, last_user_text)
                        # Only consider sending images when the FAQ pipeline surfaced a relevant image
                        sent_steps = False
                        if media:
                            # Prefer step-by-step images when available; otherwise send the single best image
                            sent_steps = await maybe_send_faq_steps(
                                payload.waId,
                                payload.channelPhoneNumber,
                                last_user_text,
                            )
                            if not sent_steps:
                                try:
                                    await send_wati_image(
                                        whatsapp_number=payload.waId,
                                        media_url=media[0],
                                        caption=None,
                                        channel_phone_number=payload.channelPhoneNumber,
                                    )
                                except Exception as e:
                                    logger.info("send_wati_image failed; will proceed with text. err=%s", e)
                        if isinstance(reply_text, tuple):
                            # safety: normalize if old signature leaks through
                            reply_text = (reply_text[0] or "").strip()
                        reply_text = (reply_text or "").strip() or greet_grace
                    except Exception:
                        reply_text = greet_plain
                else:
                    reply_text = greet_plain
                await send_wati_text(whatsapp_number=payload.waId, message_text=reply_text, reply_context_id=None, channel_phone_number=payload.channelPhoneNumber)
                try:
                    _ca_deflected = _ca_deflected or ("learning advisor. Please tell me how can I help" in (reply_text or ""))
                    log_conversation(payload.waId, last_user_text or "", reply_text or "", is_deflected=_ca_deflected)
                    if _ca_deflected and last_user_text:
                        asyncio.create_task(_send_deflection_alerts(payload.waId, last_user_text, payload.channelPhoneNumber or ""))
                except Exception:
                    pass
            except Exception as e:
                logger.exception("Failed to send chatAssigned intro (waId=%s): %s", payload.waId, str(e))
        return PlainTextResponse("Give me few minutes to get back to you")

    # WATI can send multiple event types; we only auto-reply to inbound user messages.
    if payload.owner is True:
        return PlainTextResponse("Give me few minutes to get back to you")

    if payload.eventType and payload.eventType != "message":
        return PlainTextResponse("Give me few minutes to get back to you")

    if not payload.waId:
        raise HTTPException(status_code=400, detail="Missing waId in webhook payload")

    # Idempotency/dedup guard: avoid replying twice to the same inbound message.
    # Skip this guard for RAG text flow, since we debounce/coalesce separately.
    if not (settings.edyoda_rag_enabled and (payload.text or "").strip()):
        try:
            r = _get_redis()
            # Prefer whatsappMessageId; else fall back to content hash scoped by waId
            raw_id = payload.whatsappMessageId or ""
            if not raw_id:
                h = hashlib.sha256((payload.waId or "").encode("utf-8") + b"|" + (payload.text or "").encode("utf-8")).hexdigest()[:16]
                raw_id = f"{payload.waId}:{h}"
            dedupe_key = f"{REDIS_KEY_PREFIX}dedupe:{raw_id}"
            # setnx returns True only the first time; expire after 10 minutes
            first = r.setnx(dedupe_key, "1")
            if first:
                try:
                    r.expire(dedupe_key, 600)
                except Exception:
                    pass
            else:
                # Already processed; ACK without responding again
                return PlainTextResponse("Hello World")
        except Exception:
            # Best-effort: on Redis failure, proceed without dedupe
            pass

    # Record inbound user activity (used for inactivity-based reassignment)
    try:
        if (payload.eventType is None or payload.eventType == "message") and payload.owner is not True:
            update_user_activity(payload.waId, payload.channelPhoneNumber)
    except Exception:
        # Non-fatal
        pass

    # Handle inbound image messages via Claude vision
    _extra = payload.model_extra or {}
    if settings.edyoda_rag_enabled and _extra.get("type") == "image" and _extra.get("data"):
        image_url: str = str(_extra["data"])
        caption: str = (payload.text or "").strip()
        waid_img = payload.waId

        async def _handle_image():
            try:
                headers_dl = {"Authorization": f"Bearer {settings.wati_access_token}"}
                async with httpx.AsyncClient(timeout=20) as client:
                    dl = await client.get(image_url, headers=headers_dl)
                if dl.status_code >= 400 or not dl.content:
                    logger.warning("image download failed: url=%s status=%s", image_url, dl.status_code)
                    await send_wati_text(
                        whatsapp_number=waid_img,
                        message_text="I couldn't open that image. Could you describe what you're looking for?",
                        reply_context_id=payload.whatsappMessageId,
                        channel_phone_number=payload.channelPhoneNumber,
                    )
                    return
                content_type = dl.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                if content_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                    content_type = "image/jpeg"
                reply = await to_thread.run_sync(
                    edyoda_ask_with_image, waid_img, dl.content, content_type, caption
                )
                reply = (reply or "").strip()
                if not reply:
                    reply = "I can see the image but couldn't make sense of it in this context. Could you tell me what you're looking for? I can help you find the right EdYoda program."
                if len(reply) > 3500:
                    reply = reply[:3500]
                await send_wati_text(
                    whatsapp_number=waid_img,
                    message_text=reply,
                    reply_context_id=None,
                    channel_phone_number=payload.channelPhoneNumber,
                )
                log_conversation(waid_img, f"[image] {caption}", reply, is_deflected=_is_deflected_reply(reply), image_url=image_url)
            except Exception as e:
                logger.exception("image handler failed waId=%s: %s", waid_img, e)
                _fallback = "I had trouble processing that image. Could you describe what you're looking for instead?"
                try:
                    await send_wati_text(
                        whatsapp_number=waid_img,
                        message_text=_fallback,
                        reply_context_id=None,
                        channel_phone_number=payload.channelPhoneNumber,
                    )
                except Exception:
                    pass
                log_conversation(waid_img, f"[image] {caption}", _fallback, is_deflected=True, image_url=image_url)

        asyncio.create_task(_handle_image())
        return PlainTextResponse("Hello World")

    # Debounce/coalesce rapid multi-part messages: buffer and schedule one reply
    try:
        r = _get_redis()
        waid = payload.waId
        text = (payload.text or "").strip()
        if settings.edyoda_rag_enabled and text:
            buf_list = f"{REDIS_KEY_PREFIX}buf:{waid}"
            last_seen_key = f"{REDIS_KEY_PREFIX}buf_last:{waid}"
            lock_key = f"{REDIS_KEY_PREFIX}buf_lock:{waid}"
            # Append this chunk in arrival order and set TTL
            try:
                r.rpush(buf_list, text)
                r.expire(buf_list, 600)
            except Exception:
                pass
            now_ts = int(time.time())
            try:
                r.set(last_seen_key, str(now_ts), ex=600)
            except Exception:
                pass
            # Try to be the coalescer: setnx a short-lived lock and run a debounce loop
            try:
                got_lock = r.set(lock_key, "1", nx=True, ex=30)
            except Exception:
                got_lock = False
            if got_lock:
                # Choose a dynamic debounce window:
                # - If message is short or likely multi-part (no terminal punctuation), use longer window.
                # - If the assistant spoke very recently, also use longer window to capture quick follow-ups.
                window = _DEBOUNCE_WINDOW_SEC
                try:
                    txtlen = len(text)
                    terminal = text.endswith((".", "?", "!", "।"))
                    if txtlen <= 12 or not terminal:
                        window = max(window, 8.0)
                    # Inspect last assistant activity
                    try:
                        meta = get_session_meta(waid)  # type: ignore
                    except Exception:
                        meta = {}
                    last_asst = 0
                    try:
                        last_asst = int(float(meta.get("last_assistant_ts") or 0))
                    except Exception:
                        last_asst = 0
                    if last_asst:
                        if int(time.time()) - last_asst <= 60:
                            window = max(window, 8.0)
                except Exception:
                    window = _DEBOUNCE_WINDOW_SEC

                async def _debounce_and_reply():
                    try:
                        # Wait until no new chunks arrive within the window
                        while True:
                            await asyncio.sleep(window)
                            try:
                                last_raw = r.get(last_seen_key)
                                last = int(last_raw) if last_raw else 0
                            except Exception:
                                last = 0
                            if int(time.time()) - last >= window:
                                break
                        # Consume buffer
                        try:
                            chunks = r.lrange(buf_list, 0, -1) or []
                        except Exception:
                            chunks = []
                        try:
                            r.delete(buf_list)
                            r.delete(last_seen_key)
                        except Exception:
                            pass
                        combined = "\n".join([c for c in chunks if isinstance(c, str) and c.strip()]).strip()
                        if not combined:
                            return
                        # Ask chatbot (sync in thread) and send result
                        _is_deflected = False
                        try:
                            reply_text, media, _is_deflected = await to_thread.run_sync(edyoda_ask_with_media, waid, combined)
                        except Exception:
                            reply_text, media, _is_deflected = ("I’m currently busy. Please try again in a minute.", None, True)
                        try:
                            _reply_str = reply_text if isinstance(reply_text, str) else (reply_text[0] or "")
                            log_conversation(waid, combined, _reply_str, is_deflected=_is_deflected)
                            if _is_deflected:
                                asyncio.create_task(_send_deflection_alerts(waid, combined, payload.channelPhoneNumber or ""))
                        except Exception:
                            pass
                        # Send media first (if any, and step images where relevant)
                        try:
                            sent_steps = False
                            if media:
                                sent_steps = await maybe_send_faq_steps(waid, payload.channelPhoneNumber, combined)
                                if not sent_steps:
                                    try:
                                        await send_wati_image(
                                            whatsapp_number=waid,
                                            media_url=media[0],
                                            caption=None,
                                            channel_phone_number=payload.channelPhoneNumber,
                                        )
                                    except Exception as e:
                                        logger.info("send_wati_image failed; will proceed with text. err=%s", e)
                        except Exception:
                            pass
                        # Normalize and send text
                        if isinstance(reply_text, tuple):
                            reply_text = (reply_text[0] or "")
                        reply_text = (reply_text or "").strip() or "Hello World"
                        if len(reply_text) > 3500:
                            reply_text = reply_text[:3500]
                        await send_wati_text(
                            whatsapp_number=waid,
                            message_text=reply_text,
                            reply_context_id=None,  # avoid threading multiple IDs; use plain send
                            channel_phone_number=payload.channelPhoneNumber,
                        )
                    finally:
                        # Release lock
                        try:
                            r.delete(lock_key)
                        except Exception:
                            pass
                asyncio.create_task(_debounce_and_reply())
        else:
            # Non-RAG or empty text: quick fallback reply (no debounce)
            await send_wati_text(
                whatsapp_number=payload.waId,
                message_text="Hello World",
                reply_context_id=payload.whatsappMessageId,
                channel_phone_number=payload.channelPhoneNumber,
            )
    except Exception as e:
        logger.exception("Debounce pipeline failed waId=%s: %s", payload.waId, str(e))

    # Webhook acknowledgment (WATI expects 200 OK).
    return PlainTextResponse("Hello World")


@app.post("/tasks/reassign-idle")
async def reassign_idle_chats() -> dict[str, int]:
    """
    Scan recent chat sessions and, for any user idle >= 90 minutes, reassign to WATI Bot.
    This endpoint is intended to be triggered by cron/systemd timer.
    """
    r = _get_redis()
    now_ts = int(__import__("time").time())
    checked = 0
    reassigned = 0
    # Iterate META keys only; they are lighter than scanning full lists
    try:
        now_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()
        logger.debug("reassign_idle_chats scan start now_ts=%s (%s)", now_ts, now_iso)
        for meta_key in r.scan_iter(f"{META_KEY_PREFIX}*"):
            checked += 1
            try:
                meta = r.hgetall(meta_key) or {}
                waid = str(meta_key).removeprefix(META_KEY_PREFIX)
                logger.debug("reassign_idle_chats scan waid=%s", waid)
                if not waid:
                    logger.debug("scan skip: empty waId for key=%s", meta_key)
                    continue
                last_user_ts_raw = meta.get("last_user_ts")
                if not last_user_ts_raw:
                    logger.debug("scan skip: no last_user_ts waId=%s meta=%s", waid, meta)
                    continue
                try:
                    last_user_ts = int(float(last_user_ts_raw))
                except Exception:
                    logger.debug("scan skip: invalid last_user_ts waId=%s raw=%s", waid, last_user_ts_raw)
                    continue
                # skip if already reassigned
                if str(meta.get("reassigned_to_bot", "0")) == "1":
                    logger.debug("scan skip: already reassigned waId=%s", waid)
                    continue
                idle_seconds = now_ts - last_user_ts
                if idle_seconds < 90 * 60:
                    logger.debug("scan skip: below threshold waId=%s idle_s=%s", waid, idle_seconds)
                    continue
                channel = meta.get("channel_phone_number")
                last_user_iso = datetime.fromtimestamp(last_user_ts, tz=timezone.utc).isoformat()
                logger.info(
                    "reassign attempt waId=%s idle_s=%s last_user_ts=%s (%s) channel=%s",
                    waid,
                    idle_seconds,
                    last_user_ts,
                    last_user_iso,
                    channel,
                )
                ok = await assign_chat_to_bot(waid, channel)
                if ok:
                    mark_reassigned_to_bot(waid)
                    logger.info(
                        "reassign success waId=%s idle_s=%s last_user_ts=%s (%s)",
                        waid,
                        idle_seconds,
                        last_user_ts,
                        last_user_iso,
                    )
                    reassigned += 1
                else:
                    logger.info(
                        "reassign failed waId=%s idle_s=%s last_user_ts=%s (%s)",
                        waid,
                        idle_seconds,
                        last_user_ts,
                        last_user_iso,
                    )
            except Exception as e:
                logger.debug("reassign scan inner error for key=%s: %s", meta_key, str(e))
                continue
    except Exception as e:
        logger.exception("reassign scan failed: %s", str(e))
    logger.debug("reassign_idle_chats scan end checked=%s reassigned=%s", checked, reassigned)
    return {"checked": checked, "reassigned": reassigned}


async def _background_reassign_loop() -> None:
    """Periodically run the idle reassignment scan."""
    assert _bg_stop_event is not None
    # small initial delay to allow app to warm up
    try:
        await asyncio.sleep(10)
        while not _bg_stop_event.is_set():
            try:
                result = await reassign_idle_chats()
                logger.info(
                    "bg_reassign result checked=%s reassigned=%s",
                    result.get("checked", 0),
                    result.get("reassigned", 0),
                )
            except Exception as e:
                logger.exception("bg_reassign loop iteration failed: %s", str(e))
            # wait with cancellation support
            try:
                await asyncio.wait_for(_bg_stop_event.wait(), timeout=_REASSIGN_LOOP_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
    except Exception as e:
        logger.exception("bg_reassign loop crashed: %s", str(e))


_crm_notes_task = None  # type: ignore


async def _crm_notes_loop() -> None:
    """Periodically log settled, qualifying conversations to the CRM (flag-gated).

    Decoupled from the reply path: runs the blocking batch in a worker thread.
    """
    from app.crm_notes import config as _crm_cfg, service as _crm_svc
    cfg = _crm_cfg.load()
    assert _bg_stop_event is not None
    try:
        await asyncio.sleep(30)  # warm-up
        while not _bg_stop_event.is_set():
            try:
                totals = await asyncio.to_thread(_crm_svc.run_once, dry_run=False)
                logger.info("crm_notes run: %s", totals)
            except Exception as e:
                logger.exception("crm_notes loop iteration failed: %s", str(e))
            try:
                await asyncio.wait_for(_bg_stop_event.wait(), timeout=cfg.loop_interval_seconds)
            except asyncio.TimeoutError:
                pass
    except Exception as e:
        logger.exception("crm_notes loop crashed: %s", str(e))


_crm_dedup_task = None  # type: ignore


def _seconds_until_next(times_ist: tuple):
    """Seconds until the next HH:MM IST time in the list (+ that datetime)."""
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    fires = []
    for t in times_ist:
        try:
            hh, mm = (int(x) for x in t.split(":"))
        except Exception:
            continue
        fire = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if fire <= now:
            fire += timedelta(days=1)
        fires.append(fire)
    if not fires:
        return 3600.0, None
    nxt = min(fires)
    return (nxt - now).total_seconds(), nxt


async def _crm_dedup_loop() -> None:
    """Run the lead-dedup job at the configured IST times (flag-gated)."""
    from app.crm_notes import config as _crm_cfg, dedup as _crm_dedup
    cfg = _crm_cfg.load()
    assert _bg_stop_event is not None
    try:
        while not _bg_stop_event.is_set():
            secs, nxt = _seconds_until_next(cfg.dedup_times)
            logger.info("crm_dedup: next run at %s (in %.0fs)", nxt, secs)
            try:
                await asyncio.wait_for(_bg_stop_event.wait(), timeout=secs)
                break  # stop requested
            except asyncio.TimeoutError:
                pass
            try:
                totals = await asyncio.to_thread(_crm_dedup.run_once, dry_run=False)
                logger.info("crm_dedup run: %s", totals)
            except Exception as e:
                logger.exception("crm_dedup run failed: %s", str(e))
    except Exception as e:
        logger.exception("crm_dedup loop crashed: %s", str(e))


@app.on_event("startup")
async def _on_startup() -> None:
    global _bg_reassign_task, _bg_stop_event, _crm_notes_task, _crm_dedup_task
    init_db()
    _bg_stop_event = asyncio.Event()
    _bg_reassign_task = asyncio.create_task(_background_reassign_loop())
    logger.info("Started background idle reassignment loop (interval=%ss)", _REASSIGN_LOOP_INTERVAL_SEC)
    try:
        from app.crm_notes import config as _crm_cfg
        cfg = _crm_cfg.load()
        if cfg.enabled:
            _crm_notes_task = asyncio.create_task(_crm_notes_loop())
            logger.info("Started CRM-notes background loop")
        else:
            logger.info("CRM-notes loop disabled (CRM_NOTES_ENABLED=false)")
        if cfg.dedup_enabled:
            _crm_dedup_task = asyncio.create_task(_crm_dedup_loop())
            logger.info("Started CRM-dedup scheduler (IST times=%s)", ",".join(cfg.dedup_times))
        else:
            logger.info("CRM-dedup scheduler disabled (CRM_DEDUP_ENABLED=false)")
    except Exception as e:
        logger.exception("CRM background tasks failed to start: %s", str(e))


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    global _bg_reassign_task, _bg_stop_event, _crm_notes_task, _crm_dedup_task
    try:
        if _bg_stop_event is not None:
            _bg_stop_event.set()
        for task in (_bg_reassign_task, _crm_notes_task, _crm_dedup_task):
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except Exception:
                    pass
    finally:
        _bg_reassign_task = None
        _crm_notes_task = None
        _crm_dedup_task = None
        _bg_stop_event = None
        logger.info("Stopped background idle reassignment loop")


# --- Simple browser-editable SYSTEM_PROMPT editor (protected by admin token) ---
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompt.txt"
VALIDATOR_PROMPT_PATH = Path(__file__).resolve().parent / "validator_prompt.txt"



_DEFLECT_PHRASES = [
    "don't have", "do not have", "doesn't exist", "does not exist",
    "don't offer", "do not offer", "we don't offer", "we do not offer",
    "not available", "can't find", "cannot find", "no course",
    "not in our", "isn't available", "is not available",
    "unable to find", "don't see", "not part of our",
    "outside our", "we focus on", "not something we",
    "currently busy", "try again in a minute", "trouble processing",
    "couldn't open",
]

def _is_deflected_reply(reply: str) -> bool:
    low = reply.lower()
    return any(p in low for p in _DEFLECT_PHRASES)

DEFLECT_ALERT_NUMBERS = [n.strip() for n in settings.deflect_alert_numbers.split(",") if n.strip()]

async def _send_deflection_alerts(waid: str, user_message: str, channel_phone_number: str) -> None:
    if not settings.wati_api_endpoint or not settings.wati_access_token:
        return
    base = settings.wati_api_endpoint.rstrip("/")
    url = f"{base}/api/v1/sendTemplateMessage"
    headers = {"Authorization": f"Bearer {settings.wati_access_token}", "Content-Type": "application/json"}
    for number in DEFLECT_ALERT_NUMBERS:
        try:
            payload = {
                "template_name": "chatbot_deflection_alert",
                "broadcast_name": "chatbot_deflection_alert",
                "parameters": [
                    {"name": "1", "value": "+" + waid},
                    {"name": "2", "value": user_message[:300]},
                ],
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    params={"whatsappNumber": number},
                    json=payload,
                    headers=headers,
                )
            logger.info("deflection alert sent to %s status=%s", number, resp.status_code)
        except Exception as e:
            logger.warning("deflection alert failed for %s: %s", number, e)

def _require_admin_token(request: Request) -> None:
    token = request.headers.get("X-Admin-Token") or request.query_params.get("token") or ""
    if not settings.admin_token or token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Unauthorized")



@app.get("/admin/chat-logs")
async def chat_logs_ui(request: Request):
    _require_admin_token(request)
    from fastapi.responses import HTMLResponse
    from pathlib import Path
    html = (Path(__file__).parent / "chat_logs.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/admin/chat-logs/api/users")
async def chat_logs_users(request: Request):
    _require_admin_token(request)
    from app.db import get_all_users
    from fastapi.responses import JSONResponse
    deflected_only = request.query_params.get("deflected") == "true"
    return JSONResponse(get_all_users(deflected_only=deflected_only))


@app.get("/admin/chat-logs/api/conversation/{waid:path}")
async def chat_logs_conversation(waid: str, request: Request):
    _require_admin_token(request)
    from app.db import get_conversation
    from fastapi.responses import JSONResponse
    return JSONResponse(get_conversation(waid))

@app.get("/admin/system-prompt")
async def get_system_prompt_page(request: Request) -> PlainTextResponse:
    _require_admin_token(request)
    try:
        content = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read system prompt: {e}")
    # Minimal HTML editor
    html = f"""<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Edit SYSTEM_PROMPT</title></head>
  <body>
    <h3>Edit SYSTEM_PROMPT</h3>
    <form method="post" action="/admin/system-prompt">
      <input type="hidden" name="token" value="{settings.admin_token}"/>
      <textarea name="content" rows="30" cols="120">{content.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")}</textarea>
      <br/>
      <button type="submit">Save</button>
    </form>
  </body>
 </html>"""
    return PlainTextResponse(html, media_type="text/html; charset=utf-8")


@app.post("/admin/system-prompt")
async def update_system_prompt(request: Request) -> PlainTextResponse:
    """
    Accepts:
    - application/x-www-form-urlencoded (HTML form)
    - multipart/form-data (if python-multipart installed)
    - application/json
    Falls back to manual parsing if form() is unavailable.
    """
    token = request.headers.get("X-Admin-Token") or ""
    content: Optional[str] = None
    # Try standard form parsing first (requires python-multipart)
    try:
        form = await request.form()
        token = form.get("token") or token or ""
        content = form.get("content") if isinstance(form.get("content"), str) else None
    except Exception:
        # Fallback: manual parse
        ctype = (request.headers.get("content-type") or "").lower()
        body_bytes = await request.body()
        body_text = body_bytes.decode("utf-8", errors="ignore")
        if "application/json" in ctype:
            try:
                data = await request.json()
                if isinstance(data, dict):
                    token = data.get("token") or token or ""
                    val = data.get("content")
                    content = val if isinstance(val, str) else None
            except Exception:
                pass
        elif "application/x-www-form-urlencoded" in ctype or "text/plain" in ctype:
            try:
                q = parse_qs(body_text, keep_blank_values=True)
                token = (q.get("token", [token or ""])[0]) or ""
                content = q.get("content", [None])[0]
            except Exception:
                pass

    if not settings.admin_token or (token != settings.admin_token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="content is required")
    # Write atomically
    tmp_path = SYSTEM_PROMPT_PATH.with_suffix(".txt.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(SYSTEM_PROMPT_PATH)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write system prompt: {e}")
    # Confirmation page
    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Saved</title></head>
<body><p>Saved successfully.</p><p><a href="/admin/system-prompt?token=%s">Back</a></p></body></html>""" % (
        settings.admin_token
    )
    return PlainTextResponse(html, media_type="text/html; charset=utf-8")
@app.get("/admin/validator-prompt")
async def get_validator_prompt_page(request: Request) -> PlainTextResponse:
    _require_admin_token(request)
    try:
        content = VALIDATOR_PROMPT_PATH.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read validator prompt: {e}")
    html = f"""<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Edit VALIDATOR_PROMPT</title></head>
  <body>
    <h3>Edit VALIDATOR_PROMPT</h3>
    <form method="post" action="/admin/validator-prompt">
      <input type="hidden" name="token" value="{settings.admin_token}"/>
      <textarea name="content" rows="30" cols="120">{content.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")}</textarea>
      <br/>
      <button type="submit">Save</button>
    </form>
  </body>
</html>"""
    return PlainTextResponse(html, media_type="text/html; charset=utf-8")


@app.post("/admin/validator-prompt")
async def update_validator_prompt(request: Request) -> PlainTextResponse:
    token = request.headers.get("X-Admin-Token") or ""
    content: Optional[str] = None
    try:
        form = await request.form()
        token = form.get("token") or token or ""
        content = form.get("content") if isinstance(form.get("content"), str) else None
    except Exception:
        ctype = (request.headers.get("content-type") or "").lower()
        body_bytes = await request.body()
        body_text = body_bytes.decode("utf-8", errors="ignore")
        if "application/json" in ctype:
            try:
                data = await request.json()
                if isinstance(data, dict):
                    token = data.get("token") or token or ""
                    val = data.get("content")
                    content = val if isinstance(val, str) else None
            except Exception:
                pass
        elif "application/x-www-form-urlencoded" in ctype or "text/plain" in ctype:
            try:
                q = parse_qs(body_text, keep_blank_values=True)
                token = (q.get("token", [token or ""])[0]) or ""
                content = q.get("content", [None])[0]
            except Exception:
                pass

    if not settings.admin_token or (token != settings.admin_token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="content is required")
    tmp_path = VALIDATOR_PROMPT_PATH.with_suffix(".txt.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(VALIDATOR_PROMPT_PATH)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write validator prompt: {e}")
    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Saved</title></head>
<body><p>Saved successfully.</p><p><a href="/admin/validator-prompt?token=%s">Back</a></p></body></html>""" % (
        settings.admin_token
    )
    return PlainTextResponse(html, media_type="text/html; charset=utf-8")


@app.post("/admin/refresh-course-cache")
async def refresh_cache_endpoint(request: Request) -> PlainTextResponse:
    """Reload the course cache from Pinecone. Call this after adding/hiding courses.

    Default does a full synchronous reload. With ?lazy=1 it only marks the cache
    stale so the next query reloads — this coalesces bursty re-index notifications
    (the indexer pings this per course) into a single reload.
    """
    _require_admin_token(request)
    if request.query_params.get("lazy") in ("1", "true", "yes"):
        invalidate_course_cache()
        logger.info("admin: course cache marked stale (lazy); reloads on next query")
        return PlainTextResponse("Course cache marked stale; reloads on next query.")
    count = await to_thread.run_sync(refresh_course_cache)
    logger.info("admin: course cache refreshed, %d courses loaded", count)
    return PlainTextResponse(f"Course cache refreshed: {count} courses loaded.")


@app.post("/admin/crm-notes/run")
async def crm_notes_run(request: Request) -> PlainTextResponse:
    """Run one CRM-notes batch on demand. Add ?dry_run=1 for a no-write dry run."""
    _require_admin_token(request)
    from app.crm_notes import service as _crm_svc
    dry = request.query_params.get("dry_run") in ("1", "true", "yes")
    totals = await to_thread.run_sync(lambda: _crm_svc.run_once(dry_run=dry))
    logger.info("admin: crm-notes run (dry_run=%s): %s", dry, totals)
    return PlainTextResponse(f"crm-notes run (dry_run={dry}): {totals}")


@app.post("/admin/crm-dedup/run")
async def crm_dedup_run(request: Request) -> PlainTextResponse:
    """Run the lead-dedup job on demand. Add ?dry_run=1 for a no-write plan."""
    _require_admin_token(request)
    from app.crm_notes import dedup as _crm_dedup
    dry = request.query_params.get("dry_run") in ("1", "true", "yes")
    totals = await to_thread.run_sync(lambda: _crm_dedup.run_once(dry_run=dry))
    logger.info("admin: crm-dedup run (dry_run=%s): %s", dry, totals)
    return PlainTextResponse(f"crm-dedup run (dry_run={dry}): {totals}")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def catch_all(path: str, request: Request) -> PlainTextResponse:
    # Per your requirement: for any message, reply "Hello World".
    # This route ensures even unexpected callbacks get a 200 + Hello World response.
    _ = path, request
    return PlainTextResponse("Hello World")

