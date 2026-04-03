# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MEHO OpenTelemetry Integration.

Unified logging and tracing interface with automatic context enrichment.

Usage::

    from meho_app.core.otel import get_logger, get_tracer, span

    logger = get_logger(__name__)
    logger.info("Something happened", user_id="123", connector_id="abc")

    with span("my_operation", key="value"):
        do_work()

Configuration (environment variables):
    OTEL_SERVICE_NAME: Service name (default: meho)
    OTEL_EXPORTER_OTLP_ENDPOINT: OTLP endpoint (e.g., http://localhost:5341/ingest/otlp)
    OTEL_EXPORTER_OTLP_HEADERS: Auth headers (e.g., Authorization=Bearer xxx)
    OTEL_CONSOLE: Enable console output (default: true)
    MEHO_LOG_LEVEL: Log level (default: INFO)
"""

from .config import OTelConfig, get_otel_config
from .logging import StructuredLogger, get_logger
from .tracing import get_tracer, span

__all__ = [
    "OTelConfig",
    "StructuredLogger",
    "get_logger",
    "get_otel_config",
    "get_tracer",
    "span",
]
