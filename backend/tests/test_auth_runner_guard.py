# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit coverage for the satellite-runner gateway guard (Initiative #2415, #2502).

Exercises the two guard primitives in
:mod:`meho_backplane.auth.runner_guard` and the negative route cage in
:func:`meho_backplane.middleware.verify_jwt_and_bind`, against a minimal
stub app (the real gateway routes #2498/#2499 do not exist yet):

* ``require_runner`` admits only ``principal_kind=runner`` tokens.
* ``assert_runner_scope`` binds a runner token's ``runner_id`` to the
  runner **named** by the route — match -> 200, name mismatch / unknown
  name -> 403 ``runner_scope_violation`` (no existence oracle: both the
  wrong-runner and the no-such-runner case return the *same* 403).
* The negative cage 403s a runner-kind token on any route outside
  :data:`~meho_backplane.middleware.RUNNER_ALLOWED_PATH_PREFIXES`, admits
  it on the gateway prefixes, and never touches non-runner tokens.

JWKS discovery is stubbed via :mod:`respx`; tokens are signed with the
shared ``_oidc_jwt_helpers`` minter (runner tokens via its
``principal_kind`` / ``runner_id`` knobs).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator
from meho_backplane.auth.runner_guard import assert_runner_scope, require_runner
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerPrincipal, Tenant
from meho_backplane.middleware import (
    RUNNER_ALLOWED_PATH_PREFIXES,
    RequestContextMiddleware,
    verify_jwt_and_bind,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_RUNNER_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_RUNNER_B_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

# Resolve the runner dependency once at module load (FastAPI-idiomatic
# singleton, and avoids B008 "function call in argument default").
_REQUIRE_RUNNER = Depends(require_runner())


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _build_guard_app() -> FastAPI:
    """A stub app with a gateway-prefixed guard route + two cage probes."""
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/api/v1/gateway/{runner}/probe")
    async def gateway_probe(
        runner: str,
        operator: Operator = _REQUIRE_RUNNER,
    ) -> dict[str, str]:
        async with get_sessionmaker()() as session:
            row = await assert_runner_scope(operator, runner_name=runner, session=session)
        return {"resolved_id": str(row.id), "resolved_name": row.name}

    # Gateway-prefixed but guard-less: exercises the cage's *allow* arm.
    @app.get("/api/v1/gateway/open")
    async def gateway_open(operator: Operator = Depends(verify_jwt_and_bind)) -> dict[str, str]:
        return {"kind": operator.principal_kind.value}

    # Non-gateway route: exercises the cage's *deny* arm.
    @app.get("/api/v1/elsewhere")
    async def elsewhere(operator: Operator = Depends(verify_jwt_and_bind)) -> dict[str, str]:
        return {"kind": operator.principal_kind.value}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_build_guard_app())


async def _seed() -> None:
    """Seed the tenant + two runner principals (runner-a -> A, runner-b -> B)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == _TENANT))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=_TENANT, slug="tenant-guard", name="Guard Tenant"))
        for rid, rname in ((_RUNNER_A_ID, "runner-a"), (_RUNNER_B_ID, "runner-b")):
            if (
                await session.execute(select(RunnerPrincipal).where(RunnerPrincipal.id == rid))
            ).scalar_one_or_none() is None:
                session.add(
                    RunnerPrincipal(
                        id=rid,
                        tenant_id=_TENANT,
                        name=rname,
                        keycloak_client_id=f"runner:{rname}",
                        keycloak_internal_id=f"kc-{rname}",
                        owner_sub="op-admin",
                        revoked=False,
                        created_by_sub="op-admin",
                    )
                )
        await session.commit()


def _runner_token(key: object, *, runner_id: uuid.UUID) -> str:
    return mint_token(
        key,
        sub="runner-sub",
        tenant_id=str(_TENANT),
        tenant_role="read_only",
        principal_kind="runner",
        runner_id=str(runner_id),
    )


def _user_token(key: object) -> str:
    return mint_token(key, sub="user-sub", tenant_id=str(_TENANT), tenant_role="operator")


def test_runner_allowed_path_prefixes_exact() -> None:
    """The allowlist constant is exactly the two gateway prefixes #2498/#2499 mount under."""
    assert RUNNER_ALLOWED_PATH_PREFIXES == ("/api/v1/gateway/", "/api/v1/checks/")


@pytest.mark.asyncio
async def test_runner_scope_match_returns_200(client: TestClient) -> None:
    """A runner whose ``runner_id`` matches the row named by the route passes."""
    await _seed()
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _runner_token(key, runner_id=_RUNNER_A_ID)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        response = client.get(
            "/api/v1/gateway/runner-a/probe",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"resolved_id": str(_RUNNER_A_ID), "resolved_name": "runner-a"}


@pytest.mark.asyncio
async def test_runner_scope_name_mismatch_returns_403(client: TestClient) -> None:
    """A runner naming *another* registered runner is 403 ``runner_scope_violation``."""
    await _seed()
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _runner_token(key, runner_id=_RUNNER_A_ID)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        response = client.get(
            "/api/v1/gateway/runner-b/probe",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "runner_scope_violation"}


@pytest.mark.asyncio
async def test_runner_scope_unknown_name_returns_403_no_oracle(client: TestClient) -> None:
    """An unknown runner name is the *same* 403 as a mismatch — no existence oracle."""
    await _seed()
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _runner_token(key, runner_id=_RUNNER_A_ID)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        response = client.get(
            "/api/v1/gateway/ghost-runner/probe",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "runner_scope_violation"}


@pytest.mark.asyncio
async def test_require_runner_rejects_non_runner_token(client: TestClient) -> None:
    """A non-runner-kind token on a gateway guard route is 403 (require_runner)."""
    await _seed()
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _user_token(key)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        response = client.get(
            "/api/v1/gateway/runner-a/probe",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "runner_scope_violation"}


@pytest.mark.asyncio
async def test_cage_blocks_runner_on_non_gateway_route(client: TestClient) -> None:
    """A runner-kind token on a non-gateway route is fail-closed 403 by the cage."""
    await _seed()
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _runner_token(key, runner_id=_RUNNER_A_ID)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        response = client.get(
            "/api/v1/elsewhere",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "runner_scope_violation"}


@pytest.mark.asyncio
async def test_cage_allows_runner_on_gateway_prefix(client: TestClient) -> None:
    """The cage admits a runner-kind token on an allowlisted gateway prefix."""
    await _seed()
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _runner_token(key, runner_id=_RUNNER_A_ID)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        response = client.get(
            "/api/v1/gateway/open",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"kind": "runner"}


@pytest.mark.asyncio
async def test_cage_does_not_touch_non_runner_token(client: TestClient) -> None:
    """A non-runner token is unaffected by the cage on a non-gateway route (regression)."""
    await _seed()
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _user_token(key)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        response = client.get(
            "/api/v1/elsewhere",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"kind": "user"}
