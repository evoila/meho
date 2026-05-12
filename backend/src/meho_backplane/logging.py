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

    The logger factory writes to ``sys.stdout`` **lazily** via
    :class:`structlog.PrintLoggerFactory` constructed with no ``file``
    argument. The factory then resolves stdout per-write inside
    :meth:`structlog.PrintLogger.msg`, which calls :func:`print` with
    no ``file=`` keyword whenever ``self._file is sys.stdout`` (the
    structlog-module-level alias captured at structlog import time, i.e.
    the *original* process stdout). With the no-arg factory shape, every
    constructed ``PrintLogger`` defaults its ``_file`` attribute to that
    same alias, so :func:`print` in ``msg`` runs without a pinned
    ``file`` and Python writes to whatever ``sys.stdout`` currently
    points at when the log call fires.

    This matters specifically for pytest: pytest's ``capfd`` /
    ``capsys`` machinery swaps ``sys.stdout`` for a file-descriptor
    wrapper for the duration of a test, then closes that wrapper at
    teardown. The previously-eager ``PrintLoggerFactory(file=sys.stdout)``
    shape captured the wrapper at ``configure_logging()`` time (called
    from FastAPI lifespan startup), and ``cache_logger_on_first_use=True``
    kept the resulting factory + cached PrintLogger alive into later
    tests where the wrapped fd was already closed — yielding
    ``ValueError: I/O operation on closed file.`` from the next
    middleware-emitted log line. The lazy shape avoids the capture
    entirely; production behaviour is unchanged because the real
    process ``sys.stdout`` does not get rebound at runtime.

    Python's standard ``logging`` module is intentionally not bridged
    in v0.1 — the chassis has no third-party libraries that emit
    through stdlib logging at startup. G2.2 (Vault/Keycloak SDKs) will
    likely add a ``logging.basicConfig`` shim that routes stdlib logs
    through the same JSON renderer, but until then the simpler
    configuration keeps the surface honest.

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
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
