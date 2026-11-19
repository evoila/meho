# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A live announcement stream ends when the login that opened it ends.

* ``GET /api/v1/feed`` ends at the token's ``exp`` plus the JWT leeway,
  and after ``feed_stream_max_seconds`` at the latest.
* ``GET /ui/broadcast/stream`` checks its session again; a revoked
  session ends the stream before the next announcement, and a valid
  session keeps getting announcements across several checks.

The stream tests go through ``httpx.ASGITransport``. It hands back the
response only when the app has finished it, so a request that returns at
all proves that the stream ended.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
import warnings
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from httpx import ASGITransport

from meho_backplane.api.v1.feed import FeedStart, announcement_feed_frames, token_liveness
from meho_backplane.api.v1.feed import router as feed_router
from meho_backplane.auth.jwt import _extract_exp, clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    archive_reader,
    reset_broadcast_blocking_client_for_testing,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast.archive_reader import AnnouncementFilter
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import clear_discovery_cache, reset_verifier_store_for_testing
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.auth.revalidation import reset_read_revalidation_cache_for_testing
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
    revoke_session,
)
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.broadcast import stream as stream_module
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests.broadcast_admission_helpers import seed_announcement

_TENANT = uuid.UUID("55555555-5555-5555-5555-555555555555")
_SUB = "op-stream-end"

#: Upper bound for a request whose stream must end; a stream that never
#: ends fails the test here instead of hanging the run.
_END_TIMEOUT_S = 15.0


def _reset() -> None:
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()
    reset_read_revalidation_cache_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _quiet_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Valkey wake-up that only times out, so the archive wait polls quickly."""

    async def _quiet_xread(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(0.01)

    class _Wake:
        xrevrange = AsyncMock(return_value=[("1700000000000-1", {})])
        xread = AsyncMock(side_effect=_quiet_xread)

    wake = _Wake()
    monkeypatch.setattr(archive_reader, "get_broadcast_client", lambda: wake)
    monkeypatch.setattr(archive_reader, "get_broadcast_blocking_client", lambda: wake)


def _keypair() -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return make_rsa_keypair(f"stream-end-{uuid.uuid4().hex[:8]}")


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver")


def _feed_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(feed_router)
    return app


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _later(delay: float, activity: str) -> None:
    await asyncio.sleep(delay)
    await seed_announcement(_TENANT, activity=activity)


# ---------------------------------------------------------------------------
# GET /api/v1/feed
# ---------------------------------------------------------------------------


async def test_feed_stream_ends_when_the_token_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token with ``exp`` in 3 s: nothing is sent after ``exp`` + leeway; the stream ends."""
    from tests._vault_fakes import install_fake_vault

    install_fake_vault(monkeypatch)
    leeway = 1
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", str(leeway))
    get_settings.cache_clear()
    key = _keypair()
    exp = int(time.time()) + 3
    token = mint_token(
        key, sub=_SUB, tenant_id=str(_TENANT), tenant_role="operator", extra_claims={"exp": exp}
    )
    await seed_announcement(_TENANT, activity="stored before the stream")
    end_at = exp + leeway

    with respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        live = asyncio.ensure_future(_later(0.5, "sent while the token is valid"))
        late = asyncio.ensure_future(_later(end_at - time.time() + 0.5, "sent after expiry"))
        async with _client(_feed_app()) as client, asyncio.timeout(_END_TIMEOUT_S):
            response = await client.get("/api/v1/feed", headers=_bearer(token))
        ended_at = time.time()
        await live
        await late

    assert response.status_code == 200
    body = response.text
    assert "stored before the stream" in body
    assert "sent while the token is valid" in body
    assert "sent after expiry" not in body
    # The token expiry ended the stream: it ended close to exp plus leeway.
    assert end_at - 1.0 <= ended_at <= end_at + 2.0


