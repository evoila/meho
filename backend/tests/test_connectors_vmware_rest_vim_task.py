# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the reusable vim ``*_Task`` poll-to-terminal helper (#2893).

:func:`~meho_backplane.connectors.vmware_rest.vim_task.poll_vim_task` is
the seam every mutating VI-JSON write composite shares to drive a returned
``*_Task`` MoRef (``ReconfigVM_Task``, ``CloneVM_Task``,
``ReconfigureComputeResource_Task``) to a terminal state. These tests pin
its contract against canned ``Task.info`` sequences:

* **success** -- the terminal ``success`` state surfaces ``TaskInfo.result``
  (the new-object MoRef a clone produces);
* **error** -- the terminal ``error`` state surfaces the localized fault;
* **timeout** -- the wall-clock deadline elapses before a terminal state;
* the poll loops across ``queued`` / ``running`` reads until terminal;
* MoRef / bare-moid input handling + the ``ValueError`` on a non-Task shape;
* a transport fault on the ``Task.info`` read propagates (not swallowed).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from meho_backplane.connectors.vmware_rest.vim_task import (
    TASK_STATE_ERROR,
    TASK_STATE_SUCCESS,
    poll_vim_task,
    task_moref_value,
)

_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"


def _task_info_result(
    task_moid: str,
    *,
    state: Any,
    result: Any = None,
    error_message: Any = None,
    error: Any = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Build a single-Task ``RetrievePropertiesEx`` result carrying one ``TaskInfo``.

    Fields are ``Any``-typed so tests can seed either the bare primitives
    or the boxed ``{"_typeName": ..., "_value": ...}`` forms live VI-JSON
    puts in ``Any`` placeholders (#3106). ``error_message`` shorthands a
    fault that carries only ``localizedMessage``; ``error`` seeds the full
    ``LocalizedMethodFault`` object for the fallback-chain tests (#3116).
    """
    info: dict[str, Any] = {"state": state}
    if result is not None:
        info["result"] = result
    if error_message is not None:
        info["error"] = {"localizedMessage": error_message}
    if error is not None:
        info["error"] = error
    if progress is not None:
        info["progress"] = progress
    return {
        "objects": [
            {
                "obj": {"type": "Task", "value": task_moid},
                "propSet": [{"name": "info", "val": info}],
            }
        ]
    }


class _SeqTaskConnector:
    """Stub connector serving a fixed sequence of ``Task.info`` reads.

    Records every ``_post_vmomi_json`` call; returns the queued responses in
    order and repeats the last one once the queue is exhausted (so an
    always-``running`` sequence drives the timeout branch). A response that
    is an :class:`Exception` is raised -- exercising the transport-fault
    propagation path.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self._last: Any = None
        self.calls: list[tuple[str, Any]] = []

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Any, json: Any = None
    ) -> Any:
        self.calls.append((path, json))
        response = self._responses.pop(0) if self._responses else self._last
        self._last = response
        if isinstance(response, Exception):
            raise response
        return response


# ---------------------------------------------------------------------------
# MoRef extraction
# ---------------------------------------------------------------------------


def test_task_moref_value_handles_moref_dict_bare_moid_and_junk() -> None:
    assert task_moref_value({"type": "Task", "value": "task-42"}) == "task-42"
    assert task_moref_value({"_typeName": "ManagedObjectReference", "value": "task-7"}) == "task-7"
    assert task_moref_value("task-1") == "task-1"
    assert task_moref_value({"type": "Task"}) is None
    assert task_moref_value(123) is None
    assert task_moref_value(None) is None


# ---------------------------------------------------------------------------
# Terminal outcomes
# ---------------------------------------------------------------------------


async def test_poll_returns_success_with_task_result() -> None:
    """A terminal ``success`` surfaces ``TaskInfo.result`` (e.g. a clone's new VM)."""
    conn = _SeqTaskConnector(
        [
            _task_info_result(
                "task-1", state="success", result={"type": "VirtualMachine", "value": "vm-9"}
            )
        ]
    )
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task={"type": "Task", "value": "task-1"},
        poll_interval=0.0,
    )
    assert outcome.state == TASK_STATE_SUCCESS
    assert outcome.succeeded is True
    assert outcome.timed_out is False
    assert outcome.result == {"type": "VirtualMachine", "value": "vm-9"}
    assert outcome.error_message is None
    # One read on the RetrievePropertiesEx path: the task was already terminal.
    assert len(conn.calls) == 1
    assert conn.calls[0][0] == _RETRIEVE_PROPERTIES_PATH


async def test_poll_loops_through_running_until_success() -> None:
    """Non-terminal ``queued`` / ``running`` reads are polled through to terminal."""
    conn = _SeqTaskConnector(
        [
            _task_info_result("task-1", state="queued"),
            _task_info_result("task-1", state="running", progress=40),
            _task_info_result("task-1", state="success"),
        ]
    )
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task="task-1",
        poll_interval=0.0,
    )
    assert outcome.succeeded is True
    assert len(conn.calls) == 3


