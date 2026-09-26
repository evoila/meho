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

from meho_backplane.connectors.vcf_automation._paths import (
    OAUTH_REGISTER_PATH,
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

_STATUS_ENVELOPE_NOTE = (
    "Returns {status: 'created' | 'unchanged' | 'invalid_request', …, guidance}: "
    "'unchanged' = it already exists (nothing written), 'invalid_request' = a name "
    "did not resolve (nothing written)."
)

# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

_NAME_FRAGMENT: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 128}
#: A name that goes into a FIQL filter literal: no ``,`` / ``;``.
_FIQL_NAME: dict[str, Any] = {**_NAME_FRAGMENT, "pattern": "^[^,;]+$"}

_PAGING: dict[str, Any] = {
    "page": {"type": "integer", "minimum": 1, "description": "1-based page number."},
    "pageSize": {"type": "integer", "minimum": 1, "maximum": 128, "description": "Max 128."},
}
_NAME_CONTAINS: dict[str, Any] = {
    "name_contains": {
        **_FIQL_NAME,
        "description": "Case-insensitive name substring filter (FIQL name==*value*).",
    }
}

_PASSWORD_SECRET_PROPS: dict[str, Any] = {
    "password_secret_ref": {
        "type": "string",
        "minLength": 1,
        "description": (
            "Vault KV-v2 path holding the user's password, read under your identity. "
            "The password is NEVER passed inline."
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
        "description": "Field of the secret holding the password (default 'password').",
    },
}

_ORG_USER_PROPS: dict[str, Any] = {
    "org": {**_FIQL_NAME, "description": "Org name ('System' for the provider org)."},
    "username": {**_FIQL_NAME, "description": "The org user the token belongs to."},
    "token_name": {
        "type": "string",
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        "description": "API token (OAuth client) name, unique per user.",
    },
    **_PASSWORD_SECRET_PROPS,
}

_PRINCIPALS: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "email": {"type": "string", "minLength": 1, "description": "User or group name."},
            "type": {"type": "string", "enum": ["user", "group"]},
        },
        "required": ["email"],
        "additionalProperties": False,
    },
}

_LIST_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "values": {"type": ["array", "null"]},
        "resultTotal": {"type": ["integer", "null"]},
    },
    "additionalProperties": True,
}


def _write_response(resource: str, statuses: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": statuses},
            resource: {"type": ["object", "null"]},
            "guidance": {"type": ["string", "null"]},
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    }


_CREATE_STATUSES = ["created", "unchanged", "invalid_request"]

# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

_RIGHT_LIST = VcfaTypedOp(
    op_id="vcfa.provider.right.list",
    handler_attr="provider_right_list",
    plane="provider",
    path=PROVIDER_RIGHTS_PATH,
    summary="List VCFA rights, optionally by name substring (provider plane).",
    description=(
        "Lists the rights the appliance knows via GET /cloudapi/1.0.0/rights, "
        "optionally narrowed by a name substring. Use to find exact right names "
        "(e.g. 'API Tokens: Manage') before vcfa.provider.role.create. Returns "
        "{values: [{id, name, description, category, rightType}], resultTotal}. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {**_NAME_CONTAINS, **_PAGING},
        "additionalProperties": False,
    },
    response_schema=_LIST_RESPONSE,
    group_key="vcfa-provider-reads",
    tags=("read-only", "vcfa", "provider", "rbac"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to find the exact name of a right before building a custom role, "
            "e.g. name_contains='Token'."
        ),
        "output_shape": "{values: [{id, name, category, ...}], resultTotal}.",
        "next_step": "Pass exact right names to vcfa.provider.role.create 'rights'.",
    },
)

_ROLE_LIST = VcfaTypedOp(
    op_id="vcfa.provider.role.list",
    handler_attr="provider_role_list",
    plane="provider",
    path=PROVIDER_GLOBAL_ROLES_PATH,
    summary="List VCFA global roles, or the roles available in one org (provider plane).",
    description=(
        "Without 'org': lists global roles via GET /cloudapi/1.0.0/globalRoles -- the "
        "roles tenants consume once published. With 'org': lists that org's roles via "
        "GET /cloudapi/1.0.0/roles under the org's tenant context -- the roles a user "
        "create can reference (a published global role appears here). Optional name "
        "substring filter. Returns {values: [{id, name, description, readOnly, "
        "bundleKey}], resultTotal}. An unknown org is a connector_error. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "org": {**_FIQL_NAME, "description": "Org name to scope to (omit for global roles)."},
            **_NAME_CONTAINS,
            **_PAGING,
        },
        "additionalProperties": False,
    },
    response_schema=_LIST_RESPONSE,
    group_key="vcfa-provider-reads",
    tags=("read-only", "vcfa", "provider", "rbac"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to pick a base role for vcfa.provider.role.create (no org), or to check "
            "which roles an org can assign before vcfa.provider.user.create (with org)."
        ),
        "output_shape": "{values: [{id, name, readOnly, bundleKey}], resultTotal}.",
        "next_step": "vcfa.provider.role.create (base_role) or vcfa.provider.user.create (role).",
    },
)

