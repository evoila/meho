# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the vApp operations: ``vapp.create``, ``vapp.power``,
``vapp.start_order.set`` and ``vapp.delete``.

Three layers:

* the handlers and previews against the shared vim fake, extended here with
  the vApp methods: the exact request bodies, the gated write, the task poll,
  the read after the write, every stop before a write, a vendor fault, the
  poll timeout, a parked gate, and the approval-card blast radius;
* the transport: the power-on lands on ``/sdk/vim25/<release>/...`` for a
  vCenter 8.0 U3 and a vCenter 9.0 target;
* the governed flow through the real dispatcher: each change waits for a
  second person; the delete needs a preview first and is refused for an agent.

All names and ids are made up.
"""

from __future__ import annotations

import copy
import json as jsonlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.composites import (
    _vapp,
    _vapp_change,
    _vapp_delete,
    _vapp_start_order,
    _write,
    register_vmware_composite_operations,
)
from meho_backplane.connectors.vmware_rest.composites._vapp_schemas import (
    VAPP_CREATE_PARAMETER_SCHEMA,
    VAPP_CREATE_RESPONSE_SCHEMA,
    VAPP_DELETE_RESPONSE_SCHEMA,
    VAPP_POWER_PARAMETER_SCHEMA,
    VAPP_POWER_RESPONSE_SCHEMA,
    VAPP_START_ORDER_SET_PARAMETER_SCHEMA,
    VAPP_START_ORDER_SET_RESPONSE_SCHEMA,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, blast_radius_missing_reason
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests._vmware_teardown_fake import (
    GateRecorder,
    VimFake,
    http_status_error,
    moref,
    operator,
    parked,
    preview_ctx,
)

_VAPP = "resgroup-v42"
_POOL = "resgroup-8"
_CLUSTER = "domain-c7"
_FOLDER = "group-v3"
_VM_A = "vm-41"
_VM_B = "vm-43"
_NEW_VAPP = "resgroup-v100"

_CREATE_OP = "vmware.composite.vapp.create"
_POWER_OP = "vmware.composite.vapp.power"
_ORDER_OP = "vmware.composite.vapp.start_order.set"
_DELETE_OP = "vmware.composite.vapp.delete"


def _entity(vm: str, order: int, **extra: Any) -> dict[str, Any]:
    entry = {
        "_typeName": "VAppEntityConfigInfo",
        "key": moref("VirtualMachine", vm),
        "startOrder": order,
        "startDelay": 120,
        "waitingForGuest": False,
        "startAction": "powerOn",
        "stopAction": "powerOff",
        "stopDelay": 120,
    }
    entry.update(extra)
    return entry


class VappVimFake(VimFake):
    """The shared vim fake plus the vApp methods.

    Each method changes the store the way vCenter does, so the handlers' reads
    after the write see the result. ``sync_http_fault`` makes the next call
    without a task answer with a VI-JSON fault (HTTP 500).
    """

    def __init__(self) -> None:
        super().__init__()
        self.sync_http_fault: httpx.HTTPStatusError | None = None

    async def _post_vmomi_json(self, target: Any, path: str, **kwargs: Any) -> Any:
        _mo_type, moid, method = path.strip("/").split("/")
        body = kwargs.get("json")
        if method in ("CreateVApp", "UpdateVAppConfig"):
            self.calls.append((path, copy.deepcopy(body)))
            if self.sync_http_fault is not None:
                raise self.sync_http_fault
            if method == "CreateVApp":
                return self._create(moid, body)
            if self.apply_writes:
                self._update_config(moid, body["spec"])
            return None
        if method in ("PowerOnVApp_Task", "PowerOffVApp_Task"):
            self.calls.append((path, copy.deepcopy(body)))
            on = method == "PowerOnVApp_Task"
            return self._task(method, apply=lambda: self._power(moid, on=on))
        return await super()._post_vmomi_json(target, path, **kwargs)

    def _create(self, pool: str, body: dict[str, Any]) -> dict[str, str]:
        if self.apply_writes:
            self.add(
                "VirtualApp",
                _NEW_VAPP,
                name=body["name"],
                summary={"_typeName": "VirtualAppSummary", "vAppState": "stopped"},
                vm=[],
                resourcePool=[],
            )
            self.objects[("ResourcePool", pool)]["resourcePool"].append(
                moref("VirtualApp", _NEW_VAPP)
            )
        return moref("VirtualApp", _NEW_VAPP)

    def _power(self, vapp: str, *, on: bool) -> None:
        props = self.objects[("VirtualApp", vapp)]
        props["summary"]["vAppState"] = "started" if on else "stopped"
        for ref in props["vm"]:
            vm = self.objects[("VirtualMachine", ref["value"])]
            vm["runtime.powerState"] = "poweredOn" if on else "poweredOff"

    def _update_config(self, vapp: str, spec: dict[str, Any]) -> None:
        props = self.objects[("VirtualApp", vapp)]
        current = props.setdefault("vAppConfig.entityConfig", [])
        for change in spec["entityConfig"]:
            key = change["key"]["value"]
            entry = next((e for e in current if e["key"]["value"] == key), None)
            if entry is None:
                current.append(change)
            else:
                entry.update({k: v for k, v in change.items() if k != "key"})


def _seed(
    conn: VappVimFake,
    *,
    state: str = "stopped",
    power: str = "poweredOff",
    children: list[dict[str, str]] | None = None,
) -> None:
    conn.add(
        "VirtualApp",
        _VAPP,
        name="app-stack",
        summary={"_typeName": "VirtualAppSummary", "vAppState": state},
        vm=[moref("VirtualMachine", _VM_A), moref("VirtualMachine", _VM_B)],
        resourcePool=children or [],
        **{"vAppConfig.entityConfig": [_entity(_VM_B, 2), _entity(_VM_A, 1)]},
    )
    conn.add("VirtualMachine", _VM_A, name="db-vm", **{"runtime.powerState": power})
    conn.add("VirtualMachine", _VM_B, name="web-vm", **{"runtime.powerState": power})


def _seed_parent(conn: VappVimFake, *, child_types: list[str] | None = None) -> None:
    conn.add("ClusterComputeResource", _CLUSTER, resourcePool=moref("ResourcePool", _POOL))
    conn.add("ResourcePool", _POOL, resourcePool=[moref("VirtualApp", _VAPP)])
    conn.add(
        "Folder",
        _FOLDER,
        childType=child_types or ["Folder", "VirtualMachine", "VirtualApp"],
    )


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _create(conn: Any, **params: Any) -> Any:
    return await _vapp_change.vapp_create_composite(
        operator=operator(), target=object(), params=params, connector=conn
    )


async def _power(conn: Any, action: str, **extra: Any) -> Any:
    params = {"vapp": _VAPP, "action": action, **extra}
    return await _vapp_change.vapp_power_composite(
        operator=operator(), target=object(), params=params, connector=conn
    )


async def _order(conn: Any, vms: list[dict[str, Any]], vapp: str = _VAPP) -> Any:
    return await _vapp_start_order.vapp_start_order_set_composite(
        operator=operator(), target=object(), params={"vapp": vapp, "vms": vms}, connector=conn
    )


async def _delete(conn: Any, vapp: str = _VAPP) -> Any:
    return await _vapp_delete.vapp_delete_composite(
        operator=operator(), target=object(), params={"vapp": vapp}, connector=conn
    )


# ===========================================================================
# Schemas
# ===========================================================================


@pytest.mark.parametrize(
    "schema",
    [
        VAPP_CREATE_PARAMETER_SCHEMA,
        VAPP_POWER_PARAMETER_SCHEMA,
        VAPP_START_ORDER_SET_PARAMETER_SCHEMA,
    ],
)
def test_parameter_examples_validate(schema: dict[str, Any]) -> None:
    for example in schema["examples"]:
        Draft202012Validator(schema).validate(example)


def test_create_takes_either_a_cluster_or_a_pool_not_both() -> None:
    validator = Draft202012Validator(VAPP_CREATE_PARAMETER_SCHEMA)
    both = {"name": "a", "cluster": _CLUSTER, "resource_pool": _POOL, "folder": _FOLDER}
    assert not validator.is_valid(both)
    assert not validator.is_valid({"name": "a", "folder": _FOLDER})
    assert not validator.is_valid({"name": "a", "resource_pool": _VAPP, "folder": _FOLDER})


def test_start_order_entry_needs_a_setting() -> None:
    validator = Draft202012Validator(VAPP_START_ORDER_SET_PARAMETER_SCHEMA)
    assert not validator.is_valid({"vapp": _VAPP, "vms": [{"vm": _VM_A}]})
    assert not validator.is_valid({"vapp": _VAPP, "vms": [{"vm": _VM_A, "stop_action": "x"}]})


# ===========================================================================
# vapp.create
# ===========================================================================


async def test_create_in_a_cluster_sends_the_vendor_body(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed_parent(conn)
    _seed(conn)
    out = await _create(conn, name="new-stack", cluster=_CLUSTER, folder=_FOLDER)

    assert out["status"] == "created"
    assert out["vapp"] == _NEW_VAPP
    assert out["resource_pool"] == _POOL
    assert out["state"] == "stopped"
    assert out["vms"] == []
    assert [c["op_id"] for c in gate.calls] == ["POST:/ResourcePool/{moId}/CreateVApp"]
    assert gate.calls[0]["params"]["resource_pool"] == _POOL
    [(path, body)] = conn.calls_to("CreateVApp")
    assert path == f"/ResourcePool/{_POOL}/CreateVApp"
    allocation = {
        "_typeName": "ResourceAllocationInfo",
        "reservation": 0,
        "expandableReservation": True,
        "limit": -1,
        "shares": {"_typeName": "SharesInfo", "shares": 0, "level": "normal"},
    }
    assert body == {
        "name": "new-stack",
        "resSpec": {
            "_typeName": "ResourceConfigSpec",
            "cpuAllocation": allocation,
            "memoryAllocation": allocation,
        },
        "configSpec": {"_typeName": "VAppConfigSpec"},
        "vmFolder": moref("Folder", _FOLDER),
    }
    Draft202012Validator(VAPP_CREATE_RESPONSE_SCHEMA).validate(out)


async def test_create_in_a_pool_with_a_note(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed_parent(conn)
    out = await _create(
        conn, name="new-stack", resource_pool=_POOL, folder=_FOLDER, annotation="<note>"
    )
    assert out["status"] == "created"
    [(_path, body)] = conn.calls_to("CreateVApp")
    assert body["configSpec"] == {"_typeName": "VAppConfigSpec", "annotation": "<note>"}


async def test_create_of_an_existing_vapp_is_unchanged(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed_parent(conn)
    _seed(conn)
    out = await _create(conn, name="app-stack", resource_pool=_POOL, folder=_FOLDER)
    assert out["status"] == "unchanged"
    assert out["vapp"] == _VAPP
    assert [r["vm"] for r in out["vms"]] == [_VM_A, _VM_B]
    assert gate.calls == []
    assert conn.calls_to("CreateVApp") == []
    Draft202012Validator(VAPP_CREATE_RESPONSE_SCHEMA).validate(out)


@pytest.mark.parametrize(
    ("setup", "params", "status", "fragment"),
    [
        ("pool-name", {"resource_pool": _POOL}, "precondition_failed", "resource pool"),
        ("host-folder", {"resource_pool": _POOL}, "precondition_failed", "VM folder"),
        ("none", {"cluster": "domain-c99"}, "not_found", "domain-c99"),
        ("none", {"resource_pool": "resgroup-99"}, "not_found", "resgroup-99"),
        ("none", {"resource_pool": _POOL, "cluster": _CLUSTER}, "invalid_request", "exactly"),
    ],
)
async def test_create_stops_before_any_write(
    gate: GateRecorder, setup: str, params: dict[str, Any], status: str, fragment: str
) -> None:
    conn = VappVimFake()
    _seed_parent(conn, child_types=["Folder", "HostSystem"] if setup == "host-folder" else None)
    if setup == "pool-name":
        conn.add("ResourcePool", "resgroup-9", name="new-stack")
        conn.objects[("ResourcePool", _POOL)]["resourcePool"].append(
            moref("ResourcePool", "resgroup-9")
        )
    out = await _create(conn, name="new-stack", folder=_FOLDER, **params)
    assert out["status"] == status
    assert fragment in out["guidance"]
    assert gate.calls == []
    assert conn.calls_to("CreateVApp") == []


async def test_create_vendor_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed_parent(conn)
    conn.sync_http_fault = http_status_error(
        500,
        jsonlib.dumps({"_typeName": "InvalidName", "faultstring": "The name is not valid."}),
    )
    with pytest.raises(RuntimeError, match=r"CreateVApp faulted: InvalidName: The name"):
        await _create(conn, name="new-stack", resource_pool=_POOL, folder=_FOLDER)


async def test_create_that_does_not_read_back_is_not_confirmed(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed_parent(conn)
    conn.apply_writes = False
    out = await _create(conn, name="new-stack", resource_pool=_POOL, folder=_FOLDER)
    assert out["status"] == "not_confirmed"


async def test_create_preview_names_the_pool_and_a_stop(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed_parent(conn)
    _seed(conn)
    builder = _PREVIEW_BUILDERS[_CREATE_OP]
    preview = await builder(
        preview_ctx({"name": "new-stack", "cluster": _CLUSTER, "folder": _FOLDER}, conn)
    )
    assert preview is not None
    assert preview["resource_pool"] == _POOL
    assert "stops_with" not in preview
    same = await builder(
        preview_ctx({"name": "app-stack", "cluster": _CLUSTER, "folder": _FOLDER}, conn)
    )
    assert same is not None
    assert same["stops_with"] == "unchanged"
    assert gate.calls == []


# ===========================================================================
# vapp.power
# ===========================================================================


async def test_power_on_polls_the_task_and_reads_back(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed(conn)
    out = await _power(conn, "power_on")

    assert out["status"] == "powered_on"
    assert out["state_before"] == "stopped"
    assert out["state"] == "started"
    assert out["task"] == "task-1"
    assert [(r["vm"], r["start_order"], r["power_state"]) for r in out["vms"]] == [
        (_VM_A, 1, "poweredOn"),
        (_VM_B, 2, "poweredOn"),
    ]
    assert [c["op_id"] for c in gate.calls] == ["POST:/VirtualApp/{moId}/PowerOnVApp_Task"]
    [(path, body)] = conn.calls_to("PowerOnVApp_Task")
    assert path == f"/VirtualApp/{_VAPP}/PowerOnVApp_Task"
    assert body == {}
    Draft202012Validator(VAPP_POWER_RESPONSE_SCHEMA).validate(out)


@pytest.mark.parametrize("force", [False, True])
async def test_power_off_sends_the_force_flag(gate: GateRecorder, force: bool) -> None:
    conn = VappVimFake()
    _seed(conn, state="started", power="poweredOn")
    out = await _power(conn, "power_off", force=force)
    assert out["status"] == "powered_off"
    assert out["force"] is force
    [(path, body)] = conn.calls_to("PowerOffVApp_Task")
    assert path == f"/VirtualApp/{_VAPP}/PowerOffVApp_Task"
    assert body == {"force": force}
    assert gate.calls[0]["params"]["force"] is force


@pytest.mark.parametrize(
    ("state", "action", "status"),
    [
        ("started", "power_on", "unchanged"),
        ("stopped", "power_off", "unchanged"),
        ("starting", "power_off", "precondition_failed"),
        ("stopping", "power_on", "precondition_failed"),
    ],
)
async def test_power_stops_before_any_write(
    gate: GateRecorder, state: str, action: str, status: str
) -> None:
    conn = VappVimFake()
    _seed(conn, state=state)
    out = await _power(conn, action)
    assert out["status"] == status
    assert gate.calls == []
    Draft202012Validator(VAPP_POWER_RESPONSE_SCHEMA).validate(out)


async def test_power_refuses_bad_input(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed(conn)
    assert (await _power(conn, "power_on", force=True))["status"] == "invalid_request"
    out = await _vapp_change.vapp_power_composite(
        operator=operator(),
        target=object(),
        params={"vapp": _POOL, "action": "power_on"},
        connector=conn,
    )
    assert out["status"] == "invalid_request"
    out = await _vapp_change.vapp_power_composite(
        operator=operator(),
        target=object(),
        params={"vapp": "resgroup-v99", "action": "power_on"},
        connector=conn,
    )
    assert out["status"] == "not_found"
    assert gate.calls == []


async def test_power_task_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed(conn)
    conn.task_faults["PowerOnVApp_Task"] = ("InsufficientResourcesFault", "Not enough memory.")
    with pytest.raises(RuntimeError, match=r"InsufficientResourcesFault.*Not enough memory"):
        await _power(conn, "power_on")


async def test_power_timeout(gate: GateRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_vapp, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = VappVimFake()
    _seed(conn)
    conn.running.add("PowerOnVApp_Task")
    out = await _power(conn, "power_on")
    assert out["status"] == "timeout"
    assert [i["severity"] for i in out["issues"]] == ["error"]


async def test_power_that_does_not_read_back_is_not_confirmed(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed(conn)
    conn.apply_writes = False
    out = await _power(conn, "power_on")
    assert out["status"] == "not_confirmed"


async def test_power_returns_a_parked_gate_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = VappVimFake()
    _seed(conn)
    verdict = parked("POST:/VirtualApp/{moId}/PowerOnVApp_Task")
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
    assert await _power(conn, "power_on") is verdict
    assert conn.calls_to("PowerOnVApp_Task") == []


async def test_power_preview_lists_the_vms_in_start_order(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed(conn)
    preview = await _PREVIEW_BUILDERS[_POWER_OP](
        preview_ctx({"vapp": _VAPP, "action": "power_on"}, conn)
    )
    assert preview is not None
    assert "start order" in preview["summary"]
    assert [r["vm"] for r in preview["vms"]] == [_VM_A, _VM_B]
    assert gate.calls == []


# ===========================================================================
# vapp.start_order.set
# ===========================================================================


async def test_start_order_sends_only_the_given_fields(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed(conn)
    out = await _order(
        conn,
        [
            {"vm": _VM_A, "start_order": 3, "wait_for_guest": True},
            {"vm": _VM_B, "stop_action": "guestShutdown"},
        ],
    )
    assert out["status"] == "updated"
    [(path, body)] = conn.calls_to("UpdateVAppConfig")
    assert path == f"/VirtualApp/{_VAPP}/UpdateVAppConfig"
    assert body == {
        "spec": {
            "_typeName": "VAppConfigSpec",
            "entityConfig": [
                {
                    "_typeName": "VAppEntityConfigInfo",
                    "key": moref("VirtualMachine", _VM_A),
                    "startOrder": 3,
                    "waitingForGuest": True,
                },
                {
                    "_typeName": "VAppEntityConfigInfo",
                    "key": moref("VirtualMachine", _VM_B),
                    "stopAction": "guestShutdown",
                },
            ],
        }
    }
    rows = {r["vm"]: r for r in out["vms"]}
    assert rows[_VM_A]["start_order"] == 3
    assert rows[_VM_A]["wait_for_guest"] is True
    assert rows[_VM_B]["stop_action"] == "guestShutdown"
    assert [r["vm"] for r in out["vms"]] == [_VM_B, _VM_A]
    assert [c["op_id"] for c in gate.calls] == ["POST:/VirtualApp/{moId}/UpdateVAppConfig"]
    Draft202012Validator(VAPP_START_ORDER_SET_RESPONSE_SCHEMA).validate(out)


@pytest.mark.parametrize(
    ("vms", "status", "fragment"),
    [
        ([{"vm": _VM_A, "start_order": 1}], "unchanged", "nothing was sent"),
        ([{"vm": "vm-77", "start_order": 1}], "precondition_failed", "vm-77"),
        (
            [{"vm": _VM_A, "start_order": 1}, {"vm": _VM_A, "start_delay": 5}],
            "invalid_request",
            "twice",
        ),
        ([{"vm": "db-vm", "start_order": 1}], "invalid_request", "vm must be"),
    ],
)
async def test_start_order_stops_before_any_write(
    gate: GateRecorder, vms: list[dict[str, Any]], status: str, fragment: str
) -> None:
    conn = VappVimFake()
    _seed(conn)
    out = await _order(conn, vms)
    assert out["status"] == status
    assert fragment in out["guidance"]
    assert gate.calls == []
    assert conn.calls_to("UpdateVAppConfig") == []


async def test_start_order_vendor_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed(conn)
    conn.sync_http_fault = http_status_error(
        500, jsonlib.dumps({"_typeName": "TaskInProgress", "faultstring": "The vApp is busy."})
    )
    with pytest.raises(RuntimeError, match=r"UpdateVAppConfig faulted: TaskInProgress"):
        await _order(conn, [{"vm": _VM_A, "start_order": 5}])


async def test_start_order_that_does_not_read_back_is_not_confirmed(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed(conn)
    conn.apply_writes = False
    out = await _order(conn, [{"vm": _VM_A, "start_order": 5}])
    assert out["status"] == "not_confirmed"
    assert _VM_A in out["guidance"]


async def test_start_order_preview_shows_now_and_asked(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed(conn)
    asked = [{"vm": _VM_A, "start_order": 5}]
    preview = await _PREVIEW_BUILDERS[_ORDER_OP](preview_ctx({"vapp": _VAPP, "vms": asked}, conn))
    assert preview is not None
    assert preview["requested"] == asked
    assert {r["vm"] for r in preview["current"]} == {_VM_A, _VM_B}


# ===========================================================================
# vapp.delete
# ===========================================================================


async def test_delete_destroys_a_stopped_vapp(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed(conn)
    out = await _delete(conn)

    assert out["status"] == "deleted"
    assert out["object"]["name"] == "app-stack"
    assert out["object"]["vm_count"] == 2
    assert {c["moid"] for c in out["children"]} == {_VM_A, _VM_B}
    assert [c["op_id"] for c in gate.calls] == ["POST:/VirtualApp/{moId}/Destroy_Task"]
    [(path, body)] = conn.calls_to("Destroy_Task")
    assert path == f"/VirtualApp/{_VAPP}/Destroy_Task"
    assert body == {}
    Draft202012Validator(VAPP_DELETE_RESPONSE_SCHEMA).validate(out)


async def test_delete_of_a_missing_vapp_is_unchanged(gate: GateRecorder) -> None:
    conn = VappVimFake()
    out = await _delete(conn)
    assert out["status"] == "unchanged"
    assert gate.calls == []


@pytest.mark.parametrize(
    ("seed", "fragment"),
    [
        ({"state": "started", "power": "poweredOn"}, "power it off first"),
        ({"power": "poweredOn"}, "power it off first"),
        ({"children": [moref("VirtualApp", "resgroup-v50")]}, "other vApps"),
    ],
)
async def test_delete_refuses_before_any_write(
    gate: GateRecorder, seed: dict[str, Any], fragment: str
) -> None:
    conn = VappVimFake()
    _seed(conn, **seed)
    out = await _delete(conn)
    assert out["status"] == "precondition_failed"
    assert fragment in out["guidance"]
    assert out["blockers"]
    assert gate.calls == []
    assert conn.calls_to("Destroy_Task") == []


async def test_delete_refuses_a_bad_id(gate: GateRecorder) -> None:
    conn = VappVimFake()
    out = await _delete(conn, vapp=_POOL)
    assert out["status"] == "invalid_request"
    assert conn.calls == []


async def test_delete_task_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed(conn)
    conn.task_faults["Destroy_Task"] = ("InvalidState", "The operation is not allowed.")
    with pytest.raises(RuntimeError, match=r"Destroy_Task.*not allowed"):
        await _delete(conn)


async def test_delete_preview_has_a_full_blast_radius(gate: GateRecorder) -> None:
    conn = VappVimFake()
    _seed(conn)
    preview = await _PREVIEW_BUILDERS[_DELETE_OP](preview_ctx({"vapp": _VAPP}, conn))
    assert blast_radius_missing_reason(preview) is None
    assert preview is not None
    radius = preview["blast_radius"]
    assert radius["object"]["present"] is True
    assert radius["irreversibility"] == "permanent"
    assert {c["moid"] for c in radius["children"]} == {_VM_A, _VM_B}
    assert "refusal" not in radius


async def test_delete_preview_of_a_missing_vapp(gate: GateRecorder) -> None:
    conn = VappVimFake()
    preview = await _PREVIEW_BUILDERS[_DELETE_OP](preview_ctx({"vapp": _VAPP}, conn))
    assert preview is not None
    assert preview["blast_radius"]["object"]["present"] is False
    assert preview["blast_radius"]["irreversibility"] == "none-object-already-absent"


# ===========================================================================
# Transport: the vCenter 8 and vCenter 9 VI-JSON paths
# ===========================================================================


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def _patch_no_revoke_aclose(connector: VmwareRestConnector) -> None:
    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]


@dataclass
class _StubTarget:
    name: str = "vc"
    host: str = "vc.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


def _retrieve_answer(request: httpx.Request, state: dict[str, str]) -> httpx.Response:
    body = jsonlib.loads(request.content)
    mo_type = body["specSet"][0]["propSet"][0]["type"]
    if mo_type == "Task":
        state["vapp"] = "started"
        info = {"_typeName": "TaskInfo", "state": "success", "key": "task-7"}
        obj = {"type": "Task", "value": "task-7"}
        return httpx.Response(200, json=[{"obj": obj, "propSet": [{"name": "info", "val": info}]}])
    if mo_type == "VirtualMachine":
        power = "poweredOn" if state["vapp"] == "started" else "poweredOff"
        props: dict[str, Any] = {"name": "db-vm", "runtime.powerState": power}
        obj = {"type": "VirtualMachine", "value": _VM_A}
    else:
        props = {
            "name": "app-stack",
            "summary": {"_typeName": "VirtualAppSummary", "vAppState": state["vapp"]},
            "vm": [moref("VirtualMachine", _VM_A)],
            "vAppConfig.entityConfig": [_entity(_VM_A, 1)],
        }
        obj = {"type": "VirtualApp", "value": _VAPP}
    prop_set = [{"name": k, "val": v} for k, v in props.items()]
    return httpx.Response(200, json=[{"obj": obj, "propSet": prop_set}])


@pytest.mark.parametrize(("about", "release"), [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")])
async def test_power_on_lands_on_the_vi_json_path_of_each_vcenter_version(
    gate: GateRecorder, about: str, release: str
) -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    _patch_no_revoke_aclose(connector)
    state = {"vapp": "stopped"}
    base = f"/sdk/vim25/{release}"
    try:
        async with respx.mock(base_url="https://vc.test.invalid") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": about})
            mock.post(f"{base}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=lambda request: _retrieve_answer(request, state)
            )
            power = mock.post(f"{base}/VirtualApp/{_VAPP}/PowerOnVApp_Task").respond(
                200, json={"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-7"}
            )
            out = await _vapp_change.vapp_power_composite(
                operator=operator(),
                target=_StubTarget(),
                params={"vapp": _VAPP, "action": "power_on"},
                connector=connector,
            )
        assert power.call_count == 1
        assert out["status"] == "powered_on"
        assert out["task"] == "task-7"
        assert out["vms"][0]["power_state"] == "poweredOn"
    finally:
        await connector.aclose()


# ===========================================================================
# The governed flow through the dispatcher (approval gate)
# ===========================================================================

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004341")
_TARGET_NAME = "example-vcenter"


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


def _embedding_stub() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _person(sub: str, kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name="vApp Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _bootstrap(conn: VappVimFake) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = conn  # type: ignore[assignment]
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="vmware",
                host="vcenter.example.test",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.0"},
                preferred_impl_id="vmware-rest",
                notes="seeded by test_connectors_vmware_rest_composites_vapp",
            )
        )
        await s.commit()


class _FakeTarget:
    """The resolved-target shape ``dispatch`` reads (vCenter 9.0, vmware-rest)."""

    def __init__(self) -> None:
        self.product = "vmware"
        self.fingerprint = type("_Fingerprint", (), {"version": "9.0"})()
        self.preferred_impl_id: str | None = "vmware-rest"
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = _TARGET_NAME
        self.host = "vcenter.example.test"
        self.port = 443
        self.auth_model = "shared_service_account"


async def _approval_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


async def _approve_and_resume(request_id: UUID) -> Any:
    approver = _person("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


@pytest.mark.usefixtures("_dispatch_env")
async def test_registered_risk_levels() -> None:
    await _bootstrap(VappVimFake())
    ops = (_CREATE_OP, _POWER_OP, _ORDER_OP, _DELETE_OP)
    async with get_sessionmaker()() as s:
        rows = {
            row.op_id: row
            for row in (
                await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(ops)))
            )
            .scalars()
            .all()
        }
    assert {op: rows[op].safety_level for op in ops} == {
        _CREATE_OP: "caution",
        _POWER_OP: "dangerous",
        _ORDER_OP: "caution",
        _DELETE_OP: "destructive",
    }
    assert all(rows[op].requires_approval is True for op in ops)


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.parametrize(
    ("op_id", "params", "status", "method"),
    [
        (
            _CREATE_OP,
            {"name": "new-stack", "resource_pool": _POOL, "folder": _FOLDER},
            "created",
            "CreateVApp",
        ),
        (_POWER_OP, {"vapp": _VAPP, "action": "power_on"}, "powered_on", "PowerOnVApp_Task"),
        (
            _ORDER_OP,
            {"vapp": _VAPP, "vms": [{"vm": _VM_A, "start_order": 4}]},
            "updated",
            "UpdateVAppConfig",
        ),
    ],
)
async def test_a_change_waits_for_a_second_person_then_runs_once(
    op_id: str, params: dict[str, Any], status: str, method: str
) -> None:
    conn = VappVimFake()
    _seed_parent(conn)
    _seed(conn)
    await _bootstrap(conn)
    args = {"connector_id": _CONNECTOR_ID, "op_id": op_id, "target": _TARGET_NAME, "params": params}
    call = await call_operation(_person("op-requester"), args)
    assert call["status"] == "awaiting_approval", call
    assert conn.calls_to(method) == []

    resume = await _approve_and_resume(UUID(call["extras"]["approval_request_id"]))
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == status
    assert len(conn.calls_to(method)) == 1


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_without_a_grant_cannot_power_a_vapp() -> None:
    conn = VappVimFake()
    _seed(conn)
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_POWER_OP,
        target=_FakeTarget(),
        params={"vapp": _VAPP, "action": "power_on"},
    )
    assert result.status == "denied", result
    assert "dangerous" in (result.error or "")
    assert conn.calls_to("PowerOnVApp_Task") == []


@pytest.mark.usefixtures("_dispatch_env")
async def test_delete_needs_a_preview_then_a_second_person() -> None:
    conn = VappVimFake()
    _seed(conn)
    await _bootstrap(conn)
    requester = _person("op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _DELETE_OP,
        "target": _TARGET_NAME,
        "params": {"vapp": _VAPP},
    }
    refused = await call_operation(requester, args)
    assert refused["status"] == "denied", refused
    assert refused["extras"]["error_code"] == "preview_binding_required"
    assert await _approval_count() == 0

    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    assert conn.calls_to("Destroy_Task") == []
    request_id = UUID(call["extras"]["approval_request_id"])

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"]["name"] == "app-stack"
    assert effect["blast_radius"]["irreversibility"] == "permanent"
    assert len(effect["blast_radius"]["children"]) == 2

    resume = await _approve_and_resume(request_id)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "deleted"
    assert len(conn.calls_to("Destroy_Task")) == 1


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_delete_is_refused() -> None:
    conn = VappVimFake()
    _seed(conn)
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_DELETE_OP,
        target=_FakeTarget(),
        params={"vapp": _VAPP},
    )
    assert result.status == "denied", result
    assert "destructive" in (result.error or "")
    assert conn.calls_to("Destroy_Task") == []
    assert await _approval_count() == 0
