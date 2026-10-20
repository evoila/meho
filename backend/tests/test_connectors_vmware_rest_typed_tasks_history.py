# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vmware.tasks.history`` typed op (#3701).

The op creates a ``TaskHistoryCollector``, pages it with
``ReadNextTasks`` and destroys it before it returns. The handler tests use
a fake connector that records every vmomi POST, so they check the call
order (create -> read -> destroy), that the destroy also runs on the error
path, the destroy retries and the session-close fallback, and the row
parse. Transport tests send the calls through the real connector over
respx to pin the wire paths and bodies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_tasks_history import (
    _DESTROY_ATTEMPTS,
    _MAX_PAGES,
    _PAGE_SIZE,
    VMWARE_TASKS_HISTORY_OP,
    CollectorCleanupError,
    build_task_filter_spec,
    tasks_history_impl,
)
from meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent import tasks_recent_impl
from meho_backplane.operations._validate import validate_params

_COLLECTOR = "session[52a0-1]52b0-2"
_CREATE_PATH = "/TaskManager/TaskManager/CreateCollectorForTasks"
_READ_PATH = f"/TaskHistoryCollector/{_COLLECTOR}/ReadNextTasks"
_DESTROY_PATH = f"/TaskHistoryCollector/{_COLLECTOR}/DestroyCollector"
_CLOSE_SESSION = "close_session"


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meho_backplane.connectors.vmware_rest.typed_ops_tasks_history._DESTROY_RETRY_DELAY_SECONDS",
        0,
    )


def _not_found_fault() -> httpx.HTTPStatusError:
    """A VI-JSON ``ManagedObjectNotFound`` fault, as vCenter reports it (HTTP 500)."""
    request = httpx.Request("POST", "https://vc.test.invalid/sdk/vim25/8.0.3.0" + _DESTROY_PATH)
    response = httpx.Response(
        500, json={"_typeName": "ManagedObjectNotFound", "obj": {}}, request=request
    )
    return httpx.HTTPStatusError("500", request=request, response=response)


def _make_operator() -> Operator:
    return Operator(
        sub="op-tasks-history",
        name="Tasks History Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-test"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


_COLLECTOR_REF = {
    "_typeName": "ManagedObjectReference",
    "type": "TaskHistoryCollector",
    "value": _COLLECTOR,
}


def _task_info(n: int, **extra: Any) -> dict[str, Any]:
    info = {
        "_typeName": "TaskInfo",
        "key": f"task-{n}",
        "task": {"_typeName": "ManagedObjectReference", "type": "Task", "value": f"task-{n}"},
        "descriptionId": "VirtualMachine.clone",
        "entity": {"type": "VirtualMachine", "value": f"vm-{n}"},
        "entityName": f"vm-{n}",
        "state": "success",
        "progress": 100,
        "queueTime": "2026-10-20T08:00:00Z",
    }
    info.update(extra)
    return info


_IMPORT_FAILED = _task_info(
    7,
    descriptionId="ResourcePool.ImportVApp",
    entity={"type": "ResourcePool", "value": "resgroup-8"},
    entityName="Resources",
    state="error",
    progress=None,
    error={"_typeName": "LocalizedMethodFault", "localizedMessage": "Disk upload failed."},
)


class _FakeConnector:
    """Answers vmomi POSTs by path; records the call order."""

    def __init__(
        self,
        *,
        pages: list[Any],
        create_result: Any = _COLLECTOR_REF,
        read_error: Exception | None = None,
        destroy_errors: list[Exception] | None = None,
        close_session_result: bool = True,
        recent: Any = None,
    ) -> None:
        self._pages = list(pages)
        self._create_result = create_result
        self._read_error = read_error
        # One error per destroy call, in order; a call past the list succeeds.
        self._destroy_errors = list(destroy_errors or [])
        self._close_session_result = close_session_result
        self._recent = recent
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> Any:
        del target, operator
        self.calls.append((path, json))
        if path == _CREATE_PATH:
            return self._create_result
        if path == _READ_PATH:
            if self._read_error is not None:
                raise self._read_error
            return self._pages.pop(0) if self._pages else None
        if path == _DESTROY_PATH:
            if self._destroy_errors:
                raise self._destroy_errors.pop(0)
            return {}
        if path.endswith("RetrievePropertiesEx"):
            return self._recent
        raise AssertionError(f"unexpected path {path}")

    async def close_session(self, target: Any) -> bool:
        del target
        self.calls.append((_CLOSE_SESSION, None))
        return self._close_session_result

    @property
    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]