_LOGIN_TEST = VcfaTypedOp(
    op_id="vcfa.tenant.login.test",
    handler_attr="tenant_login_test",
    plane="tenant",
    path=TENANT_SESSION_PATH,
    summary="Test the tenant-plane login only; no data call (tenant plane).",
    description=(
        "Runs only the tenant-plane login (POST /iaas/api/login -- the refresh-token "
        "exchange when the target secret carries 'refresh_token') and reports whether it "
        "authenticated. No authenticated data call is made; the minted bearer is "
        "discarded, never cached or returned. api_version comes from the unauthenticated "
        "GET /iaas/api/about. A refused login is a result (authenticated=false + error), "
        "not an op failure. Returns {authenticated, login_flow, api_version, error: "
        "{cause, status_code, message} | null}. safety_level=safe."
    ),
    parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
    response_schema={
        "type": "object",
        "properties": {
            "authenticated": {"type": "boolean"},
            "login_flow": {"type": "string"},
            "api_version": {"type": ["string", "null"]},
            "error": {"type": ["object", "null"]},
        },
        "required": ["authenticated", "error"],
        "additionalProperties": False,
    },
    group_key="vcfa-tenant-reads",
    tags=("read-only", "vcfa", "tenant", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call after storing a new tenant API token in the target secret, to prove "
            "the tenant plane accepts it before running tenant reads/writes."
        ),
        "output_shape": "{authenticated, login_flow, api_version, error}.",
        "next_step": (
            "authenticated=true → vcfa.tenant.project.list; false → read error.cause "
            "(session_establish_400 = the token was refused)."
        ),
    },
)

# ---------------------------------------------------------------------------
# Provider writes
# ---------------------------------------------------------------------------

_ORG_CREATE = VcfaTypedOp(
    op_id="vcfa.provider.org.create",
    handler_attr="provider_org_create",
    plane="provider",
    path=PROVIDER_ORGS_PATH,
    summary="Create a VCFA tenant organization (provider plane, idempotent on name).",
    description=(
        "Creates an org via POST /cloudapi/1.0.0/orgs. org_type='vm_apps' (default) sets "
        "isClassicTenant=true -- a VM Apps org, which needs no region, VPC or quota; "
        "'all_apps' needs a region + quota afterwards. The appliance may need the "
        "CLASSIC_TENANT_CREATION / MIXED_TENANCY_MODE feature flags for a VM Apps org. "
        f"{_STATUS_ENVELOPE_NOTE} org = {{id, name, displayName, isEnabled, type}}. "
        "safety_level=caution, requires approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9-]{0,62}$",
                "description": "Org name (URL slug).",
            },
            "display_name": {**_NAME_FRAGMENT, "description": "Display name (default name)."},
            "description": {"type": "string", "maxLength": 256},
            "org_type": {"type": "string", "enum": ["vm_apps", "all_apps"]},
            "is_enabled": {"type": "boolean", "description": "Default true."},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    response_schema=_write_response("org", _CREATE_STATUSES),
    group_key="vcfa-provider-writes",
    tags=("write", "vcfa", "provider", "org"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": "Call to create the tenant org a VCFA tenant plane needs.",
        "output_shape": "{status, org: {id, name, displayName, isEnabled, type}, guidance}.",
        "next_step": (
            "vcfa.provider.role.create with publish_to_org=<name>, then vcfa.provider.user.create."
        ),
    },
)

