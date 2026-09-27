# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time ``proposed_effect`` previews for the VCFA provisioning writes (#3890).

Wired onto the per-op builder hook
(:func:`~meho_backplane.operations._preview.register_preview_builder`), the
same seam the vmware-rest / keycloak write previews use. Each builder
echoes **what would be created** -- identity fields only, never a secret
-- and, when the dispatcher hands it a connector (the approval-park path),
adds one side-effect-free existence read so the approver sees
``would: "create"`` vs ``would: "unchanged"`` (the #3880
``resource_allocation.set`` live-read mold). Without a connector (the
egress-free ``preview_operation`` path) the existence read is skipped and
``would`` is ``"create_if_absent"``. A failed existence read degrades to
``exists: null`` rather than dropping the preview.

Redaction: the user / token ops carry only Vault *refs* (paths) in their
params, and these builders echo those refs, never a value. The two
credential-class ops (``user.create`` / ``api_token.revoke`` =
``credential_write``, ``api_token.create`` = ``credential_mint``) get no
generic params echo, so the bespoke builder is their only preview;
``preview_operation`` still answers ``unavailable`` for them by design
(``_request_preview._is_previewable``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from meho_backplane.connectors.vcf_automation._lookups import (
    find_global_role,
    find_org,
    find_org_user,
    find_project,
    password_ref,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__: list[str] = []


async def _existence(
    ctx: PreviewContext, lookup: Callable[[Any], Awaitable[dict[str, Any] | None]]
) -> dict[str, Any]:
    """``{would, exists, existing_id}`` from one fail-soft existence read."""
    connector = ctx.connector_instance
    if connector is None:
        return {"would": "create_if_absent", "exists": None, "existing_id": None}
    try:
        found = await lookup(connector)
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError):
        return {"would": "create_if_absent", "exists": None, "existing_id": None}
    if found is None:
        return {"would": "create", "exists": False, "existing_id": None}
    return {"would": "unchanged", "exists": True, "existing_id": found.get("id")}


async def _org_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    p = ctx.params
    name = p.get("name")
    if not isinstance(name, str):
        return None
    return {
        "action": "create_org",
        "name": name,
        "display_name": p.get("display_name") or name,
        "org_type": p.get("org_type") or "vm_apps",
        "is_enabled": p.get("is_enabled", True),
        **await _existence(ctx, lambda c: find_org(c, ctx.target, ctx.operator, name)),
    }


async def _role_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    p = ctx.params
    name = p.get("name")
    if not isinstance(name, str):
        return None
    return {
        "action": "create_global_role",
        "name": name,
        "base_role": p.get("base_role"),
        "extra_rights": list(p.get("rights") or []),
        "publish_to": "all" if p.get("publish_all") else p.get("publish_to_org"),
        **await _existence(ctx, lambda c: find_global_role(c, ctx.target, ctx.operator, name)),
    }


async def _user_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    p = ctx.params
    org_name, username = p.get("org"), p.get("username")
    if not isinstance(org_name, str) or not isinstance(username, str):
        return None
    secret_ref, mount, key = password_ref(p)

    async def _lookup(connector: Any) -> dict[str, Any] | None:
        org = await find_org(connector, ctx.target, ctx.operator, org_name)
        if org is None:
            raise KeyError(org_name)
        return await find_org_user(connector, ctx.target, ctx.operator, org, username)

    return {
        "action": "create_org_user",
        "org": org_name,
        "username": username,
        "role": p.get("role"),
        "password_source": {"secret_ref": secret_ref, "mount": mount, "key": key},
        **await _existence(ctx, _lookup),
    }


def _token_identity(ctx: PreviewContext) -> dict[str, Any] | None:
    p = ctx.params
    if not all(isinstance(p.get(k), str) for k in ("org", "username", "token_name")):
        return None
    secret_ref, mount, key = password_ref(p)
    return {
        "org": p["org"],
        "username": p["username"],
        "token_name": p["token_name"],
        "password_source": {"secret_ref": secret_ref, "mount": mount, "key": key},
    }


async def _api_token_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    identity = _token_identity(ctx)
    if identity is None:
        return None
    p = ctx.params
    return {
        "action": "mint_api_token",
        **identity,
        "store": {
            "mount": p.get("store_mount") or "secret",
            "secret_ref": p.get("store_secret_ref"),
            "field": p.get("store_field") or "refresh_token",
        },
        "token_value_returned": False,
    }


async def _api_token_revoke_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    identity = _token_identity(ctx)
    return None if identity is None else {"action": "revoke_api_token", **identity}


async def _project_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    p = ctx.params
    name = p.get("name")
    if not isinstance(name, str):
        return None
    return {
        "action": "create_project",
        "name": name,
        "description": p.get("description"),
        "administrators": p.get("administrators") or [],
        "members": p.get("members") or [],
        "viewers": p.get("viewers") or [],
        **await _existence(ctx, lambda c: find_project(c, ctx.target, ctx.operator, name)),
    }


_BUILDERS = {
    "vcfa.provider.org.create": _org_create_preview,
    "vcfa.provider.role.create": _role_create_preview,
    "vcfa.provider.user.create": _user_create_preview,
    "vcfa.provider.api_token.create": _api_token_create_preview,
    "vcfa.provider.api_token.revoke": _api_token_revoke_preview,
    "vcfa.tenant.project.create": _project_create_preview,
}

for _op_id, _builder in _BUILDERS.items():
    register_preview_builder(_op_id, _builder)
