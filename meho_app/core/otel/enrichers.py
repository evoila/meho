# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Context enrichment for OpenTelemetry logs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.sdk._logs import LogRecordProcessor

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import ReadWriteLogRecord


class ContextEnricherProcessor(LogRecordProcessor):
    """Enriches logs with request context from ContextVars.

    Automatically injects request_id, user_id, tenant_id from the
    current request context, and trace_id/span_id from the active
    OpenTelemetry span for trace correlation.
    """

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        """Add context attributes to log record."""
        # Import context vars from otel.context
        from meho_app.core.otel.context import (
            request_id_ctx,
            tenant_id_ctx,
            user_id_ctx,
        )

        # Access the inner LogRecord's attributes (runtime is a dict, Mapping in stubs)
        raw_attrs = log_record.log_record.attributes
        if raw_attrs is None:
            log_record.log_record.attributes = {}
            raw_attrs = log_record.log_record.attributes
        attrs: dict[str, Any] = raw_attrs  # type: ignore[assignment]  # OTel stubs say Mapping but runtime is dict

        # Request context
        if request_id := request_id_ctx.get():
            attrs["request_id"] = request_id
        if user_id := user_id_ctx.get():
            attrs["user_id"] = user_id
        if tenant_id := tenant_id_ctx.get():
            attrs["tenant_id"] = tenant_id

        # Trace context (automatic correlation)
        current_span = trace.get_current_span()
        if current_span and current_span.is_recording():
            ctx = current_span.get_span_context()
            attrs["trace_id"] = format(ctx.trace_id, "032x")
            attrs["span_id"] = format(ctx.span_id, "016x")

    def shutdown(self) -> None:
        """Shutdown the processor."""

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Force flush pending records."""
        return True
