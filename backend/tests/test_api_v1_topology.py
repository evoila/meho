# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.topology`.

Coverage matrix (G9.1-T5 / Task #453 acceptance criteria):

* **Route mounting** — the four topology routes appear on
  :mod:`meho_backplane.main`'s app and in the OpenAPI document.
* **dependents / dependencies / path** — each route wraps its T4 verb,
  forwards the query params (depth / kind / kind_filter / max_hops),
  and serialises the frozen result model over the wire.
* **path returns null** — an unreachable pair yields HTTP 200 with a
  ``null`` body (unreachability is a valid answer, not an error).
* **Ambiguous anchor → 409** — :class:`AmbiguousNodeError` from the
  query layer surfaces as 409 ``ambiguous_node`` with the candidate
  kinds, not an unhandled 500.
* **refresh** — wraps the T3 service; the resolved target is passed
  through; the :class:`RefreshResult` is returned verbatim.
* **RBAC** — every route requires ``operator`` minimum; ``read_only``
  gets 403.
* **Unauthenticated** — every route returns 401 without a token.
* **Audit op_id binding** — each route binds the canonical
  ``audit_op_id`` + ``audit_op_class="read"`` so the audit row + the
  broadcast classifier carry the right identity.

The T4 query verbs use PostgreSQL's ``WITH RECURSIVE ... CYCLE`` and
the T3 refresh service resolves a connector + writes the graph; both
are patched at the route's import site here. End-to-end coverage
against a real pgvector cluster lives in
``tests/integration/test_topology_api.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.topology import router as topology_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings
from meho_backplane.topology.query import AmbiguousNodeError
from meho_backplane.topology.refresh import RefreshResult
from meho_backplane.topology.schemas import TopologyNode, TopologyPath

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# Settings + JWKS cache fixtures
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


# ---------------------------------------------------------------------------
# App + JWT helpers
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(topology_router)
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


def _make_node(name: str, kind: str = "host", depth: int = 0) -> TopologyNode:
    return TopologyNode(
        id=uuid.uuid4(),
        kind=kind,
        name=name,
        properties={"seeded": name},
        depth=depth,
        via_edge_kind=None if depth == 0 else "runs-on",
    )


async def _audit_rows_for_path(path: str) -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == path))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Route mounting
# ---------------------------------------------------------------------------


def test_all_topology_routes_mounted_on_main_app() -> None:
    """The four topology routes appear on the prod app + OpenAPI doc."""
    from meho_backplane.main import app

    expected = {
        "/api/v1/topology/dependents/{name}",
        "/api/v1/topology/dependencies/{name}",
        "/api/v1/topology/path",
        "/api/v1/topology/refresh/{target_name}",
    }
    actual = {getattr(r, "path", None) for r in app.routes}
    assert not (expected - actual), f"missing: {expected - actual}"

    paths = app.openapi()["paths"]
    assert "get" in paths["/api/v1/topology/dependents/{name}"]
    assert "get" in paths["/api/v1/topology/dependencies/{name}"]
    assert "get" in paths["/api/v1/topology/path"]
    assert "post" in paths["/api/v1/topology/refresh/{target_name}"]


# ---------------------------------------------------------------------------
# Unauthenticated → 401
# ---------------------------------------------------------------------------


def test_dependents_unauthenticated_returns_401(client: TestClient) -> None:
    assert client.get("/api/v1/topology/dependents/app").status_code == 401


def test_dependencies_unauthenticated_returns_401(client: TestClient) -> None:
    assert client.get("/api/v1/topology/dependencies/app").status_code == 401


def test_path_unauthenticated_returns_401(client: TestClient) -> None:
    assert client.get("/api/v1/topology/path?from=a&to=b").status_code == 401


def test_refresh_unauthenticated_returns_401(client: TestClient) -> None:
    assert client.post("/api/v1/topology/refresh/app").status_code == 401


# ---------------------------------------------------------------------------
# RBAC — read_only gets 403
# ---------------------------------------------------------------------------


def test_dependents_readonly_returns_403(client: TestClient) -> None:
    key, token = _token(TenantRole.READ_ONLY)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get("/api/v1/topology/dependents/app", headers=_authed(token))
    assert resp.status_code == 403


def test_refresh_readonly_returns_403(client: TestClient) -> None:
    key, token = _token(TenantRole.READ_ONLY)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post("/api/v1/topology/refresh/app", headers=_authed(token))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# dependents / dependencies — wrap the T4 verb + serialise
# ---------------------------------------------------------------------------


def test_dependents_wraps_find_dependents_and_forwards_params(client: TestClient) -> None:
    key, token = _token(TenantRole.OPERATOR)
    nodes = [_make_node("host1", "host", 0), _make_node("vm1", "vm", 1)]
    fake = AsyncMock(return_value=nodes)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.find_dependents", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/dependents/host1?depth=8&kind=host&kind_filter=runs-on",
            headers=_authed(token),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [n["name"] for n in body] == ["host1", "vm1"]
    assert body[1]["via_edge_kind"] == "runs-on"
    # The query params are forwarded as keyword args to the T4 verb.
    _, kwargs = fake.call_args
    assert kwargs == {"kind": "host", "depth": 8, "kind_filter": "runs-on"}
    assert fake.call_args.args[1] == "host1"


