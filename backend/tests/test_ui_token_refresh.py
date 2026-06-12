# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the BFF inline token-refresh lifecycle (#1694).

Exercises the G0.25 FE-3 acceptance criteria end to end:

* **Reactive leg** -- a live session row holding an *expired* access
  token (the sliding-extension scenario the v0.14.0 dogfood hit)
  silently refreshes through Keycloak's token endpoint and serves the
  request; the row rotates per RFC 9700 § 4.14.
* **Proactive leg** -- with the sliding extension disabled, a row
  within 60 s of ``expires_at`` refreshes before the stored token is
  presented to the JWT chain, and ``expires_at`` extends by the new
  token's ``expires_in`` minus the login margin.
* **Failure mapping** -- ``invalid_grant`` / timeout / network error /
  malformed response each log ``ui_auth_token_refresh_failed`` with
  the contract reason, raise ``401 session_expired``, and the
  app-level handler converts that to a ``302 /ui/auth/login`` +
  cookie-clear for HTML requests while JSON callers keep the
  structured body. Exactly one token-endpoint POST per attempt (no
  retry).
* **Concurrency skip** -- a caller presenting a stale access token
  against an already-rotated row returns the stored fresh pair with
  zero network round-trips.
* **Expiry-cap discipline** -- the refresh-driven ``expires_at``
  extension clamps at ``created_at + ui_session_absolute_lifetime``
  and never moves backwards past a slid expiry.
* **Logout** -- a revoked session never reaches the refresh path.
* **Log hygiene** -- success events carry session_id /
  old_expires_at / new_expires_at / time_cost_ms and no token
  material anywhere.

The autouse fixtures in :mod:`backend.tests.conftest` provide a fresh
file-backed SQLite DB migrated to head per test (PR #898 template
pattern); Keycloak surfaces are respx-mocked.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import update
from starlette.exceptions import HTTPException as StarletteHTTPException
from structlog.testing import capture_logs

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import WebSession
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import (
    SESSION_COOKIE_NAME,
    UISessionContext,
    UISessionMiddleware,
    build_router,
    require_ui_admin,
    ui_session_expired_exception_handler,
)
from meho_backplane.ui.auth.flow import clear_discovery_cache
from meho_backplane.ui.auth.refresh import refresh_session_tokens
from meho_backplane.ui.auth.session_store import (
    create_session,
    load_session,
    load_session_for_update,
    reset_fernet_cache_for_testing,
    revoke_session,
)
from tests.conftest import (
    DEFAULT_AUDIENCE,
    DEFAULT_ISSUER,
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    public_jwks,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


_BACKPLANE_URL = "https://meho.test"
_TOKEN_ENDPOINT = f"{DEFAULT_ISSUER}/protocol/openid-connect/token"
_AUTHORIZATION_ENDPOINT = f"{DEFAULT_ISSUER}/protocol/openid-connect/auth"
_JWKS_URL = f"{DEFAULT_ISSUER}/protocol/openid-connect/certs"
_PROBE_PATH = "/ui/admin-probe"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars and reset every process-level cache.

    Mirrors :mod:`tests.test_ui_auth_flow`'s baseline so each case
    only overrides the knob under test (e.g. the sliding-extension
    window for the proactive-leg cases).
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def _mock_oidc(mock_router: respx.MockRouter, jwks: dict[str, Any]) -> None:
    """Stub discovery + JWKS with the endpoints the refresh path reads."""
    metadata = {
        "issuer": DEFAULT_ISSUER,
        "authorization_endpoint": _AUTHORIZATION_ENDPOINT,
        "token_endpoint": _TOKEN_ENDPOINT,
        "jwks_uri": _JWKS_URL,
    }
    mock_router.get(f"{DEFAULT_ISSUER}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json=metadata),
    )
    mock_router.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks))


