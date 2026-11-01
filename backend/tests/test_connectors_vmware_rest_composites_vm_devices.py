# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the VM device changes.

Covers ``vmware.composite.vm.nic.connect``, ``vmware.composite.vm.serial_port.add``,
``vmware.composite.vm.cpu_memory.options.set`` and the ``connect`` action of
``vmware.composite.vm.device.cdrom``. A REST fake and a VIM fake stand in for
the connector session and answer with saved responses written from the
vendor API reference (made-up names and ids). The policy check of the write
seam is replaced by a recorder, so the tests show what is checked and that a
held write never reaches vCenter.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

import httpx
import pytest
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest.composites import _vm_cpu_memory_options as opts
from meho_backplane.connectors.vmware_rest.composites import _vm_device_schemas as schemas
from meho_backplane.connectors.vmware_rest.composites import _vm_nic_serial as nic_serial
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    COMPOSITES_NOT_ON_8_0,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    VM_DEVICE_CDROM_PARAMETER_SCHEMA,
    VM_DEVICE_CDROM_RESPONSE_SCHEMA,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._validate import validate_params

_VM = "vm-42"
_BASE = f"/api/vcenter/vm/{_VM}"
_NIC = f"{_BASE}/hardware/ethernet/4000"


def _operator() -> Operator:
    return Operator(
        sub="op-vm-devices",
        name="VM Devices Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0d4"),
        tenant_role=TenantRole.OPERATOR,
    )


class _Target:
    name = "vc-test"


def _status_error(method: str, path: str, status: int, body: dict[str, Any]) -> httpx.HTTPError:
    request = httpx.Request(method, f"https://vc.test{path}")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _nic_info(state: str) -> dict[str, Any]:
    return {
        "label": "Network adapter 1",
        "type": "VMXNET3",
        "mac_address": "00:50:56:00:00:01",
        "state": state,
        "start_connected": True,
        "backing": {"type": "DISTRIBUTED_PORTGROUP", "network": "dvportgroup-1"},
    }


