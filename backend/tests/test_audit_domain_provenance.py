# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Accountability readers and domain provenance (#3747).

What these tests prove:

* The event class counters count every class for the same filters, keep
  old rows as ``legacy_request``, and stay inside one tenant.
* ``POST /api/v1/audit/class-counts`` answers those counters.
* The flight-recorder trace route resolves only ``operation_execution``
  rows. A decision, security or legacy row gets no trace, even when one is
  stored under its id.
* A convention edit records itself: its history row carries
  ``change_kind`` and the request correlation id, and it no longer points
  at the request wrapper row.
* A topology edit's history rows point at the topology writer's own
  domain row, never at a request wrapper row.
* Parking an approval request stays one transaction, and a credential
  write keeps its ``execution_handle`` link.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
import structlog
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.audit import router as audit_router
from meho_backplane.api.v1.audit_counts import router as audit_counts_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.audit_query import AuditQueryFilters, count_event_classes
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.conventions.schemas import ConventionCreate, ConventionKind, ConventionUpdate
from meho_backplane.conventions.service import ConventionsService
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditEventClass,
    AuditLog,
    GraphEdgeHistory,
    GraphNode,
    Tenant,
    TenantConventionHistory,
)
from meho_backplane.flight_recorder import SpanInput, record_trace
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import create_pending_request
from meho_backplane.settings import get_settings
from meho_backplane.topology.annotate import NodeRef, annotate_edge

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_A = uuid.UUID("55555555-5555-5555-5555-555555555555")
_TENANT_B = uuid.UUID("66666666-6666-6666-6666-666666666666")
_BASE = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env :class:`Settings` reads and reset the caches."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    clear_jwks_cache()
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    clear_jwks_cache()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(audit_router)
    app.include_router(audit_counts_router)
    yield TestClient(app)


def _operator(tenant_id: uuid.UUID = _TENANT_A, *, sub: str = "op-1") -> Operator:
    return Operator(
        sub=sub,
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def _token(key: Any, tenant_id: uuid.UUID = _TENANT_A) -> str:
    return mint_token(
        key, sub="op-1", tenant_role=TenantRole.OPERATOR.value, tenant_id=str(tenant_id)
    )


async def _seed_tenant(tenant_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as s:
        if await s.get(Tenant, tenant_id) is None:
            s.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="Tenant"))
            await s.commit()


async def _seed_audit_row(
    *,
    tenant_id: uuid.UUID,
    event_class: AuditEventClass | None,
    second: int = 0,
) -> uuid.UUID:
    """Insert one row; ``event_class=None`` leaves the column default (old row)."""
    row_id = uuid.uuid4()
    extra = {} if event_class is None else {"event_class": event_class}
    async with get_sessionmaker()() as s:
        s.add(
            AuditLog(
                id=row_id,
                occurred_at=_BASE + timedelta(seconds=second),
                operator_sub="op-actor",
                tenant_id=tenant_id,
                method="POST",
                path="/mcp",
                status_code=200,
                duration_ms=Decimal("1.0"),
                payload={"op_id": "vsphere.vm.list", "op_class": "read"},
                **extra,
            )
        )
        await s.commit()
    return row_id


# ---------------------------------------------------------------------------
# Event class counters
# ---------------------------------------------------------------------------


async def test_count_event_classes_counts_every_class_and_keeps_legacy(
    session: AsyncSession,
) -> None:
    """Old rows count as ``legacy_request``; the class filter is ignored."""
    await _seed_audit_row(tenant_id=_TENANT_A, event_class=AuditEventClass.OPERATION_EXECUTION)
    await _seed_audit_row(tenant_id=_TENANT_A, event_class=AuditEventClass.POLICY_DECISION)
    await _seed_audit_row(tenant_id=_TENANT_A, event_class=AuditEventClass.APPROVAL_DECISION)
    await _seed_audit_row(tenant_id=_TENANT_A, event_class=None)
    await _seed_audit_row(tenant_id=_TENANT_B, event_class=AuditEventClass.SECURITY_EVENT)

    counts = await count_event_classes(
        AuditQueryFilters(event_class=AuditEventClass.POLICY_DECISION),
        tenant_id=_TENANT_A,
        session=session,
    )

    assert counts == {
        "operation_execution": 1,
        "policy_decision": 1,
        "approval_decision": 1,
        "security_event": 0,
        "legacy_request": 1,
    }


