# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end tests for ``GET /api/v1/auth-config``.

The route is unauthenticated — these tests deliberately verify that
property by NOT passing an ``Authorization`` header and asserting the
response is 200 (not 401). The CLI's device-code flow hits this
endpoint before it has a JWT, so any future regression that adds auth
would deadlock ``meho login``.

The wire shape is locked to the CLI's parser in
``cli/internal/cmd/login.go``'s ``fetchBackplaneAuthConfig``: it
expects exactly ``keycloak_issuer`` and ``audience`` JSON keys. Field
renames here are wire-compat breaks. The shape assertion below would
catch that.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from meho_backplane.main import app
from meho_backplane.settings import get_settings


@pytest.fixture
def _auth_config_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Pin the env vars ``Settings`` reads, then clear the lru_cache.

    The settings singleton is cached at module scope by
    :func:`functools.lru_cache`. Without the cache clear, a stale
    instance constructed in an earlier test would survive into this
    one and return the previous values regardless of the monkeypatched
    env. The conftest's autouse fixture handles ``DATABASE_URL`` and
    re-clears the cache around every test; we set the auth-specific
    keys this route reads.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.evba.lab/realms/evba")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.evba.lab")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_auth_config_returns_keycloak_issuer_and_audience(
    _auth_config_settings: None,
) -> None:
    """Happy path: response carries the values the CLI parses.

    Wire shape locked to the CLI's expectation in
    ``cli/internal/cmd/login.go``'s ``fetchBackplaneAuthConfig`` —
    the parser reads ``keycloak_issuer`` and ``audience`` JSON keys.
    """
    with TestClient(app) as client:
        response = client.get("/api/v1/auth-config")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "keycloak_issuer": "https://keycloak.evba.lab/realms/evba",
        "audience": "meho-backplane",
    }


def test_auth_config_normalises_trailing_slash_on_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Keycloak issuer URL is returned without a trailing slash.

    Keycloak's discovery document advertises the canonical issuer
    without a trailing slash; the CLI then builds
    ``<issuer>/.well-known/openid-configuration``. Without
    normalisation a double slash would appear in the discovery URL.
    The route's :func:`str.rstrip` should drop trailing slashes
    regardless of how the operator typed the env var.
    """
    monkeypatch.setenv(
        "KEYCLOAK_ISSUER_URL",
        "https://keycloak.evba.lab/realms/evba/",
    )
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.evba.lab")
    get_settings.cache_clear()

    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/auth-config")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["keycloak_issuer"] == "https://keycloak.evba.lab/realms/evba"
    assert not payload["keycloak_issuer"].endswith("/")


def test_auth_config_is_unauthenticated(
    _auth_config_settings: None,
) -> None:
    """Calling the route without an ``Authorization`` header returns 200.

    This is the load-bearing security property: the CLI's
    device-code flow needs this endpoint **before** it has a JWT. If
    a future change accidentally adds ``Depends(verify_jwt_and_bind)``
    to the route, this test will start returning 401 and fail loud,
    preventing a regression that would deadlock ``meho login``.
    """
    with TestClient(app) as client:
        response = client.get("/api/v1/auth-config")

    assert response.status_code == 200
    # Sanity: a request that DID include an Authorization header
    # should also succeed (the route ignores it). This catches a
    # regression that requires auth without rejecting unauthenticated.
    with TestClient(app) as client:
        response_with_header = client.get(
            "/api/v1/auth-config",
            headers={"Authorization": "Bearer not-actually-validated"},
        )
    assert response_with_header.status_code == 200


def test_auth_config_route_is_registered_in_main_app() -> None:
    """The route table contains exactly one ``/api/v1/auth-config`` GET.

    Catches a regression where the router import lands but
    ``app.include_router(api_v1_auth_config_router)`` is removed from
    ``meho_backplane.main`` — the module imports fine but the endpoint
    404s in production. The assertion is path-and-method-specific so a
    typo in the prefix would surface as a mismatch.
    """
    matching_routes = [
        route
        for route in app.routes
        # FastAPI's APIRoute exposes ``path`` + ``methods``; non-APIRoute
        # entries (Mount, etc.) don't have ``methods`` so guard the
        # attribute access defensively.
        if getattr(route, "path", None) == "/api/v1/auth-config"
        and "GET" in getattr(route, "methods", set())
    ]
    assert len(matching_routes) == 1, (
        f"expected exactly one GET /api/v1/auth-config route, got {len(matching_routes)}"
    )