async def test_poll_returns_error_with_localized_message() -> None:
    """A terminal ``error`` surfaces the localized fault message, does not raise."""
    conn = _SeqTaskConnector(
        [_task_info_result("task-1", state="error", error_message="The disk cannot be shrunk.")]
    )
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task="task-1",
        poll_interval=0.0,
    )
    assert outcome.state == TASK_STATE_ERROR
    assert outcome.succeeded is False
    assert outcome.error_message == "The disk cannot be shrunk."
    assert outcome.result is None


async def test_poll_reaches_terminal_states_on_boxed_task_info_fields() -> None:
    """Boxed ``TaskInfo`` primitives (#3106) unbox: wrapped ``info.state`` terminates the poll.

    VI-JSON boxes primitives in ``Any`` placeholders (live 8.0.3, #3106);
    the tolerant unwrap must normalise a boxed ``state`` / ``progress`` /
    ``result`` so the poll recognises terminal states instead of spinning
    to timeout, while an un-boxed MoRef ``result`` passes through intact.
    """
    conn = _SeqTaskConnector(
        [
            _task_info_result(
                "task-1",
                state={"_typeName": "string", "_value": "running"},
                progress={"_typeName": "int", "_value": 40},
            ),
            _task_info_result(
                "task-1",
                state={"_typeName": "string", "_value": "success"},
                result={
                    "_typeName": "ManagedObjectReference",
                    "type": "VirtualMachine",
                    "value": "vm-9",
                },
            ),
        ]
    )
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task="task-1",
        poll_interval=0.0,
    )
    assert outcome.state == TASK_STATE_SUCCESS
    assert outcome.succeeded is True
    assert outcome.result == {
        "_typeName": "ManagedObjectReference",
        "type": "VirtualMachine",
        "value": "vm-9",
    }
    assert len(conn.calls) == 2


async def test_poll_surfaces_fault_from_boxed_error_state_and_message() -> None:
    """A boxed ``error`` state + boxed ``localizedMessage`` surface the fault (#3106)."""
    conn = _SeqTaskConnector(
        [
            _task_info_result(
                "task-1",
                state={"_typeName": "string", "_value": "error"},
                error_message={"_typeName": "string", "_value": "Boxed boom."},
            )
        ]
    )
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task="task-1",
        poll_interval=0.0,
    )
    assert outcome.state == TASK_STATE_ERROR
    assert outcome.error_message == "Boxed boom."
    assert outcome.result is None


async def test_poll_fault_without_localized_message_joins_fault_message_texts() -> None:
    """No ``localizedMessage``: the joined ``fault.faultMessage[*].message`` surfaces (#3116)."""
    conn = _SeqTaskConnector(
        [
            _task_info_result(
                "task-1",
                state="error",
                error={
                    "_typeName": "LocalizedMethodFault",
                    "fault": {
                        "_typeName": "InvalidArgument",
                        "faultMessage": [
                            {
                                "_typeName": "LocalizableMessage",
                                "key": "com.vmware.wcp.arg.invalid",
                                "message": "The argument is invalid.",
                            },
                            {
                                "_typeName": "LocalizableMessage",
                                "key": "com.vmware.wcp.arg.detail",
                                "message": "Entity crosses datacenter boundary.",
                            },
                        ],
                    },
                },
            )
        ]
    )
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task="task-1",
        poll_interval=0.0,
    )
    assert outcome.state == TASK_STATE_ERROR
    assert outcome.error_message == "The argument is invalid.; Entity crosses datacenter boundary."


async def test_poll_fault_without_any_message_falls_back_to_fault_type_name() -> None:
    """No ``localizedMessage`` and no message texts: ``fault._typeName`` surfaces (#3116)."""
    conn = _SeqTaskConnector(
        [
            _task_info_result(
                "task-1",
                state="error",
                error={
                    "_typeName": "LocalizedMethodFault",
                    "fault": {
                        "_typeName": "InvalidArgument",
                        "faultMessage": [
                            {"_typeName": "LocalizableMessage", "key": "com.vmware.no.text"}
                        ],
                    },
                },
            )
        ]
    )
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task="task-1",
        poll_interval=0.0,
    )
    assert outcome.state == TASK_STATE_ERROR
    assert outcome.error_message == "InvalidArgument"