async def test_count_event_classes_applies_the_other_filters(session: AsyncSession) -> None:
    """The time window still narrows the counters."""
    await _seed_audit_row(
        tenant_id=_TENANT_A, event_class=AuditEventClass.OPERATION_EXECUTION, second=0
    )
    await _seed_audit_row(
        tenant_id=_TENANT_A, event_class=AuditEventClass.OPERATION_EXECUTION, second=100
    )

    counts = await count_event_classes(
        AuditQueryFilters(since=_BASE + timedelta(seconds=50)),
        tenant_id=_TENANT_A,
        session=session,
    )

    assert counts["operation_execution"] == 1
    assert sum(counts.values()) == 1


async def test_class_counts_route_answers_per_class_counts(client: TestClient) -> None:
    await _seed_audit_row(tenant_id=_TENANT_A, event_class=AuditEventClass.POLICY_DECISION)
    await _seed_audit_row(tenant_id=_TENANT_A, event_class=None)
    await _seed_audit_row(tenant_id=_TENANT_B, event_class=AuditEventClass.POLICY_DECISION)

    key = make_rsa_keypair("kid-counts")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/audit/class-counts",
            json={},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
        rejected = client.post(
            "/api/v1/audit/class-counts",
            json={"event_class": "policy_decision"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["counts"]["policy_decision"] == 1
    assert body["counts"]["legacy_request"] == 1
    assert body["total"] == 2
    # The counters always show every class, so a class filter is refused.
    assert rejected.status_code == 422


# ---------------------------------------------------------------------------
# Flight-recorder trace resolves only execution rows
# ---------------------------------------------------------------------------


def _span() -> SpanInput:
    return SpanInput(
        span_kind="vendor_call",
        name="GET /rest/vm",
        started_at=_BASE,
        duration_ms=Decimal("1.00"),
        status="200",
        attributes={"method": "GET", "url": "https://vendor.example/rest/vm"},
    )


@pytest.mark.parametrize(
    ("event_class", "trace_present"),
    [
        (AuditEventClass.OPERATION_EXECUTION, True),
        (AuditEventClass.POLICY_DECISION, False),
        (AuditEventClass.APPROVAL_DECISION, False),
        (AuditEventClass.SECURITY_EVENT, False),
        (AuditEventClass.LEGACY_REQUEST, False),
    ],
)
async def test_trace_route_resolves_only_execution_rows(
    client: TestClient, event_class: AuditEventClass, trace_present: bool
) -> None:
    await _seed_tenant(_TENANT_A)
    audit_id = await _seed_audit_row(tenant_id=_TENANT_A, event_class=event_class)
    trace_id = await record_trace(
        audit_id=audit_id, tenant_id=_TENANT_A, spans=[_span()], now=_BASE
    )
    assert trace_id is not None

    key = make_rsa_keypair("kid-trace")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/{audit_id}/trace",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trace_present"] is trace_present
    assert (body["trace"] is not None) is trace_present
    assert body["event_class"] == event_class.value


# ---------------------------------------------------------------------------
# Convention history records the edit itself
# ---------------------------------------------------------------------------


async def _convention_history(convention_id: uuid.UUID) -> list[TenantConventionHistory]:
    async with get_sessionmaker()() as s:
        rows = await s.execute(
            select(TenantConventionHistory)
            .where(TenantConventionHistory.convention_id == convention_id)
            .order_by(TenantConventionHistory.ts)
        )
        return list(rows.scalars().all())


async def test_convention_history_records_change_kind_and_request_id() -> None:
    """Create / update / delete each write a self-describing history row."""
    await _seed_tenant(_TENANT_A)
    operator = _operator()
    service = ConventionsService()
    request_ids = [uuid.uuid4() for _ in range(3)]

    async with get_sessionmaker()() as s:
        structlog.contextvars.bind_contextvars(request_id=request_ids[0].hex)
        created = await service.create_convention(
            session=s,
            operator=operator,
            body=ConventionCreate(
                slug="provenance", title="Provenance", body="First.", kind=ConventionKind.WORKFLOW
            ),
        )
        await s.commit()
        structlog.contextvars.bind_contextvars(request_id=request_ids[1].hex)
        await service.update_convention(
            session=s, operator=operator, slug="provenance", body=ConventionUpdate(body="Second.")
        )
        await s.commit()
        # A request id that is not a UUID is optional correlation: stored as NULL.
        structlog.contextvars.bind_contextvars(request_id="not-a-uuid")
        await service.delete_convention(session=s, operator=operator, slug="provenance")
        await s.commit()

    history = await _convention_history(created.id)
    assert [row.change_kind for row in history] == ["created", "updated", "deleted"]
    assert [row.request_id for row in history] == [request_ids[0], request_ids[1], None]
    assert all(row.audit_id is None for row in history)
    assert all(row.actor_sub == operator.sub for row in history)
    # The service no longer pre-allocates the request wrapper row's id.
    assert "preallocated_audit_id" not in structlog.contextvars.get_contextvars()


# ---------------------------------------------------------------------------
# Topology history points at the writer's own domain row
# ---------------------------------------------------------------------------


async def test_topology_history_anchors_on_the_domain_row() -> None:
    await _seed_tenant(_TENANT_A)
    async with get_sessionmaker()() as s:
        for name, kind in (("vm-a", "vm"), ("ds-1", "datastore")):
            s.add(
                GraphNode(
                    id=uuid.uuid4(),
                    tenant_id=_TENANT_A,
                    kind=kind,
                    name=name,
                    discovered_by="test",
                )
            )
        await s.commit()

    operator = _operator(sub="op-topology")
    with patch("meho_backplane.topology.annotate.publish_event", new=AsyncMock()):
        async with get_sessionmaker()() as s:
            await annotate_edge(
                s,
                operator,
                NodeRef(name="vm-a", kind="vm"),
                "authenticates-via",
                NodeRef(name="ds-1", kind="datastore"),
            )

    async with get_sessionmaker()() as s:
        history = list(
            (
                await s.execute(
                    select(GraphEdgeHistory).where(GraphEdgeHistory.tenant_id == _TENANT_A)
                )
            )
            .scalars()
            .all()
        )
        assert history
        anchor_ids = {row.audit_id for row in history}
        anchors = list(
            (await s.execute(select(AuditLog).where(AuditLog.id.in_(anchor_ids)))).scalars().all()
        )

    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor.payload.get("op_id") == "topology.annotate"
    assert anchor.operator_sub == operator.sub
    assert anchor.event_class != AuditEventClass.LEGACY_REQUEST


# ---------------------------------------------------------------------------
# Approval park stays atomic and keeps the execution handle link
# ---------------------------------------------------------------------------


async def _park(session: AsyncSession, op_id: str, params: dict[str, Any]) -> ApprovalRequest:
    return await create_pending_request(
        session,
        operator=_operator(),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
        params_hash=compute_params_hash(params),
    )


async def test_approval_park_rolls_back_as_one_unit(session: AsyncSession) -> None:
    request = await _park(session, "vault.kv.write", {"key": "value"})
    request_id, audit_id = request.id, request.request_audit_id
    await session.rollback()

    async with get_sessionmaker()() as fresh:
        assert await fresh.get(ApprovalRequest, request_id) is None
        assert await fresh.get(AuditLog, audit_id) is None


async def test_approval_park_commits_request_decision_and_handle_together(
    session: AsyncSession,
) -> None:
    request = await _park(session, "vault.kv.put", {"path": "kv/app", "value": "test-only"})
    await session.commit()

    async with get_sessionmaker()() as fresh:
        stored = await fresh.get(ApprovalRequest, request.id)
        park_row = await fresh.get(AuditLog, request.request_audit_id)
    assert stored is not None
    assert stored.execution_handle is not None
    assert park_row is not None
    assert park_row.event_class == AuditEventClass.POLICY_DECISION
