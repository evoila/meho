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
