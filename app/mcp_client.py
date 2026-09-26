"""
MCP client bridge for the chatbot.

chat() is synchronous (runs in a worker thread), but the MCP stdio client is
async. This module spawns the edyoda-lms MCP server as a stdio child process,
holds one persistent ClientSession on a dedicated background event loop, and
exposes a *sync* surface:

    get_anthropic_tools() -> list of Anthropic tool schemas (empty if MCP is down)
    call_tool(name, llm_args, context=None) -> str tool result

Design points:
  * Best-effort: if the MCP server can't start, get_anthropic_tools() returns []
    and the chatbot simply runs without tools (never hard-fails a reply).
  * Trusted-identity seam (Tier-2): call_tool() accepts a `context` (the verified
    WhatsApp id, etc.) that is injected OUT-OF-BAND — it is merged into the tool
    args only for tools listed in _IDENTITY_TOOLS, and is never part of the
    LLM-facing schema. Tier-1 tools (batch date) take no identity, so nothing is
    injected and nothing leaks to the model.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("mcp_client")

_SERVER_PATH = str(Path(__file__).resolve().parents[1] / "mcp_servers" / "edyoda_lms_server.py")
_PYTHON = os.getenv("MCP_PYTHON", sys.executable)
_START_TIMEOUT = float(os.getenv("MCP_START_TIMEOUT", "30"))
_CALL_TIMEOUT = float(os.getenv("MCP_CALL_TIMEOUT", "30"))

# Tier-2 tools that must receive the verified caller identity via `context`.
# Empty today (batch-date is public). Add user-private tool names here when built.
_IDENTITY_TOOLS: set[str] = set()


class _MCPBridge:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session = None
        self._stdio_ctx = None
        self._session_ctx = None
        self._anthropic_tools: list[dict] = []
        self._ready = threading.Event()
        self._ok = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name="mcp-bridge", daemon=True)
            self._thread.start()
        self._ready.wait(timeout=_START_TIMEOUT)
        if not self._ok:
            logger.warning("MCP bridge did not become ready; chatbot will run without MCP tools")

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
            self._ok = True
        except Exception as e:
            logger.exception("MCP bridge failed to connect: %s", e)
            self._ok = False
        finally:
            self._ready.set()
        if self._ok:
            try:
                self._loop.run_forever()
            except Exception as e:  # pragma: no cover
                logger.exception("MCP bridge loop crashed: %s", e)

    async def _connect(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=_PYTHON,
            args=[_SERVER_PATH],
            env=dict(os.environ),  # forwards EDYODA_LMS_BASE_URL etc. to the server
        )
        self._stdio_ctx = stdio_client(params)
        read, write = await self._stdio_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()
        resp = await self._session.list_tools()
        self._anthropic_tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema or {"type": "object", "properties": {}},
            }
            for t in resp.tools
        ]
        logger.info("MCP connected; tools=%s", [t["name"] for t in self._anthropic_tools])

    def get_anthropic_tools(self) -> list[dict]:
        return list(self._anthropic_tools)

    def call_tool(self, name: str, llm_args: Optional[dict], context: Optional[dict] = None) -> str:
        if not self._ok or self._loop is None:
            return "Tool is temporarily unavailable."
        args: dict[str, Any] = dict(llm_args or {})
        # Trusted-identity injection: only for Tier-2 tools, never from the model.
        if context and name in _IDENTITY_TOOLS:
            args["_context"] = context
        try:
            fut = asyncio.run_coroutine_threadsafe(self._call(name, args), self._loop)
            return fut.result(timeout=_CALL_TIMEOUT)
        except Exception as e:
            logger.warning("MCP call_tool(%s) failed: %s", name, e)
            return f"Tool '{name}' failed ({type(e).__name__})."

    async def _call(self, name: str, args: dict) -> str:
        result = await self._session.call_tool(name, args)
        parts: list[str] = []
        for c in getattr(result, "content", None) or []:
            text = getattr(c, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()


_bridge: Optional[_MCPBridge] = None
_bridge_lock = threading.Lock()


def _get_bridge() -> _MCPBridge:
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = _MCPBridge()
            _bridge.start()
    return _bridge


def get_anthropic_tools() -> list[dict]:
    """Anthropic tool schemas from the MCP server; [] if MCP is unavailable."""
    try:
        return _get_bridge().get_anthropic_tools()
    except Exception as e:
        logger.warning("get_anthropic_tools failed: %s", e)
        return []


def call_tool(name: str, llm_args: Optional[dict], context: Optional[dict] = None) -> str:
    """Execute an MCP tool. `context` carries verified identity (Tier-2 seam),
    injected out-of-band and never exposed to the model."""
    try:
        return _get_bridge().call_tool(name, llm_args, context=context)
    except Exception as e:
        logger.warning("call_tool(%s) failed: %s", name, e)
        return f"Tool '{name}' failed ({type(e).__name__})."
