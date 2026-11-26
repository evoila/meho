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
hash, in the order of the first run. A result is kept only when it is JSON,
the step's operation does not read, write or make a credential (its class,
:func:`~meho_backplane.broadcast.events.classify_op`, or an operation whose
answer hides its values), and it holds no secret-named value (also no number
under a secret name); otherwise the entry stays ``started``. A replayed result is a copy, so a
handler that changes it does not change what a later wait stores.

A credential step keeps no result, so it has its own rules:

* A step that writes or makes a credential and ended well becomes
  ``done_no_result``: the entry says only that the step is finished. On a
  resume the step is not sent again and gives back no result.
* A step that reads a credential is ``run_again`` when the read is on
  :data:`_READS_SAFE_TO_SEND_AGAIN` and its policy let it go ahead with no
  person: on a resume MEHO checks its policy again and sends it again, with
  the same target and params, only when the policy still lets it go ahead
  with no person. A read on that list changes nothing on any system, and its
  value is never stored.
* Every other step that reads a credential stays ``started``: a read that
  is not on the list (it may also store a value somewhere, for example a
  read that copies a secret into Vault), and a read that a person approved
  (its own approval, or the approval of the run, also for a read run through
  ``dispatch_child``). A resume does not send it again: a later step that
  would wait stops the run at once, with an error that names the read.
* A step whose end MEHO does not know stays ``started`` and stops the run.
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

from meho_backplane.broadcast.events import classify_op
from meho_backplane.op_classes import CREDENTIAL_CHANGE_OP_CLASSES
from meho_backplane.operations._secret_values import hides_secret_values
from meho_backplane.operations._validate import compute_params_hash, params_hash_matches
from meho_backplane.redaction.secret_fields import scrub_secret_fields

__all__ = [
    "STEP_DONE",
    "STEP_DONE_NO_RESULT",
    "STEP_RUN_AGAIN",
    "STEP_STARTED",
    "StepJournal",
    "child_read_sent_again",
    "composite_resume_steps",
    "composite_step_journal",
    "current_step_journal",
    "earlier_run_sent_a_request",
    "is_credential_read",
    "mark_child_cleared_by_person",
    "step_target_key",
]

_log = structlog.get_logger(__name__)

#: The step ran to its end; the entry holds its ``result``.
STEP_DONE = "done"
#: The step was started; MEHO has no stored end for it.
STEP_STARTED = "started"
#: A step that writes or makes a credential ran to its end; no result is kept.
STEP_DONE_NO_RESULT = "done_no_result"
#: A step that reads a credential and went ahead with no person; a resume
#: checks its policy and sends it again, nothing is kept.
STEP_RUN_AGAIN = "run_again"

#: The statuses of a step that a resume does not stop at.
_FINISHED = frozenset({STEP_DONE, STEP_DONE_NO_RESULT, STEP_RUN_AGAIN})

#: Classes of an operation that writes, makes or uses a credential. MEHO
#: never stores its result: it may hold a secret under any field name.
_CREDENTIAL_CHANGE_CLASSES = CREDENTIAL_CHANGE_OP_CLASSES | {"credential_use"}

#: The credential reads a resume may send again: each one only reads, and
#: changes and stores nothing on any system. A credential read that is not
#: here is never sent again.
_READS_SAFE_TO_SEND_AGAIN = frozenset({"vault.kv.read", "vault.kv.list", "vault.kv.compare"})


def is_credential_read(op_id: str) -> bool:
    """True when *op_id* reads a credential: its class, or an answer that hides its values."""
    return classify_op(op_id) == "credential_read" or hides_secret_values(op_id)


def _may_send_again(op_id: str) -> bool:
    """True when a resume may send the credential read *op_id* again."""
    return op_id in _READS_SAFE_TO_SEND_AGAIN and is_credential_read(op_id)


