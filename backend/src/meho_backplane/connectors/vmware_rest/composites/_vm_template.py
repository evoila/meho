# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VM templates in a content library: deploy a VM, check out, check in.

Three ``dangerous`` changes on a content-library item that holds a VM
template (the vendor ``Vcenter.VmTemplate.LibraryItems`` API). Each waits for
approval (``requires_approval=True``); an agent needs a grant.

* ``vmware.composite.library.vm_template.deploy`` --
  ``POST /vcenter/vm-template/library-items/{item}?action=deploy`` with a
  ``DeploySpec`` (name, placement, storage, power-on, guest customization
  spec). vCenter answers with the new VM id when the copy is done (the call is
  synchronous, so it gets the long deploy timeout). MEHO reads the VM back.
* ``vmware.composite.library.vm_template.check_out`` --
  ``POST .../check-outs?action=check-out`` with a ``CheckOutSpec``. vCenter
  makes a VM from the template to edit and answers with its id. MEHO reads
  the check-out and the VM back.
* ``vmware.composite.library.vm_template.check_in`` --
  ``POST .../check-outs/{vm}?action=check-in`` with a ``CheckInSpec``
  (``message``). The VM becomes the new template version; vCenter keeps the
  previous one as a backup and answers with the new version. MEHO reads the
  template back.

The ``/api`` surface takes each ``*Spec`` at the top level of the body
(not under ``spec``). Every input is an id, never a name, so the approved
run changes exactly what the approver saw. vCenter answers HTTP 400 when it
refuses and HTTP 404 when something does not exist; the answer is then
``refused`` or ``not_found`` with vCenter's reason, and nothing changed.

All calls go through the shared write seam (:func:`._write._write_sub_op`)
on the connector session, so the changes need no catalog ingest and work on
vCenter 8 and 9 alike. The registry in ``_register.py`` only splats
:data:`VM_TEMPLATE_COMPOSITES`; ``_governed_subops.py`` reads
:data:`VM_TEMPLATE_GOVERNED_SUBOPS`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._library_checks import id_problem
from meho_backplane.connectors.vmware_rest.composites._vm_template_schemas import (
    CHECK_IN_LLM_INSTRUCTIONS,
    CHECK_IN_PARAMETER_SCHEMA,
    CHECK_IN_RESPONSE_SCHEMA,
    CHECK_OUT_LLM_INSTRUCTIONS,
    CHECK_OUT_PARAMETER_SCHEMA,
    CHECK_OUT_RESPONSE_SCHEMA,
    DEPLOY_LLM_INSTRUCTIONS,
    DEPLOY_PARAMETER_SCHEMA,
    DEPLOY_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _LIBRARY_ITEM_DEPLOY_TIMEOUT,
    _OP_DEPLOY_LIBRARY_VM,
    _OP_GET_VM,
    _parse_vsphere_error,
    _read_sub_op,
    _unwrap_value,
    _vsphere_fault_detail,
    _write_sub_op,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import check_vm_moid
from meho_backplane.operations._preview import register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "OP_ID_CHECK_IN",
    "OP_ID_CHECK_OUT",
    "OP_ID_DEPLOY",
    "SUB_OPS_CHECK_IN",
    "SUB_OPS_CHECK_OUT",
    "SUB_OPS_DEPLOY",
    "VM_TEMPLATE_COMPOSITES",
    "VM_TEMPLATE_GOVERNED_SUBOPS",
    "vm_template_check_in_composite",
    "vm_template_check_out_composite",
    "vm_template_deploy_composite",
]

OP_ID_DEPLOY: Final = "vmware.composite.library.vm_template.deploy"
OP_ID_CHECK_OUT: Final = "vmware.composite.library.vm_template.check_out"
OP_ID_CHECK_IN: Final = "vmware.composite.library.vm_template.check_in"

_ITEM_PATH: Final = "/vcenter/vm-template/library-items/{templateLibraryItem}"
_OP_CHECK_OUT: Final = f"POST:{_ITEM_PATH}/check-outs?action=check-out"
_OP_CHECK_IN: Final = f"POST:{_ITEM_PATH}/check-outs/{{vm}}?action=check-in"
_OP_GET_TEMPLATE: Final = f"GET:{_ITEM_PATH}"
_OP_GET_CHECK_OUT: Final = f"GET:{_ITEM_PATH}/check-outs/{{vm}}"

#: Every REST call of each change. The reconcile test checks them against the
#: pinned vCenter 8.0 and 9.0 API files; ``_governed_subops`` lists them for
#: the grant set.
SUB_OPS_DEPLOY: Final[tuple[str, ...]] = (_OP_DEPLOY_LIBRARY_VM, _OP_GET_VM)
SUB_OPS_CHECK_OUT: Final[tuple[str, ...]] = (_OP_CHECK_OUT, _OP_GET_CHECK_OUT, _OP_GET_VM)
SUB_OPS_CHECK_IN: Final[tuple[str, ...]] = (_OP_CHECK_IN, _OP_GET_TEMPLATE)

