# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Answers that carry a revealed secret value are never stored by a cache.

A person who asked to see a secret value (``reveal_secret: true``) gets it
in one answer: at once for a plain read, or once after a second person
approved (:mod:`meho_backplane.operations.approved_reveal`). That answer
must not be kept by a browser or a proxy. So it says ``Cache-Control:
no-store`` (and ``Pragma: no-cache`` for old HTTP/1.0 caches). Other answers
keep their headers as they are.

The dispatcher decides. When it puts revealed values into an answer, it
calls :func:`mark_answer_holds_secret`. Each route that runs an operation
for a person opens :func:`answer_scope` around the run and calls
:func:`set_no_store` on its response only when the answer was marked:
``POST /api/v1/operations/call``, ``POST /api/v1/approvals/{id}/reveal``,
the MCP route, and the console's operation and Vault pages. So a run that
was refused or ended with an error, and holds no value, keeps the usual
headers.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final

from starlette.responses import Response

__all__ = [
    "NO_STORE_HEADERS",
    "AnswerMark",
    "answer_scope",
    "mark_answer_holds_secret",
    "set_no_store",
]

#: The headers of an answer that carries a revealed secret value.
NO_STORE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def set_no_store[R: Response](response: R) -> R:
    """Set :data:`NO_STORE_HEADERS` on *response* and return it."""
    for name, value in NO_STORE_HEADERS.items():
        response.headers[name] = value
    return response


@dataclass
class AnswerMark:
    """Whether the answer of the current request carries a revealed secret value."""

    holds_secret: bool = False


_answer_mark_var: ContextVar[AnswerMark | None] = ContextVar("answer_mark", default=None)


@contextmanager
def answer_scope() -> Iterator[AnswerMark]:
    """Give the code inside the block a fresh mark, and yield it to the caller.

    The mark starts unset. After the block, the caller reads
    :attr:`AnswerMark.holds_secret` and calls :func:`set_no_store` on its
    response when it is set.
    """
    mark = AnswerMark()
    token = _answer_mark_var.set(mark)
    try:
        yield mark
    finally:
        _answer_mark_var.reset(token)


def mark_answer_holds_secret() -> None:
    """Mark the answer of the current request as one that carries a revealed value.

    Outside an :func:`answer_scope` this does nothing.
    """
    mark = _answer_mark_var.get()
    if mark is not None:
        mark.holds_secret = True
