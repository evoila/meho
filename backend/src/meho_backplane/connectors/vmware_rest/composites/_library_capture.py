# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Capture a VM into a content library, and the shared write step of this family.

``vmware.composite.vm.capture_to_library`` (``caution``, waits for approval)
saves a VM into a LOCAL library, as an OVF template
(``POST /vcenter/ovf/library-item``) or as a VM template
(``POST /vcenter/vm-template/library-items``). Both calls are synchronous:
vCenter answers when the item is complete. MEHO then reads the item back. An
item name that is already taken gives ``already_exists`` and changes nothing,
so a retry after a lost answer is safe.

The OVF capture sends a ``Client-Token`` header: a new UUID per request,
given back in the answer. vCenter makes nothing new when the same request
comes again with the same token. The VM-template create takes no such token.

:func:`governed_send` is the write step of the capture, the publish change
(:mod:`._library_publish`) and the VM export (:mod:`._library_export`): the
shared write check of the composites, plus request headers and check
parameters that differ from the body.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._library_checks import (
    OP_GET_LIBRARY,
    OP_GET_LIBRARY_ITEM,
    get_or_none,
    id_problem,
    int_or_none,
    resolve_library_id,
)
from meho_backplane.connectors.vmware_rest.composites._library_export_schemas import (
    CAPTURE_LLM_INSTRUCTIONS,
    CAPTURE_PARAMETER_SCHEMA,
    CAPTURE_RESPONSE_SCHEMA,
    TEMPLATE_OVF,
    TEMPLATE_VM,
)
from meho_backplane.connectors.vmware_rest.composites._library_local import LOCAL
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import moid_problem
from meho_backplane.connectors.vmware_rest.composites._write import (
    _LIBRARY_ITEM_DEPLOY_TIMEOUT,
    _OP_FIND_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    _find_content_library_ids,
    _parse_vsphere_error,
    _split_sub_op,
    _step_connector_id,
    _unwrap_value,
    _vsphere_fault_detail,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import VM_MOID_PATTERN
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
    register_preview_builder,
)
from meho_backplane.operations.composite import enforce_subop_policy

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "CAPTURE_COMPOSITE",
    "OP_CAPTURE_OVF",
    "capture_ovf",
    "failed_answer",
    "governed_send",
    "item_answer",
    "item_named",
    "local_library",
    "ovf_messages",
    "vm_capture_to_library_composite",
]

OP_ID_CAPTURE: Final = "vmware.composite.vm.capture_to_library"

OP_GET_VM: Final = "GET:/vcenter/vm/{vm}"
OP_CAPTURE_OVF: Final = "POST:/vcenter/ovf/library-item"
OP_CAPTURE_VM_TEMPLATE: Final = "POST:/vcenter/vm-template/library-items"

#: The vCenter header that makes a create safe to send twice.
CLIENT_TOKEN_HEADER: Final = "Client-Token"

#: Every REST call of the capture (reads and writes). The reconcile test checks
#: them against the pinned vCenter 8.0 and 9.0 API files, and
#: ``_governed_subops`` lists them for the grant set.
_SUB_OPS_VM_CAPTURE_TO_LIBRARY: Final[tuple[str, ...]] = (
    OP_GET_VM,
    _OP_FIND_LIBRARY,
    OP_GET_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    OP_GET_LIBRARY_ITEM,
    OP_CAPTURE_OVF,
    OP_CAPTURE_VM_TEMPLATE,
)


async def governed_send(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    op_id: str,
    *,
    path_params: dict[str, Any],
    body: dict[str, Any],
    check_params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: Any = httpx.USE_CLIENT_DEFAULT,
) -> tuple[OperationResult | None, Any]:
    """Check one write step, then send it on the connector session.

    The same check as :func:`._write._write_sub_op`, with two more inputs:
    *check_params* is what the check and an approval request show (default:
    the path parameters and the body), so a body that holds a password can
    show its Vault reference instead; *headers* go to vCenter with the call
    (the ``Client-Token``). Returns ``(gate, None)`` when the step waits for
    approval or is denied, else ``(None, payload)``.
    """
    gate = await enforce_subop_policy(
        operator=operator,
        connector_id=_step_connector_id(connector),
        op_id=op_id,
        safety_level=_write._WRITE_SAFETY_LEVEL,
        requires_approval=_write._WRITE_REQUIRES_APPROVAL,
        target=target,
        params=check_params if check_params is not None else {**path_params, **body},
    )
    if gate is not None:
        return gate, None
    method, path, _ = _split_sub_op(op_id, path_params)
    mounted = await connector.mount_op_path(target, path, operator)
    payload = await connector._post_json(
        target,
        mounted,
        operator=operator,
        verb=method,
        json=body,
        extra_headers=headers,
        timeout=timeout,
    )
    return None, payload


