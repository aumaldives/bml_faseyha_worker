"""Outbound callbacks to the faseyha backends.

These post to your own servers (not Cloudflare-fronted BML), so plain `requests`
is used, not cloudscraper.

History callbacks (list — each enabled slot is fired independently on a balance
change; receivers should dedupe by transaction id so a retry is harmless):

  format "json" (A):
    POST <url with {account} substituted>
    Content-Type: application/json
    {"history": [ <full tx objects, as BML returns them> ]}
    e.g. https://admin.faseyha.app/api/payment_callback/bank/7730000175583

  format "form" (B):
    POST <url>
    Content-Type: application/x-www-form-urlencoded
    account_code=<account number>
    account_data={"success":true,"payload":{"history":[ <full tx objects> ]}}

Dashboard push (single):
    POST <dashboard_cb_url>
    Content-Type: application/json
    <the entire BML dashboard response body, verbatim>

post_history returns True only if every enabled slot returned 2xx (so the poller
retries the whole set next tick on any failure). Each attempt is also written to
the realtime event log.
"""

import json
import time

import requests

import eventlog
from logutil import log

TIMEOUT = 20


def _auth_header(settings):
    val = (settings.get("callback_auth") or "").strip()
    return {"Authorization": val} if val else {}


def _post_one_history(cb, settings, account, new_tx):
    fmt = cb.get("format") or "json"
    url_tpl = (cb.get("url") or "").strip()
    if not url_tpl:
        return True                       # empty slot: nothing to do
    url = url_tpl.replace("{account}", account) if "{account}" in url_tpl else url_tpl
    headers = _auth_header(settings)
    t0 = time.perf_counter()
    try:
        if fmt == "form":
            envelope = {"success": True, "payload": {"history": new_tx}}
            resp = requests.post(
                url, data={"account_code": account, "account_data": json.dumps(envelope)},
                headers=headers, timeout=TIMEOUT)
        else:  # "json" -> format A
            resp = requests.post(
                url, data=json.dumps({"history": new_tx}).encode("utf-8"),
                headers={**headers, "Content-Type": "application/json"}, timeout=TIMEOUT)
        ms = (time.perf_counter() - t0) * 1000
        ok = 200 <= resp.status_code < 300
        log(f"callback history -> {url} [{fmt}] {len(new_tx)} tx -> {resp.status_code}"
            f"{'' if ok else ' (will retry)'}")
        eventlog.record("callback", f"history[{fmt}] {url} ({len(new_tx)} tx)",
                        resp.status_code, ms, resp.text)
        return ok
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        log(f"callback history -> {url} FAILED: {type(e).__name__}: {e}")
        eventlog.record("callback", f"history[{fmt}] {url}", None, ms,
                        f"{type(e).__name__}: {e}", ok=False)
        return False


def post_history(settings, account, new_tx):
    """Fire every enabled history callback. True only if all enabled ones succeed."""
    cbs = [c for c in (settings.get("history_callbacks") or [])
           if c.get("enabled") and (c.get("url") or "").strip()]
    if not cbs:
        return True                       # none configured/enabled: treat as handled
    all_ok = True
    for cb in cbs:
        if not _post_one_history(cb, settings, account, new_tx):
            all_ok = False
    return all_ok


def post_dashboard(settings, dashboard_body):
    """POST the full dashboard response verbatim. Returns True on 2xx."""
    return _post_body(settings.get("dashboard_cb_url"), settings, dashboard_body, "dashboard")


def post_notifications(settings, notifications_body):
    """Fire every enabled notification callback (up to 2) with the ALERTS body verbatim.
    True only if all enabled ones 2xx (None enabled -> True)."""
    cbs = [c for c in (settings.get("notification_callbacks") or [])
           if c.get("enabled") and (c.get("url") or "").strip()]
    if not cbs:
        return True
    all_ok = True
    for c in cbs:
        if not _post_body(c["url"], settings, notifications_body, "notifications"):
            all_ok = False
    return all_ok


def _post_body(url, settings, body, label):
    url = (url or "").strip()
    if not url:
        log(f"callback {label}: no url configured")
        return False
    headers = {**_auth_header(settings), "Content-Type": "application/json"}
    t0 = time.perf_counter()
    try:
        resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=TIMEOUT)
        ms = (time.perf_counter() - t0) * 1000
        ok = 200 <= resp.status_code < 300
        log(f"callback {label} -> {url} {len(body)}B -> {resp.status_code}")
        eventlog.record("callback", f"{label} {url} ({len(body)}B)", resp.status_code, ms, resp.text)
        return ok
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        log(f"callback {label} -> {url} FAILED: {type(e).__name__}: {e}")
        eventlog.record("callback", f"{label} {url}", None, ms, f"{type(e).__name__}: {e}", ok=False)
        return False
