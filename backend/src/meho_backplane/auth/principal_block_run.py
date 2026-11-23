# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What one running operation sent, and the step the block list refused in it.

The dispatcher opens one record for each operation it runs
(:func:`tracking_run`). A step of a multi-step operation has its own record,
below the record of the operation.

* :func:`mark_request_sent` notes that a request goes to the target. A
  refusal after that says that a part of the operation may have run
  (:func:`request_was_sent`).
* :func:`mark_step_refused` notes the first step the block list refused. The
  whole operation then stops with that refusal
  (:func:`raise_if_step_was_refused`), also when its handler catches the
  error of the step and goes on.

Both marks also reach the records above, so the multi-step operation knows
what its steps did.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

__all__ = [
    "mark_request_sent",
    "mark_step_refused",
    "raise_if_step_was_refused",
    "request_was_sent",
    "tracking_run",
]


@dataclass
class _Run:
    parent: _Run | None
    sent: bool = False
    refused: Exception | None = None

    def with_parents(self) -> Iterator[_Run]:
        run: _Run | None = self
        while run is not None:
            yield run
            run = run.parent


_run_var: ContextVar[_Run | None] = ContextVar("principal_block_run", default=None)


@contextmanager
def tracking_run(*, is_step: bool) -> Iterator[None]:
    """Keep a record while one operation runs.

    *is_step* is true for a step of a multi-step operation: its marks then
    also reach the record of that operation.
    """
    token = _run_var.set(_Run(parent=_run_var.get() if is_step else None))
    try:
        yield
    finally:
        _run_var.reset(token)


def mark_request_sent() -> None:
    """Note that the running operation sends a request to the target."""
    run = _run_var.get()
    if run is not None:
        for each in run.with_parents():
            each.sent = True


def request_was_sent() -> bool:
    """True when the running operation, or a step of it, sent a request."""
    run = _run_var.get()
    return run is not None and run.sent


def mark_step_refused(refusal: Exception) -> None:
    """Note that the block list refused a step of the running operation."""
    run = _run_var.get()
    if run is not None:
        for each in run.with_parents():
            if each.refused is None:
                each.refused = refusal


def raise_if_step_was_refused() -> None:
    """Raise the refusal of a step of the running operation, when there is one."""
    run = _run_var.get()
    if run is not None and run.refused is not None:
        raise run.refused
