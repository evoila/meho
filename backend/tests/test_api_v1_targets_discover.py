# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``GET /api/v1/targets/discover``.

G9.1-T5 / Task #453. The discover route lives on the targets router
(under the ``/api/v1/targets`` prefix) but is exercised separately
here because its dependency surface (the connector registry +
``list_candidates`` hook) is distinct from the CRUD routes covered by
``test_api_v1_targets.py``.

Coverage matrix:

* **Route mounting + ordering** — ``/api/v1/targets/discover`` appears
  in the OpenAPI doc, and the literal ``/discover`` segment is matched
  as this route rather than captured as a target name by
  ``GET /{name}`` (declaration order is load-bearing).
* **Merge** — candidates from every connector registered for the
  product are merged into ``discovered``.
* **Skipped** — a connector that returns nothing is recorded in
  ``skipped`` with ``reason="no candidates"``; a connector that
  raises is recorded with the exception summary and does NOT abort
  the sweep (the other connectors still run).
* **seed_target** — when supplied, it is resolved tenant-scoped and
  forwarded to ``list_candidates``; an unknown seed name 404s.
* **RBAC** — ``operator`` minimum; ``read_only`` gets 403.
* **Unauthenticated** — 401 without a token.
* **Audit op_id** — the audit row's payload carries
  ``op_id="targets.discover"`` + ``op_class="read"``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.targets import router as targets_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import (
    CandidateHint,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks
from ._targets_helpers import _insert_target

_TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")
# Hyphen-free product whose value equals the first hyphen-segment of every
# impl_id registered below. register_connector_v2 derives the connector_id as
# f"{impl_id}-{version}", and connectors/registry._assert_product_impl_id_round_trips
# hard-fails when the declared product does not round-trip back out of it. A
# hyphenated product (e.g. "discover-product") parses to only its first segment
# ("discover"), so it can never round-trip — hence the flat token + prefixed impl_ids.
_PRODUCT = "discoverp"


# ---------------------------------------------------------------------------
# Settings + cache fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Empty both registry layers + the connector-instance cache.

    The discover route resolves connector instances through the
    module-level ``_CONNECTOR_INSTANCE_CACHE``; clearing it keeps a
    fake connector from one test leaking into the next.
    """
    clear_registry()
    _CONNECTOR_INSTANCE_CACHE.clear()
    yield
    clear_registry()
    _CONNECTOR_INSTANCE_CACHE.clear()


# ---------------------------------------------------------------------------
# App + JWT helpers
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(targets_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _token(role: TenantRole, *, tenant_id: UUID = _TENANT_ID, sub: str = "op-1") -> tuple[Any, str]:
    key = _make_rsa_keypair(f"kid-{role.value}-{sub}")
    token = _mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(tenant_id))
    return key, token


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Fake connectors
# ---------------------------------------------------------------------------


class _BaseFakeConnector(Connector):
    product = _PRODUCT

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:
        raise NotImplementedError


class _CandidatesConnector(_BaseFakeConnector):
    async def list_candidates(self, seed_target: Any | None = None) -> list[CandidateHint]:
        return [
            CandidateHint(
                name="esxi-7",
                host="10.0.0.7",
                port=443,
                evidence={"source": "vcenter", "seed": getattr(seed_target, "name", None)},
                confidence="high",
            )
        ]


class _EmptyConnector(_BaseFakeConnector):
    async def list_candidates(self, seed_target: Any | None = None) -> list[CandidateHint]:
        return []


class _RaisingConnector(_BaseFakeConnector):
    async def list_candidates(self, seed_target: Any | None = None) -> list[CandidateHint]:
        raise RuntimeError("connector exploded")


async def _audit_rows_for_path(path: str) -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == path))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Route mounting + ordering
# ---------------------------------------------------------------------------


def test_discover_route_mounted_and_ordered_before_describe() -> None:
    """``/discover`` appears in OpenAPI and precedes ``/{name}`` on the router."""
    from meho_backplane.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/targets/discover" in paths
    assert "get" in paths["/api/v1/targets/discover"]

    # Declaration order on the router: the literal /discover route must
    # come before the parametrised /{name} route or FastAPI captures
    # "discover" as a target name.
    route_paths = [getattr(r, "path", "") for r in targets_router.routes]
    disc = route_paths.index("/api/v1/targets/discover")
    by_name = route_paths.index("/api/v1/targets/{name}")
    assert disc < by_name


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_discover_unauthenticated_returns_401(client: TestClient) -> None:
    assert client.get("/api/v1/targets/discover?product=x").status_code == 401


def test_discover_readonly_returns_403(client: TestClient) -> None:
    key, token = _token(TenantRole.READ_ONLY)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            f"/api/v1/targets/discover?product={_PRODUCT}",
            headers=_authed(token),
        )
    assert resp.status_code == 403


def test_discover_missing_product_returns_422(client: TestClient) -> None:
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get("/api/v1/targets/discover", headers=_authed(token))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Merge + skipped behaviour
# ---------------------------------------------------------------------------


def test_discover_merges_candidates_across_connectors(client: TestClient) -> None:
    """Candidates from every connector for the product are merged."""
    register_connector_v2(
        product=_PRODUCT, version="9.0", impl_id="discoverp-a", cls=_CandidatesConnector
    )
    register_connector_v2(
        product=_PRODUCT, version="8.0", impl_id="discoverp-b", cls=_EmptyConnector
    )
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            f"/api/v1/targets/discover?product={_PRODUCT}",
            headers=_authed(token),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [c["name"] for c in body["discovered"]] == ["esxi-7"]
    assert body["discovered"][0]["confidence"] == "high"
    # The empty connector lands in skipped with the clean-but-empty reason.
    skipped = {s["name"]: s["reason"] for s in body["skipped"]}
    assert skipped["discoverp-b"] == "no candidates"


def test_discover_records_raising_connector_and_continues(client: TestClient) -> None:
    """One connector raising does not abort the sweep — it is skipped."""
    register_connector_v2(
        product=_PRODUCT, version="9.0", impl_id="discoverp-good", cls=_CandidatesConnector
    )
    register_connector_v2(
        product=_PRODUCT, version="8.0", impl_id="discoverp-bad", cls=_RaisingConnector
    )
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            f"/api/v1/targets/discover?product={_PRODUCT}",
            headers=_authed(token),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The good connector still contributed despite the bad one raising.
    assert [c["name"] for c in body["discovered"]] == ["esxi-7"]
    skipped = {s["name"]: s["reason"] for s in body["skipped"]}
    assert "RuntimeError: connector exploded" in skipped["discoverp-bad"]


def test_discover_unknown_product_returns_empty(client: TestClient) -> None:
    """A product with no registered connectors returns empty lists, not 404."""
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/targets/discover?product=nonexistent",
            headers=_authed(token),
        )
    assert resp.status_code == 200
    assert resp.json() == {"discovered": [], "skipped": []}


# ---------------------------------------------------------------------------
# seed_target resolution
# ---------------------------------------------------------------------------


def test_discover_with_seed_target_resolves_and_forwards(client: TestClient) -> None:
    """A valid seed_target is resolved tenant-scoped and passed to list_candidates."""
    import asyncio

    asyncio.run(
        _insert_target(
            tenant_id=_TENANT_ID,
            name="seed-vc",
            product=_PRODUCT,
            host="10.0.0.1",
        )
    )
    register_connector_v2(
        product=_PRODUCT, version="9.0", impl_id="discoverp-a", cls=_CandidatesConnector
    )
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            f"/api/v1/targets/discover?product={_PRODUCT}&seed_target=seed-vc",
            headers=_authed(token),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # _CandidatesConnector echoes the seed name into evidence; proves
    # the resolved target (not the raw string) reached the hook.
    assert body["discovered"][0]["evidence"]["seed"] == "seed-vc"


def test_discover_unknown_seed_target_returns_404(client: TestClient) -> None:
    register_connector_v2(
        product=_PRODUCT, version="9.0", impl_id="discoverp-a", cls=_CandidatesConnector
    )
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            f"/api/v1/targets/discover?product={_PRODUCT}&seed_target=ghost",
            headers=_authed(token),
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Audit op_id binding
# ---------------------------------------------------------------------------


async def test_discover_binds_canonical_op_id(client: TestClient) -> None:
    """The audit row's payload carries op_id=targets.discover + op_class=read."""
    register_connector_v2(
        product=_PRODUCT, version="9.0", impl_id="discoverp-a", cls=_CandidatesConnector
    )
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            f"/api/v1/targets/discover?product={_PRODUCT}",
            headers=_authed(token),
        )
    assert resp.status_code == 200, resp.text

    rows = await _audit_rows_for_path("/api/v1/targets/discover")
    assert len(rows) == 1
    payload = rows[0].payload or {}
    assert payload.get("op_id") == "targets.discover"
    assert payload.get("op_class") == "read"