_ROLE_CREATE = VcfaTypedOp(
    op_id="vcfa.provider.role.create",
    handler_attr="provider_role_create",
    plane="provider",
    path=PROVIDER_GLOBAL_ROLES_PATH,
    summary="Create a custom VCFA global role from a base role + extra rights (provider plane).",
    description=(
        "Creates a custom global role: resolves every right of 'base_role' plus each "
        "exact right name in 'rights', then POST /cloudapi/1.0.0/globalRoles, PUT its "
        "rights, and optionally publishes it to one org (publish_to_org) or all orgs "
        "(publish_all). Use it because the stock 'Organization Administrator' role is "
        "read-only and lacks 'API Tokens: Manage'. Every name is resolved before any "
        f"write. {_STATUS_ENVELOPE_NOTE} Also returns rights_count and published_to. "
        "An existing role is left untouched (rights and publication unchanged). "
        "safety_level=caution, requires approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {**_FIQL_NAME, "description": "New global role name."},
            "description": {"type": "string", "maxLength": 256},
            "base_role": {**_FIQL_NAME, "description": "Global role whose rights are copied."},
            "rights": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 64,
                "uniqueItems": True,
                "description": "Exact right names to add (see vcfa.provider.right.list).",
            },
            "publish_to_org": {**_FIQL_NAME, "description": "Publish to this org."},
            "publish_all": {"type": "boolean", "description": "Publish to every org."},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    response_schema=_write_response("role", _CREATE_STATUSES),
    group_key="vcfa-provider-writes",
    tags=("write", "vcfa", "provider", "rbac"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to build an org-admin role that can mint API tokens: base_role="
            "'Organization Administrator', rights=['API Tokens: Manage'], "
            "publish_to_org=<org>."
        ),
        "output_shape": "{status, role: {id, name}, rights_count, published_to, guidance}.",
        "next_step": "vcfa.provider.user.create with role=<this role name>.",
        "parameter_hints": {
            "rights": "Exact names; resolve them with vcfa.provider.right.list first.",
        },
    },
)

_USER_CREATE = VcfaTypedOp(
    op_id="vcfa.provider.user.create",
    handler_attr="provider_user_create",
    plane="provider",
    path=PROVIDER_USERS_PATH,
    summary="Create a local VCFA org user with a role (provider plane, idempotent).",
    description=(
        "Creates a LOCAL user in an org via POST /cloudapi/1.0.0/users under the org's "
        "tenant context, holding one org-scoped role (a published global role). The "
        "password is read from Vault at password_secret_ref under your identity -- it is "
        "never an op param and never returned. "
        f"{_STATUS_ENVELOPE_NOTE} user = {{id, username, org, role}}. "
        "safety_level=caution, requires approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "org": {**_FIQL_NAME, "description": "Org the user is created in."},
            "username": {**_FIQL_NAME, "description": "Login name."},
            "role": {**_FIQL_NAME, "description": "Org role name (vcfa.provider.role.list org)."},
            **_PASSWORD_SECRET_PROPS,
            "email": {"type": "string", "maxLength": 256},
            "full_name": {"type": "string", "maxLength": 256},
            "description": {"type": "string", "maxLength": 256},
            "enabled": {"type": "boolean", "description": "Default true."},
        },
        "required": ["org", "username", "role", "password_secret_ref"],
        "additionalProperties": False,
    },
    response_schema=_write_response("user", _CREATE_STATUSES),
    group_key="vcfa-provider-writes",
    tags=("write", "vcfa", "provider", "user", "credential"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call after the org exists and the role is published to it. Store the "
            "password in Vault first (vault.kv.put) and pass its path."
        ),
        "output_shape": "{status, user: {id, username, org, role}, guidance}.",
        "next_step": "vcfa.provider.api_token.create for this user.",
        "parameter_hints": {"password_secret_ref": "Vault path; never the password itself."},
    },
)

_API_TOKEN_CREATE = VcfaTypedOp(
    op_id="vcfa.provider.api_token.create",
    handler_attr="provider_api_token_create",
    plane="provider",
    path=OAUTH_REGISTER_PATH,
    summary="Mint an org user's VCFA API token straight into Vault; never returned.",
    description=(
        "Logs in as <username>@<org> (password from password_secret_ref), registers an "
        "OAuth client (POST /oauth/tenant/<org>/register, or /oauth/provider/... for "
        "System) and runs the jwt-bearer grant to mint the API (refresh) token. The token "
        "is written to Vault at store_secret_ref/store_field (default field "
        "'refresh_token', the field the connector's tenant login reads) via the governed "
        "KV write handlers and is NEVER returned: the result carries only the Vault ref, "
        "version, SHA-256 and length. The user's role needs 'API Tokens: Manage'. "
        "Returns {status: 'created' | 'unchanged' | 'invalid_request', client_id, stored, "
        "guidance} (the token's id is urn:vcloud:token:<client_id>); 'unchanged' = a token "
        "with that name exists (nothing minted). "
        "safety_level=dangerous (credential material), requires approval."
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
            "store_mount": {"type": "string", "minLength": 1, "description": "Default 'secret'."},
            "store_field": {
                "type": "string",
                "minLength": 1,
                "description": "Field name (default 'refresh_token').",
            },
        },
        "required": ["org", "username", "token_name", "password_secret_ref", "store_secret_ref"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": _CREATE_STATUSES},
            "org": {"type": "string"},
            "username": {"type": "string"},
            "token_name": {"type": "string"},
            "client_id": {"type": ["string", "null"]},
            "stored": {"type": ["object", "null"]},
            "guidance": {"type": ["string", "null"]},
        },
        "required": ["status", "guidance"],
        "additionalProperties": False,
    },
    group_key="vcfa-provider-writes",
    tags=("write", "vcfa", "provider", "credential", "token"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to give the tenant plane a credential: mint the org user's API token "
            "into the target's Vault secret (store_field='refresh_token')."
        ),
        "output_shape": (
            "{status, client_id, stored: {mount, secret_ref, field, version, value_sha256, "
            "length}, guidance} -- the token value is never in the result."
        ),
        "next_step": "vcfa.tenant.login.test on a target whose secret holds the token.",
    },
)

