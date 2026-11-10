# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""REST tests for ``/api/v1/agents/budgets`` (set, show and clear a spend cap).

* **Role** — only ``tenant_admin`` may call any route; ``operator`` gets 403.
* **People only** — set and clear refuse a machine login (an agent cannot
  change its own cap), and the refusal is audited.
* **The cap works** — a daily request cap of 1 set over REST lets the first
  run start and refuses the second one.
* **Audit** — set and clear each write an audit row.
* **Bad input** — a body without any limit, a negative limit and an unknown
  window are refused with 422.
* **Tenant** — a cap set in one tenant is not visible in another.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.agent.models import AgentTier
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, BudgetWindowKind
from meho_backplane.main import app
from meho_backplane.operations.budget_enforcement import (
    BudgetDecisionKind,
    EnforcementContext,
    evaluate_pre_run_budget,
)
from meho_backplane.operations.identity_budget import apply_consumption, get_remaining
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_BASE = "/api/v1/agents/budgets"
_AGENT = "agent:pod-reader"


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
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)


def _headers(
    key: Any,
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    sub: str = "admin-1",
    tenant: uuid.UUID = _TENANT_A,
    kind: str | None = None,
) -> dict[str, str]:
    extra: dict[str, Any] = {"principal_kind": kind} if kind else {}
    token = mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(tenant), **extra)
    return {"Authorization": f"Bearer {token}"}


_ENDPOINTS: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", f"{_BASE}?principal_sub={_AGENT}", None),
    ("PUT", _BASE, {"principal_sub": _AGENT, "window": "daily", "request_limit": 1}),
    ("DELETE", f"{_BASE}?principal_sub={_AGENT}&window=daily", None),
)


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
@pytest.mark.parametrize("method, path, body", _ENDPOINTS)
def test_only_tenant_admin_may_call(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None, role: TenantRole
) -> None:
    """Every route is 403 ``insufficient_role`` below ``tenant_admin``."""
    key = make_rsa_keypair("kid-role")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        response = client.request(method, path, headers=_headers(key, role=role), json=body)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}


