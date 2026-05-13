# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/connectors/{product}/{op_id}`` — generic connector dispatch route.

End-to-end acceptance surface for G0.2 (Initiative #223, Task #245).

The route resolves the product slug to a registered connector class, builds
a pre-G0.3 target stub from the operator's JWT, and delegates to
``connector.execute(target, op_id, params)``. The entire authentication +
registry + connector chain is exercised on every call.

**Target resolution (pre-G0.3):** ``_TargetStub`` carries only ``raw_jwt``.
Once G0.3 (#224) lands, replace ``_build_target`` with a call to
``resolve_target(operator.tenant_id, request.target)`` that returns a real
``Target`` row from the DB. The connector's duck-type contract (any object
with ``raw_jwt`` works) is forward-compatible; no rework needed in T6 once
G0.3 lands.

**HTTP contract:**

- ``404`` — unknown product (not in registry).
- ``400`` — unknown op-id (the connector returned ``unknown_op`` in the result).
- ``401`` — unauthenticated or expired JWT (raised by ``verify_jwt_and_bind``).
- ``200`` — all other outcomes, including connector-side errors (connection
  failure, read failure, param-validation errors). Callers inspect
  ``OperationResult.status`` to distinguish success from errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import get_connector
from meho_backplane.middleware import verify_jwt_and_bind

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])


class ConnectorExecRequest(BaseModel):
    """Request body for connector operation dispatch."""

    target: str
    params: dict[str, Any] = {}


@dataclass(frozen=True)
class _TargetStub:
    """Pre-G0.3 target stand-in. Duck-types as VaultTarget via ``raw_jwt``.

    Replace with ``resolve_target(tenant_id, slug)`` from G0.3 once it lands.
    Any connector whose execute() reads ``target.raw_jwt`` works with this stub.
    """

    raw_jwt: str | None = None


def _build_target(operator: Operator, _target_slug: str) -> _TargetStub:
    """Resolve a target slug to a usable target object.

    Pre-G0.3 stub: ignores the slug and returns a stub carrying the operator's
    JWT so VaultConnector's OIDC-forward path works. G0.3-T3 replaces this with
    a real DB lookup against the targets table.
    """
    return _TargetStub(raw_jwt=operator.raw_jwt)


@router.post("/{product}/{op_id}")
async def execute_op(
    product: str,
    op_id: str,
    request: ConnectorExecRequest,
    operator: Operator = Depends(verify_jwt_and_bind),
) -> dict[str, Any]:
    """Dispatch an operation to the named connector.

    Looks up the connector by product slug, builds a pre-G0.3 target stub,
    and delegates to ``connector.execute(target, op_id, params)``. Returns
    the ``OperationResult`` as JSON on success. Returns 404 for an unknown
    product and 400 for an unknown op-id (with the known ops list in the
    error body so operators can enumerate what the connector supports).
    """
    log = _log.bind(product=product, op_id=op_id, target=request.target)

    cls = get_connector(product)
    if cls is None:
        log.warning("connector_not_found", product=product)
        raise HTTPException(status_code=404, detail=f"unknown product: {product}")

    target = _build_target(operator, request.target)
    connector = cls()
    # Connectors use fully-qualified op ids (e.g. "vault.kv.read").
    # The URL path splits that into {product}/{op_id}, so we re-join here.
    qualified_op_id = f"{product}.{op_id}"
    result = await connector.execute(target, qualified_op_id, request.params)

    unknown_op = result.status == "error" and (result.error or "").startswith("unknown_op")
    if unknown_op:
        known_ops = list(result.extras.get("known_ops", []))
        log.warning("connector_unknown_op", op_id=qualified_op_id, known_ops=known_ops)
        raise HTTPException(
            status_code=400,
            detail={"error": "unknown_op", "op_id": qualified_op_id, "known_ops": known_ops},
        )

    log.info(
        "connector_op_dispatched",
        status=result.status,
        duration_ms=result.duration_ms,
    )
    return result.model_dump()
