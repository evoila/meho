# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA provisioning ops: tenant bootstrap writes + their reads (evoila/meho#3890).

A VCF Automation 9.1 appliance with only the ``System`` org cannot serve
the tenant (IaaS) plane: ``POST /iaas/api/login`` exchanges only a
**tenant-org** user's API token. These ops make the bootstrap sequence
governed -- org → custom global role (with ``API Tokens: Manage``) →
local org user → API token (into Vault) → project -- plus the two reads
it needs (rights, roles) and a tenant-login acid test.

====================================  =========  ========  ====================================
op_id                                 tier       approval  wire
====================================  =========  ========  ====================================
``vcfa.provider.right.list``          safe       no        GET /cloudapi/1.0.0/rights
``vcfa.provider.role.list``           safe       no        GET …/globalRoles | …/roles (+org)
``vcfa.provider.org.create``          caution    yes       POST /cloudapi/1.0.0/orgs
``vcfa.provider.role.create``         caution    yes       POST …/globalRoles + rights + publish
``vcfa.provider.role.delete``         dangerous  yes       DELETE /cloudapi/1.0.0/globalRoles/{id}
``vcfa.provider.user.create``         caution    yes       POST /cloudapi/1.0.0/users (+org)
``vcfa.provider.api_token.create``    dangerous  yes       user session, /oauth/… mint → Vault
``vcfa.provider.api_token.revoke``    dangerous  yes       user session, DELETE …/tokens/{id}
``vcfa.tenant.project.create``        caution    yes       POST /iaas/api/projects
``vcfa.tenant.login.test``            safe       no        POST /iaas/api/login only
====================================  =========  ========  ====================================

Same :class:`~.typed_ops.VcfaTypedOp` shape and registrar as the seven
reads in :mod:`.typed_ops`; kept in its own tuple
(:data:`VCFA_PROVISIONING_OPS`) so that module stays the read surface.
Every op's declared ``plane`` is cross-checked against
``plane_for_path(op.path)`` at import, exactly like the reads.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_automation._op_schemas import (
    RIGHT_FIELDS,
    ROLE_FIELDS,
    cloudapi_page,
    field,
    guidance,
    obj,
)
from meho_backplane.connectors.vcf_automation._paths import (
    OAUTH_REGISTER_PATH,
    PROVIDER_GLOBAL_ROLE_PATH,
    PROVIDER_GLOBAL_ROLES_PATH,
    PROVIDER_RIGHTS_PATH,
    PROVIDER_TOKEN_PATH,
    PROVIDER_USERS_PATH,
)
from meho_backplane.connectors.vcf_automation._routing import TENANT_SESSION_PATH
from meho_backplane.connectors.vcf_automation.typed_ops import (
    PROVIDER_ORGS_PATH,
    TENANT_PROJECTS_PATH,
    VcfaTypedOp,
    validate_typed_ops,
)

__all__ = ["VCFA_PROVISIONING_OPS"]

_STATUS_NOTE = (
    "'unchanged' means it already exists and nothing was written; 'invalid_request' "
    "means a name did not resolve and nothing was written."
)

# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

_NAME_FRAGMENT: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 128}
#: A name that goes into a FIQL filter literal: no ``,`` / ``;``.
_FIQL_NAME: dict[str, Any] = {**_NAME_FRAGMENT, "pattern": "^[^,;]+$"}

_PAGING: dict[str, Any] = {
    "page": {"type": "integer", "minimum": 1, "description": "The page number, starting at 1."},
    "pageSize": {
        "type": "integer",
        "minimum": 1,
        "maximum": 128,
        "description": "Items per page (at most 128).",
    },
}
_NAME_CONTAINS: dict[str, Any] = {
    "name_contains": {
        **_NAME_FRAGMENT,
        # Rides a FIQL wildcard literal: no FIQL-reserved characters.
        "pattern": "^[^,;()*=!<>'\"]+$",
        "description": "Optional part of the name to match (FIQL name==*value*).",
    }
}