def item_answer(item_id: str, model: dict[str, Any] | None) -> dict[str, Any] | None:
    """The ``item`` answer field; vCenter's item read leaves ``id`` out."""
    if model is None:
        return None
    return {
        "library_item_id": item_id,
        "name": model.get("name"),
        "type": model.get("type"),
        "size": int_or_none(model.get("size")),
        "content_version": model.get("content_version"),
    }


def failed_answer(answer: dict[str, Any], exc: httpx.HTTPError, what: str) -> dict[str, Any]:
    _status, error_type = _parse_vsphere_error(exc)
    detail = _vsphere_fault_detail(exc)
    return {
        **answer,
        "status": "failed",
        "error_type": error_type,
        "guidance": f"{what} failed: {detail}",
        "issues": [issue("connector", "error", f"{what} failed: {detail}")],
    }


async def local_library(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    """``(library id, None)`` for a LOCAL library, or ``(None, answer)`` to return."""
    library_id, refusal = await resolve_library_id(connector, target, operator, params)
    if refusal is not None:
        return None, {"status": refusal[0], "guidance": refusal[1]}
    model = (
        await get_or_none(connector, target, operator, OP_GET_LIBRARY, {"libraryId": library_id})
        if library_id is not None
        else None
    )
    if library_id is None or model is None:
        return None, {"status": "not_found", "guidance": "No such library; nothing was changed."}
    if model.get("type") != LOCAL:
        return None, {
            "status": "precondition_failed",
            "library_id": library_id,
            "guidance": f"Library {library_id!r} is {model.get('type')!r}, not LOCAL; nothing "
            "was changed.",
        }
    return library_id, None


async def item_named(
    connector: VmwareRestConnector, target: Any, operator: Operator, library_id: str, name: str
) -> str | None:
    """The id of the item called *name* in the library, or ``None``."""
    ids = await _find_content_library_ids(
        connector,
        target,
        operator,
        op_id=_OP_FIND_LIBRARY_ITEM,
        spec={"library_id": library_id, "name": name},
    )
    valid = [found for found in ids if id_problem(found, "item id") is None]
    return valid[0] if valid else None


# ===========================================================================
# vm.capture_to_library
# ===========================================================================


async def capture_ovf(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm: str,
    library_id: str,
    item_name: str,
    description: str | None = None,
) -> tuple[OperationResult | None, str, dict[str, Any]]:
    """Run the OVF capture; ``(gate, client_token, CreateResult)``.

    Raises :exc:`httpx.HTTPError` on a vCenter fault. Shared with the VM
    export of :mod:`._library_export`.
    """
    client_token = str(uuid.uuid4())
    create_spec: dict[str, Any] = {"name": item_name}
    if description:
        create_spec["description"] = description
    body = {
        "source": {"type": "VirtualMachine", "id": vm},
        "target": {"library_id": library_id},
        "create_spec": create_spec,
    }
    gate, payload = await governed_send(
        connector,
        target,
        operator,
        OP_CAPTURE_OVF,
        path_params={},
        body=body,
        headers={CLIENT_TOKEN_HEADER: client_token},
        timeout=_LIBRARY_ITEM_DEPLOY_TIMEOUT,
    )
    result = _unwrap_value(payload) if gate is None else None
    return gate, client_token, result if isinstance(result, dict) else {}


def ovf_messages(result: dict[str, Any]) -> list[str]:
    """The error texts of an OVF ``CreateResult``."""
    error = result.get("error")
    entries = error.get("errors") if isinstance(error, dict) else None
    messages: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        text = message.get("default_message") if isinstance(message, dict) else None
        messages.append(str(text or entry.get("category") or "unknown OVF error"))
    return messages


def _capture_problem(params: dict[str, Any]) -> str | None:
    problem = moid_problem(params.get("vm"), VM_MOID_PATTERN, "vm")
    if problem is not None:
        return problem
    if params.get("template_type", TEMPLATE_OVF) == TEMPLATE_VM and not params.get("placement"):
        return "a VM_TEMPLATE capture needs placement (a folder, resource pool, host or cluster)"
    return None


def _vm_template_body(params: dict[str, Any], library_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source_vm": params["vm"],
        "name": params["item_name"],
        "library": library_id,
        "placement": dict(params["placement"]),
    }
    if params.get("description"):
        body["description"] = params["description"]
    if params.get("datastore"):
        body["vm_home_storage"] = {"datastore": params["datastore"]}
        body["disk_storage"] = {"datastore": params["datastore"]}
    return body


async def _capture_plan(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    answer: dict[str, Any],
) -> dict[str, Any] | None:
    """Read-only checks; the answer to return, or ``None`` to go on. Fills ``library_id``."""
    problem = _capture_problem(params)
    if problem is not None:
        return {**answer, "status": "invalid_request", "guidance": problem}
    vm = await get_or_none(connector, target, operator, OP_GET_VM, {"vm": params["vm"]})
    if vm is None:
        return {**answer, "status": "not_found", "guidance": "No such VM; nothing was changed."}
    library_id, refusal = await local_library(connector, target, operator, params)
    if refusal is not None or library_id is None:
        return {**answer, **(refusal or {})}
    answer["library_id"] = library_id
    existing = await item_named(connector, target, operator, library_id, params["item_name"])
    if existing is None:
        return None
    model = await get_or_none(
        connector, target, operator, OP_GET_LIBRARY_ITEM, {"libraryItemId": existing}
    )
    return {
        **answer,
        "status": "already_exists",
        "library_item_id": existing,
        "item": item_answer(existing, model),
        "guidance": "The library already has an item with this name; nothing was changed. "
        "Pick another item_name, or delete the item first.",
    }


async def vm_capture_to_library_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Capture a VM into a local library (``vm.capture_to_library``)."""
    template_type = params.get("template_type", TEMPLATE_OVF)
    answer: dict[str, Any] = {
        "vm": params.get("vm"),
        "library_id": None,
        "template_type": template_type,
        "library_item_id": None,
        "client_token": None,
        "item": None,
        "error_type": None,
        "messages": [],
        "guidance": None,
    }
    stop = await _capture_plan(connector, target, operator, params, answer)
    if stop is not None:
        return stop
    library_id = str(answer["library_id"])
    try:
        if template_type == TEMPLATE_VM:
            gate, payload = await governed_send(
                connector,
                target,
                operator,
                OP_CAPTURE_VM_TEMPLATE,
                path_params={},
                body=_vm_template_body(params, library_id),
                timeout=_LIBRARY_ITEM_DEPLOY_TIMEOUT,
            )
            item_id = _unwrap_value(payload)
        else:
            gate, answer["client_token"], result = await capture_ovf(
                connector,
                target,
                operator,
                vm=params["vm"],
                library_id=library_id,
                item_name=params["item_name"],
                description=params.get("description"),
            )
            item_id = result.get("ovf_library_item_id") if result.get("succeeded") else None
            if gate is None and item_id is None:
                messages = ovf_messages(result)
                return {
                    **answer,
                    "status": "failed",
                    "messages": messages,
                    "guidance": "vCenter could not capture the VM: "
                    + ("; ".join(messages) or "no reason given"),
                    "issues": [issue("connector", "error", "the OVF capture did not succeed")],
                }
    except httpx.HTTPError as exc:
        return failed_answer(answer, exc, "The capture")
    if gate is not None:
        return gate
    if id_problem(item_id, "item id") is not None:
        raise RuntimeError(f"vCenter answered the capture with no item id ({item_id!r})")
    model = await get_or_none(
        connector, target, operator, OP_GET_LIBRARY_ITEM, {"libraryItemId": item_id}
    )
    return {
        **answer,
        "status": "captured",
        "library_item_id": item_id,
        "item": item_answer(str(item_id), model),
        "guidance": "The VM is saved in the library."
        if model is not None
        else "vCenter made the item, but it does not read back yet. Check it with "
        "vmware.content_library.item.get.",
    }


async def _capture_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the VM, the library and whether the item name is free."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    params = dict(ctx.params)
    effect: dict[str, Any] = {
        "vm": params.get("vm"),
        "item_name": params.get("item_name"),
        "template_type": params.get("template_type", TEMPLATE_OVF),
    }
    answer: dict[str, Any] = {"library_id": None}
    try:
        stop = await _capture_plan(connector, ctx.target, ctx.operator, params, answer)
    except Exception as exc:
        return {
            **effect,
            "summary": "MEHO could not read vCenter, so it cannot check the VM and library.",
            "read_error": preview_failure_reason(exc),
        }
    effect["library_id"] = answer.get("library_id")
    if stop is not None:
        return {**effect, "summary": f"The call will change nothing: {stop.get('guidance')}"}
    return {
        **effect,
        "summary": f"vCenter saves VM {params.get('vm')!r} as a new item "
        f"{params.get('item_name')!r} of library {answer.get('library_id')!r}.",
    }


register_preview_builder(OP_ID_CAPTURE, _capture_preview)


#: The registration rows, as ``_CompositeSpec`` keyword arguments.
CAPTURE_COMPOSITE: Final[dict[str, Any]] = {
    "op_id": OP_ID_CAPTURE,
    "handler": vm_capture_to_library_composite,
    "summary": "Capture a VM into a local content library as an OVF template or VM template.",
    "description": (
        "Saves one VM into a local content library as a new item: an OVF template or a "
        "vSphere VM template. Reads the new item back. An item name that is taken gives "
        "already_exists and changes nothing. Waits until a second person approves."
    ),
    "parameter_schema": CAPTURE_PARAMETER_SCHEMA,
    "response_schema": CAPTURE_RESPONSE_SCHEMA,
    "group_key": "content_library",
    "tags": ["composite", "write", "content-library", "vm", "ovf", "capture"],
    "safety_level": "caution",
    "requires_approval": True,
    "llm_instructions": CAPTURE_LLM_INSTRUCTIONS,
}
