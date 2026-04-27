# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MEHO Observability - Backward Compatibility Layer.

DEPRECATED: Import from meho_app.core.otel directly for new code.

This module re-exports the OTEL interface so existing imports continue to work::

    from meho_app.core.observability import configure_observability, span

For new code, prefer::

    from meho_app.core.otel import get_logger, span
    logger = get_logger(__name__)

Configuration (environment variables):
    OTEL_SERVICE_NAME: Service name (default: meho)
    OTEL_EXPORTER_OTLP_ENDPOINT: OTLP endpoint (e.g., http://localhost:5341/ingest/otlp)
    OTEL_CONSOLE: Enable console output (default: true)
    MEHO_LOG_LEVEL: Log level (default: INFO)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.engine import Engine

from meho_app.core.otel import get_logger
from meho_app.core.otel import span as otel_span
from meho_app.core.otel.context import (  # noqa: F401 — re-exported via core/__init__.py
    clear_request_context,
    get_request_context,
    set_request_context,
)
from meho_app.core.otel.instrumentation import instrument_all

# Module-level state
_configured = False
_logger = get_logger("meho.observability")


# ---------------------------------------------------------------------------
# App-level configuration (called once at startup from main.py)
# ---------------------------------------------------------------------------


def configure_observability(
    *,
    app: FastAPI | None = None,
    engine: Engine | None = None,
    service_name: str | None = None,
    environment: str | None = None,
    log_level: str = "INFO",
) -> bool:
    """Configure observability via pure OpenTelemetry.

    Call once at app startup with FastAPI app and SQLAlchemy engine.
    Instruments FastAPI, SQLAlchemy, and HTTPX automatically.

    Note:
        ``service_name``, ``environment``, and ``log_level`` are accepted
        for diagnostic logging only.  Actual OTEL configuration is driven
        by environment variables (``OTEL_SERVICE_NAME``, ``ENVIRONMENT``,
        ``MEHO_LOG_LEVEL``).  See :func:`meho_app.core.otel.config.get_otel_config`.

    Args:
        app: FastAPI application for HTTP instrumentation.
        engine: SQLAlchemy engine for DB instrumentation.
        service_name: Service name logged for diagnostics (config via env vars).
        environment: Environment logged for diagnostics (config via env vars).
        log_level: Log level logged for diagnostics (config via env vars).

    Returns:
        True when instrumentation is configured.
    """
    global _configured

    if _configured:
        return True

    instrument_all(app=app, engine=engine)
    _configured = True

    _logger.info(
        "Observability configured via OTEL",
        service_name=service_name or "meho",
        environment=environment or "dev",
        log_level=log_level,
    )
    return True


def is_configured() -> bool:
    """Check if observability has been configured."""
    return _configured


# ---------------------------------------------------------------------------
# Convenience re-exports (backward compatibility)
# ---------------------------------------------------------------------------


def span(name: str, **attributes: Any) -> Any:
    """Create a traced span.

    Usage::

        with span("my_operation", key="value"):
            do_work()
    """
    return otel_span(name, **attributes)
