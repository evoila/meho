# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Trace search: ``GET /api/v1/audit/traces`` and ``find_traces`` (#6452).

A stored trace is found by request id, by caller and by time, without its
audit row. The tests seed traces with no audit row at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.audit import router as audit_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.flight_recorder import SpanInput, find_traces, record_trace
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT_A = UUID("55555555-5555-5555-5555-555555555555")
_TENANT_B = UUID("66666666-6666-6666-6666-666666666666")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    clear_jwks_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(audit_router)
    yield TestClient(app)


async def _seed_tenant(tenant_id: UUID, slug: str, *, agent_readable: bool | None = None) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=tenant_id,
                slug=slug,
                name=f"Tenant {slug}",
                flight_recorder_enabled=True,
                flight_recorder_agent_readable=agent_readable,
            )
        )
        await session.commit()


async def _seed(
    *,
    tenant_id: UUID,
    request_id: UUID | None,
    operator_sub: str | None,
    when: datetime,
    uncertain: bool = False,
) -> UUID:
    """Store a trace with no audit row. Returns its audit id."""
    audit_id = uuid.uuid4()
    trace_id = await record_trace(
        audit_id=audit_id,
        tenant_id=tenant_id,
        spans=[
            SpanInput(
                span_kind="vendor_call",
                name="GET /x",
                started_at=when,
                duration_ms=Decimal("1.0"),
                status="200",
                attributes={"method": "GET"},
            )
        ],
        redaction_uncertain=uncertain,
        request_id=request_id,
        operator_sub=operator_sub,
        now=when,
    )
    assert trace_id is not None
    return audit_id


def _get(
    client: TestClient,
    query: str,
    *,
    role: TenantRole = TenantRole.OPERATOR,
    principal_kind: str | None = None,
) -> Any:
    clear_jwks_cache()
    key = make_rsa_keypair("kid-S")
    token = mint_token(
        key,
        sub="op-1",
        tenant_role=role.value,
        tenant_id=str(_TENANT_A),
        principal_kind=principal_kind,
    )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        return client.get(
            f"/api/v1/audit/traces{query}", headers={"Authorization": f"Bearer {token}"}
        )


# ---------------------------------------------------------------------------
# find_traces
# ---------------------------------------------------------------------------


async def test_find_by_request_id_without_an_audit_row() -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    now = datetime.now(UTC)
    wanted = uuid.uuid4()
    audit_id = await _seed(tenant_id=_TENANT_A, request_id=wanted, operator_sub="u1", when=now)
    await _seed(tenant_id=_TENANT_A, request_id=uuid.uuid4(), operator_sub="u1", when=now)
    async with get_sessionmaker()() as session:
        found = await find_traces(session, tenant_id=_TENANT_A, request_id=wanted)
    assert [t.audit_id for t in found] == [audit_id]
    assert found[0].request_id == wanted
    assert found[0].operator_sub == "u1"
    assert [s.name for s in found[0].spans] == ["GET /x"]


async def test_find_by_caller_and_time_newest_first() -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    now = datetime.now(UTC)
    old = await _seed(
        tenant_id=_TENANT_A, request_id=None, operator_sub="u1", when=now - timedelta(hours=5)
    )
    mid = await _seed(
        tenant_id=_TENANT_A, request_id=None, operator_sub="u1", when=now - timedelta(hours=2)
    )
    await _seed(
        tenant_id=_TENANT_A, request_id=None, operator_sub="u2", when=now - timedelta(hours=1)
    )
    async with get_sessionmaker()() as session:
        both = await find_traces(session, tenant_id=_TENANT_A, operator_sub="u1")
        recent = await find_traces(
            session,
            tenant_id=_TENANT_A,
            operator_sub="u1",
            since=now - timedelta(hours=3),
            until=now,
        )
        nobody = await find_traces(session, tenant_id=_TENANT_A, operator_sub="nobody")
        one = await find_traces(session, tenant_id=_TENANT_A, operator_sub="u1", limit=1)
    assert [t.audit_id for t in both] == [mid, old]
    assert [t.audit_id for t in recent] == [mid]
    assert nobody == []
    assert [t.audit_id for t in one] == [mid]


async def test_find_is_tenant_scoped() -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    await _seed_tenant(_TENANT_B, "tenant-b")
    rid = uuid.uuid4()
    await _seed(tenant_id=_TENANT_B, request_id=rid, operator_sub="u1", when=datetime.now(UTC))
    async with get_sessionmaker()() as session:
        assert await find_traces(session, tenant_id=_TENANT_A, request_id=rid) == []


