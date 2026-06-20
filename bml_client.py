"""In-process Bank of Maldives client (cloudscraper).

Replaces the per-request `token_helper.py` subprocess: the server imports these
functions and calls them directly. Each worker thread keeps its own warm
cloudscraper so the Cloudflare clearance is solved once per thread and reused.

Every function returns a (status:int, body:str) tuple.
"""

import time
import threading
import traceback

import cloudscraper

import eventlog
from logutil import log

# --- BML endpoints -----------------------------------------------------------
IB_BASE       = "https://www.bankofmaldives.com.mv/internetbanking"
APP_BASE      = "https://app.bankofmaldives.com.mv"
TOKEN_URL     = f"{IB_BASE}/oauth/token"
DASHBOARD_URL = f"{IB_BASE}/api/mobile/dashboard"
NOTIF_URL     = f"{APP_BASE}/api/v2/notifications"

REDIRECT_URI  = "https://app.bankofmaldives.com.mv/oauth/mobile-callback"
CLIENT_ID     = "98C83590-513F-4716-B02B-EC68B7D9E7E7"
CODE_VERIFIER = "9n_1EIfI_5LfHLVHCy3dCi4YvDABsaeWLZ5wUGBz9cE"

# None = never time out; always wait for bankofmaldives.com.mv to respond.
TIMEOUT = None

# One cloudscraper per worker thread (requests.Session is not thread-safe).
_local = threading.local()


def redact(token):
    """Never log full tokens; keep just enough to correlate."""
    if not token:
        return "<empty>"
    return f"<len={len(token)} ...{token[-6:]}>"


def _scraper():
    """Return this thread's cloudscraper, creating (and warming) it on first use."""
    s = getattr(_local, "scraper", None)
    if s is None:
        t0 = time.perf_counter()
        s = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "android", "desktop": False}
        )
        s.headers.update({
            "x-app-version": "2.1.27.266",
            "Origin": "https://www.bankofmaldives.com.mv",
            "Referer": "https://www.bankofmaldives.com.mv/internetbanking/",
        })
        _local.scraper = s
        log(f"new cloudscraper for thread (+{(time.perf_counter() - t0) * 1000:.0f}ms)")
    return s


def _send(method, url, follow=True, **kwargs):
    """Issue one upstream request, log timing, return (status, body, location).

    When follow=False, redirects are NOT followed, so the bank's real status code
    (e.g. a 302 that would otherwise resolve to the 200 login page) is passed
    straight through. `location` is the upstream Location header on a redirect,
    else None. dashboard/history use follow=False; the rest follow as before.
    """
    s = _scraper()
    t0 = time.perf_counter()
    try:
        resp = s.request(method, url, timeout=TIMEOUT, allow_redirects=follow, **kwargs)
    except Exception as e:
        total = (time.perf_counter() - t0) * 1000
        log(f"EXCEPTION after {total:.0f}ms on {method} {url}: {type(e).__name__}: {e}")
        log("TRACEBACK: " + traceback.format_exc().replace("\n", " | "))
        eventlog.record("bml", f"{method} {url}", 502, total,
                        f"{type(e).__name__}: {e}", ok=False)
        return 502, f"request to bank failed: {type(e).__name__}: {e}", None
    net = resp.elapsed.total_seconds() * 1000
    total = (time.perf_counter() - t0) * 1000
    location = resp.headers.get("Location")
    redir = f" -> {location}" if location else ""
    log(f"<- {resp.status_code}{redir} net={net:.0f}ms "
        f"total={total:.0f}ms bytes={len(resp.text)}")
    body = resp.text + (f"\n[Location: {location}]" if location else "")
    eventlog.record("bml", f"{method} {url}", resp.status_code, total, body)
    return resp.status_code, resp.text, location


# --- public actions ----------------------------------------------------------
def exchange(code):
    log(f"-> POST {TOKEN_URL} (exchange, code len={len(code)})")
    return _send("POST", TOKEN_URL, data={
        "code": code,
        "code_verifier": CODE_VERIFIER,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
    })


def refresh(refresh_token):
    log(f"-> POST {TOKEN_URL} (refresh, refresh_token={redact(refresh_token)})")
    return _send("POST", TOKEN_URL, data={
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
    })


def dashboard(access_token):
    log(f"-> GET {DASHBOARD_URL} (dashboard, token={redact(access_token)})")
    return _send("GET", DASHBOARD_URL, follow=False,
                 headers={"Authorization": f"Bearer {access_token}"})


def history(access_token, account_id):
    url = f"{IB_BASE}/api/mobile/account/{account_id}/history/today"
    log(f"-> GET {url} (history, token={redact(access_token)})")
    return _send("GET", url, follow=False,
                 headers={"Authorization": f"Bearer {access_token}"})


def notifications(access_token, query=""):
    url = NOTIF_URL + (f"?{query}" if query else "")
    log(f"-> GET {url} (notifications, token={redact(access_token)})")
    return _send("GET", url, headers={"Authorization": f"Bearer {access_token}"})


def statement(access_token, account_id, startdate, enddate):
    """Account statement as CSV text. Upstream wants dates as MM/DD/YYYY.

    follow=False like dashboard/history: a bad/expired token redirects to the
    login page, and we want that real 3xx (not the resolved 200) so the caller
    can turn it into a clean error instead of returning the login HTML as CSV.
    """
    url = f"{IB_BASE}/api/account/{account_id}/download/csv"
    log(f"-> POST {url} (statement, token={redact(access_token)}, {startdate}..{enddate})")
    return _send("POST", url, follow=False,
                 headers={"Authorization": f"Bearer {access_token}"},
                 json={"startdate": startdate, "enddate": enddate})
