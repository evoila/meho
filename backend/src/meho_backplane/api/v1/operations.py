# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/operations/*`` -- REST surface mirroring the operation meta-tools.

G0.6-T8 (#399) of Initiative #388. Four routes mounted at
``/api/v1/operations/*`` mirroring the three agent meta-tools defined in
:mod:`meho_backplane.operations.meta_tools` plus a descriptor-inspection
diagnostic. The CLI verbs ``meho operation groups`` / ``meho operation
search`` / ``meho operation call`` and the operator UI consume this
surface; the MCP tool registrations
(:mod:`meho_backplane.mcp.tools.operations`) wrap the same handlers for
the agent transport.

* ``GET /api/v1/operations/groups?connector_id=...`` -- list enabled groups.
  Operator role.
* ``GET /api/v1/operations/search?connector_id=...&q=...&group=...&limit=...``
  -- hybrid retrieval. ``q`` is the canonical free-text query param
  (``query`` is the deprecated alias, #1854). Operator role.
* ``POST /api/v1/operations/call`` (body: ``CallOperationBody``) --
  invoke the dispatcher. Operator role.
* ``GET /api/v1/operations/{descriptor_id}`` -- inspect a single
  descriptor row including ``llm_instructions``. Tenant-admin role
  because ``llm_instructions`` is the per-op agent prompt (leaking it
  to a read-only operator amounts to a prompt-injection vector).

Tenant scoping is applied inside the meta-tool handlers, which read
``operator.tenant_id`` from the JWT-validated :class:`Operator`. The
route layer is a thin Pydantic + Depends wrapper.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.openapi.models import Example

from meho_backplane.api.v1._freetext_filter import (
    FREE_TEXT_Q_DESCRIPTION,
    resolve_free_text_filter,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.operations.meta_tools import (
    CallOperationBody,
    ConnectorNotIngestedError,
    OperationDescriptor,
    PreviewOperationBody,
    UnknownConnectorError,
    call_operation,
    describe_descriptor,
    list_operation_groups,
    preview_operation,
    search_operations,
)

#: Shared OpenAPI metadata for the ``connector_id`` query param on the
#: ``/groups`` and ``/search`` routes. The format is ``<impl_id>-<version>``
#: (NOT the bare product slug) — documented inline so the generated
#: ``/openapi.json`` the agent and operators read carries the contract,
#: rather than it living only in ``docs/cross-repo/connector-ingestion.md``.
_CONNECTOR_ID_DESCRIPTION = (
    "Connector implementation id in `<impl_id>-<version>` form — e.g. "
    "`vmware-rest-9.0`, `vault-1.x`, `k8s-1.x`. NOT the bare product "
    "name (`vault`, `vmware`): a bare product slug names no connector "
    "and returns 404. Discover valid ids via `GET /api/v1/connectors`."
)
_CONNECTOR_ID_EXAMPLES: dict[str, Example] = {
    "vmware_rest": Example(
        summary="vCenter REST (generic, ingested)",
        value="vmware-rest-9.0",
    ),
    "vault": Example(
        summary="HashiCorp Vault (typed)",
        value="vault-1.x",
    ),
    "k8s": Example(
        summary="Kubernetes (typed)",
        value="k8s-1.x",
    ),
}

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])

#: Module-level Depends closures -- required to satisfy ruff B008 (mutable
#: calls in default argument positions are disallowed). Same pattern as
#: :mod:`meho_backplane.api.v1.targets` and
#: :mod:`meho_backplane.api.v1.retrieve`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))


def connector_not_ingested_404(exc: ConnectorNotIngestedError) -> HTTPException:
    """Map :class:`ConnectorNotIngestedError` to a structured ``404``.

    The **shared** mapper for the registered-but-not-ingested 404 shape:
    ``GET /operations/groups`` (here) and ``GET /connectors/{id}/review``
    (#137) both route through it, so the two surfaces emit byte-identical
    ``{message, reason, connector_id, next_step}`` and cannot drift.

    Both the registered-but-not-ingested case and the genuinely-unknown
    case are ``404`` on REST (no resolvable connector to dispatch), but
    they stay distinguishable: this one carries a structured ``detail``
    with ``reason="connector_not_ingested"`` and the ``meho connector
    ingest …`` ``next_step`` hint, mirroring the ``state="registered"``
    row ``GET /api/v1/connectors`` already emits (#1482). An *unknown*
    connector keeps its plain-string ``detail`` (the long-form mistyped-id
    recovery hint).
    """
    return HTTPException(
        status_code=404,
        detail={
            "message": str(exc),
            "reason": "connector_not_ingested",
            "connector_id": exc.connector_id,
            "next_step": exc.next_step,
        },
    )


@router.get("/groups")
async def get_groups(
    connector_id: str = Query(
        min_length=1,
        description=_CONNECTOR_ID_DESCRIPTION,
        openapi_examples=_CONNECTOR_ID_EXAMPLES,
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=500,
        description=(
            "Page size. Default 100; max 500. Matches `list_targets` "
            "paging — sibling list surfaces share one ceiling "
            "(G0.18-T5 #1358)."
        ),
    ),
    cursor: str | None = Query(
        default=None,
        max_length=256,
        description=(
            "Keyset-pagination cursor: pass the last `group_key` from "
            "the previous page to fetch the next. Results are ordered "
            "by `group_key` ascending. A `null` `next_cursor` in the "
            "response means this page is the end of the listing "
            "(G0.18-T5 #1358)."
        ),
    ),
    operator: Operator = _require_operator,
) -> dict[str, Any]:
    """List enabled operation groups for *connector_id*.

    Delegates to :func:`list_operation_groups`. An *unknown*
    ``connector_id`` (no operations registered for the parsed triple)
    is a ``404`` — not an empty ``200``: the empty-catalog trap was
    that a mis-shaped id looked identical to an empty connector. A
    *registered-but-not-ingested* connector (v2-registered class, zero
    DB rows) is also a ``404`` but with a structured ``detail``
    carrying ``reason="connector_not_ingested"`` and the ``meho
    connector ingest …`` ``next_step`` hint, distinct from the
    unknown-connector ``404`` (#1482). A *known* connector with zero
    enabled groups still returns ``{"groups": [], "next_cursor": null}``
    (that empty is operationally meaningful).

    Pagination is keyset on ``group_key`` (G0.18-T5 #1358); the
    response carries ``next_cursor`` set to the last returned
    ``group_key`` when a page is full, ``null`` otherwise.
    """
    try:
        return await list_operation_groups(
            operator,
            {"connector_id": connector_id, "limit": limit, "cursor": cursor},
        )
    except ConnectorNotIngestedError as exc:
        raise connector_not_ingested_404(exc) from exc
    except UnknownConnectorError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/search")
async def get_search(
    connector_id: str = Query(
        min_length=1,
        description=_CONNECTOR_ID_DESCRIPTION,
        openapi_examples=_CONNECTOR_ID_EXAMPLES,
    ),
    q: str | None = Query(
        default=None,
        min_length=1,
        description=FREE_TEXT_Q_DESCRIPTION,
    ),
    query: str | None = Query(
        default=None,
        min_length=1,
        deprecated=True,
        description="Deprecated alias for `q`; still honoured.",
    ),
    group: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    operator: Operator = _require_operator,
) -> dict[str, Any]:
    """Hybrid BM25 + cosine RRF over ``endpoint_descriptor`` rows.

    Delegates to :func:`search_operations`. See its docstring for the
    full algorithm and tenant scoping. Unknown ``connector_id`` → ``404``;
    a registered-but-not-ingested connector → ``404`` with the typed
    ``connector_not_ingested`` ``detail`` (same contract as ``/groups``,
    #1482); a known connector with no matching ops returns ``200`` with
    an empty list.

    ``q`` is the canonical free-text query param across the kb / memory /
    operations-search list surfaces (#1854); ``query`` is its deprecated
    alias, kept working for back-compat. Exactly one of the two is
    required: supplying neither is a ``422`` (the search has nothing to
    match), and supplying both with different values is a ``422`` rather
    than a silent pick.
    """
    search_query = resolve_free_text_filter(
        q=q,
        legacy_value=query,
        legacy_name="query",
    )
    if search_query is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "missing_query: provide a free-text query via 'q' "
                "('query' is the deprecated alias)."
            ),
        )
    try:
        return await search_operations(
            operator,
            {
                "connector_id": connector_id,
                "query": search_query,
                "group": group,
                "limit": limit,
            },
        )
    except ConnectorNotIngestedError as exc:
        raise connector_not_ingested_404(exc) from exc
    except UnknownConnectorError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/call")
async def post_call(
    body: CallOperationBody,
    operator: Operator = _require_operator,
) -> dict[str, Any]:
    """Invoke :func:`~meho_backplane.operations.dispatch` for an op id.

    Delegates to :func:`call_operation`. The dispatcher contract is
    "always return a structured result"; errors land in the result
    envelope (``status='error'`` + ``extras.error_code``) rather than
    HTTP 4xx. The route returns 200 on a structured-error envelope so
    callers see the dispatcher's error_code in ``extras``.

    Target-failure contract (#136, completed by #2110 Option A): **every**
    target-failure mode returns HTTP 200 + the dispatcher envelope — a
    missing / empty / ``name``-less target is
    ``extras.error_code="target_required"``, a ``target`` of a wrong JSON
    type (e.g. ``target: 12345``) is
    ``extras.error_code="target_invalid_type"`` (carrying
    ``extras.received_type``), a supplied name that resolves to no live
    target is ``extras.error_code="no_target"``, and a name matching more
    than one target (alias collision) is
    ``extras.error_code="ambiguous_target"`` — the resolution codes carry
    the candidate ``matches``. No target-failure mode returns a 4xx: a
    consumer's error handling is a single switch on ``extras.error_code``.
    """
    return await call_operation(operator, body.model_dump())


@router.post("/preview")
async def post_preview(
    body: PreviewOperationBody,
    operator: Operator = _require_operator,
) -> dict[str, Any]:
    """Resolve an op + params to the literal would-be HTTP request, without sending.

    Delegates to :func:`preview_operation` (#1683). The read-only diagnosis
    sibling of ``POST /api/v1/operations/call``: it resolves the same op +
    target + params and returns the literal request
    (``{method, resolved_path, query, redacted_body}``) for an
    ``source_kind='ingested'`` op **instead of dispatching it**. Use it to
    diagnose a write 4xx from the inside -- the audit row persists only a
    hashed ``params_hash``, so the wire shape is otherwise unrecoverable.

    Returns ``200`` with the structured envelope; operator-input faults
    (unknown op, invalid params, unresolvable connector) land inside the
    envelope (``status="error"`` / ``status="unavailable"`` +
    ``extras.error_code``) rather than as HTTP 4xx, the same contract as
    ``/call``. Target-failure outcomes ride the envelope identically to
    ``/call`` (#136, completed by #2110): ``target_required`` for a missing /
    empty / ``name``-less target, ``target_invalid_type`` for a ``target`` of
    a wrong JSON type, ``no_target`` for a supplied-but-unresolvable name,
    ``ambiguous_target`` for a name matching more than one target — all HTTP
    200, no target-failure 4xx remains. The body is redacted
    through the same connector-boundary pipeline the response path uses;
    nothing is written to the audit row.
    """
    return await preview_operation(operator, body.model_dump())


@router.get("/{descriptor_id}", response_model=OperationDescriptor)
async def get_descriptor(
    descriptor_id: uuid.UUID,
    operator: Operator = _require_admin,
) -> OperationDescriptor:
    """Inspect a single :class:`EndpointDescriptor` row by id.

    Gated on ``tenant_admin`` because ``llm_instructions`` is the
    per-op agent prompt. Returns 404 when the row doesn't exist or
    belongs to a different tenant (the two cases are deliberately
    indistinguishable; see :func:`describe_descriptor`).
    """
    descriptor = await describe_descriptor(operator, descriptor_id)
    if descriptor is None:
        raise HTTPException(status_code=404, detail=f"descriptor {descriptor_id} not found")
    return descriptor
