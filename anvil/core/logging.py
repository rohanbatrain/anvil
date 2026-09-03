"""Structured logging with PII redaction at the boundary.

Log records pass through a redaction processor before rendering, so a stray
``log.info("charging", vpa=customer.vpa)`` cannot leak a payment identifier into
a log aggregator. Redaction happens on write, not on read.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from anvil.core.config import get_settings

#: Keys whose values are always masked, wherever they appear in a log event.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "vpa",
        "upi_id",
        "card_number",
        "pan",
        "account_number",
        "ifsc",
        "phone",
        "mobile",
        "email",
        "customer_email",
        "customer_phone",
        "api_key",
        "key_secret",
        "webhook_secret",
        "authorization",
        "anthropic_api_key",
        "razorpay_key_secret",
        "token",
        "otp",
        "mpin",
        "password",
        "secret",
        "signature",
    }
)


def _mask(value: Any) -> str:
    text = str(value)
    if len(text) <= 4:
        return "***"
    return f"{text[:2]}***{text[-2:]}"


def redact_processor(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    for key in list(event_dict):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = _mask(event_dict[key])
    return event_dict


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level)

    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        # Deliberately not structlog.stdlib.add_logger_name: that processor
        # reads ``logger.name``, which only exists on a stdlib logger, and this
        # configuration uses a PrintLogger. The module name is bound in
        # get_logger instead, which works with any factory.
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_processor,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    for noisy in ("httpx", "httpcore", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A logger that carries its module name as a bound field."""
    return structlog.get_logger().bind(logger=name)  # type: ignore[no-any-return]
