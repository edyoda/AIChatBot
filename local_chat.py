"""
Local chat tester for EdYoda ChatBotAgent.

Bypasses WATI entirely — calls edyoda_chatbot.ask() directly.
Uses .env.local (Redis DB 1) so prod session history is never touched.

Usage:
    cd /home/awantik/ClaudeCoWork/WAP/lmsv2.0/ChatBotAgent
    python3 local_chat.py
    python3 local_chat.py --session mytest123   # named session
    python3 local_chat.py --new                 # always start fresh
"""

import argparse
import os
import sys
from pathlib import Path

# ── Load .env.local BEFORE importing app modules ──────────────────────────────
env_local = Path(__file__).resolve().parent / ".env.local"
if not env_local.exists():
    print(f"[ERROR] {env_local} not found. Create it from .env.example first.")
    sys.exit(1)

from dotenv import load_dotenv
load_dotenv(dotenv_path=env_local, override=True)

import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")

# ── Now import app modules (they read env vars at import time) ─────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.edyoda_chatbot import ask
from app.chat_history import _get_redis, REDIS_KEY_PREFIX


def clear_session(session_id: str) -> None:
    try:
        r = _get_redis()
        key = f"{REDIS_KEY_PREFIX}{session_id}"
        r.delete(key)
        print(f"[session cleared: {session_id}]\n")
    except Exception as e:
        print(f"[warn] could not clear session: {e}\n")


def main():
    parser = argparse.ArgumentParser(description="Local EdYoda chatbot tester")
    parser.add_argument("--session", default="local_test_001", help="Session / fake phone number")
    parser.add_argument("--new", action="store_true", help="Start a fresh session (clears history)")
    args = parser.parse_args()

    session_id = args.session
    redis_url = os.getenv("REDIS_URL", "")

    print(f"EdYoda ChatBot — local test")
    print(f"Session  : {session_id}")
    print(f"Redis    : {redis_url}  (DB 1 = isolated from prod)")
    print(f"Prod safe: YES — no WATI calls, separate Redis DB")
    print(f"Type 'quit' to exit, 'clear' to reset session history.\n")

    if args.new:
        clear_session(session_id)

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break
        if user_input.lower() == "clear":
            clear_session(session_id)
            continue

        try:
            reply = ask(session_id, user_input)
            print(f"\nAman: {reply}\n")
        except Exception as e:
            print(f"[ERROR] {e}\n")


if __name__ == "__main__":
    main()