def _is_credential_change(op_id: str) -> bool:
    """True when *op_id* writes, makes or uses a credential."""
    return classify_op(op_id) in _CREDENTIAL_CHANGE_CLASSES


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
    #: True when the earlier run of this composite sent a step or a request.
    earlier_sent: bool = False
    #: The op id of the ``dispatch_child`` read a resume sends again right now.
    sending_again: str | None = None

    def take(
        self, connector_id: str, op_id: str, target: Any, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        """A copy of the earlier run's entry for this step, or ``None`` when it had none.

        The first matching entry is taken, so a step that ran twice with the
        same target and params is matched twice, in order. A taken entry
        stays on the journal, so a later wait stores it again. The caller
        gets a deep copy: changing the returned result does not change the
        stored one.

        A ``run_again`` entry is taken off the earlier run's entries but not
        added to this run's: the caller checks the step's policy, sends it
        again and records it with :meth:`start`. A ``run_again`` entry whose
        operation is not a read a resume may send again is given back as
        ``started``, so the caller stops the run instead of sending it.
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
                if entry.get("status") == STEP_RUN_AGAIN and not _may_send_again(op_id):
                    entry["status"] = STEP_STARTED
                if entry.get("status") != STEP_RUN_AGAIN:
                    self.entries.append(entry)
                return copy.deepcopy(entry)
        return None

    def start(
        self,
        connector_id: str,
        op_id: str,
        target: Any,
        params: dict[str, Any],
        *,
        may_send_again: bool = False,
    ) -> dict[str, Any]:
        """Add an entry for a step that is about to run.

        A credential read on :data:`_READS_SAFE_TO_SEND_AGAIN` is
        ``run_again`` from the start when *may_send_again* is ``True``: its
        policy let it go ahead with no person, or the step runs through the
        dispatcher, which checks it again (and calls
        :func:`mark_child_cleared_by_person` when a person's approval let it
        go ahead). Such a read changes nothing, so a resume may send it again
        however it ended. Every other step is ``started``; every other
        credential read stays ``started`` too, and a resume does not send it
        again.
        """
        entry: dict[str, Any] = {
            "connector_id": connector_id,
            "op_id": op_id,
            "target": step_target_key(target),
            "params_hash": compute_params_hash(params),
            "status": (
                STEP_RUN_AGAIN if may_send_again and _may_send_again(op_id) else STEP_STARTED
            ),
        }
        self.entries.append(entry)
        return entry

    def finish(self, entry: dict[str, Any], value: Any, *, ended_well: bool = True) -> None:
        """Mark *entry* finished, and keep *value* when it can be kept.

        * A step that reads a credential keeps its status (``run_again`` or
          ``started``); *value* is not kept.
        * A step that writes or makes a credential becomes ``done_no_result``
          when *ended_well*; *value* is not kept, whatever its field names.
          When it did not end well it stays ``started``.
        * Any other step becomes ``done`` with *value*. A value that is not
          JSON, or that holds a secret-named value (a number under a secret
          name too), is not stored, and the entry stays ``started``.

        A ``started`` entry cannot be replayed: a later run stops at it
        instead of sending it again.
        """
        op_id = entry["op_id"]
        if entry.get("status") == STEP_RUN_AGAIN or is_credential_read(op_id):
            return
        if _is_credential_change(op_id):
            if ended_well:
                entry["status"] = STEP_DONE_NO_RESULT
            _log.info(
                "composite_step_result_not_kept",
                op_id=op_id,
                reason="credential",
                finished=ended_well,
            )
            return
        try:
            result = to_jsonable_python(value)
        except PydanticSerializationError:
            _log.warning("composite_step_result_not_kept", op_id=op_id, reason="not_json")
            return
        if scrub_secret_fields(result, hide_numbers=True) != result:
            _log.warning("composite_step_result_not_kept", op_id=op_id, reason="secret")
            return
        entry["status"] = STEP_DONE
        entry["result"] = result

    def mark_cleared_by_person(self, op_id: str) -> None:
        """Mark the last entry, a ``run_again`` read *op_id*, as ``started``.

        A person's approval let the read go ahead, so a resume does not send
        it again.
        """
        if self.entries:
            last = self.entries[-1]
            if last.get("op_id") == op_id and last.get("status") == STEP_RUN_AGAIN:
                last["status"] = STEP_STARTED

    def first_unfinished(self) -> str | None:
        """The op id of the first stored step with no stored end, or ``None``."""
        for entry in (*self.entries, *self.replay):
            if entry.get("status") not in _FINISHED:
                return str(entry.get("op_id"))
        return None

    def stored(self) -> list[dict[str, Any]]:
        """Every entry to store on an approval request: this run's, then the ones not reached."""
        return [copy.deepcopy(entry) for entry in (*self.entries, *self.replay)]


@dataclass
class _ResumeSteps:
    """The stored entries of an approved request, waiting for their composite to start."""

    composite_op_id: str
    steps: list[dict[str, Any]]
    earlier_sent: bool = False
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


def mark_child_cleared_by_person(op_id: str) -> None:
    """A ``dispatch_child`` step *op_id* goes ahead under the approval of the run.

    Called by the dispatcher. The step is the last entry of the composite's
    journal: a credential read cleared this way is not sent again on a resume.
    """
    journal = current_step_journal()
    if journal is not None:
        journal.mark_cleared_by_person(op_id)


def child_read_sent_again(op_id: str) -> bool:
    """True when the ``dispatch_child`` step *op_id* is a read a resume sends again.

    Called by the dispatcher before the approval of the run lets a child step
    go ahead: such a read goes ahead again only when its own policy lets it go
    ahead with no person.
    """
    journal = current_step_journal()
    return journal is not None and journal.sending_again == op_id


def earlier_run_sent_a_request(op_id: str) -> bool:
    """True when *op_id* is about to run again after an approval and had sent something.

    The record of the run that waited: a step or a request of the multi-step
    operation was sent before it waited.
    """
    resume = _resume_steps_var.get()
    return resume is not None and resume.composite_op_id == op_id and resume.earlier_sent


@contextmanager
def composite_step_journal(op_id: str) -> Iterator[StepJournal]:
    """Bind a new journal for one run of composite *op_id*.

    During the run after an approval, the first run of the composite the
    request names gets the stored entries to replay. A nested composite, or
    a second run of the same one, starts with none.
    """
    resume = _resume_steps_var.get()
    replay: list[dict[str, Any]] = []
    earlier_sent = False
    if resume is not None and not resume.taken and resume.composite_op_id == op_id:
        resume.taken = True
        replay = [dict(entry) for entry in resume.steps if isinstance(entry, dict)]
        earlier_sent = resume.earlier_sent
    journal = StepJournal(composite_op_id=op_id, replay=replay, earlier_sent=earlier_sent)
    token = _step_journal_var.set(journal)
    try:
        yield journal
    finally:
        _step_journal_var.reset(token)


@contextmanager
def composite_resume_steps(
    composite_op_id: str, steps: Any, *, earlier_sent: bool = False
) -> Iterator[None]:
    """Hand the stored *steps* of an approved request to its composite's next run.

    *earlier_sent*: the run that waited had sent a step or a request.
    """
    stored = steps if isinstance(steps, list) else []
    token = _resume_steps_var.set(
        _ResumeSteps(composite_op_id=composite_op_id, steps=stored, earlier_sent=earlier_sent)
    )
    try:
        yield
    finally:
        _resume_steps_var.reset(token)