class _RestFake:
    """Recording double for the connector's REST session."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.post_error: httpx.HTTPError | None = None
        self.post_result: Any = None
        self.on_post: Any = None
        self.gets: list[str] = []
        self.posts: list[tuple[str, str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(self, target: Any, query: Any, operator: Operator) -> Any:
        return query

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        self.gets.append(path)
        if path not in self.responses:
            raise _status_error("GET", path, 404, {"error_type": "NOT_FOUND"})
        return copy.deepcopy(self.responses[path])

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Any = None,
        timeout: Any = None,
    ) -> Any:
        self.posts.append((verb, path, copy.deepcopy(json)))
        if self.post_error is not None:
            raise self.post_error
        if self.on_post is not None:
            self.on_post(self)
        return self.post_result


class _GateRecorder:
    def __init__(self, verdict: OperationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdict = verdict

    async def __call__(self, **kwargs: Any) -> OperationResult | None:
        self.calls.append(kwargs)
        return self._verdict


def _held(op_id: str) -> OperationResult:
    return OperationResult(status="awaiting_approval", op_id=op_id, result=None, duration_ms=1.0)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _valid(schema: dict[str, Any], out: dict[str, Any]) -> None:
    Draft202012Validator(schema).validate(out)


# ---------------------------------------------------------------------------
# Approval and safety level of every operation in the table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "level"),
    [
        ("vmware.composite.vm.nic.connect", "dangerous"),
        ("vmware.composite.vm.cpu_memory.options.set", "caution"),
        ("vmware.composite.vm.serial_port.add", "dangerous"),
        ("vmware.composite.vm.device.cdrom", "dangerous"),
    ],
)
def test_changes_wait_for_approval_on_both_catalogs(op_id: str, level: str) -> None:
    spec = next(s for s in _COMPOSITES if s.op_id == op_id)
    assert spec.safety_level == level
    assert spec.requires_approval is True
    assert op_id not in COMPOSITES_NOT_ON_8_0
    assert op_id in _PREVIEW_BUILDERS


# ---------------------------------------------------------------------------
# vm.nic.connect
# ---------------------------------------------------------------------------


async def _nic(conn: _RestFake, **params: Any) -> Any:
    return await nic_serial.vm_nic_connect_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": _VM, "nic": "4000", **params},
        connector=conn,  # type: ignore[arg-type]
    )


def _becomes(state: str) -> Any:
    def _apply(conn: _RestFake) -> None:
        conn.responses[_NIC] = _nic_info(state)

    return _apply


@pytest.mark.parametrize(
    ("action", "before", "after", "path"),
    [
        ("connect", "NOT_CONNECTED", "CONNECTED", f"{_NIC}?action=connect"),
        ("disconnect", "CONNECTED", "NOT_CONNECTED", f"{_NIC}?action=disconnect"),
    ],
)
async def test_nic_connect_sends_the_action_and_reads_again(
    gate: _GateRecorder, action: str, before: str, after: str, path: str
) -> None:
    conn = _RestFake({_NIC: _nic_info(before)})
    conn.on_post = _becomes(after)
    out = await _nic(conn, action=action)
    _valid(schemas.VM_NIC_CONNECT_RESPONSE_SCHEMA, out)
    assert conn.posts == [("POST", path, None)]
    assert out["status"] == f"{action}ed"
    assert out["before"]["state"] == before
    assert out["after"]["state"] == after
    (call,) = gate.calls
    assert call["op_id"] == f"POST:/vcenter/vm/{{vm}}/hardware/ethernet/{{nic}}?action={action}"
    assert call["params"] == {"vm": _VM, "nic": "4000"}


async def test_nic_connect_already_connected_sends_nothing(gate: _GateRecorder) -> None:
    conn = _RestFake({_NIC: _nic_info("CONNECTED")})
    out = await _nic(conn)
    assert out["status"] == "unchanged"
    assert conn.posts == []
    assert gate.calls == []


async def test_nic_connect_unknown_adapter_is_not_found(gate: _GateRecorder) -> None:
    out = await _nic(_RestFake({}))
    _valid(schemas.VM_NIC_CONNECT_RESPONSE_SCHEMA, out)
    assert out["status"] == "not_found"


async def test_nic_connect_vendor_fault_reaches_the_answer(gate: _GateRecorder) -> None:
    conn = _RestFake({_NIC: _nic_info("NOT_CONNECTED")})
    conn.post_error = _status_error(
        "POST",
        f"{_NIC}?action=connect",
        400,
        {
            "error_type": "NOT_ALLOWED_IN_CURRENT_STATE",
            "messages": [{"default_message": "The virtual machine is not powered on."}],
        },
    )
    out = await _nic(conn)
    _valid(schemas.VM_NIC_CONNECT_RESPONSE_SCHEMA, out)
    assert out["status"] == "refused"
    assert out["error_type"] == "NOT_ALLOWED_IN_CURRENT_STATE"
    assert "not powered on" in out["guidance"]


async def test_nic_connect_server_error_raises(gate: _GateRecorder) -> None:
    conn = _RestFake({_NIC: _nic_info("NOT_CONNECTED")})
    conn.post_error = _status_error("POST", f"{_NIC}?action=connect", 500, {})
    with pytest.raises(httpx.HTTPStatusError):
        await _nic(conn)


async def test_nic_connect_waits_for_approval_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = _held("POST:/vcenter/vm/{vm}/hardware/ethernet/{nic}?action=connect")
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(held))
    conn = _RestFake({_NIC: _nic_info("NOT_CONNECTED")})
    assert await _nic(conn) is held
    assert conn.posts == []


@pytest.mark.parametrize(
    ("params", "valid"),
    [
        ({"vm": "vm-42", "nic": "4000"}, True),
        ({"vm": "vm-42", "nic": "4000", "action": "disconnect"}, True),
        ({"vm": "vm-42", "nic": "4000/../x"}, False),
        ({"vm": "web-01", "nic": "4000"}, False),
        ({"vm": "vm-42", "nic": "4000", "action": "toggle"}, False),
        ({"vm": "vm-42"}, False),
    ],
)
def test_nic_connect_parameter_schema(params: dict[str, Any], valid: bool) -> None:
    errors = validate_params(schemas.VM_NIC_CONNECT_PARAMETER_SCHEMA, params)
    assert (errors == []) is valid, errors


async def test_nic_connect_refuses_a_device_id_that_is_not_digits(gate: _GateRecorder) -> None:
    with pytest.raises(ValueError, match="device id"):
        await _nic(_RestFake({}), nic="../session")


async def test_nic_connect_preview_shows_the_adapter_now() -> None:
    conn = _RestFake({_NIC: _nic_info("NOT_CONNECTED")})
    ctx = PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        operator=_operator(),
        target=_Target(),
        params={"vm": _VM, "nic": "4000", "action": "connect"},
        connector_instance=conn,  # type: ignore[arg-type]
    )
    out = await _PREVIEW_BUILDERS["vmware.composite.vm.nic.connect"](ctx)
    assert out is not None
    assert out["current"]["state"] == "NOT_CONNECTED"
    assert conn.posts == []


# ---------------------------------------------------------------------------
# vm.serial_port.add
# ---------------------------------------------------------------------------


async def _serial(conn: _RestFake, **params: Any) -> Any:
    return await nic_serial.vm_serial_port_add_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": _VM, **params},
        connector=conn,  # type: ignore[arg-type]
    )


def _port_appears(conn: _RestFake) -> None:
    conn.responses[f"{_BASE}/hardware/serial/9000"] = {
        "label": "Serial port 1",
        "yield_on_poll": False,
        "backing": {"type": "FILE", "file": "[datastore-a] vm-a/serial.log"},
        "state": "NOT_CONNECTED",
        "start_connected": True,
        "allow_guest_control": False,
    }


async def test_serial_add_to_a_file_sends_a_create_spec_and_reads_back(
    gate: _GateRecorder,
) -> None:
    conn = _RestFake({})
    conn.post_result = "9000"
    conn.on_post = _port_appears
    out = await _serial(conn, file="[datastore-a] vm-a/serial.log")
    _valid(schemas.VM_SERIAL_PORT_ADD_RESPONSE_SCHEMA, out)
    assert conn.posts == [
        (
            "POST",
            f"{_BASE}/hardware/serial",
            {
                "backing": {"type": "FILE", "file": "[datastore-a] vm-a/serial.log"},
                "start_connected": True,
            },
        )
    ]
    assert out["status"] == "added"
    assert out["port"] == "9000"
    assert out["port_info"]["backing_type"] == "FILE"
    (call,) = gate.calls
    assert call["op_id"] == "POST:/vcenter/vm/{vm}/hardware/serial"


@pytest.mark.parametrize(
    ("role", "kind"), [("server", "NETWORK_SERVER"), ("client", "NETWORK_CLIENT")]
)
async def test_serial_add_to_the_network(gate: _GateRecorder, role: str, kind: str) -> None:
    conn = _RestFake({})
    out = await _serial(
        conn, network_location="telnet://:7001", network_role=role, yield_on_poll=True
    )
    ((_, _, body),) = conn.posts
    assert body == {
        "backing": {"type": kind, "network_location": "telnet://:7001"},
        "start_connected": True,
        "yield_on_poll": True,
    }
    assert out["status"] == "added"


async def test_serial_add_vendor_fault_reaches_the_answer(gate: _GateRecorder) -> None:
    conn = _RestFake({})
    conn.post_error = _status_error(
        "POST",
        f"{_BASE}/hardware/serial",
        400,
        {
            "error_type": "NOT_ALLOWED_IN_CURRENT_STATE",
            "messages": [{"default_message": "The virtual machine is not powered off."}],
        },
    )
    out = await _serial(conn, file="[datastore-a] vm-a/serial.log")
    _valid(schemas.VM_SERIAL_PORT_ADD_RESPONSE_SCHEMA, out)
    assert out["status"] == "refused"
    assert out["error_type"] == "NOT_ALLOWED_IN_CURRENT_STATE"
    assert "not powered off" in out["guidance"]


async def test_serial_add_waits_for_approval_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = _held("POST:/vcenter/vm/{vm}/hardware/serial")
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(held))
    conn = _RestFake({})
    assert await _serial(conn, file="[datastore-a] a.log") is held
    assert conn.posts == []


async def test_serial_add_needs_exactly_one_target(gate: _GateRecorder) -> None:
    conn = _RestFake({})
    out = await _serial(conn)
    assert out["status"] == "invalid_request"
    assert conn.posts == []


@pytest.mark.parametrize(
    ("params", "valid"),
    [
        ({"vm": "vm-42", "file": "[ds] a.log"}, True),
        ({"vm": "vm-42", "network_location": "telnet://:7001"}, True),
        ({"vm": "vm-42", "file": "[ds] a.log", "network_location": "telnet://:7001"}, False),
        ({"vm": "vm-42"}, False),
        ({"vm": "vm-42", "file": "a.log"}, False),
        ({"vm": "vm-42", "network_location": "no-scheme"}, False),
        ({"vm": "vm-42", "file": "[ds] a.log", "network_role": "peer"}, False),
    ],
)
def test_serial_add_parameter_schema(params: dict[str, Any], valid: bool) -> None:
    errors = validate_params(schemas.VM_SERIAL_PORT_ADD_PARAMETER_SCHEMA, params)
    assert (errors == []) is valid, errors


# ---------------------------------------------------------------------------
# vm.device.cdrom -- the new connect action
# ---------------------------------------------------------------------------


async def test_cdrom_connect_posts_the_connect_action(gate: _GateRecorder) -> None:
    conn = _RestFake(
        {
            f"{_BASE}/hardware/cdrom/16000": {
                "backing": {"type": "ISO_FILE", "iso_file": "[datastore-a] iso/a.iso"},
                "state": "NOT_CONNECTED",
            }
        }
    )
    out = await _write.vm_device_cdrom_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": _VM, "cdrom": "16000", "action": "connect"},
        connector=conn,  # type: ignore[arg-type]
    )
    _valid(VM_DEVICE_CDROM_RESPONSE_SCHEMA, out)
    assert out["status"] == "connected"
    assert conn.posts == [("POST", f"{_BASE}/hardware/cdrom/16000?action=connect", None)]
    (call,) = gate.calls
    assert call["op_id"] == "POST:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}?action=connect"
    assert (
        validate_params(
            VM_DEVICE_CDROM_PARAMETER_SCHEMA, {"vm": _VM, "cdrom": "16000", "action": "connect"}
        )
        == []
    )


# ---------------------------------------------------------------------------
# vm.cpu_memory.options.set (VIM)
# ---------------------------------------------------------------------------


def _alloc(level: str, shares: int) -> dict[str, Any]:
    return {
        "_typeName": "ResourceAllocationInfo",
        "reservation": 0,
        "limit": -1,
        "shares": {"_typeName": "SharesInfo", "level": level, "shares": shares},
    }


class _VimFake:
    """Recording double for the connector's VI-JSON seam."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self, *, power: str = "poweredOff", task_state: str = "success") -> None:
        self.state: dict[str, Any] = {
            "name": "vm-a",
            "runtime.powerState": power,
            "config.cpuHotAddEnabled": False,
            "config.memoryHotAddEnabled": False,
            "config.cpuAllocation": _alloc("normal", 2000),
            "config.memoryAllocation": _alloc("normal", 20480),
        }
        self.task_state = task_state
        self.missing = False
        self.calls: list[tuple[str, Any]] = []

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
    ) -> Any:
        self.calls.append((path, copy.deepcopy(json)))
        if path.endswith("/ReconfigVM_Task"):
            if self.task_state == "success":
                self._apply(json["spec"])
            return {"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-9"}
        kind = json["specSet"][0]["propSet"][0]["type"]
        if kind == "Task":
            info: dict[str, Any] = {"state": self.task_state}
            if self.task_state == "error":
                info["error"] = {
                    "_typeName": "LocalizedMethodFault",
                    "fault": {"_typeName": "InvalidPowerState"},
                    "localizedMessage": "The attempted operation cannot be performed now.",
                }
            return {"objects": [{"propSet": [{"name": "info", "val": info}]}]}
        if self.missing:
            raise ConnectorResourceNotFoundError([_VM], "not found")
        props = [{"name": k, "val": copy.deepcopy(v)} for k, v in self.state.items()]
        return {"objects": [{"obj": {"type": "VirtualMachine", "value": _VM}, "propSet": props}]}

    def _apply(self, spec: dict[str, Any]) -> None:
        for field in ("cpuHotAddEnabled", "memoryHotAddEnabled"):
            if field in spec:
                self.state[f"config.{field}"] = spec[field]
        for field in ("cpuAllocation", "memoryAllocation"):
            if field in spec:
                shares = spec[field]["shares"]
                self.state[f"config.{field}"]["shares"].update(
                    level=shares["level"], shares=shares["shares"] or 1000
                )

    @property
    def reconfig_bodies(self) -> list[Any]:
        return [body for path, body in self.calls if path.endswith("/ReconfigVM_Task")]


