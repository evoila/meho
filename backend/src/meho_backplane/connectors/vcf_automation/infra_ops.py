# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the infrastructure ops: cloud accounts, cloud zones, profiles, project members.

Written once for Aria Automation 8.18 and VCF Automation 9 (the tenant plane of
a "VM Apps" org): both serve the same IaaS API. :func:`infra_ops` builds the
ops for one op id prefix; this connector registers them as ``vcfa.tenant.*``
(:data:`VCFA_INFRA_OPS`) and the vRA 8 connector as ``vcfa.vra8.*``
(:mod:`meho_backplane.connectors.vra8.infra_ops`). The handlers are in
:mod:`._infra` and :mod:`._infra_changes`.

Two ops exist only on vRA 8, because VCF Automation 9 already has them:
``zone.list`` (``vcfa.tenant.zone.list``) and ``project.create``
(``vcfa.tenant.project.create``).

=====================================  ===========  ========  =====================================
op (after the prefix)                  risk level   approval  vendor call
=====================================  ===========  ========  =====================================
``cloud_account.list``                 safe         no        GET /iaas/api/cloud-accounts
``cloud_account.create``               dangerous    yes       POST /iaas/api/cloud-accounts-vsphere
                                                              or -nsx-t
``cloud_account.delete``               destructive  yes       DELETE /iaas/api/cloud-accounts/{id}
``zone.list`` (vRA 8 only)             safe         no        GET /iaas/api/zones
``zone.set``                           dangerous    yes       POST /iaas/api/zones,
                                                              PATCH /iaas/api/zones/{id}
``flavor_profile.list``                safe         no        GET /iaas/api/flavor-profiles
``flavor_profile.set``                 dangerous    yes       POST /iaas/api/flavor-profiles,
                                                              PATCH /iaas/api/flavor-profiles/{id}
``network_profile.list``               safe         no        GET /iaas/api/network-profiles,
                                                              GET /iaas/api/network-ip-ranges
``storage_profile.list``               safe         no        GET /iaas/api/storage-profiles
``project.members.set``                dangerous    yes       PATCH /iaas/api/projects/{id}
``project.create`` (vRA 8 only)        caution      yes       POST /iaas/api/projects
=====================================  ===========  ========  =====================================
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_automation._infra_changes import MEMBER_ROLES
from meho_backplane.connectors.vcf_automation._infra_op_base import (
    INFRA_GROUP,
    STATUS_NOTE,
    S,
    change,
    change_answer,
    id_param,
    text_param,
)
from meho_backplane.connectors.vcf_automation._infra_paths import PROJECT_PATH, PROJECTS_PATH
from meho_backplane.connectors.vcf_automation._infra_specs import (
    cloud_account_create_op,
    cloud_account_delete_op,
    cloud_account_list_op,
    flavor_profile_list_op,
    flavor_profile_set_op,
    network_profile_list_op,
    storage_profile_list_op,
    zone_list_op,
    zone_set_op,
)
from meho_backplane.connectors.vcf_automation._op_schemas import field, list_of
from meho_backplane.connectors.vcf_automation.provisioning_ops import _PROJECT_CREATE
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp, validate_typed_ops

__all__ = ["INFRA_GROUP", "VCFA_INFRA_OPS", "VCFA_INFRA_OP_IDS", "infra_ops"]

# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def _members_set(prefix: str) -> VcfaTypedOp:
    principal = {
        "email": text_param("The user's email or the group name."),
        "type": {
            "type": "string",
            "enum": ["user", "group"],
            "description": "user (default) or group.",
        },
    }
    add_item = {
        "type": "object",
        "description": "One user or group with its role.",
        "properties": {
            **principal,
            "role": {
                "type": "string",
                "enum": sorted(MEMBER_ROLES),
                "description": "The role in the project; it replaces any other role.",
            },
        },
        "required": ["email", "role"],
        "additionalProperties": False,
    }
    remove_item = {
        "type": "object",
        "description": "One user or group to take out of every role.",
        "properties": principal,
        "required": ["email"],
        "additionalProperties": False,
    }
    principal_fields = {
        "email": field("string", "The email or group name."),
        "type": field("string", "user or group."),
    }
    return change(
        prefix,
        name="project.members.set",
        path=PROJECT_PATH,
        summary="Add or remove project members and administrators.",
        description=(
            "Reads the project with GET /iaas/api/projects/{id}, then sends PATCH with only "
            "the role lists that change. Other members stay as they are."
        ),
        schema={
            "type": "object",
            "properties": {
                "project_id": id_param(f"The project id (from {prefix}.project.list)."),
                "add": {
                    "type": "array",
                    "items": add_item,
                    "description": "Users or groups to add.",
                },
                "remove": {
                    "type": "array",
                    "items": remove_item,
                    "description": "Users or groups to remove.",
                },
            },
            "required": ["project_id"],
            "anyOf": [{"required": ["add"]}, {"required": ["remove"]}],
            "additionalProperties": False,
            "examples": [
                {
                    "project_id": "<project-id>",
                    "add": [{"email": "<user-email>", "type": "user", "role": "member"}],
                }
            ],
        },
        response=change_answer(
            "project",
            "project",
            {
                "id": field(S, "The project id."),
                "name": field(S, "The project name."),
                **{
                    wire: list_of(f"The project {wire}.", principal_fields)
                    for wire in MEMBER_ROLES.values()
                },
            },
            ["updated", "unchanged", "invalid_request"],
        ),
        safety="dangerous",
        when="Call to give a user or group access to a project, change its role, or take it away.",
        preconditions=(
            "An administrator can change the project and its members. Check the project with "
            f"{prefix}.project.list first. A person must approve the call."
        ),
        shape=(
            "{status: updated | unchanged | invalid_request, project: {id, name, "
            "administrators, members, viewers, supervisors}, changed, guidance}. " + STATUS_NOTE
        ),
        next_step=f"Read {prefix}.project.list to check the members.",
        tag="project",
    )


