# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the governed guest-operations channel handlers (#3100).

The ``vmware.composite.vm.guest.*`` handlers dispatch vim guest-operations
methods directly on the connector's VI-JSON seam (``_post_vmomi_json``) and,
for the one write, PUT bytes on the pooled client. These tests stub the
connector with a recording double and assert:

* the call-shape contract -- which vmomi method, in what order, with which
  MoRef + auth body -- plus the aggregation each handler returns;
* that the guest credential resolves from ``secret_ref`` (a stubbed
  ``load_basic_credentials``) and is **never** echoed into the result or a
  gate's params;
* that ``guest.net.show`` needs no guest credential at all;
* that ``guest.file.write`` gates first (no credential resolved, no vmomi
  write, no PUT when the gate short-circuits) and PUTs the bytes on a
  cleared gate, with the ``*`` transfer-URL host rewritten to the target.

The real-DB approval-park proof for ``guest.file.write`` lives in
:mod:`tests.test_connectors_vmware_rest_composites_write_gate`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import _guest

# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------


def _operator() -> Operator:
    return Operator(
        sub="agent-guest-ops",
        name="Guest Ops Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id="11111111-1111-1111-1111-111111111111",
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    """Minimal target stub -- ``host`` drives the transfer-URL rewrite."""

    name: str = "vc-test"
    host: str = "vc.example.test"
    secret_ref: str = "targets/vc"
    verify_tls: bool = False


class _FakePutClient:
    """Pooled-client stand-in that records PUTs and returns 200."""

    def __init__(self, calls: list[dict[str, Any]], *, status: int = 200) -> None:
        self._calls = calls
        self._status = status

    async def put(self, url: str, *, content: bytes | None = None) -> httpx.Response:
        self._calls.append({"url": url, "content": content})
        return httpx.Response(self._status, request=httpx.Request("PUT", url))


@dataclass
class _GuestRecordingConnector:
    """Records vmomi sub-calls + PUTs, serving canned VI-JSON responses.

    ``vmomi`` is keyed by the RetrievePropertiesEx queried object type
    (``GuestOperationsManager`` / ``VirtualMachine``) and by the concrete
    method path for the guest-ops methods.
    """

    vmomi: dict[str, Any] = field(default_factory=dict)
    put_status: int = 200
    vmomi_calls: list[dict[str, Any]] = field(default_factory=list)
    put_calls: list[dict[str, Any]] = field(default_factory=list)

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append({"path": path, "json": json})
        if path.endswith("/RetrievePropertiesEx"):
            spec_type = json["specSet"][0]["propSet"][0]["type"]
            return self.vmomi[spec_type]
        payload = self.vmomi[path]
        if isinstance(payload, Exception):
            raise payload
        return payload

    async def _http_client(self, target: Any) -> _FakePutClient:
        return _FakePutClient(self.put_calls, status=self.put_status)


class _CredRecorder:
    """Stub for ``load_basic_credentials`` -- records the requested fields."""

    def __init__(self, creds: dict[str, str]) -> None:
        self._creds = creds
        self.calls: list[tuple[str, ...]] = []

    async def __call__(
        self, target: Any, operator: Operator, *, fields: tuple[str, ...]
    ) -> dict[str, str]:
        self.calls.append(fields)
        return {f: self._creds[f] for f in fields}


class _GateRecorder:
    """Stub for ``enforce_subop_policy`` -- records gate calls, returns a verdict."""

    def __init__(self, gate_for: dict[str, OperationResult] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._gate_for = gate_for or {}

    async def __call__(
        self,
        *,
        operator: Operator,
        connector_id: str,
        op_id: str,
        safety_level: str,
        requires_approval: bool,
        target: Any,
        params: dict[str, Any],
    ) -> OperationResult | None:
        self.calls.append(
            {
                "op_id": op_id,
                "connector_id": connector_id,
                "safety_level": safety_level,
                "requires_approval": requires_approval,
                "params": dict(params),
            }
        )
        return self._gate_for.get(op_id)


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> _CredRecorder:
    recorder = _CredRecorder({"guest_username": "svc-guest", "guest_password": "s3cr3t-pw"})
    monkeypatch.setattr(_guest, "load_basic_credentials", recorder)
    return recorder


def _manager_props(prop: str, mo_type: str, moid: str) -> dict[str, Any]:
    """A RetrievePropertiesEx result carrying the sub-manager MoRef."""
    return {
        "objects": [
            {
                "obj": {"type": "GuestOperationsManager", "value": "guestOperationsManager"},
                "propSet": [
                    {
                        "name": prop,
                        "val": {
                            "_typeName": "ManagedObjectReference",
                            "type": mo_type,
                            "value": moid,
                        },
                    }
                ],
            }
        ]
    }


def _process_mgr() -> dict[str, Any]:
    return _manager_props("processManager", "GuestProcessManager", "pm-1")


def _file_mgr() -> dict[str, Any]:
    return _manager_props("fileManager", "GuestFileManager", "fm-1")


# ---------------------------------------------------------------------------
# process.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_list_resolves_manager_and_returns_processes(creds: _CredRecorder) -> None:
    procs = [
        {"name": "systemd", "pid": 1, "owner": "root", "cmdLine": "/sbin/init"},
        {"name": "sshd", "pid": 812, "owner": "root", "cmdLine": "/usr/sbin/sshd"},
    ]
    conn = _GuestRecordingConnector(
        vmomi={
            "GuestOperationsManager": _process_mgr(),
            "/GuestProcessManager/pm-1/ListProcessesInGuest": procs,
        }
    )
    out = await _guest.guest_process_list_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["vm"] == "vm-42"
    assert out["process_manager_moid"] == "pm-1"
    assert out["count"] == 2
    assert out["processes"] == procs
    # The sub-manager was resolved off the top GuestOperationsManager first.
    assert conn.vmomi_calls[0]["path"].endswith("/RetrievePropertiesEx")
    # The method body carried the VM MoRef + the NamePasswordAuthentication.
    body = conn.vmomi_calls[1]["json"]
    assert body["vm"] == {
        "_typeName": "ManagedObjectReference",
        "type": "VirtualMachine",
        "value": "vm-42",
    }
    assert body["auth"]["_typeName"] == "NamePasswordAuthentication"
    assert body["auth"]["username"] == "svc-guest"
    assert body["auth"]["interactiveSession"] is False
    assert creds.calls == [("guest_username", "guest_password")]


@pytest.mark.asyncio
async def test_process_list_caps_to_max_processes(creds: _CredRecorder) -> None:
    procs = [{"name": f"p{i}", "pid": i} for i in range(10)]
    conn = _GuestRecordingConnector(
        vmomi={
            "GuestOperationsManager": _process_mgr(),
            "/GuestProcessManager/pm-1/ListProcessesInGuest": procs,
        }
    )
    out = await _guest.guest_process_list_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "max_processes": 3},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["count"] == 3
    assert out["max_processes_applied"] == 3
    assert len(out["processes"]) == 3


@pytest.mark.asyncio
async def test_process_list_non_list_payload_raises(creds: _CredRecorder) -> None:
    conn = _GuestRecordingConnector(
        vmomi={
            "GuestOperationsManager": _process_mgr(),
            "/GuestProcessManager/pm-1/ListProcessesInGuest": {"not": "a list"},
        }
    )
    with pytest.raises(RuntimeError, match="expected list"):
        await _guest.guest_process_list_composite(
            operator=_operator(),
            target=_Target(),
            params={"vm": "vm-42"},
            connector=conn,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_guest_credential_never_in_result(creds: _CredRecorder) -> None:
    conn = _GuestRecordingConnector(
        vmomi={
            "GuestOperationsManager": _process_mgr(),
            "/GuestProcessManager/pm-1/ListProcessesInGuest": [{"name": "x", "pid": 1}],
        }
    )
    out = await _guest.guest_process_list_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert "s3cr3t-pw" not in json.dumps(out)
    assert "svc-guest" not in json.dumps(out)


# ---------------------------------------------------------------------------
# env.read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_read_whole_environment(creds: _CredRecorder) -> None:
    env = ["PATH=/usr/bin", "HOME=/root"]
    conn = _GuestRecordingConnector(
        vmomi={
            "GuestOperationsManager": _process_mgr(),
            "/GuestProcessManager/pm-1/ReadEnvironmentVariableInGuest": env,
        }
    )
    out = await _guest.guest_env_read_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["variables"] == env
    assert out["count"] == 2
    # No names -> the body carries no ``names`` key (reads the whole environment).
    assert "names" not in conn.vmomi_calls[1]["json"]


@pytest.mark.asyncio
async def test_env_read_named_variables(creds: _CredRecorder) -> None:
    conn = _GuestRecordingConnector(
        vmomi={
            "GuestOperationsManager": _process_mgr(),
            "/GuestProcessManager/pm-1/ReadEnvironmentVariableInGuest": ["PATH=/usr/bin"],
        }
    )
    out = await _guest.guest_env_read_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "names": ["PATH"]},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["count"] == 1
    assert conn.vmomi_calls[1]["json"]["names"] == ["PATH"]


# ---------------------------------------------------------------------------
# net.show (no guest credentials)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_net_show_needs_no_guest_credential(creds: _CredRecorder) -> None:
    vm_props = {
        "objects": [
            {
                "obj": {"type": "VirtualMachine", "value": "vm-42"},
                "propSet": [
                    {"name": "guest.net", "val": [{"macAddress": "00:0c:29:aa:bb:cc"}]},
                    {"name": "guest.ipStack", "val": [{"dnsConfig": {"hostName": "appliance"}}]},
                ],
            }
        ]
    }
    conn = _GuestRecordingConnector(vmomi={"VirtualMachine": vm_props})
    out = await _guest.guest_net_show_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["nics"] == [{"macAddress": "00:0c:29:aa:bb:cc"}]
    assert out["ip_stacks"] == [{"dnsConfig": {"hostName": "appliance"}}]
    # Tools-reported state -> no guest login, no credential resolution.
    assert creds.calls == []
    assert len(conn.vmomi_calls) == 1


# ---------------------------------------------------------------------------
# file.read (returns transfer info, no byte fetch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_read_returns_transfer_info(creds: _CredRecorder) -> None:
    info = {
        "url": "https://vc.example.test/guestFile?id=1&token=abc",
        "size": 4096,
        "attributes": {"_typeName": "GuestPosixFileAttributes", "permissions": 420},
    }
    conn = _GuestRecordingConnector(
        vmomi={
            "GuestOperationsManager": _file_mgr(),
            "/GuestFileManager/fm-1/InitiateFileTransferFromGuest": info,
        }
    )
    out = await _guest.guest_file_read_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "guest_path": "/etc/os-release"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["guest_path"] == "/etc/os-release"
    assert out["file_manager_moid"] == "fm-1"
    assert out["url"] == info["url"]
    assert out["size_bytes"] == 4096
    assert out["content_fetch"] == "deferred"
    # No bytes were fetched (read returns the transfer handle only).
    assert conn.put_calls == []
    # The FromGuest body carried the guest path + auth.
    body = conn.vmomi_calls[1]["json"]
    assert body["guestFilePath"] == "/etc/os-release"
    assert body["auth"]["password"] == "s3cr3t-pw"


# ---------------------------------------------------------------------------
# file.write (the one gated write)
# ---------------------------------------------------------------------------


@pytest.fixture
def auto_gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    recorder = _GateRecorder()
    monkeypatch.setattr(_guest, "enforce_subop_policy", recorder)
    return recorder


@pytest.mark.asyncio
async def test_file_write_auto_execute_puts_bytes(
    creds: _CredRecorder, auto_gate: _GateRecorder
) -> None:
    conn = _GuestRecordingConnector(
        vmomi={
            "GuestOperationsManager": _file_mgr(),
            "/GuestFileManager/fm-1/InitiateFileTransferToGuest": "https://vc.example.test/guestFile?id=9&token=xyz",
        }
    )
    out = await _guest.guest_file_write_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "guest_path": "/etc/mtu.conf", "content": "MTU=1400\n"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "written"
    assert out["guest_path"] == "/etc/mtu.conf"
    assert out["size_bytes"] == len(b"MTU=1400\n")
    assert out["overwrite"] is False
    # The bytes were PUT to the minted transfer URL.
    assert conn.put_calls == [
        {"url": "https://vc.example.test/guestFile?id=9&token=xyz", "content": b"MTU=1400\n"}
    ]
    # The gate ran with the write governance facts, params WITHOUT content.
    gate_call = auto_gate.calls[0]
    assert gate_call["op_id"] == "POST:/GuestFileManager/{moId}/InitiateFileTransferToGuest"
    assert gate_call["safety_level"] == "dangerous"
    assert gate_call["requires_approval"] is False
    assert gate_call["params"] == {"vm": "vm-42", "guest_path": "/etc/mtu.conf", "overwrite": False}
    assert "content" not in gate_call["params"]
    assert "s3cr3t-pw" not in json.dumps(out)


@pytest.mark.asyncio
async def test_file_write_rewrites_star_host(
    creds: _CredRecorder, auto_gate: _GateRecorder
) -> None:
    conn = _GuestRecordingConnector(
        vmomi={
            "GuestOperationsManager": _file_mgr(),
            "/GuestFileManager/fm-1/InitiateFileTransferToGuest": "https://*:443/guestFile?id=9&token=xyz",
        }
    )
    await _guest.guest_file_write_composite(
        operator=_operator(),
        target=_Target(host="vc.example.test"),
        params={"vm": "vm-42", "guest_path": "/tmp/x", "content": "hi"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert conn.put_calls[0]["url"] == "https://vc.example.test:443/guestFile?id=9&token=xyz"


@pytest.mark.asyncio
async def test_file_write_unwraps_boxed_url(creds: _CredRecorder, auto_gate: _GateRecorder) -> None:
    conn = _GuestRecordingConnector(
        vmomi={
            "GuestOperationsManager": _file_mgr(),
            "/GuestFileManager/fm-1/InitiateFileTransferToGuest": {
                "_typeName": "string",
                "_value": "https://vc.example.test/g?t=1",
            },
        }
    )
    await _guest.guest_file_write_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "guest_path": "/tmp/x", "content": "hi"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert conn.put_calls[0]["url"] == "https://vc.example.test/g?t=1"


@pytest.mark.asyncio
async def test_file_write_gated_short_circuits(
    creds: _CredRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    op_id = "POST:/GuestFileManager/{moId}/InitiateFileTransferToGuest"
    awaiting = OperationResult(
        status="awaiting_approval",
        op_id=op_id,
        result=None,
        duration_ms=1.0,
        extras={"approval_request_id": "00000000-0000-0000-0000-0000000000aa"},
    )
    monkeypatch.setattr(_guest, "enforce_subop_policy", _GateRecorder({op_id: awaiting}))
    conn = _GuestRecordingConnector(vmomi={})
    out = await _guest.guest_file_write_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "guest_path": "/etc/mtu.conf", "content": "MTU=1400\n"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    # Gate first: no credential resolved, no vmomi write, no bytes PUT.
    assert creds.calls == []
    assert conn.vmomi_calls == []
    assert conn.put_calls == []


@pytest.mark.asyncio
async def test_manager_resolution_failure_raises_with_override_hint(creds: _CredRecorder) -> None:
    """A GuestOperationsManager missing the sub-manager prop raises actionably."""
    empty = {
        "objects": [
            {
                "obj": {"type": "GuestOperationsManager", "value": "guestOperationsManager"},
                "propSet": [],
            }
        ]
    }
    conn = _GuestRecordingConnector(vmomi={"GuestOperationsManager": empty})
    with pytest.raises(RuntimeError, match="guest_ops_manager_moid"):
        await _guest.guest_process_list_composite(
            operator=_operator(),
            target=_Target(),
            params={"vm": "vm-42"},
            connector=conn,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_file_write_put_failure_propagates(
    creds: _CredRecorder, auto_gate: _GateRecorder
) -> None:
    """A non-2xx PUT to the transfer URL raises for the dispatcher to wrap."""
    conn = _GuestRecordingConnector(
        vmomi={
            "GuestOperationsManager": _file_mgr(),
            "/GuestFileManager/fm-1/InitiateFileTransferToGuest": "https://vc.example.test/g?t=1",
        },
        put_status=500,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await _guest.guest_file_write_composite(
            operator=_operator(),
            target=_Target(),
            params={"vm": "vm-42", "guest_path": "/tmp/x", "content": "hi"},
            connector=conn,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# program.run (the freeform in-guest exec write, #3255)
# ---------------------------------------------------------------------------


@dataclass
class _ProgramRunConnector:
    """Records vmomi sub-calls; serves a queued ListProcessesInGuest sequence.

    RetrievePropertiesEx resolves the GuestProcessManager (``pm-1``);
    StartProgramInGuest returns ``start_pid``; ListProcessesInGuest returns
    the next entry of ``list_responses`` (clamping to the last once the queue
    is drained, so a still-running loop keeps polling the same state).
    """

    start_pid: Any = 4321
    list_responses: list[Any] = field(default_factory=list)
    vmomi_calls: list[dict[str, Any]] = field(default_factory=list)
    _list_idx: int = 0

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append({"path": path, "json": json})
        if path.endswith("/RetrievePropertiesEx"):
            return _process_mgr()
        if path.endswith("/StartProgramInGuest"):
            return self.start_pid
        if path.endswith("/ListProcessesInGuest"):
            idx = min(self._list_idx, len(self.list_responses) - 1)
            self._list_idx += 1
            return self.list_responses[idx]
        raise KeyError(path)


def _proc(pid: int, *, exit_code: int | None = None) -> dict[str, Any]:
    """A GuestProcessInfo entry; ``exit_code`` set marks a completed process."""
    info: dict[str, Any] = {
        "name": "psql",
        "pid": pid,
        "owner": "svc-guest",
        "cmdLine": "/usr/bin/psql -c ...",
        "startTime": "2026-09-01T10:00:00Z",
    }
    if exit_code is not None:
        info["exitCode"] = exit_code
        info["endTime"] = "2026-09-01T10:00:05Z"
    return info


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the poll loop's inter-poll sleep a no-op so tests do not block."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_guest.asyncio, "sleep", _instant)


@pytest.mark.asyncio
async def test_program_run_no_wait_returns_pid_only(
    creds: _CredRecorder, auto_gate: _GateRecorder
) -> None:
    conn = _ProgramRunConnector(start_pid=4321)
    out = await _guest.guest_program_run_composite(
        operator=_operator(),
        target=_Target(),
        params={
            "vm": "vm-42",
            "program_path": "/usr/bin/systemctl",
            "arguments": "is-system-running",
            "working_directory": "/root",
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "started"
    assert out["pid"] == 4321
    assert out["vm"] == "vm-42"
    assert out["process_manager_moid"] == "pm-1"
    assert out["program_path"] == "/usr/bin/systemctl"
    assert out["exit_code"] is None
    assert out["wait"] is False
    # No poll happened (wait defaulted false): only RetrieveProperties + Start.
    paths = [c["path"] for c in conn.vmomi_calls]
    assert not any(p.endswith("/ListProcessesInGuest") for p in paths)
    # The StartProgramInGuest body carried the VM MoRef, auth, and GuestProgramSpec.
    start = next(c for c in conn.vmomi_calls if c["path"].endswith("/StartProgramInGuest"))
    body = start["json"]
    assert body["vm"] == {
        "_typeName": "ManagedObjectReference",
        "type": "VirtualMachine",
        "value": "vm-42",
    }
    assert body["auth"]["_typeName"] == "NamePasswordAuthentication"
    assert body["auth"]["username"] == "svc-guest"
    assert body["spec"] == {
        "_typeName": "GuestProgramSpec",
        "programPath": "/usr/bin/systemctl",
        "arguments": "is-system-running",
        "workingDirectory": "/root",
    }


@pytest.mark.asyncio
async def test_program_run_wait_returns_exit_code(
    creds: _CredRecorder, auto_gate: _GateRecorder, no_sleep: None
) -> None:
    conn = _ProgramRunConnector(
        start_pid=99,
        list_responses=[[_proc(99)], [_proc(99, exit_code=0)]],
    )
    out = await _guest.guest_program_run_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "program_path": "/bin/true", "wait": True},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "exited"
    assert out["pid"] == 99
    assert out["exit_code"] == 0  # exit code 0 is captured, not read as "still running"
    assert out["start_time"] == "2026-09-01T10:00:00Z"
    assert out["end_time"] == "2026-09-01T10:00:05Z"
    assert out["wait"] is True
    # The poll filtered ListProcessesInGuest to the started PID.
    poll = next(c for c in conn.vmomi_calls if c["path"].endswith("/ListProcessesInGuest"))
    assert poll["json"]["pids"] == [99]
    assert poll["json"]["auth"]["_typeName"] == "NamePasswordAuthentication"


@pytest.mark.asyncio
async def test_program_run_wait_nonzero_exit_code(
    creds: _CredRecorder, auto_gate: _GateRecorder, no_sleep: None
) -> None:
    conn = _ProgramRunConnector(start_pid=7, list_responses=[[_proc(7, exit_code=1)]])
    out = await _guest.guest_program_run_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "program_path": "/bin/false", "wait": True},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "exited"
    assert out["exit_code"] == 1


@pytest.mark.asyncio
async def test_program_run_wait_process_no_longer_listed(
    creds: _CredRecorder, auto_gate: _GateRecorder, no_sleep: None
) -> None:
    """Seen running, then gone before an exit code -> exit_unknown, no hang."""
    conn = _ProgramRunConnector(start_pid=55, list_responses=[[_proc(55)], []])
    out = await _guest.guest_program_run_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "program_path": "/bin/sleep", "wait": True},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "exit_unknown"
    assert out["exit_code"] is None


@pytest.mark.asyncio
async def test_program_run_wait_times_out_while_running(
    creds: _CredRecorder, auto_gate: _GateRecorder, no_sleep: None
) -> None:
    """Still running when the wall-clock deadline passes -> timeout status.

    ``timeout_seconds=0`` makes the deadline the poll's start instant, so the
    post-poll deadline check fires after exactly one poll (monotonic is
    non-decreasing) -- a deterministic timeout without patching the clock.
    """
    conn = _ProgramRunConnector(start_pid=8, list_responses=[[_proc(8)]])
    out = await _guest.guest_program_run_composite(
        operator=_operator(),
        target=_Target(),
        params={
            "vm": "vm-42",
            "program_path": "/bin/sleep",
            "wait": True,
            "timeout_seconds": 0,
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "timeout"
    assert out["exit_code"] is None


@pytest.mark.asyncio
async def test_program_run_gate_first_short_circuits(
    creds: _CredRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked gate resolves no credential and starts no program."""
    op_id = "POST:/GuestProcessManager/{moId}/StartProgramInGuest"
    awaiting = OperationResult(
        status="awaiting_approval",
        op_id=op_id,
        result=None,
        duration_ms=1.0,
        extras={"approval_request_id": "00000000-0000-0000-0000-0000000000bb"},
    )
    monkeypatch.setattr(_guest, "enforce_subop_policy", _GateRecorder({op_id: awaiting}))
    conn = _ProgramRunConnector()
    out = await _guest.guest_program_run_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "program_path": "/bin/true", "arguments": "x"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert creds.calls == []
    assert conn.vmomi_calls == []


@pytest.mark.asyncio
async def test_program_run_redacts_arguments_and_env(
    creds: _CredRecorder, auto_gate: _GateRecorder, no_sleep: None
) -> None:
    """arguments / env values never reach the result or the sub-op gate params.

    They DO reach the vim wire body (the guest needs them) but must not land
    on any governed surface -- mirroring guest.file.write excluding content.
    """
    secret_arg = "--token=SUPERSECRET123"
    conn = _ProgramRunConnector(start_pid=321, list_responses=[[_proc(321, exit_code=0)]])
    out = await _guest.guest_program_run_composite(
        operator=_operator(),
        target=_Target(),
        params={
            "vm": "vm-42",
            "program_path": "/usr/bin/deploy",
            "arguments": secret_arg,
            "env": {"API_KEY": "SECRET_ENV_VALUE", "PATH": "/usr/bin"},
            "wait": True,
        },
        connector=conn,  # type: ignore[arg-type]
    )
    dumped = json.dumps(out)
    assert "SUPERSECRET123" not in dumped
    assert "SECRET_ENV_VALUE" not in dumped
    # The gate ran with the redaction-safe params (no arguments, no env).
    gate_call = auto_gate.calls[0]
    assert gate_call["op_id"] == "POST:/GuestProcessManager/{moId}/StartProgramInGuest"
    assert gate_call["safety_level"] == "dangerous"
    assert gate_call["requires_approval"] is False
    assert gate_call["params"] == {"vm": "vm-42", "program_path": "/usr/bin/deploy"}
    assert "arguments" not in gate_call["params"]
    assert "env" not in gate_call["params"]
    # The values are still delivered to the guest on the vim wire body.
    start = next(c for c in conn.vmomi_calls if c["path"].endswith("/StartProgramInGuest"))
    spec = start["json"]["spec"]
    assert spec["arguments"] == secret_arg
    assert spec["envVariables"] == ["API_KEY=SECRET_ENV_VALUE", "PATH=/usr/bin"]


@pytest.mark.asyncio
async def test_program_run_defaults_arguments_to_empty_string(
    creds: _CredRecorder, auto_gate: _GateRecorder
) -> None:
    """arguments is required by vim; omitting it sends an empty string."""
    conn = _ProgramRunConnector(start_pid=1)
    await _guest.guest_program_run_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "program_path": "/bin/true"},
        connector=conn,  # type: ignore[arg-type]
    )
    start = next(c for c in conn.vmomi_calls if c["path"].endswith("/StartProgramInGuest"))
    spec = start["json"]["spec"]
    assert spec["arguments"] == ""
    assert "workingDirectory" not in spec
    assert "envVariables" not in spec


@pytest.mark.asyncio
async def test_program_run_no_pid_raises(creds: _CredRecorder, auto_gate: _GateRecorder) -> None:
    conn = _ProgramRunConnector(start_pid={"not": "a pid"})
    with pytest.raises(RuntimeError, match="returned no PID"):
        await _guest.guest_program_run_composite(
            operator=_operator(),
            target=_Target(),
            params={"vm": "vm-42", "program_path": "/bin/true"},
            connector=conn,  # type: ignore[arg-type]
        )
