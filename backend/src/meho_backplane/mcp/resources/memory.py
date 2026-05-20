# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho://memory/{scope}/{slug}`` — memory entry resource (G5.1-T3).

The fetch-by-(scope, slug) companion to the :mod:`search_memory`
meta-tool. ``search_memory`` returns ranked hits with a snippet plus
the ``(scope, slug)`` pair the agent needs to drill into the full
body via ``resources/read``. The split keeps every search response
small enough for an agent to scan many hits without exhausting
context, then drill into the chosen one explicitly.

URI template + variable binding
================================

``meho://memory/{scope}/{slug}`` follows the v0.2 simple-expansion
subset of RFC 6570 (two named variables, no operators). The MCP
registry's URI matcher binds ``{scope}`` and ``{slug}`` from the
concrete URI and forwards the dict to the handler.

* ``{scope}`` must be one of the five
  :class:`~meho_backplane.memory.schemas.MemoryScope` values; an
  unrecognised string surfaces as
  :class:`~meho_backplane.mcp.server.McpInvalidParamsError` before
  any DB query.
* ``{slug}`` is validated against
  :data:`~meho_backplane.memory.schemas.SLUG_PATTERN` so a malformed
  client URI surfaces as ``-32602`` rather than reaching the DB
  layer.

The URI deliberately encodes only ``scope`` and ``slug`` — not
``target_name``, even for target-flavoured scopes. This is the
load-bearing read shape the issue AC names: the read path is
**only** valid for the operator's own user-scoped memories under
this URI template. Tenant- and target-scoped reads still need
``target_name`` to disambiguate, which is currently out of scope for
the v0.2 resource template (a future polish would extend the
template or add a query-parameter form; for now the resource is
focused on the user-scoped recall case where the encoded
``source_id`` already carries the operator's ``sub`` server-side).

The handler still inspects every scope at the service layer so an
operator who passes ``meho://memory/tenant/<slug>`` receives the
tenant-scoped entry when it exists (TENANT scope has no
``target_name`` dimension); operators passing
``meho://memory/target/<slug>`` collapse to "not found" because the
``source_id`` lookup cannot reconstruct the ``target_name`` segment.
This is the right info-leak shape: the resource template never
admits a target-name probe channel.

Tenant scoping + RBAC
=====================

The handler binds :attr:`Operator.tenant_id` from the validated JWT
operator and passes it to :meth:`MemoryService.recall`. The substrate's
natural-key uniqueness on ``(tenant_id, source, source_id)`` plus the
per-scope :class:`~meho_backplane.memory.rbac.MemoryRbacResolver`
match means a different tenant's same-slug entry — or another
operator's user-scoped entry under the same slug — is structurally
invisible. :meth:`MemoryService.recall` returns ``None`` for both
cases without distinguishing "no such row" from "you don't have
access" — the **404-vs-403 collapse** the issue AC names. The
handler surfaces ``None`` as
:class:`~meho_backplane.mcp.server.McpInvalidParamsError` with a
"memory entry not found" message so the wire shape never reveals
the foreign tenant's or foreign operator's existence.

Response shape
==============

The MCP ``resources/read`` response is a ``contents[]`` array. The
dispatcher (:func:`~meho_backplane.mcp.handlers.handle_resources_read`)
wraps this handler's return value in a single text-block entry whose
``mimeType`` is taken from the resource template's
:attr:`ResourceTemplateDefinition.mimeType` (``text/markdown`` for
memory entries) and whose ``text`` is the JSON-serialised handler
return. The handler returns
``{id, tenant_id, scope, slug, body, metadata, expires_at, ...}`` so a
client UI can render the entry alongside its provenance even though
the entry's *content* is Markdown.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import (
    ResourceTemplateDefinition,
    register_mcp_resource,
)
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.memory.schemas import MemoryScope, validate_slug
from meho_backplane.memory.service import MemoryService


async def _memory_entry_handler(
    operator: Operator,
    bound: dict[str, str],
) -> dict[str, Any]:
    """Return the full :class:`~meho_backplane.memory.schemas.MemoryEntry` for ``(scope, slug)``.

    Three rejection arms, all mapped onto
    :class:`McpInvalidParamsError` (``-32602``) — JSON-RPC carries no
    HTTP-status semantics so every input-validation failure surfaces
    on the same error code:

    * **Unrecognised scope** — the URI bound a ``{scope}`` value that
      doesn't match any :class:`MemoryScope` member. Surfaces before
      the DB query so a probe with an arbitrary scope string can't
      be used as an oracle for the enum's shape.
    * **Malformed slug** — the URI bound a ``{slug}`` value that
      doesn't match :data:`SLUG_PATTERN`. Caught from
      :func:`~meho_backplane.memory.schemas.validate_slug`'s
      :class:`ValueError` and surfaced before any DB query so a
      probe attempt with an arbitrary URI fragment can't be used
      as an oracle for what slugs exist.
    * **Entry not found in the operator's tenant or under their
      RBAC** — :meth:`MemoryService.recall` collapses both cases to
      ``None``. The handler renders both as "memory entry not
      found", which is the spec-correct shape for an info-leak-safe
      404-vs-403 boundary.
    """
    scope_raw = bound["scope"]
    slug_raw = bound["slug"]

    try:
        scope = MemoryScope(scope_raw)
    except ValueError as exc:
        raise McpInvalidParamsError(
            f"memory entry: invalid scope {scope_raw!r}; "
            f"expected one of {[s.value for s in MemoryScope]}",
        ) from exc

    try:
        slug = validate_slug(slug_raw)
    except ValueError as exc:
        raise McpInvalidParamsError(
            f"memory entry: invalid slug {slug_raw!r}: {exc}",
        ) from exc

    service = MemoryService()
    entry = await service.recall(operator, scope=scope, slug=slug)
    if entry is None:
        # 404-vs-403 collapse: the resource handler never distinguishes
        # "no such memory" from "you don't have access". See module
        # docstring's "Tenant scoping + RBAC" section.
        raise McpInvalidParamsError(
            f"memory entry not found: scope={scope.value!r}, slug={slug!r}",
        )

    return entry.model_dump(mode="json")


register_mcp_resource(
    definition=ResourceTemplateDefinition(
        uriTemplate="meho://memory/{scope}/{slug}",
        name="Memory entry",
        description=(
            "Full body and metadata of one memory entry, identified by "
            "scope + slug. Use after `search_memory` has returned a hit "
            "whose snippet is not enough to act on — this resource "
            "returns the entire Markdown body plus provenance metadata "
            "(expires_at, tags, user_sub for user-scoped entries). "
            "Returns INVALID_PARAMS for an unrecognised scope, a "
            "malformed slug, or a (scope, slug) that doesn't exist "
            "under the operator's tenant + RBAC (cross-tenant or "
            "cross-operator reads collapse to 'not found' without "
            "revealing the foreign tenant's or foreign operator's "
            "existence)."
        ),
        mimeType="text/markdown",
        required_role=TenantRole.OPERATOR,
    ),
    handler=_memory_entry_handler,
)