_PASSWORD_SECRET_PROPS: dict[str, Any] = {
    "password_secret_ref": {
        "type": "string",
        "minLength": 1,
        "description": (
            "Vault KV-v2 path that holds the user's password, read under your identity. "
            "Never pass the password itself."
        ),
    },
    "password_secret_mount": {
        "type": "string",
        "minLength": 1,
        "description": "KV-v2 mount of password_secret_ref (default 'secret').",
    },
    "password_secret_key": {
        "type": "string",
        "minLength": 1,
        "description": "Field of the secret that holds the password (default 'password').",
    },
}

_ORG_USER_PROPS: dict[str, Any] = {
    "org": {**_FIQL_NAME, "description": "Org name ('System' for the provider org)."},
    "username": {**_FIQL_NAME, "description": "The org user the token belongs to."},
    "token_name": {
        "type": "string",
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        "description": "API token name (the OAuth client name), unique per user.",
    },
    **_PASSWORD_SECRET_PROPS,
}

_PRINCIPALS_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email": {"type": "string", "minLength": 1, "description": "User email or group name."},
        "type": {
            "type": "string",
            "enum": ["user", "group"],
            "description": "'user' (the default) or 'group'.",
        },
    },
    "required": ["email"],
    "additionalProperties": False,
}


def _limit(description: str) -> dict[str, Any]:
    return {"type": "integer", "minimum": 0, "description": f"{description} 0 means no limit."}


_ZONE_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "zone_id": {
            "type": "string",
            "minLength": 1,
            "description": "The cloud zone id (from vcfa.tenant.zone.list).",
        },
        "priority": {
            "type": "integer",
            "minimum": 0,
            "description": "The zone priority; a lower number wins. Default 0.",
        },
        "max_instances": _limit("The most VMs in this zone."),
        "cpu_limit": _limit("The most vCPUs in this zone."),
        "memory_limit_mb": _limit("The most memory in this zone, in MB."),
        "storage_limit_gb": _limit("The most storage in this zone, in GB."),
    },
    "required": ["zone_id"],
    "additionalProperties": False,
}

_CONSTRAINT_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "expression": {
            "type": "string",
            "minLength": 1,
            "description": "A tag rule 'key:value', or '!key:value' to keep away from a tag.",
        },
        "mandatory": {
            "type": "boolean",
            "description": "True (default): a VM that matches no network fails to deploy.",
        },
    },
    "required": ["expression"],
    "additionalProperties": False,
}


def _principals(role: str) -> dict[str, Any]:
    return {
        "type": "array",
        "items": _PRINCIPALS_ITEM,
        "description": f"Optional project {role}: users or groups.",
    }


_CREATE_STATUSES = ["created", "unchanged", "invalid_request"]


def _status_field(statuses: list[str]) -> dict[str, Any]:
    return field("string", "What the op did: " + ", ".join(statuses) + ".", enum=statuses)


_GUIDANCE_FIELD = field(["string", "null"], "A short note on what to do next, or null.")


def _write_response(resource: str, resource_fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": _status_field(_CREATE_STATUSES),
            resource: {
                **obj(f"The {resource}; null when nothing was found or made.", resource_fields),
                "type": ["object", "null"],
            },
            "guidance": _GUIDANCE_FIELD,
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

_RIGHT_LIST = VcfaTypedOp(
    op_id="vcfa.provider.right.list",
    handler_attr="provider_right_list",
    plane="provider",
    path=PROVIDER_RIGHTS_PATH,
    summary="List VCF Automation rights, optionally by part of the name (provider plane).",
    description=(
        "Lists the rights the appliance knows with GET /cloudapi/1.0.0/rights. Use "
        "it to find the exact right names, for example 'API Tokens: Manage', before "
        "vcfa.provider.role.create."
    ),
    parameter_schema={
        "type": "object",
        "properties": {**_NAME_CONTAINS, **_PAGING},
        "additionalProperties": False,
        "examples": [{"name_contains": "API Tokens"}],
    },
    response_schema=cloudapi_page("rights", RIGHT_FIELDS),
    group_key="vcfa-provider-reads",
    tags=("read-only", "vcfa", "provider", "rbac"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call to find the exact name of a right before you build a custom role, "
            "for example name_contains='Token'."
        ),
        output_shape=(
            "{values: [{id, name, description, category, rightType, impliedRights}], "
            "resultTotal, pageCount, page, pageSize}."
        ),
        next_step="Pass the exact right names to vcfa.provider.role.create 'rights'.",
    ),
)

