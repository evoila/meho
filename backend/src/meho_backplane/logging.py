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

Python's standard :mod:`logging` module is **bridged** into the same
JSON pipeline: records emitted by third-party libraries (uvicorn,
httpx, SQLAlchemy, hvac, kubernetes_asyncio, ...) are routed through a
:class:`structlog.stdlib.ProcessorFormatter` on the root handler, so
they land as the same one-JSON-line-per-record shape — level,
timestamp, contextvar-merged ``request_id`` (when in a request
context), and locals-stripped structured tracebacks. Nothing on the
backplane emits plain-text log lines any more.

The configuration is idempotent — calling :func:`configure_logging`
twice has the same effect as calling it once (the root handler is
replaced, not stacked). Tests that need a clean slate call it again
after rebinding stdout.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

import structlog
from structlog.tracebacks import ExceptionDictTransformer

#: Name pinned on the root-logger handler this module installs so that a
#: repeated :func:`configure_logging` call replaces it in place instead
#: of stacking a second handler (which would double every stdlib log
#: line). Named rather than clearing all root handlers, so pytest's own
#: ``caplog`` handler survives a mid-test :func:`configure_logging` call.
_STDLIB_BRIDGE_HANDLER_NAME = "meho-stdlib-json-bridge"


class _LazyStdoutHandler(logging.StreamHandler[TextIO]):
    """A ``StreamHandler`` that resolves ``sys.stdout`` at emit time.

    The structlog side of this module writes through a no-argument
    :class:`structlog.PrintLoggerFactory`, which resolves ``sys.stdout``
    lazily per write specifically so pytest's ``capsys`` / ``capfd``
    stream swap (which closes its wrapper at teardown) cannot strand a
    cached, closed stream on a later test -- see the note on
    :class:`~structlog.PrintLoggerFactory` in :func:`configure_logging`.

    The stdlib bridge must honour the same contract. A plain
    ``StreamHandler(sys.stdout)`` captures the stream at construction, so
    a bridge handler built while ``capsys`` is active would write to a
    closed buffer once a later test emits a stdlib log
    (``ValueError: I/O operation on closed file``). Resolving
    ``sys.stdout`` per emit keeps production behaviour identical (the
    real process stdout is never rebound at runtime) while staying
    test-safe.
    """

    def __init__(self) -> None:
        super().__init__(stream=sys.stdout)

    @property
    def stream(self) -> TextIO:
        return sys.stdout

    @stream.setter
    def stream(self, value: TextIO) -> None:
        # ``StreamHandler.__init__`` assigns ``self.stream``; swallow the
        # pin so the lazy property above always wins and stdout is
        # resolved per emit rather than frozen at construction.
        pass


def _shared_processors() -> list[structlog.typing.Processor]:
    """Processors run on both structlog-native and bridged stdlib records.

    Kept in one place so the two rendering paths stay in lock-step:
    structlog's own chain appends the exception renderer + JSON renderer
    to this list, and the stdlib bridge feeds it as the
    ``foreign_pre_chain`` of its :class:`ProcessorFormatter`. Ordering
    matters — ``merge_contextvars`` surfaces the middleware-bound
    ``request_id`` (correlation), ``add_log_level`` adds ``level``, and
    ``TimeStamper`` adds the ISO 8601 UTC ``timestamp``.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]


def _bridge_stdlib_logging(level: int, shared: list[structlog.typing.Processor]) -> None:
    """Route standard-library ``logging`` records through the JSON chain.

    Installs a single root-logger handler whose
    :class:`structlog.stdlib.ProcessorFormatter` renders every foreign
    (non-structlog) record as one JSON line. The ``foreign_pre_chain``
    runs :func:`_shared_processors` so bridged records gain the same
    ``request_id`` / ``level`` / ``timestamp`` keys; the final
    ``processors`` chain strips exception frame-locals
    (``show_locals=False``) exactly as the structlog-native path does,
    so a stdlib ``logging.exception`` carrying a secret as a frame local
    cannot leak it (CWE-532) any more than a structlog one can.

    uvicorn ships its own :func:`logging.config.dictConfig` at server
    startup -- which runs *before* the FastAPI lifespan calls
    :func:`configure_logging` -- pinning stderr handlers on ``uvicorn`` /
    ``uvicorn.error`` and a stdout access handler on ``uvicorn.access``,
    all with ``propagate=False``. This function re-points those loggers
    at the JSON root handler. Access logs are **dropped, not bridged**:
    :class:`~meho_backplane.middleware.RequestContextMiddleware` already
    emits a ``request_completed`` / ``request_failed`` JSON line per
    request (carrying ``request_id``), so a bridged ``uvicorn.access``
    line would merely duplicate it.

    Known limitation: uvicorn's pre-lifespan startup banner
    ("Started server process", "Uvicorn running on ...") is emitted
    before this reconfiguration runs, so those few lines keep uvicorn's
    own plain format. Everything logged once the app is serving is JSON.
    """
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False)),
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = _LazyStdoutHandler()
    handler.name = _STDLIB_BRIDGE_HANDLER_NAME
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.name == _STDLIB_BRIDGE_HANDLER_NAME]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error"):
        bridged = logging.getLogger(name)
        bridged.handlers.clear()
        bridged.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False


def configure_logging(level: int = logging.INFO) -> None:
    """Configure structlog for JSON output to stdout and bridge stdlib logs.

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
       traceback without locals remains sufficient for 5xx triage. The
       same renderer runs in the stdlib bridge's ``ProcessorFormatter``
       (see :func:`_bridge_stdlib_logging`), so a *stdlib* exception log
       is locals-stripped identically.
    5. ``JSONRenderer`` — final processor; serialises the event dict
       to a single JSON line.

    Items 1-3 live in :func:`_shared_processors` because the stdlib
    bridge reuses them as its ``foreign_pre_chain``; items 4-5 are
    appended here (and again, independently, inside the bridge's
    formatter) so both paths render exceptions and JSON identically.

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
    process ``sys.stdout`` does not get rebound at runtime. The stdlib
    bridge's :class:`_LazyStdoutHandler` resolves stdout per emit for
    the same reason.

    The standard-library ``logging`` module is bridged into this same
    pipeline by :func:`_bridge_stdlib_logging`; see its docstring for
    the uvicorn access-log decision and the startup-banner limitation.

    Args:
        level: Minimum log level emitted. Defaults to ``INFO``;
            tests pin this explicitly so capture is deterministic.
    """
    shared = _shared_processors()
    structlog.configure(
        processors=[
            *shared,
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
    _bridge_stdlib_logging(level, shared)