async def test_poll_fault_with_boxed_nested_primitives_surfaces_fallback_chain() -> None:
    """Fully boxed fault content (#3106 live-8.0.3 shape) still surfaces a message (#3116).

    Every nested primitive rides a ``{"_typeName": ..., "_value": ...}``
    box and the ``faultMessage`` array rides its ``ArrayOfLocalizableMessage``
    box; the recursive unwrap at ``Task.info`` extraction plus the fallback
    chain must compose to the joined message texts -- never
    ``<no fault reported>``.
    """
    conn = _SeqTaskConnector(
        [
            _task_info_result(
                "task-1",
                state={"_typeName": "string", "_value": "error"},
                error={
                    "_typeName": "LocalizedMethodFault",
                    "fault": {
                        "_typeName": "InvalidArgument",
                        "faultMessage": {
                            "_typeName": "ArrayOfLocalizableMessage",
                            "_value": [
                                {
                                    "_typeName": "LocalizableMessage",
                                    "key": {
                                        "_typeName": "string",
                                        "_value": "com.vmware.arg.invalid",
                                    },
                                    "message": {
                                        "_typeName": "string",
                                        "_value": "The argument is invalid.",
                                    },
                                }
                            ],
                        },
                    },
                },
            )
        ]
    )
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task="task-1",
        poll_interval=0.0,
    )
    assert outcome.state == TASK_STATE_ERROR
    assert outcome.error_message == "The argument is invalid."


async def test_poll_fault_blank_localized_message_falls_through_to_fault_body() -> None:
    """A whitespace-only ``localizedMessage`` is absent, not a fault description (#3116)."""
    conn = _SeqTaskConnector(
        [
            _task_info_result(
                "task-1",
                state="error",
                error={
                    "_typeName": "LocalizedMethodFault",
                    "localizedMessage": "   ",
                    "fault": {"_typeName": "TaskInProgress"},
                },
            )
        ]
    )
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task="task-1",
        poll_interval=0.0,
    )
    assert outcome.state == TASK_STATE_ERROR
    assert outcome.error_message == "TaskInProgress"


async def test_poll_fault_with_no_usable_content_reports_none() -> None:
    """A faulted TaskInfo with no fault content at all keeps ``error_message=None``."""
    conn = _SeqTaskConnector(
        [_task_info_result("task-1", state="error", error={"fault": "not-a-dict"})]
    )
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task="task-1",
        poll_interval=0.0,
    )
    assert outcome.state == TASK_STATE_ERROR
    assert outcome.error_message is None


async def test_poll_times_out_when_never_terminal() -> None:
    """A zero deadline that observes a non-terminal state returns a ``timeout`` outcome."""
    conn = _SeqTaskConnector([_task_info_result("task-1", state="running", progress=25)])
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task="task-1",
        timeout_seconds=0.0,
        poll_interval=0.0,
    )
    assert outcome.timed_out is True
    assert outcome.state == "timeout"
    assert outcome.progress == 25
    # Exactly one read, then the deadline check fired — no sleep/loop.
    assert len(conn.calls) == 1


async def test_poll_treats_missing_info_as_not_terminal_then_times_out() -> None:
    """An absent/empty ``TaskInfo`` (not populated yet) is 'not terminal', not a crash."""
    conn = _SeqTaskConnector([{"objects": []}])
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task="task-1",
        timeout_seconds=0.0,
        poll_interval=0.0,
    )
    assert outcome.timed_out is True
    assert outcome.progress is None


# ---------------------------------------------------------------------------
# Input + fault handling
# ---------------------------------------------------------------------------


async def test_poll_rejects_a_non_task_shape() -> None:
    """A value that is neither a MoRef nor a moid string raises ValueError."""
    conn = _SeqTaskConnector([])
    with pytest.raises(ValueError, match="Task MoRef or moid"):
        await poll_vim_task(
            conn,  # type: ignore[arg-type]
            object(),
            object(),  # type: ignore[arg-type]
            task=12345,
        )
    # The read was never issued — the shape was rejected up front.
    assert conn.calls == []


async def test_poll_propagates_a_transport_fault_on_the_info_read() -> None:
    """An httpx error on the Task.info read propagates — it is not 'still running'."""
    request = httpx.Request("POST", "https://vc.test" + _RETRIEVE_PROPERTIES_PATH)
    conn = _SeqTaskConnector(
        [
            httpx.HTTPStatusError(
                "boom", request=request, response=httpx.Response(500, request=request)
            )
        ]
    )
    with pytest.raises(httpx.HTTPStatusError):
        await poll_vim_task(
            conn,  # type: ignore[arg-type]
            object(),
            object(),  # type: ignore[arg-type]
            task="task-1",
            poll_interval=0.0,
        )


async def test_poll_matches_the_requested_task_among_multiple_objects() -> None:
    """The extractor keys on the Task moid when the result carries several objects."""
    conn = _SeqTaskConnector(
        [
            {
                "objects": [
                    {
                        "obj": {"type": "Task", "value": "task-other"},
                        "propSet": [{"name": "info", "val": {"state": "running"}}],
                    },
                    {
                        "obj": {"type": "Task", "value": "task-1"},
                        "propSet": [{"name": "info", "val": {"state": "success"}}],
                    },
                ]
            }
        ]
    )
    outcome = await poll_vim_task(
        conn,  # type: ignore[arg-type]
        object(),
        object(),  # type: ignore[arg-type]
        task="task-1",
        poll_interval=0.0,
    )
    assert outcome.succeeded is True