async def _audit_rows(sub: str, method: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        return list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.operator_sub == sub,
                        AuditLog.method == method,
                        AuditLog.path == _BASE,
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.parametrize("kind", ["agent", "service"])
async def test_machine_login_cannot_change_a_cap(client: TestClient, kind: str) -> None:
    """A machine login with ``tenant_admin`` gets 403 on set; no cap is written."""
    sub = f"machine-{kind}"
    key = make_rsa_keypair(f"kid-machine-{kind}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        response = client.put(
            _BASE,
            headers=_headers(key, sub=sub, kind=kind),
            json={"principal_sub": sub, "window": "daily", "request_limit": 1000},
        )
    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith("human_principal_required")
    rows = await _audit_rows(sub, "PUT")
    assert [row.status_code for row in rows] == [403]
    async with get_sessionmaker()() as session:
        reading = await get_remaining(
            session,
            tenant_id=_TENANT_A,
            principal_sub=sub,
            window_kind=BudgetWindowKind.DAILY,
        )
    assert reading.request_limit is None


def test_set_show_clear_round_trip_and_audit(client: TestClient) -> None:
    """Set a cap, show it, clear it; each change leaves an audit row."""
    key = make_rsa_keypair("kid-round")
    sub = "admin-round"
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = _headers(key, sub=sub)
        put = client.put(
            _BASE,
            headers=headers,
            json={
                "principal_sub": _AGENT,
                "window": "daily",
                "cost_limit": 5,
                "request_limit": 200,
            },
        )
        assert put.status_code == 200, put.text
        assert put.json()["cost_limit"] == 5
        assert put.json()["request_limit"] == 200
        assert put.json()["token_limit"] is None

        # A second set with one field changes only that field.
        put2 = client.put(
            _BASE,
            headers=headers,
            json={"principal_sub": _AGENT, "window": "daily", "request_limit": 300},
        )
        assert put2.status_code == 200, put2.text
        assert put2.json()["request_limit"] == 300
        assert put2.json()["cost_limit"] == 5

        shown = client.get(_BASE, headers=headers, params={"principal_sub": _AGENT})
        assert shown.status_code == 200, shown.text
        by_window = {b["window"]: b for b in shown.json()["budgets"]}
        assert set(by_window) == {"daily", "weekly", "monthly"}
        assert by_window["daily"]["request_limit"] == 300
        assert by_window["daily"]["requests_consumed"] == 0
        assert by_window["daily"]["requests_remaining"] == 300
        assert by_window["weekly"]["request_limit"] is None

        cleared = client.delete(
            _BASE, headers=headers, params={"principal_sub": _AGENT, "window": "daily"}
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["request_limit"] is None
        assert cleared.json()["cost_limit"] is None

    assert [row.status_code for row in asyncio.run(_audit_rows(sub, "PUT"))] == [200, 200]
    assert [row.status_code for row in asyncio.run(_audit_rows(sub, "DELETE"))] == [200]


@pytest.mark.parametrize(
    "body",
    [
        {"principal_sub": _AGENT, "window": "daily"},
        {"principal_sub": _AGENT, "window": "daily", "request_limit": -1},
        {"principal_sub": _AGENT, "window": "daily", "cost_limit": -0.5},
        {"principal_sub": _AGENT, "window": "hourly", "request_limit": 1},
        {"principal_sub": "", "window": "daily", "request_limit": 1},
    ],
)
def test_bad_body_is_422(client: TestClient, body: dict[str, Any]) -> None:
    key = make_rsa_keypair("kid-422")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        response = client.put(_BASE, headers=_headers(key), json=body)
    assert response.status_code == 422, response.text


def test_cap_of_one_request_refuses_the_second_run(client: TestClient) -> None:
    """Daily request cap 1: the first run may start, the second is refused."""
    key = make_rsa_keypair("kid-cap")
    sub = "agent:cap-one"
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        put = client.put(
            _BASE,
            headers=_headers(key),
            json={"principal_sub": sub, "window": "daily", "request_limit": 1},
        )
    assert put.status_code == 200, put.text

    context = EnforcementContext(
        degrade_threshold=0.8, global_kill_switch=False, disabled_tenants=frozenset()
    )

    async def _gate() -> BudgetDecisionKind:
        async with get_sessionmaker()() as session:
            decision = await evaluate_pre_run_budget(
                session,
                tenant_id=_TENANT_A,
                principal_sub=sub,
                requested_tier=AgentTier.TRIAGE,
                context=context,
            )
        return decision.kind

    async def _run_once() -> None:
        async with get_sessionmaker()() as session, session.begin():
            await apply_consumption(
                session, tenant_id=_TENANT_A, principal_sub=sub, tokens=10, cost=Decimal("0.01")
            )

    assert asyncio.run(_gate()) is BudgetDecisionKind.ALLOW
    asyncio.run(_run_once())
    assert asyncio.run(_gate()) is BudgetDecisionKind.REFUSE


def test_cap_holds_in_the_next_window_and_clear_removes_it(client: TestClient) -> None:
    """A cap set today still counts tomorrow; clearing it ends it for tomorrow too."""
    key = make_rsa_keypair("kid-carry")
    sub = "agent:carry"
    tomorrow = datetime.now(UTC) + timedelta(days=1)

    async def _read(when: datetime) -> int | None:
        async with get_sessionmaker()() as session:
            reading = await get_remaining(
                session,
                tenant_id=_TENANT_A,
                principal_sub=sub,
                window_kind=BudgetWindowKind.DAILY,
                when=when,
            )
        return reading.request_limit

    async def _charge(when: datetime) -> None:
        async with get_sessionmaker()() as session, session.begin():
            await apply_consumption(
                session,
                tenant_id=_TENANT_A,
                principal_sub=sub,
                tokens=1,
                cost=Decimal(0),
                when=when,
            )

    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = _headers(key)
        put = client.put(
            _BASE,
            headers=headers,
            json={"principal_sub": sub, "window": "daily", "request_limit": 5},
        )
        assert put.status_code == 200, put.text

        # No row for tomorrow yet: the cap still shows.
        assert asyncio.run(_read(tomorrow)) == 5
        # A run creates the row: it keeps the cap.
        asyncio.run(_charge(tomorrow + timedelta(days=1)))
        assert asyncio.run(_read(tomorrow + timedelta(days=1))) == 5

        cleared = client.delete(
            _BASE, headers=headers, params={"principal_sub": sub, "window": "daily"}
        )
        assert cleared.status_code == 200, cleared.text
    assert asyncio.run(_read(tomorrow)) is None


def test_cap_is_per_tenant(client: TestClient) -> None:
    """The same principal name in another tenant has no cap."""
    key = make_rsa_keypair("kid-tenant")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        put = client.put(
            _BASE,
            headers=_headers(key, tenant=_TENANT_A),
            json={"principal_sub": "agent:shared", "window": "monthly", "token_limit": 100},
        )
        assert put.status_code == 200, put.text
        other = client.get(
            _BASE,
            headers=_headers(key, tenant=_TENANT_B),
            params={"principal_sub": "agent:shared"},
        )
    assert other.status_code == 200, other.text
    assert all(b["token_limit"] is None for b in other.json()["budgets"])
