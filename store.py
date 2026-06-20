"""Persistent state for the /faseyha worker layer (Redis-backed, file fallback).

The original proxy endpoints (/api/*) are stateless and never touch this module.
Only the new /faseyha admin layer and the background poller use it.

Storage: the whole state is one JSON blob.
  * Primary backend  : Redis (key "<prefix>:state"), auto-started Windows service.
  * Fallback backend : a local JSON file (state.json), used if Redis can't be
                       reached at startup OR if a Redis write fails at runtime.
On first start with Redis empty, an existing state.json is migrated in.

For easy expiry tracking, whenever a BML token is saved we also set a throwaway
key "<prefix>:bml:expiry" with a Redis TTL == seconds-to-expiry, so you can run
`redis-cli TTL faseyha:bml:expiry` to see how long the access token has left.

State shape:
  auth      saved BML session -> {access_token, refresh_token, expires_at,
                                   last_refresh_at, updated_at}
  app       app login         -> {password:{salt,hash}, sessions:{sid:expiry}}
  settings  poller config      -> intervals, prefix, callback urls/formats, renewal
  balances  cache per account  -> {account_number: balance}
  seen_tx   posted tx ids       -> {account_number: [tx_id, ...]} (capped)
  accounts  last-poll display  -> [{account, alias, currency, balance, id}]
  last_poll status             -> {at, ok, message}

Env overrides: FASEYHA_REDIS_URL, FASEYHA_REDIS_PREFIX, FASEYHA_NO_REDIS=1,
FASEYHA_STATE (file path).
"""

import os
import copy
import json
import time
import hashlib
import secrets
import threading

from logutil import log

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("FASEYHA_STATE", os.path.join(HERE, "state.json"))
REDIS_URL = os.environ.get("FASEYHA_REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_PREFIX = os.environ.get("FASEYHA_REDIS_PREFIX", "faseyha")

_PBKDF2_ROUNDS = 200_000
_SESSION_TTL = 30 * 86400          # app login cookie lifetime: 30 days
_SEEN_CAP = 300                    # remember at most this many posted tx ids/account

DEFAULTS = {
    "auth": {},
    "app": {},
    "settings": {
        "poll_interval": 10,                 # seconds between dashboard fetches
        "notifications_interval": 10,        # seconds between notifications (ALERTS) fetches
        "account_prefix": "7",               # candidate set: accounts whose number starts with this
                                             # ("7" = all BML bank accounts; cards/merchant start 3/4/5/8)
        "track_all": True,                   # True = watch every candidate (incl. future ones)
        "tracked_accounts": [],              # when track_all is False, watch only these numbers
        "balance_field": "availableBalance", # which dashboard balance field to watch
        "renew_before_sec": 5 * 86400,       # auto-renew the token when this little time is left
        "renew_min_interval_sec": 6 * 3600,  # but don't refresh more often than this
        # up to N history callbacks; each fired (independently) on a balance change.
        # format "json" = A {"history":[…]} to a {account}-templated url;
        # format "form" = B account_code + account_data.
        "history_callbacks": [
            {"enabled": False, "format": "json",
             "url": "https://admin.faseyha.app/api/payment_callback/bank/{account}"},
            {"enabled": False, "format": "form", "url": ""},
        ],
        "dashboard_cb_enabled": False,
        "dashboard_cb_url": "https://stage.faseyha.app/papi/bank_callback_dashboard/",
        # up to 2 notification callbacks; each POSTed the ALERTS body on change.
        "notification_callbacks": [
            {"enabled": False, "url": ""},
            {"enabled": False, "url": ""},
        ],
        "notifications_query": "limit=20&group=ALERTS",
        "callback_auth": "",                 # optional Authorization header value for callbacks
    },
    "balances": {},
    "seen_tx": {},
    "account_ids": {},                       # {account_number: BML account id (GUID)} for force-update
    "accounts": [],
    "last_dashboard_hash": "",
    "last_notifications_hash": "",
    "last_poll": {},
    "last_notif": {},
}

_lock = threading.RLock()
_state = None
_backend = None                    # "redis" or "file"
_redis = None
_STATE_KEY = f"{REDIS_PREFIX}:state"
_EXPIRY_KEY = f"{REDIS_PREFIX}:bml:expiry"


# --------------------------------------------------------------------- backend
def _connect_redis():
    """Return a live redis client, or None (forcing the file fallback)."""
    if os.environ.get("FASEYHA_NO_REDIS"):
        return None
    try:
        import redis
        client = redis.Redis.from_url(REDIS_URL, decode_responses=True,
                                      socket_connect_timeout=2, socket_timeout=2)
        client.ping()
        return client
    except Exception as e:
        log(f"store: Redis unavailable ({type(e).__name__}: {e}) — using file fallback")
        return None


def _read_blob():
    """Load the raw state dict from the active backend (empty dict if none)."""
    if _backend == "redis":
        try:
            raw = _redis.get(_STATE_KEY)
            return json.loads(raw) if raw else {}
        except Exception as e:
            log(f"store: Redis read failed ({e}) — falling back to file")
            _use_file_backend()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _write_blob(state):
    """Persist the raw state dict; maintain the token-expiry TTL key on Redis."""
    if _backend == "redis":
        try:
            _redis.set(_STATE_KEY, json.dumps(state))
            _touch_expiry_key(state)
            return
        except Exception as e:
            log(f"store: Redis write failed ({e}) — falling back to file")
            _use_file_backend()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def _touch_expiry_key(state):
    exp = (state.get("auth") or {}).get("expires_at")
    try:
        if exp:
            ttl = int(exp - time.time())
            if ttl > 0:
                _redis.set(_EXPIRY_KEY, str(int(exp)), ex=ttl)
            else:
                _redis.delete(_EXPIRY_KEY)
        else:
            _redis.delete(_EXPIRY_KEY)
    except Exception:
        pass


def _use_file_backend():
    global _backend
    _backend = "file"


def _init_backend():
    """Choose Redis or file, migrating an existing state.json into empty Redis."""
    global _backend, _redis
    if _backend is not None:
        return
    _redis = _connect_redis()
    if _redis is not None:
        _backend = "redis"
        try:
            if not _redis.exists(_STATE_KEY) and os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    _redis.set(_STATE_KEY, f.read())
                log("store: migrated existing state.json into Redis")
        except Exception as e:
            log(f"store: migration check failed ({e})")
        log(f"store: using Redis backend ({REDIS_URL}, key {_STATE_KEY})")
    else:
        _backend = "file"
        log(f"store: using file backend ({STATE_FILE})")


# --------------------------------------------------------------------- internals
def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _ensure_loaded():
    global _state
    if _state is not None:
        return
    _init_backend()
    _state = _deep_merge(DEFAULTS, _read_blob())


def _persist():
    _write_blob(_state)


def backend():
    with _lock:
        _ensure_loaded()
        return _backend


def get_redis():
    """The live Redis client when Redis is the active backend, else None."""
    with _lock:
        _ensure_loaded()
        return _redis if _backend == "redis" else None


# ----------------------------------------------------------------------- generic
def snapshot():
    with _lock:
        _ensure_loaded()
        return copy.deepcopy(_state)


def mutate(fn):
    with _lock:
        _ensure_loaded()
        fn(_state)
        _persist()


def get_settings():
    return snapshot().get("settings") or {}


def update_settings(patch):
    allowed = set(DEFAULTS["settings"].keys())
    clean = {k: v for k, v in (patch or {}).items() if k in allowed}
    if "tracked_accounts" in clean:
        v = clean["tracked_accounts"]
        clean["tracked_accounts"] = [str(x) for x in v] if isinstance(v, list) else []
    if "track_all" in clean:
        clean["track_all"] = bool(clean["track_all"])
    if "history_callbacks" in clean:
        v = clean["history_callbacks"]
        cbs = []
        for c in (v if isinstance(v, list) else []):
            if not isinstance(c, dict):
                continue
            fmt = "form" if c.get("format") == "form" else "json"
            cbs.append({"enabled": bool(c.get("enabled")), "format": fmt,
                        "url": str(c.get("url") or "").strip()})
        clean["history_callbacks"] = cbs
    if "notification_callbacks" in clean:
        v = clean["notification_callbacks"]
        cbs = []
        for c in (v if isinstance(v, list) else []):
            if isinstance(c, dict):
                cbs.append({"enabled": bool(c.get("enabled")),
                            "url": str(c.get("url") or "").strip()})
        clean["notification_callbacks"] = cbs
    mutate(lambda s: s.setdefault("settings", {}).update(clean))
    return get_settings()


# --------------------------------------------------------------------- BML tokens
def save_auth(token_json):
    """Persist a BML /oauth token response (exchange or refresh)."""
    at = token_json.get("access_token")
    if not at:
        return False
    rt = token_json.get("refresh_token")
    expires_in = token_json.get("expires_in")
    now = time.time()

    def upd(s):
        a = s.setdefault("auth", {})
        a["access_token"] = at
        if rt:
            a["refresh_token"] = rt
        if expires_in:
            a["expires_at"] = now + float(expires_in)
        a["last_refresh_at"] = now
        a["updated_at"] = now
    mutate(upd)
    return True


def clear_auth():
    def upd(s):
        s["auth"] = {}
    mutate(upd)
    if _backend == "redis":
        try:
            _redis.delete(_EXPIRY_KEY)
        except Exception:
            pass


# ---------------------------------------------------------------- app password/login
def has_password():
    return bool((snapshot().get("app") or {}).get("password"))


def set_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS).hex()
    mutate(lambda s: s.setdefault("app", {}).__setitem__("password", {"salt": salt, "hash": h}))


