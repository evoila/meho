# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two VM device changes: connect or disconnect a network adapter, and add a serial port.

* ``vmware.composite.vm.nic.connect`` (``dangerous``) -- reads
  ``GET /vcenter/vm/{vm}/hardware/ethernet/{nic}`` first. When the adapter is
  already in the asked state, nothing is sent (``unchanged``). Else
  ``POST .../ethernet/{nic}?action=connect`` (or ``?action=disconnect``), then
  it reads the adapter again. vCenter does this only on a powered-on VM.
* ``vmware.composite.vm.serial_port.add`` (``dangerous``) --
  ``POST /vcenter/vm/{vm}/hardware/serial`` with a top-level
  ``Serial.CreateSpec``: the port writes to a file on a datastore, or listens
  on (or connects to) a network address. It reads the new port back with
  ``GET .../serial/{port}``. vCenter does this only on a powered-off VM.

Both wait for approval. Both take the VM id only (no VM name), so after the
approval the change runs on the VM the approver saw. Both call vSphere REST on
the connector session through the shared write seam
(:func:`._write._write_sub_op`), so they need no catalog ingest and work on
vCenter 8 and 9. When vCenter refuses with HTTP 400 (a VM in the wrong power
state, for example), the answer is ``refused`` with vCenter's reason, and
nothing changed.

The parameters, answers and AI guidance live in :mod:`._vm_device_schemas`.
The shared registry in ``_register.py`` only splats
:data:`VM_NIC_SERIAL_COMPOSITES`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._vm_device_schemas import (
    VM_NIC_CONNECT_LLM_INSTRUCTIONS,
    VM_NIC_CONNECT_PARAMETER_SCHEMA,
    VM_NIC_CONNECT_RESPONSE_SCHEMA,
    VM_SERIAL_PORT_ADD_LLM_INSTRUCTIONS,
    VM_SERIAL_PORT_ADD_PARAMETER_SCHEMA,
    VM_SERIAL_PORT_ADD_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _parse_vsphere_error,
    _read_sub_op,
    _unwrap_value,
    _vsphere_fault_detail,
    _write_sub_op,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import check_vm_moid
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "VM_NIC_SERIAL_COMPOSITES",
    "vm_nic_connect_composite",
    "vm_serial_port_add_composite",
]

OP_ID_VM_NIC_CONNECT: Final = "vmware.composite.vm.nic.connect"
OP_ID_VM_SERIAL_PORT_ADD: Final = "vmware.composite.vm.serial_port.add"

_OP_GET_VM_NIC: Final = "GET:/vcenter/vm/{vm}/hardware/ethernet/{nic}"
_OP_CONNECT_VM_NIC: Final = "POST:/vcenter/vm/{vm}/hardware/ethernet/{nic}?action=connect"
_OP_DISCONNECT_VM_NIC: Final = "POST:/vcenter/vm/{vm}/hardware/ethernet/{nic}?action=disconnect"
_OP_CREATE_VM_SERIAL: Final = "POST:/vcenter/vm/{vm}/hardware/serial"
_OP_GET_VM_SERIAL: Final = "GET:/vcenter/vm/{vm}/hardware/serial/{port}"

#: Every REST call of each change (reads and writes). The reconcile test
#: checks them against the pinned vCenter 8.0 and 9.0 API files, and
#: ``_governed_subops`` lists them for the grant set.
SUB_OPS_VM_NIC_CONNECT: Final[tuple[str, ...]] = (
    _OP_GET_VM_NIC,
    _OP_CONNECT_VM_NIC,
    _OP_DISCONNECT_VM_NIC,
)
SUB_OPS_VM_SERIAL_PORT_ADD: Final[tuple[str, ...]] = (
    _OP_CREATE_VM_SERIAL,
    _OP_GET_VM_SERIAL,
)

_CONNECTED: Final = "CONNECTED"

#: ``action`` -> (write sub-op, done status, state that means "already done").
_NIC_ACTIONS: Final[dict[str, tuple[str, str, bool]]] = {
    "connect": (_OP_CONNECT_VM_NIC, "connected", True),
    "disconnect": (_OP_DISCONNECT_VM_NIC, "disconnected", False),
}


def _refused(envelope: dict[str, Any], exc: httpx.HTTPError) -> dict[str, Any] | None:
    """Map a vCenter HTTP 400 to ``refused``; ``None`` for any other fault."""
    status, error_type = _parse_vsphere_error(exc)
    if status != 400:
        return None
    return {
        **envelope,
        "status": "refused",
        "error_type": error_type,
        "guidance": f"vCenter refused the change, nothing changed: {_vsphere_fault_detail(exc)}",
    }