_ROLE_LIST = VcfaTypedOp(
    op_id="vcfa.provider.role.list",
    handler_attr="provider_role_list",
    plane="provider",
    path=PROVIDER_GLOBAL_ROLES_PATH,
    summary="List VCF Automation global roles, or the roles of one org (provider plane).",
    description=(
        "Without 'org': lists the global roles (GET /cloudapi/1.0.0/globalRoles). "
        "With 'org': lists the roles that org can give a user (GET "
        "/cloudapi/1.0.0/roles); a published global role shows up there, and a "
        "VM Apps org shows its own fixed roles. An unknown org is an error."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "org": {**_FIQL_NAME, "description": "Org name to list roles for (omit for global)."},
            **_NAME_CONTAINS,
            **_PAGING,
        },
        "additionalProperties": False,
        "examples": [{"org": "<org-name>"}],
    },
    response_schema=cloudapi_page("roles", ROLE_FIELDS),
    group_key="vcfa-provider-reads",
    tags=("read-only", "vcfa", "provider", "rbac"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call without 'org' to pick a base role for vcfa.provider.role.create, or "
            "with 'org' to check which roles an org can give before "
            "vcfa.provider.user.create."
        ),
        output_shape=(
            "{values: [{id, name, description, readOnly, bundleKey, publishAll}], "
            "resultTotal, pageCount, page, pageSize}. publishAll is set on global "
            "roles only."
        ),
        next_step="vcfa.provider.role.create (base_role) or vcfa.provider.user.create (role).",
    ),
)

_LOGIN_TEST = VcfaTypedOp(
    op_id="vcfa.tenant.login.test",
    handler_attr="tenant_login_test",
    plane="tenant",
    path=TENANT_SESSION_PATH,
    summary="Test only the tenant-plane login; no data call (tenant plane).",
    description=(
        "Runs only the tenant login (POST /iaas/api/login) with the target's secret "
        "and says whether it worked. It makes no data call, and the new token is "
        "thrown away. A refused login is a result (authenticated=false), not an "
        "op failure."
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
            "authenticated": field("boolean", "True when the tenant login worked."),
            "login_flow": field(
                "string",
                "Which login ran: 'refresh_token' (an API token) or 'username_password'.",
            ),
            "api_version": field(
                ["string", "null"], "The newest API version from GET /iaas/api/about, or null."
            ),
            "error": {
                **obj(
                    "Why the login failed; null when it worked.",
                    {
                        "cause": field(
                            "string",
                            "The failure kind, for example session_establish_400.",
                        ),
                        "status_code": field(
                            ["integer", "null"], "The HTTP status of the login, if any."
                        ),
                        "message": field("string", "The error message."),
                    },
                ),
                "type": ["object", "null"],
            },
        },
        "required": ["authenticated", "error"],
        "additionalProperties": False,
    },
    group_key="vcfa-tenant-reads",
    tags=("read-only", "vcfa", "tenant", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call after you store a new tenant API token in the target secret, to "
            "check that the tenant plane accepts it before tenant reads or changes."
        ),
        output_shape=(
            "{authenticated, login_flow, api_version, error: {cause, status_code, message} | null}."
        ),
        next_step=(
            "authenticated=true: go on with vcfa.tenant.project.list. false: read "
            "error.cause (session_establish_400 means the token was refused)."
        ),
    ),
)