def check_password(pw):
    p = (snapshot().get("app") or {}).get("password")
    if not p:
        return False
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(p["salt"]), _PBKDF2_ROUNDS).hex()
    return secrets.compare_digest(h, p["hash"])


def create_session():
    sid = secrets.token_urlsafe(32)
    now = time.time()

    def upd(s):
        sessions = s.setdefault("app", {}).setdefault("sessions", {})
        for k in [k for k, exp in sessions.items() if exp <= now]:
            sessions.pop(k, None)
        sessions[sid] = now + _SESSION_TTL
    mutate(upd)
    return sid


def valid_session(sid):
    if not sid:
        return False
    sessions = (snapshot().get("app") or {}).get("sessions") or {}
    exp = sessions.get(sid)
    return bool(exp and exp > time.time())


def destroy_session(sid):
    if not sid:
        return
    mutate(lambda s: (s.get("app") or {}).get("sessions", {}).pop(sid, None))


# ------------------------------------------------------------------- public view
def public_state():
    s = snapshot()
    auth = s.get("auth") or {}
    exp = auth.get("expires_at")
    return {
        "backend": _backend,
        "app": {"has_password": bool((s.get("app") or {}).get("password"))},
        "bml": {
            "logged_in": bool(auth.get("access_token")),
            "has_refresh": bool(auth.get("refresh_token")),
            "expires_at": exp,
            "expires_in_sec": int(exp - time.time()) if exp else None,
            "last_refresh_at": auth.get("last_refresh_at"),
        },
        "settings": s.get("settings") or {},
        "accounts": s.get("accounts") or [],
        "last_poll": s.get("last_poll") or {},
    }
