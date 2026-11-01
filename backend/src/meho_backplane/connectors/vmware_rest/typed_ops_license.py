# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed reads for vSphere licences: ``vmware.license.list`` and ``.assigned.list``.

* ``vmware.license.list`` -- the licences in the vCenter's licence inventory
  (``LicenseManager.licenses``).
* ``vmware.license.assigned.list`` -- which licence each asset uses: the
  vCenter, every host and vSAN clusters
  (``LicenseAssignmentManager.QueryAssignedLicenses``).

Both are bound methods on :class:`VmwareRestConnector` in the
:mod:`~meho_backplane.connectors.vmware_rest.typed_ops` mould. They read the
vCenter directly on the connector session, so they work on a fresh install
with no spec ingest. The shared reads, the key mask and the row shapes are in
:mod:`.license_common`. The write that assigns a licence is
``vmware.composite.license.assign`` (:mod:`.composites._license`).

No result holds a full licence key: every row carries ``key_masked`` (only
the last 5 characters visible). The tag ``license-key`` keeps the vCenter
answers, which hold every key in clear, out of the flight recorder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.connectors.vmware_rest.license_common import (
    ASSIGNMENT_ROW_SCHEMA,
    LICENSE_GROUP_KEY,
    LICENSE_KEY_TAG,
    LICENSE_ROW_SCHEMA,
    LICENSE_WHEN_TO_USE,
    assignment_row,
    license_row,
    query_assigned_licenses,
    read_license_inventory,
)
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "LICENSE_GROUP_KEY",
    "LICENSE_WHEN_TO_USE",
    "VMWARE_LICENSE_ASSIGNED_LIST_OP",
    "VMWARE_LICENSE_LIST_OP",
    "license_assigned_list_impl",
    "license_list_impl",
]


def _log() -> Any:
    """Get the logger per call, so ``structlog.testing.capture_logs`` sees every event."""
    return structlog.get_logger(__name__)


async def license_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.license.list``. Returns ``{"licenses": [...]}``."""
    del params  # the schema declares no params
    inventory = await read_license_inventory(connector, target, operator)
    rows = [license_row(info) for info in inventory.licenses]
    _log().info("vmware_license_list_read", target=target.name, license_count=len(rows))
    return {"licenses": rows}


async def license_assigned_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.license.assigned.list``. Returns ``{"assignments": [...]}``.

    Reads the ``LicenseAssignmentManager`` moId off ``LicenseManager``, then
    asks it for every asset, or for ``entity_id`` only.
    """
    entity_id = params.get("entity_id")
    inventory = await read_license_inventory(connector, target, operator)
    if inventory.assignment_manager is None:
        raise RuntimeError(
            "vmware.license.assigned.list: this target has no LicenseAssignmentManager; "
            "run it on a vCenter"
        )
    assignments = await query_assigned_licenses(
        connector,
        target,
        operator,
        assignment_manager=inventory.assignment_manager,
        entity_id=entity_id if isinstance(entity_id, str) else None,
    )
    rows = [assignment_row(assignment) for assignment in assignments]
    _log().info(
        "vmware_license_assigned_list_read",
        target=target.name,
        assignment_count=len(rows),
        filtered=entity_id is not None,
    )
    return {"assignments": rows}


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_LICENSE_TAGS: tuple[str, ...] = ("read-only", "vmware", "vcenter", "license", LICENSE_KEY_TAG)

VMWARE_LICENSE_LIST_OP = VmwareTypedOp(
    op_id="vmware.license.list",
    handler_attr="license_list",
    summary="List a vCenter's licences, each key masked to its last 5 characters.",
    description=(
        "Reads LicenseManager.licenses. Returns one row per licence: name, edition, "
        "product, used and total units, cost unit, expiry and labels. The key is masked; "
        "MEHO never returns a full key. vCenter only. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "licenses": {
                "type": "array",
                "description": "One row per licence in the vCenter's licence inventory.",
                "items": LICENSE_ROW_SCHEMA,
            }
        },
        "required": ["licenses"],
    },
    group_key=LICENSE_GROUP_KEY,
    tags=_LICENSE_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which licences a vCenter holds: name, product, edition, used "
            "and total units, and end date. Call it before "
            "vmware.composite.license.assign to find the licence name."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{licenses: [{name, edition_key, product_name, product_version, key_masked, "
            "is_evaluation, total, used, cost_unit, expires_at, labels}]}. key_masked "
            "shows only the last 5 characters; MEHO never returns a full key."
        ),
        "next_step": (
            "To see which asset uses which licence, call vmware.license.assigned.list. "
            "To assign a licence, pass its name to vmware.composite.license.assign; add "
            "the last 5 characters of key_masked as key_suffix when two licences share "
            "a name."
        ),
    },
)

VMWARE_LICENSE_ASSIGNED_LIST_OP = VmwareTypedOp(
    op_id="vmware.license.assigned.list",
    handler_attr="license_assigned_list",
    summary="List which licence each vCenter asset uses: the vCenter, every host, vSAN clusters.",
    description=(
        "Calls LicenseAssignmentManager.QueryAssignedLicenses for every asset or one "
        "entity_id. Returns one row per asset: id, name, product, licence name, masked "
        "key, evaluation or not, and end date. vCenter only. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": (
                    "Optional. Read one asset only: a host moid (for example "
                    "'host-12') or the vCenter's instance UUID, as entity_id shows it. "
                    "Omit it to read every asset."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{}, {"entity_id": "<host-moid>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "description": "One row per licensed asset.",
                "items": ASSIGNMENT_ROW_SCHEMA,
            }
        },
        "required": ["assignments"],
    },
    group_key=LICENSE_GROUP_KEY,
    tags=_LICENSE_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which licence each asset uses: the vCenter, every host and "
            "vSAN clusters. Use it when a host shows an expired or evaluation licence, "
            "or before and after vmware.composite.license.assign."
        ),
        "parameter_hints": {
            "entity_id": "Omit it for every asset, or pass one entity_id from an earlier answer.",
        },
        "output_shape": (
            "{assignments: [{entity_id, entity_name, scope, product_name, "
            "product_version, license_name, edition_key, key_masked, is_evaluation, "
            "expires_at}]}. A host's entity_id is its moid; the vCenter's is its "
            "instance UUID."
        ),
        "next_step": (
            "For an asset on the evaluation licence (is_evaluation true) or with an end "
            "date in the past, pick a licence from vmware.license.list and call "
            "vmware.composite.license.assign with this entity_id."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_LICENSE_LIST_OP, VMWARE_LICENSE_ASSIGNED_LIST_OP)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {LICENSE_GROUP_KEY: LICENSE_WHEN_TO_USE}
