# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reusable vim ``*_Task`` poll-to-terminal-state helper (#2893).

Every vim (VI-JSON / SOAP-semantics) mutating method that vCenter models
as long-running returns a ``Task`` :class:`ManagedObjectReference` rather
than the finished result -- ``VirtualMachine.ReconfigVM_Task``,
``VirtualMachine.CloneVM_Task`` (Task D / #2894),
``ClusterComputeResource.ReconfigureComputeResource_Task`` (Task E /
#2895). The caller must then poll the returned Task's ``info`` property
until it reaches a terminal state (``success`` / ``error``) before
declaring the write done. This module is the single, reusable poll seam
those write composites share.

It generalises the one-shot ``Task.info`` read already present in
:mod:`~meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent`
(``vmware.tasks.recent`` reads ``TaskManager.recentTask`` then
``Task.info`` once) into a *poll loop*: it reuses that module's public
:func:`~meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent.build_task_info_retrieve_params`
request builder, re-reads ``Task.info`` through
:meth:`VmwareRestConnector._post_vmomi_json` on a fixed cadence, and
returns once the task is terminal or the wall-clock deadline elapses.

Poll semantics
--------------

* **success** -- ``TaskInfo.state == "success"``; the
  :class:`VimTaskResult` carries ``TaskInfo.result`` (e.g. the new VM
  MoRef a ``CloneVM_Task`` produced) so the caller can thread it forward.
* **error** -- ``TaskInfo.state == "error"``; the result carries the
  localized fault message (``TaskInfo.error.localizedMessage``). The poll
  helper does **not** raise on a task fault -- it returns the outcome so
  each caller maps it to its own status envelope (a disk-grow fault and a
  clone fault surface differently).
* **timeout** -- the deadline elapsed before a terminal state; the result
  carries the last-observed progress. The task itself may still complete
  in the background -- the wall-clock bound mirrors the 600 s convention
  ``vm.clone``'s ``GET:/cis/tasks/{task}`` poll established.

The bound is wall-clock, not iteration-count: a poll that observes
``queued`` / ``running`` sleeps ``poll_interval`` and re-reads until the
deadline. The helper is transport-fault-transparent -- an
:exc:`httpx.HTTPError` from the ``Task.info`` read propagates to the
caller (a load-bearing failure the dispatcher wraps as
``connector_error``), it is not swallowed as "still running".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.connectors.vmware_rest.typed_ops import _unwrap_value
from meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent import (
    build_task_info_retrieve_params,
)
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "TASK_STATE_ERROR",
    "TASK_STATE_SUCCESS",
    "VimTaskResult",
    "poll_vim_task",
]

_log = structlog.get_logger(__name__)

#: The vim ``RetrievePropertiesEx`` method path (the PropertyCollector
#: singleton moId rides the path, so the body is just the method args) --
#: the same path the typed reads and vi-json read composites POST through
#: :meth:`VmwareRestConnector._post_vmomi_json`.
_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"

_PROP_INFO = "info"

#: Terminal ``TaskInfoState`` enum values (vim25 wire contract). ``queued``
#: / ``running`` are the non-terminal states the poll loop waits through.
TASK_STATE_SUCCESS = "success"
TASK_STATE_ERROR = "error"
_TERMINAL_STATES: frozenset[str] = frozenset({TASK_STATE_SUCCESS, TASK_STATE_ERROR})

#: Poll-outcome sentinel the helper stamps when the deadline elapsed before
#: the task reached a terminal state. Not a vim ``TaskInfoState`` value --
#: the task is still ``queued`` / ``running`` server-side.
_OUTCOME_TIMEOUT = "timeout"

#: Default wall-clock bound + cadence. 600 s mirrors the ``vm.clone``
#: ``GET:/cis/tasks/{task}`` poll convention; a 2 s cadence keeps a fast
#: reconfigure responsive without hammering ``Task.info``.
_DEFAULT_TIMEOUT_SECONDS = 600.0
_DEFAULT_POLL_INTERVAL = 2.0


@dataclass(frozen=True)
class VimTaskResult:
    """Terminal (or timed-out) outcome of polling a vim ``*_Task`` MoRef.

    Attributes:
        task: The polled Task moid.
        state: ``"success"`` / ``"error"`` (terminal vim ``TaskInfoState``)
            or ``"timeout"`` (the poll deadline elapsed first).
        result: ``TaskInfo.result`` on success -- e.g. the new VM MoRef a
            ``CloneVM_Task`` produced. ``None`` on error / timeout, and on a
            success whose task produces no result (``ReconfigVM_Task``).
        error_message: The localized fault message
            (``TaskInfo.error.localizedMessage``) on error; ``None``
            otherwise.
        progress: Last-observed ``TaskInfo.progress`` (0-100) -- present on
            a timeout so the caller can report how far the background task
            got.
    """

    task: str
    state: str
    result: Any = None
    error_message: str | None = None
    progress: int | None = None

    @property
    def succeeded(self) -> bool:
        """``True`` iff the task reached the terminal ``success`` state."""
        return self.state == TASK_STATE_SUCCESS

    @property
    def timed_out(self) -> bool:
        """``True`` iff the poll deadline elapsed before a terminal state."""
        return self.state == _OUTCOME_TIMEOUT


def task_moref_value(task: Any) -> str | None:
    """Return the Task moid from a vim MoRef, a bare moid, or ``None``.

    A vim ``ManagedObjectReference`` -- what a ``*_Task`` method returns --
    serialises as ``{"type": "Task", "value": "task-42"}`` (optionally with
    a ``_typeName`` discriminator). A bare moid string is tolerated so a
    caller that already unwrapped the MoRef can pass it straight through.
    """
    if isinstance(task, dict):
        value = task.get("value")
        return value if isinstance(value, str) else None
    return task if isinstance(task, str) else None


def _extract_task_info(retrieve_result: Any, task_moid: str) -> dict[str, Any] | None:
    """Pull the raw ``TaskInfo`` for *task_moid* out of a RetrievePropertiesEx result.

    ``RetrievePropertiesEx`` returns a ``RetrieveResult`` whose ``objects``
    list carries one ``ObjectContent`` per queried object, each with a
    ``propSet`` list of ``{name, val}`` pairs. The poll queries exactly one
    Task, so the matching object's ``info`` propSet holds the ``TaskInfo``.
    Matches the object by moid when the ``obj`` MoRef is present, else falls
    back to the first object's ``info`` (single-object query). Returns
    ``None`` when the property is absent -- the caller treats that as "not
    terminal yet", never an exception.

    The ``val`` sits in an ``Any`` placeholder, so it funnels through
    :func:`unwrap_vim_value` (#3106): a boxed ``info.state`` /
    ``error.localizedMessage`` / ``progress`` / ``result`` primitive
    normalises to its bare value here, which is what makes the downstream
    terminal-state, fault-message, and progress reads box-tolerant.
    """
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    if not isinstance(objects, list):
        return None
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        obj_moid = task_moref_value(obj.get("obj"))
        if obj_moid is not None and obj_moid != task_moid:
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and prop.get("name") == _PROP_INFO:
                val = unwrap_vim_value(prop.get("val"))
                return val if isinstance(val, dict) else None
    return None


def _fault_message(info: dict[str, Any]) -> str | None:
    """Return the localized fault message from a faulted ``TaskInfo``."""
    error = info.get("error")
    if not isinstance(error, dict):
        return None
    localized = error.get("localizedMessage")
    return localized if isinstance(localized, str) else None


def _progress(info: dict[str, Any]) -> int | None:
    """Return ``TaskInfo.progress`` as an int, else ``None``."""
    value = info.get("progress")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def poll_vim_task(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    task: Any,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
) -> VimTaskResult:
    """Poll a vim ``*_Task`` MoRef's ``Task.info`` to a terminal state.

    Reads ``Task.info`` through :meth:`VmwareRestConnector._post_vmomi_json`
    (mounted on the documented VI-JSON base ``/sdk/vim25/{release}``) on a
    ``poll_interval`` cadence until ``TaskInfo.state`` is ``success`` /
    ``error`` or the ``timeout_seconds`` wall-clock deadline elapses.
    Returns the :class:`VimTaskResult` outcome -- it never raises on a task
    *fault* (that is a legible outcome each caller maps to its own status),
    only propagates a transport :exc:`httpx.HTTPError` from the read.

    ``task`` may be the vim MoRef the ``*_Task`` method returned
    (``{"type": "Task", "value": "task-42"}``) or a bare moid string. A
    MoRef the helper cannot resolve to a moid raises :exc:`ValueError` --
    the caller passed a shape that is not a Task reference.

    Parameters
    ----------
    task:
        The Task MoRef (or moid) to poll.
    timeout_seconds:
        Wall-clock bound. On elapse the result's ``state`` is ``"timeout"``
        and the task may still complete in the background. Defaults to the
        600 s ``vm.clone`` convention.
    poll_interval:
        Seconds between ``Task.info`` reads while the task is non-terminal.
        Tests pass ``0.0`` for an instant loop.
    """
    task_moid = task_moref_value(task)
    if task_moid is None:
        raise ValueError(
            f"poll_vim_task: expected a Task MoRef or moid, got {type(task).__name__}: {task!r}"
        )
    retrieve_body = build_task_info_retrieve_params([task_moid])
    deadline = time.monotonic() + timeout_seconds
    while True:
        retrieve_result = await connector._post_vmomi_json(
            target,
            _RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=retrieve_body,
        )
        info = _extract_task_info(retrieve_result, task_moid)
        state = info.get("state") if isinstance(info, dict) else None
        if info is not None and state in _TERMINAL_STATES:
            succeeded = state == TASK_STATE_SUCCESS
            _log.info(
                "vim_task_terminal",
                target=getattr(target, "name", None),
                task=task_moid,
                state=state,
            )
            return VimTaskResult(
                task=task_moid,
                state=state,
                result=info.get("result") if succeeded else None,
                error_message=None if succeeded else _fault_message(info),
                progress=_progress(info),
            )
        if time.monotonic() >= deadline:
            _log.warning(
                "vim_task_poll_timeout",
                target=getattr(target, "name", None),
                task=task_moid,
                timeout_seconds=timeout_seconds,
            )
            return VimTaskResult(
                task=task_moid,
                state=_OUTCOME_TIMEOUT,
                progress=_progress(info) if isinstance(info, dict) else None,
            )
        await asyncio.sleep(poll_interval)