async def test_a_trace_without_request_id_or_caller_is_found_by_time_only() -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    audit_id = await _seed(
        tenant_id=_TENANT_A, request_id=None, operator_sub=None, when=datetime.now(UTC)
    )
    async with get_sessionmaker()() as session:
        by_time = await find_traces(session, tenant_id=_TENANT_A)
        by_caller = await find_traces(session, tenant_id=_TENANT_A, operator_sub="u1")
    assert [t.audit_id for t in by_time] == [audit_id]
    assert by_caller == []


async def test_uncertain_traces_can_be_left_out() -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    now = datetime.now(UTC)
    await _seed(tenant_id=_TENANT_A, request_id=None, operator_sub="u1", when=now, uncertain=True)
    clean = await _seed(tenant_id=_TENANT_A, request_id=None, operator_sub="u1", when=now)
    async with get_sessionmaker()() as session:
        everything = await find_traces(session, tenant_id=_TENANT_A)
        only_clean = await find_traces(
            session, tenant_id=_TENANT_A, include_redaction_uncertain=False
        )
    assert len(everything) == 2
    assert [t.audit_id for t in only_clean] == [clean]


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


async def test_route_finds_a_trace_by_request_id(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    rid = uuid.uuid4()
    audit_id = await _seed(
        tenant_id=_TENANT_A, request_id=rid, operator_sub="u1", when=datetime.now(UTC)
    )
    resp = _get(client, f"?request_id={rid}")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [item["audit_id"] for item in items] == [str(audit_id)]
    assert items[0]["request_id"] == str(rid)
    assert items[0]["spans"][0]["name"] == "GET /x"


async def test_route_finds_by_caller_and_window(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    now = datetime.now(UTC)
    recent = await _seed(
        tenant_id=_TENANT_A, request_id=None, operator_sub="u1", when=now - timedelta(minutes=10)
    )
    await _seed(
        tenant_id=_TENANT_A, request_id=None, operator_sub="u1", when=now - timedelta(days=3)
    )
    resp = _get(client, "?caller=u1&since=1h")
    assert resp.status_code == 200, resp.text
    assert [item["audit_id"] for item in resp.json()["items"]] == [str(recent)]


async def test_route_does_not_show_another_tenants_trace(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    await _seed_tenant(_TENANT_B, "tenant-b")
    rid = uuid.uuid4()
    await _seed(tenant_id=_TENANT_B, request_id=rid, operator_sub="u1", when=datetime.now(UTC))
    resp = _get(client, f"?request_id={rid}")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_route_rejects_a_bad_window_and_a_bad_request_id(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    assert _get(client, "?since=not-a-time").status_code == 400
    assert _get(client, "?request_id=not-a-uuid").status_code == 422
    assert _get(client, "?limit=0").status_code == 422


async def test_route_needs_the_operator_claim(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    assert _get(client, "", role=TenantRole.READ_ONLY).status_code == 403
    assert client.get("/api/v1/audit/traces").status_code == 401


async def test_route_as_an_agent_reads_nothing_when_the_tenant_switch_is_off(
    client: TestClient,
) -> None:
    await _seed_tenant(_TENANT_A, "tenant-a", agent_readable=False)
    rid = uuid.uuid4()
    await _seed(tenant_id=_TENANT_A, request_id=rid, operator_sub="u1", when=datetime.now(UTC))
    agent = _get(client, f"?request_id={rid}", principal_kind="agent")
    assert agent.status_code == 200, agent.text
    assert agent.json()["items"] == []
    person = _get(client, f"?request_id={rid}")
    assert len(person.json()["items"]) == 1


async def test_route_as_an_agent_never_gets_an_uncertain_trace(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A, "tenant-a", agent_readable=True)
    now = datetime.now(UTC)
    await _seed(tenant_id=_TENANT_A, request_id=None, operator_sub="u1", when=now, uncertain=True)
    clean = await _seed(tenant_id=_TENANT_A, request_id=None, operator_sub="u1", when=now)
    agent = _get(client, "?caller=u1", principal_kind="agent")
    assert agent.status_code == 200, agent.text
    assert [item["audit_id"] for item in agent.json()["items"]] == [str(clean)]
    person = _get(client, "?caller=u1")
    assert len(person.json()["items"]) == 2
