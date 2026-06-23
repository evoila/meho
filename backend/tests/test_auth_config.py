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
expects exactly ``keycloak_issuer``, ``audience``, and
``cli_client_id`` JSON keys. Field renames here are wire-compat
breaks. The shape assertion below would catch that.
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
    monkeypatch.setenv("KEYCLOAK_CLI_CLIENT_ID", "meho-cli")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.evba.lab")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_auth_config_returns_keycloak_issuer_audience_and_cli_client_id(
    _auth_config_settings: None,
) -> None:
    """Happy path: response carries the values the CLI parses.

    Wire shape locked to the CLI's expectation in
    ``cli/internal/cmd/login.go``'s ``fetchBackplaneAuthConfig`` —
    the parser reads ``keycloak_issuer``, ``audience``, and
    ``cli_client_id`` JSON keys.
    """
    with TestClient(app) as client:
        response = client.get("/api/v1/auth-config")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "keycloak_issuer": "https://keycloak.evba.lab/realms/evba",
        "audience": "meho-backplane",
        "cli_client_id": "meho-cli",
    }


def test_auth_config_cli_client_id_defaults_empty_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KEYCLOAK_CLI_CLIENT_ID`` unset → ``cli_client_id`` is ``""``.

    The G0.9.1-T9 contract keeps backwards compatibility with the
    v0.3.1 endpoint by making ``KEYCLOAK_CLI_CLIENT_ID`` optional: the
    field still appears on every response (so the CLI's parser shape
    stays stable), but the CLI distinguishes empty-string from "field
    was set" and emits an actionable public-client error rather than
    silently falling back to ``audience``.

    Pinning this behaviour in a test stops a future contributor from
    "fixing" the empty default by raising at startup — that would break
    every existing deployment on upgrade and shifts the actionable
    error from the CLI (where the operator sees it) to a backplane
    CrashLoopBackOff (where they don't).
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.evba.lab/realms/evba")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.delenv("KEYCLOAK_CLI_CLIENT_ID", raising=False)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.evba.lab")
    get_settings.cache_clear()

    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/auth-config")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["cli_client_id"] == ""
    # Sanity: the other fields still populate.
    assert payload["keycloak_issuer"] == "https://keycloak.evba.lab/realms/evba"
    assert payload["audience"] == "meho-backplane"


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
    monkeypatch.setenv("KEYCLOAK_CLI_CLIENT_ID", "meho-cli")
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
    openapi_paths = app.openapi()["paths"]
    assert "/api/v1/auth-config" in openapi_paths, (
        "expected /api/v1/auth-config in OpenAPI paths"
    )
    assert "get" in openapi_paths["/api/v1/auth-config"], (
        "expected GET /api/v1/auth-config in OpenAPI paths"
    )
