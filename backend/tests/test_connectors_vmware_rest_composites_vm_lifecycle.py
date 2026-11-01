# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the VM template, register and instant clone changes.

``vm.template.mark``, ``vm.template.unmark``, ``vm.register``,
``vm.unregister`` and ``vm.instant_clone``. Three layers:

* the handlers and previews against the shared vim fake, extended here with
  the five vendor calls: the exact request bodies, the gated write, the task
  poll, the read after the change, every stop before a write, a vendor fault,
  a task timeout and a parked gate;
* the transport: a VI-JSON change and the REST instant clone land on the right
  path for a vCenter 8.0 U3 and a vCenter 9.0 target;
* the governed flow through the real dispatcher: each change waits for a
  second person; the registered risk levels match the table of the task.
"""

from __future__ import annotations

import copy
import json as jsonlib
import re
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
    _vm_instant_clone,
    _vm_lifecycle,
    _vm_register,
    _vm_template,
    _write,
    register_vmware_composite_operations,
)
from meho_backplane.connectors.vmware_rest.composites._vm_lifecycle_schemas import (
    VM_INSTANT_CLONE_PARAMETER_SCHEMA,
    VM_INSTANT_CLONE_RESPONSE_SCHEMA,
    VM_REGISTER_PARAMETER_SCHEMA,
    VM_REGISTER_RESPONSE_SCHEMA,
    VM_TEMPLATE_MARK_PARAMETER_SCHEMA,
    VM_TEMPLATE_MARK_RESPONSE_SCHEMA,
    VM_TEMPLATE_UNMARK_PARAMETER_SCHEMA,
    VM_TEMPLATE_UNMARK_RESPONSE_SCHEMA,
    VM_UNREGISTER_PARAMETER_SCHEMA,
    VM_UNREGISTER_RESPONSE_SCHEMA,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation
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

_VM = "vm-42"
_NEW_VM = "vm-77"
_VMX = "[<datastore-name>] app-vm/app-vm.vmx"
_MARK_OP = "vmware.composite.vm.template.mark"
_UNMARK_OP = "vmware.composite.vm.template.unmark"
_REGISTER_OP = "vmware.composite.vm.register"
_UNREGISTER_OP = "vmware.composite.vm.unregister"
_CLONE_OP = "vmware.composite.vm.instant_clone"
_ALL_OPS = (_MARK_OP, _UNMARK_OP, _REGISTER_OP, _UNREGISTER_OP, _CLONE_OP)
_CLONE_PATH = "/vcenter/vm?action=instant-clone"
_PARAMETER_SCHEMAS = {
    _MARK_OP: VM_TEMPLATE_MARK_PARAMETER_SCHEMA,
    _UNMARK_OP: VM_TEMPLATE_UNMARK_PARAMETER_SCHEMA,
    _REGISTER_OP: VM_REGISTER_PARAMETER_SCHEMA,
    _UNREGISTER_OP: VM_UNREGISTER_PARAMETER_SCHEMA,
    _CLONE_OP: VM_INSTANT_CLONE_PARAMETER_SCHEMA,
}
#: A value that looks like a real vCenter id (moid), the kind an example must not hold.
_REAL_ID = re.compile(r"^(vm|host|group-[a-z]|resgroup-v?|datastore|domain-c)[0-9]+$")
_PLACEHOLDER = re.compile(r"<[a-z-]+>")


class LifecycleVimFake(VimFake):
    """The shared vim fake plus the five vendor calls of this module.

    Each call changes the store the way vCenter does, so the reads after it
    see the result. The REST instant clone answers through ``_post_json``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rest_calls: list[tuple[str, Any]] = []
        self.rest_fault: Exception | None = None

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
        session_used: Any = None,
        session_token: str | None = None,
    ) -> Any:
        _mo_type, moid, method = path.strip("/").split("/")
        if method == "MarkAsTemplate":
            self.calls.append((path, copy.deepcopy(json)))
            return self._sync(lambda: self._vm(moid).update({"config.template": True}))
        if method == "MarkAsVirtualMachine":
            self.calls.append((path, copy.deepcopy(json)))
            return self._sync(lambda: self._unmark(moid, json))
        if method == "UnregisterVM":
            self.calls.append((path, copy.deepcopy(json)))
            return self._sync(lambda: self.objects.pop(("VirtualMachine", moid)))
        if method == "RegisterVM_Task":
            self.calls.append((path, copy.deepcopy(json)))
            return self._task(
                method,
                apply=lambda: self._register(moid, json),
                result=moref("VirtualMachine", _NEW_VM),
            )
        return await super()._post_vmomi_json(
            target,
            path,
            operator=operator,
            json=json,
            promote_managed_object_not_found=promote_managed_object_not_found,
            session_used=session_used,
            session_token=session_token,
        )

    def _vm(self, moid: str) -> dict[str, Any]:
        return self.objects[("VirtualMachine", moid)]

    def _unmark(self, moid: str, body: dict[str, Any]) -> None:
        props = self._vm(moid)
        props["config.template"] = False
        props["resourcePool"] = body["pool"]
        if "host" in body:
            props["runtime.host"] = body["host"]

    def _register(self, folder: str, body: dict[str, Any]) -> None:
        props: dict[str, Any] = {
            "name": body.get("name", "app-vm"),
            "runtime.powerState": "poweredOff",
            "config.template": body["asTemplate"],
            "config.files.vmPathName": body["path"],
            "parent": moref("Folder", folder),
        }
        if "pool" in body:
            props["resourcePool"] = body["pool"]
        self.add("VirtualMachine", _NEW_VM, **props)

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def _post_json(
        self, target: Any, path: str, *, operator: Operator, verb: str = "POST", **kwargs: Any
    ) -> Any:
        self.rest_calls.append((path, copy.deepcopy(kwargs.get("json"))))
        if self.rest_fault is not None:
            raise self.rest_fault
        assert path == f"/api{_CLONE_PATH}", path
        source = self._vm(kwargs["json"]["source"])
        if self.apply_writes:
            self.add(
                "VirtualMachine",
                _NEW_VM,
                **{**copy.deepcopy(source), "name": kwargs["json"]["name"]},
            )
        return _NEW_VM


