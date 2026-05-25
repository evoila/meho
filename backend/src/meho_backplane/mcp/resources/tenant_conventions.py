# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho://tenant/{tenant_id}/conventions/{slug}`` -- per-slug convention resource (G7.1-T4).

The fetch-by-slug companion to the session-preamble assembler in
:mod:`meho_backplane.conventions.preamble`. The preamble packs a
tenant's ``kind='operational'`` rules into ``initialize``'s
``instructions`` field; this resource gives MCP clients a way to
fetch *any* convention (operational / workflow / reference) by slug
through ``resources/read`` -- the natural drill-in surface for an
agent that needs the full body of a referenced rule (e.g. the
preamble names a slug in passing and the agent wants the
authoritative text).

URI template + variable binding
================================

``meho://tenant/{tenant_id}/conventions/{slug}`` follows the v0.2
simple-expansion subset of RFC 6570 (two named variables, no
operators). The MCP registry's URI matcher binds ``{tenant_id}``
and ``{slug}`` from the concrete URI and forwards the dict to the
handler.

* ``{tenant_id}`` must be a UUID; otherwise the handler rejects
  with :class:`~meho_backplane.mcp.server.McpInvalidParamsError`
  (-32602). The check is structural -- a malformed UUID can't
  reach the DB layer.
* ``{slug}`` is bounded by the same shape T2's
  :class:`~meho_backplane.conventions.schemas.ConventionCreate`
  validator imposes (lowercase ASCII, digits, hyphen; ``^[a-z0-9][a-z0-9-]*$``).
  An out-of-shape slug rejects without a DB probe.

Tenant scoping is the load-bearing pattern
==========================================

