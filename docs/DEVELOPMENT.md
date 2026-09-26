# Development guide

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local        # fill in keys; REDIS_URL=redis://localhost:6379/1
redis-cli ping                    # Redis must be running
```

Use Redis DB **1** for development. Production uses DB 0, so local experiments never touch real sessions.

## Ways to run

| Goal | Command |
|------|---------|
| Web chat with auto-reload | `uvicorn web_chat:app --reload --port 8100` |
| Terminal chat | `python3 local_chat.py` (options: `--session NAME`, `--new`) |
| WhatsApp server locally | `uvicorn app.main:app --port 8000` (needs `.env` in the working directory) |
| Simulate a WATI message | `curl -X POST localhost:8000/webhook/wati -H 'Content-Type: application/json' -d '{"eventType":"message","owner":false,"waId":"911234567890","text":"hi"}'` |

`local_chat.py` commands: `clear` resets the session, `quit` exits.

Note: `local_chat.py` and `web_chat.py` call the real Anthropic, OpenAI and Pinecone APIs, so they cost money and
need network access.

## Code map

| To change… | Look at |
|------------|---------|
| How a question is answered | `chat()` in `app/edyoda_chatbot.py` |
| Course vs FAQ routing thresholds | `FAQ_ABS_MIN`, `FAQ_MARGIN` and `route_and_retrieve()` |
| Course keyword matching | `_name_search()`, `_kw_matches()`, `STOP_WORDS` |
| Hallucination checks | `validate_and_correct()` |
| Deflection rules | `DEFLECT_SCORE_THRESHOLD` and the phrase list at the end of `chat()`; `_DEFLECT_PHRASES` in `app/main.py` |
| History size / TTL | `MAX_MESSAGES_PER_SESSION`, `SESSION_TTL_DAYS` in `app/chat_history.py` |
| WhatsApp behaviour | `app/main.py` (`wati_webhook`, debounce, image handling) |
| Web UI | `static/chat.html`, `web_chat.py` |
| Persona / tone | `app/system_prompt.txt` |

## Adding an MCP tool

Tools live in `mcp_servers/edyoda_lms_server.py`. Add a function decorated with `@mcp.tool()`; the client discovers
it automatically at startup and passes its schema to Claude.

```python
@mcp.tool()
def get_course_price(course_slug: str) -> str:
    """One-sentence description Claude uses to decide when to call this tool."""
    data = _lms_get("/api/v1/course-price", params={"course_url": course_slug})
    return f"{data['name']}: ..."
```

Guidelines:
- The **docstring is the tool description the model sees** – say exactly when to use it and what the argument is.
- Return a short string. Tell the model what to do on failure ("Do not tell the user X").
- Public data uses `_lms_get(..., authed=False)`. For user-private tools use `authed=True`, add the tool name to
  `_IDENTITY_TOOLS` in `app/mcp_client.py`, and read the trusted `_context` argument (the caller's WhatsApp ID),
  which is injected by the client and never chosen by the model.
- Restart the app; the MCP server is spawned once per process.
- To see tool calls, watch logs for `[TOOL]`.

Test the server on its own:
```bash
EDYODA_LMS_BASE_URL=https://backend.edyoda.com python mcp_servers/edyoda_lms_server.py   # speaks MCP over stdio
```

## Adding a CRM provider

Subclass `CRMProvider` in `app/crm_notes/crm/base.py` and implement `find_leads`, `create_lead`, `update_lead`,
`add_note`, `list_notes`, `leads_created_since` and `set_lead_status`. Register it in `get_provider()` and select it
with `CRM_PROVIDER`. The orchestrator uses only that interface. The same file has a `FakeCRMProvider` (in-memory) that
is handy for tests and dry runs.

## Testing checklist

There is no automated test suite yet. Before shipping a change:

1. `python3 local_chat.py --new` – greeting, a course question, a follow-up ("when does it start?"), an off-topic question.
2. Check logs: `[RAG] mode=` sensible, `[VALIDATOR] response OK`, `[DEFLECT]` correct for the off-topic question.
3. Ask for a batch date and confirm a `[TOOL] get_batch_start_date` line appears.
4. If touching `app/main.py`, POST a simulated webhook and confirm the debounced reply is logged.
5. `git status` – confirm no `.env*`, `.db` or token files are staged.

Good candidates for a first test suite: `_extract_keywords`, `_kw_matches`, `_normalize_phone`,
`_extract_phone_numbers`, `_infer_geo_from_waid`, `_is_deflected_reply` (all pure functions), and `web_chat`
endpoints via FastAPI's `TestClient` with `ask` monkeypatched.

## Conventions

- Configuration through env vars; never hard-code keys, tokens, phone numbers or IPs.
- Blocking work (LLM, Pinecone, Redis, SQLite) must run in a thread from async code (`to_thread.run_sync`).
- New integrations must fail open: log a warning and let the reply continue.
- Keep user-visible copy in prompt files rather than code where possible.

## Git workflow

```bash
git status                # verify no secrets are staged
git add -p
git commit -m "Describe the change"
```
`.gitignore` excludes `.env*` (except `.env.example`), `.venv/`, `*.db`, `.zoho_mcp_token.json`, logs and rollback files.
If a secret is ever committed, rotate it – removing it in a later commit does not remove it from history.