def test_dependencies_wraps_find_dependencies(client: TestClient) -> None:
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(return_value=[_make_node("app", "target", 0)])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.find_dependencies", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get("/api/v1/topology/dependencies/app", headers=_authed(token))
    assert resp.status_code == 200, resp.text
    assert [n["name"] for n in resp.json()] == ["app"]
    # Default depth (16) is forwarded when the query param is omitted.
    assert fake.call_args.kwargs["depth"] == 16


def test_dependents_depth_above_ceiling_returns_422(client: TestClient) -> None:
    """A hostile ``depth`` over the HTTP ceiling is rejected at the boundary."""
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/dependents/app?depth=999",
            headers=_authed(token),
        )
    assert resp.status_code == 422


def test_dependents_ambiguous_node_returns_409(client: TestClient) -> None:
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(side_effect=AmbiguousNodeError("app", ["target", "vm"]))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.find_dependents", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get("/api/v1/topology/dependents/app", headers=_authed(token))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "ambiguous_node"
    assert detail["name"] == "app"
    assert detail["kinds"] == ["target", "vm"]


# ---------------------------------------------------------------------------
# path — wrap find_path; null when unreachable
# ---------------------------------------------------------------------------


def test_path_returns_serialised_path(client: TestClient) -> None:
    key, token = _token(TenantRole.OPERATOR)
    tp = TopologyPath(
        nodes=(_make_node("a", "host", 0), _make_node("b", "vm", 1)),
        total_hops=1,
    )
    fake = AsyncMock(return_value=tp)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.find_path", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/path?from=a&to=b&max_hops=5",
            headers=_authed(token),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_hops"] == 1
    assert [n["name"] for n in body["nodes"]] == ["a", "b"]
    # `from` / `to` query params are forwarded positionally.
    assert fake.call_args.args[1:] == ("a", "b")
    assert fake.call_args.kwargs["max_hops"] == 5


def test_path_unreachable_returns_200_null(client: TestClient) -> None:
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(return_value=None)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.find_path", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get("/api/v1/topology/path?from=a&to=z", headers=_authed(token))
    assert resp.status_code == 200
    assert resp.json() is None


def test_path_missing_required_query_param_returns_422(client: TestClient) -> None:
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get("/api/v1/topology/path?from=a", headers=_authed(token))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# refresh — wrap the T3 service
# ---------------------------------------------------------------------------


def test_refresh_wraps_service_and_returns_result(client: TestClient) -> None:
    key, token = _token(TenantRole.OPERATOR)
    target_id = uuid.uuid4()

    class _FakeTarget:
        id = target_id
        name = "vc-1"
        product = "vmware-rest"

    result = RefreshResult(
        target_id=target_id,
        added_nodes=3,
        added_edges=2,
        updated_nodes=1,
        updated_edges=0,
        removed_nodes=0,
        removed_edges=0,
        duration_ms=12.5,
    )
    resolve = AsyncMock(return_value=_FakeTarget())
    refresh = AsyncMock(return_value=result)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.resolve_target", resolve),
        patch("meho_backplane.api.v1.topology.refresh_target_topology", refresh),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post("/api/v1/topology/refresh/vc-1", headers=_authed(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target_id"] == str(target_id)
    assert body["added_nodes"] == 3
    # The resolved target (not the raw name) is handed to the service.
    assert refresh.call_args.args[0].name == "vc-1"
    # resolve_target is tenant-scoped to the JWT's tenant_id.
    assert resolve.call_args.args[1] == _TENANT_ID
    assert resolve.call_args.args[2] == "vc-1"


# ---------------------------------------------------------------------------
# Audit op_id binding — read class on every route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "http_path", "target_module_symbol", "op_id"),
    [
        (
            "/api/v1/topology/dependents/app",
            "/api/v1/topology/dependents/app",
            "find_dependents",
            "topology.dependents",
        ),
        (
            "/api/v1/topology/dependencies/app",
            "/api/v1/topology/dependencies/app",
            "find_dependencies",
            "topology.dependencies",
        ),
    ],
)
async def test_read_route_binds_canonical_op_id(
    client: TestClient,
    url: str,
    http_path: str,
    target_module_symbol: str,
    op_id: str,
) -> None:
    """The audit row's payload carries the canonical op_id + op_class=read.

    ``audit_log.path`` is the raw HTTP path (the chassis convention);
    the canonical op id the route binds via ``audit_op_id`` lands in
    ``payload["op_id"]`` — mirrors the kb-route audit assertions.
    """
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(return_value=[_make_node("app", "target", 0)])
    with (
        respx.mock as mock_router,
        patch(f"meho_backplane.api.v1.topology.{target_module_symbol}", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(url, headers=_authed(token))
    assert resp.status_code == 200, resp.text

    rows = await _audit_rows_for_path(http_path)
    assert len(rows) == 1
    payload = rows[0].payload or {}
    assert payload.get("op_id") == op_id
    assert payload.get("op_class") == "read"