def _build_app() -> FastAPI:
    """Minimal app: session middleware + BFF router + #1694 handler + probe.

    The probe route mirrors the production wiring of every UI write
    surface -- ``Depends(require_ui_admin)`` -- so the refresh runs
    through the real dependency chain, and the exception handler is
    registered exactly as :mod:`meho_backplane.main` registers it.
    """
    app = FastAPI()
    app.add_middleware(UISessionMiddleware)
    app.add_exception_handler(StarletteHTTPException, ui_session_expired_exception_handler)
    app.include_router(build_router())

    @app.get(_PROBE_PATH)
    async def admin_probe(
        ctx: UISessionContext = Depends(require_ui_admin),
    ) -> dict[str, str]:
        return {"operator": ctx.operator_sub}

    @app.get("/ui/other-401")
    async def other_401() -> dict[str, str]:
        # Control surface for the handler-scoping test: a 401 whose
        # detail is NOT session_expired must keep FastAPI's stock
        # JSON shape even for HTML requests.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token_expired",
        )

    return app


def _seed_session(
    *,
    access_token: str,
    refresh_token: str = "refresh-token-1",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Insert a ``web_session`` row and return its cookie id."""

    async def _seed() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub="op-77",
                tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
                access_token=access_token,
                refresh_token=refresh_token,
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_seed())


def _load_row(session_id: uuid.UUID) -> Any:
    """Fetch the decrypted session row state for assertions."""

    async def _load() -> Any:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            return await load_session(session, session_id)

    return asyncio.run(_load())


def _admin_jwt(private_key: Any, *, expires_in: int = 3600, sub: str = "op-77") -> str:
    """Mint a tenant_admin JWT accepted by ``require_ui_admin``'s gate."""
    return mint_token(
        private_key,
        sub=sub,
        expires_in=expires_in,
        tenant_role="tenant_admin",
    )


def _refresh_response(access_token: str, *, expires_in: int = 300) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": access_token,
            "refresh_token": "refresh-token-2",
            "expires_in": expires_in,
            "token_type": "Bearer",
        },
    )


def _client(app: FastAPI, session_id: uuid.UUID) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


# ---------------------------------------------------------------------------
# Reactive leg -- the FE-3 dogfood scenario
# ---------------------------------------------------------------------------


def test_expired_access_token_refreshes_silently_and_serves() -> None:
    """AC: expired token inside a live row -> silent refresh -> 200.

    This is the exact v0.14.0 cycle-10 failure: the sliding extension
    keeps the row alive while the ~5-minute access token inside it
    dies. Pre-#1694 the operator got raw JSON 401 ``token_expired``;
    now the request round-trips Keycloak's refresh grant and serves.
    """
    key = make_rsa_keypair("kid-refresh-1")
    stale = _admin_jwt(key, expires_in=-120)  # beyond the 30 s leeway
    fresh = _admin_jwt(key)
    session_id = _seed_session(access_token=stale)

    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc(mock_router, public_jwks(key))
        token_route = mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=_refresh_response(fresh),
        )
        response = _client(_build_app(), session_id).get(_PROBE_PATH)

    assert response.status_code == 200
    assert response.json() == {"operator": "op-77"}
    assert token_route.call_count == 1
    # RFC 6749 § 6 grant shape: grant_type + the stored refresh token.
    form = parse_qs(token_route.calls[0].request.content.decode("ascii"))
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["refresh-token-1"]
    # RFC 9700 § 4.14 rotation: the row now holds the fresh pair.
    rotated = _load_row(session_id)
    assert rotated is not None
    assert rotated.access_token == fresh
    assert rotated.refresh_token == "refresh-token-2"


