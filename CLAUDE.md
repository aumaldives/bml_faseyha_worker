# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A thin HTTP proxy that fronts the Bank of Maldives (BML) mobile/internet-banking API. Clients
authenticate via BML's OAuth (PKCE) flow and then call proxied endpoints; this server forwards each
call to BML through **cloudscraper** (to clear Cloudflare) and passes the bank's response straight
back. It is stateless — clients hold their own access/refresh tokens and send them per request.

Runs on Windows Server. Not a git repo.

## Architecture

Request path: **client → `server.py` (FastAPI/uvicorn) → `bml_client.py` (cloudscraper) → BML**.

- **`server.py`** — FastAPI app + uvicorn entrypoint (`0.0.0.0:8000`). Routing, auth-header parsing,
  status-code passthrough, error contract, access logging. Handlers are `async`, but every bank call
  is dispatched via `run_in_threadpool(...)` because cloudscraper is blocking — this is what keeps one
  slow upstream call from stalling the event loop / blocking other clients. FastAPI's own Swagger is
  disabled (`docs_url=None`) so the custom `/docs` tester owns that path.
- **`bml_client.py`** — all BML calls + endpoint URLs/OAuth constants. `_send()` is the single choke
  point (timing logs, exception→`(502, …)`). Each worker thread keeps its **own** cloudscraper via
  `threading.local()` (a `requests.Session` is not thread-safe), so Cloudflare clearance is solved
  once per thread and reused. Functions return a **3-tuple `(status, body, location)`**.
- **`logutil.py`** — shared rotating logger; both modules call `log()` so incoming-request lines and
  bank-call lines interleave in `requests.log` (5 MB × 3). Tokens are always redacted via `redact()`.
- **`docs.html`** — self-contained interactive API tester served at `/docs` and `/`. Pure client-side
  (uses `location.origin`, `localStorage` for tokens, builds the BML login URL from a constant PKCE
  `CODE_CHALLENGE`). No server templating — edit the file directly.

### Behaviors that must be preserved when editing

- **Status-code passthrough**: `forward()` returns the bank's exact status and, if the body is valid
  JSON, the body verbatim; otherwise it wraps non-JSON as `{"status", "message"}`.
- **Redirect handling is per-endpoint**: `dashboard` and `history` call `_send(follow=False)` so the
  bank's real `302` (+ `Location`, re-emitted by `forward()`) passes through instead of resolving to
  the `200` login page. `create_session`/`refresh`/`notifications` follow redirects. (A *valid* token
  returns `200` JSON directly, so this only affects the bad/expired-token case.)
- **No timeouts**: bank calls use `timeout=None`; uvicorn runs with `timeout_keep_alive=3600`. Long
  upstream waits must never be cut off.
- **Auth tolerance**: `bearer_token()` strips a case-insensitive `Bearer ` prefix but also accepts a
  bare token. `read_input()` accepts form fields first, then JSON body.

### Endpoints

`POST /api/create_session` (field `url`|`callback_url`|`code` → exchange) · `POST /api/refresh`
(field `refresh_token`) · `GET /api/dashboard` · `GET /api/history/{id}` · `GET /api/notifications`
(query string forwarded upstream) — the three GETs require `Authorization: Bearer <token>`.
Errors: 400 missing field, 401 missing token, 404, 405 — all JSON.

## Faseyha worker layer (stateful — `/faseyha`)

On top of the stateless proxy there is a **stateful admin + background worker** that saves a BML login
server-side and pushes balance/transaction changes to your own backends.

- **`store.py`** — single JSON state blob in **Redis** (`faseyha:state`; Windows service, auto-start),
  with an automatic **file fallback** (`state.json`) if Redis is unreachable at start or a write fails.
  An existing `state.json` is migrated into Redis on first start. A throwaway key `faseyha:bml:expiry`
  carries a Redis **TTL == seconds to token expiry** for easy inspection. Holds: saved BML tokens, the
  app password (pbkdf2) + cookie sessions, poller settings, per-account balance cache + posted-tx ids.
  `update_settings()` sanitizes list/bool fields (`tracked_accounts`, `track_all`, `history_callbacks`).
  Env overrides: `FASEYHA_REDIS_URL`, `FASEYHA_REDIS_PREFIX`, `FASEYHA_NO_REDIS=1`, `FASEYHA_STATE`.
- **`poller.py`** — **two** daemon threads started on app startup (dashboard + notifications, separate
  intervals, default 10s each); **always poll while a BML session is saved**, idle only when disconnected.
  The notifications loop fetches `limit=20&group=ALERTS` (configurable) and POSTs to the notifications
  callback when the **set of alert ids changes** (first run seeds, no replay). `poll_once()` is serialized by `_poll_lock` and token refresh by
  `_refresh_lock` (double-checked, skips if refreshed in the last 10s) so the **rotating refresh token is
  never spent twice** even if the loop and a manual Poll-now overlap. Each tick: ensure a valid token
  (**auto-renew when ≤ `renew_before_sec`, default 5 days, throttled to once / 6h** — NOT at expiry),
  GET dashboard, POST the whole dashboard body **on any change**, and for **candidate** accounts (number
  starts with `account_prefix`, default `"7"`; **alias `ALI UWAIS` is hard-excluded** via
  `EXCLUDED_ALIASES`) POST **new** history transactions (deduped by tx id) **on balance change**. Which
  candidates are watched: all when `track_all`, else those in `tracked_accounts`. First sighting only
  seeds the cached balance — old tx are never replayed; a balance is advanced only after its change is
  handled, so a callback outage just retries next tick.
