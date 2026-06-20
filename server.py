"""BML API worker — FastAPI app served by uvicorn.

Replaces the single-threaded PHP server. Routes, status-code passthrough and the
interactive /docs tester match the previous router.php behaviour exactly. The bank
calls run in-process (bml_client) with warm per-thread cloudscrapers, and the
blocking cloudscraper calls are dispatched to a threadpool so they never stall the
async event loop (so one slow request can't block the others).
"""

import os
import json
import time
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import Response, FileResponse
from fastapi.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

import bml_client as bml
import store
import poller
import eventlog
import firewall
from logutil import log

HERE = os.path.dirname(os.path.abspath(__file__))

# Disable FastAPI's own Swagger/OpenAPI at /docs so our custom tester owns that path.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# --------------------------------------------------------------------- helpers
def bearer_token(request: Request):
    """Access token from the Authorization header (strip 'Bearer ', tolerate bare)."""
    auth = (request.headers.get("authorization") or "").strip()
    if not auth:
        return None
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return auth  # tolerate a bare token


async def read_input(request: Request, *keys):
    """First non-empty value among keys: form fields first, then JSON body."""
    data = {}
    try:
        form = await request.form()
        for k, v in form.items():
            data[k] = v
    except Exception:
        pass
    if not any(data.get(k) for k in keys):
        try:
            body = await request.json()
            if isinstance(body, dict):
                for k, v in body.items():
                    data.setdefault(k, v)
        except Exception:
            pass
    for key in keys:
        val = data.get(key)
        if val:
            return str(val)
    return None


def extract_code(value):
    """Pull ?code=... out of a pasted callback URL, else return the value as-is."""
    value = (value or "").strip()
    if "code=" in value:
        qs = parse_qs(urlparse(value).query)
        if qs.get("code"):
            return qs["code"][0]
    return value


def to_bml_date(value):
    """Normalise a path date to BML's MM/DD/YYYY format.

    A URL path can't carry slashes inside one segment, so the statement route
    takes dates as YYYY-MM-DD (what <input type=date> sends) or MM-DD-YYYY.
    Anything unrecognised is passed through unchanged.
    """
    s = (value or "").strip().replace("/", "-")
    parts = s.split("-")
    if len(parts) == 3 and all(parts):
        y, m, d = (parts if len(parts[0]) == 4 else (parts[2], parts[0], parts[1]))
        try:
            return f"{int(m):02d}/{int(d):02d}/{int(y):04d}"
        except ValueError:
            return s
    return s


def forward(status, body, location=None):
    """Pass the bank's status code through; return its JSON verbatim, else wrap it.

    On a redirect (dashboard/history with follow=False) the upstream Location is
    passed through so the client gets a real 3xx response, not a swallowed 200.
    """
    try:
        json.loads(body)
        payload = body  # already valid JSON -> verbatim
    except (ValueError, TypeError):
        payload = json.dumps({"status": status, "message": body})
    resp = Response(content=payload, status_code=status, media_type="application/json")
    if location:
        resp.headers["Location"] = location
    return resp


def json_error(status, message):
    return Response(content=json.dumps({"error": message}),
                    status_code=status, media_type="application/json")


def json_ok(payload, status=200):
    return Response(content=json.dumps(payload),
                    status_code=status, media_type="application/json")


# --------------------------------------------------- /faseyha app-login (cookie)
COOKIE = "fsid"
COOKIE_PATH = "/faseyha"


def app_authed(request: Request):
    """True when the request carries a valid app-login session cookie."""
    return store.valid_session(request.cookies.get(COOKIE))


def _set_session_cookie(resp, sid):
    resp.set_cookie(COOKIE, sid, max_age=30 * 86400, path=COOKIE_PATH,
                    httponly=True, samesite="lax")


# ---------------------------------------------------------------------- routes
@app.post("/api/create_session")
async def create_session(request: Request):
    url = await read_input(request, "url", "callback_url", "code")
    if url is None:
        return json_error(400, 'Missing "url" (the OAuth callback URL or code)')
    return forward(*await run_in_threadpool(bml.exchange, extract_code(url)))


@app.post("/api/refresh")
async def refresh(request: Request):
    rt = await read_input(request, "refresh_token")
    if rt is None:
        return json_error(400, 'Missing "refresh_token"')
    return forward(*await run_in_threadpool(bml.refresh, rt))


@app.get("/api/dashboard")
async def dashboard(request: Request):
    token = bearer_token(request)
    if token is None:
        return json_error(401, "Missing Authorization: Bearer <access_token>")
    return forward(*await run_in_threadpool(bml.dashboard, token))


@app.get("/api/history/{account_id}")
async def history(account_id: str, request: Request):
    token = bearer_token(request)
    if token is None:
        return json_error(401, "Missing Authorization: Bearer <access_token>")
    return forward(*await run_in_threadpool(bml.history, token, account_id))


