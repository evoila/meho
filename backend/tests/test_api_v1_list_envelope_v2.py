# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cross-endpoint conformance for the ``?envelope=v2`` list opt-in.

G0.18-T3 (#1356), completing #1312 acceptance A. The unified list
envelope (``docs/codebase/api-shape-conventions.md`` §2) is an opt-in:
passing ``?envelope=v2`` returns ``{items, next_cursor?, ...sidecars}``;
omitting it keeps the v0.8.0 default shape so no client breaks.

This module asserts the shape contract uniformly across the four sister
list endpoints widened in that Task (``conventions``, ``audit/my-recent``,
``broadcast/overrides``, ``connectors``) plus the two runbook list
endpoints (``runbooks/templates`` / ``runbooks/runs``) that shipped after
the §2 sweep and joined the opt-in in G0.22-T6 (#1611), in one place. The
two endpoints that adopted the opt-in in #1312 (``targets`` and the
topology ``dependents`` / ``dependencies`` endpoints) keep their own
envelope tests in ``test_api_v1_targets.py`` / ``test_api_v1_topology.py``,
so every §2 surface is covered. The per-endpoint behavioural suites own the
data-bearing pagination / sidecar assertions; this file pins the envelope
*contract* — that every endpoint honours the param, returns the unified
shape with the param, and is byte-shape-unchanged without it.

The tests drive the production ``meho_backplane.main:app`` so the real
middleware + router chain is exercised, against the autouse per-test
SQLite DB.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._vault_fakes import install_fake_vault

_TENANT = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Enable auth + point at the mock IdP (mirrors the per-router suites)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    with TestClient(app) as test_client:
        yield test_client


def _token(key: Any) -> str:
    """Mint a tenant_admin JWT (the most permissive built-in tier).

    ``tenant_admin`` satisfies every list endpoint's RBAC gate
    (``broadcast/overrides`` requires it; the rest require only
    ``operator``, which ``tenant_admin`` subsumes).
    """
    return mint_token(
        key,
        sub="ops@example.com",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(_TENANT),
    )


async def _seed_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        from sqlalchemy import select

        existing = await session.execute(select(Tenant).where(Tenant.id == _TENANT))
        if existing.scalar_one_or_none() is None:
            session.add(Tenant(id=_TENANT, slug="tenant-env", name="Tenant Env"))
            await session.commit()


#: (path, default_key). ``default_key`` is the key the v0.8.0 default
#: shape wraps its list under, or ``None`` when the default is a bare
#: JSON array. The topology node-scoped endpoints + ``targets`` keep
#: their envelope tests in their own suites; here we cover the four
#: sister endpoints widened by #1356 plus the two runbook list
#: endpoints widened by #1611.
_CASES = [
    pytest.param("/api/v1/conventions", "entries", id="conventions"),
    pytest.param("/api/v1/audit/my-recent", "rows", id="audit-my-recent"),
    pytest.param("/api/v1/broadcast/overrides", None, id="broadcast-overrides"),
    pytest.param("/api/v1/connectors", "connectors", id="connectors"),
    pytest.param("/api/v1/runbooks/templates", "templates", id="runbook-templates"),
    pytest.param("/api/v1/runbooks/runs", "runs", id="runbook-runs"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "default_key"), _CASES)
async def test_envelope_v2_returns_unified_shape(
    client: TestClient,
    path: str,
    default_key: str | None,
) -> None:
    """``?envelope=v2`` returns ``{items, next_cursor, ...}`` on every endpoint."""
    await _seed_tenant()
    key = make_rsa_keypair("kid-A")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"{path}?envelope=v2",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, dict)
    # The §2 contract: items always present, next_cursor always present.
    assert isinstance(body["items"], list)
    assert "next_cursor" in body
    # The default-shape list key must NOT leak into the v2 envelope.
    if default_key is not None:
        assert default_key not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "default_key"), _CASES)
async def test_default_shape_unchanged(
    client: TestClient,
    path: str,
    default_key: str | None,
) -> None:
    """Omitting ``?envelope=`` keeps the v0.8.0 default shape (no client breaks)."""
    await _seed_tenant()
    key = make_rsa_keypair("kid-A")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            path,
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    if default_key is None:
        # broadcast/overrides default is a bare JSON array.
        assert isinstance(body, list)
    else:
        assert isinstance(body, dict)
        assert default_key in body
        # The v2-only ``items`` key must NOT appear in the default shape.
        assert "items" not in body
