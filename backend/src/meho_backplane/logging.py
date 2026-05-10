# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Structured logging configuration for the backplane.

structlog is configured to emit one JSON object per log record to
stdout, where the kubernetes log collector picks it up via the standard
container-stdout pathway. Every record carries an ISO 8601 UTC
timestamp, a level, and the event name; the request-context middleware
binds ``request_id`` into structlog's contextvars so handlers downstream
of the middleware automatically include it without threading the value
through every call site.

The configuration is idempotent — calling :func:`configure_logging`
twice has the same effect as calling it once. Tests that need a clean
slate call it again after rebinding stdout.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: int = logging.INFO) -> None:
    """Configure structlog for JSON output to stdout.

    Processor chain (order matters):

    1. ``merge_contextvars`` — surfaces values bound via
       :func:`structlog.contextvars.bind_contextvars` (the middleware
       binds ``request_id`` here).
    2. ``add_log_level`` — adds the ``level`` key.
    3. ``TimeStamper(fmt="iso", utc=True)`` — adds the ``timestamp``
       key in ISO 8601 UTC form.
    4. ``dict_tracebacks`` — when an event includes ``exc_info`` (set
       by :meth:`structlog.stdlib.BoundLogger.exception`), serialises
       the exception chain into a structured ``exception`` list. Must
       run before ``JSONRenderer``; otherwise the exception surfaces
       as the unhelpful ``"exc_info": true`` literal and the traceback
       is lost. Load-bearing for production triage of 5xx responses.
    5. ``JSONRenderer`` — final processor; serialises the event dict
       to a single JSON line.

    The logger factory writes to ``sys.stdout`` directly via
    :class:`structlog.PrintLoggerFactory`. Python's standard
    ``logging`` module is intentionally not bridged in v0.1 — the
    chassis has no third-party libraries that emit through stdlib
    logging at startup. G2.2 (Vault/Keycloak SDKs) will likely add a
    ``logging.basicConfig`` shim that routes stdlib logs through the
    same JSON renderer, but until then the simpler configuration
    keeps the surface honest.

    Args:
        level: Minimum log level emitted. Defaults to ``INFO``;
            tests pin this explicitly so capture is deterministic.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
