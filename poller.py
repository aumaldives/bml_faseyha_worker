"""Background poller: watch BML balances, push history + dashboard changes.

A single daemon thread, started from server.py's startup. Each tick:

  1. Ensure a valid BML access token (proactively refresh when < 5 min left).
  2. GET the BML dashboard.
  3. If the dashboard (accounts array) changed since last time, POST the whole
     response to the dashboard callback.
  4. For every account whose number starts with the configured prefix ("77300"),
     compare the watched balance field to the cached value. On change, fetch that
     account's history and POST the transactions that haven't been posted before
     (deduped by transaction id).

First sighting of an account only seeds its cached balance (no history fetch), so
existing transactions are never replayed. A balance is only advanced once its
change has been handled successfully, so a callback outage just retries next tick.
"""

import time
import json
import hashlib
import threading

import bml_client as bml
import callbacks
import store
from logutil import log

_started = False
_start_lock = threading.Lock()
_wake = threading.Event()       # set by kick() to poll the dashboard immediately
_notif_wake = threading.Event()  # set by kick() to poll notifications immediately
_poll_lock = threading.Lock()   # serialize dashboard polls (background loop vs manual poll-now)
_refresh_lock = threading.Lock()  # collapse concurrent token refreshes into one
_notif_lock = threading.Lock()  # serialize notification polls

_REDIRECTS = (301, 302, 303, 307, 308)

# Hard excluded: personal accounts are never shown or tracked, regardless of settings.
EXCLUDED_ALIASES = {"ALI UWAIS"}


