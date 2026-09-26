# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers for the VCFA provisioning ops (evoila/meho#3890).

This module holds the right + role reads, the org and project creates,
and the tenant-plane login test. The global-role and org-user creates
live in :mod:`._role_user`; the API-token pair in :mod:`._api_token` (it
runs under the org user's own session, not the connector's cached
provider session); the shared lookups in :mod:`._lookups`.

Every provisioning write returns one envelope shape (the
``vmware.composite.vm.resource_allocation.set`` mold, #3880)::

    {"status": "created" | "unchanged" | "invalid_request", <resource>, "guidance"}

* ``invalid_request`` is returned **before any write** -- a base role,
  right, org or role name that does not resolve, or a password secret
  with no usable value. Nothing reaches the appliance's write paths.
* ``unchanged`` means the object already exists (idempotent on name):
  nothing is written and the existing id is returned.
* An upstream 4xx/5xx raises :exc:`httpx.HTTPStatusError`, which the
  dispatcher maps to ``connector_error`` carrying ``http_status`` +
  ``upstream_message`` (#2680); a rejected login raises
  :class:`~meho_backplane.connectors._shared.vcf_auth.ConnectorAuthError`
  (``connector_auth_failed``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.vcf_automation._auth import (
    load_credentials_with_override,
    tenant_login,
)
from meho_backplane.connectors.vcf_automation._lookups import (
    find_org,
    find_project,
    tenant_context_headers,
)
from meho_backplane.connectors.vcf_automation._paths import (
    PROVIDER_GLOBAL_ROLES_PATH,
    PROVIDER_RIGHTS_PATH,
    PROVIDER_ROLES_PATH,
    TENANT_IAAS_API_VERSION,
)
from meho_backplane.connectors.vcf_automation._routing import (
    TENANT_ACCEPT,
    TENANT_VERSION_PATH,
    vhost_header,
)
from meho_backplane.connectors.vcf_automation.session import VCFA_REFRESH_TOKEN_FIELD
from meho_backplane.connectors.vcf_automation.typed_ops import (
    PROVIDER_ORGS_PATH,
    TENANT_PROJECTS_PATH,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "VcfaProvisioningError",
    "org_create_body",
    "project_create_body",
    "provider_org_create",
    "provider_right_list",
    "provider_role_list",
    "tenant_login_test",
    "tenant_project_create",
]


class VcfaProvisioningError(ValueError):
    """A provisioning read could not resolve its scope (e.g. an unknown org).

    Subclasses :class:`ValueError` so the dispatcher's ``connector_error``
    envelope names it in ``extras.exception_class``.
    """


# ---------------------------------------------------------------------------
# Provider reads
# ---------------------------------------------------------------------------


def _paging(params: Mapping[str, Any]) -> dict[str, Any]:
    return {key: params[key] for key in ("page", "pageSize") if params.get(key) is not None}


def _name_contains_filter(params: Mapping[str, Any]) -> dict[str, Any]:
    needle = params.get("name_contains")
    return {"filter": f"name==*{needle}*"} if needle else {}


async def provider_right_list(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.right.list`` — ``GET /cloudapi/1.0.0/rights``."""
    return await connector._request_json(
        target,
        "GET",
        PROVIDER_RIGHTS_PATH,
        operator=operator,
        params={**_name_contains_filter(params), **_paging(params)} or None,
    )


async def provider_role_list(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.role.list`` — global roles, or one org's roles.

    Without ``org``: ``GET /cloudapi/1.0.0/globalRoles`` (the roles tenants
    consume once published). With ``org``: ``GET /cloudapi/1.0.0/roles``
    under that org's tenant-context headers -- the org-scoped role ids a
    user create references.
    """
    query = {**_name_contains_filter(params), **_paging(params)} or None
    org_name = params.get("org")
    if not org_name:
        return await connector._request_json(
            target, "GET", PROVIDER_GLOBAL_ROLES_PATH, operator=operator, params=query
        )
    org = await find_org(connector, target, operator, org_name)
    if org is None:
        raise VcfaProvisioningError(
            f"vcfa.provider.role.list: no org named {org_name!r} on target {target.name!r}; "
            "list orgs with vcfa.provider.org.list"
        )
    return await connector._request_json(
        target,
        "GET",
        PROVIDER_ROLES_PATH,
        operator=operator,
        params=query,
        extra_headers=tenant_context_headers(org),
    )


# ---------------------------------------------------------------------------
# Org create
# ---------------------------------------------------------------------------


def _org_view(org: Mapping[str, Any]) -> dict[str, Any]:
    classic = org.get("isClassicTenant")
    return {
        "id": org.get("id"),
        "name": org.get("name"),
        "displayName": org.get("displayName"),
        "isEnabled": org.get("isEnabled"),
        "type": None if classic is None else ("vm_apps" if classic else "all_apps"),
    }


def org_create_body(params: Mapping[str, Any]) -> dict[str, Any]:
    """The ``POST /cloudapi/1.0.0/orgs`` body (``TmOrg`` shape)."""
    name = params["name"]
    body: dict[str, Any] = {
        "name": name,
        "displayName": params.get("display_name") or name,
        "isEnabled": bool(params.get("is_enabled", True)),
        "isClassicTenant": (params.get("org_type") or "vm_apps") == "vm_apps",
    }
    if params.get("description"):
        body["description"] = params["description"]
    return body


async def provider_org_create(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.org.create`` — ``POST /cloudapi/1.0.0/orgs`` (idempotent on name).

    ``org_type='vm_apps'`` (default) sets ``isClassicTenant: true`` -- a VM
    Apps org, which needs no region / VPC / quota. The create may answer
    201 with the org body or 202 with an empty body (async task); in the
    latter case the org is re-read by name for its id.
    """
    name = params["name"]
    if name.casefold() == "system":
        return {
            "status": "invalid_request",
            "org": None,
            "guidance": "'System' is the provider org and cannot be created",
        }
    existing = await find_org(connector, target, operator, name)
    if existing is not None:
        return {
            "status": "unchanged",
            "org": _org_view(existing),
            "guidance": (
                f"an org named {existing.get('name')!r} already exists; nothing was written"
            ),
        }
    created = await connector._post_json(
        target, PROVIDER_ORGS_PATH, operator=operator, json=org_create_body(params)
    )
    if not created.get("id"):
        created = await find_org(connector, target, operator, name) or {"name": name}
    view = _org_view(created)
    return {
        "status": "created",
        "org": view,
        "guidance": None
        if view["id"]
        else (
            "the appliance accepted the create asynchronously and the org is not listed yet; "
            "re-read with vcfa.provider.org.list"
        ),
    }


# ---------------------------------------------------------------------------
# Tenant project create
# ---------------------------------------------------------------------------


def project_create_body(params: Mapping[str, Any]) -> dict[str, Any]:
    """The ``POST /iaas/api/projects`` body (``ProjectSpecification``)."""
    body: dict[str, Any] = {"name": params["name"]}
    if params.get("description"):
        body["description"] = params["description"]
    for key in ("administrators", "members", "viewers"):
        principals = params.get(key)
        if principals:
            body[key] = [{"email": p["email"], "type": p.get("type") or "user"} for p in principals]
    return body


def _project_view(project: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": project.get("id"),
        "name": project.get("name"),
        "organizationId": project.get("organizationId"),
    }


async def tenant_project_create(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.tenant.project.create`` — ``POST /iaas/api/projects`` (idempotent on name)."""
    existing = await find_project(connector, target, operator, params["name"])
    if existing is not None:
        return {
            "status": "unchanged",
            "project": _project_view(existing),
            "guidance": (
                f"a project named {existing.get('name')!r} already exists; nothing was written"
            ),
        }
    created = await connector._post_json(
        target,
        TENANT_PROJECTS_PATH,
        operator=operator,
        params={"apiVersion": TENANT_IAAS_API_VERSION},
        json=project_create_body(params),
    )
    return {"status": "created", "project": _project_view(created), "guidance": None}


# ---------------------------------------------------------------------------
# Tenant login test
# ---------------------------------------------------------------------------


async def tenant_login_test(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.tenant.login.test`` — run only the tenant-plane login.

    Performs a fresh :func:`._auth.tenant_login` with the target's secret
    (the ``refresh_token`` exchange when the secret carries one) and
    reports whether it authenticated. The minted bearer is discarded --
    never cached, never returned -- and no authenticated data call is
    made. ``api_version`` comes from the unauthenticated
    ``GET /iaas/api/about`` probe. A refused login is a *result*
    (``authenticated: false`` + the error), not an op failure.
    """
    del params  # schema declares the param object empty
    creds = await load_credentials_with_override(
        connector._credentials_loader, target, operator, None
    )
    client = await connector._http_client(target)
    extensions = connector._request_extensions(target)
    error: dict[str, Any] | None = None
    try:
        await tenant_login(client, creds, target, request_extensions=extensions)
    except ConnectorAuthError as exc:
        error = {"cause": exc.cause, "status_code": exc.status_code, "message": str(exc)}
    except (httpx.HTTPError, RuntimeError) as exc:
        error = {"cause": type(exc).__name__, "status_code": None, "message": str(exc)}
    api_version: str | None = None
    try:
        about = await client.get(
            TENANT_VERSION_PATH,
            headers={
                "Accept": TENANT_ACCEPT,
                **vhost_header(getattr(target, "fqdn", None), getattr(target, "port", None)),
            },
            extensions=extensions,
        )
        payload = about.json() if about.is_success else None
        if isinstance(payload, dict) and isinstance(payload.get("latestApiVersion"), str):
            api_version = payload["latestApiVersion"]
    except (httpx.HTTPError, ValueError):
        api_version = None
    return {
        "authenticated": error is None,
        "login_flow": (
            "refresh_token" if creds.get(VCFA_REFRESH_TOKEN_FIELD) else "username_password"
        ),
        "api_version": api_version,
        "error": error,
    }
