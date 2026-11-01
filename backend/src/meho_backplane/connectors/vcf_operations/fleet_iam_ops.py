# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fleet role operations of the VCF Operations connector (VCF Operations 9.1+).

Three typed operations for who may do what in a VCF 9.1 fleet:

* ``vrops.fleet.role.list`` — the VCF roles (built-in and custom) and the
  SSO realms.
* ``vrops.fleet.principal.find`` — find users or groups of an SSO realm with
  their VCF roles, or read the role assignments of one user or group.
* ``vrops.fleet.role.assign`` — give or remove VCF roles of one user or
  group. A change: it waits until a person approves it.

The calls exist from VCF Operations 9.1 on; the handlers stop on 9.0 with a
clear message (see
:mod:`~meho_backplane.connectors.vcf_operations.fleet_iam_handlers`). Only
the 9.x registrar registers these operations.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vcf_operations.typed_ops import VropsTypedOp

__all__ = [
    "VROPS_FLEET_IAM_OPS",
    "VROPS_FLEET_IAM_WHEN_TO_USE_BY_GROUP",
    "VROPS_FLEET_PRINCIPAL_FIND_OP",
    "VROPS_FLEET_ROLE_ASSIGN_OP",
    "VROPS_FLEET_ROLE_LIST_OP",
]

_GROUP_KEY = "vrops-fleet-roles"
_NEEDS_9_1 = "Needs VCF Operations 9.1 or newer."
_READ_TAGS: tuple[str, ...] = ("read-only", "vrops", "vmware", "fleet", "iam", "roles")

VROPS_FLEET_IAM_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_KEY: (
        "Use for VCF roles of the fleet in VCF Operations 9.1 and newer. Reads: the VCF "
        "roles and SSO realms, and the users and groups of a realm with their roles. "
        "Change: give or remove VCF roles of one user or group; it waits for a person's "
        "approval."
    ),
}

_STRING_LIST: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

_ASSIGNMENT_ROW: dict[str, Any] = {
    "type": "object",
    "properties": {
        "roleName": {"type": ["string", "null"], "description": "Role name."},
        "roleDisplayName": {"type": ["string", "null"], "description": "Role display name."},
        "scopeType": {
            "type": ["string", "null"],
            "description": "SSO_REALM (the whole realm) or VCF_INSTANCE.",
        },
        "resourceIds": {**_STRING_LIST, "description": "VCF instance ids of the scope."},
        "expiresAt": {
            "type": ["integer", "null"],
            "description": "When the assignment ends (epoch milliseconds), or null.",
        },
    },
}

_ROLE_LIST_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "roles": {
            "type": "array",
            "description": "One row per VCF role.",
            "items": {
                "type": "object",
                "properties": {
                    "roleName": {"type": "string", "description": "Name used to assign it."},
                    "roleDisplayName": {"type": ["string", "null"]},
                    "type": {"type": ["string", "null"], "description": "BUILT_IN or CUSTOM."},
                    "roleDescription": {"type": ["string", "null"]},
                    "componentRoles": {
                        **_STRING_LIST,
                        "description": "Component and its roles, e.g. 'VCENTER: Admin'.",
                    },
                },
            },
        },
        "ssoRealms": {
            "type": "array",
            "description": "One row per SSO realm.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": ["string", "null"], "description": "SSO realm id."},
                    "name": {"type": ["string", "null"]},
                    "vcfInstanceId": {"type": ["string", "null"]},
                    "idpId": {"type": ["string", "null"]},
                    "totalConfiguredComponents": {"type": ["integer", "null"]},
                    "coverage": {**_STRING_LIST, "description": "Covered instances."},
                    "issues": {**_STRING_LIST, "description": "Open issues of the realm."},
                },
            },
        },
    },
}

_PRINCIPAL_FIND_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ssoRealmId": {"type": "string"},
        "principalType": {"type": "string", "description": "USER or GROUP (search only)."},
        "totalCount": {"type": ["integer", "null"], "description": "Matches in all pages."},
        "principals": {
            "type": "array",
            "description": "Search only: one row per user or group.",
            "items": {
                "type": "object",
                "properties": {
                    "principalType": {"type": "string"},
                    "principalId": {"type": ["string", "null"]},
                    "name": {"type": ["string", "null"]},
                    "displayName": {"type": ["string", "null"]},
                    "domain": {"type": ["string", "null"]},
                    "email": {"type": ["string", "null"]},
                    "active": {"type": ["boolean", "null"]},
                    "roles": {**_STRING_LIST, "description": "VCF roles it holds."},
                    "directRoles": {**_STRING_LIST, "description": "Users: roles not via group."},
                },
            },
        },
        "principalId": {"type": "string", "description": "One-principal read only."},
        "assignments": {
            "type": "array",
            "description": "One-principal read only: one row per role assignment.",
            "items": _ASSIGNMENT_ROW,
        },
    },
}