async def _options(conn: _VimFake, **params: Any) -> Any:
    return await opts.vm_cpu_memory_options_set_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": _VM, **params},
        connector=conn,  # type: ignore[arg-type]
    )


async def test_options_turn_hot_add_on_in_one_task(gate: _GateRecorder) -> None:
    conn = _VimFake()
    out = await _options(conn, cpu_hot_add_enabled=True, memory_hot_add_enabled=True)
    _valid(schemas.VM_CPU_MEMORY_OPTIONS_SET_RESPONSE_SCHEMA, out)
    assert conn.reconfig_bodies == [
        {
            "spec": {
                "_typeName": "VirtualMachineConfigSpec",
                "cpuHotAddEnabled": True,
                "memoryHotAddEnabled": True,
            }
        }
    ]
    assert out["status"] == "set"
    assert out["before"]["cpu_hot_add_enabled"] is False
    assert out["after"]["cpu_hot_add_enabled"] is True
    assert out["task"] == "task-9"
    (call,) = gate.calls
    assert call["op_id"] == "POST:/VirtualMachine/{moId}/ReconfigVM_Task"
    assert call["params"]["vm"] == _VM


async def test_options_set_custom_cpu_shares_on_a_running_vm(gate: _GateRecorder) -> None:
    conn = _VimFake(power="poweredOn")
    out = await _options(conn, cpu_shares_level="custom", cpu_shares=4000)
    assert conn.reconfig_bodies == [
        {
            "spec": {
                "_typeName": "VirtualMachineConfigSpec",
                "cpuAllocation": {
                    "_typeName": "ResourceAllocationInfo",
                    "shares": {"_typeName": "SharesInfo", "level": "custom", "shares": 4000},
                },
            }
        }
    ]
    assert out["status"] == "set"
    assert out["after"]["cpu_shares"] == {"level": "custom", "shares": 4000}


