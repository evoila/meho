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
    OperationDescriptor,
    UnknownConnectorError,
    call_operation,
    describe_descriptor,
    list_operation_groups,
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


@router.get("/groups")
async def get_groups(
    connector_id: str = Query(
        min_length=1,
        description=_CONNECTOR_ID_DESCRIPTION,
        openapi_examples=_CONNECTOR_ID_EXAMPLES,
    ),
    operator: Operator = _require_operator,
) -> dict[str, Any]:
    """List enabled operation groups for *connector_id*.

    Delegates to :func:`list_operation_groups`. An *unknown*
    ``connector_id`` (no operations registered for the parsed triple)
    is a ``404`` — not an empty ``200``: the empty-catalog trap was
    that a mis-shaped id looked identical to an empty connector. A
    *known* connector with zero enabled groups still returns
    ``{"groups": []}`` (that empty is operationally meaningful).
    """
    try:
        return await list_operation_groups(operator, {"connector_id": connector_id})
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
    full algorithm and tenant scoping. Unknown ``connector_id`` → ``404``
    (same unknown-vs-known-empty contract as ``/groups``); a known
    connector with no matching ops returns ``200`` with an empty list.
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
