# Configuration

All configuration is through environment variables. Put them in an env file, never in code.

| File | Loaded by | Use |
|------|-----------|-----|
| `.env` | `app/main.py` (pydantic-settings), systemd `EnvironmentFile`, `chat_history.py` | Production |
| `.env.local` | `web_chat.py` and `local_chat.py` (default) | Local development; use Redis DB 1 |
| `.env.example` | – | Committed template with placeholders |

`web_chat.py` loads `.env.local`; set `CHAT_ENV=prod` to load `.env` instead.
Both `.env` and `.env.local` are git-ignored.

> Values are read at process start. Restart the server after changing them (prompts are the exception – see
> [OPERATIONS.md](OPERATIONS.md#editing-prompts)).

## Required

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key (responder, validator, condenser). |
| `OPENAI_API_KEY` | Used only for `text-embedding-3-large` query embeddings. |
| `PINECONE_API_KEY` | Access to the course and FAQ indexes. |
| `REDIS_URL` | Session store, e.g. `redis://localhost:6379/0`. Use `/1` for local dev. |

## WhatsApp / admin (`app/main.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `WATI_API_ENDPOINT` | – | Tenant base URL from WATI Dashboard → API Docs. Host only, no `/api/...` path. |
| `WATI_ACCESS_TOKEN` | – | WATI bearer token. |
| `EDYODA_RAG_ENABLED` | `true` | `false` makes the webhook reply "Hello World" only. Legacy spelling `EDUYODA_RAG_ENABLED` also works. |
| `ADMIN_TOKEN` | – (admin disabled) | Shared secret for all `/admin/*` routes. Use a long random string. |
| `DEFLECT_ALERT_NUMBERS` | – (alerts off) | Comma-separated WhatsApp numbers with country code, alerted when a chat is deflected. |

## Models

| Variable | Default | Allowed values |
|----------|---------|----------------|
| `CHATBOT_RESPONDER_MODEL` | `claude-opus-4-6` | `claude-opus-4-6`, `-4-7`, `-4-8`, `claude-opus-5`, `claude-sonnet-5`, `claude-sonnet-4-6` |
| `CHATBOT_VALIDATOR_MODEL` | `claude-haiku-4-5-20251001` | any responder model, plus `claude-haiku-4-5`, `claude-haiku-4-5-20251001` |
| `CHATBOT_CONDENSE_MODEL` | `claude-haiku-4-5-20251001` | same as validator |

The responder uses adaptive thinking, so it must be a 4.6+ model. An unrecognised value is logged as an error
and the default is used. To allow a new model ID, add it to `_RESPONDER_MODELS` / `_UTILITY_MODELS` in
`app/edyoda_chatbot.py`.

## Retrieval

| Variable | Default | Description |
|----------|---------|-------------|
| `PINECONE_COURSE_INDEX` | `edyoda-course-search` | Course index name. |
| `PINECONE_COURSE_NAMESPACE` | `courses` | Course namespace. |
| `PINECONE_FAQ_INDEX` | `edyoda-faqs` | FAQ index name. |
| `PINECONE_FAQ_NAMESPACE` | `faqs` | FAQ namespace. |
| `RAG_CONDENSE_QUERY` | `false` | `true` rewrites follow-up questions into standalone queries instead of appending past replies. |
| `CHATBOT_COURSE_CACHE_TTL` | `21600` (6 h) | Seconds before the in-memory course cache is reloaded. |

Constants in code (`app/edyoda_chatbot.py`): `TOP_K=3`, `MAX_TOKENS=2048`, `FAQ_ABS_MIN=0.5`,
`FAQ_MARGIN=0.3`, `DEFLECT_SCORE_THRESHOLD=0.35`. Session limits in `app/chat_history.py`:
`MAX_MESSAGES_PER_SESSION=20`, `SESSION_TTL_DAYS=30`.

## LMS and MCP tools

| Variable | Default | Description |
|----------|---------|-------------|
| `EDYODA_LMS_BASE_URL` | `https://backend.edyoda.com` | LMS API for batch dates and course visibility. |
| `EDYODA_LMS_SERVICE_SECRET` | – | Reserved for future authenticated (Tier-2) tools; unused today. |
| `EDYODA_LMS_HTTP_TIMEOUT` | `10` | Seconds, for the MCP server's HTTP calls. |
| `MCP_PYTHON` | current interpreter | Python used to spawn the MCP server. |
| `MCP_START_TIMEOUT` | `30` | Seconds to wait for the MCP server on first use. |
| `MCP_CALL_TIMEOUT` | `30` | Seconds allowed per tool call. |

## Web chat

| Variable | Default | Description |
|----------|---------|-------------|
| `CHAT_ENV` | (unset → `.env.local`) | Set to `prod` to load `.env` instead of `.env.local`. |

## CRM features (all off by default)

| Variable | Default | Description |
|----------|---------|-------------|
| `NAME_CAPTURE_ENABLED` | off | Look up / ask for / save the customer's name. |
| `CRM_PROVIDER` | `zoho` | CRM implementation. |
| `ZOHO_MCP_URL` | – (required if CRM used) | Zoho MCP endpoint. |
| `ZOHO_TOKEN_FILE` | `.zoho_mcp_token.json` | OAuth token file (git-ignored; contains secrets). |
| `CRM_NOTES_ENABLED` | `false` | Start the hourly notes loop. |
| `OPENROUTER_API_KEY` | – | Key for the summariser. |
| `CRM_SUMMARY_MODEL` | `meta-llama/llama-3.1-8b-instruct` | OpenRouter model for summaries. |
| `CRM_IDLE_SECONDS` | `1200` | A conversation must be idle this long before it is summarised. |
| `CRM_MIN_USER_MSGS` | `2` | Minimum user messages to qualify. |
| `CRM_MAX_PER_RUN` | `50` | Cap on conversations logged per run. |
| `CRM_LOOP_INTERVAL` | `3600` | Seconds between notes runs. |
| `CRM_SKIP_NUMBERS` | EdYoda support lines | Comma-separated numbers never logged. |
| `CRM_DEDUP_ENABLED` | `false` | Start the dedup scheduler. |
| `CRM_DEDUP_WINDOW_HOURS` | `24` | Only leads created in this window are examined. |
| `CRM_DEDUP_TIMES` | `08:00,17:00` | Daily run times, IST. |
| `CRM_DEDUP_ROLLBACK_DIR` | `dedup_rollback` | Where per-run rollback JSON files are written. |
| `CRM_DEDUP_MAX_CLUSTER` | `25` | Clusters larger than this are skipped for human review. |

## Example `.env.local`

```dotenv
ANTHROPIC_API_KEY=your-anthropic-key
OPENAI_API_KEY=your-openai-key
PINECONE_API_KEY=your-pinecone-key
REDIS_URL=redis://localhost:6379/1
EDYODA_RAG_ENABLED=true
ADMIN_TOKEN=a-long-random-string
EDYODA_LMS_BASE_URL=https://backend.edyoda.com
```

Generate a token with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