# ---------------------------------------------------------------------------
# vm.nic.connect
# ---------------------------------------------------------------------------


def _nic_view(info: Any) -> dict[str, Any] | None:
    """Project ``Ethernet.Info`` to the fields the approver and the agent need."""
    if not isinstance(info, dict):
        return None
    raw = info.get("backing")
    backing: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return {
        "label": info.get("label"),
        "mac_address": info.get("mac_address"),
        "state": info.get("state"),
        "start_connected": info.get("start_connected"),
        "network": backing.get("network"),
        "network_name": backing.get("network_name"),
    }


async def _read_nic(
    connector: VmwareRestConnector, operator: Operator, target: Any, vm: str, nic: str
) -> dict[str, Any] | None:
    """Read one network adapter; ``None`` when vCenter answers 404."""
    try:
        payload = await _read_sub_op(
            connector, target, operator, _OP_GET_VM_NIC, {"vm": vm, "nic": nic}
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return _nic_view(_unwrap_value(payload))


def _check_device_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise ValueError(f"{field} {value!r} is not a vCenter device id such as '4000'")
    return value


async def vm_nic_connect_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Connect or disconnect one network adapter. Op-id: ``vmware.composite.vm.nic.connect``."""
    vm = check_vm_moid(params["vm"])
    nic = _check_device_id(params["nic"], "nic")
    action = params.get("action", "connect")
    sub_op, done_status, want_connected = _NIC_ACTIONS[action]
    before = await _read_nic(connector, operator, target, vm, nic)
    envelope: dict[str, Any] = {
        "vm": vm,
        "nic": nic,
        "action": action,
        "before": before,
        "after": before,
        "error_type": None,
        "guidance": None,
    }
    if before is None:
        return {
            **envelope,
            "status": "not_found",
            "guidance": f"vCenter has no network adapter {nic} on {vm}. List the adapters "
            "with vmware.vm.info.",
        }
    if (before.get("state") == _CONNECTED) is want_connected:
        return {**envelope, "status": "unchanged", "guidance": "Already so; nothing sent."}
    try:
        gate, _ = await _write_sub_op(connector, target, operator, sub_op, {"vm": vm, "nic": nic})
    except httpx.HTTPError as exc:
        refused = _refused(envelope, exc)
        if refused is None:
            raise
        return refused
    if gate is not None:
        return gate
    try:
        after = await _read_nic(connector, operator, target, vm, nic)
    except httpx.HTTPError:
        after = None
    guidance = (
        None
        if after is not None
        else "vCenter took the request, but MEHO could not read the adapter again. Check "
        "with vmware.vm.info."
    )
    return {**envelope, "status": done_status, "after": after, "guidance": guidance}


async def _vm_nic_connect_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the adapter and its state now."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    action = ctx.params.get("action", "connect")
    try:
        vm = check_vm_moid(ctx.params["vm"])
        nic = _check_device_id(ctx.params["nic"], "nic")
        current = await _read_nic(connector, ctx.operator, ctx.target, vm, nic)
    except Exception as exc:
        return {
            "summary": "MEHO could not read the network adapter, so it cannot show its state.",
            "action": action,
            "read_error": preview_failure_reason(exc),
        }
    return {
        "summary": f"{action.capitalize()} network adapter {nic} of {vm}.",
        "vm": vm,
        "nic": nic,
        "action": action,
        "current": current,
    }


# ---------------------------------------------------------------------------
# vm.serial_port.add
# ---------------------------------------------------------------------------


def _serial_create_spec(params: dict[str, Any]) -> dict[str, Any]:
    """Build ``Serial.CreateSpec`` (top-level body on ``/api``) from the parameters."""
    file = params.get("file")
    if isinstance(file, str):
        backing: dict[str, Any] = {"type": "FILE", "file": file}
    else:
        role = params.get("network_role", "server")
        backing = {
            "type": "NETWORK_CLIENT" if role == "client" else "NETWORK_SERVER",
            "network_location": params["network_location"],
        }
        if params.get("proxy") is not None:
            backing["proxy"] = params["proxy"]
    spec: dict[str, Any] = {
        "backing": backing,
        "start_connected": params.get("start_connected", True),
    }
    for key in ("yield_on_poll", "allow_guest_control"):
        if params.get(key) is not None:
            spec[key] = params[key]
    return spec


def _serial_view(info: Any) -> dict[str, Any] | None:
    """Project ``Serial.Info`` to a flat row."""
    if not isinstance(info, dict):
        return None
    raw = info.get("backing")
    backing: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return {
        "label": info.get("label"),
        "backing_type": backing.get("type"),
        "file": backing.get("file"),
        "network_location": backing.get("network_location"),
        "state": info.get("state"),
        "start_connected": info.get("start_connected"),
        "yield_on_poll": info.get("yield_on_poll"),
        "allow_guest_control": info.get("allow_guest_control"),
    }


async def vm_serial_port_add_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Add one serial port to a VM. Op-id: ``vmware.composite.vm.serial_port.add``."""
    vm = check_vm_moid(params["vm"])
    envelope: dict[str, Any] = {
        "vm": vm,
        "requested": None,
        "port": None,
        "port_info": None,
        "error_type": None,
        "guidance": None,
    }
    if isinstance(params.get("file"), str) == isinstance(params.get("network_location"), str):
        return {
            **envelope,
            "status": "invalid_request",
            "guidance": "Pass exactly one of file or network_location; nothing sent.",
        }
    spec = _serial_create_spec(params)
    envelope["requested"] = spec
    try:
        gate, payload = await _write_sub_op(
            connector, target, operator, _OP_CREATE_VM_SERIAL, {"vm": vm, **spec}
        )
    except httpx.HTTPError as exc:
        refused = _refused(envelope, exc)
        if refused is None:
            raise
        return refused
    if gate is not None:
        return gate
    port = _unwrap_value(payload)
    if not isinstance(port, str):
        return {
            **envelope,
            "status": "added",
            "guidance": "vCenter added the port but sent no id. Read it with vmware.vm.info.",
        }
    try:
        row = _serial_view(
            _unwrap_value(
                await _read_sub_op(
                    connector, target, operator, _OP_GET_VM_SERIAL, {"vm": vm, "port": port}
                )
            )
        )
    except httpx.HTTPError:
        row = None
    return {**envelope, "status": "added", "port": port, "port_info": row}


async def _vm_serial_port_add_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the VM and the port to add. No vendor call."""
    try:
        vm = check_vm_moid(ctx.params["vm"])
        spec = _serial_create_spec(ctx.params)
    except (KeyError, ValueError, TypeError):
        return None
    target_text = spec["backing"].get("file") or spec["backing"].get("network_location")
    return {
        "summary": f"Add a serial port to {vm} ({spec['backing']['type']}: {target_text}).",
        "vm": vm,
        "requested": spec,
    }


register_preview_builder(OP_ID_VM_NIC_CONNECT, _vm_nic_connect_preview)
register_preview_builder(OP_ID_VM_SERIAL_PORT_ADD, _vm_serial_port_add_preview)


# ---------------------------------------------------------------------------
# Registration rows
# ---------------------------------------------------------------------------

#: The two registration rows, as ``_CompositeSpec`` keyword arguments.
VM_NIC_SERIAL_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": OP_ID_VM_NIC_CONNECT,
        "handler": vm_nic_connect_composite,
        "summary": "Connect or disconnect a network adapter of a running VM.",
        "description": (
            "Plugs a network adapter of a powered-on VM in or out, named by VM id and "
            "adapter id. Reads the adapter first and sends nothing when it is already in "
            "the asked state. Waits for approval."
        ),
        "parameter_schema": VM_NIC_CONNECT_PARAMETER_SCHEMA,
        "response_schema": VM_NIC_CONNECT_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "hardware", "nic"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_NIC_CONNECT_LLM_INSTRUCTIONS,
    },
    {
        "op_id": OP_ID_VM_SERIAL_PORT_ADD,
        "handler": vm_serial_port_add_composite,
        "summary": "Add a serial port to a VM, to a datastore file or a network address.",
        "description": (
            "Adds a serial port to a powered-off VM, named by its VM id. The port writes "
            "to a file on a datastore, or listens on or connects to a network address. "
            "Reads the new port back. Waits for approval."
        ),
        "parameter_schema": VM_SERIAL_PORT_ADD_PARAMETER_SCHEMA,
        "response_schema": VM_SERIAL_PORT_ADD_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "hardware", "serial"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_SERIAL_PORT_ADD_LLM_INSTRUCTIONS,
    },
)
