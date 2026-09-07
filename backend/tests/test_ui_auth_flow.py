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
from starlette.exceptions import HTTPException as StarletteHTTPException

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import WebSession
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import (
    SESSION_COOKIE_NAME,
    UISessionMiddleware,
    build_router,
)
from meho_backplane.ui.auth.errors import ui_session_expired_exception_handler
from meho_backplane.ui.auth.flow import (
    AUTHORIZATION_FLOW_TTL_SECONDS,
    PKCEVerifierStore,
    build_authorization_request,
    clear_discovery_cache,
    exchange_code_for_tokens,
    get_verifier_store,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.middleware import require_ui_session
from meho_backplane.ui.auth.revalidation import (
    reset_read_revalidation_cache_for_testing,
)
from meho_backplane.ui.auth.routes import (
    AUTHORIZATION_STATE_EXPIRED_DETAIL,
    LOGIN_BINDING_COOKIE_PREFIX,
    login_binding_cookie_name,
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
    reset_read_revalidation_cache_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    reset_read_revalidation_cache_for_testing()


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
    # Register the app-level HTTPException handler exactly as
    # meho_backplane.main does, so the callback's recoverable-state
    # HTML-redirect affordance (G0.29 #2089) is exercised under test
    # rather than only in the wired-up production app.
    app.add_exception_handler(StarletteHTTPException, ui_session_expired_exception_handler)
    app.include_router(build_router())
    if include_dummy_ui_route:

        @app.get("/ui/sentinel")
        async def sentinel() -> dict[str, str]:
            # Reachable only when the session middleware finds a
            # valid session and lets the request through.
            return {"ok": "true"}

    return app


def _https_client(app: FastAPI | None = None) -> TestClient:
    """A ``TestClient`` over HTTPS so ``Secure`` cookies survive the round-trip.

    The BFF cookies -- the ``meho_session`` cookie and the F10 (#272)
    login-binding cookie -- are ``Secure``, and the default
    ``http://testserver`` transport silently drops ``Secure`` cookies (a
    production deploy is HTTPS-only). Any login->callback flow that must
    replay the login-binding cookie the login response set uses this
    client instead of a plain ``TestClient``.
    """
    return TestClient(
        app if app is not None else _build_app(),
        base_url="https://testserver",
        follow_redirects=False,
    )


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
    verifier in a cookie would defeat the property PKCE protects. Login
    does set the F10 (#272) login-binding cookie, but that carries a
    *separate* opaque per-flow secret -- never the ``code_verifier`` --
    so the PKCE property is intact.
    """
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/login")
    # The store has exactly one pending flow now.
    store = get_verifier_store()
    assert store.size() == 1
    pending = next(iter(store._flows.values()))
    # The only cookie the login redirect sets is the login-binding
    # cookie, and it does NOT carry the session cookie or the PKCE
    # verifier -- both stay server-side.
    raw_set_cookie = response.headers["set-cookie"]
    assert LOGIN_BINDING_COOKIE_PREFIX in raw_set_cookie
    assert SESSION_COOKIE_NAME not in response.cookies
    assert pending.code_verifier not in raw_set_cookie
    # The binding cookie's value is the per-flow secret stored server
    # side -- it is what the callback cross-checks, not the verifier.
    assert pending.browser_binding in raw_set_cookie


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
        client = _https_client()
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
    # Keycloak enforces exact-match on ``redirect_uri`` at the token
    # endpoint (RFC 6749 §4.1.3); a regression that drops the field
    # from the body breaks the exchange but the existing substring
    # asserts above wouldn't notice. ``parse_qs`` returns
    # percent-decoded values, so we compare against the bare URI.
    posted_params = parse_qs(posted_body)
    assert posted_params["redirect_uri"] == [_REDIRECT_URI]
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


# Non-HTML ``Accept`` -- a scripted probe / htmx fragment fetch. The
# recoverable-state class keeps its structured JSON body for these
# callers (only HTML navigations get the login-restart redirect).
_JSON_ACCEPT = {"accept": "application/json"}
# HTML ``Accept`` -- a real browser navigating the callback URL. This
# is the class that gets the one-click login-restart affordance.
_HTML_ACCEPT = {"accept": "text/html,application/xhtml+xml"}


def test_callback_rejects_unknown_state() -> None:
    """Replay of a consumed / forged ``state`` -> recoverable 400 (JSON caller)."""
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(
            "/ui/auth/callback?code=test-code&state=not-a-real-state",
            headers=_JSON_ACCEPT,
        )
    assert response.status_code == 400
    # Non-HTML callers keep a structured body -- now the recoverable
    # ``authorization_state_expired`` code (distinct from an IdP decline).
    assert response.json()["detail"] == AUTHORIZATION_STATE_EXPIRED_DETAIL


def test_callback_rejects_missing_state() -> None:
    """Missing ``state`` -> recoverable 400 body for a JSON caller."""
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/callback?code=test-code", headers=_JSON_ACCEPT)
    assert response.status_code == 400
    assert response.json()["detail"] == AUTHORIZATION_STATE_EXPIRED_DETAIL


def test_callback_expired_state_html_redirects_to_login() -> None:
    """AC 1/2: an HTML navigation on expired ``state`` gets a 303 login restart.

    The whole point of #2089 Leg 1: instead of the raw-JSON dead-end a
    browser would render for ``{"detail": "..."}``, an operator who let
    the login window lapse is bounced back to ``/ui/auth/login`` on one
    click -- no hand-navigation, no cookie to clear (pre-session).
    """
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(
            "/ui/auth/callback?code=test-code&state=not-a-real-state",
            headers=_HTML_ACCEPT,
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/auth/login"
    # No dead ``meho_session`` cookie is set -- the callback runs before
    # any session exists, so there is nothing to clear.
    assert SESSION_COOKIE_NAME not in response.cookies
    # Never re-cache a redirect the browser might replay stale.
    assert response.headers.get("cache-control") == "no-store"


def test_callback_missing_state_html_redirects_to_login() -> None:
    """AC 1/2: a missing-``state`` HTML navigation also restarts the login flow."""
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/callback?code=test-code", headers=_HTML_ACCEPT)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/auth/login"


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


def test_callback_idp_error_html_is_not_collapsed_to_login_restart() -> None:
    """AC 4: a genuine IdP decline is NOT swept into the "start over" affordance.

    Even with an HTML ``Accept``, ``?error=access_denied`` stays a 400
    ``authorization_failed`` JSON body -- it is distinct from the
    recoverable expired-state class and must remain diagnosable rather
    than looping the operator back to a login that will decline again.
    """
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(
            "/ui/auth/callback?error=access_denied&error_description=user-cancelled",
            headers=_HTML_ACCEPT,
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "authorization_failed"


def test_callback_502s_when_token_endpoint_unreachable() -> None:
    """Network failure on the token endpoint surfaces as 502 (not a login restart).

    AC 4: the unreachable-token-endpoint path stays a distinguishable
    502 even for an HTML navigation -- restarting the login flow would
    just hit the same dead token endpoint, so it must not be collapsed
    into the recoverable "start over" affordance.
    """
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        mock_router.post(_TOKEN_ENDPOINT).mock(side_effect=httpx.ConnectError("boom"))
        client = _https_client()
        login_response = client.get("/ui/auth/login")
        state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
        response = client.get(
            f"/ui/auth/callback?code=test-code&state={state}",
            headers=_HTML_ACCEPT,
        )
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
        client = _https_client()
        login_response = client.get("/ui/auth/login")
        state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
        first = client.get(f"/ui/auth/callback?code=code-1&state={state}")
        second = client.get(f"/ui/auth/callback?code=code-2&state={state}")
    assert first.status_code == 302
    assert second.status_code == 400


# ---------------------------------------------------------------------------
# /ui/auth login-CSRF browser binding (F10, #272)
# ---------------------------------------------------------------------------


def _token_json(access_token: str) -> dict[str, Any]:
    """Minimal Keycloak token-endpoint body for the binding tests."""
    return {
        "access_token": access_token,
        "refresh_token": "rt",
        "expires_in": 3600,
        "token_type": "Bearer",
    }


def test_login_sets_browser_binding_cookie_with_lax_httponly_secure() -> None:
    """F10 (#272): login sets a short-lived HttpOnly/Secure/SameSite=Lax cookie.

    ``SameSite=Lax`` (not ``Strict``) is the callback-compatible policy:
    the callback is a top-level GET the IdP initiates, cross-site for a
    separately hosted Keycloak, and ``Strict`` would drop the cookie.
    The cookie is scoped to ``/ui/auth`` and its value is the per-flow
    secret the store holds.
    """
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/login")
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    name = login_binding_cookie_name(state)
    binding_header = next(
        h for h in response.headers.get_list("set-cookie") if h.startswith(f"{name}=")
    )
    lowered = binding_header.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered
    assert "path=/ui/auth" in lowered
    assert f"max-age={AUTHORIZATION_FLOW_TTL_SECONDS}" in lowered
    # The cookie value is the per-flow secret the store stashed -- not
    # the PKCE verifier, and distinct from the OAuth ``state``.
    pending = next(iter(get_verifier_store()._flows.values()))
    assert response.cookies[name] == pending.browser_binding
    assert pending.browser_binding != state


def test_callback_from_second_browser_without_binding_cookie_is_rejected() -> None:
    """F10 (#272): the headline attack -- A's unconsumed callback in B fails.

    Browser A starts login; browser B (a fresh client that never started
    the flow, so it holds none of A's cookies) follows A's callback URL.
    No session is created and the token endpoint is never reached -- the
    binding is checked before token exchange.
    """
    access_token, jwks = _mint_access_token()
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, jwks=jwks)
        token_route = mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(200, json=_token_json(access_token)),
        )
        browser_a = TestClient(_build_app(), follow_redirects=False)
        login = browser_a.get("/ui/auth/login")
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        # Browser B: separate cookie jar -> no binding cookie for `state`.
        browser_b = TestClient(_build_app(), follow_redirects=False)
        response = browser_b.get(
            f"/ui/auth/callback?code=attacker-code&state={state}",
            headers=_JSON_ACCEPT,
        )
    assert response.status_code == 400
    assert response.json()["detail"] == AUTHORIZATION_STATE_EXPIRED_DETAIL
    # Rejected before the token exchange -- no session, no token POST.
    assert not token_route.called
    assert SESSION_COOKIE_NAME not in response.cookies


def test_callback_same_browser_succeeds_and_clears_binding_cookie() -> None:
    """F10 (#272): the initiating browser completes login; the cookie is cleared.

    Same-browser success is the legitimate path -- it must still work --
    and the single-use binding cookie is expired on the success response.
    """
    access_token, jwks = _mint_access_token()
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, jwks=jwks)
        mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(200, json=_token_json(access_token)),
        )
        client = _https_client()
        login = client.get("/ui/auth/login?return_to=/ui/dashboard")
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        callback = client.get(f"/ui/auth/callback?code=test-code&state={state}")
    assert callback.status_code == 302
    assert callback.headers["location"] == "/ui/dashboard"
    assert SESSION_COOKIE_NAME in callback.cookies
    # The binding cookie is expired (Max-Age=0) on the success response.
    name = login_binding_cookie_name(state)
    binding_header = next(
        h for h in callback.headers.get_list("set-cookie") if h.startswith(f"{name}=")
    )
    assert "max-age=0" in binding_header.lower()


def test_concurrent_logins_bind_independently_and_both_complete() -> None:
    """F10 (#272): concurrent logins in one browser must not break binding.

    Two logins from one browser get distinct per-``state`` cookie names,
    so the second does not clobber the first's binding, and both
    callbacks complete.
    """
    access_token, jwks = _mint_access_token()
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, jwks=jwks)
        mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(200, json=_token_json(access_token)),
        )
        client = _https_client()
        login1 = client.get("/ui/auth/login?return_to=/ui/one")
        state1 = parse_qs(urlparse(login1.headers["location"]).query)["state"][0]
        login2 = client.get("/ui/auth/login?return_to=/ui/two")
        state2 = parse_qs(urlparse(login2.headers["location"]).query)["state"][0]
        # Distinct per-flow cookie names -- login2 did not overwrite the
        # login1 binding cookie.
        assert login_binding_cookie_name(state1) != login_binding_cookie_name(state2)
        cb1 = client.get(f"/ui/auth/callback?code=c1&state={state1}")
        cb2 = client.get(f"/ui/auth/callback?code=c2&state={state2}")
    assert cb1.status_code == 302
    assert cb1.headers["location"] == "/ui/one"
    assert cb2.status_code == 302
    assert cb2.headers["location"] == "/ui/two"


def test_exchange_code_rejects_missing_browser_binding() -> None:
    """F10 (#272): the flow primitive fails closed when no binding is presented."""
    from meho_backplane.ui.auth.flow import OAuthFlowError

    async def _go() -> None:
        with respx.mock(assert_all_called=False) as mock_router:
            _mock_oidc_metadata(mock_router)
            _url, state, _binding = await build_authorization_request(
                redirect_uri=_REDIRECT_URI,
                return_to="/ui/",
            )
            with pytest.raises(OAuthFlowError):
                await exchange_code_for_tokens(
                    redirect_uri=_REDIRECT_URI,
                    authorization_response=f"{_REDIRECT_URI}?code=c&state={state}",
                    state=state,
                    browser_binding=None,
                )

    asyncio.run(_go())


def test_exchange_code_rejects_mismatched_browser_binding() -> None:
    """F10 (#272): a binding value that is not the stored secret fails closed.

    No token-endpoint mock is registered, so a regression that let the
    exchange proceed past the mismatch would raise a respx "not mocked"
    error rather than silently pass.
    """
    from meho_backplane.ui.auth.flow import OAuthFlowError

    async def _go() -> None:
        with respx.mock(assert_all_called=False) as mock_router:
            _mock_oidc_metadata(mock_router)
            _url, state, binding = await build_authorization_request(
                redirect_uri=_REDIRECT_URI,
                return_to="/ui/",
            )
            with pytest.raises(OAuthFlowError):
                await exchange_code_for_tokens(
                    redirect_uri=_REDIRECT_URI,
                    authorization_response=f"{_REDIRECT_URI}?code=c&state={state}",
                    state=state,
                    browser_binding=f"{binding}-tampered",
                )

    asyncio.run(_go())


def test_exchange_code_rejects_non_ascii_browser_binding() -> None:
    """F10 (#272): a hostile non-ASCII binding fails closed, not 500.

    ``hmac.compare_digest`` refuses non-ASCII ``str`` operands; the
    guard must map that to the recoverable ``OAuthFlowError`` rather
    than let a ``TypeError`` escape as a 500.
    """
    from meho_backplane.ui.auth.flow import OAuthFlowError

    async def _go() -> None:
        with respx.mock(assert_all_called=False) as mock_router:
            _mock_oidc_metadata(mock_router)
            _url, state, _binding = await build_authorization_request(
                redirect_uri=_REDIRECT_URI,
                return_to="/ui/",
            )
            with pytest.raises(OAuthFlowError):
                await exchange_code_for_tokens(
                    redirect_uri=_REDIRECT_URI,
                    authorization_response=f"{_REDIRECT_URI}?code=c&state={state}",
                    state=state,
                    browser_binding="bindïng-with-nön-ascii",
                )

    asyncio.run(_go())


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
    # The "and clears" part of the test name -- a stale browser
    # cookie under the same name MUST be emitted with an expiry
    # in the past so the next request lands without a cookie. A
    # regression where ``_handle_logout`` skips the clear on the
    # no-cookie path would leave a phantom cookie alive.
    set_cookie = response.headers.get("set-cookie", "").lower()
    assert f"{SESSION_COOKIE_NAME.lower()}=" in set_cookie
    assert "max-age=0" in set_cookie or "expires=" in set_cookie


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
# Read-path token revalidation -- drift-gated revocation-lag bound
# ---------------------------------------------------------------------------


_TOKEN_ENDPOINT_URL = f"{DEFAULT_ISSUER}/protocol/openid-connect/token"
_JWKS_URL = f"{DEFAULT_ISSUER}/protocol/openid-connect/certs"


def _seed_session_with_token(access_token: str) -> uuid.UUID:
    """Insert a live ``web_session`` row holding *access_token*."""

    async def _seed() -> uuid.UUID:
        from datetime import timedelta

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub="op-77",
                tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
                access_token=access_token,
                refresh_token="refresh-token-1",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    return asyncio.run(_seed())


def _build_read_probe_app() -> FastAPI:
    """The BFF app plus a read probe gated exactly like production reads.

    The probe declares ``Depends(require_ui_session)`` -- the dependency
    every read-render ``/ui/*`` GET handler uses -- so the revalidation
    tests exercise the same middleware + dependency chain as the real
    read surfaces, not just the bare middleware passthrough.
    """
    from fastapi import Depends

    from meho_backplane.ui.auth import UISessionContext

    app = _build_app(include_dummy_ui_route=False)

    @app.get("/ui/read-probe")
    async def read_probe(
        ctx: UISessionContext = Depends(require_ui_session),
    ) -> dict[str, str]:
        return {"operator": ctx.operator_sub}

    return app


def _pin_revalidation_threshold(monkeypatch: pytest.MonkeyPatch, seconds: str) -> None:
    monkeypatch.setenv("UI_SESSION_READ_REVALIDATION_SECONDS", seconds)
    get_settings.cache_clear()


def test_read_path_redirects_when_stored_token_fails_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored token that fails the JWT chain bounces the read to login.

    Threshold 0 = strict revalidate-every-request mode, so the very
    first GET past the middleware presents the (structurally invalid)
    stored token to the chain and must redirect -- not render.
    """
    _pin_revalidation_threshold(monkeypatch, "0")
    session_id = _seed_session_with_token("not-a-jwt")

    with respx.mock(assert_all_called=False) as mock_router:
        # The JWT chain warms the JWKS cache before classifying the
        # malformed token, so discovery + JWKS must resolve.
        _mock_oidc_metadata(mock_router, jwks=public_jwks(make_rsa_keypair("kid-reval-0")))
        client = TestClient(_build_read_probe_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        response = client.get("/ui/read-probe")

    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_read_path_revoked_grant_bounces_to_login_after_token_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IdP-side revocation surfaces within the documented lag window.

    A revoked-but-unexpired JWT still verifies (revocation does not
    invalidate an issued signature), so the read path notices at the
    first revalidation past the token's ``exp``: the reactive refresh
    presents the refresh grant, Keycloak answers ``invalid_grant``,
    and the operator is redirected to login instead of getting the
    previous behaviour -- authorized renders until the 12 h absolute
    session lifetime.
    """
    from structlog.testing import capture_logs

    _pin_revalidation_threshold(monkeypatch, "0")
    key = make_rsa_keypair("kid-reval-1")
    expired = mint_token(key, sub="op-77", expires_in=-120)
    session_id = _seed_session_with_token(expired)

    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, jwks=public_jwks(key))
        token_route = mock_router.post(_TOKEN_ENDPOINT_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"}),
        )
        with capture_logs() as captured:
            client = TestClient(_build_read_probe_app(), follow_redirects=False)
            client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
            response = client.get("/ui/read-probe")

    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")
    assert token_route.call_count == 1
    failed = [e for e in captured if e["event"] == "ui_read_session_revalidation_failed"]
    assert len(failed) == 1
    assert failed[0]["session_id"] == str(session_id)
    # No token material in any captured event.
    assert expired not in repr(captured)


def test_read_path_expired_token_refreshes_silently_and_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merely-expired token refreshes through the reactive leg -> 200.

    The revalidation must not log active operators out every token
    TTL: an expired-but-refreshable token rotates silently (RFC 9700
    one-time-use pair) and the read renders.
    """
    _pin_revalidation_threshold(monkeypatch, "0")
    key = make_rsa_keypair("kid-reval-2")
    expired = mint_token(key, sub="op-77", expires_in=-120)
    fresh = mint_token(key, sub="op-77")
    session_id = _seed_session_with_token(expired)

    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, jwks=public_jwks(key))
        token_route = mock_router.post(_TOKEN_ENDPOINT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": fresh,
                    "refresh_token": "refresh-token-2",
                    "expires_in": 300,
                    "token_type": "Bearer",
                },
            ),
        )
        client = TestClient(_build_read_probe_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        response = client.get("/ui/read-probe")

    assert response.status_code == 200
    assert response.json() == {"operator": "op-77"}
    assert token_route.call_count == 1

    async def _check_rotated() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            rotated = await load_session(session, session_id)
            assert rotated is not None
            assert rotated.access_token == fresh
            assert rotated.refresh_token == "refresh-token-2"

    asyncio.run(_check_rotated())


def test_read_path_revalidation_hits_cached_jwks_no_outbound_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid token revalidates against the cached JWKS -- zero IdP calls.

    Even in strict revalidate-every-request mode, per-request cost is
    one in-memory signature check: the JWKS document is fetched once
    (cache warm-up) and the token endpoint is never contacted while
    the access token is unexpired.
    """
    _pin_revalidation_threshold(monkeypatch, "0")
    key = make_rsa_keypair("kid-reval-3")
    valid = mint_token(key, sub="op-77")
    session_id = _seed_session_with_token(valid)

    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        jwks_route = mock_router.get(_JWKS_URL).mock(
            return_value=httpx.Response(200, json=public_jwks(key)),
        )
        token_route = mock_router.post(_TOKEN_ENDPOINT_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"}),
        )
        client = TestClient(_build_read_probe_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        first = client.get("/ui/read-probe")
        second = client.get("/ui/read-probe")

    assert first.status_code == 200
    assert second.status_code == 200
    # One JWKS warm-up fetch, then cache hits; no refresh round-trips.
    assert jwks_route.call_count == 1
    assert token_route.call_count == 0


def test_read_path_within_drift_window_skips_revalidation() -> None:
    """Inside the drift window the read path never touches the token.

    Under the default threshold (300 s) the first sight of a session
    seeds the anchor -- the token was validated at creation by the
    callback -- and immediate follow-up reads render without a JWT
    decode. This pins the amortisation contract: the per-request hot
    path inside the window stays token-free, and the documented lag
    window (token TTL + threshold) is the trade for it.
    """
    session_id = _seed_session_with_token("not-a-jwt")

    with respx.mock(assert_all_called=False):
        client = TestClient(_build_read_probe_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        first = client.get("/ui/read-probe")
        second = client.get("/ui/read-probe")

    assert first.status_code == 200
    assert second.status_code == 200


# ---------------------------------------------------------------------------
# PKCE verifier store -- in-process semantics
# ---------------------------------------------------------------------------


def test_pkce_verifier_store_pop_is_single_use() -> None:
    """A second pop on the same state yields ``None``."""

    async def _go() -> None:
        store = PKCEVerifierStore()
        await store.put("state-1", code_verifier="v", return_to="/ui/", browser_binding="b1")
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
        await store.put("state-stale", code_verifier="v1", return_to="/ui/", browser_binding="bs")
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
        await store.put("state-fresh", code_verifier="v2", return_to="/ui/", browser_binding="bf")
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
            url, state, _binding = await build_authorization_request(
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
                    browser_binding=None,
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
        client = _https_client()
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


# ---------------------------------------------------------------------------
# RFC 8707 resource indicator -- fail-closed on unset BACKPLANE_URL (#964)
# ---------------------------------------------------------------------------


def test_resource_indicator_fails_closed_when_backplane_url_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resource_indicator`` raises when ``BACKPLANE_URL`` is empty.

    Regression for #964 M1. Previously the helper returned ``""`` on an
    unset ``backplane_url``; authlib forwarded ``resource=`` verbatim
    and Keycloak silently dropped it, so the OAuth flow proceeded
    without the RFC 8707 audience binding. The fail-closed shape
    surfaces the misconfig via the route handlers' existing
    :class:`OAuthFlowConfigurationError` catch (503 with remediation).
    """
    from meho_backplane.ui.auth.flow import (
        OAuthFlowConfigurationError,
        _resource_indicator,
    )

    monkeypatch.setenv("BACKPLANE_URL", "")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.backplane_url == ""

    with pytest.raises(OAuthFlowConfigurationError) as exc_info:
        _resource_indicator(settings)
    assert "backplane_url_unset_cannot_derive_resource_indicator" in str(exc_info.value)


def test_build_authorization_request_503s_when_backplane_url_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``BACKPLANE_URL`` propagates to a 503 on ``/ui/auth/login``.

    The new :class:`OAuthFlowConfigurationError` raised from
    ``_resource_indicator`` reaches the route handler via
    :func:`build_authorization_request`; the existing
    ``except OAuthFlowConfigurationError`` block at
    ``routes.py:288`` maps it to a 503. This confirms the call-chain
    plumbing the task body's AC #2 calls out -- no new route plumbing
    needed.
    """
    monkeypatch.setenv("BACKPLANE_URL", "")
    get_settings.cache_clear()
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/login")
    assert response.status_code == 503
