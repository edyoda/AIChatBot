# Operations

Day-to-day tasks for running the bot.

## Editing prompts

| Prompt | File | Purpose |
|--------|------|---------|
| System prompt | `app/system_prompt.txt` | Persona, tone, rules, contact details. Sent to the responder on every call. |
| Validator prompt | `app/validator_prompt.txt` | Template used only when the validator has to rewrite a draft. |

Both files are **re-read on every request**, so edits take effect immediately with no restart.

Ways to edit:
1. Edit the file directly on the server.
2. Use the browser editors (WhatsApp app only): `/admin/system-prompt?token=<ADMIN_TOKEN>` and
   `/admin/validator-prompt?token=<ADMIN_TOKEN>`.

The validator prompt is filled with Python `str.format`, so it must keep exactly these placeholders:
`{history_block}`, `{query}`, `{issues}`, `{verified_block}`, `{draft_response}`. Any other literal `{` or `}`
must be doubled (`{{` / `}}`). A mistake here is **not** caught gracefully: `.format()` runs outside the
validator's error handling, so any reply that needs a correction will fail (web UI: 502; WhatsApp: the
"currently busy" fallback). Test validator-prompt edits with a question that triggers the validator
(e.g. ask for a course that doesn't exist).

After a change, test with `local_chat.py` or the web UI before relying on it in production. Keep prompts in git
so you can roll back.

## Updating the course catalog

The bot keeps all visible courses in memory (TTL `CHATBOT_COURSE_CACHE_TTL`, default 6 h) for keyword search and
URL validation. After adding, editing or hiding courses:

```bash
# one reload right now
curl -X POST http://localhost/admin/refresh-course-cache -H "X-Admin-Token: $ADMIN_TOKEN"
# or mark stale; reloads on the next user message (good for bulk re-indexing)
curl -X POST "http://localhost/admin/refresh-course-cache?lazy=1" -H "X-Admin-Token: $ADMIN_TOKEN"
```

Visibility is fetched live from the LMS on each load. If the LMS is down, the Pinecone `visibility_status`
metadata is used and a warning is logged.

## Reviewing conversations and deflections

- Open `/admin/chat-logs?token=<ADMIN_TOKEN>` to browse users and conversations.
- Filter to deflected chats: `/admin/chat-logs/api/users?deflected=true`.
- A conversation is **deflected** when the best retrieval score is below 0.35, or the reply contains
  phrases such as "don't have", "can't find", "not available". Deflections send a WhatsApp alert to
  `DEFLECT_ALERT_NUMBERS` and are the best source of missing courses or FAQs.
- Data is stored in `chatbot_logs.db` (SQLite) in the project root. Back it up like any customer data.
- The web chat UI does not write to this log.

## Changing models

Set `CHATBOT_RESPONDER_MODEL` (and optionally the validator/condense variants) in `.env` and restart.
Check the startup log line `Chatbot models (env-configurable): responder=... validator=... condense=...`.
A rejected value produces `is not a recognized/compatible model id; falling back` in the log.
To roll back, restore the previous value and restart.

## Session management

```bash
redis-cli KEYS 'chat:session:*' | head            # list sessions (avoid KEYS on a busy production Redis)
redis-cli LRANGE chat:session:<id> 0 -1           # view a conversation
redis-cli HGETALL chat:meta:<id>                  # view session metadata
redis-cli DEL chat:session:<id> chat:meta:<id>    # reset one user
```

For the web UI, the **New chat** button clears the session, or call `DELETE /api/session/{id}`.

## Idle-chat reassignment

A background loop runs every 60 s. Chats whose last user message is ≥ 90 minutes old and not yet reassigned are
moved back to the WATI "Bot" operator via `assignOperator`. It is tracked with `reassigned_to_bot` in
`chat:meta:<id>` and reset on the next user message. Trigger manually: `POST /tasks/reassign-idle`.

## CRM jobs (optional)

Set `ZOHO_MCP_URL`, provide the Zoho token file, then enable the flags in [CONFIGURATION.md](CONFIGURATION.md#crm-features-all-off-by-default).

```bash
# always start with a dry run
curl -X POST "http://localhost/admin/crm-notes/run?dry_run=1" -H "X-Admin-Token: $ADMIN_TOKEN"
curl -X POST "http://localhost/admin/crm-dedup/run?dry_run=1" -H "X-Admin-Token: $ADMIN_TOKEN"
```

- **Notes**: a conversation qualifies after `CRM_IDLE_SECONDS` of inactivity with at least `CRM_MIN_USER_MSGS`
  user messages, and is logged once per new activity. Numbers in `CRM_SKIP_NUMBERS` are never logged.
- **Dedup**: keeps the newest lead, marks older duplicates `Lead_Status='Duplicate'`, and copies notes to the survivor.
  Each run writes `dedup_rollback/dedup_rollback_<timestamp>.json` recording previous statuses so a run can be reversed.
  Clusters larger than `CRM_DEDUP_MAX_CLUSTER` are skipped for manual review.

## Logs

| Deployment | Where |
|------------|-------|
| systemd | `sudo journalctl -u chatbot-agent -f` |
| Manual run | stdout/stderr |

Useful log prefixes:

| Prefix | Meaning |
|--------|---------|
| `[RAG]` | Routing decision, scores, name-search and vector hits |
| `[VALIDATOR]` | Issues found and whether a correction was applied |
| `[TOOL]` | MCP tool calls and results |
| `[DEFLECT]` | Deflection decision for the reply |
| `reassign ...`, `crm_notes ...`, `crm_dedup ...` | Background jobs |

## Troubleshooting

| Symptom | Likely cause and fix |
|---------|----------------------|
| `RuntimeError: REDIS_URL is required` | `REDIS_URL` missing from the env file being loaded. |
| `Redis ping failed` | Redis is not running or the URL/password is wrong: `redis-cli -u "$REDIS_URL" ping`. |
| `Missing PINECONE_API_KEY` / auth errors | Key missing or wrong; check the `RAG key visibility` startup log line. |
| Web UI shows "The assistant is unavailable" | See the server log for the real error (Anthropic/OpenAI/Pinecone/Redis). |
| Address already in use on 8000 | Another service owns the port; run on another one (`--port 8100`). |
| Replies are slow (5–20 s) | Expected: embeddings + Pinecone + Claude with thinking, plus a validator pass when issues are found. A cheaper responder model or `RAG_CONDENSE_QUERY=false` reduces latency. |
| WhatsApp reply arrives ~10 s late | The debounce window is intentional so multi-part messages get one answer. Tune `_DEBOUNCE_WINDOW_SEC` in `app/main.py`. |
| Bot says a course doesn't exist | Refresh the course cache; check the course is visible in the LMS and indexed in Pinecone. |
| Batch date questions get no tool answer | MCP server did not start; look for `MCP bridge failed to connect`. Check `EDYODA_LMS_BASE_URL` and `MCP_PYTHON`. |
| `403` from WATI on send | Wrong/expired `WATI_ACCESS_TOKEN` or endpoint. |
| Replies fail only when the validator has to correct something | Malformed placeholders in `validator_prompt.txt` (`KeyError`/`IndexError` in the log) – see [Editing prompts](#editing-prompts). |
| Admin routes return 401 | `ADMIN_TOKEN` unset or the token doesn't match. |
| The bot's name is inconsistent | The code uses three names: **Jessica** (`system_prompt.txt`), **Grace** (the `chatAssigned` greeting in `app/main.py`) and **Aman** (label in `local_chat.py` and in the validator's conversation history). Customers see Jessica in replies and Grace in the greeting; pick one name and align the two. |