async def _run(conn: _FakeConnector, params: dict[str, Any]) -> dict[str, Any]:
    return await tasks_history_impl(conn, _make_operator(), _Target(), params)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Filter spec
# ---------------------------------------------------------------------------


def test_filter_spec_time_window_on_queued_time() -> None:
    spec = build_task_filter_spec(
        {"since": "2026-10-20T08:00:00Z", "until": "2026-10-20T10:00:00Z"}
    )
    assert spec == {
        "_typeName": "TaskFilterSpec",
        "time": {
            "_typeName": "TaskFilterSpecByTime",
            "timeType": "queuedTime",
            "beginTime": "2026-10-20T08:00:00Z",
            "endTime": "2026-10-20T10:00:00Z",
        },
    }


def test_filter_spec_converts_offsets_and_naive_times_to_utc() -> None:
    spec = build_task_filter_spec(
        {"since": "2026-10-20T08:00:00", "until": "2026-10-20T12:30:00+02:00"}
    )
    assert spec["time"]["beginTime"] == "2026-10-20T08:00:00Z"
    assert spec["time"]["endTime"] == "2026-10-20T10:30:00Z"
    assert "entity" not in spec


def test_filter_spec_without_until_has_no_end_time() -> None:
    spec = build_task_filter_spec({"since": "2026-10-20T08:00:00Z"})
    assert "endTime" not in spec["time"]


