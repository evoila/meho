# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the host power operations (maintenance-mode start, reboot, shut down, standby).

* The handlers on a recording double with a stubbed gate: the vendor body,
  the governed write, the refusals that send nothing, a faulted task, a poll
  timeout, the read after the task, and the running-task check of the
  maintenance-mode start.
* The real connector over mocked HTTP on the vCenter 8 and 9 VI-JSON paths.
* The registry rows (risk level, approval, both catalogs) and the preview.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.composites import (
    _host_power,
    _host_power_ops,
    _write,
)
from meho_backplane.connectors.vmware_rest.composites._host_maintenance_enter import (
    host_maintenance_enter_composite,
)
from meho_backplane.connectors.vmware_rest.composites._host_power import (
    host_reboot_composite,
    host_shutdown_composite,
    host_standby_enter_composite,
    host_standby_exit_composite,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    COMPOSITES_NOT_ON_8_0,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._validate import validate_params

_ENTER_OP = "POST:/HostSystem/{moId}/EnterMaintenanceMode_Task"
_REBOOT_OP = "POST:/HostSystem/{moId}/RebootHost_Task"


def _operator() -> Operator:
    return Operator(
        sub="op-host-power",
        name="Host Power Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a4a5"),
        tenant_role=TenantRole.OPERATOR,
    )


