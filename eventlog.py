"""Realtime event log for /faseyha/logs.

Records one entry per interesting response — served proxy requests, BML calls, and
outbound callbacks — each with timestamp, kind, label, HTTP status, duration (ms)
and a (redacted, truncated) response body so it's viewable in the UI.

Storage: a Redis list "<prefix>:logs" (newest first), capped to CAP entries; reads
also drop anything older than MAX_AGE (~1 day). If Redis isn't the active backend,
entries append to events.log (JSON lines) instead. record() never raises, so the
stateless proxy is never affected by logging.

Bodies are redacted: access_token / refresh_token / token / rtoken values are masked
before storage so secrets are never persisted to the log.
"""

import os
import re
import json
import time
import threading

import store
from logutil import log

HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(HERE, "events.log")

CAP = 1500             # max entries kept in Redis
MAX_AGE = 86400        # ~1 day retention on read
BODY_CAP = 60_000      # truncate stored bodies to this many chars
_FILE_CAP = 4000       # max lines kept in the file fallback

_KEY = f"{store.REDIS_PREFIX}:logs"
_lock = threading.Lock()

# mask the *value* of any of these JSON keys
_SECRET_RE = re.compile(
    r'("(?:access_token|refresh_token|token|rtoken)"\s*:\s*")([^"]*)(")',
    re.IGNORECASE)


def _redact(body):
    if not body:
        return body
    s = body if isinstance(body, str) else str(body)
    s = _SECRET_RE.sub(lambda m: m.group(1) + "***redacted***" + m.group(3), s)
    if len(s) > BODY_CAP:
        s = s[:BODY_CAP] + f"\n…(+{len(s) - BODY_CAP} more chars truncated)"
    return s


def record(kind, label, status=None, ms=None, body=None, ok=None):
    """Append one event. kind: 'http' | 'bml' | 'callback'. Never raises."""
    try:
        if ok is None and status is not None:
            try:
                ok = 200 <= int(status) < 300
            except (TypeError, ValueError):
                ok = None
        entry = {
            "at": time.time(),
            "kind": kind,
            "label": label,
            "status": status,
            "ms": round(ms) if ms is not None else None,
            "ok": ok,
            "body": _redact(body),
        }
        line = json.dumps(entry)
        r = store.get_redis()
        if r is not None:
            try:
                r.lpush(_KEY, line)
                r.ltrim(_KEY, 0, CAP - 1)
                return
            except Exception:
                pass
        _file_append(line)
    except Exception:
        pass


def recent(limit=200, max_age=MAX_AGE):
    """Newest-first list of entries, capped to `limit` and younger than max_age."""
    cutoff = time.time() - max_age
    items = []
    r = store.get_redis()
    if r is not None:
        try:
            for raw in r.lrange(_KEY, 0, max(limit * 2, 400)):
                try:
                    items.append(json.loads(raw))
                except Exception:
                    pass
        except Exception:
            items = []
    if not items:
        items = _file_read(max(limit * 2, 400))
    items = [e for e in items if (e.get("at") or 0) >= cutoff]
    return items[:limit]


# ---------------------------------------------------------------- file fallback
def _file_append(line):
    with _lock:
        try:
            with open(EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            # opportunistic trim so the fallback file can't grow unbounded
            if os.path.getsize(EVENTS_FILE) > 8 * 1024 * 1024:
                with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()[-_FILE_CAP:]
                with open(EVENTS_FILE, "w", encoding="utf-8") as f:
                    f.writelines(lines)
        except Exception:
            pass


def _file_read(n):
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        out = []
        for ln in reversed(lines):           # newest first
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return out
    except FileNotFoundError:
        return []
    except Exception:
        return []