def _renamed(value: Any, prefix: str) -> Any:
    """A copy of *value* whose texts name the ops of *prefix* instead of ``vcfa.tenant``."""
    if isinstance(value, str):
        return value.replace("vcfa.tenant.", f"{prefix}.")
    if isinstance(value, dict):
        return {key: _renamed(item, prefix) for key, item in value.items()}
    if isinstance(value, list):
        return [_renamed(item, prefix) for item in value]
    return value


def _project_create(prefix: str) -> VcfaTypedOp:
    return change(
        prefix,
        name="project.create",
        path=PROJECTS_PATH,
        summary="Create a project, with users and cloud zones; safe to run twice.",
        description=(
            "Sends POST /iaas/api/projects with optional users, cloud zones with a quota and "
            "network tag rules. A project with the same name answers 'unchanged'."
        ),
        schema=_renamed(_PROJECT_CREATE.parameter_schema, prefix),
        response=change_answer(
            "project",
            "project",
            {
                "id": field(S, "The project id."),
                "name": field("string", "The project name."),
                "orgId": field(S, "The id of the organization."),
            },
            ["created", "unchanged", "invalid_request"],
            changed=False,
        ),
        safety="caution",
        when="Call to create a project, the scope for deployments, with its users and zones.",
        preconditions=(
            f"Zone ids come from {prefix}.zone.list. The login user may create projects (a 403 "
            "means it may not). A person must approve the call."
        ),
        shape=(
            "{status: created | unchanged | invalid_request, project: {id, name, orgId}, "
            "guidance}. An existing project is not changed."
        ),
        next_step=f"Read {prefix}.project.list; change members with {prefix}.project.members.set.",
        tag="project",
    )


def infra_ops(prefix: str, *, with_vra8_only: bool) -> tuple[VcfaTypedOp, ...]:
    """The infrastructure ops for *prefix*, in the order of a set-up.

    ``with_vra8_only`` adds ``zone.list`` and ``project.create``, which VCF
    Automation 9 already has as other ops.
    """
    zone_list = f"{prefix}.zone.list"
    ops = [
        cloud_account_list_op(prefix),
        cloud_account_create_op(prefix),
        cloud_account_delete_op(prefix),
    ]
    if with_vra8_only:
        ops.append(zone_list_op(prefix))
    ops += [
        zone_set_op(prefix, zone_list),
        flavor_profile_list_op(prefix),
        flavor_profile_set_op(prefix),
        network_profile_list_op(prefix),
        storage_profile_list_op(prefix),
        _members_set(prefix),
    ]
    if with_vra8_only:
        ops.append(_project_create(prefix))
    return tuple(ops)


#: The infrastructure ops of VCF Automation 9 (tenant plane).
VCFA_INFRA_OPS: Final[tuple[VcfaTypedOp, ...]] = infra_ops("vcfa.tenant", with_vra8_only=False)

validate_typed_ops(VCFA_INFRA_OPS)


def _written_out(*, op_id: str) -> str:
    return op_id


#: The same nine op ids, written out in full. The CLI guard test finds every
#: typed op by reading literal op_id keyword arguments in this package.
VCFA_INFRA_OP_IDS: Final[tuple[str, ...]] = (
    _written_out(op_id="vcfa.tenant.cloud_account.list"),
    _written_out(op_id="vcfa.tenant.cloud_account.create"),
    _written_out(op_id="vcfa.tenant.cloud_account.delete"),
    _written_out(op_id="vcfa.tenant.zone.set"),
    _written_out(op_id="vcfa.tenant.flavor_profile.list"),
    _written_out(op_id="vcfa.tenant.flavor_profile.set"),
    _written_out(op_id="vcfa.tenant.network_profile.list"),
    _written_out(op_id="vcfa.tenant.storage_profile.list"),
    _written_out(op_id="vcfa.tenant.project.members.set"),
)

if tuple(op.op_id for op in VCFA_INFRA_OPS) != VCFA_INFRA_OP_IDS:
    raise AssertionError("VCFA_INFRA_OP_IDS does not match VCFA_INFRA_OPS")
