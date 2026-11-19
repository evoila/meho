# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The steps of a multi-step operation, for the "uncertain" rule.

A multi-step operation (``source_kind='composite'``) runs its steps through
``dispatch_child``. When one step ends ``uncertain`` (MEHO does not know if
that change happened) and the operation then fails, MEHO does not know the
state of the target either. So the operation's own error result is stored
as ``uncertain`` too, with the list of its steps and the state of each:

* ``done``: the step answered ``ok``;
* ``unknown``: the step ended ``uncertain``;
* ``failed``: any other answer, or the step call raised.

The dispatcher opens one step log per multi-step run
(:func:`track_composite_steps`), wraps the run's ``dispatch_child``
(:func:`recording_steps`), and marks every error result of that run on its
way to the audit row (:func:`mark_when_a_step_is_unknown`). The log carries
the run's ``audit_id``, so a step's own error result, which passes the same
audit path inside the run, is never marked by the log of its parent.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

from meho_backplane.connectors import OperationResult
from meho_backplane.operations.uncertain_outcome import UNCERTAIN_OUTCOME, with_uncertain_outcome

if TYPE_CHECKING:
    from meho_backplane.operations.composite import DispatchChild

__all__ = [
    "STEPS_KEY",
    "mark_when_a_step_is_unknown",
    "recording_steps",
    "track_composite_steps",
]

#: The ``extras`` key that holds the steps of an uncertain multi-step run.
STEPS_KEY: Final[str] = "steps"

StepState = Literal["done", "failed", "unknown"]


@dataclass
class _StepLog:
    audit_id: uuid.UUID
    steps: list[dict[str, str]] = field(default_factory=list)

    def record(self, op_id: str, result: Any) -> None:
        self.steps.append({"op_id": op_id, "state": _state_of(result)})

    def any_unknown(self) -> bool:
        return any(step["state"] == "unknown" for step in self.steps)


_step_log_var: ContextVar[_StepLog | None] = ContextVar("composite_step_log", default=None)


def _state_of(result: Any) -> StepState:
    if not isinstance(result, OperationResult):
        return "failed"
    if result.status == "ok":
        return "done"
    if result.extras.get("execution_outcome") == UNCERTAIN_OUTCOME:
        return "unknown"
    return "failed"


@contextmanager
def track_composite_steps(audit_id: uuid.UUID, *, enabled: bool) -> Iterator[None]:
    """Keep a step log for the run with *audit_id* while the block runs.

    Does nothing when *enabled* is ``False`` (an operation with one step).
    """
    if not enabled:
        yield
        return
    token = _step_log_var.set(_StepLog(audit_id=audit_id))
    try:
        yield
    finally:
        _step_log_var.reset(token)


def _log_of(audit_id: uuid.UUID) -> _StepLog | None:
    log = _step_log_var.get()
    return log if log is not None and log.audit_id == audit_id else None


def recording_steps(dispatch_child: DispatchChild, audit_id: uuid.UUID) -> DispatchChild:
    """*dispatch_child* that writes each step into the log of the run *audit_id*."""
    log = _log_of(audit_id)
    if log is None:
        return dispatch_child

    async def _recorded(
        *,
        connector_id: str,
        op_id: str,
        params: dict[str, Any],
        target: Any = None,
    ) -> OperationResult:
        try:
            result = await dispatch_child(
                connector_id=connector_id, op_id=op_id, params=params, target=target
            )
        except BaseException:
            log.record(op_id, None)
            raise
        log.record(op_id, result)
        return result

    return _recorded


def mark_when_a_step_is_unknown(result: OperationResult, audit_id: uuid.UUID) -> OperationResult:
    """*result* as ``uncertain`` with its steps, when a step of the run ended uncertain.

    Only an error result of the multi-step run *audit_id* is marked. Any
    other result passes unchanged.
    """
    log = _log_of(audit_id)
    if log is None or result.status != "error" or not log.any_unknown():
        return result
    already = result.extras.get("execution_outcome") == UNCERTAIN_OUTCOME
    marked = result if already else with_uncertain_outcome(result)
    extras = {**marked.extras, STEPS_KEY: [dict(step) for step in log.steps]}
    return marked.model_copy(update={"extras": extras})