def start():
    """Start the poller threads once (idempotent): dashboard + notifications."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        threading.Thread(target=_run, name="poller", daemon=True).start()
        threading.Thread(target=_run_notifications, name="poller-notif", daemon=True).start()
        log("poller threads started (dashboard + notifications)")


def kick():
    """Ask both loops to poll right now (e.g. just after a fresh BML login)."""
    _wake.set()
    _notif_wake.set()


def _run():
    while True:
        try:
            poll_once()
        except Exception as e:
            log(f"poller: ERROR {type(e).__name__}: {e}")
            store.mutate(lambda s: s.__setitem__(
                "last_poll", {"at": time.time(), "ok": False, "message": f"error: {e}"}))
        interval = (store.get_settings().get("poll_interval") or 10)
        _wake.wait(timeout=max(5, int(interval)))
        _wake.clear()


def _run_notifications():
    while True:
        try:
            poll_notifications_once()
        except Exception as e:
            log(f"poller: notifications ERROR {type(e).__name__}: {e}")
        interval = (store.get_settings().get("notifications_interval") or 10)
        _notif_wake.wait(timeout=max(5, int(interval)))
        _notif_wake.clear()


# ----------------------------------------------------------------------- token mgmt
def _save_token_response(status, body):
    if status != 200:
        return None
    try:
        j = json.loads(body)
    except Exception:
        return None
    return j.get("access_token") if store.save_auth(j) else None


def _refresh_token(force=False):
    """Refresh the access token AT MOST ONCE across threads (double-checked lock).

    Renews when fewer than `renew_before_sec` (default 5 days) remain, throttled to
    once per `renew_min_interval_sec` (default 6h). `force=True` skips those checks
    (used when the bank just rejected the token) but STILL collapses concurrent
    callers: if another thread refreshed within the last few seconds we reuse it,
    so the rotating refresh token is never spent twice.
    """
    with _refresh_lock:
        auth = store.snapshot().get("auth") or {}
        at = auth.get("access_token")
        rt = auth.get("refresh_token")
        if not rt:
            return at
        now = time.time()
        last = auth.get("last_refresh_at") or 0
        if (now - last) < 10:          # someone refreshed while we waited for the lock
            return at
        if not force:
            settings = store.get_settings()
            before = settings.get("renew_before_sec") or 5 * 86400
            min_iv = settings.get("renew_min_interval_sec") or 6 * 3600
            exp = auth.get("expires_at") or 0
            if not (exp and (exp - now) < before and (now - last) >= min_iv):
                return at              # no longer needs renewing
            log(f"poller: token has {(exp - now) / 86400:.1f}d left "
                f"(< {before / 86400:.0f}d) — auto-renewing")
        else:
            log("poller: bank rejected token — renewing now")
        tok = _save_token_response(*bml.refresh(rt)[:2])
        if not tok:
            log("poller: token renew failed; keeping existing token")
        return tok or at


def _ensure_token():
    """Valid access token, auto-renewing when little life is left. None if not logged in."""
    s = store.snapshot()
    auth = s.get("auth") or {}
    at = auth.get("access_token")
    if not at:
        return None
    settings = s.get("settings") or {}
    before = settings.get("renew_before_sec") or 5 * 86400
    min_iv = settings.get("renew_min_interval_sec") or 6 * 3600
    now = time.time()
    exp = auth.get("expires_at") or 0
    last = auth.get("last_refresh_at") or 0
    if (exp and (exp - now) < before and (now - last) >= min_iv
            and auth.get("refresh_token")):
        return _refresh_token(force=False)
    return at


# ----------------------------------------------------------------------- poll
def _record(ok, message):
    store.mutate(lambda s: s.__setitem__(
        "last_poll", {"at": time.time(), "ok": ok, "message": message}))


def poll_once():
    """Public entry. Serialized so the background loop and a manual Poll-now never
    overlap — guarantees a single token refresh and no double-posted callbacks."""
    with _poll_lock:
        _poll_once_locked()


def _poll_once_locked():
    token = _ensure_token()
    if not token:
        _record(False, "not logged in to BML — complete login on /faseyha")
        return

    status, body, _loc = bml.dashboard(token)
    if status == 200:
        _process_dashboard(token, body)
        return

    # Bad/expired token: bank redirects to its login page (or 401). Refresh once.
    if status in _REDIRECTS or status == 401:
        tok2 = _refresh_token(force=True)
        if tok2 and tok2 != token:
            status, body, _loc = bml.dashboard(tok2)
            if status == 200:
                _process_dashboard(tok2, body)
                return
        _record(False, f"BML session invalid ({status}) — re-login on /faseyha")
        return

    _record(False, f"dashboard upstream returned {status}")


def _process_dashboard(token, body):
    try:
        data = json.loads(body)
    except Exception:
        _record(False, "dashboard response was not JSON")
        return

    s = store.snapshot()
    settings = s.get("settings") or {}
    prefix = settings.get("account_prefix") or "77300"
    bfield = settings.get("balance_field") or "availableBalance"
    track_all = settings.get("track_all", True)
    selected = set(settings.get("tracked_accounts") or [])

    accounts = (data.get("payload") or {}).get("dashboard") or []
    # remember every account's number -> id (GUID) so force-update can resolve any account
    id_map = {str(a.get("account")): a.get("id")
              for a in accounts if a.get("account") and a.get("id")}
    candidates = [a for a in accounts
                  if str(a.get("account") or "").startswith(prefix)
                  and str(a.get("alias") or "").strip().upper() not in EXCLUDED_ALIASES]

    # --- dashboard change -> push the whole body verbatim ---
    dash_sig = hashlib.sha256(
        json.dumps(accounts, sort_keys=True).encode("utf-8")).hexdigest()
    if dash_sig != s.get("last_dashboard_hash"):
        if settings.get("dashboard_cb_enabled"):
            callbacks.post_dashboard(settings, body)
        store.mutate(lambda st: st.__setitem__("last_dashboard_hash", dash_sig))

    # --- per-account balance change -> push new history (tracked accounts only) ---
    prev = s.get("balances") or {}
    seen = s.get("seen_tx") or {}
    new_balances = dict(prev)
    n_tracked = 0

    for a in candidates:
        acc = str(a.get("account"))
        bal = a.get(bfield)
        is_tracked = track_all or acc in selected
        if is_tracked:
            n_tracked += 1
        old = prev.get(acc)
        if old is None:
            new_balances[acc] = bal             # seed every candidate; never replay old tx
            continue
        if bal != old and is_tracked:
            ok = _handle_change(token, a, acc, settings, seen)
            new_balances[acc] = bal if ok else old   # retry next tick on failure
        else:
            new_balances[acc] = bal             # untracked or unchanged: just cache it

    display = [{"account": str(a.get("account")), "alias": a.get("alias"),
                "currency": a.get("currency"), "balance": a.get(bfield),
                "id": a.get("id"),
                "tracked": bool(track_all or str(a.get("account")) in selected)}
               for a in candidates]

    def upd(st):
        st["balances"] = new_balances
        st["seen_tx"] = seen
        st["account_ids"] = id_map
        st["accounts"] = display
        st["last_poll"] = {"at": time.time(), "ok": True,
                           "message": f"{n_tracked} of {len(candidates)} account(s) tracked"}
    store.mutate(upd)


def _handle_change(token, account_obj, acc, settings, seen):
    """Fetch history for one account and post the new transactions. True on success."""
    acc_id = account_obj.get("id")
    status, body, _loc = bml.history(token, acc_id)
    if status != 200:
        log(f"poller: history for {acc} returned {status}")
        return False
    try:
        hist = (json.loads(body).get("payload") or {}).get("history") or []
    except Exception:
        log(f"poller: history for {acc} was not JSON")
        return False

    seen_ids = seen.get(acc) or []
    seen_set = set(seen_ids)
    new_tx = [t for t in hist if t.get("id") not in seen_set]
    if not new_tx:
        return True                              # balance moved but nothing new today

    # post to every enabled history callback; on any failure leave the balance
    # unchanged so the whole set is retried next tick (receivers dedupe by tx id).
    if not callbacks.post_history(settings, acc, new_tx):
        return False

    seen[acc] = ([t.get("id") for t in new_tx] + seen_ids)[:store._SEEN_CAP]
    log(f"poller: {acc} balance changed, {len(new_tx)} new tx handled")
    return True


# ------------------------------------------------------------------- force update
def _account_id(account_number, token):
    """Resolve an account number -> BML id, refreshing the saved map if unknown."""
    m = store.snapshot().get("account_ids") or {}
    if m.get(account_number):
        return m[account_number]
    status, body, _loc = bml.dashboard(token)
    if status != 200:
        return None
    try:
        accounts = (json.loads(body).get("payload") or {}).get("dashboard") or []
    except Exception:
        return None
    id_map = {str(a.get("account")): a.get("id")
              for a in accounts if a.get("account") and a.get("id")}
    if id_map:
        store.mutate(lambda s: s.__setitem__("account_ids", id_map))
    return id_map.get(account_number)


def force_update(account_number):
    """Fetch an account's history and POST it to the history callbacks NOW, even
    if no balance change was detected. Serialized with the poll loop. Returns a
    dict summary. Posted tx ids are marked seen so the next poll won't repeat them."""
    account_number = str(account_number)
    with _poll_lock:
        token = _ensure_token()
        if not token:
            return {"ok": False, "error": "not logged in to BML"}
        acc_id = _account_id(account_number, token)
        if not acc_id:
            return {"ok": False, "error": f"unknown account {account_number}"}
        status, body, _loc = bml.history(token, acc_id)
        if status != 200:
            return {"ok": False, "error": f"history returned {status}", "account": account_number}
        try:
            hist = (json.loads(body).get("payload") or {}).get("history") or []
        except Exception:
            return {"ok": False, "error": "history response was not JSON"}

        settings = store.get_settings()
        enabled = [c for c in (settings.get("history_callbacks") or [])
                   if c.get("enabled") and (c.get("url") or "").strip()]
        posted = callbacks.post_history(settings, account_number, hist)  # force: post ALL

        seen_ids = [t.get("id") for t in hist if t.get("id")]

        def upd(s):
            st = s.setdefault("seen_tx", {})
            st[account_number] = (seen_ids + (st.get(account_number) or []))[:store._SEEN_CAP]
        store.mutate(upd)

        log(f"force_update {account_number}: {len(hist)} tx, "
            f"callbacks_enabled={len(enabled)}, posted_ok={posted}")
        return {"ok": True, "account": account_number, "id": acc_id,
                "transactions": len(hist), "callbacks_enabled": len(enabled),
                "posted_ok": posted, "history": hist}