_ROLE_ASSIGN_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ssoRealmId": {"type": "string"},
        "principalId": {"type": "string"},
        "changed": {"type": "boolean", "description": "False when nothing had to change."},
        "vendorCall": {
            "type": ["string", "null"],
            "enum": ["PUT", "DELETE", None],
            "description": "PUT (new full set), DELETE (no role left) or null.",
        },
        "before": {"type": "array", "items": _ASSIGNMENT_ROW},
        "after": {"type": "array", "items": _ASSIGNMENT_ROW},
    },
}

_SSO_REALM_ID: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "SSO realm id, from vrops.fleet.role.list (ssoRealms[].id).",
}


VROPS_FLEET_ROLE_LIST_OP = VropsTypedOp(
    op_id="vrops.fleet.role.list",
    handler_attr="fleet_role_list",
    summary="List the VCF roles of the fleet (built-in and custom) and the SSO realms.",
    description=(
        "Reads GET /suite-api/api/fleet-management/iam/roles (all pages) and GET "
        f"/suite-api/api/fleet-management/iam/ssorealms. {_NEEDS_9_1} Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["BUILT_IN", "CUSTOM"],
                "description": "Only built-in or only custom roles. Leave out for both.",
            },
        },
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema=_ROLE_LIST_RESPONSE,
    group_key=_GROUP_KEY,
    tags=(*_READ_TAGS, "sso-realm"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first for any role question: it gives the role names to assign and the "
            "SSO realm ids the other fleet role operations need."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{roles: [{roleName, roleDisplayName, type, roleDescription, componentRoles}], "
            "ssoRealms: [{id, name, vcfInstanceId, idpId, totalConfiguredComponents, "
            "coverage, issues}]}. When the result comes back as a handle, read the realms "
            "with result_query path='/ssoRealms'. The built-in VCF Administrator role "
            "is vcf_administrator."
        ),
        "next_step": (
            "Pass an ssoRealms[].id to vrops.fleet.principal.find to find a user or group, "
            "then a roleName to vrops.fleet.role.assign."
        ),
    },
)


VROPS_FLEET_PRINCIPAL_FIND_OP = VropsTypedOp(
    op_id="vrops.fleet.principal.find",
    handler_attr="fleet_principal_find",
    summary="Find users or groups of an SSO realm and show their VCF roles.",
    description=(
        "Searches users or groups (POST .../ssorealms/{ssoRealmId}/users/query or "
        "groups/query, reads sent as POST), or reads one principal's role assignments "
        f"(GET .../principals/{{principalId}}/roles). {_NEEDS_9_1} Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "ssoRealmId": _SSO_REALM_ID,
            "principalType": {
                "type": "string",
                "enum": ["USER", "GROUP"],
                "description": "Search users or groups. Use this or principalId.",
            },
            "name": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Part of the name to search for: user name, first or last name for "
                    "users; display name for groups. Leave out to list all."
                ),
            },
            "roleFilter": {
                "type": "string",
                "enum": ["HAS_ROLE", "HAS_NO_ROLE"],
                "description": "Only principals with a VCF role, or only those without.",
            },
            "page": {"type": "integer", "minimum": 0, "description": "0-based page number."},
            "pageSize": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": "Rows per page. MEHO sends 100 when none is given.",
            },
            "principalId": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Read the role assignments (with scope and expiry) of this one user or "
                    "group instead of searching. Use this or principalType."
                ),
            },
        },
        "required": ["ssoRealmId"],
        "oneOf": [{"required": ["principalType"]}, {"required": ["principalId"]}],
        "additionalProperties": False,
        "examples": [{"ssoRealmId": "<sso-realm-id>", "principalType": "USER", "name": "<name>"}],
    },
    response_schema=_PRINCIPAL_FIND_RESPONSE,
    group_key=_GROUP_KEY,
    tags=(*_READ_TAGS, "user", "group"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to find the id of a user or group before changing its roles, or to "
            "check who holds a role."
        ),
        "parameter_hints": {
            "principalId": "Use principals[].principalId from a search.",
            "pageSize": "If totalCount is larger than the rows, read the next page.",
        },
        "output_shape": (
            "Search: {ssoRealmId, principalType, totalCount, principals: [{principalType, "
            "principalId, name, displayName, domain, email, active, roles, directRoles}]}. "
            "One principal: {ssoRealmId, principalId, assignments: [{roleName, "
            "roleDisplayName, scopeType, resourceIds, expiresAt}]}. An empty principals "
            "list means no match."
        ),
        "next_step": (
            "Pass principalId to vrops.fleet.role.assign to give or remove roles, or to "
            "this operation again to see the scope of each role."
        ),
    },
)


