# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vmware.tasks.history`` typed op (#3701).

The op creates a ``TaskHistoryCollector``, pages it with
``ReadNextTasks`` and destroys it before it returns. The handler tests use
a fake connector that records every vmomi POST and the session token it
was sent with, so they check the call order (create -> read -> destroy),
that the read and the destroy stay on the session that created the
collector, that the destroy also runs on the error path and on a cancel
(also a cancel while the create request is pending), the destroy retries
and the one cleanup rule (the session is closed unless the owning session
confirmed the collector gone, also after a create whose answer was lost),
and the row parse.
Transport tests send the calls through the real connector over respx to
pin the wire paths, bodies and session headers.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.session import SessionTokenUsed
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
#: The session token the fake connector sends the create request with.
_OWNER_TOKEN = "tok-owner"


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meho_backplane.connectors.vmware_rest.typed_ops_tasks_history._DESTROY_RETRY_DELAY_SECONDS",
        0,
    )


def _vim_fault(type_name: str) -> httpx.HTTPStatusError:
    """A VI-JSON method fault, as vCenter reports it (HTTP 500, the fault as the body)."""
    request = httpx.Request("POST", "https://vc.test.invalid/sdk/vim25/8.0.3.0" + _DESTROY_PATH)
    response = httpx.Response(500, json={"_typeName": type_name}, request=request)
    return httpx.HTTPStatusError("500", request=request, response=response)


def _not_found_fault() -> httpx.HTTPStatusError:
    return _vim_fault("ManagedObjectNotFound")


def _not_authenticated_fault() -> httpx.HTTPStatusError:
    return _vim_fault("NotAuthenticated")


def _lost_answer() -> httpx.ReadTimeout:
    """The request went out, the answer never came back."""
    return httpx.ReadTimeout("read timed out")


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
        create_error: Exception | None = None,
        read_error: Exception | None = None,
        destroy_errors: list[Exception] | None = None,
        close_session_result: bool = True,
        recent: Any = None,
    ) -> None:
        self._pages = list(pages)
        self._create_result = create_result
        self._create_error = create_error
        self._read_error = read_error
        # One error per destroy call, in order; a call past the list succeeds.
        self._destroy_errors = list(destroy_errors or [])
        self._close_session_result = close_session_result
        self._recent = recent
        # The token a vmomi call is "sent" with; a test may change it while
        # a held call is pending, like a re-login by another request.
        self.session_token: str | None = _OWNER_TOKEN
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        # The token each vmomi call went out with: the pinned one, else the
        # cached one at the time of the call.
        self.tokens: list[tuple[str, str | None]] = []
        # Set when a test wants a call on that path to block until released.
        self.hold: dict[str, asyncio.Event] = {}
        self.entered: dict[str, asyncio.Event] = {}
        self.finished: list[str] = []

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
        session_used: SessionTokenUsed | None = None,
        session_token: str | None = None,
    ) -> Any:
        del target, operator
        self.calls.append((path, json))
        sent_with = session_token if session_token is not None else self.session_token
        self.tokens.append((path, sent_with))
        if session_used is not None:
            session_used.token = sent_with
        if path in self.hold:
            self.entered[path].set()
            await self.hold[path].wait()
            self.finished.append(path)
        if path == _CREATE_PATH:
            if self._create_error is not None:
                raise self._create_error
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

    async def close_session(self, target: Any, *, token: str | None = None) -> bool:
        del target
        self.calls.append((_CLOSE_SESSION, {"token": token}))
        if _CLOSE_SESSION in self.hold:
            self.entered[_CLOSE_SESSION].set()
            await self.hold[_CLOSE_SESSION].wait()
            self.finished.append(_CLOSE_SESSION)
        return self._close_session_result

    def hold_path(self, path: str) -> None:
        """Make the next call on *path* wait until :meth:`release` runs."""
        self.hold[path] = asyncio.Event()
        self.entered[path] = asyncio.Event()

    def release(self, path: str) -> None:
        self.hold[path].set()

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
    # Every call on the collector goes to the session that created it.
    assert conn.tokens == [(p, _OWNER_TOKEN) for p in conn.paths]
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