async def test_options_named_shares_level_sends_a_zero_count(gate: _GateRecorder) -> None:
    conn = _VimFake()
    out = await _options(conn, memory_shares_level="high")
    body = conn.reconfig_bodies[0]["spec"]["memoryAllocation"]["shares"]
    assert body == {"_typeName": "SharesInfo", "level": "high", "shares": 0}
    assert out["status"] == "set"


async def test_options_hot_add_on_a_running_vm_sends_nothing(gate: _GateRecorder) -> None:
    conn = _VimFake(power="poweredOn")
    out = await _options(conn, cpu_hot_add_enabled=True)
    _valid(schemas.VM_CPU_MEMORY_OPTIONS_SET_RESPONSE_SCHEMA, out)
    assert out["status"] == "requires_power_off"
    assert conn.reconfig_bodies == []
    assert gate.calls == []


async def test_options_already_set_sends_nothing(gate: _GateRecorder) -> None:
    conn = _VimFake(power="poweredOn")
    out = await _options(conn, cpu_hot_add_enabled=False, cpu_shares_level="normal")
    assert out["status"] == "unchanged"
    assert conn.reconfig_bodies == []


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"cpu_shares": 4000},
        {"cpu_shares_level": "high", "cpu_shares": 4000},
        {"memory_shares_level": "custom"},
    ],
)
async def test_options_bad_request_sends_nothing(
    gate: _GateRecorder, params: dict[str, Any]
) -> None:
    conn = _VimFake()
    out = await _options(conn, **params)
    assert out["status"] == "invalid_request"
    assert conn.calls == []