@app.get("/api/statement/{account_id}/{from_date}/{to_date}")
async def statement(account_id: str, from_date: str, to_date: str, request: Request):
    token = bearer_token(request)
    if token is None:
        return json_error(401, "Missing Authorization: Bearer <access_token>")
    start = to_bml_date(from_date)
    end = to_bml_date(to_date)
    status, body, _location = await run_in_threadpool(
        bml.statement, token, account_id, start, end)
    if 200 <= status < 300:
        # Upstream returns CSV text (not JSON) -> pass it straight through.
        return Response(content=body, status_code=status,
                        media_type="text/csv; charset=utf-8")
    if status in (301, 302, 303, 307, 308):
        # Bad/expired token makes the bank redirect to its login page.
        return json_error(401, "Session expired or invalid token - re-authenticate")
    # Any other upstream error: bank's status + JSON body (or wrapped non-JSON).
    return forward(status, body)


@app.get("/api/notifications")
async def notifications(request: Request):
    token = bearer_token(request)
    if token is None:
        return json_error(401, "Missing Authorization: Bearer <access_token>")
    query = request.url.query  # raw query string, forwarded upstream as-is
    return forward(*await run_in_threadpool(bml.notifications, token, query))


@app.api_route("/api/forceupdate/{account_number}", methods=["GET", "POST"])
async def force_update(account_number: str, request: Request):
    """Force-fetch an account's history and push it to the configured history
    callbacks NOW — even if no balance change was detected. Uses the saved BML
    session + saved account-id map (firewall-gated like the other /api routes)."""
    result = await run_in_threadpool(poller.force_update, account_number)
    if result.get("ok"):
        return json_ok(result)
    err = (result.get("error") or "").lower()
    code = 409 if "not logged" in err else 404 if "unknown account" in err \
        else 502 if "history" in err else 400
    return json_ok(result, code)


@app.get("/docs")
@app.get("/")
async def docs():
    return FileResponse(os.path.join(HERE, "docs.html"), media_type="text/html")


# ============================================================ /faseyha admin layer
@app.get("/faseyha")
async def faseyha_page():
    return FileResponse(os.path.join(HERE, "faseyha.html"), media_type="text/html")


@app.get("/faseyha/api/app-status")
async def faseyha_app_status(request: Request):
    """Public: does an app password exist yet, and is this browser logged in?"""
    return json_ok({"has_password": store.has_password(), "authed": app_authed(request)})


@app.post("/faseyha/api/app-setup")
async def faseyha_app_setup(request: Request):
    """First-run only: create the app password and log in."""
    if store.has_password():
        return json_error(409, "Password already set")
    pw = await read_input(request, "password")
    if not pw or len(pw) < 4:
        return json_error(400, "Password must be at least 4 characters")
    store.set_password(pw)
    resp = json_ok({"ok": True})
    _set_session_cookie(resp, store.create_session())
    return resp


@app.post("/faseyha/api/app-login")
async def faseyha_app_login(request: Request):
    if not store.has_password():
        return json_error(412, "No password set yet — set one first")
    pw = await read_input(request, "password")
    if not pw or not store.check_password(pw):
        return json_error(401, "Wrong password")
    resp = json_ok({"ok": True})
    _set_session_cookie(resp, store.create_session())
    return resp


@app.post("/faseyha/api/app-logout")
async def faseyha_app_logout(request: Request):
    store.destroy_session(request.cookies.get(COOKIE))
    resp = json_ok({"ok": True})
    resp.delete_cookie(COOKIE, path=COOKIE_PATH)
    return resp


@app.post("/faseyha/api/change-password")
async def faseyha_change_password(request: Request):
    if not app_authed(request):
        return json_error(401, "Not authenticated")
    old = await read_input(request, "old_password")
    new = await read_input(request, "new_password")
    if not store.check_password(old or ""):
        return json_error(401, "Current password is wrong")
    if not new or len(new) < 4:
        return json_error(400, "New password must be at least 4 characters")
    store.set_password(new)
    return json_ok({"ok": True})


@app.get("/faseyha/api/state")
async def faseyha_state(request: Request):
    if not app_authed(request):
        return json_error(401, "Not authenticated")
    return json_ok(store.public_state())


@app.post("/faseyha/api/bml-login")
async def faseyha_bml_login(request: Request):
    """Exchange a BML callback URL/code for tokens and SAVE them server-side."""
    if not app_authed(request):
        return json_error(401, "Not authenticated")
    url = await read_input(request, "url", "callback_url", "code")
    if url is None:
        return json_error(400, 'Missing "url" (the OAuth callback URL or code)')
    status, body, _loc = await run_in_threadpool(bml.exchange, extract_code(url))
    if status == 200:
        try:
            saved = store.save_auth(json.loads(body))
        except (ValueError, TypeError):
            saved = False
        if saved:
            poller.kick()                 # poll immediately with the fresh session
            return json_ok({"ok": True})
    return forward(status, body)          # surface the bank's error verbatim