_API_TOKEN_REVOKE = VcfaTypedOp(
    op_id="vcfa.provider.api_token.revoke",
    handler_attr="provider_api_token_revoke",
    plane="provider",
    path=PROVIDER_TOKEN_PATH,
    summary="Revoke an org user's VCFA API token by name (provider plane).",
    description=(
        "Logs in as <username>@<org> (password from password_secret_ref), finds the "
        "user's API token named token_name and revokes it via DELETE "
        "/cloudapi/1.0.0/tokens/{id} (the OAuth client it was minted under). Anything "
        "still using the token stops working. A missing token is 'unchanged'. The Vault "
        "copy is not touched. Returns {status: 'revoked' | 'unchanged' | "
        "'invalid_request', client_id, guidance}. safety_level=dangerous, requires approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": dict(_ORG_USER_PROPS),
        "required": ["org", "username", "token_name", "password_secret_ref"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["revoked", "unchanged", "invalid_request"]},
            "client_id": {"type": ["string", "null"]},
            "guidance": {"type": ["string", "null"]},
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group_key="vcfa-provider-writes",
    tags=("write", "vcfa", "provider", "credential", "token"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": "Call to retire an API token that is unused, leaked or superseded.",
        "output_shape": "{status, client_id, guidance}.",
        "next_step": "Remove the dead Vault copy with vault.kv.patch or vault.kv.delete.",
    },
)

# ---------------------------------------------------------------------------
# Tenant write
# ---------------------------------------------------------------------------

_PROJECT_CREATE = VcfaTypedOp(
    op_id="vcfa.tenant.project.create",
    handler_attr="tenant_project_create",
    plane="tenant",
    path=TENANT_PROJECTS_PATH,
    summary="Create a project in the tenant organization (tenant plane, idempotent on name).",
    description=(
        "Creates a project via POST /iaas/api/projects?apiVersion=2021-07-15 on the "
        "tenant plane (the target's tenant login), with optional administrators, "
        "members and viewers ({email, type: user|group}). "
        f"{_STATUS_ENVELOPE_NOTE} project = {{id, name, organizationId}}. A 403 means "
        "the tenant user lacks project-create rights. safety_level=caution, requires "
        "approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {**_NAME_FRAGMENT, "description": "Project name."},
            "description": {"type": "string", "maxLength": 1024},
            "administrators": _PRINCIPALS,
            "members": _PRINCIPALS,
            "viewers": _PRINCIPALS,
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    response_schema=_write_response("project", _CREATE_STATUSES),
    group_key="vcfa-tenant-writes",
    tags=("write", "vcfa", "tenant", "project"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": "Call to seed a project in the tenant org (the deployment scope).",
        "output_shape": "{status, project: {id, name, organizationId}, guidance}.",
        "next_step": "vcfa.tenant.project.list to confirm.",
    },
)

#: The provisioning ops, reads first then writes in bootstrap order.
VCFA_PROVISIONING_OPS: Final[tuple[VcfaTypedOp, ...]] = (
    _RIGHT_LIST,
    _ROLE_LIST,
    _ORG_CREATE,
    _ROLE_CREATE,
    _USER_CREATE,
    _API_TOKEN_CREATE,
    _API_TOKEN_REVOKE,
    _PROJECT_CREATE,
    _LOGIN_TEST,
)


validate_typed_ops(VCFA_PROVISIONING_OPS)
