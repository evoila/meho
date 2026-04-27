# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Auto-instrumentation for FastAPI, SQLAlchemy, HTTPX."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


def instrument_fastapi(app: FastAPI) -> None:
    """Instrument FastAPI with OpenTelemetry.

    Adds automatic tracing for all HTTP requests handled by the
    FastAPI application, excluding noisy polling endpoints.

    Args:
        app: The FastAPI application instance.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    # Exclude noisy endpoints from tracing
    excluded_urls = "/health,/api/knowledge/jobs/active,/api/chat/sessions"

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=excluded_urls,
    )


def instrument_sqlalchemy(engine: Any) -> None:
    """Instrument SQLAlchemy with OpenTelemetry.

    Adds automatic tracing for all database queries executed
    through the provided engine.

    Args:
        engine: The SQLAlchemy engine instance.
    """
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(
        engine=engine.sync_engine,
        enable_commenter=True,
    )


def instrument_httpx() -> None:
    """Instrument HTTPX for outbound HTTP calls.

    Adds automatic tracing for all outbound HTTP requests made
    through HTTPX clients (both sync and async).
    """
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()


def instrument_all(
    app: FastAPI | None = None,
    engine: Any | None = None,
) -> None:
    """Instrument all supported libraries.

    Convenience function to instrument HTTPX, and optionally
    FastAPI and SQLAlchemy.

    Args:
        app: Optional FastAPI application for HTTP instrumentation.
        engine: Optional SQLAlchemy engine for DB instrumentation.
    """
    instrument_httpx()

    if app:
        instrument_fastapi(app)

    if engine:
        instrument_sqlalchemy(engine)
