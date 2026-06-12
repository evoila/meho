# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho://tenant/{tenant_id}/info`` — the reference MCP resource (G0.5-T4).

The simplest universally-useful resource: an identity bundle every MCP
client can fetch at session start to confirm which tenant it's operating
on. Establishes the ``meho://`` URI scheme convention every downstream
G3-G9 resource will follow.

Tenant-boundary enforcement is the load-bearing pattern
=======================================================

The handler validates that the ``tenant_id`` bound from the URI
template matches the operator's ``tenant_id`` from the JWT — cross-
tenant reads are rejected at the resource layer, not just at the
JWT-validation layer. The MCP dispatcher's call-time RBAC re-check
already gates *role*, but it doesn't gate *tenant scope* (an
``operator``-role operator in tenant A can absolutely read
``meho://tenant/<A>/info``; what's forbidden is reading
``meho://tenant/<B>/info`` from inside tenant A's session).

Downstream resources (G4 ``meho://kb/{slug}``, G5
``meho://target/{tenant_id}/{target_slug}``, …) copy this pattern: bind
the tenant variable from the URI template, validate it equals
``operator.tenant_id``, reject otherwise. Adding the check at the
registry handler rather than relying on a global middleware keeps the
contract obvious at the call site and avoids a forgotten check
escalating into a tenant-boundary CVE.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.mcp.registry import (
    ResourceTemplateDefinition,
    register_mcp_resource,
)
from meho_backplane.mcp.server import McpInvalidParamsError


async def _tenant_info_handler(
    operator: Operator,
    bound: dict[str, str],
) -> dict[str, Any]:
    """Return the operator's tenant identity bundle.

    Three rejection arms, all mapped onto the dispatcher's
    :class:`~meho_backplane.mcp.server.McpInvalidParamsError`
    (``-32602``) — the JSON-RPC transport doesn't carry HTTP-status
    semantics; ``-32602`` plus a structured ``message`` is the
    spec-correct shape for any input-validation failure in this layer:

    * **Malformed ``tenant_id``** — the URI bound a non-UUID string.
      Equivalent to AC #8's ``400`` on a bad tenant_id.
    * **Cross-tenant read** — bound tenant differs from the operator's
      JWT tenant. Equivalent to AC #7's ``403``. The check runs *before*
      the DB query so a probe attempt against an arbitrary UUID can't
      learn whether that tenant exists.
    * **Tenant not found** — the operator's own tenant_id has no row
      in the ``tenant`` table. Only reachable if the JWT's
      ``tenant_id`` claim was minted before the tenant row was seeded;
      treated as INVALID_PARAMS rather than INTERNAL_ERROR because the
      operator can act on the message (re-seed / re-mint), and -32603
      would hide a configuration error inside a generic-error envelope.
    """
    raw_id = bound["tenant_id"]
    try:
        bound_uuid = UUID(raw_id)
    except ValueError as exc:
        raise McpInvalidParamsError(
            f"tenant_info: invalid tenant_id (not a UUID): {raw_id!r}",
        ) from exc

    if bound_uuid != operator.tenant_id:
        raise McpInvalidParamsError(
            "tenant_info: cross-tenant access denied — bound "
            f"tenant_id {raw_id!r} does not match the operator's tenant",
        )

    async with get_sessionmaker()() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == bound_uuid))
        ).scalar_one_or_none()
    if tenant is None:
        raise McpInvalidParamsError(
            f"tenant_info: tenant row not found for id {raw_id!r}",
        )

    return {
        "id": str(tenant.id),
        "slug": tenant.slug,
        "name": tenant.name,
        "operator_role": operator.tenant_role.value,
        # G4.5-T1: the tenant's provisioned capability keys, sorted for a
        # deterministic wire shape. ONE source of truth for provisioning
        # — MCP clients and the CLI (T5) read what's enabled from here
        # rather than re-deriving it from the JWT claim. Empty array when
        # the tenant has no add-on provisioned (fail-closed default).
        "capabilities": sorted(operator.capabilities),
    }


register_mcp_resource(
    definition=ResourceTemplateDefinition(
        uriTemplate="meho://tenant/{tenant_id}/info",
        name="Tenant identity",
        description=(
            "Identity bundle for the operator's tenant: id, slug, name, "
            "and the operator's role within that tenant. Cross-tenant "
            "reads return INVALID_PARAMS — the bound tenant_id must match "
            "the operator's JWT tenant claim."
        ),
        mimeType="application/json",
        required_role=TenantRole.READ_ONLY,
    ),
    handler=_tenant_info_handler,
)
