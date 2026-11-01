# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.composite.vm.nic.remove`` -- take one network adapter off a VM.

``dangerous``, waits for approval. It reads
``GET /vcenter/vm/{vm}/hardware/ethernet/{nic}`` first: an unknown VM or
adapter answers ``not_found`` and nothing is sent. Then ``DELETE`` on the same
path through the shared write seam (:func:`._write._write_sub_op`), then it
lists the adapters again (``GET .../hardware/ethernet``). vCenter refuses some
requests with HTTP 400 (for example a guest that does not allow hot remove);
the answer is then ``refused`` with vCenter's reason, and nothing changed.
Both paths exist on vCenter 8.0 and 9.0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

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
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "OP_ID_VM_NIC_REMOVE",
    "SUB_OPS_VM_NIC_REMOVE",
    "vendor_refusal",
    "vm_nic_remove_composite",
    "vm_nic_remove_preview",
]

OP_ID_VM_NIC_REMOVE: Final = "vmware.composite.vm.nic.remove"

_OP_GET_VM_NIC: Final = "GET:/vcenter/vm/{vm}/hardware/ethernet/{nic}"
_OP_LIST_VM_NICS: Final = "GET:/vcenter/vm/{vm}/hardware/ethernet"
_OP_DELETE_VM_NIC: Final = "DELETE:/vcenter/vm/{vm}/hardware/ethernet/{nic}"

#: Every REST call of the change. The reconcile test checks them against the
#: pinned vCenter 8.0 and 9.0 API files.
SUB_OPS_VM_NIC_REMOVE: Final[tuple[str, ...]] = (
    _OP_GET_VM_NIC,
    _OP_LIST_VM_NICS,
    _OP_DELETE_VM_NIC,
)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def vendor_refusal(envelope: dict[str, Any], exc: httpx.HTTPError) -> dict[str, Any] | None:
    """Map HTTP 400 to ``refused`` and 404 to ``not_found``; ``None`` for any other fault."""
    status, error_type = _parse_vsphere_error(exc)
    if status not in (400, 404):
        return None
    detail = _vsphere_fault_detail(exc)
    return {
        **envelope,
        "status": "refused" if status == 400 else "not_found",
        "error_type": error_type,
        "guidance": f"vCenter said no, nothing changed: {detail}",
    }


def _nic_row(nic: str, raw: Any) -> dict[str, Any]:
    info = raw if isinstance(raw, dict) else {}
    raw_backing = info.get("backing")
    backing: dict[str, Any] = raw_backing if isinstance(raw_backing, dict) else {}
    return {
        "nic": nic,
        "label": _str(info.get("label")),
        "type": _str(info.get("type")),
        "mac_address": _str(info.get("mac_address")),
        "backing_type": _str(backing.get("type")),
        "network": _str(backing.get("network")),
        "network_name": _str(backing.get("network_name")),
        "state": _str(info.get("state")),
    }


async def _read_nic(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str, nic: str
) -> dict[str, Any]:
    raw = await _read_sub_op(connector, target, operator, _OP_GET_VM_NIC, {"vm": vm, "nic": nic})
    return _nic_row(nic, _unwrap_value(raw))


async def _nic_ids(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str
) -> list[str] | None:
    try:
        raw = _unwrap_value(
            await _read_sub_op(connector, target, operator, _OP_LIST_VM_NICS, {"vm": vm})
        )
    except httpx.HTTPError:
        return None
    if not isinstance(raw, list):
        return None
    return [e["nic"] for e in raw if isinstance(e, dict) and isinstance(e.get("nic"), str)]


async def vm_nic_remove_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Remove one network adapter from a VM. Op-id: ``vmware.composite.vm.nic.remove``."""
    vm = check_vm_moid(params["vm"])
    nic = str(params["nic"])
    envelope: dict[str, Any] = {
        "vm": vm,
        "nic": nic,
        "removed_nic": None,
        "remaining_nics": None,
        "error_type": None,
        "guidance": None,
    }
    try:
        before = await _read_nic(connector, target, operator, vm, nic)
    except httpx.HTTPError as exc:
        refusal = vendor_refusal(envelope, exc)
        if refusal is None:
            raise
        return refusal
    envelope["removed_nic"] = before
    try:
        gate, _ = await _write_sub_op(
            connector, target, operator, _OP_DELETE_VM_NIC, {"vm": vm, "nic": nic}
        )
    except httpx.HTTPError as exc:
        refusal = vendor_refusal(envelope, exc)
        if refusal is None:
            raise
        return refusal
    if gate is not None:
        return gate
    remaining = await _nic_ids(connector, target, operator, vm)
    envelope["remaining_nics"] = remaining
    if remaining is None:
        return {
            **envelope,
            "status": "removed",
            "guidance": "vCenter took the request, but MEHO could not list the adapters "
            "again. Check with vmware.vm.info.",
        }
    if nic in remaining:
        return {
            **envelope,
            "status": "still_present",
            "guidance": "vCenter took the request, but the adapter still reads back. "
            "Read the VM again.",
        }
    return {**envelope, "status": "removed"}


async def vm_nic_remove_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the adapter that goes away."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        vm = check_vm_moid(ctx.params["vm"])
        nic = str(ctx.params["nic"])
        row = await _read_nic(connector, ctx.target, ctx.operator, vm, nic)
    except Exception as exc:
        return {
            "summary": "MEHO could not read the network adapter, so it cannot show it.",
            "read_error": preview_failure_reason(exc),
        }
    network = row["network_name"] or row["network"] or "no network"
    return {
        "summary": f"Remove {row['label'] or 'adapter ' + nic} ({network}) from {vm}.",
        "vm": vm,
        "nic": row,
    }
