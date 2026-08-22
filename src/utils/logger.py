"""
Shared logger for AI Revenue Recovery Agent.

Usage (anywhere in the project):
    from src.utils.logger import get_logger
    logger = get_logger(__name__)
    logger.info("event_name", key="value", amount=999.0)

Features:
- Structured JSON logging via structlog (machine-readable in prod)
- Pretty coloured console output in dev (auto-detected via LOG_FORMAT env var)
- IST timestamps on every log line
- One-time setup — safe to call get_logger() from any module
"""

from __future__ import annotations

import logging
import logging.config
import os
import sys
from datetime import datetime, timedelta, timezone

import structlog

# ── IST timezone ──────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

# ── Config ────────────────────────────────────────────────────────────────────
# Set LOG_LEVEL=DEBUG in .env or environment to increase verbosity.
# Set LOG_FORMAT=json for structured JSON output (production / log aggregators).
_LOG_LEVEL  = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_FORMAT = os.getenv("LOG_FORMAT", "console")   # "console" | "json"

_CONFIGURED = False   # guard against double-setup


def _ist_timestamp(_: logging.Logger, __: str, event_dict: dict) -> dict:
    """Inject IST timestamp into every structlog event."""
    event_dict["timestamp"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    return event_dict


def _setup() -> None:
    """One-time structlog + stdlib logging configuration."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    # ── Shared processors ─────────────────────────────────────────────────────
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _ist_timestamp,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
    ]

    # ── Output renderer ───────────────────────────────────────────────────────
    if _LOG_FORMAT == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    # ── structlog configuration ───────────────────────────────────────────────
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ── stdlib logging configuration ─────────────────────────────────────────
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Return a configured structlog logger for the given module name.

    Args:
        name: Typically __name__ of the calling module.

    Returns:
        A structlog BoundLogger with IST timestamps and structured fields.

    Example:
        logger = get_logger(__name__)
        logger.info("upi_failure_detected", vpa="rahul@oksbi", code="U30", amount=999.0)
    """
    _setup()
    return structlog.get_logger(name)


# ── Convenience: bind request-scoped context fields ───────────────────────────

def bind_request_context(**kwargs) -> None:
    """
    Bind key-value pairs to the current async context (applies to all log
    calls in the same async task / request lifecycle).

    Example:
        bind_request_context(request_id="req-abc123", customer_id="CUST-A1")
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_request_context() -> None:
    """Clear all context-var bound fields (call at end of request handling)."""
    structlog.contextvars.clear_contextvars()
