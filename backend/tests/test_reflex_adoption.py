# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the #3134 reflex-adoption KPI surface.

Coverage matrix (issue #3134 acceptance criteria):

* **Service-level aggregation** (``compute_reflex_report``) — every
  metric proven against a seeded ``audit_log`` fixture whose known
  session sequences produce hand-computable expected values:
  read-before-act (read-first / no-broadcast_recent / broadcast-after),
  announce-coverage (covered / uncovered / announce-after-op),
  write-back rate, and the surface split (CLI rows with no
  ``agent_session_id``).
* **Tenant boundary** — a second tenant's rows never leak into the
  first tenant's numbers.
* **HTTP route** — RBAC (operator 200, read_only 403, operator +
  ``tenant_filter`` 403, ``platform_admin`` + ``tenant_filter`` 200),
  malformed ``since`` → 400, and a populated end-to-end read-back.

Service tests seed ``audit_log`` / ``agent_announcement`` directly via
:class:`AsyncSession` so the aggregation logic is unit-testable without
a live request. Route tests reuse the ``test_retrieval_usage`` app +
JWT-helper pattern.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.audit_reflex import router as reflex_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement, AuditLog, Tenant
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.reflex.adoption import (
    ADD_TOOLS,
    BROADCAST_RECENT_TOOL,
    CALL_OPERATION_TOOL,
    DISPATCH_METHOD,
    MCP_TOOL_PATH_PREFIX,
    ReflexReport,
    compute_reflex_report,
)

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Settings + JWKS cache fixtures (mirrors test_retrieval_usage.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads, around every test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    clear_jwks_cache()
    yield
    clear_jwks_cache()


# ---------------------------------------------------------------------------
# Timeline + tenants
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
_SINCE = _T0 - timedelta(hours=1)
_UNTIL = _T0 + timedelta(hours=1)

_TENANT_A = UUID("00000000-0000-0000-0000-00000000a0a0")
_TENANT_B = UUID("00000000-0000-0000-0000-00000000b0b0")

_CALL_PATH = f"{MCP_TOOL_PATH_PREFIX}{CALL_OPERATION_TOOL}"
_RECENT_PATH = f"{MCP_TOOL_PATH_PREFIX}{BROADCAST_RECENT_TOOL}"
_ADD_KB_PATH = f"{MCP_TOOL_PATH_PREFIX}{ADD_TOOLS[0]}"
_ADD_MEM_PATH = f"{MCP_TOOL_PATH_PREFIX}{ADD_TOOLS[1]}"

_WRITE_OP = "vsphere.vm.create"  # classify_op -> "write"
_WRITE_OP_2 = "vsphere.vm.delete"  # classify_op -> "write"
_READ_OP = "vsphere.vm.list"  # classify_op -> "read"


def _at(seconds: int) -> datetime:
    """A timestamp *seconds* after the fixture base ``_T0``."""
    return _T0 + timedelta(seconds=seconds)


async def _seed_tenant(tenant_id: UUID) -> None:
    """Insert the FK-parent :class:`Tenant` row (announce store requires it)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Tenant(id=tenant_id, slug=f"t-{tenant_id.hex}", name=f"Tenant {tenant_id.hex[-6:]}"),
        )
        await session.commit()


async def _seed_audit(
    *,
    tenant_id: UUID | None,
    path: str,
    occurred_at: datetime,
    method: str = "MCP",
    status_code: int = 200,
    agent_session_id: UUID | None = None,
) -> None:
    """Insert one ``audit_log`` row directly."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=uuid.uuid4(),
                occurred_at=occurred_at,
                operator_sub="op-1",
                tenant_id=tenant_id,
                agent_session_id=agent_session_id,
                method=method,
                path=path,
                status_code=status_code,
                request_id=None,
                duration_ms=Decimal("1.00"),
                payload={},
            ),
        )
        await session.commit()


async def _seed_dispatch(
    *,
    tenant_id: UUID | None,
    op_id: str,
    occurred_at: datetime,
    agent_session_id: UUID | None = None,
    status_code: int = 200,
) -> None:
    """Insert one dispatcher (``method='DISPATCH'``) row with ``path=op_id``."""
    await _seed_audit(
        tenant_id=tenant_id,
        path=op_id,
        occurred_at=occurred_at,
        method=DISPATCH_METHOD,
        status_code=status_code,
        agent_session_id=agent_session_id,
    )


