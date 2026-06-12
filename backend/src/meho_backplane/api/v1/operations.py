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
* ``GET /api/v1/operations/search?connector_id=...&query=...&group=...&limit=...``
  -- hybrid retrieval. Operator role.
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


def _connector_not_ingested_404(exc: ConnectorNotIngestedError) -> HTTPException:
    """Map :class:`ConnectorNotIngestedError` to a structured ``404``.

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
        raise _connector_not_ingested_404(exc) from exc
    except UnknownConnectorError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/search")
async def get_search(
    connector_id: str = Query(
        min_length=1,
        description=_CONNECTOR_ID_DESCRIPTION,
        openapi_examples=_CONNECTOR_ID_EXAMPLES,
    ),
    query: str = Query(min_length=1),
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
    """
    try:
        return await search_operations(
            operator,
            {
                "connector_id": connector_id,
                "query": query,
                "group": group,
                "limit": limit,
            },
        )
    except ConnectorNotIngestedError as exc:
        raise _connector_not_ingested_404(exc) from exc
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
    envelope (``status='error'`` + ``error='<code>: …'``) rather than
    HTTP 4xx. The route returns 200 on a structured-error envelope so
    callers see the dispatcher's error_code in ``extras``.

    The one HTTP-side gate is the target resolution: a missing-target
    name (``{"target": {}}`` without ``"name"``) surfaces as a 400.
    """
    try:
        return await call_operation(operator, body.model_dump())
    except ValueError as exc:
        # Missing `target.name` is the only ValueError the meta-tool raises.
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    ``/call``. The one HTTP-side gate is target resolution: a missing-target
    name (``{"target": {}}`` without ``"name"``) surfaces as a ``400``. The
    body is redacted through the same connector-boundary pipeline the
    response path uses; nothing is written to the audit row.
    """
    try:
        return await preview_operation(operator, body.model_dump())
    except ValueError as exc:
        # Missing `target.name` is the only ValueError the meta-tool raises.
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
