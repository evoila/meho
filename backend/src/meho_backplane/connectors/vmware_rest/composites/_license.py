# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.composite.license.assign`` -- give one asset a licence from the inventory.

The asset is a host or the vCenter itself (any asset that
``vmware.license.assigned.list`` shows). The caller names the licence by its
name, by the last 5 characters of its key (``key_suffix``), or by both. The
handler finds the full key in ``LicenseManager.licenses`` itself, so the key
is never a parameter. It is not in the approval request, the audit row, the
park-time preview or a log line, and the result shows only the masked key.

Flow (:func:`_plan` is shared with the park-time preview):

1. One property read: the licence inventory and the
   ``LicenseAssignmentManager`` moId.
2. Pick the licence. ``license_not_found`` / ``license_ambiguous`` stop the
   call before any change.
3. ``QueryAssignedLicenses`` for the asset. ``asset_not_found`` stops the
   call; ``unchanged`` when the asset already uses that licence.
4. ``UpdateAssignedLicense(entity, licenseKey, entityDisplayName)`` through
   the governed VI-JSON seam :func:`._write._write_vmomi_sub_op`. The gate
   gets the asset id, the licence name and the masked key, never the key. An
   error is raised as it is. The tag ``license-key`` makes the dispatcher
   replace the vendor's text in the error result and the audit row
   (:mod:`meho_backplane.operations._vendor_text`), so a fault that repeats
   the key does not show it.

``UpdateAssignedLicense`` is synchronous (no vCenter task). Its answer is the
licence now assigned; the result shows it masked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.vmware_rest.composites._write import _write_vmomi_sub_op
from meho_backplane.connectors.vmware_rest.license_common import (
    LICENSE_ROW_SCHEMA,
    OP_QUERY_ASSIGNED_LICENSES,
    OP_RETRIEVE_PROPERTIES,
    OP_UPDATE_ASSIGNED_LICENSE,
    assignment_row,
    license_row,
    mask_license_key,
    query_assigned_licenses,
    read_license_inventory,
)
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "LICENSE_ASSIGN_LLM_INSTRUCTIONS",
    "LICENSE_ASSIGN_PARAMETER_SCHEMA",
    "LICENSE_ASSIGN_RESPONSE_SCHEMA",
    "license_assign_composite",
    "license_assign_preview",
]

_OP_ID: Final = "vmware.composite.license.assign"

#: vi-json sub-op manifest for ``license.assign``: the inventory read, the
#: assignment read and the gated assignment write.
_VIM_SUB_OPS_LICENSE_ASSIGN: Final[tuple[str, ...]] = (
    OP_RETRIEVE_PROPERTIES,
    OP_QUERY_ASSIGNED_LICENSES,
    OP_UPDATE_ASSIGNED_LICENSE,
)

_KEY_SUFFIX_LENGTH: Final = 5


def _log() -> Any:
    """Get the logger per call, so ``structlog.testing.capture_logs`` sees every event."""
    return structlog.get_logger(__name__)


@dataclass(frozen=True)
class _Plan:
    """What the assignment would do. ``selected`` and ``asset`` hold FULL keys.

    Never return, log or audit ``selected`` or ``asset``; the envelope and the
    preview use their masked rows. ``stop`` is the status envelope that ends
    the call before the write (a refusal or ``unchanged``), else ``None``.
    """

    assignment_manager: str
    selected: dict[str, Any] | None
    asset: dict[str, Any] | None
    candidates: list[dict[str, Any]]
    stop: dict[str, Any] | None


def _same_name(value: Any, wanted: str) -> bool:
    return isinstance(value, str) and value.casefold() == wanted.casefold()


def _matches(info: dict[str, Any], name: str | None, suffix: str | None) -> bool:
    key = info.get("licenseKey")
    if not isinstance(key, str) or not key:
        return False
    if name is not None and not _same_name(info.get("name"), name):
        return False
    return suffix is None or key[-_KEY_SUFFIX_LENGTH:].casefold() == suffix.casefold()


def _asked_for(name: str | None, suffix: str | None) -> str:
    parts = [f"name {name!r}"] if name is not None else []
    if suffix is not None:
        parts.append(f"key ending in {suffix!r}")
    return " and ".join(parts)