- **`callbacks.py`** — outbound POSTs (plain `requests`, not cloudscraper). **`history_callbacks` is a
  list (up to 2 slots)**, each `{enabled, format, url}`, all fired on a balance change; `post_history`
  returns True only if every enabled slot 2xx (retried as a set — receivers must dedupe by tx id).
  Format **A** (`json`) = `{"history":[…full tx…]}` to a `{account}`-templated URL; format **B** (`form`)
  = `account_code` + `account_data={"success":true,"payload":{"history":[…]}}`. Dashboard = raw body.
- **`eventlog.py`** — realtime log for `/faseyha/logs`: a Redis list `faseyha:logs` (newest-first, capped,
  ~1-day retention on read; file fallback `events.log`). One entry per served `/api/*` request, BML call
  (via `bml_client._send`) and callback, with status/ms/redacted-truncated body. Token values
  (`access_token`/`refresh_token`/`token`/`rtoken`) are **redacted** before storage. `record()` never raises.
- **`faseyha.html` / `logs.html` / `docs.html`** — **Tailwind CSS (Play CDN)** dark UI. `faseyha.html` is
  the gated SPA at `/faseyha`: first-run password setup → login (cookie session, path `/faseyha`) → BML
  login (same OAuth/PKCE exchange as `/docs`, but tokens are **saved**) + accounts table with track-all /
  per-account selection + settings form (interval, prefix, balance field, renew threshold, two history
  callbacks, dashboard callback, optional auth header) + link to `/faseyha/logs`. `logs.html` auto-refreshes
  the event log. A 20s status poll keeps the connection pill / last-poll line live without clobbering edits.

- **`firewall.py`** — read/modify the inbound Windows Firewall rule `BMLAPI Worker (port 8000) - allowed
  IPs` (override name with `FASEYHA_FW_RULE`) via PowerShell (the app runs as SYSTEM/Highest). Every
  IP/CIDR/range is validated with stdlib `ipaddress` before being passed in; the DisplayName is a fixed
  constant — no unvalidated user input reaches the shell. `set_allowed` refuses an empty list.

`/faseyha/api/*` routes: `app-status` (public), `app-setup`/`app-login`/`app-logout`/`change-password`,
`state`, `bml-login`/`bml-logout`, `settings`, `poll-now`, `logs`, `firewall` (GET list / POST
`{action:add|remove|set, ips:[…]}`). Pages: `/faseyha`, `/faseyha/logs`. All except `app-status`/setup/
login require the `fsid` session cookie. Callbacks are **disabled by default** until enabled in settings.

Proxy route **`GET|POST /api/forceupdate/{accountNumber}`** (firewall-gated, no app password): forces a
history fetch for that account and posts it to the history callbacks **even with no balance change**,
using the saved BML session + the `account_ids` map (number→GUID, refreshed every poll). Posted tx ids
are marked seen so the next poll won't repeat them. The /faseyha page has per-account *Force* buttons
plus an info card documenting the endpoint.

## Running / operating

The app is **already deployed** as a Windows Scheduled Task named **`BMLAPI Worker Server`** that runs
`venv\Scripts\python.exe server.py` at boot as SYSTEM (restart-on-failure, no time limit). **Redis** runs
as its own Automatic Windows service, so on machine boot both come up and the saved BML session resumes.
Listen port is `8000` (override with the `PORT` env var). New deps: `redis` (in venv + requirements).

```powershell
# uvicorn does NOT auto-reload — restart the task after editing any .py:
Stop-ScheduledTask  -TaskName 'BMLAPI Worker Server'
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask -TaskName 'BMLAPI Worker Server'

# Run in foreground for debugging instead (Ctrl+C to stop): start_server.bat  (or)
venv\Scripts\python.exe server.py

# Confirm it's listening on 0.0.0.0:8000
Get-NetTCPConnection -LocalPort 8000 -State Listen

# Watch logs
Get-Content requests.log -Tail 30 -Wait

# Dependencies (already installed in venv): fastapi uvicorn python-multipart cloudscraper requests
venv\Scripts\python.exe -m pip install <pkg>
```

There are no automated tests. Verify changes by hitting endpoints (`curl.exe -i ...`) and checking
status codes + `requests.log`. The interactive `/docs` page drives the full token flow end-to-end.

Note: `docs.html` is browser-rendered over plain HTTP from remote IPs, so it relies only on
`crypto.getRandomValues` (works in non-secure contexts); do not introduce `crypto.subtle` there.

## Network exposure

Bound to `0.0.0.0:8000` but a Windows Firewall rule (`BMLAPI Worker (port 8000) - allowed IPs`)
restricts inbound TCP 8000 to specific client IPs. The stateless `/api/*` proxy has **no app-level auth**
beyond the bank tokens — keep that access control at the firewall. The `/faseyha` layer **is** password
gated (cookie session), but it is still single-tenant and trusts the firewall as the outer boundary.
Redis listens on `127.0.0.1:6379` only (localhost), so it is not exposed off-box.