async def _seed_announce(
    *,
    tenant_id: UUID,
    run_id: UUID | None,
    created_at: datetime,
) -> None:
    """Insert one :class:`AgentAnnouncement` claim (``run_id`` = session)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AgentAnnouncement(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                principal_sub="op-1",
                activity="announced work",
                phase="start",
                targets=[],
                run_id=run_id,
                created_at=created_at,
            ),
        )
        await session.commit()


async def _report(tenant_id: UUID | None) -> ReflexReport:
    """Compute the report over the full fixture window for *tenant_id*."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return await compute_reflex_report(
            session=session,
            since=_SINCE,
            until=_UNTIL,
            tenant_id=tenant_id,
        )


def _surface(report: ReflexReport, name: str) -> object:
    """Return the :class:`SurfaceMetrics` for *name* (``agent`` / ``cli_rest``)."""
    return next(s for s in report.surfaces if s.surface == name)


# ---------------------------------------------------------------------------
# Shape / empty baselines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_window_returns_both_surfaces_with_none_ratios() -> None:
    """Empty audit_log → both surfaces present, zero counts, ``None`` ratios."""
    report = await _report(_TENANT_A)
    assert isinstance(report, ReflexReport)
    assert [s.surface for s in report.surfaces] == ["agent", "cli_rest"]
    for surface in report.surfaces:
        assert surface.read_before_act_pct is None
        assert surface.read_before_act_sessions == 0
        assert surface.announce_coverage_pct is None
        assert surface.write_back_per_100_call_ops is None


# ---------------------------------------------------------------------------
# read-before-act
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_before_act_scores_first_call_vs_broadcast_recent() -> None:
    """Four sessions → 2 read-first / 4 = 50%.

    * ``s1`` broadcast_recent before first call → read-first.
    * ``s2`` two broadcast_recents, earliest before first call → read-first.
    * ``s3`` a call with no broadcast_recent at all → not read-first.
    * ``s4`` a call then a broadcast_recent (after) → not read-first.
    """
    await _seed_tenant(_TENANT_A)
    s1, s2, s3, s4 = (uuid.uuid4() for _ in range(4))
    # s1: recent then call
    await _seed_audit(
        tenant_id=_TENANT_A, path=_RECENT_PATH, occurred_at=_at(10), agent_session_id=s1
    )
    await _seed_audit(
        tenant_id=_TENANT_A, path=_CALL_PATH, occurred_at=_at(20), agent_session_id=s1
    )
    # s2: two recents (earliest before the call), then call
    await _seed_audit(
        tenant_id=_TENANT_A, path=_RECENT_PATH, occurred_at=_at(100), agent_session_id=s2
    )
    await _seed_audit(
        tenant_id=_TENANT_A, path=_RECENT_PATH, occurred_at=_at(110), agent_session_id=s2
    )
    await _seed_audit(
        tenant_id=_TENANT_A, path=_CALL_PATH, occurred_at=_at(120), agent_session_id=s2
    )
    # s3: call only (no broadcast_recent)
    await _seed_audit(
        tenant_id=_TENANT_A, path=_CALL_PATH, occurred_at=_at(30), agent_session_id=s3
    )
    # s4: call then recent (recent after → does not count)
    await _seed_audit(
        tenant_id=_TENANT_A, path=_CALL_PATH, occurred_at=_at(40), agent_session_id=s4
    )
    await _seed_audit(
        tenant_id=_TENANT_A, path=_RECENT_PATH, occurred_at=_at(50), agent_session_id=s4
    )

    agent = _surface(await _report(_TENANT_A), "agent")
    assert agent.read_before_act_sessions == 4
    assert agent.read_before_act_read_first == 2
    assert agent.read_before_act_pct == 50.0


# ---------------------------------------------------------------------------
# announce-coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_announce_coverage_counts_earlier_same_session_claim() -> None:
    """Two write ops, one with an earlier same-session announce → 50%."""
    await _seed_tenant(_TENANT_A)
    a1, a2 = uuid.uuid4(), uuid.uuid4()
    await _seed_dispatch(
        tenant_id=_TENANT_A, op_id=_WRITE_OP, occurred_at=_at(200), agent_session_id=a1
    )
    await _seed_announce(tenant_id=_TENANT_A, run_id=a1, created_at=_at(150))
    await _seed_dispatch(
        tenant_id=_TENANT_A, op_id=_WRITE_OP_2, occurred_at=_at(210), agent_session_id=a2
    )
    # A read op in a1 must not count toward the write denominator.
    await _seed_dispatch(
        tenant_id=_TENANT_A, op_id=_READ_OP, occurred_at=_at(220), agent_session_id=a1
    )

    agent = _surface(await _report(_TENANT_A), "agent")
    assert agent.announce_coverage_write_ops == 2
    assert agent.announce_coverage_announced == 1
    assert agent.announce_coverage_pct == 50.0


