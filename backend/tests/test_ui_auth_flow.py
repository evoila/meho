# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the BFF OAuth + PKCE login flow (Task #865).

Exercises every acceptance criterion on issue #865:

* ``/ui/auth/login`` builds an authorization URL with
  ``code_challenge`` + ``code_challenge_method=S256`` +
  ``resource=<backplane_url>/api`` and 302s the browser to Keycloak.
* ``/ui/auth/callback`` exchanges code + verifier (respx-mocked token
  endpoint) for tokens, validates the access token through the
  chassis JWT chain, creates a ``web_session`` row, and sets the
  ``meho_session`` cookie with ``HttpOnly`` + ``Secure`` +
  ``SameSite=Strict`` + ``Path=/``.
* ``/ui/auth/logout`` revokes the session, clears the cookie, and
  302s to Keycloak's end-session endpoint.
* :class:`UISessionMiddleware`: ``/ui/*`` with no/expired session →
  302 to login; with a valid session → operator loaded.
* PKCE verifier store: server-side, one-shot, expires past the TTL.
* CSRF: ``state`` round-trip cross-check; replay of a consumed
  ``state`` rejected.
* Open-redirect: a crafted ``?return_to=`` value is sanitised.

The autouse fixtures in :mod:`backend.tests.conftest`
(``_default_database_url`` + ``_schema_template_db``) provide a fresh
file-backed SQLite DB migrated to head before every test, so the
``web_session`` table is present without any per-test
``alembic upgrade head`` replay (per PR #898's per-worker template
pattern).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import WebSession
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import (
    SESSION_COOKIE_NAME,
    UISessionMiddleware,
    build_router,
)
from meho_backplane.ui.auth.flow import (
    AUTHORIZATION_FLOW_TTL_SECONDS,
    PKCEVerifierStore,
    build_authorization_request,
    clear_discovery_cache,
    exchange_code_for_tokens,
    get_verifier_store,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    load_session,
    reset_fernet_cache_for_testing,
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
# Fixtures
# ---------------------------------------------------------------------------


_BACKPLANE_URL = "https://meho.test"
_REDIRECT_URI = f"{_BACKPLANE_URL}/ui/auth/callback"
_AUTHORIZATION_ENDPOINT = f"{DEFAULT_ISSUER}/protocol/openid-connect/auth"
_TOKEN_ENDPOINT = f"{DEFAULT_ISSUER}/protocol/openid-connect/token"
_END_SESSION_ENDPOINT = f"{DEFAULT_ISSUER}/protocol/openid-connect/logout"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test.

    The chassis-wide :class:`Settings` requires ``KEYCLOAK_ISSUER_URL``
    / ``KEYCLOAK_AUDIENCE`` / ``VAULT_ADDR``; the BFF additionally
    needs the operator-console encryption key + the confidential
    client id/secret. Every test inherits the same baseline so
    individual cases only override the knob under test.
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
    reset_verifier_store_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def _mock_oidc_metadata(
    mock_router: respx.MockRouter,
    *,
    include_end_session: bool = True,
    jwks: dict[str, Any] | None = None,
) -> None:
    """Stub the discovery + JWKS endpoints with the BFF-relevant URLs.

    Replaces the chassis ``mock_discovery_and_jwks`` helper for the BFF
    suite -- the chassis helper writes a discovery doc with only
    ``issuer`` + ``jwks_uri``, but the BFF flow also reads
    ``authorization_endpoint`` / ``token_endpoint`` /
    ``end_session_endpoint`` from the same document. Registering the
    chassis helper alongside this one collides on the URL and the
    second mock wins -- yielding a discovery doc without the BFF
    fields. This helper writes one merged document and registers the
    JWKS endpoint when ``jwks`` is provided (the callback path needs
    it; the login path does not).
    """
    metadata: dict[str, Any] = {
        "issuer": DEFAULT_ISSUER,
        "authorization_endpoint": _AUTHORIZATION_ENDPOINT,
        "token_endpoint": _TOKEN_ENDPOINT,
        "jwks_uri": f"{DEFAULT_ISSUER}/protocol/openid-connect/certs",
    }
    if include_end_session:
        metadata["end_session_endpoint"] = _END_SESSION_ENDPOINT
    mock_router.get(f"{DEFAULT_ISSUER}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json=metadata),
    )
    if jwks is not None:
        mock_router.get(metadata["jwks_uri"]).mock(
            return_value=httpx.Response(200, json=jwks),
        )


def _build_app(*, include_dummy_ui_route: bool = True) -> FastAPI:
    """Construct a minimal FastAPI app with the BFF wired in.

    The BFF router lives at ``/ui/auth/*``; ``include_dummy_ui_route``
    optionally registers a ``GET /ui/sentinel`` route the middleware
    redirect tests exercise.
    """
    app = FastAPI()
    app.add_middleware(UISessionMiddleware)
    app.include_router(build_router())
    if include_dummy_ui_route:

        @app.get("/ui/sentinel")
        async def sentinel() -> dict[str, str]:
            # Reachable only when the session middleware finds a
            # valid session and lets the request through.
            return {"ok": "true"}

    return app


# ---------------------------------------------------------------------------
# /ui/auth/login -- builds the PKCE authorization URL (AC 1)
# ---------------------------------------------------------------------------


def test_login_redirects_to_keycloak_with_pkce_and_resource() -> None:
    """AC 1: ``/ui/auth/login`` 302s to Keycloak with S256 PKCE + resource."""
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/login")
    assert response.status_code == 302
    location = response.headers["location"]
    parsed = urlparse(location)
    # Bound to Keycloak's auth endpoint.
    assert location.startswith(_AUTHORIZATION_ENDPOINT)
    params = parse_qs(parsed.query)
    # OAuth 2.1 + PKCE + RFC 8707 contract on the URL.
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["meho-web"]
    assert params["redirect_uri"] == [_REDIRECT_URI]
    assert params["code_challenge_method"] == ["S256"]
    assert "code_challenge" in params
    # The challenge value is the S256 of the verifier; here we only
    # check it is non-empty and base64url-ish (authlib generates the
    # value, so cryptographic strength is its responsibility).
    assert len(params["code_challenge"][0]) >= 16
    assert "state" in params
    assert len(params["state"][0]) >= 16
    # RFC 8707 resource indicator -- the BFF binds tokens to the
    # backplane API.
    assert params["resource"] == [f"{_BACKPLANE_URL}/api"]


def test_login_persists_verifier_in_server_side_store_not_cookie() -> None:
    """The PKCE verifier MUST NOT live in the client cookie.

    Defends decision #11's "tokens stay server-side" contract: a
    verifier in a cookie would defeat the property PKCE protects.
    """
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/login")
    # No cookies set on the login redirect -- the verifier is in
    # the server-side store, not on the client.
    assert response.cookies == {}
    # The store has exactly one pending flow now.
    assert get_verifier_store().size() == 1


def test_login_503s_when_client_secret_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC: unset ``UI_KEYCLOAK_CLIENT_SECRET`` surfaces an actionable 503."""
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "")
    get_settings.cache_clear()
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/login")
    assert response.status_code == 503
    body = response.json()
    assert "UI_KEYCLOAK_CLIENT_SECRET" in body["detail"]
    assert "keycloak-web-client.md" in body["detail"]


def test_login_502s_when_discovery_endpoint_unreachable() -> None:
    """A network failure on the discovery hit surfaces as 502, not 500."""
    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get(f"{DEFAULT_ISSUER}/.well-known/openid-configuration").mock(
            side_effect=httpx.ConnectError("simulated")
        )
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/login")
    assert response.status_code == 502
    assert response.json()["detail"] == "upstream_auth_provider_unreachable"


# ---------------------------------------------------------------------------
# return_to validation (open-redirect guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_return_to"),
    [
        ("/ui/dashboard", "/ui/dashboard"),
        ("/ui/", "/ui/"),
        ("", "/ui/"),
        ("//evil.example.com/path", "/ui/"),
        ("https://evil.example.com/path", "/ui/"),
        ("/api/secret", "/ui/"),
        ("/etc/passwd", "/ui/"),
    ],
)
def test_login_sanitises_return_to_against_open_redirect(
    raw: str,
    expected_return_to: str,
) -> None:
    """An operator-supplied ``return_to`` outside ``/ui/`` falls back to ``/ui/``.

    The login route stashes ``return_to`` in the PKCE verifier store;
    the callback reads it back and 302s there on success. A crafted
    value (absolute URL, ``//host``, path outside ``/ui/``) must be
    rejected so the callback never bounces the operator off-host.
    """
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        if raw:
            client.get(f"/ui/auth/login?return_to={raw}")
        else:
            client.get("/ui/auth/login")
    # Pop the registered flow -- the stored ``return_to`` is what
    # the callback would honour. Inspecting it confirms the sanitiser
    # ran before the value reached storage.
    store = get_verifier_store()
    # There's exactly one flow; the state value is whatever authlib
    # generated. Iterate over the flows dict to read it.
    assert store.size() == 1
    # Reach into the dict directly -- this is a test-only invariant
    # check; production code uses :meth:`pop`.
    flows = store._flows
    pending = next(iter(flows.values()))
    assert pending.return_to == expected_return_to


# ---------------------------------------------------------------------------
# /ui/auth/callback -- code exchange + session creation (AC 2)
# ---------------------------------------------------------------------------


def _mint_access_token(
    *,
    audience: str = DEFAULT_AUDIENCE,
    sub: str = "op-42",
) -> tuple[str, dict[str, Any]]:
    """Mint a JWT signed by a fresh keypair and return ``(token, jwks)``.

    The JWKS lets respx stub the chassis JWT chain's JWKS endpoint
    so :func:`verify_jwt_for_audience` can decode the token.
    """
    key = make_rsa_keypair("test-kid")
    token = mint_token(key, sub=sub, audience=audience)
    return token, public_jwks(key)


def test_callback_creates_session_and_sets_cookie() -> None:
    """AC 2: callback exchanges code+verifier, creates session row, sets cookie."""
    access_token, jwks = _mint_access_token()
    refresh_token = "refresh-token-value"

    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, jwks=jwks)
        # The token endpoint returns access + refresh + expires_in
        # exactly as Keycloak does.
        token_route = mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            ),
        )
        client = TestClient(_build_app(), follow_redirects=False)
        # Step 1: login mints a state + verifier.
        login_response = client.get("/ui/auth/login?return_to=/ui/dashboard")
        login_location = login_response.headers["location"]
        state = parse_qs(urlparse(login_location).query)["state"][0]
        # Step 2: simulate Keycloak's redirect back to the callback.
        callback_response = client.get(
            f"/ui/auth/callback?code=test-code&state={state}",
        )

    assert token_route.called
    # Verify the token request body shape -- code, code_verifier,
    # redirect_uri, resource indicator. authlib's default
    # ``client_secret_basic`` puts the client credentials in the
    # ``Authorization`` header (RFC 6749 §2.3.1); Keycloak accepts
    # both that and ``client_secret_post``, so the body does not
    # carry ``client_id`` / ``client_secret``. The header check below
    # confirms the secret is on the wire to the IdP without ever
    # surfacing the value in the test output.
    call = token_route.calls[0]
    posted_body = call.request.content.decode("utf-8")
    assert "code=test-code" in posted_body
    assert "code_verifier=" in posted_body
    assert "grant_type=authorization_code" in posted_body
    assert "resource=https%3A%2F%2Fmeho.test%2Fapi" in posted_body
    # ``Authorization: Basic <base64(client_id:client_secret)>`` --
    # the header is present (length > 'Basic '), but we deliberately
    # do not unpack the value because the secret-leak sweep in
    # ``conftest`` would otherwise flag the test on any future
    # accidental print.
    auth_header = call.request.headers.get("authorization")
    assert auth_header is not None
    assert auth_header.startswith("Basic ")
    assert len(auth_header) > len("Basic ")

    # Step 3: callback redirects to the originally-requested return_to.
    assert callback_response.status_code == 302
    assert callback_response.headers["location"] == "/ui/dashboard"

    # Cookie attributes -- HttpOnly + Secure + SameSite=Strict + Path=/.
    # Starlette emits the directives capitalised as shown but lowercases
    # the attribute *values* (e.g. ``samesite=strict``); compare the
    # full string lowercased so the assertions are case-insensitive on
    # both directive names and values.
    set_cookie = callback_response.headers["set-cookie"].lower()
    assert f"{SESSION_COOKIE_NAME.lower()}=" in set_cookie
    assert "httponly" in set_cookie
    # respx + testclient strips Secure (the TestClient is not over
    # TLS); Starlette still emits the directive on the raw header
    # because the cookie was constructed with ``secure=True``.
    assert "secure" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/" in set_cookie

    # The cookie value parses as a UUID -- the session row's PK.
    cookie_value = callback_response.cookies[SESSION_COOKIE_NAME]
    session_id = uuid.UUID(cookie_value)

    # Step 4: the ``web_session`` row exists and carries ENCRYPTED
    # tokens (not plaintext).
    async def _check_row() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await session.get(WebSession, session_id)
            assert row is not None
            assert row.operator_sub == "op-42"
            assert str(row.tenant_id) == DEFAULT_TENANT_ID
            # Stored ciphertext is bytes; never the plaintext token.
            assert isinstance(row.access_token, bytes)
            assert access_token.encode("utf-8") not in row.access_token
            assert refresh_token.encode("utf-8") not in row.refresh_token

    asyncio.run(_check_row())


def test_callback_rejects_unknown_state() -> None:
    """Replay of a consumed / forged ``state`` collapses to a 400."""
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(
            "/ui/auth/callback?code=test-code&state=not-a-real-state",
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "authorization_failed"


def test_callback_rejects_missing_state() -> None:
    """The CSRF guard fires on a missing ``state`` -- no verifier lookup possible."""
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/callback?code=test-code")
    assert response.status_code == 400
    assert response.json()["detail"] == "authorization_failed"


def test_callback_propagates_idp_error_to_400() -> None:
    """IdP-emitted ``?error=access_denied`` -> 400 (operator cancelled)."""
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(
            "/ui/auth/callback?error=access_denied&error_description=user-cancelled"
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "authorization_failed"


def test_callback_502s_when_token_endpoint_unreachable() -> None:
    """Network failure on the token endpoint surfaces as 502."""
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        mock_router.post(_TOKEN_ENDPOINT).mock(side_effect=httpx.ConnectError("boom"))
        client = TestClient(_build_app(), follow_redirects=False)
        login_response = client.get("/ui/auth/login")
        state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
        response = client.get(f"/ui/auth/callback?code=test-code&state={state}")
    assert response.status_code == 502
    assert response.json()["detail"] == "upstream_auth_provider_unreachable"


def test_callback_rejects_replayed_state_after_first_consumption() -> None:
    """The verifier is single-use; a second callback with the same state fails."""
    access_token, jwks = _mint_access_token()
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, jwks=jwks)
        mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": access_token,
                    "refresh_token": "refresh-x",
                    "expires_in": 3600,
                },
            ),
        )
        client = TestClient(_build_app(), follow_redirects=False)
        login_response = client.get("/ui/auth/login")
        state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
        first = client.get(f"/ui/auth/callback?code=code-1&state={state}")
        second = client.get(f"/ui/auth/callback?code=code-2&state={state}")
    assert first.status_code == 302
    assert second.status_code == 400


# ---------------------------------------------------------------------------
# /ui/auth/logout -- revoke + clear + Keycloak end-session redirect (AC 3)
# ---------------------------------------------------------------------------


def test_logout_revokes_session_and_clears_cookie_and_redirects_to_end_session() -> None:
    """AC 3: logout revokes the row, clears the cookie, redirects to Keycloak."""

    # Seed a session row directly so the logout test does not
    # double-pay for the full callback round-trip.
    async def _seed_session() -> uuid.UUID:
        from datetime import timedelta

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub="op-99",
                tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
                access_token="access-token-plaintext",
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    session_id = asyncio.run(_seed_session())

    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        response = client.get("/ui/auth/logout")

    assert response.status_code == 302
    location = response.headers["location"]
    # End-session endpoint with the BFF's two parameters.
    assert location.startswith(_END_SESSION_ENDPOINT)
    params = parse_qs(urlparse(location).query)
    assert params["client_id"] == ["meho-web"]
    assert params["post_logout_redirect_uri"] == [f"{_BACKPLANE_URL}/ui/auth/login"]

    # Cookie cleared via Max-Age=0 (or expires in the past) on the
    # same name+path -- TestClient surfaces this as the cookie
    # being absent on the next request.
    set_cookie = response.headers["set-cookie"]
    assert f'{SESSION_COOKIE_NAME}=""' in set_cookie or f"{SESSION_COOKIE_NAME}=;" in set_cookie

    # The session row's ``revoked_at`` is now set.
    async def _check_revoked() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await session.get(WebSession, session_id)
            assert row is not None
            assert row.revoked_at is not None

    asyncio.run(_check_revoked())


def test_logout_redirects_to_login_when_end_session_endpoint_absent() -> None:
    """A discovery doc without ``end_session_endpoint`` -> local login redirect."""
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, include_end_session=False)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/logout")
    assert response.status_code == 302
    assert response.headers["location"] == f"{_BACKPLANE_URL}/ui/auth/login"


def test_logout_without_cookie_still_redirects_and_clears() -> None:
    """An anonymous ``/ui/auth/logout`` hit drops nothing but still redirects."""
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/logout")
    assert response.status_code == 302
    # Still bounces to the end-session URL so an IdP-side session
    # the operator may have under a different tab also gets a
    # clean termination.
    assert response.headers["location"].startswith(_END_SESSION_ENDPOINT)


# ---------------------------------------------------------------------------
# Session middleware -- redirect on missing session, load on hit (AC 4)
# ---------------------------------------------------------------------------


def test_middleware_redirects_unauthenticated_ui_request_to_login() -> None:
    """AC 4: a ``/ui/*`` page with no session 302s to login with return_to."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/sentinel")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("/ui/auth/login?return_to=")
    # The encoded return_to round-trips the original path.
    return_to = parse_qs(urlparse(location).query)["return_to"][0]
    assert return_to == "/ui/sentinel"


def test_middleware_lets_authenticated_ui_request_through() -> None:
    """AC 4: with a valid session, the middleware lets the request through."""

    async def _seed_session() -> uuid.UUID:
        from datetime import timedelta

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub="op-77",
                tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
                access_token="a",
                refresh_token="r",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    session_id = asyncio.run(_seed_session())

    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        response = client.get("/ui/sentinel")

    assert response.status_code == 200
    assert response.json() == {"ok": "true"}


def test_middleware_redirects_when_session_is_expired() -> None:
    """A session past ``expires_at`` is treated as no session."""

    async def _seed_expired_session() -> uuid.UUID:
        from datetime import timedelta

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub="op-77",
                tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
                access_token="a",
                refresh_token="r",
                # Negative lifetime -- the row's expires_at is in
                # the past at insertion time, so load_session returns
                # None.
                lifetime=timedelta(seconds=-1),
            )
            return decrypted.id

    session_id = asyncio.run(_seed_expired_session())

    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        response = client.get("/ui/sentinel")

    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_middleware_redirects_when_cookie_is_malformed() -> None:
    """A non-UUID cookie value is treated as no session, never an exception."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, "not-a-uuid")
        response = client.get("/ui/sentinel")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_middleware_bypasses_static_assets() -> None:
    """``/ui/static/*`` bypasses the session check (chassis CSS / JS)."""
    # No sentinel registered for /ui/static; FastAPI's 404 still
    # surfaces because the middleware lets the request through.
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/static/tailwind.css")
    assert response.status_code == 404
    # Crucially: not 302. The bypass worked.


def test_middleware_lets_auth_routes_through_without_session() -> None:
    """The BFF auth surfaces themselves bypass the session check."""
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/login")
    # Login route runs, not bounced to itself.
    assert response.status_code == 302
    assert response.headers["location"].startswith(_AUTHORIZATION_ENDPOINT)


def test_middleware_does_not_touch_non_ui_paths() -> None:
    """Out-of-prefix paths pass through untouched."""
    app = _build_app(include_dummy_ui_route=False)

    @app.get("/api/probe")
    async def probe() -> dict[str, str]:
        return {"out": "of-scope"}

    with respx.mock(assert_all_called=False):
        client = TestClient(app, follow_redirects=False)
        response = client.get("/api/probe")
    assert response.status_code == 200
    assert response.json() == {"out": "of-scope"}


# ---------------------------------------------------------------------------
# PKCE verifier store -- in-process semantics
# ---------------------------------------------------------------------------


def test_pkce_verifier_store_pop_is_single_use() -> None:
    """A second pop on the same state yields ``None``."""

    async def _go() -> None:
        store = PKCEVerifierStore()
        await store.put("state-1", code_verifier="v", return_to="/ui/")
        first = await store.pop("state-1")
        second = await store.pop("state-1")
        assert first is not None
        assert first.code_verifier == "v"
        assert second is None

    asyncio.run(_go())


def test_pkce_verifier_store_expires_past_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale entry is reaped on the next ``put`` call."""

    async def _go() -> None:
        store = PKCEVerifierStore()
        # First put -- normal.
        await store.put("state-stale", code_verifier="v1", return_to="/ui/")
        assert store.size() == 1
        # Fast-forward monotonic time past the TTL by patching the
        # store's internal time reference.
        import meho_backplane.ui.auth.flow as flow_mod

        original = flow_mod.time.monotonic
        monkeypatch.setattr(
            flow_mod.time,
            "monotonic",
            lambda: original() + AUTHORIZATION_FLOW_TTL_SECONDS + 1,
        )
        # A fresh put triggers the reap and drops the stale entry.
        await store.put("state-fresh", code_verifier="v2", return_to="/ui/")
        assert store.size() == 1
        assert await store.pop("state-stale") is None
        fresh = await store.pop("state-fresh")
        assert fresh is not None and fresh.code_verifier == "v2"

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Direct flow-module tests (not via TestClient)
# ---------------------------------------------------------------------------


def test_build_authorization_request_carries_state_and_resource() -> None:
    """The flow-level primitive emits a valid PKCE URL on its own."""

    async def _go() -> None:
        with respx.mock(assert_all_called=False) as mock_router:
            _mock_oidc_metadata(mock_router)
            url, state = await build_authorization_request(
                redirect_uri=_REDIRECT_URI,
                return_to="/ui/dashboard",
            )
        params = parse_qs(urlparse(url).query)
        assert params["state"] == [state]
        assert params["resource"] == [f"{_BACKPLANE_URL}/api"]
        assert params["code_challenge_method"] == ["S256"]

    asyncio.run(_go())


def test_exchange_code_rejects_unknown_state() -> None:
    """The flow-level primitive raises on a state the store does not know."""
    from meho_backplane.ui.auth.flow import OAuthFlowError

    async def _go() -> None:
        with respx.mock(assert_all_called=False) as mock_router:
            _mock_oidc_metadata(mock_router)
            with pytest.raises(OAuthFlowError):
                await exchange_code_for_tokens(
                    redirect_uri=_REDIRECT_URI,
                    authorization_response=(f"{_REDIRECT_URI}?code=c&state=forged"),
                    state="forged",
                )

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Round-trip: login → callback → middleware lets request through
# ---------------------------------------------------------------------------


def test_full_login_round_trip_lets_authenticated_page_through() -> None:
    """End-to-end: log in, then the session cookie reaches the sentinel route."""
    access_token, jwks = _mint_access_token(sub="op-roundtrip")

    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, jwks=jwks)
        mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": access_token,
                    "refresh_token": "rt",
                    "expires_in": 3600,
                },
            ),
        )
        client = TestClient(_build_app(), follow_redirects=False)
        # 1. Unauthenticated -> redirect to login.
        deep_link = client.get("/ui/sentinel")
        assert deep_link.status_code == 302
        # 2. Follow login -> Keycloak.
        login_response = client.get(
            deep_link.headers["location"].replace("https://testserver", ""),
        )
        state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
        # 3. Callback creates the session + sets the cookie.
        callback_response = client.get(
            f"/ui/auth/callback?code=test-code&state={state}",
        )
        assert callback_response.status_code == 302
        # Final return_to is /ui/sentinel via the original deep link.
        assert callback_response.headers["location"] == "/ui/sentinel"
        # 4. Now the sentinel is reachable. TestClient drops the
        # ``Secure``-flagged cookie on the HTTP transport (the
        # cookie jar will not send a Secure cookie over plain HTTP),
        # so we manually replay it. The production redirect at
        # https://meho.evba.lab is HTTPS-only and the browser sends
        # the cookie without issue; this is purely a TestClient
        # interaction quirk.
        cookie_value = callback_response.cookies[SESSION_COOKIE_NAME]
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        page = client.get("/ui/sentinel")

    assert page.status_code == 200
    assert page.json() == {"ok": "true"}

    # And the session row's identity matches the token's ``sub``.
    session_id = uuid.UUID(cookie_value)

    async def _check_loaded() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await load_session(session, session_id)
            assert decrypted is not None
            assert decrypted.operator_sub == "op-roundtrip"

    asyncio.run(_check_loaded())
