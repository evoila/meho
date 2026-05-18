# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Startup guard for an unresolvable MCP resource URI (G0.8-T4, #633).

The ``/mcp`` router is mounted unconditionally. A deploy that sets
neither ``MCP_RESOURCE_URI`` nor ``BACKPLANE_URL`` leaves the resolved
MCP audience empty, so every ``/mcp`` request fails closed with a 401
and no signal points at the cause — the consumer dogfood signal was an
operator staring at a context-free 401 with the surface dark.

:func:`meho_backplane.main._assert_mcp_resource_uri_configured` runs in
the FastAPI lifespan and converts that silent-dark-surface failure into
a loud startup crash carrying the remediation. These tests assert both
the crash and that the boot still succeeds once the audience resolves
(the chart-derived default path).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import AUDIENCE_NOT_CONFIGURED_REMEDIATION
from meho_backplane.main import _assert_mcp_resource_uri_configured, app
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the non-MCP required env vars ``Settings`` construction needs.

    ``get_settings()`` reads ``KEYCLOAK_ISSUER_URL`` / ``KEYCLOAK_AUDIENCE``
    / ``VAULT_ADDR`` as hard ``os.environ[...]`` lookups; the conftest
    autouse fixtures pin only ``DATABASE_URL`` / ``RETRIEVAL_MODEL_CACHE_DIR``
    / ``BACKPLANE_URL``. These tests target the MCP-audience guard
    specifically, so they own the rest of the required surface and let
    each test drive only ``BACKPLANE_URL`` / ``MCP_RESOURCE_URI``.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_guard_raises_runtimeerror_with_remediation_when_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither env var set → the guard raises with the actionable message."""
    monkeypatch.setenv("BACKPLANE_URL", "")
    monkeypatch.setenv("MCP_RESOURCE_URI", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError) as exc_info:
            _assert_mcp_resource_uri_configured()
    finally:
        get_settings.cache_clear()

    message = str(exc_info.value)
    assert message == AUDIENCE_NOT_CONFIGURED_REMEDIATION
    assert "MCP_RESOURCE_URI" in message
    assert "BACKPLANE_URL" in message
    assert "oidc-audience-mapper" in message
    assert "docs/cross-repo/mcp-client-setup.md" in message


def test_lifespan_aborts_startup_when_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The FastAPI lifespan crashes (not a 200 boot) on an unresolvable URI.

    ``with TestClient(app)`` runs the lifespan; the guard runs after
    the MCP module import step and before the embedding preload, so the
    ``RuntimeError`` propagates out of the context-manager enter and the
    process never reaches a serving state — the deploy-time equivalent
    is CrashLoopBackOff, which is the whole point: a dark ``/mcp`` is
    worse than a loud crash.
    """
    monkeypatch.setenv("BACKPLANE_URL", "")
    monkeypatch.setenv("MCP_RESOURCE_URI", "")
    get_settings.cache_clear()
    try:
        with (
            pytest.raises(RuntimeError, match="audience_not_configured"),
            TestClient(app),
        ):
            pass
    finally:
        get_settings.cache_clear()


def test_lifespan_boots_when_backplane_url_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``BACKPLANE_URL`` set, the derived ``/mcp`` audience resolves.

    This is the chart-derived-default path (the chart injects
    ``BACKPLANE_URL=https://<ingress.host>`` for the common
    ingress-fronted deploy). The guard must pass silently and the app
    must reach a serving state — ``GET /`` returns its identity payload.
    """
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("MCP_RESOURCE_URI", "")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            response = client.get("/")
        assert response.status_code == 200
    finally:
        get_settings.cache_clear()


def test_guard_passes_with_explicit_mcp_resource_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``MCP_RESOURCE_URI`` (non-default mount) satisfies the guard."""
    monkeypatch.setenv("BACKPLANE_URL", "")
    monkeypatch.setenv("MCP_RESOURCE_URI", "https://meho.test/api/mcp")
    get_settings.cache_clear()
    try:
        _assert_mcp_resource_uri_configured()  # must not raise
    finally:
        get_settings.cache_clear()