async def _start_and_wait_at(conn: _FakeConnector, path: str) -> asyncio.Task[dict[str, Any]]:
    """Start the op and return its task once it is blocked inside the call on *path*."""
    conn.hold_path(path)
    task = asyncio.ensure_future(_run(conn, {"since": "2026-10-20T08:00:00Z"}))
    await asyncio.wait_for(conn.entered[path].wait(), timeout=5)
    return task


@pytest.mark.asyncio
async def test_destroy_failures_close_the_session_that_owns_the_collector() -> None:
    """Every destroy try fails: MEHO closes the session, so vCenter drops the collector."""
    conn = _FakeConnector(
        pages=[[_task_info(1)]],
        destroy_errors=[RuntimeError("destroy failed")] * _DESTROY_ATTEMPTS,
    )
    out = await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths.count(_DESTROY_PATH) == _DESTROY_ATTEMPTS
    # The close names the session the create request was sent with.
    assert conn.calls[-1] == (_CLOSE_SESSION, {"token": _OWNER_TOKEN})
    assert len(out["tasks"]) == 1
    assert out["collector_cleanup"] == "session_closed"


@pytest.mark.asyncio
async def test_cleanup_closes_the_session_the_create_was_sent_with() -> None:
    """The session is replaced while the create response is pending: the old one is closed.

    The create request went out with the first token. Another request
    replaces the session before the create answer arrives, so the cached
    token is a newer session that never held the collector. The failed
    destroys must close the first session, not the replacement.
    """
    conn = _FakeConnector(
        pages=[[_task_info(1)]],
        destroy_errors=[RuntimeError("destroy failed")] * _DESTROY_ATTEMPTS,
    )
    task = await _start_and_wait_at(conn, _CREATE_PATH)
    conn.session_token = "tok-replacement"
    conn.release(_CREATE_PATH)
    out = await task
    assert conn.paths.count(_DESTROY_PATH) == _DESTROY_ATTEMPTS
    # The read and the destroys stay on the first session too.
    assert {t for _, t in conn.tokens} == {_OWNER_TOKEN}
    assert conn.calls[-1] == (_CLOSE_SESSION, {"token": _OWNER_TOKEN})
    assert out["collector_cleanup"] == "session_closed"


@pytest.mark.asyncio
async def test_a_replaced_session_does_not_hide_the_collector() -> None:
    """The session is replaced after the create: a ManagedObjectNotFound counts only from the owner.

    The destroy is sent to the first session, so its ManagedObjectNotFound
    answer really means the collector is gone. No session is closed.
    """
    conn = _FakeConnector(pages=[[_task_info(1)]], destroy_errors=[_not_found_fault()])
    task = await _start_and_wait_at(conn, _CREATE_PATH)
    conn.session_token = "tok-replacement"
    conn.release(_CREATE_PATH)
    out = await task
    assert conn.tokens == [
        (_CREATE_PATH, _OWNER_TOKEN),
        (_READ_PATH, _OWNER_TOKEN),
        (_DESTROY_PATH, _OWNER_TOKEN),
    ]
    assert _CLOSE_SESSION not in conn.paths
    assert out["collector_cleanup"] == "destroyed"


@pytest.mark.asyncio
async def test_a_gone_owning_session_counts_as_destroyed() -> None:
    """The owning session answers NotAuthenticated: it is gone, and the collector with it."""
    conn = _FakeConnector(pages=[[_task_info(1)]], destroy_errors=[_not_authenticated_fault()])
    out = await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths.count(_DESTROY_PATH) == 1
    assert _CLOSE_SESSION not in conn.paths
    assert out["collector_cleanup"] == "destroyed"


@pytest.mark.asyncio
async def test_a_lost_create_answer_closes_the_session() -> None:
    """The create went out but its answer was lost: the session it was sent with is closed."""
    conn = _FakeConnector(pages=[], create_error=_lost_answer())
    with pytest.raises(httpx.ReadTimeout):
        await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.calls == [
        (_CREATE_PATH, {"filter": build_task_filter_spec({"since": "2026-10-20T08:00:00Z"})}),
        (_CLOSE_SESSION, {"token": _OWNER_TOKEN}),
    ]