def _vm(conn: LifecycleVimFake, *, power: str = "poweredOff", template: bool = False) -> None:
    props: dict[str, Any] = {
        "name": "app-vm",
        "runtime.powerState": power,
        "config.template": template,
        "config.files.vmPathName": _VMX,
        "parent": moref("Folder", "group-v3"),
        "runtime.host": moref("HostSystem", "host-12"),
    }
    if not template:
        props["resourcePool"] = moref("ResourcePool", "resgroup-8")
    conn.add("VirtualMachine", _VM, **props)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _call(handler: Any, conn: Any, params: dict[str, Any]) -> Any:
    return await handler(operator=operator(), target=object(), params=params, connector=conn)


def _vim_fault(fault_type: str, text: str) -> httpx.HTTPStatusError:
    return http_status_error(500, jsonlib.dumps({"_typeName": fault_type, "faultstring": text}))


# ===========================================================================
# vm.template.mark / vm.template.unmark
# ===========================================================================


async def test_mark_happy_path(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn)
    out = await _call(_vm_template.vm_template_mark_composite, conn, {"vm": _VM})

    assert out["status"] == "marked"
    assert out["before"]["is_template"] is False
    assert out["after"]["is_template"] is True
    assert out["after"]["vmx_path"] == _VMX
    assert [c["op_id"] for c in gate.calls] == ["POST:/VirtualMachine/{moId}/MarkAsTemplate"]
    assert conn.calls_to("MarkAsTemplate") == [(f"/VirtualMachine/{_VM}/MarkAsTemplate", {})]
    assert conn.methods() == ["RetrievePropertiesEx", "MarkAsTemplate", "RetrievePropertiesEx"]
    Draft202012Validator(VM_TEMPLATE_MARK_RESPONSE_SCHEMA).validate(out)


