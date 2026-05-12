# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``/.well-known/oauth-protected-resource`` metadata route.

Covers G0.5-T2 (#247) acceptance criteria for the discovery surface:

* The metadata document is unauthenticated (no Bearer required).
* The ``resource`` field matches the canonical MCP URI derived from
  ``MCP_RESOURCE_URI`` or, falling back, from ``BACKPLANE_URL``.
* ``authorization_servers`` contains the configured Keycloak issuer
  with no trailing slash.
* ``scopes_supported`` advertises the v0.2 scope namespace.
* ``bearer_methods_supported`` advertises only ``["header"]`` per the
  MCP transport spec's ban on URI-query tokens.

The auth-chain tests for ``/mcp`` itself live in :mod:`tests.test_mcp_auth`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from meho_backplane.main import app
from meho_backplane.settings import get_settings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """``TestClient`` with the MCP settings pinned for the well-known route.

    The metadata document reads ``BACKPLANE_URL`` /
    ``MCP_RESOURCE_URI`` / ``KEYCLOAK_ISSUER_URL`` at request time.
    The shared conftest sets KEYCLOAK_* defaults; the MCP-specific
    pieces are pinned here per-test so the asserts below can verify
    the wire shape against known values.
    """
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("MCP_RESOURCE_URI", "")
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def test_well_known_returns_required_rfc9728_fields(client: TestClient) -> None:
    """The metadata document carries every field RFC 9728 requires or recommends."""
    response = client.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    body = response.json()

    # RFC 9728 §3 required.
    assert body["resource"] == "https://meho.test/mcp"

    # MCP 2025-06-18 §Authorization Server Discovery: authorization_servers
    # MUST contain at least one issuer identifier.
    assert body["authorization_servers"] == ["https://keycloak.test/realms/meho"]

    # RFC 9728 §3 recommended.
    assert body["scopes_supported"] == ["mcp:read", "mcp:execute"]

    # MCP transport §Access Token Usage: "Access tokens MUST NOT be included
    # in the URI query string" — only header-bound tokens are accepted.
    assert body["bearer_methods_supported"] == ["header"]


def test_well_known_does_not_require_authentication(client: TestClient) -> None:
    """RFC 9728 discovery is the bootstrap step; it MUST be unauthenticated.

    A request without ``Authorization`` returns 200, not 401 — otherwise
    a fresh MCP client could never reach the metadata document to learn
    where to obtain a token.
    """
    response = client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200


def test_well_known_honors_explicit_mcp_resource_uri_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``MCP_RESOURCE_URI`` setting overrides the derived URI.

    Operators with non-default MCP mounts (e.g. ``/api/mcp``) set
    ``MCP_RESOURCE_URI`` explicitly; the metadata document echoes it
    verbatim per RFC 9728 §3 / MCP §"Resource Parameter Implementation".
    """
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("MCP_RESOURCE_URI", "https://meho.test/api/mcp")
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    try:
        client = TestClient(app)
        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200
        assert response.json()["resource"] == "https://meho.test/api/mcp"
    finally:
        get_settings.cache_clear()


def test_well_known_strips_trailing_slash_on_backplane_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical URI form is no-trailing-slash per MCP spec.

    MCP 2025-06-18 §"Canonical Server URI" notes both
    ``https://mcp.example.com/`` and ``https://mcp.example.com`` are
    valid absolute URIs but implementations SHOULD use the no-slash
    form. The helper strips a trailing slash on ``BACKPLANE_URL``
    before concatenating ``/mcp`` so a mis-set env var doesn't produce
    a double-slash ``https://meho.test//mcp``.
    """
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test/")
    monkeypatch.setenv("MCP_RESOURCE_URI", "")
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    try:
        client = TestClient(app)
        response = client.get("/.well-known/oauth-protected-resource")
        assert response.json()["resource"] == "https://meho.test/mcp"
    finally:
        get_settings.cache_clear()


def test_well_known_keycloak_issuer_url_has_no_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``authorization_servers`` entries strip trailing slashes.

    Pydantic's ``HttpUrl`` appends a trailing slash to URLs without a
    path component; the metadata document strips it so downstream
    clients can use the value verbatim as the issuer identifier when
    constructing AS-metadata URLs per RFC 8414.

    The env var is deliberately set **with** a trailing slash so the
    test actually exercises the strip — a previous version of this
    test set the value without a trailing slash, which made the
    ``not server.endswith("/")`` assertion pass trivially regardless
    of whether the strip logic ran.
    """
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("MCP_RESOURCE_URI", "")
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho/")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    try:
        client = TestClient(app)
        body = client.get("/.well-known/oauth-protected-resource").json()
        # Exact equality, not just "doesn't end with /" — pins the
        # canonical form against future regressions.
        assert body["authorization_servers"] == [
            "https://keycloak.test/realms/meho",
        ]
    finally:
        get_settings.cache_clear()