@pytest.mark.asyncio
async def test_announce_coverage_ignores_claim_created_after_op() -> None:
    """An announce created *after* the write op does not cover it → 0%."""
    await _seed_tenant(_TENANT_A)
    a1 = uuid.uuid4()
    await _seed_dispatch(
        tenant_id=_TENANT_A, op_id=_WRITE_OP, occurred_at=_at(200), agent_session_id=a1
    )
    await _seed_announce(tenant_id=_TENANT_A, run_id=a1, created_at=_at(300))

    agent = _surface(await _report(_TENANT_A), "agent")
    assert agent.announce_coverage_write_ops == 1
    assert agent.announce_coverage_announced == 0
    assert agent.announce_coverage_pct == 0.0


# ---------------------------------------------------------------------------
# write-back rate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_back_rate_is_adds_per_100_call_ops() -> None:
    """1 add over 2 call_operations → 50 per 100."""
    await _seed_tenant(_TENANT_A)
    sid = uuid.uuid4()
    await _seed_audit(
        tenant_id=_TENANT_A, path=_CALL_PATH, occurred_at=_at(10), agent_session_id=sid
    )
    await _seed_audit(
        tenant_id=_TENANT_A, path=_CALL_PATH, occurred_at=_at(20), agent_session_id=sid
    )
    await _seed_audit(
        tenant_id=_TENANT_A, path=_ADD_KB_PATH, occurred_at=_at(30), agent_session_id=sid
    )

    agent = _surface(await _report(_TENANT_A), "agent")
    assert agent.write_back_call_operations == 2
    assert agent.write_back_add_calls == 1
    assert agent.write_back_per_100_call_ops == 50.0


@pytest.mark.asyncio
async def test_write_back_counts_both_add_tools() -> None:
    """``add_to_knowledge`` + ``add_to_memory`` both feed the numerator."""
    await _seed_tenant(_TENANT_A)
    sid = uuid.uuid4()
    await _seed_audit(
        tenant_id=_TENANT_A, path=_CALL_PATH, occurred_at=_at(10), agent_session_id=sid
    )
    await _seed_audit(
        tenant_id=_TENANT_A, path=_ADD_KB_PATH, occurred_at=_at(20), agent_session_id=sid
    )
    await _seed_audit(
        tenant_id=_TENANT_A, path=_ADD_MEM_PATH, occurred_at=_at(30), agent_session_id=sid
    )

    agent = _surface(await _report(_TENANT_A), "agent")
    assert agent.write_back_add_calls == 2
    assert agent.write_back_per_100_call_ops == 200.0


# ---------------------------------------------------------------------------
# surface split — CLI/REST rows (no agent_session_id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_rows_split_out_and_meta_metrics_are_na() -> None:
    """CLI rows (no ``agent_session_id``) land in ``cli_rest``.

    * The MCP-meta-tool metrics (read-before-act, write-back) have no
      session on ``cli_rest`` → ``None`` (N/A).
    * A CLI write dispatch counts toward announce-coverage but can never
      be covered (no session to correlate a claim) → 0%.
    * The agent surface keeps its own numbers, unaffected by the split.
    """
    await _seed_tenant(_TENANT_A)
    sid = uuid.uuid4()
    # Agent-surface activity (read-first + write-back).
    await _seed_audit(
        tenant_id=_TENANT_A, path=_RECENT_PATH, occurred_at=_at(10), agent_session_id=sid
    )
    await _seed_audit(
        tenant_id=_TENANT_A, path=_CALL_PATH, occurred_at=_at(20), agent_session_id=sid
    )
    await _seed_audit(
        tenant_id=_TENANT_A, path=_ADD_KB_PATH, occurred_at=_at(30), agent_session_id=sid
    )
    # CLI/REST activity (no agent_session_id): a write dispatch + a read dispatch.
    await _seed_dispatch(
        tenant_id=_TENANT_A, op_id=_WRITE_OP, occurred_at=_at(500), agent_session_id=None
    )
    await _seed_dispatch(
        tenant_id=_TENANT_A, op_id=_READ_OP, occurred_at=_at(510), agent_session_id=None
    )

    report = await _report(_TENANT_A)
    cli = _surface(report, "cli_rest")
    assert cli.read_before_act_sessions == 0
    assert cli.read_before_act_pct is None
    assert cli.write_back_call_operations == 0
    assert cli.write_back_per_100_call_ops is None
    assert cli.announce_coverage_write_ops == 1
    assert cli.announce_coverage_announced == 0
    assert cli.announce_coverage_pct == 0.0

    agent = _surface(report, "agent")
    assert agent.read_before_act_pct == 100.0
    assert agent.write_back_per_100_call_ops == 100.0
    assert agent.announce_coverage_write_ops == 0
    assert agent.announce_coverage_pct is None


