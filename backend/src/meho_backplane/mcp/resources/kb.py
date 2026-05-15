# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho://kb/{slug}`` — kb entry resource (G4.1-T3).

The fetch-by-slug companion to the :mod:`search_knowledge` meta-tool.
``search_knowledge`` returns a 200-char snippet per hit; an agent that
wants the full body of a specific hit calls ``resources/read`` on
``meho://kb/{slug}`` with the hit's slug. The split keeps every search
response small enough for an agent to scan many hits without exhausting
context, then drill into the chosen one explicitly.

URI template + variable binding
================================

``meho://kb/{slug}`` follows the v0.2 simple-expansion subset of
RFC 6570 (one named variable, no operators). The MCP registry's URI
matcher binds ``{slug}`` from the concrete URI and forwards the dict
to the handler; the variable name is the only thing the matcher
guarantees (the *value* is whatever the client passed). The handler
re-validates the bound slug against :data:`~SLUG_PATTERN` so a
malformed client URI surfaces as ``-32602`` rather than reaching the
DB layer.

Tenant scoping
==============

The handler binds :attr:`Operator.tenant_id` from the validated JWT
operator and passes it to :meth:`KbService.get_entry`. The substrate's
natural-key uniqueness on ``(tenant_id, source, source_id)`` means a
different tenant's same-slug entry is structurally invisible — the
``get_entry`` call returns ``None`` (treated as "not found") regardless
of whether another tenant happens to own that slug. The handler does
not need a separate cross-tenant rejection branch because the
``None`` return path already covers it without revealing the foreign
tenant's existence.

Response shape
==============

The MCP ``resources/read`` response is a ``contents[]`` array. The
dispatcher (:func:`~meho_backplane.mcp.handlers.handle_resources_read`)
wraps this handler's return value in a single text-block entry whose
``mimeType`` is taken from the resource template's
:attr:`ResourceTemplateDefinition.mimeType` (``text/markdown`` for kb
entries) and whose ``text`` is the JSON-serialised handler return.
The handler returns ``{slug, body, metadata, ...}`` so a client UI can
render the entry alongside its provenance even though the entry's
*content* is Markdown.

Subscriptions
=============

The MCP 2025-06-18 spec adds ``resources/subscribe`` for change
notifications. v0.2 advertises ``subscribe: false`` in the
:class:`~meho_backplane.mcp.schemas.ServerCapabilities` envelope
(``meho_backplane.mcp.server._initialize``) so clients know not to
subscribe. Long-poll / SSE-based subscriptions are a v0.2.next concern;
the bookkeeping (per-row ``updated_at`` already lives on the
``documents`` table) is already in place for when that lands.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.kb.schemas import InvalidKbSlugError, validate_slug
from meho_backplane.kb.service import KbService
from meho_backplane.mcp.registry import (
    ResourceTemplateDefinition,
    register_mcp_resource,
)
from meho_backplane.mcp.server import McpInvalidParamsError


async def _kb_entry_handler(
    operator: Operator,
    bound: dict[str, str],
) -> dict[str, Any]:
    """Return the full :class:`~meho_backplane.kb.schemas.KbEntry` for ``{slug}``.

    Three rejection arms, all mapped onto
    :class:`McpInvalidParamsError` (``-32602``) — JSON-RPC carries no
    HTTP-status semantics so every input-validation failure surfaces
    on the same error code:

    * **Malformed slug** — the URI bound a value that doesn't match
      :data:`~meho_backplane.kb.schemas.SLUG_PATTERN`. Caught from
      :func:`~meho_backplane.kb.schemas.validate_slug`'s
      :class:`InvalidKbSlugError` and surfaced before any DB query so
      a probe attempt with an arbitrary URI fragment can't be used as
      an oracle for what slugs exist.
    * **Entry not found in the operator's tenant** — either the slug
      doesn't exist at all, or it exists only under a different
      tenant. The handler does not distinguish the two cases in its
      error message; both collapse to "kb entry not found", which is
      the spec-correct shape for a tenant-boundary breach (don't
      leak the foreign tenant's existence). See module docstring's
      "Tenant scoping" section.
    * **(implicit) Missing slug binding** — the URI matcher in the
      registry only invokes this handler when ``{slug}`` has been
      bound, so this arm is structurally unreachable. Defensive
      ``bound["slug"]`` access surfaces a KeyError → -32603 if the
      matcher ever changes shape, which is the right loud failure for
      a registry bug.
    """
    slug = bound["slug"]
    try:
        validate_slug(slug)
    except InvalidKbSlugError as exc:
        raise McpInvalidParamsError(f"kb entry: invalid slug {slug!r}: {exc}") from exc

    service = KbService()
    entry = await service.get_entry(tenant_id=operator.tenant_id, slug=slug)
    if entry is None:
        raise McpInvalidParamsError(f"kb entry not found: {slug!r}")

    return entry.model_dump(mode="json")


register_mcp_resource(
    definition=ResourceTemplateDefinition(
        uriTemplate="meho://kb/{slug}",
        name="Knowledge base entry",
        description=(
            "Full body and metadata of one kb entry, identified by slug. "
            "Use after `search_knowledge` has returned a hit whose "
            "200-char snippet is not enough to act on — this resource "
            "returns the entire Markdown body plus provenance metadata. "
            "Returns INVALID_PARAMS for a malformed slug or for a slug "
            "that doesn't exist under the operator's tenant (cross-"
            "tenant reads collapse to 'not found' without revealing the "
            "foreign tenant's existence)."
        ),
        mimeType="text/markdown",
        required_role=TenantRole.OPERATOR,
    ),
    handler=_kb_entry_handler,
)
