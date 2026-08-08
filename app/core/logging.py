"""Structured JSON logging with a `run_id` that threads through API → agent node → Mesh call.

`configure_logging()` is called once from `app.main` at startup. Any code that wants a `run_id` to
show up in every log line for the duration of a request or agent run calls `set_run_id()` at the
start of that unit of work; `get_run_id()` reads it back from anywhere (it's a contextvar, so it is
async-task-safe and does not need to be threaded through every function signature).

No new dependency is introduced for this — a small custom `logging.Formatter` is enough and keeps
the dependency list honest per CLAUDE.md ("don't add dependencies without a one-sentence reason").
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

_run_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)

_RESERVED_RECORD_ATTRS = frozenset(
    logging.LogRecord(
        name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
    ).__dict__.keys()
) | {"message", "asctime", "extra_fields"}


def new_run_id() -> str:
    """Generate a fresh opaque run id (hex uuid4, no dashes — compact in logs and URLs)."""
    return uuid.uuid4().hex


def set_run_id(run_id: str | None = None) -> str:
    """Bind a run id to the current context (request or agent run). Returns the id in effect."""
    resolved = run_id or new_run_id()
    _run_id_ctx.set(resolved)
    return resolved


def get_run_id() -> str | None:
    """Read the run id bound to the current context, if any."""
    return _run_id_ctx.get()


class JSONFormatter(logging.Formatter):
    """Renders each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": get_run_id(),
        }

        # Anything passed via `logging.info(..., extra={...})` that isn't a stdlib LogRecord
        # attribute gets merged straight into the JSON payload.
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS:
                payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger. Safe to call more than once (e.g. reload)."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Avoid stacking duplicate handlers across `--reload` restarts / repeated calls in tests.
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
