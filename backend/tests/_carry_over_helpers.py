# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared set-up of the tests of the "always wait" carry-over dry run.

A connector ``cowapi-1.x`` with ``cowapi.vm.list`` (``safe``, group
``compute``), ``cowapi.vm.update`` (``caution``, group ``compute``) and
``cowapi.disk.list`` (``safe``, group ``storage``); tenant A (the tokens'
default tenant) and tenant B. All names and values are made up.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    OperationPolicyOverride,
    TenantPolicy,
)
from meho_backplane.operations import governed_subops
from meho_backplane.operations.governed_subops import GovernedSubopSurface
from meho_backplane.operations.typed_register import register_typed_operation

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

TENANT = uuid.UUID(DEFAULT_TENANT_ID)
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000b0b0b")
CONNECTOR = "cowapi-1.x"
ROUTE = "/api/v1/policy/carry-over/dry-run"
EXPORT_ROUTE = "/api/v1/policy/export"
CALL_ROUTE = "/api/v1/operations/call"
AUDIT_OP = "policy.carry_over.dry_run"
PREVIEW = "As `destructive`, a call needs a matching preview first. A call without one is refused."
SANDBOX = (
    "As `destructive`, a delete grant can cover the operation for a service login, "
    "because this install is in sandbox mode."
)
NO_OPERATION = "As `destructive`, this entry changes no operation: it matches none."


def step_text(op_id: str, *composites: str) -> str:
    names = ", ".join(f"`{c}`" for c in composites)
    return (
        "As `destructive`, a step of a multi-step operation is refused, not held for "
        f"approval. `{op_id}` is a step of {names}, so such a run stops at this step."
    )


def add_steps(
    monkeypatch: pytest.MonkeyPatch, composite: str, connector_id: str, *steps: str
) -> None:
    """Declare *steps* as the step list of the multi-step operation *composite*."""
    monkeypatch.setitem(
        governed_subops._REGISTRY,
        composite,
        GovernedSubopSurface(connector_id=connector_id, sub_op_ids=steps),
    )


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def register(
    embedding: AsyncMock, op_id: str, group: str, level: Any, *, product: str = "cowapi"
) -> None:
    await register_typed_operation(
        product=product,
        version="1.x",
        impl_id=product,
        op_id=op_id,
        handler=_handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema={"type": "object", "properties": {}},
        group_key=group,
        when_to_use="Fixture group.",
        safety_level=level,
        embedding_service=embedding,
    )


async def tenant_row(op_id: str, tenant_id: uuid.UUID, *, level: str = "safe") -> None:
    """A row of the tenant's own copy of ``cowapi-1.x``, in no group.

    It runs the handler of the built-in row of the same operation, when one exists.
    """
    async with get_sessionmaker()() as session:
        built_in = select(EndpointDescriptor.handler_ref).where(
            EndpointDescriptor.tenant_id.is_(None),
            EndpointDescriptor.product == "cowapi",
            EndpointDescriptor.op_id == op_id,
        )
        handler_ref = (await session.execute(built_in)).scalar_one_or_none()
        session.add(
            EndpointDescriptor(
                tenant_id=tenant_id,
                product="cowapi",
                version="1.x",
                impl_id="cowapi",
                op_id=op_id,
                source_kind="typed",
                handler_ref=handler_ref,
                safety_level=level,
                requires_approval=False,
                parameter_schema={"type": "object", "properties": {}},
            )
        )
        await session.commit()


async def override(
    op_id: str,
    tenant_id: uuid.UUID | None,
    *,
    approval: bool | None,
    product: str = "cowapi",
) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            OperationPolicyOverride(
                tenant_id=tenant_id,
                product=product,
                version="1.x",
                impl_id=product,
                op_id=op_id,
                requires_approval=approval,
                category=None if approval is not None else "compute",
                set_by_sub="admin-a",
            )
        )
        await session.commit()


async def policy(tenant_id: uuid.UUID | None, classifications: list[dict[str, Any]]) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TenantPolicy(
                tenant_id=tenant_id,
                document={"classifications": classifications},
                set_by_sub="admin-a",
            )
        )
        await session.commit()


async def setup(embedding: AsyncMock) -> None:
    """Three operations, overrides and policy files in three scopes."""
    await register(embedding, "cowapi.vm.list", "compute", "safe")
    await register(embedding, "cowapi.vm.update", "compute", "caution")
    await register(embedding, "cowapi.disk.list", "storage", "safe")
    await override("cowapi.vm.update", TENANT, approval=True)
    await override("cowapi.disk.list", TENANT, approval=None)
    await override("cowapi.vm.list", None, approval=True)
    await override("cowapi.disk.list", OTHER_TENANT, approval=True)
    await policy(
        TENANT,
        [
            {"match": {"connector": "cowapi-*", "group": "compute"}, "always_wait": True},
            {"match": {"op": "cowapi.vm.list"}, "always_wait": False},
            {"match": {"op": "nothing.*"}, "always_wait": True},
            {"match": {"op": "cowapi.disk.list"}, "category": "storage"},
        ],
    )
    await policy(None, [{"match": {"group": "storage"}, "always_wait": True}])
    await policy(OTHER_TENANT, [{"match": {"op": "cowapi.*"}, "always_wait": True}])


def token(key: Any, *, sub: str = "admin-a", tenant: uuid.UUID = TENANT, **claims: Any) -> str:
    claims.setdefault("tenant_role", TenantRole.TENANT_ADMIN.value)
    return mint_token(key, sub=sub, tenant_id=str(tenant), **claims)


def auth(bearer: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}"}


def get(client: TestClient, key: Any, route: str, scope: str | None = None, **claims: Any) -> Any:
    params = {"scope": scope} if scope else None
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        return client.get(route, params=params, headers=auth(token(key, **claims)))


def dry_run(client: TestClient, key: Any, scope: str | None = None, **claims: Any) -> Any:
    return get(client, key, ROUTE, scope, **claims)


def call(client: TestClient, key: Any, op_id: str, **claims: Any) -> dict[str, Any]:
    """Call *op_id* of ``cowapi-1.x`` with a token (an Operator unless *claims* say else)."""
    claims.setdefault("sub", "op-a")
    claims.setdefault("tenant_role", TenantRole.OPERATOR.value)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            CALL_ROUTE,
            json={"connector_id": CONNECTOR, "op_id": op_id, "params": {}},
            headers=auth(token(key, **claims)),
        )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def op(op_id: str, *step_of: str, connector: str = CONNECTOR) -> dict[str, Any]:
    return {"connector_id": connector, "op_id": op_id, "step_of": list(step_of)}


async def audit_rows(path: str = ROUTE) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        stmt = select(AuditLog).where(AuditLog.path == path).order_by(AuditLog.occurred_at)
        return list((await session.execute(stmt)).scalars())
