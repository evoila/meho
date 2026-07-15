# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the gateway runner dead-man switch (#2415, #2501).

Three layers:

* **Sweeper** — :func:`meho_backplane.gateway.deadman._run_one_tick` driven
  directly against seeded rows (reaper / memory-expiry test mould): a
  lapsed runner's ``runner_assignments`` row flips to ``stale_at`` with one
  audit row, a fresh runner is untouched, a second tick is a no-op,
  concurrency yields exactly one audit row, and an accepted result batch
  clears the flip.
* **Heartbeat** — the four runner-plane endpoints
  (#2498 ``GET .../next`` + ``POST .../result``, #2499 ``GET
  /checks/assignment`` + ``POST /checks/results``) over the real middleware
  + runner guard: each strictly advances ``runner_principal.last_seen_at``
  on the central clock, and the stamp is never client-controlled.
* **Idle end-to-end** — the landed runner tick loop drives an authenticated
  poll against a stub central even when idle; the idle work cycle *is* the
  heartbeat (the #1501 lesson), with no dedicated ping endpoint.

The ``last_seen_at`` / ``stale_at`` columns are created by migration
``0061``, which the per-test schema template replays, so the autouse
``_default_database_url`` DB has them.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.api.v1.checks import router as checks_router
from meho_backplane.api.v1.gateway import router as gateway_router
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    RunnerAssignmentRow,
    RunnerPrincipal,
    Tenant,
)
from meho_backplane.gateway.deadman import (
    GATEWAY_RUNNER_STALE_PATH,
    _run_one_tick,
    _threshold_seconds,
    start_gateway_deadman_sweeper,
    stop_gateway_deadman_sweeper,
)
from meho_backplane.gateway.queue import (
    claim_next_command,
    enqueue_command,
)
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.loop import RunnerState, run_one_tick
from meho_backplane.runner.spool import ResultSpool
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_RUNNER_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
_RUNNER_NAME = "runner-dm"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires + reset caches per test."""
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


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_runner(
    *,
    tenant_id: uuid.UUID,
    runner_id: uuid.UUID,
    name: str,
    last_seen_age_seconds: float,
    tenant_slug: str,
    with_assignment: bool = True,
) -> None:
    """Seed a tenant + runner principal (``last_seen_at`` back-dated) + assignment."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=tenant_id, slug=tenant_slug, name=tenant_slug))
        session.add(
            RunnerPrincipal(
                id=runner_id,
                tenant_id=tenant_id,
                name=name,
                keycloak_client_id=f"runner:{name}:{runner_id}",
                keycloak_internal_id=f"kc-{runner_id}",
                owner_sub="op-admin",
                created_by_sub="op-admin",
                last_seen_at=datetime.now(UTC) - timedelta(seconds=last_seen_age_seconds),
            )
        )
        if with_assignment:
            session.add(
                RunnerAssignmentRow(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    runner_name=name,
                    items=[],
                    stale_at=None,
                )
            )
        await session.commit()


async def _stale_at(tenant_id: uuid.UUID, runner_name: str) -> datetime | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stale: datetime | None = await session.scalar(
            select(RunnerAssignmentRow.stale_at).where(
                RunnerAssignmentRow.tenant_id == tenant_id,
                RunnerAssignmentRow.runner_name == runner_name,
            )
        )
    return stale


async def _stale_audit_rows(runner_name: str | None = None) -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.path == GATEWAY_RUNNER_STALE_PATH)
                )
            )
            .scalars()
            .all()
        )
    if runner_name is None:
        return list(rows)
    return [r for r in rows if r.payload.get("runner") == runner_name]


def _lapsed_age() -> float:
    """A ``last_seen_at`` age comfortably past the stale threshold."""
    return float(_threshold_seconds() + 60)