# ------------------------------------------------------------------ notifications
def poll_notifications_once():
    with _notif_lock:
        _poll_notifications_locked()


def _poll_notifications_locked():
    token = _ensure_token()
    if not token:
        return                                    # dashboard loop records the "not logged in" status
    settings = store.get_settings()
    query = settings.get("notifications_query") or "limit=20&group=ALERTS"
    status, body, _loc = bml.notifications(token, query)
    if status != 200:
        store.mutate(lambda s: s.__setitem__(
            "last_notif", {"at": time.time(), "ok": False, "message": f"upstream {status}"}))
        return
    try:
        payload = json.loads(body).get("payload") or []
    except Exception:
        store.mutate(lambda s: s.__setitem__(
            "last_notif", {"at": time.time(), "ok": False, "message": "response was not JSON"}))
        return

    # change = the SET of alert ids changed (ignores read/unread flips on the same alerts)
    ids = sorted(str(n.get("id")) for n in payload if n.get("id"))
    sig = hashlib.sha256(json.dumps(ids).encode("utf-8")).hexdigest()
    prev = store.snapshot().get("last_notifications_hash")
    changed = sig != prev
    seeding = not prev                            # first run: seed, don't replay old alerts

    # post only when the alert set actually changed (never on the first seeding poll)
    if changed and not seeding:
        callbacks.post_notifications(settings, body)

    def upd(s):
        s["last_notifications_hash"] = sig
        s["last_notif"] = {"at": time.time(), "ok": True,
                           "message": f"{len(ids)} alert(s)"
                                      + (" · seeded" if seeding else " · changed" if changed else "")}
    store.mutate(upd)
