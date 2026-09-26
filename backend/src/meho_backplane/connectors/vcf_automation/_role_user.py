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

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

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
    PROVIDER_GLOBAL_ROLE_PATH,
    PROVIDER_GLOBAL_ROLE_PUBLISH_ALL_PATH,
    PROVIDER_GLOBAL_ROLE_PUBLISH_PATH,
    PROVIDER_GLOBAL_ROLE_RIGHTS_PATH,
    PROVIDER_GLOBAL_ROLE_TENANTS_PATH,
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

_log = structlog.get_logger(__name__)

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


async def _is_published_to(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    role: Mapping[str, Any],
    org: Mapping[str, Any],
) -> bool:
    """Whether the existing *role* is already published to *org* (or to all orgs)."""
    if role.get("publishAll") is True:
        return True
    path = PROVIDER_GLOBAL_ROLE_TENANTS_PATH.format(id=quote_segment(str(role.get("id"))))
    tenants = await list_all(connector, target, operator, path)
    return any(str(t.get("id")) == str(org.get("id")) for t in tenants)


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


async def _delete_role_quietly(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    role_segment: str,
) -> bool:
    """Best-effort rollback of a just-created role; never raises."""
    try:
        await connector._post_json(
            target,
            PROVIDER_GLOBAL_ROLE_PATH.format(id=role_segment),
            operator=operator,
            verb="DELETE",
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        _log.warning("vcfa_role_rollback_failed", error=type(exc).__name__)
        return False
    return True


async def _put_rights(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    role_segment: str,
    rights: list[dict[str, Any]],
) -> None:
    await connector._post_json(
        target,
        PROVIDER_GLOBAL_ROLE_RIGHTS_PATH.format(id=role_segment),
        operator=operator,
        verb="PUT",
        json={"values": rights},
    )


async def _converge_existing(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    params: Mapping[str, Any],
    existing: Mapping[str, Any],
    publish_org: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Repair an existing role toward the request instead of blindly answering ``unchanged``.

    A role left half-built by an earlier run (rights PUT or publish failed)
    must be repairable by re-running the op. So: a role that carries **no**
    rights gets the requested rights; a requested publication that is not in
    place yet is applied. Rights already present are never replaced (the op
    creates roles; it does not edit a curated one). Answers ``updated`` with
    the ``reconciled`` steps, or ``unchanged`` when nothing was needed.
    """
    segment = quote_segment(str(existing.get("id")))
    current = await list_all(
        connector, target, operator, PROVIDER_GLOBAL_ROLE_RIGHTS_PATH.format(id=segment)
    )
    rights: list[dict[str, Any]] = []
    if not current and (params.get("base_role") or params.get("rights")):
        rights, problem = await _resolve_role_rights(connector, target, operator, params)
        if problem is not None:
            return {
                "status": "invalid_request",
                "role": entity_ref(existing),
                "rights_count": 0,
                "published_to": [],
                "reconciled": [],
                "guidance": problem,
            }
    publish_all = bool(params.get("publish_all")) and existing.get("publishAll") is not True
    org = publish_org
    if (
        org is not None
        and not publish_all
        and await _is_published_to(connector, target, operator, existing, org)
    ):
        org = None
    reconciled: list[str] = []
    if rights:
        await _put_rights(connector, target, operator, segment, rights)
        reconciled.append("rights")
    published = await _publish_role(
        connector, target, operator, segment, publish_all=publish_all, org=org
    )
    if published:
        reconciled.append("publication")
    name = existing.get("name")
    return {
        "status": "updated" if reconciled else "unchanged",
        "role": entity_ref(existing),
        "rights_count": len(rights) if rights else len(current),
        "published_to": published,
        "reconciled": reconciled,
        "guidance": (
            f"global role {name!r} already existed; completed: {', '.join(reconciled)}"
            if reconciled
            else f"global role {name!r} already exists with rights and the requested "
            "publication; nothing was written"
        ),
    }


async def provider_role_create(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.role.create`` — custom global role (idempotent, self-repairing).

    Flow: resolve the ``publish_to_org`` org → if the role exists, converge
    it (:func:`_converge_existing`: fill empty rights, apply a missing
    publication → ``updated`` / ``unchanged``) → otherwise resolve the base
    role's rights + each extra right name (``invalid_request`` on a miss,
    nothing written) → ``POST /cloudapi/1.0.0/globalRoles`` → ``PUT
    …/{id}/rights`` (on failure the new role is deleted so no rights-less
    role is left behind) → optional publish (``…/tenants/publish`` with
    ``{"values": [{name, id}]}`` for one org, or ``…/tenants/publishAll``).
    A failed publish leaves a complete but unpublished role, which a re-run
    publishes.
    """
    envelope: dict[str, Any] = {
        "role": None,
        "rights_count": None,
        "published_to": [],
        "reconciled": [],
    }
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
    existing = await find_global_role(connector, target, operator, params["name"])
    if existing is not None:
        return await _converge_existing(connector, target, operator, params, existing, publish_org)
    rights, problem = await _resolve_role_rights(connector, target, operator, params)
    if problem is not None:
        return {**envelope, "status": "invalid_request", "guidance": problem}

    role_id = await _create_global_role(connector, target, operator, params)
    segment = quote_segment(role_id)
    try:
        await _put_rights(connector, target, operator, segment, rights)
    except BaseException:
        deleted = await asyncio.shield(_delete_role_quietly(connector, target, operator, segment))
        _log.warning("vcfa_role_rights_put_failed", role_deleted=deleted)
        raise
    published = await _publish_role(
        connector,
        target,
        operator,
        segment,
        publish_all=bool(params.get("publish_all")),
        org=publish_org,
    )
    return {
        **envelope,
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