def test_filter_spec_entity_self_and_children() -> None:
    base = {"since": "2026-10-20T08:00:00Z", "entity": "resgroup-8", "entity_type": "ResourcePool"}
    spec = build_task_filter_spec(base)
    assert spec["entity"] == {
        "_typeName": "TaskFilterSpecByEntity",
        "entity": {
            "_typeName": "ManagedObjectReference",
            "type": "ResourcePool",
            "value": "resgroup-8",
        },
        "recursion": "self",
    }
    spec = build_task_filter_spec({**base, "include_children": True})
    assert spec["entity"]["recursion"] == "all"


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"since": "yesterday"}, "not an ISO-8601"),
        ({}, "must be an ISO-8601"),
        ({"since": "2026-10-20T10:00:00Z", "until": "2026-10-20T09:00:00Z"}, "later than"),
        ({"since": "2026-10-20T10:00:00Z", "entity": "vm-1"}, "needs 'entity_type'"),
    ],
)
def test_filter_spec_rejects_bad_params(params: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_task_filter_spec(params)


# ---------------------------------------------------------------------------
# Handler: create -> read -> destroy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_read_destroy_in_order() -> None:
    conn = _FakeConnector(pages=[[_task_info(1), _task_info(2)]])
    out = await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths == [_CREATE_PATH, _READ_PATH, _DESTROY_PATH]
    assert conn.calls[0][1] == {"filter": build_task_filter_spec({"since": "2026-10-20T08:00:00Z"})}
    assert conn.calls[1][1] == {"maxCount": _PAGE_SIZE}
    assert conn.calls[2][1] is None
    assert [t["task"] for t in out["tasks"]] == ["task-1", "task-2"]
    assert out["truncated"] is False
    assert out["scanned"] == 2
    assert out["collector_cleanup"] == "destroyed"


@pytest.mark.asyncio
async def test_pages_until_a_short_page() -> None:
    full = [_task_info(n) for n in range(_PAGE_SIZE)]
    conn = _FakeConnector(pages=[full, [_task_info(500)]])
    out = await _run(conn, {"since": "2026-10-20T08:00:00Z", "max_tasks": 1000})
    assert conn.paths == [_CREATE_PATH, _READ_PATH, _READ_PATH, _DESTROY_PATH]
    assert len(out["tasks"]) == _PAGE_SIZE + 1
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_null_page_means_no_tasks() -> None:
    conn = _FakeConnector(pages=[None])
    out = await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert out["tasks"] == []
    assert out["truncated"] is False
    assert conn.paths[-1] == _DESTROY_PATH


@pytest.mark.asyncio
async def test_max_tasks_caps_rows_and_sets_truncated() -> None:
    conn = _FakeConnector(pages=[[_task_info(n) for n in range(5)]])
    out = await _run(conn, {"since": "2026-10-20T08:00:00Z", "max_tasks": 3})
    assert [t["task"] for t in out["tasks"]] == ["task-0", "task-1", "task-2"]
    assert out["truncated"] is True
    assert conn.paths[-1] == _DESTROY_PATH


@pytest.mark.asyncio
async def test_page_budget_stops_the_scan() -> None:
    full = [_task_info(n) for n in range(_PAGE_SIZE)]
    conn = _FakeConnector(pages=[full] * (_MAX_PAGES + 5))
    out = await _run(
        conn,
        {"since": "2026-10-20T08:00:00Z", "description_ids": ["ResourcePool.ImportVApp"]},
    )
    assert conn.paths.count(_READ_PATH) == _MAX_PAGES
    assert out["tasks"] == []
    assert out["scanned"] == _MAX_PAGES * _PAGE_SIZE
    assert out["truncated"] is True
    assert conn.paths[-1] == _DESTROY_PATH


@pytest.mark.asyncio
async def test_description_ids_filter_rows_after_the_read() -> None:
    conn = _FakeConnector(pages=[[_task_info(1), _IMPORT_FAILED, _task_info(2)]])
    out = await _run(
        conn,
        {"since": "2026-10-20T08:00:00Z", "description_ids": ["ResourcePool.ImportVApp"]},
    )
    # vCenter's TaskFilterSpec has no operation filter, so none is sent.
    assert "descriptionId" not in json.dumps(conn.calls[0][1])
    (row,) = out["tasks"]
    assert row["operation"] == "ResourcePool.ImportVApp"
    assert out["scanned"] == 3


@pytest.mark.asyncio
async def test_destroy_runs_when_the_read_fails() -> None:
    conn = _FakeConnector(pages=[], read_error=RuntimeError("read failed"))
    with pytest.raises(RuntimeError, match="read failed"):
        await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths == [_CREATE_PATH, _READ_PATH, _DESTROY_PATH]


@pytest.mark.asyncio
async def test_destroy_is_retried_once_after_a_transient_failure() -> None:
    conn = _FakeConnector(pages=[[_task_info(1)]], destroy_errors=[RuntimeError("flaky")])
    out = await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths == [_CREATE_PATH, _READ_PATH, _DESTROY_PATH, _DESTROY_PATH]
    assert out["collector_cleanup"] == "destroyed"


@pytest.mark.asyncio
async def test_destroy_of_an_already_gone_collector_counts_as_destroyed() -> None:
    conn = _FakeConnector(pages=[[_task_info(1)]], destroy_errors=[_not_found_fault()])
    out = await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths.count(_DESTROY_PATH) == 1
    assert _CLOSE_SESSION not in conn.paths
    assert out["collector_cleanup"] == "destroyed"


@pytest.mark.asyncio
async def test_destroy_failures_close_the_session_that_owns_the_collector() -> None:
    """Every destroy try fails: MEHO closes the session, so vCenter drops the collector."""
    conn = _FakeConnector(
        pages=[[_task_info(1)]],
        destroy_errors=[RuntimeError("destroy failed")] * _DESTROY_ATTEMPTS,
    )
    out = await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths.count(_DESTROY_PATH) == _DESTROY_ATTEMPTS
    assert conn.paths[-1] == _CLOSE_SESSION
    assert len(out["tasks"]) == 1
    assert out["collector_cleanup"] == "session_closed"


@pytest.mark.asyncio
async def test_unconfirmed_cleanup_fails_the_call() -> None:
    """Destroy keeps failing and the session close fails too: the call reports an error."""
    conn = _FakeConnector(
        pages=[[_task_info(1)]],
        destroy_errors=[RuntimeError("destroy failed")] * _DESTROY_ATTEMPTS,
        close_session_result=False,
    )
    with pytest.raises(CollectorCleanupError, match="could not be removed"):
        await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths.count(_DESTROY_PATH) == _DESTROY_ATTEMPTS
    assert conn.paths[-1] == _CLOSE_SESSION


@pytest.mark.asyncio
async def test_failed_destroy_after_a_read_error_closes_the_session() -> None:
    """The read error is the one raised; the collector is still removed with the session."""
    conn = _FakeConnector(
        pages=[],
        read_error=RuntimeError("read failed"),
        destroy_errors=[RuntimeError("destroy failed")] * _DESTROY_ATTEMPTS,
    )
    with pytest.raises(RuntimeError, match="read failed"):
        await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths.count(_DESTROY_PATH) == _DESTROY_ATTEMPTS
    assert conn.paths[-1] == _CLOSE_SESSION


@pytest.mark.asyncio
async def test_read_error_wins_over_an_unconfirmed_cleanup() -> None:
    conn = _FakeConnector(
        pages=[],
        read_error=RuntimeError("read failed"),
        destroy_errors=[RuntimeError("destroy failed")] * _DESTROY_ATTEMPTS,
        close_session_result=False,
    )
    with pytest.raises(RuntimeError, match="read failed"):
        await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths[-1] == _CLOSE_SESSION


@pytest.mark.asyncio
async def test_bad_params_create_no_collector() -> None:
    conn = _FakeConnector(pages=[])
    with pytest.raises(ValueError):
        await _run(conn, {"since": "not-a-time"})
    assert conn.calls == []


@pytest.mark.asyncio
async def test_missing_collector_reference_raises_without_destroy() -> None:
    conn = _FakeConnector(pages=[], create_result={})
    with pytest.raises(RuntimeError, match="no collector reference"):
        await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths == [_CREATE_PATH]


@pytest.mark.asyncio
async def test_finds_a_failed_task_that_left_the_recent_list() -> None:
    """The RCA case: recentTask no longer lists the task, history still has it."""
    empty_recent = {
        "objects": [
            {
                "obj": {"type": "TaskManager", "value": "TaskManager"},
                "propSet": [{"name": "recentTask", "val": []}],
            }
        ]
    }
    conn = _FakeConnector(pages=[[_IMPORT_FAILED]], recent=empty_recent)
    recent = await tasks_recent_impl(conn, _make_operator(), _Target(), {})  # type: ignore[arg-type]
    assert recent == {"tasks": []}

    out = await _run(
        conn,
        {
            "since": "2026-10-20T07:00:00Z",
            "until": "2026-10-20T09:00:00Z",
            "entity": "resgroup-8",
            "entity_type": "ResourcePool",
        },
    )
    (row,) = out["tasks"]
    assert row == {
        "task": "task-7",
        "operation": "ResourcePool.ImportVApp",
        "entity": "resgroup-8",
        "entity_type": "ResourcePool",
        "entity_name": "Resources",
        "state": "error",
        "progress": None,
        "cancelled": None,
        "queue_time": "2026-10-20T08:00:00Z",
        "start_time": None,
        "complete_time": None,
        "error_message": "Disk upload failed.",
    }
    assert out["collector_cleanup"] == "destroyed"


# ---------------------------------------------------------------------------
# Transport: real connector over respx
# ---------------------------------------------------------------------------


async def _stub_loader(_target: Any, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> VmwareRestConnector:
    """A real connector whose teardown skips the session-revoke call."""
    connector = VmwareRestConnector(session_loader=_stub_loader)

    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]
    return connector


@pytest.mark.asyncio
async def test_wire_paths_and_bodies_on_sdk_vim25() -> None:
    connector = _make_connector()
    base = "/sdk/vim25/8.0.3.0"
    try:
        async with respx.mock(base_url="https://vc.test.invalid") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            create = mock.post(f"{base}{_CREATE_PATH}").respond(200, json=_COLLECTOR_REF)
            read = mock.post(f"{base}{_READ_PATH}").respond(200, json=[_IMPORT_FAILED])
            destroy = mock.post(f"{base}{_DESTROY_PATH}").respond(204)
            out = await connector.tasks_history(
                _make_operator(), _Target(), {"since": "2026-10-20T08:00:00Z"}
            )
            create_body = json.loads(create.calls.last.request.content)
            read_body = json.loads(read.calls.last.request.content)
        assert create_body["filter"]["time"]["timeType"] == "queuedTime"
        assert read_body == {"maxCount": _PAGE_SIZE}
        assert destroy.call_count == 1
        assert out["tasks"][0]["operation"] == "ResourcePool.ImportVApp"
        assert out["collector_cleanup"] == "destroyed"
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_wire_destroy_runs_after_a_read_fault() -> None:
    connector = _make_connector()
    base = "/sdk/vim25/8.0.3.0"
    try:
        async with respx.mock(base_url="https://vc.test.invalid") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post(f"{base}{_CREATE_PATH}").respond(200, json=_COLLECTOR_REF)
            mock.post(f"{base}{_READ_PATH}").respond(
                500, json={"_typeName": "SystemError", "reason": "boom"}
            )
            destroy = mock.post(f"{base}{_DESTROY_PATH}").respond(204)
            with pytest.raises(httpx.HTTPStatusError):
                await connector.tasks_history(
                    _make_operator(), _Target(), {"since": "2026-10-20T08:00:00Z"}
                )
        assert destroy.call_count == 1
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_wire_destroy_faults_end_the_session_on_vcenter() -> None:
    """Destroy faults every time: DELETE /api/session runs with the session token.

    The cached session is gone afterwards, so the next call logs in again
    on a clean session with a fresh collector budget.
    """
    connector = _make_connector()
    base = "/sdk/vim25/8.0.3.0"
    try:
        async with respx.mock(base_url="https://vc.test.invalid") as mock:
            login = mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post(f"{base}{_CREATE_PATH}").respond(200, json=_COLLECTOR_REF)
            mock.post(f"{base}{_READ_PATH}").respond(200, json=[_IMPORT_FAILED])
            destroy = mock.post(f"{base}{_DESTROY_PATH}").respond(
                500, json={"_typeName": "SystemError", "reason": "boom"}
            )
            revoke = mock.delete("/api/session").respond(204)
            out = await connector.tasks_history(
                _make_operator(), _Target(), {"since": "2026-10-20T08:00:00Z"}
            )
            assert destroy.call_count == _DESTROY_ATTEMPTS
            assert revoke.call_count == 1
            assert revoke.calls[0].request.headers["vmware-api-session-id"] == "tok"
            assert out["collector_cleanup"] == "session_closed"
            assert connector._session_tokens == {}

            await connector.tasks_history(
                _make_operator(), _Target(), {"since": "2026-10-20T08:00:00Z"}
            )
        assert login.call_count == 2
    finally:
        await connector.aclose()


# ---------------------------------------------------------------------------
# Schema + registration
# ---------------------------------------------------------------------------


def test_schema_examples_validate() -> None:
    schema = VMWARE_TASKS_HISTORY_OP.parameter_schema
    for example in schema["examples"]:
        assert validate_params(schema, example) == []


def test_schema_rejects_missing_since_and_entity_without_type() -> None:
    schema = VMWARE_TASKS_HISTORY_OP.parameter_schema
    assert validate_params(schema, {}) != []
    assert validate_params(schema, {"since": "2026-10-20T08:00:00Z", "entity": "vm-1"}) != []
    assert validate_params(schema, {"since": "2026-10-20T08:00:00Z", "max_tasks": 5000}) != []


def test_tasks_history_is_a_registered_safe_typed_op() -> None:
    assert VMWARE_TASKS_HISTORY_OP in VMWARE_TYPED_OPS
    assert VMWARE_TASKS_HISTORY_OP.op_id == "vmware.tasks.history"
    assert VMWARE_TASKS_HISTORY_OP.safety_level == "safe"
    assert VMWARE_TASKS_HISTORY_OP.requires_approval is False
    handler = getattr(VmwareRestConnector, VMWARE_TASKS_HISTORY_OP.handler_attr, None)
    assert callable(handler)
    group_key = VMWARE_TASKS_HISTORY_OP.group_key
    assert group_key is not None
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[group_key].strip()
