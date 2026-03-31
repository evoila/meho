# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unified logging interface wrapping OpenTelemetry."""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from typing import Any

from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk.resources import Resource

from .config import get_otel_config
from .enrichers import ContextEnricherProcessor
from .exporters import create_log_exporters

_initialized = False

# Noisy third-party loggers that should be suppressed to WARNING.
# These flood the console with routine INFO messages (SQL statements,
# HTTP connection details, async internals, OTEL export retries).
_NOISY_LOGGERS = (
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "httpx",
    "httpcore",
    "asyncio",
    "uvicorn.access",
    "opentelemetry.sdk",
    "opentelemetry.exporter",
)


def _initialize_logging() -> None:
    """Initialize OpenTelemetry logging (called once)."""
    global _initialized
    if _initialized:
        return

    config = get_otel_config()

    # Create resource
    resource = Resource.create(
        {
            "service.name": config.service_name,
            "service.version": config.service_version,
            "deployment.environment": config.environment,
        }
    )

    # Create logger provider
    provider = LoggerProvider(resource=resource)

    # Add enricher processor first
    provider.add_log_record_processor(ContextEnricherProcessor())

    # Add exporters (OTLP only -- console is handled by stdlib below)
    for processor in create_log_exporters(config):
        provider.add_log_record_processor(processor)

    set_logger_provider(provider)

    # Integrate with Python's logging module
    log_level = getattr(logging, config.log_level, logging.INFO)
    handler = LoggingHandler(level=log_level, logger_provider=provider)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(log_level)

    # Human-readable console output (replaces raw JSON ConsoleLogRecordExporter)
    if config.console_enabled:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-5s %(name)s : %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logging.getLogger().addHandler(console_handler)

    # Suppress noisy third-party loggers
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _initialized = True


def _reset_logging() -> None:
    """Reset logging state (for testing only)."""
    global _initialized
    _initialized = False


class StructuredLogger:
    """Structured logger with automatic context enrichment.

    Wraps Python's standard logging.Logger but accepts structured
    keyword attributes instead of string formatting. The attributes
    are passed through to OpenTelemetry as log record attributes.

    Usage::

        logger = get_logger(__name__)
        logger.info("User logged in", user_id="123", method="oauth")
        logger.error("Request failed", status_code=500, path="/api/data")
    """

    def __init__(self, name: str) -> None:
        _initialize_logging()
        self._logger = logging.getLogger(name)
        self._name = name

    def _log(self, level: int, message: str, **attributes: Any) -> None:
        """Log with structured attributes."""
        # Use extra dict for structured attributes
        self._logger.log(level, message, extra={"attributes": attributes})

    def log(self, level: int, message: str, **attributes: Any) -> None:
        """Log at the given level with optional structured attributes.

        Mirrors the stdlib ``logging.Logger.log()`` signature so callers
        that pass a dynamic log level continue to work.
        """
        self._log(level, message, **attributes)

    def debug(self, message: str, **attributes: Any) -> None:
        """Log a debug message with optional structured attributes."""
        self._log(logging.DEBUG, message, **attributes)

    def info(self, message: str, **attributes: Any) -> None:
        """Log an info message with optional structured attributes."""
        self._log(logging.INFO, message, **attributes)

    def warning(self, message: str, **attributes: Any) -> None:
        """Log a warning message with optional structured attributes."""
        self._log(logging.WARNING, message, **attributes)

    def warn(self, message: str, **attributes: Any) -> None:
        """Alias for warning."""
        self.warning(message, **attributes)

    def error(self, message: str, **attributes: Any) -> None:
        """Log an error message with optional structured attributes."""
        self._log(logging.ERROR, message, **attributes)

    def exception(self, message: str, **attributes: Any) -> None:
        """Log error with exception info (must be called from except block)."""
        self._logger.exception(message, extra={"attributes": attributes})

    def critical(self, message: str, **attributes: Any) -> None:
        """Log a critical message with optional structured attributes."""
        self._log(logging.CRITICAL, message, **attributes)


@lru_cache(maxsize=256)
def get_logger(name: str) -> StructuredLogger:
    """Get a structured logger for the given name.

    Loggers are cached so repeated calls with the same name return
    the same instance.

    Args:
        name: Logger name, typically ``__name__``.

    Returns:
        A StructuredLogger instance.
    """
    return StructuredLogger(name)