@pytest.mark.asyncio
async def test_a_failed_create_closes_the_session_even_when_the_close_fails() -> None:
    """The create error is the one raised; the failed close is only logged."""
    conn = _FakeConnector(pages=[], create_error=_lost_answer(), close_session_result=False)
    with pytest.raises(httpx.ReadTimeout):
        await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths == [_CREATE_PATH, _CLOSE_SESSION]


@pytest.mark.asyncio
async def test_a_create_that_was_never_sent_closes_nothing() -> None:
    """No session token was recorded, so the create never reached vCenter."""
    conn = _FakeConnector(pages=[], create_error=RuntimeError("login failed"))
    conn.session_token = None
    with pytest.raises(RuntimeError, match="login failed"):
        await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths == [_CREATE_PATH]


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
async def test_cancel_during_the_create_still_destroys_the_collector() -> None:
    """A cancel while the create answer is pending: the create finishes, the destroy runs."""
    conn = _FakeConnector(pages=[[_task_info(1)]])
    task = await _start_and_wait_at(conn, _CREATE_PATH)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    conn.release(_CREATE_PATH)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.finished == [_CREATE_PATH]
    assert conn.paths == [_CREATE_PATH, _DESTROY_PATH]


@pytest.mark.asyncio
async def test_cancel_during_the_create_closes_the_session_when_the_destroy_fails() -> None:
    conn = _FakeConnector(
        pages=[],
        destroy_errors=[RuntimeError("destroy failed")] * _DESTROY_ATTEMPTS,
    )
    task = await _start_and_wait_at(conn, _CREATE_PATH)
    task.cancel()
    conn.release(_CREATE_PATH)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.paths.count(_DESTROY_PATH) == _DESTROY_ATTEMPTS
    assert conn.calls[-1] == (_CLOSE_SESSION, {"token": _OWNER_TOKEN})


@pytest.mark.asyncio
async def test_cancel_during_the_create_without_a_collector_closes_the_session() -> None:
    """The create answer carries no reference: the session is closed, then the cancel propagates."""
    conn = _FakeConnector(pages=[], create_result={})
    task = await _start_and_wait_at(conn, _CREATE_PATH)
    task.cancel()
    conn.release(_CREATE_PATH)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.calls == [
        (_CREATE_PATH, {"filter": build_task_filter_spec({"since": "2026-10-20T08:00:00Z"})}),
        (_CLOSE_SESSION, {"token": _OWNER_TOKEN}),
    ]


@pytest.mark.asyncio
async def test_cancel_during_the_create_with_a_lost_answer_closes_the_session() -> None:
    """Cancelled while the create is pending and the create then fails: the session is closed.

    vCenter may have created the collector before the answer was lost, so
    the session it was sent with is closed. The cancel is raised afterwards.
    """
    conn = _FakeConnector(pages=[], create_error=_lost_answer())
    task = await _start_and_wait_at(conn, _CREATE_PATH)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    conn.release(_CREATE_PATH)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.finished == [_CREATE_PATH]
    assert conn.calls[-1] == (_CLOSE_SESSION, {"token": _OWNER_TOKEN})
    assert conn.paths == [_CREATE_PATH, _CLOSE_SESSION]


