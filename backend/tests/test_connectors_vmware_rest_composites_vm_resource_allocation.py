# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``vm.resource_allocation.show`` / ``.set`` (#3880).

A recording vim fake stands in for the connector's VI-JSON seam
(``_post_vmomi_json``): ``RetrievePropertiesEx`` on a ``VirtualMachine``
serves the current allocation (and, after a successful reconfigure, the
applied one), ``ReconfigVM_Task`` returns a Task MoRef, and the ``Task``
poll serves a configurable terminal state. The #2254 sub-op gate is
replaced by a recorder so the tests prove what is gated and that a park
keeps the write off the wire.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import (
    _vm_allocation,
    _write,
    _write_preview,
)
from meho_backplane.connectors.vmware_rest.composites._vm_allocation import (
    vm_resource_allocation_set_composite,
    vm_resource_allocation_show_composite,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    VM_RESOURCE_ALLOCATION_SET_PARAMETER_SCHEMA,
    VM_RESOURCE_ALLOCATION_SET_RESPONSE_SCHEMA,
    VM_RESOURCE_ALLOCATION_SHOW_RESPONSE_SCHEMA,
)
from meho_backplane.operations._preview import PreviewContext

_RECONFIG_OP_ID = "POST:/VirtualMachine/{moId}/ReconfigVM_Task"


def _operator() -> Operator:
    return Operator(
        sub="op-vm-allocation",
        name="VM Allocation Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0b1"),
        tenant_role=TenantRole.OPERATOR,
    )


def _alloc(limit: int, reservation: int, shares: int = 1000) -> dict[str, Any]:
    """A ``ResourceAllocationInfo`` as live VI-JSON returns it."""
    return {
        "_typeName": "ResourceAllocationInfo",
        "reservation": reservation,
        "expandableReservation": False,
        "limit": limit,
        "shares": {"_typeName": "SharesInfo", "shares": shares, "level": "normal"},
    }


class _VimFake:
    """Recording double for the connector's VI-JSON seam."""

    def __init__(
        self,
        *,
        cpu: dict[str, Any] | None,
        memory: dict[str, Any] | None,
        task_state: str = "success",
        task_error: str | None = None,
    ) -> None:
        self.cpu = cpu
        self.memory = memory
        self.task_state = task_state
        self.task_error = task_error
        self.vmomi_calls: list[tuple[str, Any]] = []

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append((path, copy.deepcopy(json)))
        if path.endswith("/ReconfigVM_Task"):
            if self.task_state == "success":
                self._apply(json["spec"])
            return {"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-77"}
        spec_type = json["specSet"][0]["propSet"][0]["type"]
        if spec_type == "VirtualMachine":
            prop_set: list[dict[str, Any]] = [{"name": "name", "val": "web-01"}]
            if self.cpu is not None:
                prop_set.append({"name": "config.cpuAllocation", "val": self.cpu})
            if self.memory is not None:
                prop_set.append({"name": "config.memoryAllocation", "val": self.memory})
            if self.cpu is None and self.memory is None:
                return {"objects": []}
            return {
                "objects": [
                    {"obj": {"type": "VirtualMachine", "value": "vm-42"}, "propSet": prop_set}
                ]
            }
        if spec_type == "Task":
            info: dict[str, Any] = {"state": self.task_state}
            if self.task_error is not None:
                info["error"] = {"localizedMessage": self.task_error}
            return {
                "objects": [
                    {
                        "obj": {"type": "Task", "value": "task-77"},
                        "propSet": [{"name": "info", "val": info}],
                    }
                ]
            }
        raise AssertionError(f"unexpected RetrievePropertiesEx type {spec_type!r}")

    def _apply(self, spec: dict[str, Any]) -> None:
        for field, attr in (("cpuAllocation", "cpu"), ("memoryAllocation", "memory")):
            if field in spec:
                current = getattr(self, attr)
                current.update({k: v for k, v in spec[field].items() if k != "_typeName"})

    @property
    def reconfig_bodies(self) -> list[Any]:
        return [body for path, body in self.vmomi_calls if path.endswith("/ReconfigVM_Task")]


