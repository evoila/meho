# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Turn a VM into a template and back -- ``vm.template.mark`` and ``vm.template.unmark``.

vCenter's REST API has no call for this, so both use the VIM API as VI-JSON:

* ``vmware.composite.vm.template.mark`` (``dangerous``) --
  ``VirtualMachine.MarkAsTemplate``. The VM must be powered off; a template
  answers ``unchanged``.
* ``vmware.composite.vm.template.unmark`` (``dangerous``) --
  ``VirtualMachine.MarkAsVirtualMachine`` with a resource pool and an
  optional host. A VM that is no template answers ``unchanged``.

Both calls are synchronous (no vCenter task). After the call MEHO reads the
VM again and answers ``marked`` / ``unmarked`` only when ``config.template``
reads back as asked. Shared parts: :mod:`._vm_lifecycle`; schemas:
:mod:`._vm_lifecycle_schemas`; the registry in ``_register.py`` only splats
:data:`VM_TEMPLATE_COMPOSITES`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    STATUS_UNCHANGED,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import moid_problem
from meho_backplane.connectors.vmware_rest.composites._vm_lifecycle import (
    OP_MARK_AS_TEMPLATE,
    OP_MARK_AS_VIRTUAL_MACHINE,
    POWERED_OFF,
    STATUS_NOT_CONFIRMED,
    STATUS_NOT_FOUND,
    read_vm,
    run_vim_change,
)
from meho_backplane.connectors.vmware_rest.composites._vm_lifecycle_schemas import (
    RESOURCE_POOL_MOID_PATTERN,
    VM_TEMPLATE_MARK_LLM_INSTRUCTIONS,
    VM_TEMPLATE_MARK_PARAMETER_SCHEMA,
    VM_TEMPLATE_MARK_RESPONSE_SCHEMA,
    VM_TEMPLATE_UNMARK_LLM_INSTRUCTIONS,
    VM_TEMPLATE_UNMARK_PARAMETER_SCHEMA,
    VM_TEMPLATE_UNMARK_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    HOST_MOID_PATTERN,
    VM_MOID_PATTERN,
)
from meho_backplane.connectors.vmware_rest.vim_body import vim_moref
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
    "VM_TEMPLATE_COMPOSITES",
    "unmark_body",
    "vm_template_mark_composite",
    "vm_template_unmark_composite",
]

OP_ID_VM_TEMPLATE_MARK: Final = "vmware.composite.vm.template.mark"
OP_ID_VM_TEMPLATE_UNMARK: Final = "vmware.composite.vm.template.unmark"

STATUS_MARKED: Final = "marked"
STATUS_UNMARKED: Final = "unmarked"


def _envelope(vm: str, status: str | None, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "vm": vm,
        "before": None,
        "after": None,
        "task": None,
        "guidance": None,
    }
    out.update(fields)
    return out


def _input_problem(params: dict[str, Any]) -> str | None:
    problem = moid_problem(params.get("vm"), VM_MOID_PATTERN, "vm")
    if problem is None and "resource_pool" in params:
        problem = moid_problem(params["resource_pool"], RESOURCE_POOL_MOID_PATTERN, "resource_pool")
    if problem is None and params.get("host") is not None:
        problem = moid_problem(params["host"], HOST_MOID_PATTERN, "host")
    return problem


async def _precheck(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    *,
    to_template: bool,
) -> dict[str, Any]:
    """The envelope the handler stops with, or one with ``status=None`` to go on."""
    vm = params["vm"]
    problem = _input_problem(params)
    if problem is not None:
        return _envelope(vm, STATUS_INVALID_REQUEST, guidance=problem)
    before = await read_vm(connector, target, operator, vm)
    if before is None:
        return _envelope(vm, STATUS_NOT_FOUND, guidance=f"no VM {vm!r}; pass a VM id such as vm-42")
    if before["is_template"] is to_template:
        state = "a template" if to_template else "not a template"
        return _envelope(
            vm,
            STATUS_UNCHANGED,
            before=before,
            after=before,
            guidance=f"the VM is {state} already; nothing was sent",
        )
    if to_template and before["power_state"] != POWERED_OFF:
        state = before["power_state"] or "in an unknown power state"
        return _envelope(
            vm,
            STATUS_PRECONDITION_FAILED,
            before=before,
            guidance=f"the VM is {state}; power it off first",
        )
    return _envelope(vm, None, before=before)


def unmark_body(params: dict[str, Any]) -> dict[str, Any]:
    """The ``MarkAsVirtualMachine`` request body: the pool, and the host when given."""
    body: dict[str, Any] = {"pool": vim_moref("ResourcePool", params["resource_pool"])}
    if params.get("host") is not None:
        body["host"] = vim_moref("HostSystem", params["host"])
    return body