@app.post("/faseyha/api/bml-logout")
async def faseyha_bml_logout(request: Request):
    if not app_authed(request):
        return json_error(401, "Not authenticated")
    store.clear_auth()
    return json_ok({"ok": True})


@app.post("/faseyha/api/settings")
async def faseyha_settings(request: Request):
    if not app_authed(request):
        return json_error(401, "Not authenticated")
    try:
        patch = await request.json()
    except Exception:
        patch = None
    if not isinstance(patch, dict):
        return json_error(400, "Expected a JSON object of settings")
    return json_ok({"ok": True, "settings": store.update_settings(patch)})


@app.post("/faseyha/api/poll-now")
async def faseyha_poll_now(request: Request):
    if not app_authed(request):
        return json_error(401, "Not authenticated")
    await run_in_threadpool(poller.poll_once)
    return json_ok({"ok": True, "last_poll": store.snapshot().get("last_poll") or {}})


@app.get("/faseyha/logs")
async def faseyha_logs_page():
    return FileResponse(os.path.join(HERE, "logs.html"), media_type="text/html")


@app.get("/faseyha/api/logs")
async def faseyha_logs(request: Request):
    if not app_authed(request):
        return json_error(401, "Not authenticated")
    try:
        limit = max(1, min(1000, int(request.query_params.get("limit", "200"))))
    except (TypeError, ValueError):
        limit = 200
    return json_ok({"events": eventlog.recent(limit)})


@app.get("/faseyha/api/firewall")
async def faseyha_firewall_get(request: Request):
    if not app_authed(request):
        return json_error(401, "Not authenticated")
    ips, err = await run_in_threadpool(firewall.get_allowed)
    if ips is None:
        return json_error(500, err)
    return json_ok({"rule": firewall.RULE, "addresses": ips})


@app.post("/faseyha/api/firewall")
async def faseyha_firewall_post(request: Request):
    if not app_authed(request):
        return json_error(401, "Not authenticated")
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return json_error(400, "Expected a JSON object")
    action = body.get("action")
    ips = [str(x).strip() for x in (body.get("ips") or []) if str(x).strip()]
    if action not in ("add", "remove", "set"):
        return json_error(400, 'action must be "add", "remove" or "set"')
    if action in ("add", "set"):
        bad = [i for i in ips if not firewall.valid_ip(i)]
        if bad:
            return json_error(400, "invalid IP(s): " + ", ".join(bad))
    current, err = await run_in_threadpool(firewall.get_allowed)
    if current is None:
        return json_error(500, err)
    if action == "add":
        target = current + [i for i in ips if i not in current]
    elif action == "remove":
        drop = set(ips)
        target = [i for i in current if i not in drop]
    else:  # set
        target = ips
    ok, err = await run_in_threadpool(firewall.set_allowed, target)
    if not ok:
        return json_error(400, err)
    final, _ = await run_in_threadpool(firewall.get_allowed)
    return json_ok({"ok": True, "addresses": final if final is not None else target})


# --------------------------------------------------------------- error handlers
@app.exception_handler(StarletteHTTPException)
async def http_exception(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return json_error(404, "Not found: " + request.url.path)
    if exc.status_code == 405:
        return json_error(405, "Method not allowed")
    return json_error(exc.status_code, exc.detail or "error")


# ---------------------------------------------------- start the background poller
@app.on_event("startup")
async def _startup():
    poller.start()


# -------------------------------------------------- per-request access logging
@app.middleware("http")
async def access_log(request: Request, call_next):
    t0 = time.perf_counter()
    client = request.client.host if request.client else "?"
    q = ("?" + request.url.query) if request.url.query else ""
    log(f"--> {client} {request.method} {request.url.path}{q}")
    resp = await call_next(request)
    dt = (time.perf_counter() - t0) * 1000
    log(f"<== {client} {request.method} {request.url.path} "
        f"-> {resp.status_code} ({dt:.0f}ms)")
    # realtime event log: only the proxy endpoints (skip the /faseyha admin UI's
    # own polling so the log stays meaningful). No body here — bodies for the bank
    # calls are captured at the bml_client layer.
    if request.url.path.startswith("/api/"):
        eventlog.record("http", f"{client} {request.method} {request.url.path}{q}",
                        resp.status_code, dt)
    return resp


# --------------------------------------------------------------------- run it
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    log(f"server starting on 0.0.0.0:{port}")
    # No request-handler timeout (handlers can run as long as the bank needs);
    # generous keep-alive so a long upstream wait is never cut off mid-response.
    uvicorn.run(app, host="0.0.0.0", port=port,
                timeout_keep_alive=3600, log_level="warning", access_log=False)
