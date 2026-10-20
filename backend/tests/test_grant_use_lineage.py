# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Lineage ids on the standing-grant auto-approval row (#3695).

When a standing grant clears the gate, ``service_grants._record_grant_use``
writes an ``approval.decision`` / ``auto-approved`` audit row. Before #3695
that row had ``request_id``, ``agent_session_id`` and ``parent_audit_id`` all
NULL, so nothing linked it to the call it cleared. These tests check:

* the row carries the request id, the MCP session id and the composite
  parent's audit id when they are bound;
* the row leaves them NULL when nothing is bound (a plain call outside a
  request or session);
* the call's ``DISPATCH`` row carries the same ``request_id``, so the
  auto-approval links forward to the call it cleared.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy import select

import meho_backplane.broadcast.publisher as _publisher
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, ServicePrincipalGrant
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._audit import parent_audit_id_var
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000003695")
_PRINCIPAL = "svc:deploy-bot"
_COMPOSITE_CONNECTOR = "vmware-rest-9.0"
_COMPOSITE_OP = "vmware.composite.vm.create"
_STUB_CONNECTOR = "stub-1.x"
_STUB_OP = "stub.deploy"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _noop_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*_a: object, **_kw: object) -> None:
        pass

    monkeypatch.setattr(_publisher, "publish_event", _noop)


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    clear_registry()
    reset_dispatcher_caches()
    yield
    structlog.contextvars.clear_contextvars()
    clear_registry()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


def _service_operator() -> Operator:
    return Operator(
        sub=_PRINCIPAL,
        name="svc",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


async def _seed_grant(*, op_id: str, connector_id: str) -> uuid.UUID:
    grant_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            ServicePrincipalGrant(
                id=grant_id,
                tenant_id=_TENANT_ID,
                principal_sub=_PRINCIPAL,
                op_id=op_id,
                connector_id=connector_id,
                target_id=None,
                reason="unattended build",
                created_by_sub="op-admin",
            )
        )
        await s.commit()
    return grant_id


async def _grant_use_row(grant_id: uuid.UUID) -> AuditLog:
    async with get_sessionmaker()() as s:
        rows = (
            await s.scalars(
                select(AuditLog).where(
                    AuditLog.method == "APPROVAL",
                    AuditLog.path == "approval.decision",
                )
            )
        ).all()
    matches = [r for r in rows if r.payload.get("grant_id") == str(grant_id)]
    assert len(matches) == 1
    return matches[0]


class _StubConnector(Connector):
    """Minimal connector so the dispatcher resolver finds a class for the stub op."""

    product = "stub"
    version = "1.x"
    impl_id = "stub"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


async def _stub_deploy_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    return {"deployed": True}


async def _clear_composite_gate() -> None:
    """Run the composite child gate the way a composite handler does."""
    result = await enforce_subop_policy(
        operator=_service_operator(),
        connector_id=_COMPOSITE_CONNECTOR,
        op_id=_COMPOSITE_OP,
        safety_level="caution",
        requires_approval=False,
        target=None,
        params={"name": "vm-under-test"},
    )
    assert result is None  # the grant cleared the gate


@pytest.mark.asyncio
async def test_grant_use_row_carries_request_session_and_parent_ids() -> None:
    grant_id = await _seed_grant(op_id=_COMPOSITE_OP, connector_id=_COMPOSITE_CONNECTOR)
    request_id = uuid.uuid4()
    session_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    structlog.contextvars.bind_contextvars(
        request_id=request_id.hex, mcp_session_id=str(session_id)
    )
    token = parent_audit_id_var.set(parent_id)
    try:
        await _clear_composite_gate()
    finally:
        parent_audit_id_var.reset(token)

    row = await _grant_use_row(grant_id)
    assert row.request_id == request_id
    assert row.agent_session_id == session_id
    assert row.parent_audit_id == parent_id


@pytest.mark.asyncio
async def test_grant_use_row_ids_stay_null_when_nothing_is_bound() -> None:
    grant_id = await _seed_grant(op_id=_COMPOSITE_OP, connector_id=_COMPOSITE_CONNECTOR)
    await _clear_composite_gate()

    row = await _grant_use_row(grant_id)
    assert row.request_id is None
    assert row.agent_session_id is None
    assert row.parent_audit_id is None


@pytest.mark.asyncio
async def test_grant_use_row_shares_request_id_with_its_dispatch_row(
    stub_embedding_service: AsyncMock,
) -> None:
    """The auto-approval and the DISPATCH row of the same call share ``request_id``."""
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    await register_typed_operation(
        product="stub",
        version="1.x",
        impl_id="stub",
        op_id=_STUB_OP,
        handler=_stub_deploy_handler,
        summary="Stub deploy for lineage tests.",
        description="A caution-level write a service principal needs a grant for.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="caution",
        embedding_service=stub_embedding_service,
    )
    grant_id = await _seed_grant(op_id=_STUB_OP, connector_id=_STUB_CONNECTOR)
    request_id = uuid.uuid4()
    structlog.contextvars.bind_contextvars(request_id=str(request_id))

    result = await dispatch(
        operator=_service_operator(),
        connector_id=_STUB_CONNECTOR,
        op_id=_STUB_OP,
        target=None,
        params={},
    )
    assert result.status == "ok", result

    grant_row = await _grant_use_row(grant_id)
    async with get_sessionmaker()() as s:
        dispatch_row = (
            await s.scalars(
                select(AuditLog).where(AuditLog.method == "DISPATCH", AuditLog.path == _STUB_OP)
            )
        ).one()
    assert grant_row.request_id == request_id
    assert dispatch_row.request_id == request_id
