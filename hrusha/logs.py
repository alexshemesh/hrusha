"""Structured JSON logging to stderr.

Every log line is one JSON object with stable field names so lines can
be filtered by machine (`jq 'select(.sync_run_id == "...")'`). Extra
fields passed via ``logger.info(..., extra={...})`` are merged in.
Wallet addresses may appear in local logs; logs are never shipped
anywhere (see docs/DESIGN.md, Security & Privacy).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_RECORD_BUILTIN_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field, value in record.__dict__.items():
            if field not in _RECORD_BUILTIN_FIELDS:
                line[field] = value
        if record.exc_info and record.exc_info[0]:
            line["exception"] = record.exc_info[0].__name__
        return json.dumps(line, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx/httpcore log full request URLs at INFO/DEBUG — Alchemy URLs
    # embed the API key, so those loggers stay at WARNING unconditionally
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