@pytest.mark.asyncio
async def test_cancel_during_the_create_waits_for_the_session_close() -> None:
    """A second cancel during the session close does not cut it short."""
    conn = _FakeConnector(pages=[], create_error=_lost_answer())
    conn.hold_path(_CLOSE_SESSION)
    task = await _start_and_wait_at(conn, _CREATE_PATH)
    task.cancel()
    conn.release(_CREATE_PATH)
    await asyncio.wait_for(conn.entered[_CLOSE_SESSION].wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    conn.release(_CLOSE_SESSION)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.finished == [_CREATE_PATH, _CLOSE_SESSION]


@pytest.mark.asyncio
async def test_cancel_during_the_read_still_destroys_the_collector() -> None:
    """A cancel while a page is being read: the destroy runs, then the cancel propagates."""
    conn = _FakeConnector(pages=[[_task_info(1)]])
    task = await _start_and_wait_at(conn, _READ_PATH)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.paths == [_CREATE_PATH, _READ_PATH, _DESTROY_PATH]


@pytest.mark.asyncio
async def test_cancel_during_the_cleanup_lets_the_destroy_finish() -> None:
    """A cancel while the destroy is in flight waits for it; the collector is gone."""
    conn = _FakeConnector(pages=[[_task_info(1)]])
    task = await _start_and_wait_at(conn, _DESTROY_PATH)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    conn.release(_DESTROY_PATH)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.finished == [_DESTROY_PATH]
    assert conn.paths == [_CREATE_PATH, _READ_PATH, _DESTROY_PATH]


@pytest.mark.asyncio
async def test_cancel_during_the_cleanup_keeps_the_destroy_retries() -> None:
    """A cancel during a failing destroy does not cut the retries or the session close."""
    conn = _FakeConnector(
        pages=[[_task_info(1)]],
        destroy_errors=[RuntimeError("destroy failed")] * _DESTROY_ATTEMPTS,
    )
    task = await _start_and_wait_at(conn, _DESTROY_PATH)
    task.cancel()
    conn.release(_DESTROY_PATH)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.paths.count(_DESTROY_PATH) == _DESTROY_ATTEMPTS
    assert conn.calls[-1] == (_CLOSE_SESSION, {"token": _OWNER_TOKEN})


@pytest.mark.asyncio
async def test_timeout_during_the_read_still_destroys_the_collector() -> None:
    """``asyncio.timeout`` cancels the task; the cleanup runs and the timeout is reported."""
    conn = _FakeConnector(pages=[[_task_info(1)]])
    conn.hold_path(_READ_PATH)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths == [_CREATE_PATH, _READ_PATH, _DESTROY_PATH]


@pytest.mark.asyncio
async def test_bad_params_create_no_collector() -> None:
    conn = _FakeConnector(pages=[])
    with pytest.raises(ValueError):
        await _run(conn, {"since": "not-a-time"})
    assert conn.calls == []


@pytest.mark.asyncio
async def test_missing_collector_reference_closes_the_session_without_destroy() -> None:
    conn = _FakeConnector(pages=[], create_result={})
    with pytest.raises(RuntimeError, match="no collector reference"):
        await _run(conn, {"since": "2026-10-20T08:00:00Z"})
    assert conn.paths == [_CREATE_PATH, _CLOSE_SESSION]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 307])