async def test_mark_a_template_is_unchanged(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn, template=True)
    out = await _call(_vm_template.vm_template_mark_composite, conn, {"vm": _VM})
    assert out["status"] == "unchanged"
    assert gate.calls == []


async def test_mark_a_running_vm_stops_before_any_write(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn, power="poweredOn")
    out = await _call(_vm_template.vm_template_mark_composite, conn, {"vm": _VM})
    assert out["status"] == "precondition_failed"
    assert "power it off" in out["guidance"]
    assert gate.calls == []
    assert conn.calls_to("MarkAsTemplate") == []
    Draft202012Validator(VM_TEMPLATE_MARK_RESPONSE_SCHEMA).validate(out)


async def test_mark_unknown_vm_and_bad_id(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    out = await _call(_vm_template.vm_template_mark_composite, conn, {"vm": _VM})
    assert out["status"] == "not_found"
    out = await _call(_vm_template.vm_template_mark_composite, conn, {"vm": "app-vm"})
    assert out["status"] == "invalid_request"
    assert gate.calls == []


async def test_mark_vendor_fault_reaches_the_error(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn)
    conn.sync_fault = _vim_fault("InvalidPowerState", "The VM is not powered off.")
    with pytest.raises(RuntimeError, match=r"MarkAsTemplate.*InvalidPowerState.*not powered off"):
        await _call(_vm_template.vm_template_mark_composite, conn, {"vm": _VM})


async def test_mark_that_does_not_read_back_is_not_confirmed(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn)
    conn.apply_writes = False
    out = await _call(_vm_template.vm_template_mark_composite, conn, {"vm": _VM})
    assert out["status"] == "not_confirmed"


async def test_mark_returns_a_parked_gate_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = LifecycleVimFake()
    _vm(conn)
    verdict = parked("POST:/VirtualMachine/{moId}/MarkAsTemplate")
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
    out = await _call(_vm_template.vm_template_mark_composite, conn, {"vm": _VM})
    assert out is verdict
    assert conn.calls_to("MarkAsTemplate") == []


async def test_unmark_happy_path_sends_pool_and_host(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn, template=True)
    params = {"vm": _VM, "resource_pool": "resgroup-9", "host": "host-13"}
    out = await _call(_vm_template.vm_template_unmark_composite, conn, params)

    assert out["status"] == "unmarked"
    assert out["after"]["is_template"] is False
    assert out["after"]["resource_pool"] == "resgroup-9"
    assert out["after"]["host"] == "host-13"
    assert gate.calls[0]["params"]["resource_pool"] == "resgroup-9"
    [(path, body)] = conn.calls_to("MarkAsVirtualMachine")
    assert path == f"/VirtualMachine/{_VM}/MarkAsVirtualMachine"
    assert body == {
        "pool": moref("ResourcePool", "resgroup-9"),
        "host": moref("HostSystem", "host-13"),
    }
    Draft202012Validator(VM_TEMPLATE_UNMARK_RESPONSE_SCHEMA).validate(out)


async def test_unmark_without_host_sends_only_the_pool(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn, template=True)
    params = {"vm": _VM, "resource_pool": "resgroup-8"}
    Draft202012Validator(VM_TEMPLATE_UNMARK_PARAMETER_SCHEMA).validate(params)
    await _call(_vm_template.vm_template_unmark_composite, conn, params)
    [(_path, body)] = conn.calls_to("MarkAsVirtualMachine")
    assert set(body) == {"pool"}


async def test_unmark_a_vm_is_unchanged(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn)
    params = {"vm": _VM, "resource_pool": "resgroup-8"}
    out = await _call(_vm_template.vm_template_unmark_composite, conn, params)
    assert out["status"] == "unchanged"
    assert gate.calls == []


async def test_unmark_refuses_a_bad_pool_id(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn, template=True)
    params = {"vm": _VM, "resource_pool": "Resources"}
    out = await _call(_vm_template.vm_template_unmark_composite, conn, params)
    assert out["status"] == "invalid_request"
    assert conn.calls == []


async def test_unmark_vendor_fault_reaches_the_error(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn, template=True)
    conn.sync_fault = _vim_fault("InvalidArgument", "The host is not in the pool's cluster.")
    params = {"vm": _VM, "resource_pool": "resgroup-8", "host": "host-99"}
    with pytest.raises(RuntimeError, match=r"InvalidArgument.*not in the pool"):
        await _call(_vm_template.vm_template_unmark_composite, conn, params)


# ===========================================================================
# vm.register
# ===========================================================================


async def test_register_a_vm_happy_path(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    params = {
        "vmx_path": _VMX,
        "folder": "group-v3",
        "name": "app-vm-restored",
        "resource_pool": "resgroup-8",
    }
    Draft202012Validator(VM_REGISTER_PARAMETER_SCHEMA).validate(params)
    out = await _call(_vm_register.vm_register_composite, conn, params)

    assert out["status"] == "registered"
    assert out["vm"] == _NEW_VM
    assert out["task"] == "task-1"
    assert out["after"]["name"] == "app-vm-restored"
    assert out["after"]["is_template"] is False
    assert [c["op_id"] for c in gate.calls] == ["POST:/Folder/{moId}/RegisterVM_Task"]
    [(path, body)] = conn.calls_to("RegisterVM_Task")
    assert path == "/Folder/group-v3/RegisterVM_Task"
    assert body == {
        "path": _VMX,
        "asTemplate": False,
        "name": "app-vm-restored",
        "pool": moref("ResourcePool", "resgroup-8"),
    }
    Draft202012Validator(VM_REGISTER_RESPONSE_SCHEMA).validate(out)


async def test_register_a_template_sends_host_and_no_pool(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    params = {
        "vmx_path": "[<datastore-name>] tpl/tpl.vmtx",
        "folder": "group-v3",
        "as_template": True,
        "host": "host-12",
    }
    Draft202012Validator(VM_REGISTER_PARAMETER_SCHEMA).validate(params)
    out = await _call(_vm_register.vm_register_composite, conn, params)
    assert out["status"] == "registered"
    assert out["after"]["is_template"] is True
    [(_path, body)] = conn.calls_to("RegisterVM_Task")
    assert body["asTemplate"] is True
    assert "pool" not in body
    assert body["host"]["value"] == "host-12"


@pytest.mark.parametrize(
    ("params", "fragment", "schema_rejects"),
    [
        (
            {"vmx_path": "app-vm.vmx", "folder": "group-v3", "resource_pool": "resgroup-8"},
            "vmx_path",
            True,
        ),
        # The id fields carry no pattern (their examples are placeholders): the handler
        # checks the shape, the schema does not.
        ({"vmx_path": _VMX, "folder": "group-h4", "resource_pool": "resgroup-8"}, "folder", False),
        ({"vmx_path": _VMX, "folder": "group-v3"}, "needs resource_pool", True),
        ({"vmx_path": _VMX, "folder": "group-v3", "as_template": True}, "needs host", True),
        (
            {
                "vmx_path": _VMX,
                "folder": "group-v3",
                "as_template": True,
                "host": "host-1",
                "resource_pool": "resgroup-8",
            },
            "no resource_pool",
            True,
        ),
    ],
)
async def test_register_refuses_bad_inputs_before_any_write(
    gate: GateRecorder, params: dict[str, Any], fragment: str, schema_rejects: bool
) -> None:
    conn = LifecycleVimFake()
    out = await _call(_vm_register.vm_register_composite, conn, params)
    assert out["status"] == "invalid_request"
    assert fragment in out["guidance"]
    assert gate.calls == []
    assert conn.calls == []
    assert Draft202012Validator(VM_REGISTER_PARAMETER_SCHEMA).is_valid(params) is not schema_rejects


async def test_register_task_fault_reaches_the_error(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    conn.task_faults["RegisterVM_Task"] = ("AlreadyExists", "The VM is already registered.")
    params = {"vmx_path": _VMX, "folder": "group-v3", "resource_pool": "resgroup-8"}
    with pytest.raises(RuntimeError, match=r"AlreadyExists.*already registered"):
        await _call(_vm_register.vm_register_composite, conn, params)


async def test_register_timeout(gate: GateRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_vm_lifecycle, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = LifecycleVimFake()
    conn.running.add("RegisterVM_Task")
    params = {"vmx_path": _VMX, "folder": "group-v3", "resource_pool": "resgroup-8"}
    out = await _call(_vm_register.vm_register_composite, conn, params)
    assert out["status"] == "timeout"
    assert out["task"] == "task-1"
    assert [i["severity"] for i in out["issues"]] == ["error"]
    Draft202012Validator(VM_REGISTER_RESPONSE_SCHEMA).validate(out)


# ===========================================================================
# vm.unregister
# ===========================================================================


async def test_unregister_happy_path_keeps_the_way_back(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn)
    out = await _call(_vm_register.vm_unregister_composite, conn, {"vm": _VM})

    assert out["status"] == "unregistered"
    assert out["before"]["vmx_path"] == _VMX
    assert out["before"]["folder"] == "group-v3"
    assert _VMX in out["guidance"]
    assert [c["op_id"] for c in gate.calls] == ["POST:/VirtualMachine/{moId}/UnregisterVM"]
    assert conn.calls_to("UnregisterVM") == [(f"/VirtualMachine/{_VM}/UnregisterVM", {})]
    Draft202012Validator(VM_UNREGISTER_RESPONSE_SCHEMA).validate(out)


async def test_unregister_a_running_vm_stops_before_any_write(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn, power="poweredOn")
    out = await _call(_vm_register.vm_unregister_composite, conn, {"vm": _VM})
    assert out["status"] == "precondition_failed"
    assert gate.calls == []


async def test_unregister_unknown_vm_is_not_found(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    out = await _call(_vm_register.vm_unregister_composite, conn, {"vm": _VM})
    assert out["status"] == "not_found"
    assert gate.calls == []


async def test_unregister_that_does_not_read_back_is_still_present(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn)
    conn.apply_writes = False
    out = await _call(_vm_register.vm_unregister_composite, conn, {"vm": _VM})
    assert out["status"] == "still_present"


async def test_unregister_vendor_fault_reaches_the_error(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn)
    conn.sync_fault = _vim_fault("InvalidState", "The VM is busy.")
    with pytest.raises(RuntimeError, match=r"UnregisterVM.*InvalidState.*busy"):
        await _call(_vm_register.vm_unregister_composite, conn, {"vm": _VM})


# ===========================================================================
# vm.instant_clone
# ===========================================================================


async def test_instant_clone_happy_path(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn, power="poweredOn")
    params = {
        "source_vm": _VM,
        "name": "app-vm-ic1",
        "folder": "group-v3",
        "datastore": "datastore-11",
        "disconnect_all_nics": True,
    }
    Draft202012Validator(VM_INSTANT_CLONE_PARAMETER_SCHEMA).validate(params)
    out = await _call(_vm_instant_clone.vm_instant_clone_composite, conn, params)

    assert out["status"] == "cloned"
    assert out["vm"] == _NEW_VM
    assert out["after"]["name"] == "app-vm-ic1"
    assert out["after"]["power_state"] == "poweredOn"
    assert [c["op_id"] for c in gate.calls] == [f"POST:{_CLONE_PATH}"]
    assert conn.rest_calls == [
        (
            f"/api{_CLONE_PATH}",
            {
                "source": _VM,
                "name": "app-vm-ic1",
                "placement": {"folder": "group-v3", "datastore": "datastore-11"},
                "disconnect_all_nics": True,
            },
        )
    ]
    Draft202012Validator(VM_INSTANT_CLONE_RESPONSE_SCHEMA).validate(out)


async def test_instant_clone_of_a_stopped_vm_stops_before_any_write(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn)
    out = await _call(
        _vm_instant_clone.vm_instant_clone_composite, conn, {"source_vm": _VM, "name": "c1"}
    )
    assert out["status"] == "precondition_failed"
    assert "running source VM" in out["guidance"]
    assert gate.calls == []
    assert conn.rest_calls == []


async def test_instant_clone_refuses_bad_placement(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn, power="poweredOn")
    params = {"source_vm": _VM, "name": "c1", "datastore": "ds1"}
    out = await _call(_vm_instant_clone.vm_instant_clone_composite, conn, params)
    assert out["status"] == "invalid_request"
    assert conn.calls == []


async def test_instant_clone_vendor_fault_reaches_the_error(gate: GateRecorder) -> None:
    conn = LifecycleVimFake()
    _vm(conn, power="poweredOn")
    request = httpx.Request("POST", "https://vc.example.test/api/vcenter/vm")
    body = {
        "error_type": "ALREADY_EXISTS",
        "messages": [{"default_message": "A VM named c1 already exists."}],
    }
    conn.rest_fault = httpx.HTTPStatusError(
        "400", request=request, response=httpx.Response(400, request=request, json=body)
    )
    with pytest.raises(RuntimeError, match=r"HTTP 400 \(ALREADY_EXISTS\): A VM named c1"):
        await _call(
            _vm_instant_clone.vm_instant_clone_composite, conn, {"source_vm": _VM, "name": "c1"}
        )


async def test_instant_clone_returns_a_parked_gate_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = LifecycleVimFake()
    _vm(conn, power="poweredOn")
    verdict = parked(f"POST:{_CLONE_PATH}")
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
    out = await _call(
        _vm_instant_clone.vm_instant_clone_composite, conn, {"source_vm": _VM, "name": "c1"}
    )
    assert out is verdict
    assert conn.rest_calls == []


# ===========================================================================
# Previews (what the approver sees)
# ===========================================================================


@pytest.mark.parametrize(
    ("op_id", "params", "vm_kwargs", "fragment"),
    [
        (_MARK_OP, {"vm": _VM}, {}, "into a template"),
        (_UNMARK_OP, {"vm": _VM, "resource_pool": "resgroup-8"}, {"template": True}, "back into"),
        (_UNREGISTER_OP, {"vm": _VM}, {}, "files stay"),
        (_CLONE_OP, {"source_vm": _VM, "name": "c1"}, {"power": "poweredOn"}, "instant clone"),
        (
            _REGISTER_OP,
            {"vmx_path": _VMX, "folder": "group-v3", "resource_pool": "resgroup-8"},
            {},
            "Register the VM",
        ),
    ],
)
async def test_previews_show_the_change(
    op_id: str, params: dict[str, Any], vm_kwargs: dict[str, Any], fragment: str
) -> None:
    conn = LifecycleVimFake()
    _vm(conn, **vm_kwargs)
    preview = await _PREVIEW_BUILDERS[op_id](preview_ctx(params, conn))
    assert preview is not None
    assert fragment in preview["summary"]
    assert "stops_with" not in preview
    assert conn.calls_to("MarkAsTemplate") == []
    assert conn.rest_calls == []


async def test_preview_names_the_stop() -> None:
    conn = LifecycleVimFake()
    _vm(conn, power="poweredOn")
    preview = await _PREVIEW_BUILDERS[_UNREGISTER_OP](preview_ctx({"vm": _VM}, conn))
    assert preview is not None
    assert preview["stops_with"] == "precondition_failed"


# ===========================================================================
# Examples: placeholders, never ids that look real
# ===========================================================================


@pytest.mark.parametrize("op_id", _ALL_OPS)
def test_examples_hold_placeholders_and_validate(op_id: str) -> None:
    """Every example validates, and every string in it is a placeholder.

    A model copies an example value that looks real (``vm-42``) as a real
    target. So the examples hold ``<vm-id>``-style placeholders, and the id
    fields carry no ``pattern`` that such a placeholder would fail.
    """
    schema = _PARAMETER_SCHEMAS[op_id]
    examples = schema["examples"]
    assert examples
    for example in examples:
        assert validate_params(schema, example) == []
        for key, value in example.items():
            if isinstance(value, str):
                assert _PLACEHOLDER.search(value), (op_id, key, value)
                assert not _REAL_ID.match(value), (op_id, key, value)


@pytest.mark.parametrize(
    ("handler", "params"),
    [
        (_vm_template.vm_template_mark_composite, {"vm": "<vm-id>"}),
        (
            _vm_template.vm_template_unmark_composite,
            {"vm": "<template-id>", "resource_pool": "<resource-pool-id>"},
        ),
        (
            _vm_register.vm_register_composite,
            {"vmx_path": _VMX, "folder": "<vm-folder-id>", "resource_pool": "<resource-pool-id>"},
        ),
        (_vm_register.vm_unregister_composite, {"vm": "<vm-id>"}),
        (_vm_instant_clone.vm_instant_clone_composite, {"source_vm": "<vm-id>", "name": "c1"}),
    ],
)
async def test_a_placeholder_sent_as_is_stops_before_any_write(
    gate: GateRecorder, handler: Any, params: dict[str, Any]
) -> None:
    """The schema lets a placeholder through; the handler refuses it and sends nothing."""
    conn = LifecycleVimFake()
    _vm(conn, power="poweredOn")
    out = await _call(handler, conn, params)
    assert out["status"] == "invalid_request"
    assert gate.calls == []
    assert conn.calls == []
    assert conn.rest_calls == []


# ===========================================================================
# Transport: the vCenter 8.0 and 9.0 paths
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


def _retrieve_answer(request: httpx.Request, answers: list[dict[str, Any]]) -> httpx.Response:
    body = jsonlib.loads(request.content)
    moid = body["specSet"][0]["objectSet"][0]["obj"]["value"]
    props = answers.pop(0)
    prop_set = [{"name": k, "val": v} for k, v in props.items()]
    return httpx.Response(
        200, json=[{"obj": {"type": "VirtualMachine", "value": moid}, "propSet": prop_set}]
    )


_LIVE_VM = {
    "name": "app-vm",
    "runtime.powerState": "poweredOff",
    "config.template": False,
    "config.files.vmPathName": _VMX,
}


@pytest.mark.parametrize(("about", "release"), [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")])
async def test_mark_lands_on_the_vi_json_path_of_each_vcenter_version(
    gate: GateRecorder, about: str, release: str
) -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    _patch_no_revoke_aclose(connector)
    answers = [dict(_LIVE_VM), {**_LIVE_VM, "config.template": True}]
    base = f"/sdk/vim25/{release}"
    try:
        async with respx.mock(base_url="https://vc.test.invalid") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": about})
            mock.post(f"{base}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=lambda request: _retrieve_answer(request, answers)
            )
            mark = mock.post(f"{base}/VirtualMachine/{_VM}/MarkAsTemplate").respond(204)
            out = await _vm_template.vm_template_mark_composite(
                operator=operator(), target=_StubTarget(), params={"vm": _VM}, connector=connector
            )
        assert mark.call_count == 1
        assert out["status"] == "marked"
    finally:
        await connector.aclose()


@pytest.mark.parametrize(("about", "release"), [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")])
async def test_instant_clone_lands_on_the_rest_path_of_each_vcenter_version(
    gate: GateRecorder, about: str, release: str
) -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    _patch_no_revoke_aclose(connector)
    running = {**_LIVE_VM, "runtime.powerState": "poweredOn"}
    answers = [running, {**running, "name": "c1"}]
    base = f"/sdk/vim25/{release}"
    try:
        async with respx.mock(base_url="https://vc.test.invalid") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": about})
            mock.post(f"{base}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=lambda request: _retrieve_answer(request, answers)
            )
            clone = mock.post("/api/vcenter/vm", params={"action": "instant-clone"}).respond(
                200, json=_NEW_VM
            )
            out = await _vm_instant_clone.vm_instant_clone_composite(
                operator=operator(),
                target=_StubTarget(),
                params={"source_vm": _VM, "name": "c1"},
                connector=connector,
            )
        assert clone.call_count == 1
        assert jsonlib.loads(clone.calls[0].request.content) == {"source": _VM, "name": "c1"}
        assert out["status"] == "cloned"
        assert out["vm"] == _NEW_VM
    finally:
        await connector.aclose()


# ===========================================================================
# The governed flow through the dispatcher (approval gate)
# ===========================================================================

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004340")
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
        name="VM Lifecycle Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _bootstrap(conn: LifecycleVimFake) -> None:
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
                notes="seeded by test_connectors_vmware_rest_composites_vm_lifecycle",
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
    await _bootstrap(LifecycleVimFake())
    async with get_sessionmaker()() as s:
        rows = {
            row.op_id: row
            for row in (
                await s.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_ALL_OPS))
                )
            )
            .scalars()
            .all()
        }
    assert {op: rows[op].safety_level for op in _ALL_OPS} == {
        _MARK_OP: "dangerous",
        _UNMARK_OP: "dangerous",
        _REGISTER_OP: "caution",
        _UNREGISTER_OP: "dangerous",
        _CLONE_OP: "dangerous",
    }
    assert all(rows[op].requires_approval is True for op in _ALL_OPS)


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.parametrize(
    ("op_id", "params", "vm_kwargs", "status", "method"),
    [
        (_MARK_OP, {"vm": _VM}, {}, "marked", "MarkAsTemplate"),
        (
            _UNMARK_OP,
            {"vm": _VM, "resource_pool": "resgroup-8"},
            {"template": True},
            "unmarked",
            "MarkAsVirtualMachine",
        ),
        (
            _REGISTER_OP,
            {"vmx_path": _VMX, "folder": "group-v3", "resource_pool": "resgroup-8"},
            {},
            "registered",
            "RegisterVM_Task",
        ),
        (_UNREGISTER_OP, {"vm": _VM}, {}, "unregistered", "UnregisterVM"),
    ],
)
async def test_a_change_waits_for_a_second_person_then_runs_once(
    op_id: str, params: dict[str, Any], vm_kwargs: dict[str, Any], status: str, method: str
) -> None:
    conn = LifecycleVimFake()
    if op_id != _REGISTER_OP:
        _vm(conn, **vm_kwargs)
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
async def test_instant_clone_waits_for_a_second_person_then_runs_once() -> None:
    conn = LifecycleVimFake()
    _vm(conn, power="poweredOn")
    await _bootstrap(conn)
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _CLONE_OP,
        "target": _TARGET_NAME,
        "params": {"source_vm": _VM, "name": "c1"},
    }
    call = await call_operation(_person("op-requester"), args)
    assert call["status"] == "awaiting_approval", call
    assert conn.rest_calls == []
    resume = await _approve_and_resume(UUID(call["extras"]["approval_request_id"]))
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "cloned"
    assert len(conn.rest_calls) == 1


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_without_a_grant_cannot_unregister() -> None:
    conn = LifecycleVimFake()
    _vm(conn)
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_UNREGISTER_OP,
        target=_FakeTarget(),
        params={"vm": _VM},
    )
    assert result.status == "denied", result
    assert conn.calls_to("UnregisterVM") == []
    assert await _approval_count() == 0
