# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vcfa.provider.role.create`` + ``vcfa.provider.user.create`` (evoila/meho#3890).

A fresh VCFA org has no usable role: with ``ADVANCED_RIGHTS_BUNDLE_MODE``
(the default) tenant roles are **global roles** that must be published to
the org, and the stock *Organization Administrator* role is ``readOnly`` and
lacks ``API Tokens: Manage``. So the tenant-bootstrap sequence is: a custom
global role (a base role's rights + extra rights), published to the org,
then a local user holding that role.

Both handlers return the provisioning envelope documented in
:mod:`._provisioning` (``created`` / ``unchanged`` / ``invalid_request``);
every name resolution runs before the first write.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vcf_automation._lookups import (
    entity_ref,
    find_by_field,
    find_global_role,
    find_org,
    find_org_user,
    list_all,
    password_missing_guidance,
    quote_segment,
    read_password,
    tenant_context_headers,
)
from meho_backplane.connectors.vcf_automation._paths import (
    PROVIDER_GLOBAL_ROLE_PUBLISH_ALL_PATH,
    PROVIDER_GLOBAL_ROLE_PUBLISH_PATH,
    PROVIDER_GLOBAL_ROLE_RIGHTS_PATH,
    PROVIDER_GLOBAL_ROLES_PATH,
    PROVIDER_RIGHTS_PATH,
    PROVIDER_ROLES_PATH,
    PROVIDER_USERS_PATH,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = ["provider_role_create", "provider_user_create"]

#: ``bundleKey`` a custom (non-system) global role carries.
_CUSTOM_ROLE_BUNDLE_KEY: Final = "com.vmware.vcloud.undefined.key"


# ---------------------------------------------------------------------------
# Global role create
# ---------------------------------------------------------------------------


async def _resolve_role_rights(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    params: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    """Resolve the base role's rights + each extra right name → ``(refs, problem)``."""
    refs: dict[str, dict[str, Any]] = {}
    base_name = params.get("base_role")
    if base_name:
        base = await find_global_role(connector, target, operator, base_name)
        if base is None:
            return [], (
                f"base_role {base_name!r} is not a global role on this appliance; "
                "list them with vcfa.provider.role.list"
            )
        path = PROVIDER_GLOBAL_ROLE_RIGHTS_PATH.format(id=quote_segment(str(base.get("id"))))
        for row in await list_all(connector, target, operator, path):
            if row.get("id"):
                refs[str(row["id"])] = entity_ref(row)
    missing: list[str] = []
    for right_name in params.get("rights") or []:
        right = await find_by_field(connector, target, operator, PROVIDER_RIGHTS_PATH, right_name)
        if right is None or not right.get("id"):
            missing.append(right_name)
        else:
            refs[str(right["id"])] = entity_ref(right)
    if missing:
        return [], (
            f"no right named {', '.join(repr(m) for m in missing)} on this appliance; "
            "find exact names with vcfa.provider.right.list"
        )
    if not refs:
        return [], "the role would carry no rights: pass base_role and/or rights"
    return list(refs.values()), None


async def _create_global_role(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    params: Mapping[str, Any],
) -> str:
    """``POST /cloudapi/1.0.0/globalRoles`` and return the new role id."""
    name = params["name"]
    created = await connector._post_json(
        target,
        PROVIDER_GLOBAL_ROLES_PATH,
        operator=operator,
        json={
            "name": name,
            "description": params.get("description") or "",
            "bundleKey": _CUSTOM_ROLE_BUNDLE_KEY,
            "readOnly": False,
            "publishAll": False,
        },
    )
    role_id = created.get("id")
    if not role_id:
        found = await find_global_role(connector, target, operator, name)
        role_id = found.get("id") if found else None
    if not role_id:
        raise RuntimeError(
            f"vcfa.provider.role.create: POST {PROVIDER_GLOBAL_ROLES_PATH} succeeded but "
            f"global role {name!r} has no id and could not be re-read"
        )
    return str(role_id)


async def _publish_role(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    role_segment: str,
    *,
    publish_all: bool,
    org: Mapping[str, Any] | None,
) -> list[str] | str:
    """Publish the role to every org (``publishAll``) or to *org*; return what was done."""
    if publish_all:
        await connector._post_json(
            target,
            PROVIDER_GLOBAL_ROLE_PUBLISH_ALL_PATH.format(id=role_segment),
            operator=operator,
            json={},
        )
        return "all"
    if org is not None:
        await connector._post_json(
            target,
            PROVIDER_GLOBAL_ROLE_PUBLISH_PATH.format(id=role_segment),
            operator=operator,
            json={"values": [entity_ref(org)]},
        )
        return [str(org.get("name"))]
    return []


async def provider_role_create(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.role.create`` — custom global role (idempotent on name).

    Flow: existing-name check (``unchanged``) → resolve the base role's
    rights, each extra right name, and the ``publish_to_org`` org -- all
    before any write (``invalid_request`` on a miss) → ``POST
    /cloudapi/1.0.0/globalRoles`` → ``PUT …/{id}/rights`` → optional publish
    (``…/tenants/publish`` with ``{"values": [{name, id}]}`` for one org, or
    ``…/tenants/publishAll``).
    """
    envelope: dict[str, Any] = {"role": None, "rights_count": None, "published_to": []}
    existing = await find_global_role(connector, target, operator, params["name"])
    if existing is not None:
        return {
            **envelope,
            "status": "unchanged",
            "role": entity_ref(existing),
            "guidance": (
                f"a global role named {existing.get('name')!r} already exists; nothing was "
                "written (its rights and publication were not changed)"
            ),
        }
    rights, problem = await _resolve_role_rights(connector, target, operator, params)
    if problem is not None:
        return {**envelope, "status": "invalid_request", "guidance": problem}
    publish_org: dict[str, Any] | None = None
    org_name = params.get("publish_to_org")
    if org_name:
        publish_org = await find_org(connector, target, operator, org_name)
        if publish_org is None:
            return {
                **envelope,
                "status": "invalid_request",
                "guidance": f"publish_to_org {org_name!r} is not an org on this appliance",
            }

    role_id = await _create_global_role(connector, target, operator, params)
    segment = quote_segment(role_id)
    await connector._post_json(
        target,
        PROVIDER_GLOBAL_ROLE_RIGHTS_PATH.format(id=segment),
        operator=operator,
        verb="PUT",
        json={"values": rights},
    )
    published = await _publish_role(
        connector,
        target,
        operator,
        segment,
        publish_all=bool(params.get("publish_all")),
        org=publish_org,
    )
    return {
        "status": "created",
        "role": {"id": role_id, "name": params["name"]},
        "rights_count": len(rights),
        "published_to": published,
        "guidance": None,
    }


# ---------------------------------------------------------------------------
# Org user create
# ---------------------------------------------------------------------------


def _user_body(
    params: Mapping[str, Any],
    password: str,
    role: Mapping[str, Any],
    org: Mapping[str, Any],
) -> dict[str, Any]:
    """The ``POST /cloudapi/1.0.0/users`` body (``go-vcloud-director`` ``OpenApiUser``)."""
    body: dict[str, Any] = {
        "username": params["username"],
        "password": password,
        "providerType": "LOCAL",
        "enabled": bool(params.get("enabled", True)),
        "locked": False,
        "roleEntityRefs": [entity_ref(role)],
        "orgEntityRef": entity_ref(org),
    }
    for param_key, body_key in (
        ("email", "email"),
        ("full_name", "fullName"),
        ("description", "description"),
    ):
        if params.get(param_key):
            body[body_key] = params[param_key]
    return body


async def provider_user_create(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.user.create`` — local org user (idempotent on username).

    ``POST /cloudapi/1.0.0/users`` under the org's tenant-context headers.
    The role is an **org-scoped** role (a published global role appears
    there). The password is read from ``password_secret_ref`` under the
    operator's identity -- it is never an op param -- and is sent only in
    the create body; the result carries no credential.
    """
    org_name, username, role_name = params["org"], params["username"], params["role"]
    envelope: dict[str, Any] = {"user": None}
    org = await find_org(connector, target, operator, org_name)
    if org is None:
        return {
            **envelope,
            "status": "invalid_request",
            "guidance": f"no org named {org_name!r}; create it with vcfa.provider.org.create",
        }
    existing = await find_org_user(connector, target, operator, org, username)
    if existing is not None:
        return {
            **envelope,
            "status": "unchanged",
            "user": {
                "id": existing.get("id"),
                "username": existing.get("username"),
                "org": org_name,
            },
            "guidance": (
                f"user {username!r} already exists in org {org_name!r}; nothing was written"
            ),
        }
    headers = tenant_context_headers(org)
    role = await find_by_field(
        connector, target, operator, PROVIDER_ROLES_PATH, role_name, headers=headers
    )
    if role is None:
        return {
            **envelope,
            "status": "invalid_request",
            "guidance": (
                f"role {role_name!r} is not available in org {org_name!r}; publish a global "
                "role to the org (vcfa.provider.role.create with publish_to_org) and check "
                "vcfa.provider.role.list with org"
            ),
        }
    password = await read_password(operator, target, params)
    if password is None:
        return {
            **envelope,
            "status": "invalid_request",
            "guidance": password_missing_guidance(params),
        }
    created = await connector._post_json(
        target,
        PROVIDER_USERS_PATH,
        operator=operator,
        json=_user_body(params, password, role, org),
        extra_headers=headers,
    )
    return {
        "status": "created",
        "user": {
            "id": created.get("id"),
            "username": created.get("username") or username,
            "org": org_name,
            "role": role_name,
        },
        "guidance": None,
    }