async def test_options_unknown_vm(gate: _GateRecorder) -> None:
    conn = _VimFake()
    conn.missing = True
    out = await _options(conn, cpu_hot_add_enabled=True)
    assert out["status"] == "vm_not_found"
    assert conn.reconfig_bodies == []


async def test_options_vendor_fault_reaches_the_answer(gate: _GateRecorder) -> None:
    conn = _VimFake(task_state="error")
    with pytest.raises(RuntimeError) as err:
        await _options(conn, cpu_hot_add_enabled=True)
    assert "InvalidPowerState" in str(err.value)
    assert "cannot be performed now" in str(err.value)


async def test_options_waits_for_approval_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = _held("POST:/VirtualMachine/{moId}/ReconfigVM_Task")
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(held))
    conn = _VimFake()
    assert await _options(conn, cpu_hot_add_enabled=True) is held
    assert conn.reconfig_bodies == []


async def test_options_timeout_is_recorded_as_failed(
    gate: _GateRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(opts, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = _VimFake(task_state="running")
    out = await _options(conn, cpu_hot_add_enabled=True)
    _valid(schemas.VM_CPU_MEMORY_OPTIONS_SET_RESPONSE_SCHEMA, out)
    assert out["status"] == "timeout"
    assert out["issues"][0]["severity"] == "error"


@pytest.mark.parametrize(
    ("params", "valid"),
    [
        ({"vm": "vm-42", "cpu_hot_add_enabled": True}, True),
        ({"vm": "vm-42", "memory_shares_level": "custom", "memory_shares": 1}, True),
        ({"vm": "vm-42"}, False),
        ({"vm": "vm-42", "cpu_shares": 10}, False),
        ({"vm": "vm-42", "cpu_shares_level": "custom", "cpu_shares": 0}, False),
        ({"vm": "vm-42", "cpu_shares_level": "max"}, False),
        ({"vm": "web-01", "cpu_hot_add_enabled": True}, False),
    ],
)
def test_options_parameter_schema(params: dict[str, Any], valid: bool) -> None:
    errors = validate_params(schemas.VM_CPU_MEMORY_OPTIONS_SET_PARAMETER_SCHEMA, params)
    assert (errors == []) is valid, errors


async def test_options_preview_names_the_power_problem() -> None:
    conn = _VimFake(power="poweredOn")
    ctx = PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        operator=_operator(),
        target=_Target(),
        params={"vm": _VM, "memory_hot_add_enabled": True},
        connector_instance=conn,  # type: ignore[arg-type]
    )
    out = await _PREVIEW_BUILDERS["vmware.composite.vm.cpu_memory.options.set"](ctx)
    assert out is not None
    assert out["current"]["memory_hot_add_enabled"] is False
    assert "not powered off" in out["problem"]
    assert conn.reconfig_bodies == []