_PLACEMENT_KEYS: Final[tuple[str, ...]] = ("folder", "resource_pool", "host", "cluster")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count(value: Any) -> int | None:
    return len(value) if isinstance(value, (Mapping, list)) else None


def _template_item(params: Mapping[str, Any]) -> str:
    """The template item id; refuse anything that is not a UUID before it reaches a path."""
    item = params["template_library_item"]
    problem = id_problem(item, "template_library_item")
    if problem is not None:
        raise ValueError(problem)
    return str(item)


def _placement(params: Mapping[str, Any]) -> dict[str, Any]:
    return {key: params[key] for key in _PLACEMENT_KEYS if isinstance(params.get(key), str)}


def _vendor_answer(envelope: dict[str, Any], exc: httpx.HTTPError) -> dict[str, Any] | None:
    """vCenter HTTP 400 is ``refused`` and 404 is ``not_found``; anything else is ``None``."""
    status, error_type = _parse_vsphere_error(exc)
    if status == 400:
        word, text = "refused", "vCenter refused the change, nothing changed"
    elif status == 404:
        word, text = "not_found", "vCenter did not find the item, the VM or a placement object"
    else:
        return None
    return {
        **envelope,
        "status": word,
        "error_type": error_type,
        "guidance": f"{text}: {_vsphere_fault_detail(exc)}",
    }


async def _send(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    params: dict[str, Any],
    envelope: dict[str, Any],
    timeout: Any = httpx.USE_CLIENT_DEFAULT,
) -> tuple[dict[str, Any] | OperationResult | None, Any]:
    """Send one governed write. Returns ``(early answer, None)`` or ``(None, payload)``."""
    try:
        gate, payload = await _write_sub_op(
            connector, target, operator, op_id, params, timeout=timeout
        )
    except httpx.HTTPError as exc:
        answer = _vendor_answer(envelope, exc)
        if answer is None:
            raise
        return answer, None
    if gate is not None:
        return gate, None
    return None, _unwrap_value(payload)


async def _read_back(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    op_id: str,
    params: dict[str, Any],
) -> tuple[Mapping[str, Any] | None, str | None]:
    """One read after the change: ``(answer, None)``, or ``(None, why it failed)``."""
    try:
        payload = await _read_sub_op(connector, target, operator, op_id, params)
    except httpx.HTTPError as exc:
        return None, f"read back failed: {_vsphere_fault_detail(exc)}"
    return _mapping(_unwrap_value(payload)), None


def _vm_row(info: Mapping[str, Any]) -> dict[str, Any]:
    """Project a vendor ``VM.Info``. The wire uses ``guest_OS`` / ``size_MiB``."""
    cpu = _mapping(info.get("cpu"))
    memory = _mapping(info.get("memory"))
    return {
        "name": _text(info.get("name")),
        "power_state": _text(info.get("power_state")),
        "guest_os": _text(info.get("guest_OS", info.get("guest_os"))),
        "cpu_count": _int(cpu.get("count")),
        "memory_mib": _int(memory.get("size_MiB", memory.get("size_mib"))),
    }


def _template_row(info: Mapping[str, Any]) -> dict[str, Any]:
    """Project a vendor ``VmTemplate.LibraryItems.Info``."""
    cpu = _mapping(info.get("cpu"))
    memory = _mapping(info.get("memory"))
    return {
        "vm_template": _text(info.get("vm_template")),
        "guest_os": _text(info.get("guest_OS", info.get("guest_os"))),
        "cpu_count": _int(cpu.get("count")),
        "cores_per_socket": _int(cpu.get("cores_per_socket")),
        "memory_mib": _int(memory.get("size_MiB", memory.get("size_mib"))),
        "disk_count": _count(info.get("disks")),
        "nic_count": _count(info.get("nics")),
    }


async def _vm_info(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str
) -> tuple[dict[str, Any] | None, str | None]:
    info, note = await _read_back(connector, target, operator, _OP_GET_VM, {"vm": vm})
    return (None if info is None else _vm_row(info)), note


def _new_vm_id(payload: Any, what: str) -> str:
    if not isinstance(payload, str):
        raise RuntimeError(f"{what}: vCenter answered without a VM id ({type(payload).__name__})")
    return check_vm_moid(payload)


# ---------------------------------------------------------------------------
# library.vm_template.deploy
# ---------------------------------------------------------------------------