class _GateRecorder:
    def __init__(self, verdict: OperationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdict = verdict

    async def __call__(self, **kwargs: Any) -> OperationResult | None:
        self.calls.append(kwargs)
        return self._verdict


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _set(conn: _VimFake, **params: Any) -> Any:
    return await vm_resource_allocation_set_composite(
        operator=_operator(),
        target=object(),
        params={"vm": "vm-42", **params},
        connector=conn,  # type: ignore[arg-type]
    )


def _assert_set_schema(out: dict[str, Any]) -> None:
    Draft202012Validator(VM_RESOURCE_ALLOCATION_SET_RESPONSE_SCHEMA).validate(out)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


async def test_show_returns_cpu_and_memory_allocation() -> None:
    conn = _VimFake(cpu=_alloc(2000, 500), memory=_alloc(-1, 0, shares=40960))
    out = await vm_resource_allocation_show_composite(
        operator=_operator(),
        target=object(),
        params={"vm": "vm-42"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "ok"
    assert out["name"] == "web-01"
    assert out["cpu_allocation"] == {
        "limit": 2000,
        "reservation": 500,
        "shares": {"level": "normal", "shares": 1000},
    }
    assert out["memory_allocation"]["limit"] == -1
    Draft202012Validator(VM_RESOURCE_ALLOCATION_SHOW_RESPONSE_SCHEMA).validate(out)
    # One read, the three properties, scoped to the VM moid.
    (path, body), *_ = conn.vmomi_calls
    assert path.endswith("/RetrievePropertiesEx")
    assert body["specSet"][0]["propSet"][0]["pathSet"] == [
        "name",
        "config.cpuAllocation",
        "config.memoryAllocation",
    ]
    assert body["specSet"][0]["objectSet"][0]["obj"]["value"] == "vm-42"
    assert len(conn.vmomi_calls) == 1


async def test_show_vm_not_found() -> None:
    conn = _VimFake(cpu=None, memory=None)
    out = await vm_resource_allocation_show_composite(
        operator=_operator(),
        target=object(),
        params={"vm": "vm-missing"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "vm_not_found"
    assert out["cpu_allocation"] is None
    Draft202012Validator(VM_RESOURCE_ALLOCATION_SHOW_RESPONSE_SCHEMA).validate(out)


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


async def test_set_cpu_limit_reconfigures_only_that_field(gate: _GateRecorder) -> None:
    conn = _VimFake(cpu=_alloc(-1, 0), memory=_alloc(-1, 0))
    out = await _set(conn, cpu_limit_mhz=1000)
    assert out["status"] == "set"
    assert out["before"]["cpu_allocation"]["limit"] == -1
    assert out["after"]["cpu_allocation"]["limit"] == 1000
    assert out["after"]["memory_allocation"]["limit"] == -1
    assert out["task"] == "task-77"
    assert out["task_state"] == "success"
    assert out["error"] is None
    _assert_set_schema(out)

    # The ConfigSpec carries ONLY cpuAllocation.limit (unset = unchanged).
    assert conn.reconfig_bodies == [
        {
            "spec": {
                "_typeName": "VirtualMachineConfigSpec",
                "cpuAllocation": {"_typeName": "ResourceAllocationInfo", "limit": 1000},
            }
        }
    ]
    # The vim write went through the governed seam with its logical params.
    assert len(gate.calls) == 1
    gated = gate.calls[0]
    assert gated["op_id"] == _RECONFIG_OP_ID
    assert gated["params"] == {"vm": "vm-42", "cpu_limit_mhz": 1000}
    assert conn.vmomi_calls[1][0] == "/VirtualMachine/vm-42/ReconfigVM_Task"


async def test_set_clear_limit_with_minus_one(gate: _GateRecorder) -> None:
    conn = _VimFake(cpu=_alloc(1000, 0), memory=_alloc(-1, 0))
    out = await _set(conn, cpu_limit_mhz=-1)
    assert out["status"] == "set"
    assert out["before"]["cpu_allocation"]["limit"] == 1000
    assert out["after"]["cpu_allocation"]["limit"] == -1
    assert conn.reconfig_bodies[0]["spec"]["cpuAllocation"]["limit"] == -1


async def test_set_cpu_and_memory_together(gate: _GateRecorder) -> None:
    conn = _VimFake(cpu=_alloc(-1, 0), memory=_alloc(-1, 0))
    out = await _set(
        conn,
        cpu_reservation_mhz=500,
        memory_limit_mb=4096,
        memory_reservation_mb=1024,
    )
    assert out["status"] == "set"
    spec = conn.reconfig_bodies[0]["spec"]
    assert spec["cpuAllocation"] == {"_typeName": "ResourceAllocationInfo", "reservation": 500}
    assert spec["memoryAllocation"] == {
        "_typeName": "ResourceAllocationInfo",
        "limit": 4096,
        "reservation": 1024,
    }
    assert out["after"]["memory_allocation"]["limit"] == 4096


async def test_set_unchanged_issues_no_task(gate: _GateRecorder) -> None:
    conn = _VimFake(cpu=_alloc(1000, 0), memory=_alloc(-1, 0))
    out = await _set(conn, cpu_limit_mhz=1000)
    assert out["status"] == "unchanged"
    assert out["task"] is None
    assert out["after"] == out["before"]
    assert conn.reconfig_bodies == []
    assert gate.calls == []
    _assert_set_schema(out)


async def test_set_without_any_field_is_invalid_before_any_io(gate: _GateRecorder) -> None:
    conn = _VimFake(cpu=_alloc(-1, 0), memory=_alloc(-1, 0))
    out = await _set(conn)
    assert out["status"] == "invalid_request"
    assert "at least one" in out["guidance"]
    assert conn.vmomi_calls == []
    assert gate.calls == []
    _assert_set_schema(out)


async def test_set_limit_below_reservation_is_refused(gate: _GateRecorder) -> None:
    conn = _VimFake(cpu=_alloc(-1, 2000), memory=_alloc(-1, 0))
    out = await _set(conn, cpu_limit_mhz=1000)
    assert out["status"] == "invalid_request"
    assert "below its reservation" in out["guidance"]
    assert out["before"]["cpu_allocation"]["reservation"] == 2000
    assert conn.reconfig_bodies == []
    assert gate.calls == []


async def test_set_limit_and_lower_reservation_in_one_call_is_allowed(
    gate: _GateRecorder,
) -> None:
    conn = _VimFake(cpu=_alloc(-1, 2000), memory=_alloc(-1, 0))
    out = await _set(conn, cpu_limit_mhz=1000, cpu_reservation_mhz=0)
    assert out["status"] == "set"


async def test_set_out_of_range_value_is_refused(gate: _GateRecorder) -> None:
    conn = _VimFake(cpu=_alloc(-1, 0), memory=_alloc(-1, 0))
    out = await _set(conn, memory_reservation_mb=-1)
    assert out["status"] == "invalid_request"
    assert "out of range" in out["guidance"]
    assert conn.reconfig_bodies == []


async def test_set_vm_not_found(gate: _GateRecorder) -> None:
    conn = _VimFake(cpu=None, memory=None)
    out = await _set(conn, cpu_limit_mhz=1000)
    assert out["status"] == "vm_not_found"
    assert conn.reconfig_bodies == []
    _assert_set_schema(out)


async def test_set_gate_park_keeps_the_write_off_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parked = OperationResult(
        status="awaiting_approval",
        op_id=_RECONFIG_OP_ID,
        result=None,
        duration_ms=1.0,
    )
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(parked))
    conn = _VimFake(cpu=_alloc(-1, 0), memory=_alloc(-1, 0))
    out = await _set(conn, cpu_limit_mhz=1000)
    assert out is parked
    assert conn.reconfig_bodies == []


async def test_set_task_fault_is_a_structured_error(gate: _GateRecorder) -> None:
    conn = _VimFake(
        cpu=_alloc(-1, 0),
        memory=_alloc(-1, 0),
        task_state="error",
        task_error="A specified parameter was not correct: spec.cpuAllocation.limit",
    )
    out = await _set(conn, cpu_limit_mhz=1000)
    assert out["status"] == "task_failed"
    assert out["task"] == "task-77"
    assert out["task_state"] == "error"
    assert "spec.cpuAllocation.limit" in out["error"]
    assert out["before"]["cpu_allocation"]["limit"] == -1
    assert out["after"] is None
    _assert_set_schema(out)


async def test_set_poll_timeout(monkeypatch: pytest.MonkeyPatch, gate: _GateRecorder) -> None:
    monkeypatch.setattr(_vm_allocation, "_RESOURCE_ALLOCATION_TASK_TIMEOUT_SECONDS", 0.0)
    conn = _VimFake(cpu=_alloc(-1, 0), memory=_alloc(-1, 0), task_state="running")
    out = await _set(conn, cpu_limit_mhz=1000)
    assert out["status"] == "timeout"
    assert out["task"] == "task-77"
    assert out["task_state"] == "timeout"
    _assert_set_schema(out)


# ---------------------------------------------------------------------------
# parameter schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"vm": "vm-42"},
        {"vm": "vm-42", "cpu_limit_mhz": -2},
        {"vm": "vm-42", "cpu_reservation_mhz": -1},
        {"vm": "vm-42", "cpu_limit_mhz": 100, "cpu_shares": 10},
        {"cpu_limit_mhz": 100},
    ],
)
def test_parameter_schema_rejects(params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(VM_RESOURCE_ALLOCATION_SET_PARAMETER_SCHEMA).validate(params)


@pytest.mark.parametrize(
    "params",
    [
        {"vm": "vm-42", "cpu_limit_mhz": -1},
        {"vm": "vm-42", "memory_reservation_mb": 0},
        {"vm": "vm-42", "cpu_limit_mhz": 1000, "memory_limit_mb": 2048},
    ],
)
def test_parameter_schema_accepts(params: dict[str, Any]) -> None:
    Draft202012Validator(VM_RESOURCE_ALLOCATION_SET_PARAMETER_SCHEMA).validate(params)


# ---------------------------------------------------------------------------
# park-time preview
# ---------------------------------------------------------------------------


def _preview_ctx(params: dict[str, Any], connector: Any) -> PreviewContext:
    return PreviewContext(
        descriptor=object(),  # type: ignore[arg-type]  # the builder ignores it
        connector_instance=connector,
        operator=_operator(),
        target=object(),
        params=params,
    )


async def test_preview_live_reads_current_and_echoes_requested() -> None:
    conn = _VimFake(cpu=_alloc(-1, 0), memory=_alloc(-1, 0))
    preview = await _write_preview._vm_resource_allocation_set_preview(
        _preview_ctx({"vm": "vm-42", "cpu_limit_mhz": 1000}, conn)
    )
    assert preview == {
        "vm": "vm-42",
        "name": "web-01",
        "current": {
            "cpu_allocation": {
                "limit": -1,
                "reservation": 0,
                "shares": {"level": "normal", "shares": 1000},
            },
            "memory_allocation": {
                "limit": -1,
                "reservation": 0,
                "shares": {"level": "normal", "shares": 1000},
            },
        },
        "requested": {"cpu_limit_mhz": 1000},
    }
    # Read-only: the reconfigure never fires at park time.
    assert conn.reconfig_bodies == []


async def test_preview_declines_without_connector() -> None:
    assert (
        await _write_preview._vm_resource_allocation_set_preview(
            _preview_ctx({"vm": "vm-42", "cpu_limit_mhz": 1000}, None)
        )
        is None
    )