class _Gate:
    """Records each governed write and lets it run (or returns a canned verdict)."""

    def __init__(self, verdict: OperationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdict = verdict

    async def __call__(self, **kwargs: Any) -> OperationResult | None:
        self.calls.append(
            {k: kwargs[k] for k in ("op_id", "safety_level", "requires_approval", "params")}
        )
        return self._verdict


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _Gate:
    recorder = _Gate()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _host_props(
    *,
    connection: str = "connected",
    power: str = "poweredOn",
    maintenance: bool = True,
    standby: str | None = "none",
    recent: list[str] | None = None,
) -> list[dict[str, Any]]:
    props: list[dict[str, Any]] = [
        {"name": "name", "val": "<host-name>"},
        {"name": "runtime.connectionState", "val": connection},
        {"name": "runtime.powerState", "val": power},
        {"name": "runtime.inMaintenanceMode", "val": maintenance},
        {
            "name": "runtime.dasHostState",
            "val": {"_typeName": "ClusterDasFdmHostState", "state": "connectedToMaster"},
        },
    ]
    if standby is not None:
        props.append({"name": "runtime.standbyMode", "val": standby})
    if recent is not None:
        props.append(
            {
                "name": "recentTask",
                "val": {
                    "_typeName": "ArrayOfManagedObjectReference",
                    "_value": [
                        {"_typeName": "ManagedObjectReference", "type": "Task", "value": t}
                        for t in recent
                    ],
                },
            }
        )
    return props


def _result(mo_type: str, moid: str, props: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "_typeName": "RetrieveResult",
        "objects": [
            {
                "_typeName": "ObjectContent",
                "obj": {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid},
                "propSet": [{"_typeName": "DynamicProperty", **p} for p in props],
            }
        ],
    }


def _task_info(
    state: str, *, description_id: str = "HostSystem.reboot", fault: str | None = None
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "_typeName": "TaskInfo",
        "state": state,
        "descriptionId": description_id,
    }
    if fault is not None:
        info["error"] = {
            "_typeName": "LocalizedMethodFault",
            "fault": {"_typeName": fault},
            "localizedMessage": "The operation is not allowed in the current state.",
        }
    return info


class _Conn:
    """Recording double: host reads in order, task infos by id, task MoRefs for writes."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(
        self,
        host_reads: list[list[dict[str, Any]] | Exception | None],
        *,
        tasks: dict[str, dict[str, Any]] | None = None,
        new_task: str = "task-91",
    ) -> None:
        self.host_reads = list(host_reads)
        self.tasks = tasks or {}
        self.new_task = new_task
        self.writes: list[tuple[str, Any]] = []

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
    ) -> Any:
        del target, operator, promote_managed_object_not_found
        if not path.endswith("/RetrievePropertiesEx"):
            self.writes.append((path, json))
            return {"_typeName": "ManagedObjectReference", "type": "Task", "value": self.new_task}
        spec = json["specSet"][0]
        mo_type = spec["propSet"][0]["type"]
        moids = [o["obj"]["value"] for o in spec["objectSet"]]
        if mo_type == "HostSystem":
            read = self.host_reads.pop(0)
            if isinstance(read, Exception):
                raise read
            return {"objects": []} if read is None else _result("HostSystem", moids[0], read)
        assert mo_type == "Task"
        objects = [
            _result("Task", m, [{"name": "info", "val": self.tasks[m]}])["objects"][0]
            for m in moids
            if m in self.tasks
        ]
        return {"_typeName": "RetrieveResult", "objects": objects}


async def _call(handler: Any, conn: _Conn, params: dict[str, Any]) -> Any:
    return await handler(operator=_operator(), target=object(), params=params, connector=conn)


# ---------------------------------------------------------------------------
# host.maintenance.enter -- a vendor job
# ---------------------------------------------------------------------------


async def test_enter_starts_the_task_and_answers_accepted(gate: _Gate) -> None:
    conn = _Conn([_host_props(maintenance=False, recent=[])])
    out = await _call(
        host_maintenance_enter_composite,
        conn,
        {"host": "host-21", "vsan_mode": "evacuateAllData", "timeout_seconds": 3600},
    )
    assert out["status"] == "accepted"
    assert out["task"] == "task-91"
    assert out["status_read"] == {"op_id": "vmware.task.info", "params": {"task": "task-91"}}
    assert out["in_maintenance_mode"] is False
    assert "not in maintenance mode yet" in out["guidance"]
    assert conn.writes == [
        (
            "/HostSystem/host-21/EnterMaintenanceMode_Task",
            {
                "timeout": 3600,
                "maintenanceSpec": {
                    "_typeName": "HostMaintenanceSpec",
                    "vsanMode": {
                        "_typeName": "VsanHostDecommissionMode",
                        "objectAction": "evacuateAllData",
                    },
                },
            },
        )
    ]
    assert gate.calls == [
        {
            "op_id": _ENTER_OP,
            "safety_level": "dangerous",
            "requires_approval": False,
            "params": {"host": "host-21"},
        }
    ]


async def test_enter_default_vsan_mode_and_no_timeout(gate: _Gate) -> None:
    conn = _Conn([_host_props(maintenance=False)])
    out = await _call(host_maintenance_enter_composite, conn, {"host": "host-21"})
    assert out["vsan_mode"] == "ensureObjectAccessibility"
    body = conn.writes[0][1]
    assert body["timeout"] == 0
    assert body["maintenanceSpec"]["vsanMode"]["objectAction"] == "ensureObjectAccessibility"


async def test_enter_returns_a_running_start_and_starts_nothing(gate: _Gate) -> None:
    conn = _Conn(
        [_host_props(maintenance=False, recent=["task-5", "task-6", "task-7"])],
        tasks={
            "task-5": _task_info("running", description_id="VirtualMachine.powerOn"),
            "task-6": _task_info("success", description_id="HostSystem.enterMaintenanceMode"),
            "task-7": _task_info("running", description_id="HostSystem.enterMaintenanceMode"),
        },
    )
    out = await _call(host_maintenance_enter_composite, conn, {"host": "host-21"})
    assert out["status"] == "already_running"
    assert out["task"] == "task-7"
    assert out["status_read"]["params"] == {"task": "task-7"}
    assert conn.writes == []
    assert gate.calls == []


async def test_enter_ignores_an_ended_start(gate: _Gate) -> None:
    conn = _Conn(
        [_host_props(maintenance=False, recent=["task-6"])],
        tasks={"task-6": _task_info("error", description_id="HostSystem.enterMaintenanceMode")},
    )
    out = await _call(host_maintenance_enter_composite, conn, {"host": "host-21"})
    assert out["status"] == "accepted"
    assert len(conn.writes) == 1


@pytest.mark.parametrize(
    ("reads", "params", "status"),
    [
        ([_host_props(maintenance=True)], {"host": "host-21"}, "unchanged"),
        ([_host_props(connection="notResponding", maintenance=False)], {"host": "h"}, None),
        ([None], {"host": "host-404"}, "not_found"),
        ([], {"host": "../host-1"}, "invalid_request"),
    ],
    ids=["already-in-maintenance", "not-connected", "not-found", "bad-id"],
)
async def test_enter_refusals_send_nothing(
    gate: _Gate, reads: list[Any], params: dict[str, Any], status: str | None
) -> None:
    conn = _Conn(reads)
    out = await _call(host_maintenance_enter_composite, conn, params)
    assert out["status"] == (status or "not_connected")
    assert out["task"] is None
    assert out["status_read"] is None
    assert conn.writes == []
    assert gate.calls == []


async def test_enter_returns_the_gate_when_the_write_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    parked = OperationResult(
        status="awaiting_approval", op_id=_ENTER_OP, result=None, duration_ms=1.0
    )
    monkeypatch.setattr(_write, "enforce_subop_policy", _Gate(parked))
    conn = _Conn([_host_props(maintenance=False)])
    out = await _call(host_maintenance_enter_composite, conn, {"host": "host-21"})
    assert out is parked
    assert conn.writes == []


# ---------------------------------------------------------------------------
# reboot / shutdown / standby
# ---------------------------------------------------------------------------


async def test_reboot_waits_for_the_task_and_reads_the_host_again(gate: _Gate) -> None:
    conn = _Conn(
        [_host_props(maintenance=True), _host_props(connection="notResponding", power="unknown")],
        tasks={"task-91": _task_info("success")},
    )
    out = await _call(host_reboot_composite, conn, {"host": "host-21"})
    assert out["status"] == "done"
    assert out["task"] == "task-91"
    assert out["connection_state"] == "notResponding"
    assert out["power_state"] == "unknown"
    assert "issues" not in out
    assert conn.writes == [("/HostSystem/host-21/RebootHost_Task", {"force": False})]
    assert [c["op_id"] for c in gate.calls] == [_REBOOT_OP]


async def test_reboot_refuses_a_host_outside_maintenance_mode(gate: _Gate) -> None:
    conn = _Conn([_host_props(maintenance=False)])
    out = await _call(host_reboot_composite, conn, {"host": "host-21"})
    assert out["status"] == "not_in_maintenance"
    assert "force" in out["guidance"]
    assert conn.writes == []
    assert gate.calls == []


async def test_force_reboots_a_host_outside_maintenance_mode(gate: _Gate) -> None:
    conn = _Conn(
        [_host_props(maintenance=False), _host_props(maintenance=False)],
        tasks={"task-91": _task_info("success")},
    )
    out = await _call(host_reboot_composite, conn, {"host": "host-21", "force": True})
    assert out["status"] == "done"
    assert conn.writes[0][1] == {"force": True}


async def test_shutdown_sends_its_method(gate: _Gate) -> None:
    conn = _Conn([_host_props(), _host_props()], tasks={"task-91": _task_info("success")})
    out = await _call(host_shutdown_composite, conn, {"host": "host-21"})
    assert out["status"] == "done"
    assert conn.writes == [("/HostSystem/host-21/ShutdownHost_Task", {"force": False})]


async def test_a_faulted_task_fails_the_call_with_fault_type_and_text(gate: _Gate) -> None:
    conn = _Conn([_host_props()], tasks={"task-91": _task_info("error", fault="InvalidState")})
    with pytest.raises(RuntimeError) as excinfo:
        await _call(host_shutdown_composite, conn, {"host": "host-21"})
    message = str(excinfo.value)
    assert message.startswith("host.shutdown: ShutdownHost_Task task task-91 on host 'host-21'")
    assert "(InvalidState)" in message
    assert "not allowed in the current state" in message


async def test_a_poll_timeout_is_a_status_with_the_task_id(
    gate: _Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_host_power, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = _Conn([_host_props(), _host_props()], tasks={"task-91": _task_info("running")})
    out = await _call(host_reboot_composite, conn, {"host": "host-21"})
    assert out["status"] == "timeout"
    assert out["task"] == "task-91"
    assert "did not fail" in out["guidance"]
    assert [i["severity"] for i in out["issues"]] == ["warning"]


async def test_the_read_after_the_task_is_best_effort(gate: _Gate) -> None:
    conn = _Conn(
        [_host_props(), httpx.ConnectError("no answer")],
        tasks={"task-91": _task_info("success")},
    )
    out = await _call(host_reboot_composite, conn, {"host": "host-21"})
    assert out["status"] == "done"
    assert out["connection_state"] is None
    assert out["issues"][0]["severity"] == "warning"
    assert "ConnectError" in out["issues"][0]["message"]


async def test_standby_enter_sends_timeout_and_vm_option(gate: _Gate) -> None:
    conn = _Conn(
        [_host_props(maintenance=False), _host_props(power="standBy", standby="in")],
        tasks={"task-91": _task_info("success")},
    )
    out = await _call(
        host_standby_enter_composite,
        conn,
        {"host": "host-21", "timeout_seconds": 300, "evacuate_powered_off_vms": True},
    )
    assert out["status"] == "done"
    assert out["power_state"] == "standBy"
    assert conn.writes == [
        (
            "/HostSystem/host-21/PowerDownHostToStandBy_Task",
            {"timeoutSec": 300, "evacuatePoweredOffVms": True},
        )
    ]


async def test_standby_enter_on_a_host_in_standby_is_unchanged(gate: _Gate) -> None:
    conn = _Conn([_host_props(power="standBy")])
    out = await _call(host_standby_enter_composite, conn, {"host": "host-21"})
    assert out["status"] == "unchanged"
    assert conn.writes == []


async def test_standby_exit_wakes_the_host(gate: _Gate) -> None:
    conn = _Conn(
        [_host_props(power="standBy", connection="disconnected"), _host_props()],
        tasks={"task-91": _task_info("success")},
    )
    out = await _call(host_standby_exit_composite, conn, {"host": "host-21"})
    assert out["status"] == "done"
    assert out["power_state"] == "poweredOn"
    assert conn.writes == [("/HostSystem/host-21/PowerUpHostFromStandBy_Task", {"timeoutSec": 600})]


async def test_standby_exit_on_a_powered_on_host_is_unchanged(gate: _Gate) -> None:
    conn = _Conn([_host_props()])
    out = await _call(host_standby_exit_composite, conn, {"host": "host-21"})
    assert out["status"] == "unchanged"
    assert conn.writes == []


# ---------------------------------------------------------------------------
# The real connector on the vCenter 8 and 9 VI-JSON paths
# ---------------------------------------------------------------------------


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "<user>", "password": "<password>"}


@dataclass
class _Target:
    name: str = "vc-example"
    host: str = "vc.example.test"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


@pytest.mark.parametrize(("version", "release"), [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")])
async def test_real_connector_reboot_on_the_vi_json_path(
    gate: _Gate, version: str, release: str
) -> None:
    base = f"/sdk/vim25/{release}"
    reads = iter(
        [
            _result("HostSystem", "host-21", _host_props()),
            _result("Task", "task-91", [{"name": "info", "val": _task_info("success")}]),
            _result("HostSystem", "host-21", _host_props(connection="notResponding")),
        ]
    )
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url="https://vc.example.test") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": version})
            mock.post(f"{base}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=lambda request: httpx.Response(200, json=next(reads))
            )
            reboot = mock.post(f"{base}/HostSystem/host-21/RebootHost_Task").respond(
                200,
                json={"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-91"},
            )
            out = await host_reboot_composite(
                operator=_operator(),
                target=_Target(),
                params={"host": "host-21"},
                connector=connector,
            )
        assert reboot.call_count == 1
        assert reboot.calls.last.request.content == b'{"force":false}'
        assert out["status"] == "done"
        assert out["connection_state"] == "notResponding"
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


# ---------------------------------------------------------------------------
# Registry rows and preview
# ---------------------------------------------------------------------------

_EXPECTED_LEVELS = {
    "vmware.composite.host.maintenance.enter": "dangerous",
    "vmware.composite.host.reboot": "dangerous",
    "vmware.composite.host.shutdown": "dangerous",
    "vmware.composite.host.standby.enter": "dangerous",
    "vmware.composite.host.standby.exit": "caution",
}


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_LEVELS))
def test_every_change_waits_for_approval_on_both_catalogs(op_id: str) -> None:
    spec = next(s for s in _COMPOSITES if s.op_id == op_id)
    assert spec.safety_level == _EXPECTED_LEVELS[op_id]
    assert spec.requires_approval is True
    assert spec.group_key == "host"
    assert op_id not in COMPOSITES_NOT_ON_8_0
    assert _PREVIEW_BUILDERS[op_id] is _host_power_ops.host_power_preview
    for example in spec.parameter_schema["examples"]:
        validate_params(spec.parameter_schema, example)


@dataclass
class _Descriptor:
    op_id: str


async def test_preview_shows_the_host_and_its_vms(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _vms(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{"vm": "vm-1", "name": "<vm-name>", "power_state": "POWERED_ON"}]

    monkeypatch.setattr(_host_power_ops, "_resolve_vm_list", _vms)
    ctx = PreviewContext(
        descriptor=_Descriptor("vmware.composite.host.reboot"),  # type: ignore[arg-type]
        connector_instance=_Conn([_host_props()]),  # type: ignore[arg-type]
        operator=_operator(),
        target=object(),
        params={"host": "host-21", "force": True},
    )
    preview = await _host_power_ops.host_power_preview(ctx)
    assert preview is not None
    assert preview["force"] is True
    assert preview["in_maintenance_mode"] is True
    assert preview["vm_count"] == 1
    assert "restarts" in preview["what_happens_next"]
    assert "unavailable" not in preview


async def test_preview_lists_a_failed_read(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _vms(**_kwargs: Any) -> list[dict[str, Any]]:
        raise httpx.ConnectError("no answer")

    monkeypatch.setattr(_host_power_ops, "_resolve_vm_list", _vms)
    ctx = PreviewContext(
        descriptor=_Descriptor("vmware.composite.host.maintenance.enter"),  # type: ignore[arg-type]
        connector_instance=_Conn([httpx.ConnectError("no answer")]),  # type: ignore[arg-type]
        operator=_operator(),
        target=object(),
        params={"host": "host-21"},
    )
    preview = await _host_power_ops.host_power_preview(ctx)
    assert preview is not None
    assert len(preview["unavailable"]) == 2
    assert all("ConnectError" in line for line in preview["unavailable"])
