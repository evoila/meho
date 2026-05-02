# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Exporter factory for OpenTelemetry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import OTelConfig

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LogRecordProcessor
    from opentelemetry.sdk.trace import SpanProcessor


def create_log_exporters(config: OTelConfig) -> list[LogRecordProcessor]:
    """Create log exporters based on configuration.

    Returns a list of LogRecordProcessor instances wrapping the
    configured OTLP exporters.  Console output is handled separately
    via a stdlib ``logging.StreamHandler`` (see ``logging.py``).
    """
    from opentelemetry.sdk._logs.export import (
        BatchLogRecordProcessor,
    )

    processors: list[LogRecordProcessor] = []

    # OTLP exporter (Seq, Loki, etc.)
    if config.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )

        exporter = OTLPLogExporter(
            endpoint=f"{config.otlp_endpoint}/v1/logs",
            headers=config.otlp_headers or None,
        )
        processors.append(
            BatchLogRecordProcessor(
                exporter,
                max_export_batch_size=config.max_batch_size,
                schedule_delay_millis=config.batch_delay_ms,
            )
        )

    return processors


def create_trace_exporters(config: OTelConfig) -> list[SpanProcessor]:
    """Create trace exporters based on configuration.

    Returns a list of SpanProcessor instances wrapping the
    configured OTLP exporters.  Console output is handled separately
    via a stdlib ``logging.StreamHandler`` (see ``logging.py``).
    """
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
    )

    processors: list[SpanProcessor] = []

    # OTLP exporter
    if config.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(
            endpoint=f"{config.otlp_endpoint}/v1/traces",
            headers=config.otlp_headers or None,
        )
        processors.append(BatchSpanProcessor(exporter))

    return processors
