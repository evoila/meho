# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Startup advisory for an unenforced Vault tenant-scope guard (#1673).

The application-layer ``vault.kv.*`` tenant-scope guard (#1643) is opt-in
and default-off (``VAULT_KV_TENANT_SCOPE_PREFIX=""``). On a deploy whose
Vault layout *is* tenant-partitioned, leaving the prefix empty silently
turns the guard into a no-op, so cross-tenant ``vault.kv.*`` isolation is
unenforced at the app layer with no signal.

:func:`meho_backplane.main._advise_vault_tenant_scope_unenforced` runs in
the FastAPI lifespan and emits exactly one structured
``vault_tenant_scope_unenforced`` advisory when the prefix is unset. These
tests assert it fires when unset, stays silent when set, and does not
change boot behaviour either way (it is observability-only — no raise).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from meho_backplane.main import _advise_vault_tenant_scope_unenforced, app
from meho_backplane.settings import get_settings

_ADVISORY_EVENT = "vault_tenant_scope_unenforced"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the required env the lifespan / ``Settings`` construction needs.

    ``get_settings()`` reads ``KEYCLOAK_ISSUER_URL`` / ``KEYCLOAK_AUDIENCE``
    / ``VAULT_ADDR`` as hard ``os.environ[...]`` lookups, and the lifespan's
    MCP-audience guard needs a resolvable ``BACKPLANE_URL``. The conftest
    autouse fixtures pin only ``DATABASE_URL`` / ``RETRIEVAL_MODEL_CACHE_DIR``
    / ``BACKPLANE_URL``; this module owns the rest so each test drives only
    ``VAULT_KV_TENANT_SCOPE_PREFIX``.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_advisory_fires_once_when_prefix_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty prefix → exactly one structured advisory naming the env var."""
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    try:
        with capture_logs() as captured:
            _advise_vault_tenant_scope_unenforced()
    finally:
        get_settings.cache_clear()

    advisories = [e for e in captured if e.get("event") == _ADVISORY_EVENT]
    assert len(advisories) == 1
    event = advisories[0]
    assert event["log_level"] == "warning"
    assert event["enable_via"] == "VAULT_KV_TENANT_SCOPE_PREFIX"
    assert event["doc"] == "docs/codebase/connectors-vault-tenant-scope.md"


def test_advisory_silent_when_prefix_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured prefix → the guard is enforced, so no advisory is logged."""
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "tenant-{tenant_id}/")
    get_settings.cache_clear()
    try:
        with capture_logs() as captured:
            _advise_vault_tenant_scope_unenforced()
    finally:
        get_settings.cache_clear()

    advisories = [e for e in captured if e.get("event") == _ADVISORY_EVENT]
    assert advisories == []


def test_advisory_does_not_block_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The advisory is observability-only: the lifespan still boots to serving.

    Running the full lifespan with the prefix unset exercises the advisory
    on the real startup path and asserts it does not raise — the app
    reaches a serving state and ``GET /`` returns its identity payload.
    """
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            response = client.get("/")
        assert response.status_code == 200
    finally:
        get_settings.cache_clear()