def _deploy_body(params: Mapping[str, Any]) -> dict[str, Any]:
    """The ``DeploySpec``, top level, from the operation parameters."""
    body: dict[str, Any] = {
        "name": params["name"],
        "placement": _placement(params),
        "powered_on": bool(params.get("powered_on", False)),
    }
    if isinstance(params.get("description"), str):
        body["description"] = params["description"]
    datastore = params.get("datastore")
    if isinstance(datastore, str):
        body["vm_home_storage"] = {"datastore": datastore}
        body["disk_storage"] = {"datastore": datastore}
    spec_name = params.get("guest_customization_spec")
    if isinstance(spec_name, str):
        body["guest_customization"] = {"name": spec_name}
    return body


async def vm_template_deploy_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Deploy a VM from a VM-template library item. Op-id: ``library.vm_template.deploy``."""
    item = _template_item(params)
    envelope: dict[str, Any] = {
        "template_library_item": item,
        "vm": None,
        "vm_info": None,
        "read_note": None,
        "error_type": None,
        "guidance": None,
    }
    early, payload = await _send(
        connector,
        target,
        operator,
        op_id=_OP_DEPLOY_LIBRARY_VM,
        params={"templateLibraryItem": item, **_deploy_body(params)},
        envelope=envelope,
        # Synchronous: vCenter holds the POST open for the whole copy (#3076).
        timeout=_LIBRARY_ITEM_DEPLOY_TIMEOUT,
    )
    if early is not None:
        return early
    vm = _new_vm_id(payload, OP_ID_DEPLOY)
    info, note = await _vm_info(connector, target, operator, vm)
    return {**envelope, "status": "deployed", "vm": vm, "vm_info": info, "read_note": note}


# ---------------------------------------------------------------------------
# library.vm_template.check_out
# ---------------------------------------------------------------------------


async def vm_template_check_out_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Check a VM template out as a VM to edit. Op-id: ``library.vm_template.check_out``."""
    item = _template_item(params)
    envelope: dict[str, Any] = {
        "template_library_item": item,
        "vm": None,
        "check_out": None,
        "vm_info": None,
        "read_note": None,
        "error_type": None,
        "guidance": None,
    }
    body: dict[str, Any] = {
        "name": params["name"],
        "powered_on": bool(params.get("powered_on", False)),
    }
    placement = _placement(params)
    if placement:
        body["placement"] = placement
    early, payload = await _send(
        connector,
        target,
        operator,
        op_id=_OP_CHECK_OUT,
        params={"templateLibraryItem": item, **body},
        envelope=envelope,
    )
    if early is not None:
        return early
    vm = _new_vm_id(payload, OP_ID_CHECK_OUT)
    check_out, note = await _read_back(
        connector, target, operator, _OP_GET_CHECK_OUT, {"templateLibraryItem": item, "vm": vm}
    )
    info, vm_note = await _vm_info(connector, target, operator, vm)
    return {
        **envelope,
        "status": "checked_out",
        "vm": vm,
        "check_out": (
            None
            if check_out is None
            else {"time": _text(check_out.get("time")), "user": _text(check_out.get("user"))}
        ),
        "vm_info": info,
        "read_note": note or vm_note,
    }


# ---------------------------------------------------------------------------
# library.vm_template.check_in
# ---------------------------------------------------------------------------