The handler validates that the ``tenant_id`` bound from the URI
template matches the operator's ``tenant_id`` from the JWT --
cross-tenant reads are rejected at the resource layer, not just
the JWT validation layer. The MCP dispatcher's call-time RBAC
re-check already gates *role*, but it doesn't gate *tenant scope*
(an ``operator``-role operator in tenant A can absolutely read
``meho://tenant/<A>/conventions/<slug>``; what's forbidden is
reading ``meho://tenant/<B>/conventions/<slug>`` from inside
tenant A's session).

This mirrors the :mod:`~meho_backplane.mcp.resources.tenant_info`
pattern (G0.5-T4 #249) -- bind the tenant variable from the URI
template, validate it equals ``operator.tenant_id``, reject
otherwise. The check fires BEFORE the DB query so a probe attempt
against an arbitrary UUID can't learn whether that tenant exists.

The issue body's acceptance criterion calls this a "403"; in the
JSON-RPC transport that maps to -32602 (the transport carries no
HTTP-status semantics; -32602 plus the "cross-tenant" message is
the spec-correct shape). Same divergence the
:mod:`~meho_backplane.mcp.resources.tenant_info` module documented.

Response shape
==============

The handler returns the full convention shape:
``{id, tenant_id, slug, title, body, kind, priority, created_by_sub,
created_at, updated_at}`` as a JSON-serialised dict. The dispatcher
(:func:`~meho_backplane.mcp.handlers.handle_resources_read`) wraps it
in a single ``contents`` entry with ``mimeType="text/markdown"`` --
mirroring the kb / memory resources -- so a client UI can render the
body alongside its provenance metadata.

Why ``text/markdown`` even though the wrapper is JSON: the
*content* of the resource is the Markdown body; the JSON wrapper
exists so clients have programmatic access to ``priority`` /
``kind`` / ``created_by_sub`` without having to parse the Markdown.
The mimeType signal lets the rendering client know that the
``body`` key inside the JSON is Markdown, not plaintext.
"""

from __future__ import annotations

import re
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantConvention
from meho_backplane.mcp.registry import (
    ResourceTemplateDefinition,
    register_mcp_resource,
)
from meho_backplane.mcp.server import McpInvalidParamsError

#: Slug shape T2's :class:`~meho_backplane.conventions.schemas.ConventionCreate`
#: enforces on writes. Pre-compiled at module load so each
#: ``resources/read`` call doesn't re-parse the pattern. Pinned at
#: module level (not imported from schemas) because the schemas
#: pattern is embedded inside the pydantic ``Field`` regex param
#: and re-using it would require either an extract-to-constant
#: change in schemas.py (out of T4's scope) or a fragile
#: ``ConventionCreate.model_fields['slug'].metadata`` reach-around.
#: Pinning here means a future tightening of the slug shape needs
#: a synchronised update in *both* sites -- not great, but the
#: cost is zero ongoing maintenance until that hypothetical
#: tightening happens, and the patterns are documented as identical.
_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]*$")


async def _conventions_resource_handler(
    operator: Operator,
    bound: dict[str, str],
) -> dict[str, Any]:
    """Return the full :class:`TenantConvention` for ``(tenant_id, slug)``.

    Four rejection arms, all mapped onto
    :class:`McpInvalidParamsError` (-32602) per the JSON-RPC transport
    contract:

    * **Malformed ``tenant_id``** -- the URI bound a non-UUID string.
    * **Cross-tenant access** -- bound tenant differs from the
      operator's JWT tenant. Equivalent to AC's "403"; check runs
      BEFORE the DB query so the rejection cannot be used as an
      oracle for which tenants exist. The check is the load-bearing
      tenant-boundary enforcement the issue body specifies.
    * **Malformed ``slug``** -- the URI bound a slug that doesn't
      match :data:`_SLUG_RE`. Surfaces before the DB query so a
      probe attempt with an arbitrary URI fragment can't be used as
      an oracle for what slugs exist.
    * **Convention not found** -- the slug doesn't exist under this
      tenant. The handler treats this the same as cross-tenant for
      info-leak symmetry, but reaches this arm only after the
      tenant-boundary check passes (the bound tenant *is* the
      operator's tenant).
    """
    raw_tenant_id = bound["tenant_id"]
    raw_slug = bound["slug"]

    try:
        bound_tenant_id = UUID(raw_tenant_id)
    except ValueError as exc:
        raise McpInvalidParamsError(
            f"tenant_conventions: invalid tenant_id (not a UUID): {raw_tenant_id!r}",
        ) from exc

    if bound_tenant_id != operator.tenant_id:
        # AC's "403" -- mapped to -32602 per the JSON-RPC transport.
        # Pre-DB-query rejection so the response cannot oracle which
        # foreign tenants happen to host conventions.
        raise McpInvalidParamsError(
            "tenant_conventions: cross-tenant access denied -- "
            f"bound tenant_id {raw_tenant_id!r} does not match the "
            "operator's tenant",
        )

    if not _SLUG_RE.match(raw_slug):
        raise McpInvalidParamsError(
            f"tenant_conventions: invalid slug {raw_slug!r} (must match ^[a-z0-9][a-z0-9-]*$)",
        )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(TenantConvention).where(
                    TenantConvention.tenant_id == bound_tenant_id,
                    TenantConvention.slug == raw_slug,
                ),
            )
        ).scalar_one_or_none()

    if row is None:
        raise McpInvalidParamsError(
            f"tenant_conventions: convention not found: slug={raw_slug!r}",
        )

    # Surface the entire convention shape -- the operator already
    # cleared the tenant-boundary check, so every column is
    # legitimately theirs to read. Mirrors the API surface's
    # :class:`~meho_backplane.conventions.schemas.Convention` shape
    # (full body + metadata).
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "slug": row.slug,
        "title": row.title,
        "body": row.body,
        "kind": row.kind,
        "priority": row.priority,
        "created_by_sub": row.created_by_sub,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


register_mcp_resource(
    definition=ResourceTemplateDefinition(
        uriTemplate="meho://tenant/{tenant_id}/conventions/{slug}",
        name="Tenant operational convention",
        description=(
            "Full body + metadata of one tenant convention by slug. "
            "Tenant-scoped: the bound tenant_id MUST match the "
            "operator's JWT tenant claim or the read is rejected "
            "with INVALID_PARAMS (the JSON-RPC analogue of the "
            "issue's '403' acceptance criterion). Drill-in surface "
            "for an agent that wants the authoritative text of a "
            "rule referenced (by slug) from the session preamble "
            "in the initialize.instructions field."
        ),
        mimeType="text/markdown",
        required_role=TenantRole.READ_ONLY,
    ),
    handler=_conventions_resource_handler,
)
