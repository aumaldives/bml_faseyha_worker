# BML Faseyha Worker

A self-hosted service for the **Bank of Maldives (BML)** mobile/internet-banking API. It has two layers:

1. **A thin stateless proxy** (`/api/*`) — authenticate through BML's OAuth (PKCE) flow, then call simple
   JSON endpoints. Each call is forwarded to BML through [cloudscraper](https://pypi.org/project/cloudscraper/)
   (to clear Cloudflare) and the bank's response is passed straight back. Clients hold their own tokens.
2. **A stateful worker + admin** (`/faseyha`) — saves a BML login **server-side**, polls the dashboard on a
   schedule, and **pushes balance/transaction changes to your own backends** via configurable callbacks. It
   keeps a per-account balance cache, auto-renews the access token, and ships an admin UI, a realtime log
   viewer, a force-update API and a firewall IP manager.

Built with **FastAPI + uvicorn**, state in **Redis** (with a JSON-file fallback). Runs on Windows Server.

> **Unofficial.** Not affiliated with or endorsed by Bank of Maldives. Use responsibly and only with your
> own account/credentials.

---

## How it works

```
                          ┌───────────────────────── /faseyha admin + poller ──────────────────────────┐
client ──▶ server.py (FastAPI) ──▶ bml_client.py (cloudscraper) ──▶ BML        poller ──▶ your callbacks │
   │            │                                                              (history / dashboard push)│
   └─ /api/* ───┘            store.py ──▶ Redis (state, tokens, settings)  ◀── eventlog ──▶ /faseyha/logs │
                          └────────────────────────────────────────────────────────────────────────────┘
```

| Module | Role |
|--------|------|
| `server.py` | FastAPI app + uvicorn entry. Routing, status-code passthrough, access logging, the `/faseyha` routes, starts the poller on startup. |
| `bml_client.py` | All BML calls. One warm cloudscraper **per worker thread** (Cloudflare solved once, reused). Returns `(status, body, location)`. |
| `store.py` | State as one JSON blob in **Redis** (`faseyha:state`), automatic **file fallback** (`state.json`). Saved tokens, app password (pbkdf2) + sessions, settings, balance cache, posted-tx ids, account-id map. |
| `poller.py` | Background daemon thread: refresh token → fetch dashboard → push changes. Serialized + race-safe token refresh. |
| `callbacks.py` | Outbound POSTs to your backends (history × up to 2 destinations, dashboard). |
| `eventlog.py` | Realtime log (Redis list, ~1-day retention, file fallback). Response time/code/body, tokens redacted. |
| `firewall.py` | Read/modify the Windows Firewall allow-list for the port. |
| `docs.html` · `faseyha.html` · `logs.html` | Tailwind UIs: API tester, admin dashboard, log viewer. |

---

## Requirements

- Python 3.11+ (developed on 3.14)
- **Redis** reachable at `127.0.0.1:6379` (on Windows: the [tporadowski Redis](https://github.com/tporadowski/redis)
  build installs as an auto-start service). Optional — the worker falls back to a local `state.json` if Redis is down.
- Outbound internet access to `bankofmaldives.com.mv`

## Setup

```bash
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
# venv/bin/pip install -r requirements.txt                   # Linux/macOS
```

## Run

```bash
venv\Scripts\python.exe server.py        # serves on 0.0.0.0:8000 (override with PORT env)
```

Open `http://<host>:8000/docs` for the API tester or `http://<host>:8000/faseyha` for the admin dashboard.
To run on boot (Windows), register a Scheduled Task that runs `venv\Scripts\python.exe server.py`. Redis runs
as its own auto-start service, so on reboot both come up and the saved BML session resumes.

---

## The proxy API (`/api/*`)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/create_session` | — | Exchange an OAuth callback URL / `code` for tokens |
| POST | `/api/refresh` | — | Exchange a refresh token for a new access token |
| GET | `/api/dashboard` | Bearer | Mobile dashboard |
| GET | `/api/history/{id}` | Bearer | Today's transaction history for an account id |
| GET | `/api/statement/{id}/{from}/{to}` | Bearer | Account statement (CSV) between two dates |
| GET | `/api/notifications` | Bearer | Notifications (query params forwarded upstream) |
| GET·POST | `/api/forceupdate/{accountNumber}` | firewall | Force-fetch history + push to callbacks (see below) |
| GET | `/docs` | — | Interactive tester |

Authenticated endpoints take `Authorization: Bearer <access_token>`. Bodies accept form-encoded **or** JSON.
The bank's status code is passed through; JSON bodies are returned verbatim. `dashboard`/`history`/`statement`
do **not** follow redirects, so an expired token surfaces the bank's real `302` instead of a login page.

---

## The worker layer (`/faseyha`)

A password-gated admin (cookie session) on top of the proxy:

- **Server-side BML login** — same OAuth/PKCE exchange as `/docs`, but the tokens are **saved**. The access
  token **auto-renews when ≤ 5 days remain** (configurable, throttled). Refresh is race-safe — never spent twice.
- **Background poller** — fetches the dashboard every N seconds whenever a session is connected. On **any
  dashboard change** it POSTs the whole dashboard to a callback; for watched accounts, on a **balance change**
  it fetches history and POSTs the **new** transactions (deduped by tx id; first sighting only seeds the balance).
- **Account selection** — candidate accounts are those whose number starts with a prefix (default `7`); personal
  accounts (alias `ALI UWAIS`) are hard-excluded. *Track all* or pick specific accounts.
- **Two history callbacks** — each `{enabled, format, url}`:
  - **Format A** (`json`): `POST {url}` with `{"history":[…]}` (use `{account}` in the URL for the number).
  - **Format B** (`form`): `account_code=<number>` + `account_data={"success":true,"payload":{"history":[…]}}`.
- **Dashboard callback** — POSTs the full dashboard body verbatim on any change.
- **Notifications poll** — a second, independently-timed loop fetches the app-domain notifications
  (`limit=20&group=ALERTS`, query configurable) and POSTs the response to **up to two callbacks** when the
  **set of alert ids changes** (read/unread flips ignored; first run only seeds). Dashboard and
  notifications intervals are configured **separately** (default **10s** each).
- **Force-update API** — `GET|POST /api/forceupdate/{accountNumber}` fetches an account's history and pushes it
  to the enabled callbacks **even with no balance change**, then returns the data:

  ```bash
  curl -X POST http://<host>:8000/api/forceupdate/7730000175583
  # { "ok": true, "account": "...", "id": "...", "transactions": 12,
  #   "callbacks_enabled": 1, "posted_ok": true, "history": [ { "id": "FT...", "amount": 100, ... } ] }
  ```

- **Realtime logs** — `/faseyha/logs` shows every served request, BML call and callback with status,
  response time and a viewable (token-redacted) body. Backed by a Redis list (~1-day retention).
- **Firewall manager** — view/add/remove the IPs allowed to reach the port (Windows Firewall rule), with
  IP/CIDR/range validation.

### `/faseyha/api/*` routes

`app-status` (public) · `app-setup` / `app-login` / `app-logout` / `change-password` · `state` ·
`bml-login` / `bml-logout` · `settings` · `poll-now` · `logs` · `firewall`. All except `app-status`/setup/login
require the session cookie. **Callbacks are disabled by default** until enabled in settings.

---

## Logging

All requests and upstream calls are logged to `requests.log` (rotating, 5 MB × 3) with timing; access tokens
are always redacted. The structured realtime log lives in Redis (`faseyha:logs`) and is viewable at `/faseyha/logs`.

## Security

- The proxy `/api/*` has **no app-level auth** beyond the bank tokens — access control is the **firewall**
  IP allow-list (managed from `/faseyha`). The `/faseyha` admin **is** password-gated.
- Redis listens on `127.0.0.1` only. Tokens are redacted in logs and never stored in the event log.
- Never commit real tokens or `state.json` (already in `.gitignore`).
- Prefer running behind HTTPS (reverse proxy) if exposed beyond a trusted network.