# ---------------------------------------------------------------------------
# Provider writes
# ---------------------------------------------------------------------------

_ORG_CREATE = VcfaTypedOp(
    op_id="vcfa.provider.org.create",
    handler_attr="provider_org_create",
    plane="provider",
    path=PROVIDER_ORGS_PATH,
    summary="Create a VCF Automation tenant org (provider plane); safe to run twice.",
    description=(
        "Creates an org with POST /cloudapi/1.0.0/orgs. org_type 'vm_apps' (the "
        "default) makes a VM Apps org, which needs no region, VPC or quota; "
        "'all_apps' needs a region and a quota later. An org with the same name "
        "answers 'unchanged'."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9-]{0,62}$",
                "description": "Org name, used in URLs: letters, digits and '-'.",
            },
            "display_name": {**_NAME_FRAGMENT, "description": "Display name (default: name)."},
            "description": {
                "type": "string",
                "maxLength": 256,
                "description": "Optional org description.",
            },
            "org_type": {
                "type": "string",
                "enum": ["vm_apps", "all_apps"],
                "description": (
                    "'vm_apps' (default): a VM Apps org. 'all_apps': an All Apps org, "
                    "which needs a region and a quota."
                ),
            },
            "is_enabled": {"type": "boolean", "description": "Enable the org (default true)."},
        },
        "required": ["name"],
        "additionalProperties": False,
        "examples": [{"name": "example-org", "display_name": "Example Org"}],
    },
    response_schema=_write_response(
        "org",
        {
            "id": field(["string", "null"], "The org id (a URN); null while it is being made."),
            "name": field("string", "The org name."),
            "displayName": field(["string", "null"], "The display name."),
            "isEnabled": field(["boolean", "null"], "True when the org is enabled."),
            "type": field(["string", "null"], "'vm_apps' or 'all_apps'."),
        },
    ),
    group_key="vcfa-provider-writes",
    tags=("write", "vcfa", "provider", "org"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to create the tenant org that a VCF Automation tenant plane needs.",
        preconditions=(
            "The name is not 'System'. A VM Apps org may need the appliance feature "
            "flags CLASSIC_TENANT_CREATION and MIXED_TENANCY_MODE. A person must "
            "approve the call."
        ),
        output_shape=(
            "{status: created | unchanged | invalid_request, org: {id, name, "
            f"displayName, isEnabled, type}}, guidance}}. {_STATUS_NOTE} When the "
            "appliance makes the org in the background, org.id is null at first."
        ),
        next_step=(
            "All Apps org: vcfa.provider.role.create with publish_to_org=<name>. VM Apps "
            "org: pick one of its own roles with vcfa.provider.role.list org=<name>. "
            "Then vcfa.provider.user.create."
        ),
    ),
)

