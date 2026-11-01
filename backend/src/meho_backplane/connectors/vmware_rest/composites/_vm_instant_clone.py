# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Make an instant clone of a running VM -- ``vmware.composite.vm.instant_clone`` (``dangerous``).

One REST call, ``POST /api/vcenter/vm?action=instant-clone`` (vSphere API
6.7.1 and newer, so on vCenter 8.0 and 9.x). The call is synchronous: its
answer is the new VM's id. Before it, MEHO reads the source VM through VI-JSON
and refuses a source that is not powered on (vCenter clones only a running
VM). After it, MEHO reads the new VM and returns its state.

Shared parts: :mod:`._vm_lifecycle`; schemas: :mod:`._vm_lifecycle_schemas`;
the registry in ``_register.py`` only splats :data:`VM_INSTANT_CLONE_COMPOSITES`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import moid_problem
from meho_backplane.connectors.vmware_rest.composites._vm_lifecycle import (
    POWERED_ON,
    STATUS_NOT_CONFIRMED,
    STATUS_NOT_FOUND,
    read_vm,
)
from meho_backplane.connectors.vmware_rest.composites._vm_lifecycle_schemas import (
    RESOURCE_POOL_MOID_PATTERN,
    VM_INSTANT_CLONE_LLM_INSTRUCTIONS,
    VM_INSTANT_CLONE_PARAMETER_SCHEMA,
    VM_INSTANT_CLONE_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _unwrap_value,
    _vsphere_fault_detail,
    _write_sub_op,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_MOID_PATTERN,
    VM_FOLDER_MOID_PATTERN,
    VM_MOID_PATTERN,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "COMPOSITES",
    "instant_clone_body",
    "vm_instant_clone_composite",
]

OP_ID_VM_INSTANT_CLONE: Final = "vmware.composite.vm.instant_clone"

#: The REST call (``Vcenter.VM_instantClone``).
_OP_INSTANT_CLONE: Final = "POST:/vcenter/vm?action=instant-clone"
#: REST child-op list; the VI-JSON reads are in ``_vm_lifecycle``.
_SUB_OPS_VM_INSTANT_CLONE: Final[tuple[str, ...]] = (_OP_INSTANT_CLONE,)

#: The call holds the request open until the clone exists; allow some minutes.
_INSTANT_CLONE_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=5.0)

STATUS_CLONED: Final = "cloned"

#: Placement inputs -> ``InstantClonePlacementSpec`` fields, with their id shapes.
_PLACEMENT: Final[tuple[tuple[str, str, str], ...]] = (
    ("folder", "folder", VM_FOLDER_MOID_PATTERN),
    ("resource_pool", "resource_pool", RESOURCE_POOL_MOID_PATTERN),
    ("datastore", "datastore", DATASTORE_MOID_PATTERN),
)


def instant_clone_body(params: dict[str, Any]) -> dict[str, Any]:
    """The flat ``Vcenter.VM.InstantCloneSpec`` request body."""
    body: dict[str, Any] = {"source": params["source_vm"], "name": params["name"]}
    placement = {field: params[key] for key, field, _ in _PLACEMENT if params.get(key) is not None}
    if placement:
        body["placement"] = placement
    if params.get("disconnect_all_nics") is not None:
        body["disconnect_all_nics"] = bool(params["disconnect_all_nics"])
    return body


def _input_problem(params: dict[str, Any]) -> str | None:
    problem = moid_problem(params.get("source_vm"), VM_MOID_PATTERN, "source_vm")
    if problem is not None:
        return problem
    name = params.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 79:
        return f"name must be a VM name of 1 to 79 characters; got {name!r}"
    for key, _, pattern in _PLACEMENT:
        if params.get(key) is not None and (problem := moid_problem(params[key], pattern, key)):
            return problem
    return None


def _envelope(params: dict[str, Any], status: str | None, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "source_vm": params.get("source_vm"),
        "source": None,
        "vm": None,
        "after": None,
        "task": None,
        "guidance": None,
    }
    out.update(fields)
    return out