def test_refresh_success_event_fields_and_no_token_leakage() -> None:
    """AC: success event carries the audit fields; logs carry no tokens."""
    key = make_rsa_keypair("kid-refresh-2")
    stale = _admin_jwt(key, expires_in=-120)
    fresh = _admin_jwt(key)
    session_id = _seed_session(access_token=stale, refresh_token="rt-secret-old")

    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc(mock_router, public_jwks(key))
        mock_router.post(_TOKEN_ENDPOINT).mock(return_value=_refresh_response(fresh))
        with capture_logs() as captured:
            response = _client(_build_app(), session_id).get(_PROBE_PATH)

    assert response.status_code == 200
    succeeded = [e for e in captured if e["event"] == "ui_auth_token_refresh_succeeded"]
    assert len(succeeded) == 1
    event = succeeded[0]
    assert event["session_id"] == str(session_id)
    assert isinstance(event["old_expires_at"], str)
    assert isinstance(event["new_expires_at"], str)
    assert isinstance(event["time_cost_ms"], float)
    # No token material in any captured event: not the stale/fresh
    # access tokens, not either refresh token, not the client secret.
    serialised = repr(captured)
    for secret in (stale, fresh, "rt-secret-old", "refresh-token-2", "test-client-secret"):
        assert secret not in serialised


# ---------------------------------------------------------------------------
# Proactive leg -- row near expires_at (sliding extension disabled)
# ---------------------------------------------------------------------------