_ROLE_CREATE = VcfaTypedOp(
    op_id="vcfa.provider.role.create",
    handler_attr="provider_role_create",
    plane="provider",
    path=PROVIDER_GLOBAL_ROLES_PATH,
    summary="Create a custom VCF Automation global role from a base role and rights.",
    description=(
        "Creates a global role with every right of 'base_role' plus each right in "
        "'rights', then may publish it to one org or to all orgs. Every name is "
        "checked before any write. Run it again to repair a half-built role."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {**_FIQL_NAME, "description": "The new global role name."},
            "description": {
                "type": "string",
                "maxLength": 256,
                "description": "Optional role description.",
            },
            "base_role": {**_FIQL_NAME, "description": "Global role whose rights are copied."},
            "rights": {
                "type": "array",
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "description": "One exact right name.",
                },
                "maxItems": 64,
                "uniqueItems": True,
                "description": "Exact right names to add (see vcfa.provider.right.list).",
            },
            "publish_to_org": {**_FIQL_NAME, "description": "Publish the role to this org."},
            "publish_all": {"type": "boolean", "description": "Publish the role to every org."},
        },
        "required": ["name"],
        "additionalProperties": False,
        "examples": [
            {
                "name": "<role-name>",
                "base_role": "Organization Administrator",
                "rights": ["API Tokens: Manage"],
                "publish_to_org": "<org-name>",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": _status_field([*_CREATE_STATUSES, "updated"]),
            "role": {
                **obj(
                    "The role; null when nothing was found or made.",
                    {
                        "id": field("string", "The role id (a URN)."),
                        "name": field("string", "The role name."),
                    },
                ),
                "type": ["object", "null"],
            },
            "rights_count": field(["integer", "null"], "How many rights the role has."),
            "published_to": {
                "type": ["array", "string"],
                "description": "The org names the role was published to now, or 'all'.",
                "items": {"type": "string"},
            },
            "reconciled": {
                "type": "array",
                "description": "On 'updated': what was repaired, 'rights' and/or 'publication'.",
                "items": {"type": "string"},
            },
            "guidance": _GUIDANCE_FIELD,
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group_key="vcfa-provider-writes",
    tags=("write", "vcfa", "provider", "rbac"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to build an org admin role that can make API tokens, because the "
            "built-in 'Organization Administrator' role cannot be changed and lacks "
            "'API Tokens: Manage': base_role='Organization Administrator', "
            "rights=['API Tokens: Manage'], publish_to_org=<org>."
        ),
        preconditions=(
            "Pass base_role or rights (or both) for a new role. Look up exact right "
            "names with vcfa.provider.right.list; the publish_to_org org must exist "
            "and be an All Apps org: a VM Apps org cannot get a global role, so the "
            "call answers invalid_request and names that org's own roles. A person "
            "must approve the call."
        ),
        output_shape=(
            "{status: created | updated | unchanged | invalid_request, role: {id, "
            f"name}}, rights_count, published_to, reconciled, guidance}}. {_STATUS_NOTE} "
            "'updated' means an existing role got its missing rights or publication; "
            "rights already on a role are never replaced. If adding the rights fails "
            "right after the create, the new role is deleted."
        ),
        next_step="vcfa.provider.user.create with role=<this role name>.",
        parameter_hints={
            "rights": "Exact names; look them up with vcfa.provider.right.list first.",
        },
    ),
)

