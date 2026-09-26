# HTTP API

Two independent FastAPI apps:

- **Web chat app** – `web_chat:app` (default port 8100). No authentication.
- **WhatsApp/admin app** – `app.main:app` (port 80 in production). Admin routes need `ADMIN_TOKEN`.

Interactive OpenAPI docs are served at `/docs` on the web chat app. On `app.main` the catch-all route
answers `Hello World` to unknown paths, but FastAPI's built-in `/docs` is registered first and still works.

---

## Web chat app (`web_chat.py`)

### `GET /`
Serves the chat page (`static/chat.html`).

### `GET /api/session`
Creates a new session ID.

```json
{ "session_id": "web_3f9c1a2b4d5e6f70" }
```

### `POST /api/chat`
Send a message and get the reply. History is stored server-side under the session ID.

Request:
```json
{ "session_id": "web_3f9c1a2b4d5e6f70", "message": "Do you have an AI agents course?" }
```

| Field | Rules |
|-------|-------|
| `session_id` | 1–64 chars, `[A-Za-z0-9_.-]` only |
| `message` | 1–4000 chars |

Response `200`:
```json
{ "session_id": "web_3f9c1a2b4d5e6f70", "reply": "..." }
```

| Status | Meaning |
|--------|---------|
| 422 | Validation failed (bad session ID, empty or too-long message) |
| 502 | The assistant failed (LLM/Pinecone/Redis error); details are in the server log |

A request can take several seconds (retrieval + Claude + possible validator pass).

### `GET /api/history/{session_id}`
Returns stored messages, oldest first (max 20). Unknown sessions return an empty list; Redis errors also return an empty list.

```json
{
  "session_id": "web_3f9c...",
  "messages": [
    { "role": "user", "content": "hi", "ts": 1790427832 },
    { "role": "assistant", "content": "Hi there ...", "ts": 1790427832 }
  ]
}
```

### `DELETE /api/session/{session_id}`
Deletes the session's history. Response `{ "cleared": true }`; `500` if Redis fails.

### Example

```bash
SID=$(curl -s localhost:8100/api/session | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
curl -s -X POST localhost:8100/api/chat \
  -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$SID\",\"message\":\"hi\"}"
```

> There is no auth or rate limiting. Anyone who can reach the port can spend your LLM budget.
> See [DEPLOYMENT.md](DEPLOYMENT.md#security-checklist).

---

## WhatsApp / admin app (`app/main.py`)

### Public routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check; returns plain text `Hello World`. |
| POST | `/webhook/wati` | WATI webhook receiver. Always answers `200` quickly; replies are sent asynchronously through the WATI API. |
| POST | `/tasks/reassign-idle` | Runs one idle-reassignment scan (also runs automatically every 60 s). Returns `{"checked": n, "reassigned": n}`. **Not authenticated** – see the security checklist. |

#### `POST /webhook/wati` payload
```json
{
  "eventType": "message",
  "owner": false,
  "waId": "85264318721",
  "text": "hi",
  "whatsappMessageId": "wamid.example",
  "channelPhoneNumber": "17435002445"
}
```
Unknown fields are accepted. Image messages include `"type": "image"` and a `"data"` URL.

Behaviour:
- `eventType=chatAssigned` → sends an intro/answer based on the user's last message.
- `owner=true` (agent message) or any other event type → acknowledged, no reply.
- Text message → buffered and answered once after the debounce window (8–10 s of silence).
- Image message → answered through Claude vision.
- `400` if `waId` is missing.

### Admin routes (require `ADMIN_TOKEN`)

Send the token as the `X-Admin-Token` header, or `?token=` in the query string. Missing/wrong → `401`.
If `ADMIN_TOKEN` is unset, all admin routes return `401`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/chat-logs` | HTML conversation viewer. |
| GET | `/admin/chat-logs/api/users?deflected=true` | JSON list of users with message counts; `deflected=true` filters to users with at least one deflection. |
| GET | `/admin/chat-logs/api/conversation/{waid}` | JSON conversation for one user. |
| GET / POST | `/admin/system-prompt` | HTML editor / save for `app/system_prompt.txt`. |
| GET / POST | `/admin/validator-prompt` | HTML editor / save for `app/validator_prompt.txt`. |
| POST | `/admin/refresh-course-cache` | Reload the course cache from Pinecone now. `?lazy=1` only marks it stale (reloads on next query) – use this for bulk re-indexing. |
| POST | `/admin/crm-notes/run` | Run one CRM-notes batch. `?dry_run=1` reports without writing. |
| POST | `/admin/crm-dedup/run` | Run the lead-dedup job. `?dry_run=1` reports the plan only. |

POSTs to the prompt editors accept form data or JSON `{ "token": "...", "content": "..." }` and write the file
atomically. `content` must be non-empty (`400` otherwise).

```bash
curl -X POST "http://localhost/admin/refresh-course-cache?lazy=1" -H "X-Admin-Token: $ADMIN_TOKEN"
curl -X POST "http://localhost/admin/crm-notes/run?dry_run=1"     -H "X-Admin-Token: $ADMIN_TOKEN"
```

### Catch-all
Any other method/path returns `200 Hello World`.