# ---------------------------------------------------------------------------
# Sweeper — flip / no-flip / idempotency / recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lapsed_runner_flips_stale_and_audits() -> None:
    """A runner past ``multiplier x cadence`` flips ``stale_at`` + one audit row.

    AC (a): one tick sets ``stale_at`` on the ``runner_assignments`` row AND
    writes exactly one audit row with ``method='INTERNAL'``,
    ``path='gateway.runner.stale'``, payload carrying the runner name +
    lapse seconds.
    """
    tenant = uuid.uuid4()
    await _seed_runner(
        tenant_id=tenant,
        runner_id=uuid.uuid4(),
        name="lapsed-a",
        last_seen_age_seconds=_lapsed_age(),
        tenant_slug=f"t-{tenant.hex[:8]}",
    )

    await _run_one_tick()

    stale_at = await _stale_at(tenant, "lapsed-a")
    assert stale_at is not None, "the lapsed runner's assignment row must be flipped"

    audits = await _stale_audit_rows("lapsed-a")
    assert len(audits) == 1
    audit = audits[0]
    assert audit.method == "INTERNAL"
    assert audit.path == "gateway.runner.stale"
    assert audit.status_code == 200
    assert audit.payload["runner"] == "lapsed-a"
    # Lapse seconds is central-clock derived and at least the threshold.
    assert audit.payload["lapse_seconds"] >= float(_threshold_seconds())


@pytest.mark.asyncio
async def test_fresh_runner_is_untouched() -> None:
    """AC (b): a runner seen within the threshold is not flipped and not audited."""
    tenant = uuid.uuid4()
    await _seed_runner(
        tenant_id=tenant,
        runner_id=uuid.uuid4(),
        name="fresh-a",
        last_seen_age_seconds=0.0,
        tenant_slug=f"t-{tenant.hex[:8]}",
    )

    await _run_one_tick()

    assert await _stale_at(tenant, "fresh-a") is None
    assert await _stale_audit_rows("fresh-a") == []


@pytest.mark.asyncio
async def test_second_tick_is_a_no_op() -> None:
    """AC (c): an immediate second tick flips nothing and writes zero extra audit rows."""
    tenant = uuid.uuid4()
    await _seed_runner(
        tenant_id=tenant,
        runner_id=uuid.uuid4(),
        name="lapsed-b",
        last_seen_age_seconds=_lapsed_age(),
        tenant_slug=f"t-{tenant.hex[:8]}",
    )

    await _run_one_tick()
    first_stale = await _stale_at(tenant, "lapsed-b")
    assert first_stale is not None

    await _run_one_tick()
    # The ``stale_at IS NULL`` filter makes the second tick a natural no-op:
    # the marker is unchanged and no additional audit row is written.
    assert await _stale_at(tenant, "lapsed-b") == first_stale
    assert len(await _stale_audit_rows("lapsed-b")) == 1


@pytest.mark.asyncio
async def test_two_concurrent_ticks_write_exactly_one_audit_row() -> None:
    """Multi-replica safety: two concurrent ticks flip once and audit once.

    The advisory lock is a no-op on SQLite, so exactness rests on the
    conditional ``UPDATE ... WHERE stale_at IS NULL`` + ``rowcount`` gate.
    """
    tenant = uuid.uuid4()
    await _seed_runner(
        tenant_id=tenant,
        runner_id=uuid.uuid4(),
        name="lapsed-c",
        last_seen_age_seconds=_lapsed_age(),
        tenant_slug=f"t-{tenant.hex[:8]}",
    )

    await asyncio.gather(_run_one_tick(), _run_one_tick())

    assert await _stale_at(tenant, "lapsed-c") is not None
    assert len(await _stale_audit_rows("lapsed-c")) == 1


@pytest.mark.asyncio
async def test_result_ingest_clears_stale_and_leaves_others_flipped() -> None:
    """AC (d): an accepted result clears the reporter's flip; a sibling stays flipped.

    Recovery is data-driven — :func:`clear_runner_stale` (called from the
    result-ingest path) is the only clear; the sweeper never un-flips.
    """
    tenant = uuid.uuid4()
    slug = f"t-{tenant.hex[:8]}"
    await _seed_runner(
        tenant_id=tenant,
        runner_id=uuid.uuid4(),
        name="recover",
        last_seen_age_seconds=_lapsed_age(),
        tenant_slug=slug,
    )
    await _seed_runner(
        tenant_id=tenant,
        runner_id=uuid.uuid4(),
        name="still-stale",
        last_seen_age_seconds=_lapsed_age(),
        tenant_slug=slug,
    )

    await _run_one_tick()
    assert await _stale_at(tenant, "recover") is not None
    assert await _stale_at(tenant, "still-stale") is not None

    # An accepted result ingestion from ``recover`` clears only its marker.
    from meho_backplane.gateway.deadman import clear_runner_stale

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await clear_runner_stale(session, tenant_id=tenant, runner_name="recover")
        await session.commit()

    assert await _stale_at(tenant, "recover") is None
    assert await _stale_at(tenant, "still-stale") is not None