async def test_feed_stream_with_a_long_token_ends_after_the_longest_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token valid for an hour: the stream still ends after ``feed_stream_max_seconds``."""
    from tests._vault_fakes import install_fake_vault

    install_fake_vault(monkeypatch)
    monkeypatch.setenv("FEED_STREAM_MAX_SECONDS", "1")
    get_settings.cache_clear()
    key = _keypair()
    token = mint_token(key, sub=_SUB, tenant_id=str(_TENANT), tenant_role="operator")

    with respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        started = time.monotonic()
        async with _client(_feed_app()) as client, asyncio.timeout(_END_TIMEOUT_S):
            response = await client.get("/api/v1/feed", headers=_bearer(token))
        took = time.monotonic() - started

    assert response.status_code == 200
    assert 0.9 <= took <= 5.0


# ---------------------------------------------------------------------------
# GET /ui/broadcast/stream
# ---------------------------------------------------------------------------


def _ui_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    return app


async def _seed_session(key: Any) -> uuid.UUID:
    """The tenant and a console session whose access token is a real operator JWT."""
    token = mint_token(key, sub=_SUB, tenant_id=str(_TENANT), tenant_role="operator")
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=_TENANT, slug="stream-end", name="Stream end"))
    async with get_sessionmaker()() as session, session.begin():
        decrypted = await create_session(
            session,
            operator_sub=_SUB,
            tenant_id=_TENANT,
            access_token=token,
            refresh_token="made-up-refresh-token",
            lifetime=timedelta(hours=1),
        )
    return decrypted.id


async def _revoke_then_announce(session_id: uuid.UUID) -> None:
    await asyncio.sleep(0.5)
    async with get_sessionmaker()() as session, session.begin():
        await revoke_session(session, session_id)
    await seed_announcement(_TENANT, activity="sent after the sign-out")


async def test_console_stream_ends_after_the_session_is_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revoked session gets no new announcement after the next check; the stream ends."""
    monkeypatch.setenv("UI_SESSION_READ_REVALIDATION_SECONDS", "0")
    get_settings.cache_clear()
    key = _keypair()
    session_id = await _seed_session(key)
    await seed_announcement(_TENANT, activity="sent before the sign-out")

    with respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        revoke = asyncio.ensure_future(_revoke_then_announce(session_id))
        async with _client(_ui_app()) as client, asyncio.timeout(_END_TIMEOUT_S):
            client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
            response = await client.get("/ui/broadcast/stream")
        await revoke

    assert response.status_code == 200
    assert "sent before the sign-out" in response.text
    assert "sent after the sign-out" not in response.text


async def test_console_stream_keeps_a_valid_session_across_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid session gets every announcement, with a session check before each one."""
    monkeypatch.setenv("UI_SESSION_READ_REVALIDATION_SECONDS", "0")
    get_settings.cache_clear()
    key = _keypair()
    session_id = await _seed_session(key)
    ctx = UISessionContext(session_id=session_id, operator_sub=_SUB, tenant_id=_TENANT)
    checks: list[uuid.UUID] = []
    real_check = stream_module.require_ui_operator

    async def _counting_check(request: Request, session: UISessionContext) -> UISessionContext:
        checks.append(session.session_id)
        return await real_check(request, session)

    monkeypatch.setattr(stream_module, "require_ui_operator", _counting_check)
    liveness = stream_module.session_liveness(Request({"type": "http", "headers": []}), ctx)
    frames = announcement_feed_frames(
        stream_module.session_reader(ctx),
        start=FeedStart(after=0),
        flt=AnnouncementFilter(),
        surface="ui",
        liveness=liveness,
    )
    received: list[str] = []

    async def _read_three() -> None:
        async for frame in frames:
            received.append(frame)
            if len(received) == 3:
                return

    with respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        reader = asyncio.ensure_future(_read_three())
        try:
            for index in range(3):
                await asyncio.sleep(0.2)
                await seed_announcement(_TENANT, activity=f"step {index}")
            async with asyncio.timeout(_END_TIMEOUT_S):
                await reader
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            await frames.aclose()  # type: ignore[attr-defined]

    assert [f"step {index}" in frame for index, frame in enumerate(received)] == [True] * 3
    assert len(checks) >= 3


# ---------------------------------------------------------------------------
# Edge inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "want"),
    [
        ({"exp": 1_900_000_000}, 1_900_000_000),
        ({"exp": 1_900_000_000.7}, 1_900_000_000),
        ({"exp": True}, None),
        ({"exp": "1900000000"}, None),
        ({}, None),
    ],
)
def test_exp_claim_is_read_as_whole_seconds(claims: dict[str, object], want: int | None) -> None:
    assert _extract_exp(claims) == want


def test_stream_without_a_token_expiry_ends_after_the_longest_time() -> None:
    """An Operator without ``token_exp`` still gets the fixed longest time."""
    operator = Operator(
        sub=_SUB, raw_jwt="<test-raw-jwt>", tenant_id=_TENANT, tenant_role=TenantRole.OPERATOR
    )
    before = time.monotonic()
    liveness = token_liveness(operator)
    assert before + 3600 <= liveness.ends_at <= time.monotonic() + 3600


def test_stream_ends_at_the_earlier_of_expiry_and_longest_time() -> None:
    operator = Operator(
        sub=_SUB,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        token_exp=int(time.time()) + 60,
    )
    left = token_liveness(operator).ends_at - time.monotonic()
    # 60 s to expiry plus the default 30 s leeway, far below the hour.
    assert 85 <= left <= 91
