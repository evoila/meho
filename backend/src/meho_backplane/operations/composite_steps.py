# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The steps a multi-step operation already ran, kept for the run after an approval.

A multi-step (composite) operation can stop at a step that waits for a
person. After the approval MEHO runs the operation again from its first
step. This module makes sure no step that already ran is sent again.

* While a composite runs, :func:`composite_step_journal` binds one
  :class:`StepJournal` for its handler body. Each step the composite runs
  through ``dispatch_child``, :func:`~meho_backplane.operations.composite.run_governed_subop`
  or :func:`~meho_backplane.operations.composite.enforce_subop_policy` gets
  an entry: ``started`` before it runs, ``done`` with its result after.
* When a step waits for approval, the entries are stored on the approval
  request, in ``resume_parent["steps"]``.
* The run after the approval binds those entries with
  :func:`composite_resume_steps`. A step of the new run that has an entry
  is not sent again: a ``done`` step returns its stored result, and a step
  with no stored end stops the run (its outcome is unknown).

An entry is matched on the connector, the op id, the target and the params
hash, in the order of the first run. A result is kept only when it is JSON
and holds no secret-named value (also no number under a secret name);
otherwise the entry stays ``started``. A replayed result is a copy, so a
handler that changes it does not change what a later wait stores.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic_core import PydanticSerializationError, to_jsonable_python

from meho_backplane.operations._validate import compute_params_hash, params_hash_matches
from meho_backplane.redaction.secret_fields import scrub_secret_fields

__all__ = [
    "STEP_DONE",
    "STEP_STARTED",
    "StepJournal",
    "composite_resume_steps",
    "composite_step_journal",
    "current_step_journal",
    "step_target_key",
]

_log = structlog.get_logger(__name__)

#: The step ran to its end; the entry holds its ``result``.
STEP_DONE = "done"
#: The step was started; MEHO has no stored end for it.
STEP_STARTED = "started"


def step_target_key(target: Any) -> str | None:
    """The stable identity of a step's target: its id, else its name, else ``None``."""
    if target is None:
        return None
    if isinstance(target, str):
        return f"name:{target}"
    raw_id = getattr(target, "id", None)
    if isinstance(raw_id, uuid.UUID):
        return f"id:{raw_id}"
    name = getattr(target, "name", None)
    if isinstance(name, str):
        return f"name:{name}"
    return f"repr:{target!r}"


@dataclass
class StepJournal:
    """The steps one run of one composite went through, in order."""

    composite_op_id: str
    #: Entries of this run: the ones taken from *replay*, then the new ones.
    entries: list[dict[str, Any]] = field(default_factory=list)
    #: Entries of the earlier run that this run has not reached yet.
    replay: list[dict[str, Any]] = field(default_factory=list)

    def take(
        self, connector_id: str, op_id: str, target: Any, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        """A copy of the earlier run's entry for this step, or ``None`` when it had none.

        The first matching entry is taken, so a step that ran twice with the
        same target and params is matched twice, in order. A taken entry
        stays on the journal, so a later wait stores it again. The caller
        gets a deep copy: changing the returned result does not change the
        stored one.
        """
        target_key = step_target_key(target)
        for index, entry in enumerate(self.replay):
            if (
                entry.get("connector_id") == connector_id
                and entry.get("op_id") == op_id
                and entry.get("target") == target_key
                and params_hash_matches(params, str(entry.get("params_hash", "")))
            ):
                del self.replay[index]
                self.entries.append(entry)
                return copy.deepcopy(entry)
        return None

    def start(
        self, connector_id: str, op_id: str, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Add a ``started`` entry for a step that is about to run."""
        entry: dict[str, Any] = {
            "connector_id": connector_id,
            "op_id": op_id,
            "target": step_target_key(target),
            "params_hash": compute_params_hash(params),
            "status": STEP_STARTED,
        }
        self.entries.append(entry)
        return entry

    def finish(self, entry: dict[str, Any], value: Any) -> None:
        """Mark *entry* ``done`` with *value*, when *value* can be kept.

        A value that is not JSON, or that holds a secret-named value (a
        number under a secret name too), is not stored. The entry then stays
        ``started``: a later run cannot replay the step and stops instead of
        sending it again.
        """
        try:
            result = to_jsonable_python(value)
        except PydanticSerializationError:
            _log.warning("composite_step_result_not_kept", op_id=entry["op_id"], reason="not_json")
            return
        if scrub_secret_fields(result, hide_numbers=True) != result:
            _log.warning("composite_step_result_not_kept", op_id=entry["op_id"], reason="secret")
            return
        entry["status"] = STEP_DONE
        entry["result"] = result

    def stored(self) -> list[dict[str, Any]]:
        """Every entry to store on an approval request: this run's, then the ones not reached."""
        return [copy.deepcopy(entry) for entry in (*self.entries, *self.replay)]


@dataclass
class _ResumeSteps:
    """The stored entries of an approved request, waiting for their composite to start."""

    composite_op_id: str
    steps: list[dict[str, Any]]
    taken: bool = False


_step_journal_var: ContextVar[StepJournal | None] = ContextVar(
    "composite_step_journal", default=None
)
_resume_steps_var: ContextVar[_ResumeSteps | None] = ContextVar(
    "composite_resume_steps", default=None
)


def current_step_journal() -> StepJournal | None:
    """The journal of the composite whose handler body is running, or ``None``."""
    return _step_journal_var.get()


@contextmanager
def composite_step_journal(op_id: str) -> Iterator[StepJournal]:
    """Bind a new journal for one run of composite *op_id*.

    During the run after an approval, the first run of the composite the
    request names gets the stored entries to replay. A nested composite, or
    a second run of the same one, starts with none.
    """
    resume = _resume_steps_var.get()
    replay: list[dict[str, Any]] = []
    if resume is not None and not resume.taken and resume.composite_op_id == op_id:
        resume.taken = True
        replay = [dict(entry) for entry in resume.steps if isinstance(entry, dict)]
    journal = StepJournal(composite_op_id=op_id, replay=replay)
    token = _step_journal_var.set(journal)
    try:
        yield journal
    finally:
        _step_journal_var.reset(token)


@contextmanager
def composite_resume_steps(composite_op_id: str, steps: Any) -> Iterator[None]:
    """Hand the stored *steps* of an approved request to its composite's next run."""
    stored = steps if isinstance(steps, list) else []
    token = _resume_steps_var.set(_ResumeSteps(composite_op_id=composite_op_id, steps=stored))
    try:
        yield
    finally:
        _resume_steps_var.reset(token)
