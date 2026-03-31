# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tracing utilities wrapping OpenTelemetry."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from .config import get_otel_config
from .exporters import create_trace_exporters

_initialized = False


def _initialize_tracing() -> None:
    """Initialize OpenTelemetry tracing (called once)."""
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

    # Create tracer provider
    provider = TracerProvider(resource=resource)

    # Add exporters
    for processor in create_trace_exporters(config):
        provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)

    _initialized = True


def _reset_tracing() -> None:
    """Reset tracing state (for testing only)."""
    global _initialized
    _initialized = False


@lru_cache
def get_tracer(name: str) -> trace.Tracer:
    """Get a tracer for the given name.

    Tracers are cached so repeated calls with the same name return
    the same instance.

    Args:
        name: Tracer name, typically ``__name__``.

    Returns:
        An OpenTelemetry Tracer instance.
    """
    _initialize_tracing()
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes: Any) -> Generator[trace.Span, None, None]:
    """Context manager for creating spans.

    Usage::

        with span("my_operation", key="value") as s:
            do_work()

    Args:
        name: Name of the span.
        **attributes: Key-value attributes to attach to the span.

    Yields:
        The created span.
    """
    tracer = get_tracer("meho")
    with tracer.start_as_current_span(name, attributes=attributes) as s:
        yield s