def _select(
    licenses: tuple[dict[str, Any], ...], name: str | None, suffix: str | None
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
    """Pick one licence; return ``(licence, candidates, stop)``."""
    if name is None and suffix is None:
        # The schema already asks for one of them; never pick a licence blind.
        return (
            None,
            [],
            {"status": "license_not_found", "guidance": "pass license_name or key_suffix"},
        )
    matches = [info for info in licenses if _matches(info, name, suffix)]
    if len(matches) == 1:
        return matches[0], [], None
    if not matches:
        return (
            None,
            [],
            {
                "status": "license_not_found",
                "guidance": (
                    "no licence in the vCenter's licence inventory has "
                    f"{_asked_for(name, suffix)}; call vmware.license.list to see the names "
                    "and masked keys. This call does not add new keys."
                ),
            },
        )
    candidates = [license_row(info) for info in matches]
    return (
        None,
        candidates,
        {
            "status": "license_ambiguous",
            "guidance": (
                f"{len(matches)} licences have {_asked_for(name, suffix)}; call again with "
                "key_suffix set to the last 5 characters of key_masked of one candidate"
            ),
        },
    )


def _asset_of(assignments: list[dict[str, Any]], entity_id: str) -> dict[str, Any] | None:
    for assignment in assignments:
        if assignment.get("entityId") == entity_id:
            return assignment
    return None


def _assigned_key(asset: dict[str, Any]) -> Any:
    info = asset.get("assignedLicense")
    return info.get("licenseKey") if isinstance(info, dict) else None


async def _plan(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> _Plan:
    """Read the inventory and the asset, pick the licence; no change is made."""
    entity_id: str = params["entity_id"]
    name = params.get("license_name")
    suffix = params.get("key_suffix")
    inventory = await read_license_inventory(connector, target, operator)
    if inventory.assignment_manager is None:
        raise RuntimeError(
            f"{_OP_ID}: this target has no LicenseAssignmentManager; run it on a vCenter"
        )
    selected, candidates, stop = _select(inventory.licenses, name, suffix)
    if selected is None:
        return _Plan(inventory.assignment_manager, None, None, candidates, stop)
    assignments = await query_assigned_licenses(
        connector,
        target,
        operator,
        assignment_manager=inventory.assignment_manager,
        entity_id=entity_id,
    )
    asset = _asset_of(assignments, entity_id)
    if asset is None:
        stop = {
            "status": "asset_not_found",
            "guidance": (
                f"vCenter knows no licensed asset with entity_id {entity_id!r}; call "
                "vmware.license.assigned.list to see the asset ids"
            ),
        }
    elif _assigned_key(asset) == selected.get("licenseKey"):
        stop = {
            "status": "unchanged",
            "guidance": "the asset already uses this licence; nothing was changed",
        }
    return _Plan(inventory.assignment_manager, selected, asset, candidates, stop)


def _before(asset: dict[str, Any] | None) -> dict[str, Any] | None:
    if asset is None:
        return None
    row = assignment_row(asset)
    return {key: row[key] for key in ("license_name", "key_masked", "is_evaluation", "expires_at")}


def _entity_name(asset: dict[str, Any] | None) -> str | None:
    return assignment_row(asset)["entity_name"] if asset is not None else None


async def _update_assigned_license(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    plan: _Plan,
    selected: dict[str, Any],
    entity_id: str,
) -> tuple[OperationResult | None, Any]:
    """Gate, then send ``UpdateAssignedLicense``.

    The full key is only in the request body. The gate params name the asset
    and the licence by name and masked key. An error is raised as it is; the
    dispatcher hides the vendor's text of this operation (see the module
    docstring).
    """
    key: str = selected["licenseKey"]
    name = selected.get("name")
    body: dict[str, Any] = {"entity": entity_id, "licenseKey": key}
    display_name = _entity_name(plan.asset)
    if display_name is not None:
        body["entityDisplayName"] = display_name
    return await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=OP_UPDATE_ASSIGNED_LICENSE,
        vmomi_path=f"/LicenseAssignmentManager/{plan.assignment_manager}/UpdateAssignedLicense",
        body=body,
        params={
            "entity_id": entity_id,
            "license_name": name,
            "key_masked": mask_license_key(key),
        },
    )


async def license_assign_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Assign one licence from the inventory to one asset (a host or the vCenter).

    Op-id: ``vmware.composite.license.assign``. A refusal or ``unchanged``
    returns before any write. A policy gate returns its
    :class:`OperationResult` as is (nothing on the wire). An error of the
    write is raised as it is, so the dispatcher sorts it as usual.
    """
    entity_id: str = params["entity_id"]
    plan = await _plan(connector, target, operator, params)
    envelope: dict[str, Any] = {
        "entity_id": entity_id,
        "entity_name": _entity_name(plan.asset),
        "license": license_row(plan.selected) if plan.selected is not None else None,
        "before": _before(plan.asset),
        "candidates": plan.candidates,
        "guidance": None,
    }
    if plan.stop is not None:
        return {**envelope, **plan.stop}
    if plan.selected is None:  # pragma: no cover - _plan sets stop when no licence is picked
        raise RuntimeError(f"{_OP_ID}: no licence was picked")

    gate, payload = await _update_assigned_license(
        connector, target, operator, plan, plan.selected, entity_id
    )
    if gate is not None:
        return gate
    assigned = unwrap_vim_value(payload)
    if isinstance(assigned, dict) and assigned.get("licenseKey"):
        envelope["license"] = license_row(assigned)
    _log().info(
        "vmware_license_assigned",
        target=getattr(target, "name", None),
        entity_id=entity_id,
        license_name=envelope["license"]["name"] if envelope["license"] else None,
        key_masked=envelope["license"]["key_masked"] if envelope["license"] else None,
    )
    return {**envelope, "status": "assigned"}


async def license_assign_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time preview: the asset's licence now, and the licence it would get.

    Runs the handler's own read-only plan (:func:`_plan`); the write never
    fires here. Keys are masked. ``outcome`` is ``assign``, or the status the
    call would stop with (for example ``license_ambiguous``). Declines
    (``None``) without a resolved connector or an ``entity_id``.
    """
    entity_id = ctx.params.get("entity_id")
    if not isinstance(entity_id, str) or ctx.connector_instance is None:
        return None
    plan = await _plan(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        ctx.params,
    )
    return {
        "entity_id": entity_id,
        "entity_name": _entity_name(plan.asset),
        "current": _before(plan.asset),
        "requested": license_row(plan.selected) if plan.selected is not None else None,
        "outcome": plan.stop["status"] if plan.stop is not None else "assign",
    }


# ---------------------------------------------------------------------------
# Schemas + AI guidance
# ---------------------------------------------------------------------------

LICENSE_ASSIGN_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "The asset to license, as entity_id shows it in "
                "vmware.license.assigned.list: a host moid (for example 'host-12') or "
                "the vCenter's instance UUID."
            ),
        },
        "license_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "Name of the licence, as vmware.license.list shows it. Letter case does not matter."
            ),
        },
        "key_suffix": {
            "type": "string",
            "pattern": "^[A-Za-z0-9]{5}$",
            "description": (
                "The last 5 characters of the licence key, as key_masked shows them. "
                "Use it when two licences share a name."
            ),
        },
    },
    "required": ["entity_id"],
    "anyOf": [{"required": ["license_name"]}, {"required": ["key_suffix"]}],
    "additionalProperties": False,
    "examples": [{"entity_id": "<host-moid>", "license_name": "<licence-name>"}],
}

_BEFORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "The licence the asset used before this call. The key is masked.",
    "properties": {
        "license_name": {"type": ["string", "null"], "description": "Licence name."},
        "key_masked": {"type": ["string", "null"], "description": "Masked licence key."},
        "is_evaluation": {"type": "boolean", "description": "True for the evaluation licence."},
        "expires_at": {"type": ["string", "null"], "description": "End date (ISO 8601)."},
    },
}

LICENSE_ASSIGN_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "assigned",
                "unchanged",
                "license_not_found",
                "license_ambiguous",
                "asset_not_found",
            ],
            "description": (
                "'assigned': vCenter took the new licence. 'unchanged': the asset "
                "already used it; nothing was sent. 'license_not_found' / "
                "'license_ambiguous' / 'asset_not_found': refused before any change; "
                "see guidance. A vendor fault is an error result, not a status."
            ),
        },
        "entity_id": {"type": "string", "description": "The asset id from the call."},
        "entity_name": {"type": ["string", "null"], "description": "Display name of the asset."},
        "license": {
            "anyOf": [LICENSE_ROW_SCHEMA, {"type": "null"}],
            "description": "The licence assigned, or picked for the asset. The key is masked.",
        },
        "before": {"anyOf": [_BEFORE_SCHEMA, {"type": "null"}]},
        "candidates": {
            "type": "array",
            "items": LICENSE_ROW_SCHEMA,
            "description": "For 'license_ambiguous': the licences that match. Keys are masked.",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "What to do next, for a status that stopped the call.",
        },
    },
    "required": ["status", "entity_id", "license", "before", "candidates", "guidance"],
}

LICENSE_ASSIGN_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to give one asset a licence from the vCenter's licence inventory: for "
        "example a host whose evaluation licence has ended, or the vCenter itself. The "
        "call waits until a second person approves it."
    ),
    "parameter_hints": {
        "entity_id": (
            "entity_id from vmware.license.assigned.list: a host moid or the vCenter's UUID."
        ),
        "license_name": "name from vmware.license.list.",
        "key_suffix": "The last 5 characters of key_masked; add it when two licences share a name.",
    },
    "preconditions": (
        "The licence must already be in the vCenter's licence inventory "
        "(vmware.license.list); this call does not add keys. Check there that it has "
        "enough free units (total minus used) for the asset. On vCenter 9, licences are "
        "usually managed in VCF Operations; if vCenter refuses, use the vrops licensing "
        "operations."
    ),
    "output_shape": (
        "{status, entity_id, entity_name, license: {name, key_masked, edition_key, "
        "total, used, expires_at, ...}, before: {license_name, key_masked, is_evaluation, "
        "expires_at}, candidates, guidance}. status is assigned, unchanged, "
        "license_not_found, license_ambiguous or asset_not_found. Keys are masked."
    ),
    "next_step": (
        "Check the result with vmware.license.assigned.list for the same entity_id. For "
        "license_ambiguous, call again with key_suffix from one of the candidates."
    ),
}
