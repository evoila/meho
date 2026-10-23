# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the vCenter alarm operations (#4383, part 1).

* ``vmware.alarm.triggered.list`` -- the typed read of the triggered alarms;
* ``vmware.composite.alarm.acknowledge`` -- acknowledge one alarm;
* ``vmware.composite.alarm.reset`` -- reset alarms to green by filter.

A fake VI-JSON seam (``_post_vmomi_json``) holds a small alarm state: the
ServiceContent, the ``AlarmState`` entries of the root folder, the alarm rules
and the object names. ``AcknowledgeAlarm`` and ``ClearTriggeredAlarms`` change
that state the way vCenter does, so the handlers' reads after the change see
the result. No test calls a vCenter. All ids and names are made up.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.alarm_state import (
    ALARMS_GROUP_KEY,
    ALARMS_WHEN_TO_USE,
    trigger_kind,
)
from meho_backplane.connectors.vmware_rest.composites import _alarm, _write
from meho_backplane.connectors.vmware_rest.composites._alarm import (
    alarm_acknowledge_composite,
    alarm_acknowledge_preview,
    alarm_reset_composite,
    alarm_reset_preview,
)
from meho_backplane.connectors.vmware_rest.composites._alarm_schemas import (
    ALARM_ACKNOWLEDGE_PARAMETER_SCHEMA,
    ALARM_RESET_PARAMETER_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._governed_subops import (
    _GOVERNED_SUBOP_MANIFEST,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    register_vmware_composite_operations,
    register_vmware_composite_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    register_vmware_typed_operations,
    register_vmware_typed_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.typed_ops_alarm_triggered import (
    VMWARE_ALARM_TRIGGERED_LIST_OP,
    alarm_triggered_list_impl,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor, OperationGroup
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings
from tests.test_operation_contracts import OperationContract, check_operation

_TENANT_ID = UUID("00000000-0000-0000-0000-000000004383")
_ROOT = "group-d1"
_MANAGER = "AlarmManager"
_LIST_OP = "vmware.alarm.triggered.list"
_ACK_OP = "vmware.composite.alarm.acknowledge"
_RESET_OP = "vmware.composite.alarm.reset"
_ACK_SUB_OP = "POST:/AlarmManager/{moId}/AcknowledgeAlarm"
_CLEAR_SUB_OP = "POST:/AlarmManager/{moId}/ClearTriggeredAlarms"


def moref(mo_type: str, moid: str) -> dict[str, str]:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


def _boxed(value: str) -> dict[str, str]:
    return {"_typeName": "string", "_value": value}


def _event(status: str = "red") -> dict[str, Any]:
    return {"_typeName": "EventAlarmExpression", "eventType": "Event", "status": status}


def _metric() -> dict[str, Any]:
    return {"_typeName": "MetricAlarmExpression", "operator": "isAbove", "red": 90}


def _state_rule() -> dict[str, Any]:
    return {"_typeName": "StateAlarmExpression", "operator": "isEqual", "statePath": "state"}


def _or(*children: dict[str, Any]) -> dict[str, Any]:
    return {"_typeName": "OrAlarmExpression", "expression": list(children)}


def _state(
    alarm: str,
    entity_type: str,
    entity: str,
    status: str,
    time: str,
    *,
    acknowledged: bool = False,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "_typeName": "AlarmState",
        "key": f"{alarm}.{entity}",
        "alarm": moref("Alarm", alarm),
        "entity": moref(entity_type, entity),
        "overallStatus": status,
        "time": time,
        "acknowledged": acknowledged,
    }
    if acknowledged:
        state["acknowledgedByUser"] = "<user>"
        state["acknowledgedTime"] = "2026-01-02T10:00:00Z"
    return state


def _vendor_trigger_match(type_trigger: str, kind: str | None) -> bool:
    if type_trigger == "triggerTypeAll":
        return True
    wanted = "event" if type_trigger == "triggerTypeEvent" else "metric"
    return kind in (wanted, "mixed")


_VENDOR_ENTITY_TYPES = {"entityTypeHost": "HostSystem", "entityTypeVm": "VirtualMachine"}


class _AlarmVim:
    """Recording fake of the connector's VI-JSON seam, with alarm state."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, bool]] = []
        self.states: list[dict[str, Any]] = []
        self.rules: dict[str, tuple[str, dict[str, Any] | None]] = {}
        self.names: dict[tuple[str, str], str] = {}
        self.alarm_manager: str | None = _MANAGER
        #: Method name, or ``names:<type>``, that raises.
        self.fail: dict[str, Exception] = {}
        #: Raise on a triggeredAlarmState read once a change was sent.
        self.fail_read_after_change = False
        self.apply_changes = True
        self.missing: set[str] = set()
        self._changed = False

    def methods(self) -> list[str]:
        return [path.rsplit("/", 1)[-1] for path, _, _ in self.calls]

    def calls_to(self, method: str) -> list[tuple[str, Any]]:
        return [(p, b) for p, b, _ in self.calls if p.endswith(f"/{method}")]

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
    ) -> Any:
        self.calls.append((path, copy.deepcopy(json), promote_managed_object_not_found))
        _, _, method = path.strip("/").split("/")
        if method in self.fail:
            raise self.fail[method]
        if method == "RetrieveServiceContent":
            content: dict[str, Any] = {
                "_typeName": "ServiceContent",
                "rootFolder": moref("Folder", _ROOT),
            }
            if self.alarm_manager is not None:
                content["alarmManager"] = moref("AlarmManager", self.alarm_manager)
            return content
        if method == "RetrievePropertiesEx":
            return self._retrieve(json, promote_managed_object_not_found)
        if method == "AcknowledgeAlarm":
            self._changed = True
            if self.apply_changes:
                self._acknowledge(json)
            return {}
        if method == "ClearTriggeredAlarms":
            self._changed = True
            if self.apply_changes:
                self._clear(json["filter"])
            return {}
        raise AssertionError(f"unexpected vim call {path}")

    def _retrieve(self, body: Any, promote: bool) -> dict[str, Any]:
        spec = body["specSet"][0]
        mo_type = spec["propSet"][0]["type"]
        path_set = spec["propSet"][0]["pathSet"]
        moids = [o["obj"]["value"] for o in spec["objectSet"]]
        if path_set == ["triggeredAlarmState"]:
            if self.fail_read_after_change and self._changed:
                raise httpx.ConnectError("read failed")
            (moid,) = moids
            if moid in self.missing:
                if promote:
                    raise ConnectorResourceNotFoundError([moid], f"{moid} not found")
                raise RuntimeError(f"ManagedObjectNotFound: {moid}")
            states = [s for s in self.states if moid == _ROOT or s["entity"]["value"] == moid]
            val = {"_typeName": "ArrayOfAlarmState", "_value": copy.deepcopy(states)}
            return {
                "objects": [
                    {"obj": moref(mo_type, moid), "propSet": [{"name": path_set[0], "val": val}]}
                ]
            }
        if f"names:{mo_type}" in self.fail:
            raise self.fail[f"names:{mo_type}"]
        objects = []
        for moid in moids:
            if mo_type == "Alarm":
                name, rule = self.rules[moid]
                props = [{"name": "info.name", "val": _boxed(name)}]
                if rule is not None:
                    props.append({"name": "info.expression", "val": copy.deepcopy(rule)})
            else:
                props = [{"name": "name", "val": _boxed(self.names[(mo_type, moid)])}]
            objects.append({"obj": moref(mo_type, moid), "propSet": props})
        return {"objects": objects}

    def _acknowledge(self, body: dict[str, Any]) -> None:
        for state in self.states:
            if (
                state["alarm"]["value"] == body["alarm"]["value"]
                and state["entity"]["value"] == body["entity"]["value"]
            ):
                state["acknowledged"] = True
                state["acknowledgedByUser"] = "<user>"
                state["acknowledgedTime"] = "2026-01-03T09:00:00Z"

    def _clear(self, spec: dict[str, Any]) -> None:
        entity_type = _VENDOR_ENTITY_TYPES.get(spec["typeEntity"])

        def cleared(state: dict[str, Any]) -> bool:
            kind = trigger_kind(self.rules[state["alarm"]["value"]][1])
            return (
                state["overallStatus"] in spec["status"]
                and (entity_type is None or state["entity"]["type"] == entity_type)
                and _vendor_trigger_match(spec["typeTrigger"], kind)
            )

        self.states = [s for s in self.states if not cleared(s)]


def _seeded() -> _AlarmVim:
    """Two red event alarms on hosts, one on the vCenter, one yellow metric on a VM."""
    vim = _AlarmVim()
    vim.rules = {
        "alarm-101": ("Test event alarm A", _or(_event("red"), _event("green"))),
        "alarm-102": ("Test event alarm B", _event("red")),
        "alarm-103": ("Test metric alarm", _or(_metric())),
    }
    vim.states = [
        _state("alarm-101", "HostSystem", "host-201", "red", "2026-01-01T08:00:00Z"),
        _state(
            "alarm-103",
            "VirtualMachine",
            "vm-301",
            "yellow",
            "2026-01-01T12:00:00Z",
            acknowledged=True,
        ),
        _state("alarm-101", "HostSystem", "host-202", "red", "2026-01-01T09:00:00Z"),
        _state("alarm-102", "Folder", _ROOT, "red", "2026-01-01T10:00:00Z"),
    ]
    vim.names = {
        ("HostSystem", "host-201"): "host-a",
        ("HostSystem", "host-202"): "host-b",
        ("Folder", _ROOT): "Datacenters",
        ("VirtualMachine", "vm-301"): "vm-c",
    }
    return vim


def _operator(sub: str = "op-alarms", kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name="Alarm Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


class _Target:
    name = "vc-test"


class _GateRecorder:
    """Stand-in for ``enforce_subop_policy``: records the call, returns a verdict."""

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


def _parked(op_id: str) -> OperationResult:
    return OperationResult(status="awaiting_approval", op_id=op_id, result=None, duration_ms=1.0)


async def _list(vim: _AlarmVim, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return await alarm_triggered_list_impl(
        vim,  # type: ignore[arg-type]
        _operator(),
        _Target(),  # type: ignore[arg-type]
        params or {},
    )


async def _ack(vim: _AlarmVim, alarm: str, entity: str) -> Any:
    return await alarm_acknowledge_composite(
        operator=_operator(),
        target=_Target(),
        params={"alarm_id": alarm, "entity_id": entity},
        connector=vim,  # type: ignore[arg-type]
    )


async def _reset(vim: _AlarmVim, status: list[str], obj: str, trig: str) -> Any:
    return await alarm_reset_composite(
        operator=_operator(),
        target=_Target(),
        params={"status": status, "object_kind": obj, "trigger_kind": trig},
        connector=vim,  # type: ignore[arg-type]
    )


def _keys(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(r["alarm_id"], r["entity_id"]) for r in rows]


# ---------------------------------------------------------------------------
# vmware.alarm.triggered.list
# ---------------------------------------------------------------------------


async def test_list_reads_the_whole_vcenter_from_the_root_folder() -> None:
    vim = _seeded()
    out = await _list(vim)

    assert out["scope"] == {"entity_type": "Folder", "entity_id": _ROOT}
    assert out["read_note"] is None
    # Red first, newest first; then yellow.
    assert _keys(out["alarms"]) == [
        ("alarm-102", _ROOT),
        ("alarm-101", "host-202"),
        ("alarm-101", "host-201"),
        ("alarm-103", "vm-301"),
    ]
    first = out["alarms"][0]
    assert first == {
        "alarm_id": "alarm-102",
        "alarm_name": "Test event alarm B",
        "trigger_kind": "event",
        "entity_id": _ROOT,
        "entity_type": "Folder",
        "entity_name": "Datacenters",
        "status": "red",
        "time": "2026-01-01T10:00:00Z",
        "acknowledged": False,
        "acknowledged_by": None,
        "acknowledged_time": None,
    }
    acked = out["alarms"][3]
    assert acked["trigger_kind"] == "metric"
    assert acked["acknowledged_by"] == "<user>"
    assert acked["acknowledged_time"] == "2026-01-02T10:00:00Z"

    assert vim.methods()[0] == "RetrieveServiceContent"
    path, body, promote = vim.calls[1]
    assert path == "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    assert body["specSet"][0]["propSet"][0] == {
        "_typeName": "PropertySpec",
        "type": "Folder",
        "pathSet": ["triggeredAlarmState"],
    }
    assert promote is False
    alarm_read = vim.calls[2][1]["specSet"][0]
    assert alarm_read["propSet"][0]["pathSet"] == ["info.name", "info.expression"]
    assert [o["obj"]["value"] for o in alarm_read["objectSet"]] == [
        "alarm-101",
        "alarm-102",
        "alarm-103",
    ]
    # One name read per object type.
    assert [c[1]["specSet"][0]["propSet"][0]["type"] for c in vim.calls[3:]] == [
        "Folder",
        "HostSystem",
        "VirtualMachine",
    ]


async def test_list_under_one_object_reads_only_that_object() -> None:
    vim = _seeded()
    out = await _list(vim, {"entity_type": "HostSystem", "entity_id": "host-201"})

    assert out["scope"] == {"entity_type": "HostSystem", "entity_id": "host-201"}
    assert _keys(out["alarms"]) == [("alarm-101", "host-201")]
    assert "RetrieveServiceContent" not in vim.methods()
    _, body, promote = vim.calls[0]
    spec = body["specSet"][0]
    assert spec["propSet"][0]["type"] == "HostSystem"
    assert spec["objectSet"][0]["obj"] == moref("HostSystem", "host-201")
    assert promote is True


async def test_list_of_a_missing_object_is_not_found() -> None:
    vim = _seeded()
    vim.missing.add("host-999")
    with pytest.raises(ConnectorResourceNotFoundError):
        await _list(vim, {"entity_type": "HostSystem", "entity_id": "host-999"})


async def test_list_keeps_the_ids_when_a_name_read_fails() -> None:
    vim = _seeded()
    vim.fail["names:Alarm"] = httpx.ConnectError("names down")
    out = await _list(vim)

    assert out["read_note"] == "Alarm names not read (ConnectError)"
    assert all(row["alarm_name"] is None for row in out["alarms"])
    assert all(row["trigger_kind"] is None for row in out["alarms"])
    assert {row["entity_name"] for row in out["alarms"]} == {
        "Datacenters",
        "host-a",
        "host-b",
        "vm-c",
    }


async def test_list_with_nothing_triggered_reads_no_names() -> None:
    vim = _AlarmVim()
    out = await _list(vim)
    assert out["alarms"] == []
    assert out["read_note"] is None
    assert vim.methods() == ["RetrieveServiceContent", "RetrievePropertiesEx"]


async def test_acknowledged_by_is_empty_until_the_alarm_is_acknowledged() -> None:
    vim = _seeded()
    vim.states = [_state("alarm-101", "HostSystem", "host-201", "red", "2026-01-01T08:00:00Z")]
    vim.states[0]["acknowledgedByUser"] = "<stale-user>"
    out = await _list(vim)
    assert out["alarms"][0]["acknowledged"] is False
    assert out["alarms"][0]["acknowledged_by"] is None


async def test_an_entry_without_an_object_type_is_skipped() -> None:
    vim = _seeded()
    vim.states = [_state("alarm-101", "HostSystem", "host-201", "red", "2026-01-01T08:00:00Z")]
    del vim.states[0]["entity"]["type"]
    out = await _list(vim)
    assert out["alarms"] == []


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        (_event(), "event"),
        (_or(_event("red"), _event("green")), "event"),
        (_metric(), "metric"),
        (_state_rule(), "metric"),
        (_or(_metric(), _state_rule()), "metric"),
        (_or(_event(), _metric()), "mixed"),
        ({"_typeName": "AndAlarmExpression", "expression": [_or(_event())]}, "event"),
        (_or(_event(), {"_typeName": "SomeFutureExpression"}), None),
        (_or(), None),
        (None, None),
    ],
)
def test_trigger_kind_of_an_alarm_rule(rule: Any, expected: str | None) -> None:
    assert trigger_kind(rule) == expected


@pytest.mark.parametrize(
    ("params", "valid"),
    [
        ({}, True),
        ({"entity_type": "ClusterComputeResource", "entity_id": "domain-c7"}, True),
        ({"entity_type": "HostSystem"}, False),
        ({"entity_id": "host-201"}, False),
        ({"entity_type": "Alarm", "entity_id": "alarm-101"}, False),
        ({"entity_type": "HostSystem", "entity_id": ""}, False),
        ({"other": 1}, False),
    ],
)
def test_list_parameters_are_both_or_neither(params: dict[str, Any], valid: bool) -> None:
    errors = validate_params(VMWARE_ALARM_TRIGGERED_LIST_OP.parameter_schema, params)
    assert (not errors) is valid, errors


# ---------------------------------------------------------------------------
# vmware.composite.alarm.acknowledge
# ---------------------------------------------------------------------------


async def test_acknowledge_sends_one_call_and_reads_the_result_back(gate: _GateRecorder) -> None:
    vim = _seeded()
    out = await _ack(vim, "alarm-101", "host-201")

    assert out["status"] == "acknowledged"
    assert out["alarm"]["alarm_name"] == "Test event alarm A"
    assert out["alarm"]["entity_name"] == "host-a"
    assert out["alarm"]["acknowledged"] is True
    assert out["alarm"]["acknowledged_by"] == "<user>"
    assert out["alarm"]["acknowledged_time"] == "2026-01-03T09:00:00Z"
    assert "issues" not in out
    assert vim.calls_to("AcknowledgeAlarm") == [
        (
            f"/AlarmManager/{_MANAGER}/AcknowledgeAlarm",
            {"alarm": moref("Alarm", "alarm-101"), "entity": moref("HostSystem", "host-201")},
        )
    ]
    (call,) = gate.calls
    assert call["op_id"] == _ACK_SUB_OP
    assert call["params"] == {"alarm": "alarm-101", "entity": "host-201"}


async def test_acknowledge_of_an_acknowledged_alarm_changes_nothing(gate: _GateRecorder) -> None:
    vim = _seeded()
    out = await _ack(vim, "alarm-103", "vm-301")
    assert out["status"] == "unchanged"
    assert out["alarm"]["acknowledged_by"] == "<user>"
    assert vim.calls_to("AcknowledgeAlarm") == []
    assert gate.calls == []


async def test_acknowledge_of_an_alarm_that_is_not_triggered_fails(gate: _GateRecorder) -> None:
    vim = _seeded()
    out = await _ack(vim, "alarm-101", "host-999")
    assert out["status"] == "not_triggered"
    assert out["alarm"] is None
    assert out["issues"][0]["severity"] == "error"
    assert vim.calls_to("AcknowledgeAlarm") == []
    assert gate.calls == []


async def test_acknowledge_waits_when_the_gate_parks(monkeypatch: pytest.MonkeyPatch) -> None:
    parked = _parked(_ACK_SUB_OP)
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(parked))
    vim = _seeded()
    out = await _ack(vim, "alarm-101", "host-201")
    assert out is parked
    assert vim.calls_to("AcknowledgeAlarm") == []


async def test_acknowledge_passes_a_vendor_fault_on(gate: _GateRecorder) -> None:
    vim = _seeded()
    request = httpx.Request("POST", "https://vc.example.invalid/sdk/vim25/9.0.0.0/x")
    fault = httpx.HTTPStatusError(
        "NoPermission: permission to perform this operation was denied",
        request=request,
        response=httpx.Response(500, request=request),
    )
    vim.fail["AcknowledgeAlarm"] = fault
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await _ack(vim, "alarm-101", "host-201")
    assert "NoPermission" in str(raised.value)


async def test_acknowledge_not_shown_after_the_call_is_not_verified(gate: _GateRecorder) -> None:
    vim = _seeded()
    vim.apply_changes = False
    out = await _ack(vim, "alarm-101", "host-201")
    assert out["status"] == "not_verified"
    assert out["issues"][0]["severity"] == "error"
    assert len(vim.calls_to("AcknowledgeAlarm")) == 1


async def test_acknowledge_on_a_target_without_alarm_manager_fails(gate: _GateRecorder) -> None:
    vim = _seeded()
    vim.alarm_manager = None
    out = await _ack(vim, "alarm-101", "host-201")
    assert out["status"] == "no_alarm_manager"
    assert out["issues"][0]["severity"] == "error"
    assert vim.methods() == ["RetrieveServiceContent"]


def test_acknowledge_example_validates_and_both_ids_are_required() -> None:
    schema = ALARM_ACKNOWLEDGE_PARAMETER_SCHEMA
    assert validate_params(schema, schema["examples"][0]) == []
    assert validate_params(schema, {"alarm_id": "alarm-101"})
    assert validate_params(schema, {"entity_id": "host-201"})


# ---------------------------------------------------------------------------
# vmware.composite.alarm.reset
# ---------------------------------------------------------------------------


async def test_reset_sends_the_filter_and_lists_what_it_cleared(gate: _GateRecorder) -> None:
    vim = _seeded()
    out = await _reset(vim, ["red"], "all", "event")

    assert out["status"] == "reset"
    assert out["filter"] == {"status": ["red"], "object_kind": "all", "trigger_kind": "event"}
    assert _keys(out["cleared"]) == [
        ("alarm-102", _ROOT),
        ("alarm-101", "host-202"),
        ("alarm-101", "host-201"),
    ]
    assert out["cleared"][0]["alarm_name"] == "Test event alarm B"
    assert out["still_triggered"] == []
    assert "issues" not in out
    assert vim.calls_to("ClearTriggeredAlarms") == [
        (
            f"/AlarmManager/{_MANAGER}/ClearTriggeredAlarms",
            {
                "filter": {
                    "_typeName": "AlarmFilterSpec",
                    "status": ["red"],
                    "typeEntity": "entityTypeAll",
                    "typeTrigger": "triggerTypeEvent",
                }
            },
        )
    ]
    (call,) = gate.calls
    assert call["op_id"] == _CLEAR_SUB_OP
    assert call["params"] == out["filter"]
    # The yellow VM alarm is untouched.
    assert [s["alarm"]["value"] for s in vim.states] == ["alarm-103"]


async def test_reset_on_hosts_leaves_the_vcenter_alarm(gate: _GateRecorder) -> None:
    vim = _seeded()
    out = await _reset(vim, ["red"], "host", "event")

    assert out["status"] == "reset"
    assert _keys(out["cleared"]) == [("alarm-101", "host-202"), ("alarm-101", "host-201")]
    assert vim.calls_to("ClearTriggeredAlarms")[0][1]["filter"]["typeEntity"] == "entityTypeHost"
    assert {s["alarm"]["value"] for s in vim.states} == {"alarm-102", "alarm-103"}


async def test_reset_with_no_matching_alarm_sends_nothing(gate: _GateRecorder) -> None:
    vim = _seeded()
    # The only yellow alarm is on a VM, and the only red ones are event alarms.
    for status, obj, trig in ((["yellow"], "host", "all"), (["red"], "all", "metric")):
        out = await _reset(vim, status, obj, trig)
        assert out["status"] == "unchanged"
        assert out["cleared"] == []
    assert vim.calls_to("ClearTriggeredAlarms") == []
    assert gate.calls == []


async def test_reset_that_vcenter_did_not_apply_lists_the_alarms(gate: _GateRecorder) -> None:
    vim = _seeded()
    vim.apply_changes = False
    out = await _reset(vim, ["red"], "host", "all")
    assert out["status"] == "unchanged"
    assert out["cleared"] == []
    assert _keys(out["still_triggered"]) == [("alarm-101", "host-202"), ("alarm-101", "host-201")]
    assert "still_triggered" in out["guidance"]


async def test_reset_with_a_failed_read_after_the_call_is_not_verified(
    gate: _GateRecorder,
) -> None:
    vim = _seeded()
    vim.fail_read_after_change = True
    out = await _reset(vim, ["red"], "all", "all")
    assert out["status"] == "not_verified"
    assert out["cleared"] is None
    assert out["issues"][0]["severity"] == "error"
    assert "ConnectError" in out["issues"][0]["message"]
    assert len(vim.calls_to("ClearTriggeredAlarms")) == 1


async def test_reset_waits_when_the_gate_parks(monkeypatch: pytest.MonkeyPatch) -> None:
    parked = _parked(_CLEAR_SUB_OP)
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(parked))
    vim = _seeded()
    out = await _reset(vim, ["red"], "all", "all")
    assert out is parked
    assert vim.calls_to("ClearTriggeredAlarms") == []


async def test_reset_passes_a_vendor_fault_on(gate: _GateRecorder) -> None:
    vim = _seeded()
    vim.fail["ClearTriggeredAlarms"] = RuntimeError("vmware vim ClearTriggeredAlarms failed")
    with pytest.raises(RuntimeError, match="ClearTriggeredAlarms"):
        await _reset(vim, ["red"], "all", "all")


@pytest.mark.parametrize(
    "params",
    [
        {"status": [], "object_kind": "all", "trigger_kind": "all"},
        {"status": ["green"], "object_kind": "all", "trigger_kind": "all"},
        {"status": ["red", "red"], "object_kind": "all", "trigger_kind": "all"},
        {"status": ["red"], "object_kind": "cluster", "trigger_kind": "all"},
        {"status": ["red"], "object_kind": "all", "trigger_kind": "state"},
        {"status": ["red"], "object_kind": "all"},
    ],
)
def test_reset_refuses_a_wrong_filter_before_any_call(params: dict[str, Any]) -> None:
    assert validate_params(ALARM_RESET_PARAMETER_SCHEMA, params)


def test_reset_example_validates() -> None:
    schema = ALARM_RESET_PARAMETER_SCHEMA
    assert validate_params(schema, schema["examples"][0]) == []


# ---------------------------------------------------------------------------
# Approval previews
# ---------------------------------------------------------------------------


def _ctx(params: dict[str, Any], connector: Any) -> PreviewContext:
    return PreviewContext(
        descriptor=object(),  # type: ignore[arg-type]  # the builders do not read it
        connector_instance=connector,
        operator=_operator(),
        target=_Target(),
        params=params,
    )


async def test_acknowledge_preview_shows_the_alarm_as_it_is_now() -> None:
    vim = _seeded()
    out = await alarm_acknowledge_preview(
        _ctx({"alarm_id": "alarm-101", "entity_id": "host-201"}, vim)
    )
    assert out is not None
    assert out["action"] == "acknowledge_alarm"
    assert out["alarm"]["alarm_name"] == "Test event alarm A"
    assert out["alarm"]["entity_name"] == "host-a"
    assert vim.calls_to("AcknowledgeAlarm") == []

    echo = await alarm_acknowledge_preview(_ctx({"alarm_id": "alarm-1", "entity_id": "x"}, None))
    assert echo == {"action": "acknowledge_alarm", "alarm_id": "alarm-1", "entity_id": "x"}
    assert await alarm_acknowledge_preview(_ctx({"alarm_id": 5}, vim)) is None


async def test_reset_preview_lists_the_matching_alarms_with_a_cap() -> None:
    vim = _seeded()
    out = await alarm_reset_preview(
        _ctx({"status": ["red"], "object_kind": "host", "trigger_kind": "all"}, vim)
    )
    assert out is not None
    assert out["total_matching"] == 2
    assert _keys(out["matching_now"]) == [("alarm-101", "host-202"), ("alarm-101", "host-201")]
    assert "not only one" in out["note"]
    assert vim.calls_to("ClearTriggeredAlarms") == []

    many = _seeded()
    many.states = [
        _state("alarm-101", "HostSystem", f"host-{n}", "red", f"2026-01-01T08:{n:02d}:00Z")
        for n in range(30)
    ]
    many.names = {("HostSystem", f"host-{n}"): f"host-{n}" for n in range(30)}
    capped = await alarm_reset_preview(
        _ctx({"status": ["red"], "object_kind": "all", "trigger_kind": "all"}, many)
    )
    assert capped is not None
    assert capped["total_matching"] == 30
    assert len(capped["matching_now"]) == _alarm._PREVIEW_CAP


async def test_reset_preview_without_a_connector_echoes_the_filter() -> None:
    params = {"status": ["yellow", "red"], "object_kind": "vm", "trigger_kind": "metric"}
    out = await alarm_reset_preview(_ctx(params, None))
    assert out is not None
    assert out["filter"] == {
        "status": ["red", "yellow"],
        "object_kind": "vm",
        "trigger_kind": "metric",
    }
    assert "matching_now" not in out
    assert await alarm_reset_preview(_ctx({"status": ["red"]}, None)) is None


def test_both_changes_have_a_preview_builder_and_a_child_op_list() -> None:
    assert _PREVIEW_BUILDERS[_ACK_OP] is alarm_acknowledge_preview
    assert _PREVIEW_BUILDERS[_RESET_OP] is alarm_reset_preview
    assert _GOVERNED_SUBOP_MANIFEST[_ACK_OP] == (
        "POST:/ServiceInstance/{moId}/RetrieveServiceContent",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        _ACK_SUB_OP,
    )
    assert _GOVERNED_SUBOP_MANIFEST[_RESET_OP] == (
        "POST:/ServiceInstance/{moId}/RetrieveServiceContent",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        _CLEAR_SUB_OP,
    )


# ---------------------------------------------------------------------------
# Registration: both catalogs, risk level, approval, contract rules
# ---------------------------------------------------------------------------


@pytest.fixture
def _db_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


async def _register_all() -> None:
    stub = _embedding_stub()
    await register_vmware_composite_operations(embedding_service=stub)
    await register_vmware_composite_operations_8_0(embedding_service=stub)
    await register_vmware_typed_operations(embedding_service=stub)
    await register_vmware_typed_operations_8_0(embedding_service=stub)


@pytest.mark.usefixtures("_db_env")
@pytest.mark.parametrize(
    ("op_id", "source_kind", "safety_level", "requires_approval"),
    [
        (_LIST_OP, "typed", "safe", False),
        (_ACK_OP, "composite", "caution", True),
        (_RESET_OP, "composite", "caution", True),
    ],
)
async def test_each_op_is_on_both_catalogs_with_its_risk_and_contract(
    op_id: str, source_kind: str, safety_level: str, requires_approval: bool
) -> None:
    await _register_all()
    async with get_sessionmaker()() as s:
        rows = (
            (await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)))
            .scalars()
            .all()
        )
        assert {row.version for row in rows} == {"9.0", "8.0"}
        for row in rows:
            assert row.source_kind == source_kind
            assert row.safety_level == safety_level
            assert row.requires_approval is requires_approval
            group = (
                await s.execute(select(OperationGroup).where(OperationGroup.id == row.group_id))
            ).scalar_one()
            assert group.group_key == ALARMS_GROUP_KEY
            assert group.when_to_use == ALARMS_WHEN_TO_USE
            contract = OperationContract(
                connector="vmware-rest",
                op_id=row.op_id,
                summary=row.summary,
                description=row.description,
                parameter_schema=dict(row.parameter_schema),
                response_schema=row.response_schema,
                llm_instructions=row.llm_instructions,
                safety_level=row.safety_level,
                requires_approval=row.requires_approval,
                group_text=group.when_to_use,
            )
            assert check_operation(contract) == {}


def test_the_typed_read_is_in_the_typed_op_tuple() -> None:
    assert VMWARE_ALARM_TRIGGERED_LIST_OP in VMWARE_TYPED_OPS
    assert hasattr(VmwareRestConnector, VMWARE_ALARM_TRIGGERED_LIST_OP.handler_attr)


# ---------------------------------------------------------------------------
# End to end through the dispatcher: a person's reset waits for approval
# ---------------------------------------------------------------------------

_TARGET_NAME = "example-vcenter"


async def _bootstrap(vim: _AlarmVim) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = vim  # type: ignore[assignment]
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
                notes="seeded by test_connectors_vmware_rest_alarms",
            )
        )
        await s.commit()


@pytest.mark.usefixtures("_db_env")
async def test_a_reset_waits_for_approval_then_runs_once() -> None:
    vim = _seeded()
    await _bootstrap(vim)
    requester = _operator("op-requester")
    args = {
        "connector_id": "vmware-rest-9.0",
        "op_id": _RESET_OP,
        "target": _TARGET_NAME,
        "params": {"status": ["red"], "object_kind": "host", "trigger_kind": "event"},
    }

    call = await call_operation(requester, args)
    assert call["status"] == "awaiting_approval", call
    assert vim.calls_to("ClearTriggeredAlarms") == []
    request_id = UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "caution"
    assert effect["preview"]["total_matching"] == 2

    approver = _operator("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "reset"
    assert _keys(resume.result["cleared"]) == [("alarm-101", "host-202"), ("alarm-101", "host-201")]
    assert len(vim.calls_to("ClearTriggeredAlarms")) == 1


@pytest.mark.usefixtures("_db_env")
async def test_an_agent_acknowledge_waits_for_approval() -> None:
    vim = _seeded()
    await _bootstrap(vim)
    agent = _operator("agent-1", PrincipalKind.AGENT)
    call = await call_operation(
        agent,
        {
            "connector_id": "vmware-rest-9.0",
            "op_id": _ACK_OP,
            "target": _TARGET_NAME,
            "params": {"alarm_id": "alarm-101", "entity_id": "host-201"},
        },
    )
    assert call["status"] == "awaiting_approval", call
    assert vim.calls_to("AcknowledgeAlarm") == []
