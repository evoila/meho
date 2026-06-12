# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chassis smoke test for the FastAPI ``/ui/*`` integration (Task #866, T5).

Exercises every acceptance criterion on issue #866:

* ``GET /ui/`` unauthenticated -> 302 ``/ui/auth/login`` with the
  ``return_to`` query parameter pointing back to ``/ui/``.
* ``GET /ui/auth/login`` -> 302 to Keycloak (mocked via ``respx``).
* Simulated callback with valid ``code`` + ``state`` -> session row
  created -> 302 to ``/ui/``.
* ``GET /ui/`` with the ``meho_session`` cookie -> 200 carrying
  ``<title>MEHO Operator Console`` + the 3x2 surface grid + sidebar
  links to all five surfaces.
* ``GET /ui/{broadcast,knowledge,topology,connectors,memory}`` ->
  200 with placeholder content.
* State-changing ``/ui/*`` request without a valid CSRF token -> 403.
* Middleware order verified: ``/ui/*`` uses session-cookie auth;
  ``/api/*`` still uses JWT (sanity check).

Test app construction mirrors :mod:`backend.tests.test_ui_auth_flow`:
a minimal :class:`FastAPI` instance with the BFF auth router + the
UI routes + the session and CSRF middlewares. The session-cookie
attribute set ``Secure=True`` would otherwise prevent the TestClient
(non-TLS) from sending the cookie on subsequent requests; the test
patches the cookie attributes via the TestClient cookie jar.

The autouse fixtures in :mod:`backend.tests.conftest`
(``_default_database_url`` + ``_schema_template_db``) provide a fresh
file-backed SQLite DB migrated to head before every test, so the
``web_session`` table is present without any per-test
``alembic upgrade head`` replay.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import (
    SESSION_COOKIE_NAME,
    UISessionMiddleware,
)
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    EncryptionKeyMissingError,
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    _csrf_secret,
    mint_csrf_token,
)
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
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

#: Five surface routes the chassis sidebar links to. Initiative #337
#: work-item #5 enumerates these exact URLs; the chassis smoke test
#: pins them so a future surface Initiative renaming the path triggers
#: an explicit test break (not a silent sidebar-vs-route divergence).
_SURFACE_ROUTES = ("/ui/broadcast", "/ui/knowledge", "/ui/topology", "/ui/connectors", "/ui/memory")

#: Subset of :data:`_SURFACE_ROUTES` that still render the chassis
#: "Coming soon" stub. ``/ui/topology`` is omitted because Initiative
#: #342 Task #880 (G10.5-T1) replaced the stub with the real table
#: view; ``/ui/broadcast`` is omitted because Initiative #338 Task #867
#: (G10.1-T1) replaced the stub with the real live-feed view. G10.2-G10.4
#: will trim this tuple further as their surface Initiatives land. The
#: chassis smoke test still pins the sidebar links via
#: :data:`_SURFACE_ROUTES` so a sidebar-vs-route divergence surfaces
#: explicitly.
_STUB_SURFACE_ROUTES = ("/ui/knowledge", "/ui/connectors", "/ui/memory")


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test.

    Mirrors :mod:`backend.tests.test_ui_auth_flow._bff_env` so the
    same baseline holds: chassis Keycloak / Vault / DB settings plus
    the BFF encryption key + confidential client credentials.
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
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def _mock_oidc_metadata(
    mock_router: respx.MockRouter,
    *,
    jwks: dict[str, Any] | None = None,
) -> None:
    """Stub Keycloak's discovery + (optional) JWKS endpoints.

    Identical shape to the helper in :mod:`backend.tests.test_ui_auth_flow`;
    duplicated here to keep the smoke test independent of that suite's
    private helpers.
    """
    metadata: dict[str, Any] = {
        "issuer": DEFAULT_ISSUER,
        "authorization_endpoint": _AUTHORIZATION_ENDPOINT,
        "token_endpoint": _TOKEN_ENDPOINT,
        "end_session_endpoint": _END_SESSION_ENDPOINT,
        "jwks_uri": f"{DEFAULT_ISSUER}/protocol/openid-connect/certs",
    }
    mock_router.get(f"{DEFAULT_ISSUER}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json=metadata),
    )
    if jwks is not None:
        mock_router.get(metadata["jwks_uri"]).mock(
            return_value=httpx.Response(200, json=jwks),
        )


#: Dependency reference for the JWT-gated ``/api/sentinel`` route below.
#: Lifted to module scope so the function signature stays compatible
#: with ruff's B008 ("do not call function as default argument") which
#: would trip on ``Depends(...)`` inlined into the parameter list.
_REQUIRE_JWT = Depends(verify_jwt_and_bind)


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app with the full UI chassis wired in.

    Mirrors the production wiring in :mod:`meho_backplane.main`:
    StaticFiles at ``/ui/static`` + the BFF auth router + the
    surface router + ``UISessionMiddleware`` outermost +
    ``CSRFMiddleware`` next. The chassis smoke test does NOT
    register the chassis Audit / RequestContext middlewares because
    the test only exercises the ``/ui/*`` surface; the inner-chain
    coverage lives in their own per-middleware suites. The
    ``/api/sentinel`` route declares
    :func:`~meho_backplane.middleware.verify_jwt_and_bind` so the
    middleware-order acceptance criterion #4 ("/api/* still uses
    JWT") is exercised end-to-end -- a missing Bearer header yields
    401 from the JWT dependency, proving both that the UI session
    middleware passed the request through AND that the production
    JWT contract still gates the API surface.
    """
    app = FastAPI()
    # CSRF middleware registered first so the session middleware
    # ends up outermost -- production wiring is the same shape
    # (``main.py`` registers CSRF then UISession in that order).
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(static_root_dir()), check_dir=False),
        name="ui_static",
    )
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())

    @app.get("/api/sentinel")
    async def _api_sentinel(operator: Operator = _REQUIRE_JWT) -> dict[str, str]:
        # Stand-in for the ``/api/v1/*`` JWT-protected surface. Uses
        # the same :func:`verify_jwt_and_bind` dependency the
        # production ``/api/v1/*`` routes rely on -- so a missing
        # Bearer header yields 401 from the JWT dependency (proving
        # the UI session middleware passed the request through AND
        # the JWT layer rejected it), while a valid Bearer with a
        # mocked JWKS yields 200 (proving the JWT dependency
        # resolves cleanly end-to-end).
        return {"ok": "true", "operator_sub": operator.sub}

    return app


def _trust_test_cookies(client: TestClient) -> None:
    """Mark the TestClient as trusting the ``Secure`` cookies.

    httpx's cookie jar refuses to send ``Secure`` cookies over HTTP.
    TestClient (which httpx underpins) speaks HTTP only; the
    backplane sets the ``Secure`` attribute unconditionally because
    the production deploy terminates TLS at ingress. Patching the
    jar in-place is the canonical workaround documented in the
    Starlette TestClient discussion.
    """
    # No-op shim today; the TestClient transport accepts cookies it
    # received on a 302 redirect chain without checking the Secure
    # flag, and our tests rebuild a fresh TestClient between flow
    # steps. Kept as a single point of override for a future
    # transport-level change.
    _ = client


# ---------------------------------------------------------------------------
# Helpers -- seed a session row + cookie shortcut for authenticated tests
# ---------------------------------------------------------------------------


def _seed_session_sync(
    *,
    operator_sub: str = "op-42",
    tenant_id: uuid.UUID | None = None,
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row directly and return its UUID.

    Bypasses the full callback round-trip for tests that only
    exercise the dashboard render / stub routes. The Fernet key set
    by the autouse fixture is the same one the session-store reads,
    so the seeded row decrypts cleanly on the next ``load_session``.
    """
    tenant = tenant_id or uuid.UUID(DEFAULT_TENANT_ID)

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant,
                access_token="access-token-plaintext",
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_do())


# ---------------------------------------------------------------------------
# AC 1: unauthenticated /ui/ -> 302 /ui/auth/login?return_to=/ui/
# ---------------------------------------------------------------------------


def test_dashboard_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/`` without a session 302s to login with ``return_to=/ui/``."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("/ui/auth/login?return_to=")
    return_to = parse_qs(urlparse(location).query)["return_to"][0]
    assert return_to == "/ui/"


# ---------------------------------------------------------------------------
# AC 2: GET /ui/auth/login -> 302 to Keycloak
# ---------------------------------------------------------------------------


def test_login_redirects_to_keycloak() -> None:
    """``GET /ui/auth/login`` 302s to Keycloak with PKCE + the resource indicator."""
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/auth/login")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(_AUTHORIZATION_ENDPOINT)
    params = parse_qs(urlparse(location).query)
    assert params["code_challenge_method"] == ["S256"]
    assert "state" in params


# ---------------------------------------------------------------------------
# AC 3: callback with valid code+verifier -> session row + 302 to /ui/
# ---------------------------------------------------------------------------


def test_callback_creates_session_and_redirects_to_dashboard() -> None:
    """A valid callback creates a session row and 302s to ``/ui/``."""
    key = make_rsa_keypair("test-kid")
    access_token = mint_token(key)
    jwks = public_jwks(key)
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, jwks=jwks)
        mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": access_token,
                    "refresh_token": "refresh-x",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            ),
        )
        client = TestClient(_build_app(), follow_redirects=False)
        login_response = client.get("/ui/auth/login")  # default return_to=/ui/
        state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
        callback = client.get(
            f"/ui/auth/callback?code=test-code&state={state}",
        )
    assert callback.status_code == 302
    # Default return_to is /ui/ (the login route's safe default).
    assert callback.headers["location"] == "/ui/"
    cookie_value = callback.cookies[SESSION_COOKIE_NAME]
    # Cookie parses as a UUID -- the session row's PK.
    uuid.UUID(cookie_value)


# ---------------------------------------------------------------------------
# AC 4: authenticated /ui/ -> 200 + chassis HTML shape
# ---------------------------------------------------------------------------


def test_dashboard_authenticated_renders_console_html() -> None:
    """``GET /ui/`` with a session 302s nothing -- renders the dashboard HTML."""
    session_id = _seed_session_sync()
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        response = client.get("/ui/")
    assert response.status_code == 200
    body = response.text
    # Page title -- the chassis page_title block resolves to "MEHO
    # Operator Console" via the dashboard.html override.
    assert "<title>MEHO Operator Console" in body
    # Sidebar links to every one of the 5 surface routes.
    for route in _SURFACE_ROUTES:
        assert f'href="{route}"' in body, f"sidebar link to {route} missing"
    # Bento surface grid present (branded ``meho-card`` markup; the
    # rebrand replaced DaisyUI's ``card``/``card-body`` shell).
    # Five surface cards + one deploy card = 6 card blocks.
    assert body.count("meho-card") >= 6
    # HTMX SSE wiring on the recent-activity tray. The dashboard
    # subscribes to ``/api/v1/feed`` with no query parameters; the
    # feed endpoint (G6.1-T4 #310) does not accept a ``limit`` knob
    # and FastAPI silently drops unknown query params, so any
    # ``?limit=N`` here would only be a no-op surface promise. Trim-
    # to-last-N is G10.1 (#338) client-side surface work.
    assert 'sse-connect="/api/v1/feed"' in body
    assert 'sse-swap="broadcast"' in body
    # Version footer renders the chassis version global.
    assert "MEHO backplane v" in body
    # CSRF cookie set on the response so subsequent state-changing
    # requests can echo it back.
    assert CSRF_COOKIE_NAME in response.cookies


# ---------------------------------------------------------------------------
# AC 5: 5 surface stub routes return 200 with placeholder content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", _STUB_SURFACE_ROUTES)
def test_surface_stub_returns_placeholder(route: str) -> None:
    """Each remaining surface stub renders the ``Coming soon`` placeholder.

    ``/ui/topology`` is excluded -- the chassis stub is replaced by
    Initiative #342 Task #880 (G10.5-T1), so the route now returns
    the real tabular surface. Tests for that surface live in
    :mod:`backend.tests.test_ui_topology_table`. The remaining four
    surfaces stay stubs until their own G10.x Initiatives land.
    """
    session_id = _seed_session_sync()
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        response = client.get(route)
    assert response.status_code == 200, f"{route} did not return 200"
    body = response.text
    assert "Coming soon" in body
    # The surface title shows up in the page header AND the <title>
    # suffix (verified by case-insensitive substring match).
    surface_slug = route.rsplit("/", 1)[-1]
    assert surface_slug.lower() in body.lower()


# ---------------------------------------------------------------------------
# AC 6: CSRF rejection on a state-changing request without a valid token
# ---------------------------------------------------------------------------


def test_csrf_rejects_state_change_without_token() -> None:
    """``POST /ui/sentinel`` with a session but no CSRF token -> 403."""
    session_id = _seed_session_sync()

    app = _build_app()

    # Register a single state-changing sentinel route that would
    # otherwise return 200 if the CSRF middleware did not intercept.
    @app.post("/ui/sentinel-write")
    async def _sentinel_write() -> dict[str, str]:
        return {"ok": "true"}

    with respx.mock(assert_all_called=False):
        client = TestClient(app, follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        # No CSRF cookie + no header -> reject.
        response = client.post("/ui/sentinel-write")
    assert response.status_code == 403
    assert response.json()["detail"] == "csrf_token_invalid"


def test_csrf_rejects_state_change_with_mismatched_cookie_header() -> None:
    """Cookie value != header value -> 403 (cookie-injection guard)."""
    session_id = _seed_session_sync()

    app = _build_app()

    @app.post("/ui/sentinel-write")
    async def _sentinel_write() -> dict[str, str]:  # pragma: no cover - never reached
        return {"ok": "true"}

    valid_token = mint_csrf_token(str(session_id))
    with respx.mock(assert_all_called=False):
        client = TestClient(app, follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        client.cookies.set(CSRF_COOKIE_NAME, valid_token)
        # Different value on the header than on the cookie -> reject.
        response = client.post(
            "/ui/sentinel-write",
            headers={CSRF_HEADER_NAME: "tampered-mismatch-token"},
        )
    assert response.status_code == 403


def test_csrf_accepts_matching_token() -> None:
    """A matching cookie + header pair passes the double-submit check."""
    session_id = _seed_session_sync()

    app = _build_app()

    @app.post("/ui/sentinel-write")
    async def _sentinel_write() -> dict[str, str]:
        return {"ok": "true"}

    valid_token = mint_csrf_token(str(session_id))
    with respx.mock(assert_all_called=False):
        client = TestClient(app, follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        client.cookies.set(CSRF_COOKIE_NAME, valid_token)
        response = client.post(
            "/ui/sentinel-write",
            headers={CSRF_HEADER_NAME: valid_token},
        )
    assert response.status_code == 200


def test_csrf_rejects_forged_signature() -> None:
    """Cookie + header agree, but neither validates against the session_id."""
    session_id = _seed_session_sync()

    app = _build_app()

    @app.post("/ui/sentinel-write")
    async def _sentinel_write() -> dict[str, str]:  # pragma: no cover - never reached
        return {"ok": "true"}

    # An attacker who could only inject a cookie value (no access to
    # the HMAC secret) would set both halves to the same arbitrary
    # value. The HMAC binding to the session_id rejects.
    forged = "deadbeef.cafebabe"
    with respx.mock(assert_all_called=False):
        client = TestClient(app, follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        client.cookies.set(CSRF_COOKIE_NAME, forged)
        response = client.post(
            "/ui/sentinel-write",
            headers={CSRF_HEADER_NAME: forged},
        )
    assert response.status_code == 403


def test_csrf_secret_fails_fast_on_empty_encryption_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_csrf_secret`` raises ``EncryptionKeyMissingError`` when the key is empty.

    The CSRF middleware reuses ``UI_SESSION_ENCRYPTION_KEY`` as its HMAC
    keying material (see ``_csrf_secret`` docstring + the OWASP signed
    double-submit cookie pattern). The previous implementation silently
    returned ``b""`` on a missing key, which would mint deterministic
    HMAC tokens an attacker could forge off-line. Mirrors the
    ``_get_fernet`` fail-fast pattern in
    :mod:`meho_backplane.ui.auth.session_store` so a single chassis
    convention covers both the session-store and the CSRF keying
    material.
    """
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(EncryptionKeyMissingError, match="UI_SESSION_ENCRYPTION_KEY"):
        _csrf_secret()


# ---------------------------------------------------------------------------
# AC 7: middleware order -- /ui/* uses cookie auth; /api/* untouched
# ---------------------------------------------------------------------------


def test_middleware_passes_api_routes_through_to_jwt_layer() -> None:
    """``GET /api/sentinel`` without a Bearer header -> 401 from the JWT dependency.

    The UI session middleware is ``/ui/``-scoped; out-of-prefix paths
    must reach the route handler unchanged. ``/api/sentinel`` declares
    :func:`verify_jwt_and_bind` (the production JWT contract on every
    ``/api/v1/*`` route), so an unauthenticated request transits the
    UI middlewares unchanged and is rejected by the JWT dependency
    with a 401 ``missing_token``. The 401 proves BOTH halves of AC4
    ("/ui/* uses session-cookie auth, /api/* uses JWT") --

    * 401 (not 302) -> UI session middleware passed it through.
    * 401 (not 200) -> the JWT dependency is in the call chain.

    Replaces the prior 200 expectation (which only exercised the UI
    middleware half and let the unprotected route silently violate
    AC4's other half, the "/api/* JWT" claim).
    """
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        # No session cookie + no Bearer header; the UI middleware
        # passes /api/sentinel through, the JWT dependency rejects.
        response = client.get("/api/sentinel")
    assert response.status_code == 401, (
        f"expected JWT layer to reject without Bearer; got {response.status_code}: {response.text}"
    )
    # Specific detail code surfaced by ``verify_jwt`` on a missing
    # ``Authorization`` header (see ``auth.jwt._extract_bearer_token``).
    assert response.json() == {"detail": "missing_token"}


def test_middleware_lets_jwt_protected_api_route_through_with_valid_bearer() -> None:
    """Positive control: a valid Bearer reaches ``/api/sentinel`` and returns 200.

    Mirrors the production wiring: a JWT signed by the mocked Keycloak
    JWKS validates cleanly through ``verify_jwt_and_bind`` and the
    sentinel handler returns 200 with the operator's ``sub``. Confirms
    the JWT chain (issuer + audience + signature + tenant claims) is
    actually exercisable by the smoke harness -- without this assertion
    the negative test above could pass simply because the route was
    misconfigured into a permanent 401.
    """
    key = make_rsa_keypair("test-kid")
    access_token = mint_token(key)
    jwks = public_jwks(key)
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, jwks=jwks)
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(
            "/api/sentinel",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    assert response.status_code == 200, (
        f"expected 200 with valid Bearer; got {response.status_code}: {response.text}"
    )
    payload = response.json()
    assert payload["ok"] == "true"
    assert payload["operator_sub"] == "op-42"


def test_middleware_redirects_ui_routes_without_session() -> None:
    """``GET /ui/broadcast`` without a session 302s to login (positive control)."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/broadcast")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_middleware_lets_authenticated_ui_request_through() -> None:
    """Positive control: a valid session unblocks a ``/ui/`` surface route."""
    session_id = _seed_session_sync()
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        response = client.get("/ui/broadcast")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Sanity: full unauth -> login -> callback -> /ui/ end-to-end
# ---------------------------------------------------------------------------


def test_full_flow_unauth_login_callback_then_dashboard() -> None:
    """End-to-end smoke: unauth -> login -> callback -> dashboard render."""
    key = make_rsa_keypair("test-kid")
    access_token = mint_token(key)
    jwks = public_jwks(key)
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, jwks=jwks)
        mock_router.post(_TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": access_token,
                    "refresh_token": "refresh-x",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            ),
        )
        client = TestClient(_build_app(), follow_redirects=False)
        _trust_test_cookies(client)
        # Step 1: hit /ui/ unauthenticated -> 302 to login.
        first = client.get("/ui/")
        assert first.status_code == 302
        # Step 2: follow to /ui/auth/login -> 302 to Keycloak.
        login_target = first.headers["location"]
        assert login_target.startswith("/ui/auth/login")
        login = client.get(login_target)
        assert login.status_code == 302
        # Step 3: simulate Keycloak's redirect back to /ui/auth/callback.
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        callback = client.get(
            f"/ui/auth/callback?code=test-code&state={state}",
        )
        assert callback.status_code == 302
        assert callback.headers["location"] == "/ui/"
        # The session cookie carries ``Secure=True`` (production
        # terminates TLS at ingress); httpx's cookie jar refuses to
        # send Secure cookies over the TestClient's HTTP transport, so
        # we lift the cookie out of the callback response and re-set
        # it on the jar to bypass the Secure filter. Production
        # browsers see this transparently over HTTPS.
        session_cookie = callback.cookies[SESSION_COOKIE_NAME]
        client.cookies.set(SESSION_COOKIE_NAME, session_cookie)
        # Step 4: hit /ui/ again -> 200 + dashboard HTML.
        dash = client.get("/ui/")
    assert dash.status_code == 200
    assert "<title>MEHO Operator Console" in dash.text
