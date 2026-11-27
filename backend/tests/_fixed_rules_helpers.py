# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared helpers for the whole-path tests of the fixed rules at the call gate.

A made-up connector ``rulebox-1.x``, typed operations of it, one target,
and callers of each kind that call over ``POST /api/v1/operations/call``
with a signed token, so the real gate decides and the real audit rows are
written. The settings, the stand-in connector, the run approvals and the
audit reads come from :mod:`tests._hard_to_undo_helpers`.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import register_typed_operation
from tests._hard_to_undo_helpers import TENANT
from tests._oidc_jwt_helpers import mint_token

CONNECTOR = "rulebox-1.x"
TARGET = "rule-box-01"
PRODUCT = {"product": "rulebox", "version": "1.x", "impl_id": "rulebox"}

#: Every run of a stand-in operation, so a test can prove what ran.
RUNS: list[str] = []


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del target, params
    RUNS.append("ran")
    return {"done": True}


async def register(
    op_id: str,
    *,
    safety_level: str = "caution",
    requires_approval: bool = True,
    changes_rights: bool = False,
) -> None:
    """A typed operation of the made-up connector, with no delete name, verb or tag."""
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_typed_operation(
        **PRODUCT,
        op_id=op_id,
        handler=_handler,
        summary="Change a box.",
        description="Changes one box of a made-up product.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level=safety_level,  # type: ignore[arg-type]
        requires_approval=requires_approval,
        changes_rights=changes_rights,
        embedding_service=embedding,
    )


async def call_as(
    client: tuple[httpx.AsyncClient, Any],
    op_id: str,
    *,
    kind: str,
    sub: str,
    work_ref: str | None = None,
) -> dict[str, Any]:
    """A caller of *kind* (``user``, ``agent``, ``service``) calls *op_id* over REST."""
    ac, key = client
    extra = {"client_id": f"{sub}-client"} if kind == "service" else None
    token = mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(TENANT),
        principal_kind=kind,
        extra_claims=extra,
    )
    body: dict[str, Any] = {"connector_id": CONNECTOR, "op_id": op_id, "target": TARGET}
    body["params"] = {}
    if work_ref is not None:
        body["work_ref"] = work_ref
    response = await ac.post(
        "/api/v1/operations/call", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def call_rows(op_id: str) -> list[AuditLog]:
    """The audit rows of the calls of *op_id* (not the approval rows)."""
    async with get_sessionmaker()() as session:
        rows = await session.scalars(select(AuditLog).where(AuditLog.path == op_id))
        return list(rows.all())


def new_id() -> str:
    return str(uuid.uuid4())
