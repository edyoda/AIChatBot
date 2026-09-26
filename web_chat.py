"""
Web chat interface for the EdYoda ChatBotAgent.

Bypasses WATI — calls edyoda_chatbot.ask() directly, same as local_chat.py.
Loads .env.local (Redis DB 1) so prod session history is never touched.

Usage:
    uvicorn web_chat:app --reload --port 8000
    # then open http://localhost:8000
    # use --env prod to load .env instead:  CHAT_ENV=prod uvicorn web_chat:app
"""

import logging
import os
import sys
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ── Load env BEFORE importing app modules (they read env vars at import time) ──
from dotenv import load_dotenv

_env_file = BASE_DIR / (".env" if os.getenv("CHAT_ENV") == "prod" else ".env.local")
if not _env_file.exists():
    sys.exit(f"[ERROR] {_env_file} not found. Create it from .env.example first.")
load_dotenv(dotenv_path=_env_file, override=True)

sys.path.insert(0, str(BASE_DIR))

from anyio import to_thread
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.chat_history import REDIS_KEY_PREFIX, _get_redis, get_recent_messages
from app.edyoda_chatbot import ask

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web_chat")

app = FastAPI(title="EdYoda Chat", version="0.1.0")


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    session_id: str
    reply: str


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "chat.html")


@app.get("/api/session")
async def new_session() -> dict[str, str]:
    # Prefix keeps web sessions distinguishable from WhatsApp numbers in Redis.
    return {"session_id": f"web_{uuid.uuid4().hex[:16]}"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty")
    try:
        # ask() is blocking (LLM + Pinecone calls) — keep it off the event loop.
        reply = await to_thread.run_sync(ask, req.session_id, message)
    except Exception:
        logger.exception("chat failed session=%s", req.session_id)
        raise HTTPException(status_code=502, detail="The assistant is unavailable. Please try again.")
    return ChatResponse(session_id=req.session_id, reply=(reply or "").strip())


@app.get("/api/history/{session_id}")
async def history(session_id: str) -> dict:
    try:
        msgs = await to_thread.run_sync(get_recent_messages, session_id)
    except Exception:
        logger.exception("history failed session=%s", session_id)
        msgs = []
    return {"session_id": session_id, "messages": msgs}


@app.delete("/api/session/{session_id}")
async def clear_session(session_id: str) -> dict[str, bool]:
    try:
        await to_thread.run_sync(_get_redis().delete, f"{REDIS_KEY_PREFIX}{session_id}")
    except Exception:
        logger.exception("clear failed session=%s", session_id)
        raise HTTPException(status_code=500, detail="Could not clear session")
    return {"cleared": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_chat:app", host="127.0.0.1", port=8000, reload=True)