@pytest.mark.asyncio
async def test_central_clock_only_no_runner_timestamp() -> None:
    """Central-clock discipline: the flip ignores any runner-reported time.

    A runner whose ``last_seen_at`` is future-dated far past the cutoff is
    not flipped — proving the cutoff is judged on ``last_seen_at`` (the
    central stamp) alone. There is no runner-reported timestamp the sweeper
    could consult; this pins the intent behaviourally.
    """
    tenant = uuid.uuid4()
    await _seed_runner(
        tenant_id=tenant,
        runner_id=uuid.uuid4(),
        name="future-clock",
        last_seen_age_seconds=-3600.0,  # last_seen_at an hour in the future
        tenant_slug=f"t-{tenant.hex[:8]}",
    )

    await _run_one_tick()

    assert await _stale_at(tenant, "future-clock") is None


# ---------------------------------------------------------------------------
# Heartbeat — the four runner-plane endpoints stamp last_seen_at
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Minimal app mounting both runner-plane routers under real middleware."""
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(gateway_router)
    app.include_router(checks_router)
    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="https://testserver",
    ) as ac:
        yield ac


def _runner_token(key: object) -> str:
    return mint_token(
        key,
        sub="runner-sub",
        tenant_id=str(_TENANT),
        tenant_role="read_only",
        principal_kind="runner",
        runner_id=str(_RUNNER_ID),
    )


async def _read_last_seen() -> datetime:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        value: datetime | None = await session.scalar(
            select(RunnerPrincipal.last_seen_at).where(RunnerPrincipal.id == _RUNNER_ID)
        )
    assert value is not None
    return value


@pytest.mark.asyncio
async def test_all_four_runner_plane_endpoints_stamp_last_seen(
    client: httpx.AsyncClient,
) -> None:
    """AC: each of the four runner-plane endpoints strictly advances ``last_seen_at``.

    The single choke-point (``assert_runner_scope``) stamps on the central
    clock as a side effect of every authenticated runner-plane request.
    """
    await _seed_runner(
        tenant_id=_TENANT,
        runner_id=_RUNNER_ID,
        name=_RUNNER_NAME,
        last_seen_age_seconds=3600.0,  # start an hour stale so advances are obvious
        tenant_slug="tenant-hb",
    )
    # A delivered command so POST /gateway/.../result returns 200.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        command = await enqueue_command(
            session,
            tenant_id=_TENANT,
            runner_id=_RUNNER_NAME,
            op_id="net.ping",
            params={"host": "10.0.0.1"},
            enqueued_by_sub="enqueuer",
            target_descriptor=None,
        )
        await claim_next_command(session, tenant_id=_TENANT, runner_id=_RUNNER_NAME)
        await session.commit()
        command_id = command.id

    key = make_rsa_keypair("kid-hb")
    jwks = public_jwks(key)
    token = _runner_token(key)
    headers = {"Authorization": f"Bearer {token}"}

    async def _assert_advances(call: object, label: str) -> None:
        before = await _read_last_seen()
        await asyncio.sleep(0.01)  # guarantee a distinct central-clock tick
        resp = await call()  # type: ignore[operator]
        assert resp.status_code not in (401, 403), f"{label}: guard rejected ({resp.status_code})"
        after = await _read_last_seen()
        assert after > before, f"{label}: last_seen_at did not advance ({before} -> {after})"

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)

        await _assert_advances(
            lambda: client.get(f"/api/v1/gateway/{_RUNNER_NAME}/next?wait=0", headers=headers),
            "gateway.next",
        )
        await _assert_advances(
            lambda: client.post(
                f"/api/v1/gateway/{_RUNNER_NAME}/result",
                headers=headers,
                json={"command_id": str(command_id), "outcome": "succeeded", "result": {}},
            ),
            "gateway.result",
        )
        await _assert_advances(
            lambda: client.get(f"/api/v1/checks/assignment?runner={_RUNNER_NAME}", headers=headers),
            "checks.assignment",
        )
        await _assert_advances(
            lambda: client.post(
                "/api/v1/checks/results",
                headers=headers,
                json={"runner_id": _RUNNER_NAME, "results": []},
            ),
            "checks.results",
        )


@pytest.mark.asyncio
async def test_last_seen_is_never_client_controlled(client: httpx.AsyncClient) -> None:
    """AC: the stamp is keyed by the token claim and reads no request field.

    A bogus ``last_seen_at`` query field is ignored; the stored value is the
    fresh central stamp, not the 1999 value the client supplied.
    """
    await _seed_runner(
        tenant_id=_TENANT,
        runner_id=_RUNNER_ID,
        name=_RUNNER_NAME,
        last_seen_age_seconds=3600.0,
        tenant_slug="tenant-hb",
    )
    key = make_rsa_keypair("kid-hb")
    jwks = public_jwks(key)
    headers = {"Authorization": f"Bearer {_runner_token(key)}"}

    before = await _read_last_seen()
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, jwks)
        resp = await client.get(
            f"/api/v1/gateway/{_RUNNER_NAME}/next?wait=0&last_seen_at=1999-01-01T00:00:00%2B00:00",
            headers=headers,
        )
    assert resp.status_code == 204

    after = await _read_last_seen()
    # The stamp advanced to ~now (not the 1999 value, and not unchanged).
    assert after > before
    assert after.year >= 2026


# ---------------------------------------------------------------------------
# Idle heartbeat end-to-end — the idle cycle is the heartbeat (#1501 lesson)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_runner_issues_authenticated_request_no_dedicated_ping(
    tmp_path: Path,
) -> None:
    """AC: an idle runner still issues an authenticated poll — the heartbeat.

    Drives the landed runner tick loop against a stub central with an empty
    (idle) assignment and asserts the tick lands an authenticated
    ``GET /api/v1/checks/assignment`` — the request the runner guard stamps
    ``last_seen_at`` on — and that no dedicated heartbeat/ping endpoint is
    dialed. The idle work cycle carries the heartbeat; a healthy idle runner
    therefore refreshes ``last_seen_at`` at least once per poll window (the
    ``GATEWAY_LONGPOLL_MAX_WAIT_SECONDS``-anchored threshold gives slack).
    """
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/assignment"):
            # Idle: an empty assignment, no work to execute.
            return httpx.Response(200, json={"assignment_version": "v0", "items": []})
        return httpx.Response(200, json={"accepted": 0, "duplicates": 0})

    client = RunnerClient(
        central_url="https://central.test",
        runner_id="runner-idle",
        token="tok-idle-123",
        transport=httpx.MockTransport(handler),
    )
    state = RunnerState()
    async with client:
        await run_one_tick(
            client=client,
            spool=ResultSpool(tmp_path, max_files=100),
            state=state,
            runner_id="runner-idle",
        )

    assignment_calls = [r for r in seen if r[1].endswith("/assignment")]
    assert len(assignment_calls) == 1, "the idle tick must issue exactly one assignment poll"
    method, _path, auth = assignment_calls[0]
    assert method == "GET"
    assert auth == "Bearer tok-idle-123", "the idle poll must carry the runner's bearer token"
    # No dedicated heartbeat/ping endpoint exists or was dialed — the stamp
    # piggybacks the real work request (the #1501 zombie-heartbeat lesson).
    assert not any("heartbeat" in path or path.endswith("/ping") for _m, path, _a in seen)


# ---------------------------------------------------------------------------
# Lifespan gating — mandatory / default-on
# ---------------------------------------------------------------------------


def test_deadman_setting_default_on() -> None:
    """AC: with ``GATEWAY_DEADMAN_ENABLED`` unset the sweeper is enabled by default."""
    assert get_settings().gateway_deadman_enabled is True


@pytest.mark.asyncio
async def test_deadman_sweeper_start_stop_clean() -> None:
    """AC: the lifespan starts a live sweeper task and cancels it cleanly.

    Mirrors the ``main.py`` gating shape (a task handle exists when enabled)
    and the clean cancel-and-await unwind (no "Task was destroyed but it is
    pending!" warning under pytest-asyncio).
    """
    task = start_gateway_deadman_sweeper()
    try:
        assert isinstance(task, asyncio.Task)
        assert not task.done()
    finally:
        await stop_gateway_deadman_sweeper(task)
    assert task.cancelled() or task.done()