async def test_wire_a_redirected_session_close_is_not_a_closed_session(status: int) -> None:
    """Destroy faults every time and DELETE /api/session answers a redirect.

    The shared client hands a cross-origin 3xx back unfollowed, so the
    session (and the collector in it) may still be alive. The call must
    not report ``session_closed``; it fails with ``CollectorCleanupError``.
    """
    connector = _make_connector()
    base = "/sdk/vim25/8.0.3.0"
    try:
        async with respx.mock(base_url="https://vc.test.invalid") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post(f"{base}{_CREATE_PATH}").respond(200, json=_COLLECTOR_REF)
            mock.post(f"{base}{_READ_PATH}").respond(200, json=[_IMPORT_FAILED])
            destroy = mock.post(f"{base}{_DESTROY_PATH}").respond(
                500, json={"_typeName": "SystemError", "reason": "boom"}
            )
            revoke = mock.delete("/api/session").respond(
                status, headers={"Location": "https://elsewhere.test.invalid/api/session"}
            )
            with pytest.raises(CollectorCleanupError, match="could not be removed"):
                await connector.tasks_history(
                    _make_operator(), _Target(), {"since": "2026-10-20T08:00:00Z"}
                )
            assert destroy.call_count == _DESTROY_ATTEMPTS
            assert revoke.call_count == 1
            assert revoke.calls[0].request.headers["vmware-api-session-id"] == "tok"
            assert connector._session_tokens == {}
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_wire_cleanup_closes_the_session_that_owns_the_collector() -> None:
    """The connector logs in again before the cleanup: the old session is closed, not the new one.

    The collector was created on the first session. Its max-age runs out
    before the destroy, so the connector re-logs in (second token) and the
    first session lives on, with the collector in it. The destroys go to
    that first session; when they fail, it is the one closed, and the
    second one stays cached.
    """
    connector = _make_connector()
    base = "/sdk/vim25/8.0.3.0"
    target = _Target()
    key = target_cache_key(target)

    def _age_out_the_session(_request: httpx.Request) -> httpx.Response:
        connector._session_established_at[key] = time.monotonic() - 10_000
        return httpx.Response(200, json=[_IMPORT_FAILED])

    try:
        async with respx.mock(base_url="https://vc.test.invalid") as mock:
            login = mock.post("/api/session").mock(
                side_effect=[httpx.Response(200, json="tok-1"), httpx.Response(200, json="tok-2")]
            )
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post(f"{base}{_CREATE_PATH}").respond(200, json=_COLLECTOR_REF)
            mock.post(f"{base}{_READ_PATH}").mock(side_effect=_age_out_the_session)
            destroy = mock.post(f"{base}{_DESTROY_PATH}").respond(
                500, json={"_typeName": "SystemError", "reason": "boom"}
            )
            revoke = mock.delete("/api/session").respond(204)
            out = await connector.tasks_history(
                _make_operator(), target, {"since": "2026-10-20T08:00:00Z"}
            )
            assert login.call_count == 2
            assert destroy.call_count == _DESTROY_ATTEMPTS
            assert [c.request.headers["vmware-api-session-id"] for c in destroy.calls] == [
                "tok-1"
            ] * _DESTROY_ATTEMPTS
            assert revoke.call_count == 1
            assert revoke.calls[0].request.headers["vmware-api-session-id"] == "tok-1"
            assert out["collector_cleanup"] == "session_closed"
            assert connector._session_tokens == {key: "tok-2"}
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_wire_cleanup_closes_the_session_the_create_was_sent_with() -> None:
    """The session is replaced while the create answer is pending: the old one is closed.

    The create request goes out with the first token. Before its answer
    arrives, another request has logged in again (the cache now holds a
    second token). The collector lives in the first session, so the read,
    the destroys and the revoke all carry the first token, and the second
    session stays cached.
    """
    connector = _make_connector()
    base = "/sdk/vim25/8.0.3.0"
    target = _Target()
    key = target_cache_key(target)

    def _replace_the_session(request: httpx.Request) -> httpx.Response:
        assert request.headers["vmware-api-session-id"] == "tok-1"
        connector._session_tokens[key] = "tok-2"
        return httpx.Response(200, json=_COLLECTOR_REF)

    try:
        async with respx.mock(base_url="https://vc.test.invalid") as mock:
            mock.post("/api/session").respond(200, json="tok-1")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post(f"{base}{_CREATE_PATH}").mock(side_effect=_replace_the_session)
            read = mock.post(f"{base}{_READ_PATH}").respond(200, json=[_IMPORT_FAILED])
            destroy = mock.post(f"{base}{_DESTROY_PATH}").respond(
                500, json={"_typeName": "SystemError", "reason": "boom"}
            )
            revoke = mock.delete("/api/session").respond(204)
            out = await connector.tasks_history(
                _make_operator(), target, {"since": "2026-10-20T08:00:00Z"}
            )
            assert read.calls.last.request.headers["vmware-api-session-id"] == "tok-1"
            assert destroy.call_count == _DESTROY_ATTEMPTS
            assert destroy.calls.last.request.headers["vmware-api-session-id"] == "tok-1"
            assert revoke.call_count == 1
            assert revoke.calls[0].request.headers["vmware-api-session-id"] == "tok-1"
            assert out["collector_cleanup"] == "session_closed"
            assert connector._session_tokens == {key: "tok-2"}
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_wire_not_found_counts_only_from_the_session_that_owns_the_collector() -> None:
    """The session is replaced while the create is pending; the owner answers ManagedObjectNotFound.

    The destroy carries the first token, so the fault comes from the
    session that owns the collector and means it is gone. No session is
    closed and the second session stays cached.
    """
    connector = _make_connector()
    base = "/sdk/vim25/8.0.3.0"
    target = _Target()
    key = target_cache_key(target)

    def _replace_the_session(_request: httpx.Request) -> httpx.Response:
        connector._session_tokens[key] = "tok-2"
        return httpx.Response(200, json=_COLLECTOR_REF)

    try:
        # Routes that must stay unused are registered too, so a stray call
        # fails here instead of being swallowed by the connector.
        async with respx.mock(base_url="https://vc.test.invalid", assert_all_called=False) as mock:
            mock.post("/api/session").respond(200, json="tok-1")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post(f"{base}{_CREATE_PATH}").mock(side_effect=_replace_the_session)
            read = mock.post(f"{base}{_READ_PATH}").respond(200, json=[_IMPORT_FAILED])
            destroy = mock.post(f"{base}{_DESTROY_PATH}").respond(
                500, json={"_typeName": "ManagedObjectNotFound", "obj": {}}
            )
            revoke = mock.delete("/api/session").respond(204)
            out = await connector.tasks_history(
                _make_operator(), target, {"since": "2026-10-20T08:00:00Z"}
            )
            assert read.calls.last.request.headers["vmware-api-session-id"] == "tok-1"
            assert destroy.call_count == 1
            assert destroy.calls.last.request.headers["vmware-api-session-id"] == "tok-1"
            assert revoke.call_count == 0
            assert out["collector_cleanup"] == "destroyed"
            assert connector._session_tokens == {key: "tok-2"}
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_wire_a_lost_create_answer_ends_the_session() -> None:
    """The create request times out: DELETE /api/session runs with the token it was sent with."""
    connector = _make_connector()
    base = "/sdk/vim25/8.0.3.0"
    try:
        # Routes that must stay unused are registered too, so a stray call
        # fails here instead of being swallowed by the connector.
        async with respx.mock(base_url="https://vc.test.invalid", assert_all_called=False) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post(f"{base}{_CREATE_PATH}").mock(side_effect=httpx.ReadTimeout("lost"))
            destroy = mock.post(f"{base}{_DESTROY_PATH}").respond(204)
            revoke = mock.delete("/api/session").respond(204)
            with pytest.raises(httpx.ReadTimeout):
                await connector.tasks_history(
                    _make_operator(), _Target(), {"since": "2026-10-20T08:00:00Z"}
                )
            assert destroy.call_count == 0
            assert revoke.call_count == 1
            assert revoke.calls[0].request.headers["vmware-api-session-id"] == "tok"
            assert connector._session_tokens == {}
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_wire_a_gone_owning_session_is_not_replaced_by_a_new_login() -> None:
    """The owning session dies mid-read: no re-login for the collector calls, nothing to close.

    ``NotAuthenticated`` on the pinned read is raised as is (a new session
    would not hold the collector). The destroy goes to the same session,
    and its ``NotAuthenticated`` answer confirms the collector is gone.
    """
    connector = _make_connector()
    base = "/sdk/vim25/8.0.3.0"
    try:
        # Routes that must stay unused are registered too, so a stray call
        # fails here instead of being swallowed by the connector.
        async with respx.mock(base_url="https://vc.test.invalid", assert_all_called=False) as mock:
            login = mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post(f"{base}{_CREATE_PATH}").respond(200, json=_COLLECTOR_REF)
            read = mock.post(f"{base}{_READ_PATH}").respond(
                500, json={"_typeName": "NotAuthenticated"}
            )
            destroy = mock.post(f"{base}{_DESTROY_PATH}").respond(
                500, json={"_typeName": "NotAuthenticated"}
            )
            revoke = mock.delete("/api/session").respond(204)
            with pytest.raises(httpx.HTTPStatusError):
                await connector.tasks_history(
                    _make_operator(), _Target(), {"since": "2026-10-20T08:00:00Z"}
                )
            assert login.call_count == 1
            assert read.call_count == 1
            assert destroy.call_count == 1
            assert destroy.calls.last.request.headers["vmware-api-session-id"] == "tok"
            assert revoke.call_count == 0
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
