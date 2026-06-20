"""Shared rotating logger for the BML API worker.

Both server.py and bml_client.py call `log()` so incoming-request lines and
bank-call timing lines interleave in a single file: requests.log
"""

import os
import logging
from logging.handlers import RotatingFileHandler

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requests.log")

_logger = logging.getLogger("bmlapi")
_logger.setLevel(logging.INFO)
_logger.propagate = False

if not _logger.handlers:
    # 5 MB per file, keep 3 old copies, so the log can never grow unbounded.
    _handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    # thread name lets you tell concurrent requests apart
    _handler.setFormatter(logging.Formatter(
        "[%(asctime)s.%(msecs)03d] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_handler)


def log(msg):
    """Append a timestamped, thread-tagged line to requests.log (best effort)."""
    try:
        _logger.info(msg)
    except Exception:
        pass