async def _change(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    *,
    to_template: bool,
) -> dict[str, Any] | OperationResult:
    checked = await _precheck(connector, target, operator, params, to_template=to_template)
    if checked["status"] is not None:
        return checked
    vm = params["vm"]
    gate_params: dict[str, Any] = {"vm": params["vm"]}
    if to_template:
        method, op_id, body = "MarkAsTemplate", OP_MARK_AS_TEMPLATE, {}
        gate_params["op"] = OP_ID_VM_TEMPLATE_MARK
        path = f"/VirtualMachine/{vm}/MarkAsTemplate"
    else:
        method, op_id, body = (
            "MarkAsVirtualMachine",
            OP_MARK_AS_VIRTUAL_MACHINE,
            unmark_body(params),
        )
        gate_params.update(
            op=OP_ID_VM_TEMPLATE_UNMARK,
            resource_pool=params["resource_pool"],
            host=params.get("host"),
        )
        path = f"/VirtualMachine/{vm}/MarkAsVirtualMachine"
    gate, _ = await run_vim_change(
        connector,
        target,
        operator,
        op_id=op_id,
        vmomi_path=path,
        body=body,
        params=gate_params,
    )
    if gate is not None:
        return gate
    after = await read_vm(connector, target, operator, vm)
    if after is None or after["is_template"] is not to_template:
        want = "a template" if to_template else "a VM"
        return {
            **checked,
            "after": after,
            "status": STATUS_NOT_CONFIRMED,
            "guidance": f"{method} succeeded, but the VM does not read back as {want}",
        }
    return {**checked, "after": after, "status": STATUS_MARKED if to_template else STATUS_UNMARKED}


async def vm_template_mark_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Turn one powered-off VM into a template. Op-id: ``vm.template.mark``."""
    return await _change(connector, target, operator, params, to_template=True)


async def vm_template_unmark_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Turn one template back into a VM. Op-id: ``vm.template.unmark``."""
    return await _change(connector, target, operator, params, to_template=False)


def _preview_for(to_template: bool) -> Any:
    async def _preview(ctx: PreviewContext) -> dict[str, Any] | None:
        """Show the approver the VM now and whether the call would stop before the change."""
        vm = ctx.params.get("vm")
        if not isinstance(vm, str) or ctx.connector_instance is None:
            return None
        if not to_template and not isinstance(ctx.params.get("resource_pool"), str):
            return None
        connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
        try:
            checked = await _precheck(
                connector, ctx.target, ctx.operator, ctx.params, to_template=to_template
            )
        except Exception as exc:
            return {
                "summary": f"MEHO could not read VM {vm}, so it cannot show its state.",
                "vm": vm,
                "read_error": preview_failure_reason(exc),
            }
        if to_template:
            summary = f"Turn VM {vm} into a template. A template cannot be powered on."
        else:
            summary = (
                f"Turn template {vm} back into a VM in resource pool {ctx.params['resource_pool']}."
            )
        preview: dict[str, Any] = {"summary": summary, "vm": vm, "current": checked["before"]}
        if checked["status"] is not None:
            preview["summary"] = f"The call will change nothing on {vm}: {checked['guidance']}."
            preview["stops_with"] = checked["status"]
        return preview

    return _preview


register_preview_builder(OP_ID_VM_TEMPLATE_MARK, _preview_for(True))
register_preview_builder(OP_ID_VM_TEMPLATE_UNMARK, _preview_for(False))


#: The two registration rows, as ``_CompositeSpec`` keyword arguments.
VM_TEMPLATE_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": OP_ID_VM_TEMPLATE_MARK,
        "handler": vm_template_mark_composite,
        "summary": "Turn a powered-off VM into a template.",
        "description": (
            "Marks a VM, named by its VM id, as a template (VIM MarkAsTemplate). The VM must be "
            "off. A template can be cloned but not powered on. Returns the VM before and after. "
            "Waits for approval."
        ),
        "parameter_schema": VM_TEMPLATE_MARK_PARAMETER_SCHEMA,
        "response_schema": VM_TEMPLATE_MARK_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "template", "vi-json"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_TEMPLATE_MARK_LLM_INSTRUCTIONS,
    },
    {
        "op_id": OP_ID_VM_TEMPLATE_UNMARK,
        "handler": vm_template_unmark_composite,
        "summary": "Turn a template back into a VM in a resource pool.",
        "description": (
            "Turns a template, named by its VM id, back into a VM (VIM MarkAsVirtualMachine) "
            "and places it in the given resource pool, optionally on a given host. The VM "
            "stays powered off. Waits for approval."
        ),
        "parameter_schema": VM_TEMPLATE_UNMARK_PARAMETER_SCHEMA,
        "response_schema": VM_TEMPLATE_UNMARK_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "template", "vi-json"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_TEMPLATE_UNMARK_LLM_INSTRUCTIONS,
    },
)