_ROLE_DELETE = VcfaTypedOp(
    op_id="vcfa.provider.role.delete",
    handler_attr="provider_role_delete",
    plane="provider",
    path=PROVIDER_GLOBAL_ROLE_PATH,
    summary="Delete a custom VCF Automation global role by name; safe to run twice.",
    description=(
        "Finds the global role by name and deletes it with DELETE "
        "/cloudapi/1.0.0/globalRoles/{id}. A role that does not exist answers "
        "'unchanged'. A built-in role is refused and nothing is sent. When several "
        "roles match the name only by case, the call is refused; use the exact name."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {**_FIQL_NAME, "description": "The global role name to delete."},
        },
        "required": ["name"],
        "additionalProperties": False,
        "examples": [{"name": "<role-name>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": _status_field(["deleted", "unchanged", "invalid_request"]),
            "role": {
                **obj(
                    "The role that was found; null when there is none.",
                    {
                        "id": field("string", "The role id (a URN)."),
                        "name": field("string", "The role name."),
                    },
                ),
                "type": ["object", "null"],
            },
            "guidance": _GUIDANCE_FIELD,
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group_key="vcfa-provider-writes",
    tags=("write", "vcfa", "provider", "rbac", "delete"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to remove a custom global role that nobody needs, for example one "
            "left behind by a vcfa.provider.role.create that could not publish it."
        ),
        preconditions=(
            "Check the name with vcfa.provider.role.list first. Users in any org that "
            "hold the role lose it; the appliance may refuse while users still hold "
            "it. A second person must approve the call."
        ),
        output_shape=(
            "{status: deleted | unchanged | invalid_request, role: {id, name}, "
            "guidance}. 'unchanged' means no global role has that name. "
            "'invalid_request' means the role is built in; nothing was sent."
        ),
        next_step="vcfa.provider.role.list to confirm the role is gone.",
    ),
)

_USER_CREATE = VcfaTypedOp(
    op_id="vcfa.provider.user.create",
    handler_attr="provider_user_create",
    plane="provider",
    path=PROVIDER_USERS_PATH,
    summary="Create a local VCF Automation org user with a role; safe to run twice.",
    description=(
        "Creates a local user in an org with POST /cloudapi/1.0.0/users, holding one "
        "org role (a published global role, or a VM Apps org's own role). The "
        "password is read from Vault at password_secret_ref; it is never a parameter "
        "and never returned."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "org": {**_FIQL_NAME, "description": "The org the user is created in."},
            "username": {**_FIQL_NAME, "description": "The login name."},
            "role": {
                **_FIQL_NAME,
                "description": "Org role name (see vcfa.provider.role.list with org).",
            },
            **_PASSWORD_SECRET_PROPS,
            "email": {
                "type": "string",
                "maxLength": 256,
                "description": "Optional email address of the user.",
            },
            "full_name": {
                "type": "string",
                "maxLength": 256,
                "description": "Optional full name of the user.",
            },
            "description": {
                "type": "string",
                "maxLength": 256,
                "description": "Optional user description.",
            },
            "enabled": {"type": "boolean", "description": "Enable the user (default true)."},
        },
        "required": ["org", "username", "role", "password_secret_ref"],
        "additionalProperties": False,
        "examples": [
            {
                "org": "<org-name>",
                "username": "<username>",
                "role": "<role-name>",
                "password_secret_ref": "<vault-path>",
            }
        ],
    },
    response_schema=_write_response(
        "user",
        {
            "id": field(["string", "null"], "The user id (a URN)."),
            "username": field("string", "The login name."),
            "org": field("string", "The org name."),
            "role": field("string", "The role name; set on 'created'."),
        },
    ),
    group_key="vcfa-provider-writes",
    tags=("write", "vcfa", "provider", "user", "credential"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to create the local org user that will own the tenant API token.",
        preconditions=(
            "The org exists and has the role (see vcfa.provider.role.list with org). "
            "Store the password in "
            "Vault first (vault.kv.put) and pass its path. A person must approve "
            "the call."
        ),
        output_shape=(
            "{status: created | unchanged | invalid_request, user: {id, username, "
            f"org, role}}, guidance}}. {_STATUS_NOTE}"
        ),
        next_step="vcfa.provider.api_token.create for this user.",
        parameter_hints={"password_secret_ref": "A Vault path; never the password itself."},
    ),
)