async def _precheck(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> dict[str, Any]:
    """The envelope the handler stops with, or one with ``status=None`` to go on."""
    problem = _input_problem(params)
    if problem is not None:
        return _envelope(params, STATUS_INVALID_REQUEST, guidance=problem)
    source_vm = params["source_vm"]
    source = await read_vm(connector, target, operator, source_vm)
    if source is None:
        return _envelope(
            params, STATUS_NOT_FOUND, guidance=f"no VM {source_vm!r}; pass a VM id such as vm-42"
        )
    if source["power_state"] != POWERED_ON:
        state = source["power_state"] or "in an unknown power state"
        return _envelope(
            params,
            STATUS_PRECONDITION_FAILED,
            source=source,
            guidance=f"an instant clone needs a running source VM; the VM is {state}",
        )
    return _envelope(params, None, source=source)


async def vm_instant_clone_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Make an instant clone of one running VM. Op-id: ``vm.instant_clone``."""
    checked = await _precheck(connector, target, operator, params)
    if checked["status"] is not None:
        return checked
    try:
        gate, payload = await _write_sub_op(
            connector,
            target,
            operator,
            _OP_INSTANT_CLONE,
            instant_clone_body(params),
            timeout=_INSTANT_CLONE_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"{OP_ID_VM_INSTANT_CLONE}: instant clone of {params['source_vm']!r} failed: "
            f"{_vsphere_fault_detail(exc)}"
        ) from exc
    if gate is not None:
        return gate
    vm = _unwrap_value(payload)
    vm = vm if isinstance(vm, str) else None
    after = await read_vm(connector, target, operator, vm) if vm is not None else None
    if after is None:
        return {
            **checked,
            "vm": vm,
            "status": STATUS_NOT_CONFIRMED,
            "guidance": "vCenter answered the instant clone, but the new VM does not read back",
        }
    return {**checked, "vm": vm, "after": after, "status": STATUS_CLONED}


async def _instant_clone_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the source VM, the new name and the placement."""
    source_vm = ctx.params.get("source_vm")
    if not isinstance(source_vm, str) or ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        checked = await _precheck(connector, ctx.target, ctx.operator, ctx.params)
    except Exception as exc:
        return {
            "summary": f"MEHO could not read VM {source_vm}, so it cannot show its state.",
            "source_vm": source_vm,
            "read_error": preview_failure_reason(exc),
        }
    preview: dict[str, Any] = {
        "summary": (
            f"Make a running instant clone of {source_vm} named {ctx.params.get('name')!r}. "
            "It starts with the same guest identity as the source."
        ),
        "source_vm": source_vm,
        "source": checked["source"],
        "name": ctx.params.get("name"),
        "placement": {key: ctx.params.get(key) for key, _, _ in _PLACEMENT},
        "disconnect_all_nics": ctx.params.get("disconnect_all_nics"),
    }
    if checked["status"] is not None:
        preview["summary"] = f"The call will clone nothing: {checked['guidance']}."
        preview["stops_with"] = checked["status"]
    return preview


#: The registration rows; ``_register.py`` collects them at start-up.
COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=OP_ID_VM_INSTANT_CLONE,
        handler=vm_instant_clone_composite,
        summary="Make an instant clone of a running VM.",
        description=(
            "Makes a new, running VM that shares the memory and disk state of a running source "
            "VM (REST instant-clone). Optional folder, resource pool and datastore; default: "
            "as the source. Returns the new VM id and state. Waits for approval."
        ),
        parameter_schema=VM_INSTANT_CLONE_PARAMETER_SCHEMA,
        response_schema=VM_INSTANT_CLONE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "clone", "rest"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_INSTANT_CLONE_LLM_INSTRUCTIONS,
        preview=_instant_clone_preview,
        classifier_other=True,
    ),
)
