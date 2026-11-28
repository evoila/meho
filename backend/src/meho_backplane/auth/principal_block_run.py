# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What one running operation sent, and the step the block list refused in it.

The dispatcher opens one record for each operation it runs
(:func:`tracking_run`). A step of a multi-step operation has its own record,
below the record of the operation.

* :func:`mark_request_sent` notes that a request goes to the target. A
  refusal after that says that a part of the operation may have run
  (:func:`request_was_sent`). For a step, the records above also note the
  step's operation id, and :func:`mark_step_sent` notes a direct step of a
  multi-step operation. A refusal names these steps (:func:`sent_steps`).
* :func:`mark_step_refused` notes the first step the block list refused. The
  whole operation then stops with that refusal
  (:func:`raise_if_step_was_refused`), also when its handler catches the
  error of the step and goes on. Each later step or request reads it first
  (:func:`step_refused_in_run`) and is not sent.

Both marks also reach the records above, so the multi-step operation knows
what its steps did.

* :func:`cleanup_without_block_check` marks a clean-up: the removal of an
  account, user or token that the same run just made on a target and could
  not store. Its requests run with no block check (:func:`in_cleanup`).
* :func:`finish_without_block_check` marks a finish: the step that brings
  the target and the secret store in step after a change that the run
  cannot undo. Its requests run with no block check too.
  :func:`note_not_removed` notes what such a clean-up could not remove, so
  the answer names it (:func:`not_removed`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

__all__ = [
    "cleanup_without_block_check",
    "finish_without_block_check",
    "in_cleanup",
    "mark_request_sent",
    "mark_step_refused",
    "mark_step_sent",
    "not_removed",
    "note_not_removed",
    "raise_if_step_was_refused",
    "request_was_sent",
    "run_login",
    "sent_steps",
    "step_refused_in_run",
    "tracking_run",
]


@dataclass
class _Run:
    parent: _Run | None
    login: tuple[str, uuid.UUID | None] | None = None
    op_id: str | None = None
    sent: bool = False
    steps_sent: list[str] = field(default_factory=list)
    refused: Exception | None = None
    not_removed: list[str] = field(default_factory=list)

    def with_parents(self) -> Iterator[_Run]:
        run: _Run | None = self
        while run is not None:
            yield run
            run = run.parent


_run_var: ContextVar[_Run | None] = ContextVar("principal_block_run", default=None)
_cleanup_var: ContextVar[bool] = ContextVar("principal_block_cleanup", default=False)


@contextmanager
def tracking_run(
    *, is_step: bool, login: tuple[str, uuid.UUID | None], op_id: str | None = None
) -> Iterator[None]:
    """Keep a record while one operation runs.

    *is_step* is true for a step of a multi-step operation: its marks then
    also reach the record of that operation. *login* is the ``(sub,
    tenant_id)`` of the operation's operator, the login every send of the
    operation checks (:func:`run_login`). *op_id* is the operation id; for a
    step, the records above note it when the step sends (:func:`sent_steps`).
    """
    parent = _run_var.get() if is_step else None
    token = _run_var.set(_Run(parent=parent, login=login, op_id=op_id))
    try:
        yield
    finally:
        _run_var.reset(token)


def run_login() -> tuple[str, uuid.UUID | None] | None:
    """The login of the running operation, or ``None`` outside one."""
    run = _run_var.get()
    return None if run is None else run.login


def mark_request_sent() -> None:
    """Note that the running operation sends a request to the target."""
    run = _run_var.get()
    if run is None:
        return
    for each in run.with_parents():
        each.sent = True
    if run.parent is not None and run.op_id is not None:
        _note_step(run.parent, run.op_id)


def mark_step_sent(op_id: str) -> None:
    """Note that a direct step *op_id* of the running multi-step operation goes to the target."""
    run = _run_var.get()
    if run is None:
        return
    for each in run.with_parents():
        each.sent = True
    _note_step(run, op_id)


def _note_step(run: _Run, op_id: str) -> None:
    for each in run.with_parents():
        if op_id not in each.steps_sent:
            each.steps_sent.append(op_id)


def sent_steps() -> list[str]:
    """The operation ids of the steps the running multi-step operation sent, in order."""
    run = _run_var.get()
    if run is None:
        return []
    *_, top = run.with_parents()
    return list(top.steps_sent)


def request_was_sent() -> bool:
    """True when the running operation, or a record above it, sent a request.

    The records above count too: for a nested part, a step that the
    multi-step operation sent before it is an earlier step of the same run.
    """
    run = _run_var.get()
    return run is not None and any(each.sent for each in run.with_parents())


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


def step_refused_in_run() -> Exception | None:
    """The first refusal of the running operation or of a record above it, else ``None``.

    The records above count too: a step of a nested or parallel part of the
    same multi-step operation may be the one that was refused.
    """
    run = _run_var.get()
    if run is None:
        return None
    return next((each.refused for each in run.with_parents() if each.refused is not None), None)


@contextmanager
def _without_block_check() -> Iterator[None]:
    token = _cleanup_var.set(True)
    try:
        yield
    finally:
        _cleanup_var.reset(token)


@contextmanager
def cleanup_without_block_check() -> Iterator[None]:
    """Run the requests of this block with no block check, for a clean-up.

    Only for the removal of an account, user or token that the same run just
    made on a target and could not store, and the end of MEHO's own Vault
    login. The requests still count as sent.
    """
    with _without_block_check():
        yield


@contextmanager
def finish_without_block_check() -> Iterator[None]:
    """Run the requests of this block with no block check, so the run finishes.

    Only for an operation that changed a target and cannot undo the change:
    the step that brings the target and the secret store in step runs to its
    end. Examples: the store of a token that the run rotated, and the setting
    of a password that the run stored for a login. The requests still count
    as sent.
    """
    with _without_block_check():
        yield


def in_cleanup() -> bool:
    """True inside :func:`cleanup_without_block_check` or :func:`finish_without_block_check`."""
    return _cleanup_var.get()


def note_not_removed(what: str) -> None:
    """Note that a clean-up of the running operation could not remove *what*."""
    run = _run_var.get()
    if run is None:
        return
    for each in run.with_parents():
        if what not in each.not_removed:
            each.not_removed.append(what)


def not_removed() -> list[str]:
    """What the clean-ups of the running operation could not remove, in order."""
    run = _run_var.get()
    return [] if run is None else list(run.not_removed)
