# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read log lines as the production log setup writes them.

:func:`production_logging` runs :func:`~meho_backplane.logging.configure_logging`,
so each line is the JSON line of production, after the request log context
was merged into it. Read the lines from ``capfd`` with :func:`json_lines`.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from structlog._config import BoundLoggerLazyProxy

from meho_backplane.logging import configure_logging

#: The fields the request line adds of its own. The rest of the request line
#: is the request log context.
_REQUEST_LINE_OWN_FIELDS = frozenset(
    {"event", "level", "timestamp", "method", "path", "status", "duration_ms", "exception"}
)


@contextmanager
def production_logging() -> Iterator[None]:
    """Log with the production setup; put the earlier setup back at the end.

    Loggers are not cached, so a ``structlog.testing.capture_logs`` block in
    the same test still sees the lines of the code under test.
    """
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    configure_logging(logging.INFO)
    structlog.configure(cache_logger_on_first_use=False)
    _drop_cached_loggers()
    try:
        yield
    finally:
        structlog.reset_defaults()
        _drop_cached_loggers()
        root.handlers[:] = handlers
        root.setLevel(level)


def _drop_cached_loggers() -> None:
    """Make each module-level logger of MEHO read the current setup again."""
    for name, module in tuple(sys.modules.items()):
        if module is None or not name.startswith("meho_backplane"):
            continue
        for value in tuple(vars(module).values()):
            if isinstance(value, BoundLoggerLazyProxy):
                value.__dict__.pop("bind", None)


def json_lines(out: str) -> list[dict[str, Any]]:
    """Every JSON line of *out*."""
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def request_context_fields(lines: list[dict[str, Any]]) -> set[str]:
    """The fields of the request log context, read from the line of the request."""
    request = [line for line in lines if line["event"] in ("request_completed", "request_failed")]
    assert len(request) == 1, lines
    return set(request[0]) - _REQUEST_LINE_OWN_FIELDS
