# EdYoda ChatBotAgent

An AI learning-advisor chatbot for [EdYoda](https://www.edyoda.com). It answers questions about EdYoda's
course catalog, FAQs and batch dates using retrieval-augmented generation (RAG) over Pinecone with Claude
as the responder. It can be reached through two front ends:

| Front end | Entry point | Purpose |
|-----------|-------------|---------|
| **WhatsApp (WATI webhook)** | `app/main.py` | Production. Receives WATI webhooks and replies on WhatsApp. |
| **Web chat UI** | `web_chat.py` + `static/chat.html` | Browser chat page for trying the bot without WhatsApp. |
| **Terminal chat** | `local_chat.py` | Quick CLI tester. |

All three call the same core: `app/edyoda_chatbot.py`.

## Features

- **RAG over Pinecone** – separate course and FAQ indexes; a router picks the right one per question.
- **Claude responder + validator** – a second, cheaper model checks every reply for hallucinated URLs,
  unverified phone numbers and false "course doesn't exist" claims, and rewrites when needed.
- **Live tool use via MCP** – Claude can call an LMS tool (`get_batch_start_date`) for real batch dates.
- **Redis session memory** – last 20 messages per session, 30-day expiry.
- **Hot-editable prompts** – system and validator prompts are plain text files re-read on every request.
- **Deflection tracking** – detects out-of-scope questions, logs them, and alerts staff on WhatsApp.
- **Optional CRM integration** – customer-name capture, conversation summaries as Zoho notes, lead dedup.
- **Image understanding** – inbound WhatsApp images are answered through Claude vision.

## Quick start (web chat)

```bash
# 1. Install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure (never commit real keys)
cp .env.example .env.local        # then edit .env.local and fill in the keys
# Use Redis DB 1 in REDIS_URL so you never touch production sessions:
#   REDIS_URL=redis://localhost:6379/1

# 3. Make sure Redis is running
redis-cli ping                    # -> PONG

# 4. Run
uvicorn web_chat:app --port 8100
```

Open <http://localhost:8100>.

Other modes:

```bash
python3 local_chat.py                                      # terminal chat
sudo -E .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 80   # WhatsApp webhook server
```

## Documentation

| Document | What's in it |
|----------|--------------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, request pipeline, data stores, module map |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every environment variable, defaults, and what it controls |
| [docs/API.md](docs/API.md) | HTTP endpoints for the web chat app and the WATI/admin app |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Redis, systemd, WATI webhook setup, security checklist |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Prompts, course cache, CRM jobs, logs, troubleshooting |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Local dev workflow, testing, extending the bot |

## Repository layout

```
.
├── web_chat.py              # Web chat FastAPI app (standalone, uses .env.local)
├── static/chat.html         # Web chat front end
├── local_chat.py            # Terminal chat tester
├── app/
│   ├── main.py              # WATI webhook FastAPI app + admin routes + background jobs
│   ├── edyoda_chatbot.py    # Core: routing, retrieval, Claude call, tool loop, validator
│   ├── chat_history.py      # Redis-backed session history + session metadata
│   ├── db.py                # SQLite conversation log (chatbot_logs.db)
│   ├── mcp_client.py        # Sync bridge to the MCP tool server
│   ├── name_capture.py      # Customer-name capture against the CRM
│   ├── system_prompt.txt    # Main persona/rules prompt (hot-reloaded)
│   ├── validator_prompt.txt # Validator prompt template (hot-reloaded)
│   ├── chat_logs.html       # Admin conversation viewer
│   └── crm_notes/           # CRM notes + lead dedup (Zoho provider)
├── mcp_servers/
│   └── edyoda_lms_server.py # MCP server exposing LMS tools
├── chatbot-agent.service    # systemd unit
├── redis-memory.conf        # Redis 500MB cap + LRU eviction for sessions
├── requirements.txt
└── .env.example             # All variables, with placeholder values
```

## Security

- Secrets live only in environment files (`.env`, `.env.local`), which are git-ignored.
  Only `.env.example` (placeholders) is committed.
- The admin routes (`/admin/*`) are protected by `ADMIN_TOKEN`; the web chat app has **no authentication**
  and should only be exposed behind a login or on localhost. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#security-checklist).