VROPS_FLEET_ROLE_ASSIGN_OP = VropsTypedOp(
    op_id="vrops.fleet.role.assign",
    handler_attr="fleet_role_assign",
    summary="Give or remove VCF roles of a user or group. Waits for a person's approval.",
    description=(
        "Reads the current roles, then sends the merged set with PUT "
        ".../ssorealms/{ssoRealmId}/principals/{principalId}/roles (DELETE when no role "
        f"is left). Other roles stay. {_NEEDS_9_1} A change."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "ssoRealmId": _SSO_REALM_ID,
            "principalId": {
                "type": "string",
                "minLength": 1,
                "description": "User or group id, from vrops.fleet.principal.find.",
            },
            "add": {
                "type": "array",
                "minItems": 1,
                "description": "Roles to give. A role it already holds gets the new scope.",
                "items": {
                    "type": "object",
                    "description": "One role to give, with its scope.",
                    "properties": {
                        "roleName": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Role name, from vrops.fleet.role.list.",
                        },
                        "scopeType": {
                            "type": "string",
                            "enum": ["SSO_REALM", "VCF_INSTANCE"],
                            "description": (
                                "SSO_REALM (default): the whole realm. VCF_INSTANCE: only "
                                "the instances in resourceIds."
                            ),
                        },
                        "resourceIds": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "description": "One VCF instance id.",
                            },
                            "description": "VCF instance ids. Only with scopeType VCF_INSTANCE.",
                        },
                        "expiresAt": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "When the role ends (epoch milliseconds). Optional.",
                        },
                    },
                    "required": ["roleName"],
                    "additionalProperties": False,
                },
            },
            "remove": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "description": "One role name to remove.",
                },
                "description": "Role names to remove. Other roles stay.",
            },
        },
        "required": ["ssoRealmId", "principalId"],
        "anyOf": [{"required": ["add"]}, {"required": ["remove"]}],
        "additionalProperties": False,
        "examples": [
            {
                "ssoRealmId": "<sso-realm-id>",
                "principalId": "<principal-id>",
                "add": [{"roleName": "<role-name>", "scopeType": "SSO_REALM"}],
            }
        ],
    },
    response_schema=_ROLE_ASSIGN_RESPONSE,
    group_key=_GROUP_KEY,
    tags=("vrops", "vmware", "fleet", "iam", "roles", "assign", "change"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when the operator asks to give a user or group a VCF role, or to take one away."
        ),
        "preconditions": (
            "A person must approve the call before it runs; the call waits until then. "
            "Get the realm id and role names from vrops.fleet.role.list and the principal "
            "id from vrops.fleet.principal.find. MEHO refuses a change that leaves the "
            "realm with no user or group holding the VCF Administrator role."
        ),
        "parameter_hints": {
            "add": "Give each role once. scopeType VCF_INSTANCE needs resourceIds.",
            "remove": "Names of roles to take away; a role it does not hold is skipped.",
        },
        "output_shape": (
            "{ssoRealmId, principalId, changed, vendorCall, before: [...], after: [...]}; "
            "each list holds {roleName, roleDisplayName, scopeType, resourceIds, expiresAt}."
        ),
        "next_step": (
            "Check with vrops.fleet.principal.find and the same principalId that the "
            "roles are as expected."
        ),
    },
)


#: The fleet role operations. Registered by the 9.x registrar only.
VROPS_FLEET_IAM_OPS: tuple[VropsTypedOp, ...] = (
    VROPS_FLEET_ROLE_LIST_OP,
    VROPS_FLEET_PRINCIPAL_FIND_OP,
    VROPS_FLEET_ROLE_ASSIGN_OP,
)