_API_TOKEN_CREATE = VcfaTypedOp(
    op_id="vcfa.provider.api_token.create",
    handler_attr="provider_api_token_create",
    plane="provider",
    path=OAUTH_REGISTER_PATH,
    summary="Create an org user's VCF Automation API token and store it in Vault; never returned.",
    description=(
        "Logs in as the org user, registers an OAuth client and gets the user's API "
        "token, then writes it to Vault at store_secret_ref through vault_target. "
        "The token is never returned: the result has only where it is stored, a "
        "SHA-256 and the length."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            **_ORG_USER_PROPS,
            "store_secret_ref": {
                "type": "string",
                "minLength": 1,
                "description": "Vault KV-v2 path the token is written to (merged if it exists).",
            },
            "vault_target": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": (
                    "Name of the Vault target (list_targets) whose Vault role writes "
                    "store_secret_ref: the target you would run vault.kv.patch on."
                ),
            },
            "store_mount": {
                "type": "string",
                "minLength": 1,
                "description": "KV-v2 mount of store_secret_ref (default 'secret').",
            },
            "store_field": {
                "type": "string",
                "minLength": 1,
                "description": "Field the token is written to (default 'refresh_token').",
            },
        },
        "required": [
            "org",
            "username",
            "token_name",
            "password_secret_ref",
            "store_secret_ref",
            "vault_target",
        ],
        "additionalProperties": False,
        "examples": [
            {
                "org": "<org-name>",
                "username": "<username>",
                "token_name": "meho-token",
                "password_secret_ref": "<vault-path>",
                "store_secret_ref": "<vault-path>",
                "vault_target": "<vault-target-name>",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": _status_field(_CREATE_STATUSES),
            "org": field("string", "The org name."),
            "username": field("string", "The org user."),
            "token_name": field("string", "The API token name."),
            "client_id": field(
                ["string", "null"],
                "The OAuth client id; the token id is urn:vcloud:token:<client_id>.",
            ),
            "stored": {
                **obj(
                    "Where the token was stored; null when nothing was made.",
                    {
                        "vault_target": field("string", "The Vault target that wrote it."),
                        "mount": field("string", "The KV-v2 mount."),
                        "secret_ref": field("string", "The KV-v2 path."),
                        "field": field("string", "The field that holds the token."),
                        "version": field(["integer", "null"], "The new secret version."),
                        "value_sha256": field("string", "SHA-256 of the token, to compare."),
                        "length": field("integer", "The token length."),
                    },
                ),
                "type": ["object", "null"],
            },
            "guidance": _GUIDANCE_FIELD,
        },
        "required": ["status", "guidance"],
        "additionalProperties": False,
    },
    group_key="vcfa-provider-writes",
    tags=("write", "vcfa", "provider", "credential", "token"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to give the tenant plane a credential: create the org user's API "
            "token in the target's Vault secret (store_field='refresh_token', the "
            "field the tenant login reads). If the call is refused with "
            "write_capability_denied, the vault_target's role cannot write the path: "
            "fix the grant or pick another vault_target, then call again."
        ),
        preconditions=(
            "The org user exists and its role has 'API Tokens: Manage'. The user's "
            "password is in Vault at password_secret_ref. Your MEHO policy for "
            "vault.kv.patch / vault.kv.put on vault_target must not deny. A person "
            "must approve the call; a run without that approval is refused."
        ),
        output_shape=(
            "{status: created | unchanged | invalid_request, org, username, "
            "token_name, client_id, stored: {vault_target, mount, secret_ref, field, "
            "version, value_sha256, length}, guidance}. The token value is never in "
            "the result. 'unchanged' means a token with that name exists and nothing "
            "was made."
        ),
        parameter_hints={
            "vault_target": (
                "Target NAME of a Vault target that has its own extras.vault_role with "
                "read, create and update on the path; the write runs under that role. A Vault "
                "target WITHOUT a vault_role uses your own identity's role, which "
                "usually cannot write the path."
            ),
        },
        next_step="vcfa.tenant.login.test on a target whose secret holds the token.",
    ),
)

