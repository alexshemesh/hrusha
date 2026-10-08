"""Structured JSON logging to stderr, optionally mirrored to a file.

Every log line is one JSON object with stable field names so lines can
be filtered by machine (`jq 'select(.sync_run_id == "...")'`). Extra
fields passed via ``logger.info(..., extra={...})`` are merged in.
Wallet addresses may appear in local logs; logs are never shipped
anywhere (see docs/DESIGN.md, Security & Privacy).

When HRUSHA_LOG_DIR is set (the Docker image points it at the mounted
/logs volume), the same JSON lines also go to a size-rotated
``hrusha.jsonl`` there — identical behavior bare-metal if you export
the variable yourself.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

LOG_DIR_ENV_VAR = "HRUSHA_LOG_DIR"
LOG_FILE_NAME = "hrusha.jsonl"
LOG_FILE_MAX_BYTES = 10_000_000
LOG_FILE_BACKUPS = 5

_RECORD_BUILTIN_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}

# Alchemy carries the API key in the URL PATH, so any library that logs a
# request URL leaks it. Quieting the loggers we know about is a blocklist the
# next dependency walks straight past (web3/urllib3 did exactly that), so the
# formatter redacts as well: nothing reaches a handler unmasked.
REDACTED = "***"
_ALCHEMY_PATHS = r"v2|data/v1|prices/v1"
_PATTERNS = (
    # full URL, as web3/httpx log it: https://base-mainnet.g.alchemy.com/v2/KEY
    re.compile(rf"(alchemy\.com/(?:{_ALCHEMY_PATHS})/)[^/\s\"']+"),
    # urllib3 logs host and path apart: ...alchemy.com:443 "POST /v2/KEY HTTP/1.1"
    # so the host is not in the same match — key on the path alone. The length
    # floor keeps real path words ("tokens", "historical") out of the net.
    re.compile(rf"(/(?:{_ALCHEMY_PATHS})/)[A-Za-z0-9_-]{{12,}}"),
)


def redact(text: str) -> str:
    """Mask secrets that appear inside otherwise-loggable strings."""
    for pattern in _PATTERNS:
        text = pattern.sub(rf"\1{REDACTED}", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for field, value in record.__dict__.items():
            if field not in _RECORD_BUILTIN_FIELDS:
                line[field] = redact(value) if isinstance(value, str) else value
        if record.exc_info and record.exc_info[0]:
            line["exception"] = record.exc_info[0].__name__
        return redact(json.dumps(line, default=str))


def setup_logging(level: int = logging.INFO) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_dir = os.environ.get(LOG_DIR_ENV_VAR)
    if log_dir:
        directory = Path(log_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                directory / LOG_FILE_NAME,
                maxBytes=LOG_FILE_MAX_BYTES,
                backupCount=LOG_FILE_BACKUPS,
            )
        )
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)
    # These log full request URLs at INFO/DEBUG — Alchemy URLs embed the API
    # key in the path, so they stay at WARNING unconditionally. httpx/httpcore
    # cover the providers; web3 talks through requests/urllib3, so `-v` used to
    # write the key to the log file hundreds of times per sync.
    for noisy in ("httpx", "httpcore", "web3", "urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