def test_proactive_refresh_when_row_near_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: a row within 60 s of ``expires_at`` refreshes pre-verify.

    Sliding extension is pinned to 0 so ``expires_at`` keeps tracking
    the login-time token TTL (the only config where the row clock can
    approach expiry while its token is still valid). The stored token
    is *valid* -- the refresh fires purely off the row's clock -- and
    ``expires_at`` extends by ``expires_in - 60`` from the response.
    """
    monkeypatch.setenv("UI_SESSION_SLIDING_EXTENSION_SECONDS", "0")
    get_settings.cache_clear()
    key = make_rsa_keypair("kid-refresh-3")
    near_expiry_valid = _admin_jwt(key, expires_in=90)
    fresh = _admin_jwt(key)
    session_id = _seed_session(
        access_token=near_expiry_valid,
        lifetime=timedelta(seconds=30),
    )

    before = datetime.now(UTC)
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc(mock_router, public_jwks(key))
        token_route = mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=_refresh_response(fresh, expires_in=300),
        )
        response = _client(_build_app(), session_id).get(_PROBE_PATH)

    assert response.status_code == 200
    assert token_route.call_count == 1
    rotated = _load_row(session_id)
    assert rotated is not None
    assert rotated.access_token == fresh
    # expires_at extended to ~now + (300 - 60); generous tolerance for
    # test-runner scheduling.
    delta = (rotated.expires_at - before).total_seconds()
    assert 200 <= delta <= 260


# ---------------------------------------------------------------------------
# Failure mapping + error-handler contract
# ---------------------------------------------------------------------------


def _run_failed_refresh(
    *,
    token_mock: Any,
    accept: str,
) -> tuple[httpx.Response, list[dict[str, Any]], Any]:
    """Drive one refresh failure; return (response, logs, token route)."""
    key = make_rsa_keypair("kid-refresh-fail")
    stale = _admin_jwt(key, expires_in=-120)
    session_id = _seed_session(access_token=stale)

    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc(mock_router, public_jwks(key))
        if isinstance(token_mock, Exception):
            token_route = mock_router.post(_TOKEN_ENDPOINT).mock(side_effect=token_mock)
        else:
            token_route = mock_router.post(_TOKEN_ENDPOINT).mock(return_value=token_mock)
        client = _client(_build_app(), session_id)
        with capture_logs() as captured:
            response = client.get(_PROBE_PATH, headers={"accept": accept})
    return response, captured, token_route


def _failed_reasons(captured: list[dict[str, Any]]) -> list[str]:
    return [str(e["reason"]) for e in captured if e["event"] == "ui_auth_token_refresh_failed"]


def test_invalid_grant_redirects_html_to_login_and_clears_cookie() -> None:
    """AC: refresh failure on an HTML request -> 302 login + cookie clear.

    Single attempt only (no retry), reason logged as ``invalid_grant``.
    """
    response, captured, token_route = _run_failed_refresh(
        token_mock=httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "Token is not active"},
        ),
        accept="text/html,application/xhtml+xml",
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("/ui/auth/login?return_to=")
    assert parse_qs(urlparse(location).query)["return_to"] == [_PROBE_PATH]
    assert response.headers["cache-control"] == "no-store"
    # Cookie cleared: Max-Age=0 on the meho_session set-cookie.
    set_cookie = response.headers["set-cookie"]
    assert SESSION_COOKIE_NAME in set_cookie
    assert "Max-Age=0" in set_cookie
    # Fail-closed, single attempt.
    assert token_route.call_count == 1
    assert _failed_reasons(captured) == ["invalid_grant"]


def test_refresh_failure_keeps_json_shape_for_non_html_callers() -> None:
    """AC: JSON callers get ``{"detail": "session_expired"}``, not a 302."""
    response, captured, _ = _run_failed_refresh(
        token_mock=httpx.Response(400, json={"error": "invalid_grant"}),
        accept="application/json",
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "session_expired"}
    # The dead cookie is still dropped on the JSON shape.
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert _failed_reasons(captured) == ["invalid_grant"]


@pytest.mark.parametrize(
    ("token_mock", "expected_reason"),
    [
        pytest.param(
            httpx.ConnectTimeout("connection timed out"),
            "timeout",
            id="timeout",
        ),
        pytest.param(
            httpx.Response(502, text="<html>Bad Gateway</html>"),
            "network_error",
            id="network-error",
        ),
        pytest.param(
            httpx.Response(200, json={"token_type": "Bearer"}),
            "malformed_response",
            id="malformed-response",
        ),
    ],
)
def test_refresh_failure_reasons_map_per_contract(
    token_mock: Any,
    expected_reason: str,
) -> None:
    """AC: each failure class logs its structured reason and fails closed."""
    response, captured, token_route = _run_failed_refresh(
        token_mock=token_mock,
        accept="text/html",
    )
    assert response.status_code == 302
    assert token_route.call_count == 1
    assert _failed_reasons(captured) == [expected_reason]


def test_non_session_expired_401_keeps_default_json_shape() -> None:
    """The handler intercepts ONLY ``session_expired``.

    A ``token_expired`` 401 on a /ui route (and by extension every
    /api 401 code) keeps FastAPI's stock JSON body even when the
    browser asks for HTML.
    """
    key = make_rsa_keypair("kid-refresh-4")
    session_id = _seed_session(access_token=_admin_jwt(key))
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc(mock_router, public_jwks(key))
        client = _client(_build_app(), session_id)
        response = client.get("/ui/other-401", headers={"accept": "text/html"})
    assert response.status_code == 401
    assert response.json() == {"detail": "token_expired"}
    assert "location" not in response.headers


# ---------------------------------------------------------------------------
# Concurrency + expiry-cap discipline (direct primitive coverage)
# ---------------------------------------------------------------------------


def test_concurrent_loser_skips_token_endpoint_and_returns_stored_pair() -> None:
    """AC: second refresher sees the winner's tokens, no network call.

    Simulates the post-lock state of the losing request determinist-
    ically: the row already holds a rotated pair, the caller still
    presents the access token it loaded before blocking on the
    ``SELECT ... FOR UPDATE`` lock.
    """
    session_id = _seed_session(access_token="winner-access-token")

    async def _losing_refresh() -> Any:
        return await refresh_session_tokens(
            session_id,
            stale_access_token="loser-stale-access-token",
        )

    with respx.mock(assert_all_called=False) as mock_router:
        token_route = mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(500),
        )
        with capture_logs() as captured:
            result = asyncio.run(_losing_refresh())

    assert token_route.call_count == 0
    assert result.access_token == "winner-access-token"
    events = [e["event"] for e in captured]
    assert "ui_auth_token_refresh_skipped_concurrent_winner" in events
    assert "ui_auth_token_refresh_succeeded" not in events


def test_refresh_extension_clamps_at_absolute_lifetime_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC/out-of-scope guard: refresh cannot push past the absolute cap."""
    monkeypatch.setenv("UI_SESSION_ABSOLUTE_LIFETIME_SECONDS", "90")
    get_settings.cache_clear()
    session_id = _seed_session(
        access_token="old-access",
        lifetime=timedelta(seconds=80),
    )

    async def _refresh() -> Any:
        return await refresh_session_tokens(
            session_id,
            stale_access_token="old-access",
        )

    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc(mock_router, {"keys": []})
        mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=_refresh_response("new-access", expires_in=3600),
        )
        rotated = asyncio.run(_refresh())

    # The candidate now + (3600 - 60) clamps to the ceiling
    # created_at + 90 s exactly; the extension still moved the expiry
    # forward from its login-time created_at + 80 s.
    assert rotated.expires_at == rotated.created_at + timedelta(seconds=90)
    assert rotated.expires_at > rotated.created_at + timedelta(seconds=80)
    assert rotated.access_token == "new-access"