async def vm_template_check_in_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Check a VM back in as the new template version. Op-id: ``library.vm_template.check_in``."""
    item = _template_item(params)
    vm = check_vm_moid(params["vm"])
    envelope: dict[str, Any] = {
        "template_library_item": item,
        "vm": vm,
        "version": None,
        "template": None,
        "read_note": None,
        "error_type": None,
        "guidance": None,
    }
    early, payload = await _send(
        connector,
        target,
        operator,
        op_id=_OP_CHECK_IN,
        params={"templateLibraryItem": item, "vm": vm, "message": params["message"]},
        envelope=envelope,
    )
    if early is not None:
        return early
    template, note = await _read_back(
        connector, target, operator, _OP_GET_TEMPLATE, {"templateLibraryItem": item}
    )
    return {
        **envelope,
        "status": "checked_in",
        "version": _text(payload),
        "template": None if template is None else _template_row(template),
        "read_note": note,
    }


# ---------------------------------------------------------------------------
# Approval previews
# ---------------------------------------------------------------------------

_ECHO_KEYS: Final[tuple[str, ...]] = (
    "template_library_item",
    "name",
    "vm",
    "message",
    *_PLACEMENT_KEYS,
    "datastore",
    "powered_on",
    "guest_customization_spec",
)


async def _live_reads(ctx: PreviewContext, item: str) -> dict[str, Any]:
    """The template now, and for a check-in the check-out and the VM. Never raises on HTTP."""
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    template, note = await _read_back(
        connector, ctx.target, ctx.operator, _OP_GET_TEMPLATE, {"templateLibraryItem": item}
    )
    reads: dict[str, Any] = {
        "template": None if template is None else _template_row(template),
        "read_note": note,
    }
    vm = ctx.params.get("vm")
    if isinstance(vm, str) and ctx.descriptor.op_id == OP_ID_CHECK_IN:
        check_out, out_note = await _read_back(
            connector,
            ctx.target,
            ctx.operator,
            _OP_GET_CHECK_OUT,
            {"templateLibraryItem": item, "vm": vm},
        )
        info, vm_note = await _vm_info(connector, ctx.target, ctx.operator, vm)
        reads["check_out"] = (
            None
            if check_out is None
            else {"time": _text(check_out.get("time")), "user": _text(check_out.get("user"))}
        )
        reads["vm_info"] = info
        reads["read_note"] = note or out_note or vm_note
    return reads


async def _preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the inputs and, with a connector, the template as it is now."""
    item = ctx.params.get("template_library_item")
    if id_problem(item, "template_library_item") is not None:
        return None
    op_id = ctx.descriptor.op_id
    if op_id == OP_ID_DEPLOY:
        summary = f"Deploy a new VM {ctx.params.get('name')!r} from VM template item {item}."
    elif op_id == OP_ID_CHECK_OUT:
        summary = f"Check out VM template item {item} as a new VM {ctx.params.get('name')!r}."
    else:
        summary = (
            f"Save VM {ctx.params.get('vm')} into VM template item {item} as its new version; "
            "the previous version stays as a backup."
        )
    preview: dict[str, Any] = {
        "summary": summary,
        **{key: ctx.params[key] for key in _ECHO_KEYS if key in ctx.params},
    }
    if ctx.connector_instance is not None:
        preview.update(await _live_reads(ctx, str(item)))
    return preview


for _op_id in (OP_ID_DEPLOY, OP_ID_CHECK_OUT, OP_ID_CHECK_IN):
    register_preview_builder(_op_id, _preview)


# ---------------------------------------------------------------------------
# Registration rows
# ---------------------------------------------------------------------------

_TAGS: Final[list[str]] = ["composite", "write", "content-library", "vm-template", "vm"]

#: The three registration rows, as ``_CompositeSpec`` keyword arguments.
VM_TEMPLATE_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": OP_ID_DEPLOY,
        "handler": vm_template_deploy_composite,
        "summary": "Deploy a new VM from a VM template in a content library.",
        "description": (
            "Makes a new VM from a VM-template library item, with placement, optional "
            "datastore, power-on and a saved guest customization spec. Waits for the copy, "
            "then reads the VM back. Waits for approval."
        ),
        "parameter_schema": DEPLOY_PARAMETER_SCHEMA,
        "response_schema": DEPLOY_RESPONSE_SCHEMA,
        "group_key": "content_library",
        "tags": [*_TAGS, "deploy"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": DEPLOY_LLM_INSTRUCTIONS,
    },
    {
        "op_id": OP_ID_CHECK_OUT,
        "handler": vm_template_check_out_composite,
        "summary": "Check a VM template out of a content library as a VM you can edit.",
        "description": (
            "vCenter makes a VM from a VM-template library item so you can change it; the "
            "template stays as it is until the check-in. Reads the check-out and the VM "
            "back. Waits for approval."
        ),
        "parameter_schema": CHECK_OUT_PARAMETER_SCHEMA,
        "response_schema": CHECK_OUT_RESPONSE_SCHEMA,
        "group_key": "content_library",
        "tags": [*_TAGS, "check-out"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": CHECK_OUT_LLM_INSTRUCTIONS,
    },
    {
        "op_id": OP_ID_CHECK_IN,
        "handler": vm_template_check_in_composite,
        "summary": "Check a changed VM back into its content-library item as a new template.",
        "description": (
            "Saves a checked-out, powered-off VM into its VM-template library item as the "
            "new version; vCenter keeps the previous version as a backup. Reads the template "
            "back. Waits for approval."
        ),
        "parameter_schema": CHECK_IN_PARAMETER_SCHEMA,
        "response_schema": CHECK_IN_RESPONSE_SCHEMA,
        "group_key": "content_library",
        "tags": [*_TAGS, "check-in"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": CHECK_IN_LLM_INSTRUCTIONS,
    },
)

#: The governed REST calls of each change, for ``_governed_subops``.
VM_TEMPLATE_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    OP_ID_DEPLOY: SUB_OPS_DEPLOY,
    OP_ID_CHECK_OUT: SUB_OPS_CHECK_OUT,
    OP_ID_CHECK_IN: SUB_OPS_CHECK_IN,
}
