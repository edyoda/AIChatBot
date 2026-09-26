# Deployment

This covers running the WhatsApp server (`app/main.py`) on a Linux host. For the web chat UI in production,
see [Web chat in production](#web-chat-in-production).

## Prerequisites

- Linux server with Python 3.10+ and a public IP or domain reachable by WATI
- Redis 6+
- API keys: Anthropic, OpenAI, Pinecone; WATI endpoint and token
- Pinecone indexes already populated (`edyoda-course-search`, `edyoda-faqs`)

## 1. Get the code and install

```bash
git clone <your-repo-url> ChatBotAgent
cd ChatBotAgent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.example .env
nano .env            # fill in real values – see docs/CONFIGURATION.md
chmod 600 .env       # only the owner can read secrets
```

`WATI_API_ENDPOINT` must be the tenant base URL only; the app appends `/api/v1/...`.

## 3. Redis

**Option A – same server (Ubuntu/Debian)**
```bash
sudo apt update && sudo apt install -y redis-server
# Cap memory at 500MB and evict only keys with a TTL (chat sessions), least-recently-used first
sudo cp redis-memory.conf /etc/redis/redis-memory.conf
echo 'include /etc/redis/redis-memory.conf' | sudo tee -a /etc/redis/redis.conf
sudo systemctl restart redis-server
sudo systemctl enable redis-server
```

**Option B – Docker**
```bash
docker run -d --name redis -p 127.0.0.1:6379:6379 \
  redis:7-alpine redis-server --maxmemory 500mb --maxmemory-policy volatile-lru
```

**Option C – managed Redis** (ElastiCache, Redis Cloud): set `REDIS_URL=redis://:password@host:6379/0`.

Set `REDIS_URL` in `.env` (e.g. `redis://localhost:6379/0`). Keep Redis bound to localhost or a private network.

When Redis hits its cap, sessions are evicted LRU; an evicted user just loses history until they write again.

## 4. Run

**One-off**
```bash
sudo -E .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 80
```
`sudo -E` keeps your environment; binding port 80 needs root. Alternatives are a reverse proxy (recommended) or
`setcap cap_net_bind_service` on the Python binary.

**Always on (systemd)**

Edit `chatbot-agent.service` first: it contains example paths (`/home/ubuntu/...`) and `User=root`. Set
`WorkingDirectory`, `EnvironmentFile` and `ExecStart` to your install path, and prefer a dedicated non-root user
with the app behind a reverse proxy on port 8000.

```bash
sudo cp chatbot-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chatbot-agent
sudo systemctl status chatbot-agent
curl http://localhost/health        # -> Hello World
```

| Command | Purpose |
|---------|---------|
| `sudo systemctl restart chatbot-agent` | Apply code or `.env` changes |
| `sudo systemctl stop chatbot-agent` | Stop |
| `sudo journalctl -u chatbot-agent -f` | Stream logs |

The unit restarts the app 5 s after a crash and starts it on boot.

## 5. Connect WATI

In the WATI dashboard, set the webhook URL to:

```
http://<YOUR_SERVER_IP_OR_DOMAIN>/webhook/wati
```

Use HTTPS in production (see below). Then simulate an inbound message to verify:

```bash
curl -X POST http://localhost/webhook/wati -H 'Content-Type: application/json' -d '{
  "eventType": "message", "owner": false, "waId": "85264318721",
  "text": "hi", "whatsappMessageId": "wamid.example", "channelPhoneNumber": "17435002445"
}'
```
The response is immediate (`Hello World`); the actual WhatsApp reply arrives after the debounce window.

For the deflection alert, create a WATI template named `chatbot_deflection_alert` with two parameters
(`{{1}}` = customer number, `{{2}}` = message) and set `DEFLECT_ALERT_NUMBERS`.

## HTTPS with a reverse proxy (recommended)

Run uvicorn on `127.0.0.1:8000` and terminate TLS in nginx or Caddy. Minimal nginx:

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;
    # ssl_certificate ... (e.g. from certbot)

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

## Web chat in production

`web_chat.py` is meant for internal testing. It has no login or rate limit, and it loads `.env.local` by default.
If you deploy it:

1. Run with `CHAT_ENV=prod` only if you intend it to share the production Redis; otherwise give it its own
   Redis DB (`REDIS_URL=.../1`).
2. Put it behind authentication (nginx basic auth, an SSO proxy, or a VPN) and add rate limiting.
3. Run without `--reload`: `uvicorn web_chat:app --host 127.0.0.1 --port 8100`.

## Security checklist

- [ ] `.env` is `chmod 600` and not in git (`git ls-files | grep .env` shows only `.env.example`).
- [ ] `ADMIN_TOKEN` is a long random value. The prompt editor pages embed the token in the page and URL, so use HTTPS and avoid sharing those links.
- [ ] `/tasks/reassign-idle` is unauthenticated; block it at the proxy (`location = /tasks/reassign-idle { deny all; }`) since the app already runs the job internally every 60 s.
- [ ] Web chat is behind auth or bound to localhost.
- [ ] Redis is not exposed to the internet and, if remote, requires a password.
- [ ] Run as a non-root user where possible.
- [ ] Keys were rotated if they were ever pasted into chat, tickets or logs.
- [ ] `chatbot_logs.db` holds real customer conversations – restrict file permissions and back it up securely.

## Upgrading

```bash
git pull
source .venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart chatbot-agent
sudo journalctl -u chatbot-agent -n 50      # check for startup errors
```
Prompt-only changes do not need a restart.