def test_refresh_never_shrinks_a_slid_expires_at() -> None:
    """Monotonic guard: a slid row keeps its later expiry post-refresh."""
    session_id = _seed_session(
        access_token="old-access",
        lifetime=timedelta(hours=2),
    )
    original = _load_row(session_id)
    assert original is not None

    async def _refresh() -> Any:
        return await refresh_session_tokens(
            session_id,
            stale_access_token="old-access",
        )

    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc(mock_router, {"keys": []})
        mock_router.post(_TOKEN_ENDPOINT).mock(
            # now + (300 - 60) is far earlier than the 2 h expiry.
            return_value=_refresh_response("new-access", expires_in=300),
        )
        rotated = asyncio.run(_refresh())

    assert rotated.expires_at >= original.expires_at
    assert rotated.access_token == "new-access"


# ---------------------------------------------------------------------------
# Logout + session-store primitive
# ---------------------------------------------------------------------------


def test_revoked_session_redirects_via_middleware_without_refresh() -> None:
    """AC: logout-revoked sessions never reach the refresh path."""
    key = make_rsa_keypair("kid-refresh-5")
    session_id = _seed_session(access_token=_admin_jwt(key, expires_in=-120))

    async def _revoke() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            await revoke_session(session, session_id)

    asyncio.run(_revoke())

    with respx.mock(assert_all_called=False) as mock_router:
        token_route = mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(500),
        )
        response = _client(_build_app(), session_id).get(_PROBE_PATH)

    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")
    assert token_route.call_count == 0


def test_load_session_for_update_is_side_effect_free() -> None:
    """The locked load decrypts without bumping last_seen / sliding."""
    session_id = _seed_session(access_token="a-token", refresh_token="r-token")
    baseline = _load_row(session_id)  # load_session bumps last_seen once
    assert baseline is not None

    async def _locked_load() -> tuple[Any, Any]:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            locked = await load_session_for_update(session, session_id)
        async with sessionmaker() as session, session.begin():
            row = await session.get(WebSession, session_id)
            assert row is not None
            return locked, row.last_seen_at

    locked, last_seen_after = asyncio.run(_locked_load())
    assert locked is not None
    assert locked.access_token == "a-token"
    assert locked.refresh_token == "r-token"
    # SQLite returns naive UTC datetimes; normalise both sides before
    # comparing (the chassis "naive means UTC" read-side convention).
    assert last_seen_after.replace(tzinfo=UTC) == baseline.last_seen_at.replace(tzinfo=UTC)

    async def _gone_states() -> tuple[Any, Any]:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            await session.execute(
                update(WebSession)
                .where(WebSession.id == session_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
            )
        async with sessionmaker() as session, session.begin():
            expired = await load_session_for_update(session, session_id)
            missing = await load_session_for_update(session, uuid.uuid4())
            return expired, missing

    expired, missing = asyncio.run(_gone_states())
    assert expired is None
    assert missing is None