# ---------------------------------------------------------------------------
# tenant boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_scoping_prevents_cross_tenant_leakage() -> None:
    """Tenant B's read-first session never lifts tenant A's numbers."""
    await _seed_tenant(_TENANT_A)
    await _seed_tenant(_TENANT_B)
    # Tenant A: one session that is NOT read-first (call only).
    a_sid = uuid.uuid4()
    await _seed_audit(
        tenant_id=_TENANT_A, path=_CALL_PATH, occurred_at=_at(20), agent_session_id=a_sid
    )
    # Tenant B: one read-first session.
    b_sid = uuid.uuid4()
    await _seed_audit(
        tenant_id=_TENANT_B, path=_RECENT_PATH, occurred_at=_at(10), agent_session_id=b_sid
    )
    await _seed_audit(
        tenant_id=_TENANT_B, path=_CALL_PATH, occurred_at=_at(20), agent_session_id=b_sid
    )

    agent_a = _surface(await _report(_TENANT_A), "agent")
    assert agent_a.read_before_act_sessions == 1
    assert agent_a.read_before_act_read_first == 0
    assert agent_a.read_before_act_pct == 0.0

    agent_b = _surface(await _report(_TENANT_B), "agent")
    assert agent_b.read_before_act_read_first == 1
    assert agent_b.read_before_act_pct == 100.0


# ---------------------------------------------------------------------------
# HTTP route
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """A :class:`FastAPI` with the reflex route + production middleware stack."""
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(reflex_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def test_route_read_only_returns_403(client: TestClient) -> None:
    """``read_only`` is below the operator floor → 403."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-ro", tenant_role=TenantRole.READ_ONLY.value)
    with respx.mock as router:
        _mock_discovery_and_jwks(router, _public_jwks(key))
        resp = client.get("/api/v1/audit/reflex", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_route_operator_with_tenant_filter_returns_403(client: TestClient) -> None:
    """``operator`` + a foreign ``tenant_filter`` → 403 (cross-tenant gate)."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-x", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as router:
        _mock_discovery_and_jwks(router, _public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/reflex?tenant_filter={_TENANT_B}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "cross_tenant_requires_platform_admin"}


def test_route_platform_admin_with_tenant_filter_returns_200(client: TestClient) -> None:
    """``platform_admin`` + ``tenant_filter`` → 200 scoped to the filter."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-admin",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        platform_admin=True,
    )
    with respx.mock as router:
        _mock_discovery_and_jwks(router, _public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/reflex?tenant_filter={_TENANT_B}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["tenant_id"] == str(_TENANT_B)


def test_route_malformed_since_returns_400(client: TestClient) -> None:
    """``since=whenever`` is unparseable → 400 carrying the parser detail."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as router:
        _mock_discovery_and_jwks(router, _public_jwks(key))
        resp = client.get(
            "/api/v1/audit/reflex?since=whenever",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 400
    assert "whenever" in resp.json()["detail"]


def test_route_default_returns_200_zero_report(client: TestClient) -> None:
    """Default request with no audit_log rows → 200, both surfaces, None ratios."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as router:
        _mock_discovery_and_jwks(router, _public_jwks(key))
        resp = client.get("/api/v1/audit/reflex", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert [s["surface"] for s in body["surfaces"]] == ["agent", "cli_rest"]
    assert body["surfaces"][0]["read_before_act_pct"] is None
