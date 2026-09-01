"""Structured logging with secret redaction.

Every log line carries ``run_id`` so a run can be reconstructed after the fact,
and no secret value can appear in output even if something logs a whole request
or a provider echoes a URL containing a token.

    from finder.logging import configure_logging, get_logger
    configure_logging(secrets=load_secrets())
    log = get_logger(__name__).bind(run_id=ctx.run_id)
    log.info("fetched", url=url, status=200)
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from finder.secrets import Secrets

REDACTED = "***REDACTED***"

# Populated by configure_logging(). Module-level because the processor runs on
# every event and must not re-read the environment.
_SECRET_VALUES: list[str] = []


def register_secrets(secrets: Secrets) -> None:
    """Register secret values for redaction. Longest first, so a token that
    contains another shorter value is masked whole rather than in pieces."""
    global _SECRET_VALUES
    _SECRET_VALUES = sorted(secrets.redactable_values(), key=len, reverse=True)


def _redact_text(text: str) -> str:
    for value in _SECRET_VALUES:
        if value in text:
            text = text.replace(value, REDACTED)
    return text


def _redact(value: Any) -> Any:
    """Recursively scrub secret values out of anything headed for a log line."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        rebuilt = [_redact(v) for v in value]
        return type(value)(rebuilt) if isinstance(value, tuple) else rebuilt
    return value


def redaction_processor(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """structlog processor: scrub every value in the event."""
    if not _SECRET_VALUES:
        return event_dict
    return {k: _redact(v) for k, v in event_dict.items()}


def configure_logging(
    *,
    secrets: Secrets | None = None,
    level: str = "INFO",
    json_output: bool = True,
) -> None:
    """Configure structlog once, at process start.

    ``json_output=False`` gives a human-readable console renderer for local work;
    scheduled runs should keep JSON so lines stay machine-queryable by run_id.
    """
    if secrets is not None:
        register_secrets(secrets)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redaction_processor,  # last before rendering: nothing escapes it
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
