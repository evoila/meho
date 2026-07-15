# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Coverage for the outbound long-poll gateway command plane (#2415, #2498).

Two layers:

* **Route edge** — the runner-facing ``GET /api/v1/gateway/{runner}/next``
  and ``POST /api/v1/gateway/{runner}/result`` over a minimal app carrying
  the real ``RequestContextMiddleware`` (so #2502's route cage + runner
  guard run for real). Tokens are minted with the shared
  ``_oidc_jwt_helpers`` runner knobs; JWKS discovery is stubbed via
  :mod:`respx`. App requests ride an explicit :class:`ASGITransport`
  (which respx does not intercept), so the app's internal JWKS fetch is
  the only thing respx mocks.
* **Service** — :mod:`meho_backplane.gateway.queue`'s enqueue / claim /
  record functions directly, for the concurrency (exactly-one-claimer),
  tenant-isolation, and clamp assertions that are cleaner without HTTP.

The ``gateway_command`` table is created by migration ``0059``, which the
per-test schema template replays, so the autouse ``_default_database_url``
DB has it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

import meho_backplane.api.v1.gateway as gateway_route
from meho_backplane.api.v1.gateway import router as gateway_router
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GatewayCommand, GatewayCommandStatus, RunnerPrincipal, Tenant
from meho_backplane.gateway.queue import (
    GATEWAY_LONGPOLL_MAX_WAIT_SECONDS,
    GatewayCommandNotDeliveredError,
    GatewayCommandNotFoundError,
    claim_next_command,
    clamp_longpoll_wait,
    enqueue_command,
    record_result,
)
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

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_RUNNER_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_RUNNER_B_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_RUNNER_A_NAME = "runner-a"
_RUNNER_B_NAME = "runner-b"

_TARGET_DESCRIPTOR = {"name": "vc01", "product": "vmware-vcenter", "version": "8.0"}


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


def _build_app() -> FastAPI:
    """A minimal app mounting the gateway router under the real middleware."""
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(gateway_router)
    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="https://testserver",
    ) as ac:
        yield ac


async def _seed() -> None:
    """Seed two tenants + runner-a / runner-b (both in tenant A)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for tid, slug in ((_TENANT_A, "tenant-a"), (_TENANT_B, "tenant-b")):
            if (
                await session.execute(select(Tenant).where(Tenant.id == tid))
            ).scalar_one_or_none() is None:
                session.add(Tenant(id=tid, slug=slug, name=slug))
        for rid, rname in ((_RUNNER_A_ID, _RUNNER_A_NAME), (_RUNNER_B_ID, _RUNNER_B_NAME)):
            if (
                await session.execute(select(RunnerPrincipal).where(RunnerPrincipal.id == rid))
            ).scalar_one_or_none() is None:
                session.add(
                    RunnerPrincipal(
                        id=rid,
                        tenant_id=_TENANT_A,
                        name=rname,
                        keycloak_client_id=f"runner:{rname}",
                        keycloak_internal_id=f"kc-{rname}",
                        owner_sub="op-admin",
                        revoked=False,
                        created_by_sub="op-admin",
                    )
                )
        await session.commit()


def _runner_token(key: object, *, runner_id: uuid.UUID, tenant_id: uuid.UUID = _TENANT_A) -> str:
    return mint_token(
        key,
        sub="runner-sub",
        tenant_id=str(tenant_id),
        tenant_role="read_only",
        principal_kind="runner",
        runner_id=str(runner_id),
    )


def _user_token(key: object) -> str:
    return mint_token(key, sub="user-sub", tenant_id=str(_TENANT_A), tenant_role="operator")


async def _enqueue(
    *,
    tenant_id: uuid.UUID,
    runner_id: str,
    op_id: str = "net.ping",
    params: dict[str, object] | None = None,
    target_descriptor: dict[str, object] | None = None,
) -> uuid.UUID:
    """Enqueue one command (committed) and return its id."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        command = await enqueue_command(
            session,
            tenant_id=tenant_id,
            runner_id=runner_id,
            op_id=op_id,
            params=params if params is not None else {"host": "10.0.0.1"},
            enqueued_by_sub="enqueuer-sub",
            target_descriptor=target_descriptor,
        )
        await session.commit()
        return command.id


async def _get_row(command_id: uuid.UUID) -> GatewayCommand | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return await session.get(GatewayCommand, command_id)


