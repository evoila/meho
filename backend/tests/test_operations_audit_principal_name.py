# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The DISPATCH audit row carries the caller's name (#4029).

``write_audit_row`` puts ``Operator.name`` into ``payload["principal_name"]``,
the same key the MCP wrapper rows and the approval rows use. The audit query
reads that key, so ``meho audit show`` / ``meho audit query`` show the name.

Two paths are pinned against a real database:

* a direct call -- the caller's name lands on the row;
* an approved run (``dispatch(..., _approved=True)``, the path the approval
  resume takes) -- the approver's name lands on the row, next to the
  approver's ``operator_sub``.

A token without a name leaves the key out, so the reader returns ``None``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, Literal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.audit_query import AuditQueryFilters, query_audit
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-00000000a0a0")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


class _NoOpConnector(Connector):
    product = "demo"
    version = "1.x"
    impl_id = "demo"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> Any:
        raise NotImplementedError


async def _module_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    return {"done": True}


class _FakeFingerprint:
    version: str | None = None


class _FakeTarget:
    def __init__(self) -> None:
        self.product = "demo"
        self.fingerprint = _FakeFingerprint()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT
        self.name = "demo-target"
        self.host = "demo.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


def _operator(sub: str, name: str | None) -> Operator:
    return Operator(
        sub=sub,
        name=name,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _register_op(
    op_id: str, *, safety_level: Literal["safe", "dangerous"], embedding: AsyncMock
) -> None:
    register_connector_v2(product="demo", version="", impl_id="", cls=_NoOpConnector)
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id=op_id,
        handler=_module_handler,
        summary="Demo op.",
        description="Demo op used by the principal-name audit tests.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level=safety_level,
    )


async def _dispatch_row(op_id: str) -> AuditLog:
    async with get_sessionmaker()() as session:
        query = select(AuditLog).where(AuditLog.path == op_id, AuditLog.method == "DISPATCH")
        rows = (await session.execute(query)).scalars().all()
    assert len(rows) == 1
    return rows[0]


async def _queried_principal_name(audit_id: UUID) -> str | None:
    async with get_sessionmaker()() as session:
        result = await query_audit(AuditQueryFilters(), tenant_id=_TENANT, session=session)
    (entry,) = [e for e in result.rows if e.id == audit_id]
    return entry.principal_name


@pytest.mark.asyncio
async def test_direct_call_row_carries_caller_name(stub_embedding_service: AsyncMock) -> None:
    await _register_op("demo.thing.get", safety_level="safe", embedding=stub_embedding_service)

    result = await dispatch(
        operator=_operator("user-alice", "Alice Example"),
        connector_id="demo-1.x",
        op_id="demo.thing.get",
        target=_FakeTarget(),
        params={},
    )

    assert result.status == "ok"
    row = await _dispatch_row("demo.thing.get")
    assert row.operator_sub == "user-alice"
    assert row.payload["principal_name"] == "Alice Example"
    assert await _queried_principal_name(row.id) == "Alice Example"


@pytest.mark.asyncio
async def test_approved_run_row_carries_approver_name(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register_op(
        "demo.thing.delete", safety_level="dangerous", embedding=stub_embedding_service
    )

    result = await dispatch(
        operator=_operator("user-bob-approver", "Bob Approver"),
        connector_id="demo-1.x",
        op_id="demo.thing.delete",
        target=_FakeTarget(),
        params={},
        _approved=True,
    )

    assert result.status == "ok"
    row = await _dispatch_row("demo.thing.delete")
    assert row.operator_sub == "user-bob-approver"
    assert row.payload["principal_name"] == "Bob Approver"
    assert await _queried_principal_name(row.id) == "Bob Approver"


@pytest.mark.asyncio
async def test_token_without_name_leaves_key_out(stub_embedding_service: AsyncMock) -> None:
    await _register_op("demo.thing.get", safety_level="safe", embedding=stub_embedding_service)

    result = await dispatch(
        operator=_operator("user-noname", None),
        connector_id="demo-1.x",
        op_id="demo.thing.get",
        target=_FakeTarget(),
        params={},
    )

    assert result.status == "ok"
    row = await _dispatch_row("demo.thing.get")
    assert "principal_name" not in row.payload
    assert await _queried_principal_name(row.id) is None