_API_TOKEN_REVOKE = VcfaTypedOp(
    op_id="vcfa.provider.api_token.revoke",
    handler_attr="provider_api_token_revoke",
    plane="provider",
    path=PROVIDER_TOKEN_PATH,
    summary="Revoke an org user's VCF Automation API token by name (provider plane).",
    description=(
        "Logs in as the org user, finds the API token named token_name and revokes "
        "it with DELETE /cloudapi/1.0.0/tokens/{id}. Anything that still uses the "
        "token stops working. The Vault copy is not touched."
    ),
    parameter_schema={
        "type": "object",
        "properties": dict(_ORG_USER_PROPS),
        "required": ["org", "username", "token_name", "password_secret_ref"],
        "additionalProperties": False,
        "examples": [
            {
                "org": "<org-name>",
                "username": "<username>",
                "token_name": "meho-token",
                "password_secret_ref": "<vault-path>",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": _status_field(["revoked", "unchanged", "invalid_request"]),
            "org": field("string", "The org name."),
            "username": field("string", "The org user."),
            "token_name": field("string", "The API token name."),
            "client_id": field(["string", "null"], "The OAuth client id of the token."),
            "guidance": _GUIDANCE_FIELD,
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group_key="vcfa-provider-writes",
    tags=("write", "vcfa", "provider", "credential", "token"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to retire an API token that is not used, leaked or replaced.",
        preconditions=(
            "Nothing still needs the token: every client that uses it stops working. "
            "The user's password is in Vault at password_secret_ref. A person must "
            "approve the call."
        ),
        output_shape=(
            "{status: revoked | unchanged | invalid_request, org, username, "
            "token_name, client_id, guidance}. 'unchanged' means no token with that "
            "name exists."
        ),
        next_step="Remove the old Vault copy with vault.kv.patch or vault.kv.delete.",
    ),
)

# ---------------------------------------------------------------------------
# Tenant write
# ---------------------------------------------------------------------------

_PROJECT_CREATE = VcfaTypedOp(
    op_id="vcfa.tenant.project.create",
    handler_attr="tenant_project_create",
    plane="tenant",
    path=TENANT_PROJECTS_PATH,
    summary="Create a project in the tenant org (tenant plane); safe to run twice.",
    description=(
        "Creates a project with POST /iaas/api/projects on the tenant plane, with "
        "optional users, cloud zones with a quota, and network tag rules. A project "
        "with the same name answers 'unchanged' and is not changed."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {**_NAME_FRAGMENT, "description": "The project name."},
            "description": {
                "type": "string",
                "maxLength": 1024,
                "description": "Optional project description.",
            },
            "administrators": _principals("administrators"),
            "members": _principals("members"),
            "viewers": _principals("viewers"),
            "zones": {
                "type": "array",
                "items": _ZONE_ITEM,
                "description": "Optional cloud zones the project may deploy to, each with a quota.",
            },
            "network_constraints": {
                "type": "array",
                "items": _CONSTRAINT_ITEM,
                "description": (
                    "Optional network tag rules. With 'project:<project-name>', the "
                    "project's VMs only use networks that carry that tag."
                ),
            },
        },
        "required": ["name"],
        "additionalProperties": False,
        "examples": [
            {
                "name": "<project-name>",
                "administrators": [{"email": "<user-email>", "type": "user"}],
                "zones": [{"zone_id": "<zone-id>", "max_instances": 10, "cpu_limit": 20}],
                "network_constraints": [{"expression": "project:<project-name>"}],
            }
        ],
    },
    response_schema=_write_response(
        "project",
        {
            "id": field(["string", "null"], "The project id."),
            "name": field("string", "The project name."),
            "organizationId": field(["string", "null"], "The id of the tenant org."),
        },
    ),
    group_key="vcfa-tenant-writes",
    tags=("write", "vcfa", "tenant", "project"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to create a project (the scope for deployments) in the tenant org, "
            "optionally with a quota per cloud zone and a rule that keeps its VMs on "
            "its own network."
        ),
        preconditions=(
            "The target's tenant login works (check with vcfa.tenant.login.test) and "
            "its user may create projects (a 403 means it may not). Zone ids come from "
            "vcfa.tenant.zone.list; a network rule needs a network with the same tag "
            "(vcfa.tenant.fabric_network.update). A person must approve the call."
        ),
        output_shape=(
            "{status: created | unchanged | invalid_request, project: {id, name, "
            f"organizationId}}, guidance}}. {_STATUS_NOTE} An existing project keeps "
            "its zones and rules."
        ),
        next_step=(
            "vcfa.tenant.project.list to confirm, then vcfa.tenant.blueprint_request.create "
            "with the project id."
        ),
    ),
)

#: The provisioning ops, reads first then writes in bootstrap order.
VCFA_PROVISIONING_OPS: Final[tuple[VcfaTypedOp, ...]] = (
    _RIGHT_LIST,
    _ROLE_LIST,
    _ORG_CREATE,
    _ROLE_CREATE,
    _ROLE_DELETE,
    _USER_CREATE,
    _API_TOKEN_CREATE,
    _API_TOKEN_REVOKE,
    _PROJECT_CREATE,
    _LOGIN_TEST,
)


validate_typed_ops(VCFA_PROVISIONING_OPS)