# ---------------------------------------------------------------------------
# GET /next — claim + hold behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_empty_queue_immediate_204(client: httpx.AsyncClient) -> None:
    """An empty queue with ``wait=0`` returns ``204`` after one claim attempt."""
    await _seed()
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _runner_token(key, runner_id=_RUNNER_A_ID)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        resp = await client.get(
            f"/api/v1/gateway/{_RUNNER_A_NAME}/next?wait=0",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 204
    assert resp.content == b""


@pytest.mark.asyncio
async def test_next_returns_envelope_and_marks_delivered(client: httpx.AsyncClient) -> None:
    """Enqueue then ``next?wait=0`` returns the envelope; the row is delivered."""
    await _seed()
    command_id = await _enqueue(
        tenant_id=_TENANT_A,
        runner_id=_RUNNER_A_NAME,
        op_id="net.ping",
        params={"host": "10.0.0.1"},
        target_descriptor=_TARGET_DESCRIPTOR,
    )
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _runner_token(key, runner_id=_RUNNER_A_ID)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        resp = await client.get(
            f"/api/v1/gateway/{_RUNNER_A_NAME}/next?wait=0",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(command_id)
    assert body["op_id"] == "net.ping"
    assert body["params"] == {"host": "10.0.0.1"}
    assert body["target_descriptor"] == _TARGET_DESCRIPTOR

    row = await _get_row(command_id)
    assert row is not None
    assert row.status == GatewayCommandStatus.DELIVERED.value
    assert row.delivered_at is not None


@pytest.mark.asyncio
async def test_next_long_poll_wakes_on_enqueue_before_deadline(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held ``wait=5`` poll returns ``200`` when a command is enqueued mid-hold."""
    await _seed()
    # Tight poll interval so the hold reacts fast + the test stays quick.
    monkeypatch.setattr(gateway_route, "_CLAIM_POLL_INTERVAL_SECONDS", 0.05)
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _runner_token(key, runner_id=_RUNNER_A_ID)

    async def _enqueue_after_delay() -> uuid.UUID:
        await asyncio.sleep(0.3)
        return await _enqueue(tenant_id=_TENANT_A, runner_id=_RUNNER_A_NAME)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        started = time.monotonic()
        poll_task = asyncio.create_task(
            client.get(
                f"/api/v1/gateway/{_RUNNER_A_NAME}/next?wait=5",
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        command_id = await _enqueue_after_delay()
        resp = await poll_task
        elapsed = time.monotonic() - started

    assert resp.status_code == 200
    assert resp.json()["id"] == str(command_id)
    assert elapsed < 5.0, (
        f"poll should return on enqueue, not at the deadline (took {elapsed:.2f}s)"
    )


# ---------------------------------------------------------------------------
# POST /result — report lifecycle + status-code split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_records_outcome_then_conflicts_on_replay(
    client: httpx.AsyncClient,
) -> None:
    """A delivered command accepts one result (200) then 409s on a replay."""
    await _seed()
    command_id = await _enqueue(tenant_id=_TENANT_A, runner_id=_RUNNER_A_NAME)
    # Claim it (pending -> delivered) so a result is accepted.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await claim_next_command(session, tenant_id=_TENANT_A, runner_id=_RUNNER_A_NAME)
        await session.commit()

    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _runner_token(key, runner_id=_RUNNER_A_ID)
    payload = {
        "command_id": str(command_id),
        "outcome": "succeeded",
        "result": {"reachable": True},
    }

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        first = await client.post(
            f"/api/v1/gateway/{_RUNNER_A_NAME}/result",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        second = await client.post(
            f"/api/v1/gateway/{_RUNNER_A_NAME}/result",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

    assert first.status_code == 200
    assert first.json()["status"] == "succeeded"
    assert second.status_code == 409

    row = await _get_row(command_id)
    assert row is not None
    assert row.status == GatewayCommandStatus.SUCCEEDED.value
    assert row.result == {"reachable": True}
    assert row.completed_at is not None


@pytest.mark.asyncio
async def test_result_foreign_command_returns_404(client: httpx.AsyncClient) -> None:
    """A result naming a command enqueued for another runner is 404 (no oracle)."""
    await _seed()
    # Enqueue + deliver a command for runner-b, then POST it as runner-a.
    command_id = await _enqueue(tenant_id=_TENANT_A, runner_id=_RUNNER_B_NAME)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await claim_next_command(session, tenant_id=_TENANT_A, runner_id=_RUNNER_B_NAME)
        await session.commit()

    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _runner_token(key, runner_id=_RUNNER_A_ID)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        resp = await client.post(
            f"/api/v1/gateway/{_RUNNER_A_NAME}/result",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_id": str(command_id), "outcome": "succeeded"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "gateway_command_not_found"


# ---------------------------------------------------------------------------
# Auth matrix — the runner-scope guard (#2502) on both routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_non_runner_token_forbidden(client: httpx.AsyncClient) -> None:
    """A human operator JWT is 403 on the poll route (require_runner)."""
    await _seed()
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _user_token(key)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        resp = await client.get(
            f"/api/v1/gateway/{_RUNNER_A_NAME}/next?wait=0",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "runner_scope_violation"


@pytest.mark.asyncio
async def test_result_non_runner_token_forbidden(client: httpx.AsyncClient) -> None:
    """A human operator JWT is 403 on the result route too (require_runner)."""
    await _seed()
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _user_token(key)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        resp = await client.post(
            f"/api/v1/gateway/{_RUNNER_A_NAME}/result",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_id": str(uuid.uuid4()), "outcome": "succeeded"},
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "runner_scope_violation"


@pytest.mark.asyncio
async def test_next_runner_scope_mismatch_forbidden(client: httpx.AsyncClient) -> None:
    """A runner token for runner-a on runner-b's route is 403 runner_scope_violation."""
    await _seed()
    key = make_rsa_keypair("kid-A")
    jwks = public_jwks(key)
    token = _runner_token(key, runner_id=_RUNNER_A_ID)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        resp = await client.get(
            f"/api/v1/gateway/{_RUNNER_B_NAME}/next?wait=0",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "runner_scope_violation"


# ---------------------------------------------------------------------------
# Service layer — clamp, concurrency, tenant isolation
# ---------------------------------------------------------------------------


def test_clamp_longpoll_wait_bounds() -> None:
    """``clamp_longpoll_wait`` floors at 0 and caps at the exported ceiling."""
    ceiling = GATEWAY_LONGPOLL_MAX_WAIT_SECONDS
    assert clamp_longpoll_wait(0) == 0
    assert clamp_longpoll_wait(10) == 10
    assert clamp_longpoll_wait(ceiling) == ceiling
    assert clamp_longpoll_wait(3600) == ceiling
    assert clamp_longpoll_wait(ceiling + 1) == ceiling
    assert clamp_longpoll_wait(-5) == 0


@pytest.mark.asyncio
async def test_two_concurrent_claimers_exactly_one_wins() -> None:
    """One pending command + two concurrent claimers -> exactly one gets it."""
    await _seed()
    await _enqueue(tenant_id=_TENANT_A, runner_id=_RUNNER_A_NAME)

    sessionmaker = get_sessionmaker()

    async def _claim_once() -> bool:
        async with sessionmaker() as session:
            row = await claim_next_command(session, tenant_id=_TENANT_A, runner_id=_RUNNER_A_NAME)
            await session.commit()
            return row is not None

    results = await asyncio.gather(_claim_once(), _claim_once())
    assert sum(results) == 1, f"expected exactly one claimer to win, got {results}"


@pytest.mark.asyncio
async def test_claim_is_tenant_scoped() -> None:
    """A pending command for tenant B is invisible to a tenant-A claim (204-equiv)."""
    await _seed()
    await _enqueue(tenant_id=_TENANT_B, runner_id=_RUNNER_A_NAME)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await claim_next_command(session, tenant_id=_TENANT_A, runner_id=_RUNNER_A_NAME)
    assert row is None, "a tenant-A claim must never see tenant B's command"


@pytest.mark.asyncio
async def test_record_result_cross_tenant_is_not_found() -> None:
    """Reporting a command under the wrong tenant is not-found (404-equiv)."""
    await _seed()
    command_id = await _enqueue(tenant_id=_TENANT_B, runner_id=_RUNNER_A_NAME)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await claim_next_command(session, tenant_id=_TENANT_B, runner_id=_RUNNER_A_NAME)
        await session.commit()

    async with sessionmaker() as session:
        with pytest.raises(GatewayCommandNotFoundError):
            await record_result(
                session,
                tenant_id=_TENANT_A,
                runner_id=_RUNNER_A_NAME,
                command_id=command_id,
                outcome=GatewayCommandStatus.SUCCEEDED,
            )


@pytest.mark.asyncio
async def test_record_result_on_pending_conflicts() -> None:
    """A never-claimed (pending) command rejects a result with a conflict (409-equiv)."""
    await _seed()
    command_id = await _enqueue(tenant_id=_TENANT_A, runner_id=_RUNNER_A_NAME)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with pytest.raises(GatewayCommandNotDeliveredError):
            await record_result(
                session,
                tenant_id=_TENANT_A,
                runner_id=_RUNNER_A_NAME,
                command_id=command_id,
                outcome=GatewayCommandStatus.FAILED,
                error="boom",
            )
