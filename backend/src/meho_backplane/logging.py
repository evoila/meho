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
from structlog.tracebacks import ExceptionDictTransformer


def configure_logging(level: int = logging.INFO) -> None:
    """Configure structlog for JSON output to stdout.

    Processor chain (order matters):

    1. ``merge_contextvars`` — surfaces values bound via
       :func:`structlog.contextvars.bind_contextvars` (the middleware
       binds ``request_id`` here).
    2. ``add_log_level`` — adds the ``level`` key.
    3. ``TimeStamper(fmt="iso", utc=True)`` — adds the ``timestamp``
       key in ISO 8601 UTC form.
    4. ``ExceptionRenderer(ExceptionDictTransformer(show_locals=False))``
       — the ``dict_tracebacks`` processor with frame-local rendering
       **disabled**. When an event includes ``exc_info`` (set by
       :meth:`structlog.stdlib.BoundLogger.exception`), it serialises the
       exception chain into a structured ``exception`` list. Must run
       before ``JSONRenderer``; otherwise the exception surfaces as the
       unhelpful ``"exc_info": true`` literal and the traceback is lost.
       The structured frames (file / line / function / exception type +
       message) stay — only the per-frame *locals* dict is dropped.

       ``show_locals`` defaults to ``True`` in structlog, which renders
       every frame's local variables into the log line. That is a
       credential-disclosure vector (CWE-532): any secret held as a frame
       local on a traceback is written to stdout verbatim. The
       motivating incident was a failed scheduled agent run logging the
       agent's ``client_credentials`` secret (held as
       ``agent_client_secret`` on the scheduler fire path). The secret is
       now additionally wrapped in :class:`~pydantic.SecretStr` at the
       source (defense in depth), but disabling ``show_locals`` closes
       the vector for *every* frame across the backplane — including
       frames that must hold a secret as a plain ``str`` (e.g. the httpx
       form-post in :mod:`meho_backplane.auth.agent_token`, where the raw
       value is unavoidable). Frame locals are convenient for triage but
       not worth a standing credential-leak surface; the structured
       traceback without locals remains sufficient for 5xx triage.
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
            # ``dict_tracebacks`` with frame-local rendering disabled.
            # ``show_locals=True`` (the structlog default) writes every
            # frame's locals into the log line -- a credential-disclosure
            # vector (CWE-532) for any secret held as a frame local on a
            # traceback. See :func:`configure_logging`'s docstring.
            structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False)),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
