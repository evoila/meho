# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho://retrieve/{query}`` — hybrid-retrieval MCP resource (G0.5-T9).

The MCP-resource face of the G0.4 retrieval substrate. The in-process
:func:`~meho_backplane.retrieval.retriever.retrieve` helper (G0.4-T4,
#261) and its HTTP surface ``POST /api/v1/retrieve`` (G0.4-T5, #262) are
already in place; this resource exposes the same hybrid BM25 + cosine
retrieval through the MCP resource registry so an agent can pull ranked
hits with a plain ``resources/read`` instead of a tool call.

URI template + variable binding
================================

``meho://retrieve/{query}`` follows the v0.2 simple-expansion subset of
RFC 6570 (one named variable, no operators). The MCP registry's URI
matcher binds ``{query}`` from the concrete URI as a single path segment
(``[^/]+``) and forwards it to the handler. The client percent-encodes
the free-form query into that segment; the handler
:func:`~urllib.parse.unquote`\\ s it back before retrieval, so a query
carrying spaces (``kubernetes%20ingress``) or a literal slash
(``a%2Fb``) round-trips losslessly. The tenant is **not** in the URI —
it comes purely from the operator's JWT (see "Tenant scoping").

Tenant scoping
==============

The handler reads :attr:`Operator.tenant_id` from the validated MCP-auth
operator and threads it into :func:`retrieve`; there is no URI variable
or any other surface that accepts a tenant id, so cross-tenant retrieval
is impossible by construction. The substrate filters every candidate
query by ``tenant_id``, so a query issued under tenant A can never
surface tenant B's documents — the same guarantee the HTTP route relies
on.

RBAC
====

``operator`` role minimum — identical to ``POST /api/v1/retrieve``.
``read_only`` operators are filtered out of
``resources/templates/list`` and rejected at ``resources/read`` time by
the dispatcher's call-time role re-check
(:func:`~meho_backplane.mcp.handlers.handle_resources_read`). Diagnostic
retrieval is an operator-level action; ``read_only`` agents use the
higher-level kb / memory search tools.

Audit privacy contract
======================

Retrieval queries leak operator intent, so — exactly like the HTTP
route — the audit trail must record *that* a retrieval happened and a
correlatable identity for the query, never the raw query text. The
handler binds the four ``audit_*`` contextvars the HTTP route introduced
(``audit_query_hash`` / ``audit_source`` / ``audit_kind`` /
``audit_hit_count``); :func:`~meho_backplane.mcp.audit.write_mcp_audit_row`
merges them into the ``audit_log.payload`` so the persisted row carries
``{query_hash, source, kind, hit_count}``.

Because the query is a *URI path segment* (not a JSON body field), the
generic ``resources/read`` audit path would otherwise persist the raw
query inside ``audit_log.path`` and ``payload.uri``. The resource
therefore opts into ``audit_redact_uri=True`` on its
:class:`~meho_backplane.mcp.registry.ResourceTemplateDefinition`, which
makes the dispatcher substitute a query-stripped sentinel
(``meho://retrieve/<redacted>``) for both the audit ``path`` and
``payload.uri``. The result is a row that is fully attributable (tenant,
operator, ``query_hash``, ``hit_count``) yet carries no recoverable
query string — matching the HTTP route's privacy posture.

Response shape
==============

The MCP ``resources/read`` response is a ``contents[]`` array. The
dispatcher wraps this handler's return value in a single text block
whose ``mimeType`` is the template's ``application/json`` and whose
``text`` is the JSON-serialised handler return. The handler returns
``{"hits": [...]}`` where each hit is a
:class:`~meho_backplane.retrieval.retriever.RetrievalHit` serialised via
``model_dump(mode="json")`` — the identical hit shape the HTTP route
returns under ``RetrieveResponse.hits``.
"""

from __future__ import annotations

import hashlib
from typing import Any, Final
from urllib.parse import unquote

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import (
    ResourceTemplateDefinition,
    register_mcp_resource,
)
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.retrieval.retriever import retrieve

#: Default hit ceiling for the resource read. The URI template carries no
#: ``limit`` variable (a resource read is a fixed-shape fetch, not a
#: parameterised query), so the resource pins the same default the HTTP
#: route's ``RetrieveRequest.limit`` uses (10). An agent that needs a
#: different ceiling uses the ``search_knowledge`` / ``search_memory``
#: meta-tools, which take an explicit ``limit`` argument.
_DEFAULT_LIMIT: Final[int] = 10


def _compute_query_hash(query: str) -> str:
    """SHA-256 hex digest of *query* (UTF-8 encoded).

    Matches :func:`meho_backplane.api.v1.retrieve._compute_query_hash`
    byte-for-byte so an analyst correlating a known query against an
    ``audit_log.payload.query_hash`` gets the same digest whether the
    retrieval came through the HTTP route or this resource.
    """
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


async def _retrieve_handler(
    operator: Operator,
    bound: dict[str, str],
) -> dict[str, Any]:
    """Run hybrid retrieval for the URI-bound query, tenant-scoped to the JWT.

    The ``{query}`` segment is percent-decoded before retrieval. The four
    ``audit_*`` contextvars are bound **before** the retrieval call so a
    handler exception still leaves an audit row carrying the query
    identity (``audit_hit_count`` absent in that case — the partial
    signal is itself useful for postmortem), mirroring the HTTP route.

    One rejection arm: a ``{query}`` that decodes to an empty/whitespace
    string surfaces as :class:`McpInvalidParamsError` (``-32602``) before
    the embedding pipeline runs — the same gate the HTTP route's
    ``Field(min_length=1)`` enforces, kept here because the resource has
    no Pydantic request body to lean on.
    """
    query = unquote(bound["query"])
    if not query.strip():
        raise McpInvalidParamsError(
            "retrieve: empty query — the URI's {query} segment must decode to a non-empty string",
        )

    log = structlog.get_logger()
    # Bind the privacy-preserving query identity up front so a handler
    # exception still produces an audit row with the query hash. Only the
    # SHA-256 hash is ever bound — never the raw query. ``source`` / ``kind``
    # are None for the resource (it retrieves across every source); the
    # audit-payload resolver drops None-valued contextvars.
    structlog.contextvars.bind_contextvars(
        audit_query_hash=_compute_query_hash(query),
        audit_source=None,
        audit_kind=None,
    )

    # `principal_sub=operator.sub` enforces the mandatory per-principal
    # isolation predicate at the retrieval boundary (#1797). This resource
    # retrieves across *every* source with no `metadata_filters`, so
    # without the predicate a `source="memory"` user-scoped row written by
    # another principal in the same tenant would surface here. The
    # substrate gates `memory-user` / `memory-user-tenant` /
    # `memory-user-target` rows on `stored user_sub == operator.sub`;
    # tenant-broadcast and non-memory rows are unaffected.
    hits = await retrieve(
        tenant_id=operator.tenant_id,
        query=query,
        limit=_DEFAULT_LIMIT,
        principal_sub=operator.sub,
    )

    structlog.contextvars.bind_contextvars(audit_hit_count=len(hits))
    log.info(
        "mcp_retrieve_resource_completed",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        hit_count=len(hits),
    )
    return {"hits": [hit.model_dump(mode="json") for hit in hits]}


register_mcp_resource(
    definition=ResourceTemplateDefinition(
        uriTemplate="meho://retrieve/{query}",
        name="Hybrid retrieval",
        description=(
            "Hybrid BM25 + cosine retrieval over the operator's tenant "
            "corpus, ranked by Reciprocal Rank Fusion. Percent-encode the "
            "free-form query into the {query} segment; the result is the "
            "same ranked RetrievalHit list `POST /api/v1/retrieve` returns "
            "(per-signal scores + ranks carried for observability). The "
            "tenant is taken from the JWT — there is no cross-tenant "
            "retrieval surface. Requires the `operator` role."
        ),
        mimeType="application/json",
        required_role=TenantRole.OPERATOR,
        audit_redact_uri=True,
    ),
    handler=_retrieve_handler,
)
