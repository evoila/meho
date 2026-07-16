# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.topology`.

Coverage matrix (G9.1-T5 / Task #453 acceptance criteria):

* **Route mounting** — the four topology routes appear on
  :mod:`meho_backplane.main`'s app and in the OpenAPI document.
* **dependents / dependencies / path** — each route wraps its T4 verb,
  forwards the query params (depth / kind / kind_filter / max_hops /
  include_stale, #2538), and serialises the frozen result model over
  the wire.
* **path returns null** — an unreachable pair yields HTTP 200 with a
  ``null`` body (unreachability is a valid answer, not an error).
* **Ambiguous anchor → 409** — :class:`AmbiguousNodeError` from the
  query layer surfaces as 409 ``ambiguous_node`` with the candidate
  kinds, not an unhandled 500.
* **refresh** — wraps the T3 service; the resolved target is passed
  through; the :class:`RefreshResult` is returned verbatim.
* **refresh resolver failures → structured 4xx** — #2092:
  :class:`NoMatchingConnector` out of the service surfaces as 422
  ``no_matching_connector`` and :class:`AmbiguousConnectorResolution`
  as 409 ``ambiguous_connector`` with candidates, not a bare
  ``text/plain`` 500 from FastAPI's default handler.
* **refresh no-populator signal → wire** — #2093: the
  ``no_populator_for_product`` / ``populated_products`` fields the
  service stamps for a populator-less product pass through the
  ``response_model`` unfiltered (and serialise as nulls when a
  populator ran).
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
from meho_backplane.connectors import AmbiguousConnectorResolution
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, GraphEdge, GraphNode
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings
from meho_backplane.topology.annotate import AutoEdgeDeletionError, NodeRef
from meho_backplane.topology.query import AmbiguousNodeError
from meho_backplane.topology.refresh import RefreshResult
from meho_backplane.topology.resolvers import NodeNotFoundError
from meho_backplane.topology.schemas import (
    TopologyEdge,
    TopologyEdgeEndpoint,
    TopologyNode,
    TopologyPath,
)

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

    paths = app.openapi()["paths"]

    expected = {
        "/api/v1/topology/dependents/{name}",
        "/api/v1/topology/dependencies/{name}",
        "/api/v1/topology/path",
        "/api/v1/topology/refresh/{target_name}",
    }
    assert not (expected - paths.keys()), f"missing: {expected - paths.keys()}"

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
    # #2538 chain provenance is part of the serialised shape (null on
    # the factory-built rows; the substrate fills it from the CTE).
    assert body[0]["parent_node_id"] is None
    assert body[0]["via_edge_id"] is None
    # The query params are forwarded as keyword args to the T4 verb.
    _, kwargs = fake.call_args
    assert kwargs == {
        "kind": "host",
        "depth": 8,
        "kind_filter": "runs-on",
        "include_stale": True,
    }
    assert fake.call_args.args[1] == "host1"


def test_dependents_forwards_include_stale_false(client: TestClient) -> None:
    """``?include_stale=false`` (#2538) reaches the T4 verb as a bool."""
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(return_value=[_make_node("host1", "host", 0)])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.find_dependents", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/dependents/host1?include_stale=false",
            headers=_authed(token),
        )
    assert resp.status_code == 200, resp.text
    assert fake.call_args.kwargs["include_stale"] is False


def test_dependents_envelope_v2_returns_kind_nodes_shape(
    client: TestClient,
) -> None:
    """``?envelope=v2`` returns ``{kind, nodes}`` matching MCP (Finding E).

    G0.16-T6 Finding E (#1312). Per
    ``docs/codebase/api-shape-conventions.md`` §4, REST and MCP
    sister operations agree on response shape; the convention names
    migration as REST-toward-MCP. The opt-in shape mirrors the MCP
    ``query_topology`` tool's response (``{kind: "dependents",
    nodes: [...]}``); the default keeps the v0.8.0 bare list so no
    client breaks.
    """
    key, token = _token(TenantRole.OPERATOR)
    nodes = [_make_node("host1", "host", 0), _make_node("vm1", "vm", 1)]
    fake = AsyncMock(return_value=nodes)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.find_dependents", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/dependents/host1?envelope=v2",
            headers=_authed(token),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "dependents"
    assert [n["name"] for n in body["nodes"]] == ["host1", "vm1"]


def test_dependencies_envelope_v2_returns_kind_nodes_shape(
    client: TestClient,
) -> None:
    """``?envelope=v2`` on ``/dependencies`` returns ``{kind: "dependencies", nodes}``."""
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(return_value=[_make_node("app", "target", 0)])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.find_dependencies", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/dependencies/app?envelope=v2",
            headers=_authed(token),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "dependencies"
    assert [n["name"] for n in body["nodes"]] == ["app"]


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


def test_dependents_untracked_node_returns_404_node_untracked(
    client: TestClient,
) -> None:
    """G0.18-T4 (#1357, RDC #789 N2). Closure verb on an anchor with no
    matching ``graph_node`` returns 404 ``node_untracked`` rather than
    an empty 200 list. The distinct slug separates this case from the
    annotate-flow ``node_not_found`` so the CLI can render the
    closure-specific "register / refresh first" diagnostic.
    """
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(side_effect=NodeNotFoundError("vault-prod"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.find_dependents", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/dependents/vault-prod",
            headers=_authed(token),
        )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "node_untracked"
    assert detail["name"] == "vault-prod"
    assert "kind" not in detail  # No kind pin was supplied.


def test_dependencies_untracked_node_returns_404_node_untracked(
    client: TestClient,
) -> None:
    """The dependencies verb mirrors the dependents 404 contract.

    Same G0.18-T4 (#1357) translation: an untracked anchor surfaces as
    404 ``node_untracked``. Includes the ``kind`` echo when the caller
    supplied a ``kind=`` query pin so the diagnostic is
    self-contained.
    """
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(side_effect=NodeNotFoundError("vc-prod", kind="target"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.find_dependencies", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/dependencies/vc-prod?kind=target",
            headers=_authed(token),
        )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "node_untracked"
    assert detail["name"] == "vc-prod"
    assert detail["kind"] == "target"


def test_dependents_tracked_node_with_no_dependents_returns_one_element_root(
    client: TestClient,
) -> None:
    """A tracked-but-no-dependents anchor stays a 200 with ``[root]``.

    Companion to ``test_dependents_untracked_node_returns_404_...`` —
    the two together prove the G0.18-T4 (#1357) untracked-vs-empty
    distinction the false-negative RDC #789 N2 hinged on. The
    substrate's depth-0 anchor row is what makes this case
    structurally distinct from the untracked one (which raises before
    the CTE runs).
    """
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(return_value=[_make_node("ns-prod-foo", "namespace", 0)])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.find_dependents", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/dependents/ns-prod-foo",
            headers=_authed(token),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "ns-prod-foo"
    assert body[0]["depth"] == 0


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
    # #2538: default include_stale=true is forwarded explicitly.
    assert fake.call_args.kwargs["include_stale"] is True


def test_path_forwards_include_stale_false(client: TestClient) -> None:
    """``?include_stale=false`` (#2538) reaches ``find_path`` as a bool."""
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(return_value=None)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.find_path", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/path?from=a&to=b&include_stale=false",
            headers=_authed(token),
        )
    assert resp.status_code == 200, resp.text
    assert fake.call_args.kwargs["include_stale"] is False


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
    # #2093 — a populator-backed refresh serialises the no-populator
    # signal as explicit nulls, keeping the two no-op classes
    # discriminable on the wire.
    assert body["no_populator_for_product"] is None
    assert body["populated_products"] is None
    # The resolved target (not the raw name) is handed to the service.
    assert refresh.call_args.args[0].name == "vc-1"
    # resolve_target is tenant-scoped to the JWT's tenant_id.
    assert resolve.call_args.args[1] == _TENANT_ID
    assert resolve.call_args.args[2] == "vc-1"


def test_refresh_no_populator_signal_passes_through_to_wire(client: TestClient) -> None:
    """The #2093 coverage-gap signal survives response-model serialisation.

    The service stamps ``no_populator_for_product`` + ``populated_products``
    when the resolved connector inherits the base ``discover_topology``
    no-op; this test pins the route's ``response_model=RefreshResult``
    contract — the signal fields reach the JSON body unfiltered so a
    consumer can distinguish the all-zero coverage-gap no-op from a
    clean populator run with a single field check.
    """
    key, token = _token(TenantRole.OPERATOR)
    target_id = uuid.uuid4()

    class _FakeTarget:
        id = target_id
        name = "argocd-1"
        product = "argocd"

    result = RefreshResult(
        target_id=target_id,
        added_nodes=0,
        added_edges=0,
        updated_nodes=0,
        updated_edges=0,
        removed_nodes=0,
        removed_edges=0,
        duration_ms=1.5,
        no_populator_for_product="argocd",
        populated_products=("k8s",),
    )
    resolve = AsyncMock(return_value=_FakeTarget())
    refresh = AsyncMock(return_value=result)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.resolve_target", resolve),
        patch("meho_backplane.api.v1.topology.refresh_target_topology", refresh),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post("/api/v1/topology/refresh/argocd-1", headers=_authed(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["no_populator_for_product"] == "argocd"
    assert body["populated_products"] == ["k8s"]
    assert body["added_nodes"] == 0


def test_refresh_no_matching_connector_returns_structured_422(client: TestClient) -> None:
    """A product no connector supports maps to 422, not a bare 500 (#2092).

    Only ``resolve_target`` is patched — the *real*
    ``refresh_target_topology`` runs and raises
    :class:`NoMatchingConnector` out of the raising
    ``resolve_connector`` (the ``kubernetes`` legacy slug is not a
    registered product; ``KubernetesConnector`` self-registers as
    ``k8s``), proving the route's mapping catches the exception the
    service actually raises. Pre-#2092 this leaked through FastAPI's
    default handler as ``500 text/plain "Internal Server Error"``.
    """
    key, token = _token(TenantRole.OPERATOR)

    class _FakeTarget:
        id = uuid.uuid4()
        name = "rke2-legacy"
        product = "kubernetes"
        fingerprint = None
        preferred_impl_id = None

    resolve = AsyncMock(return_value=_FakeTarget())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.resolve_target", resolve),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post("/api/v1/topology/refresh/rke2-legacy", headers=_authed(token))
    # Explicitly not the pre-fix shape: bare 500 + text/plain body.
    assert resp.status_code != 500
    assert resp.text != "Internal Server Error"
    assert resp.status_code == 422, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    detail = resp.json()["detail"]
    assert detail["error"] == "no_matching_connector"
    assert detail["product"] == "kubernetes"
    assert "kubernetes" in detail["message"]


def test_refresh_ambiguous_connector_returns_409_with_candidates(client: TestClient) -> None:
    """A resolver tie maps to 409 ``ambiguous_connector`` + candidates (#2092)."""
    key, token = _token(TenantRole.OPERATOR)

    class _FakeTarget:
        id = uuid.uuid4()
        name = "vc-1"
        product = "vmware-rest"

    candidates = [
        ("vmware-rest", "8", "vmware-rest"),
        ("vmware-rest", "8", "vmware-rest-alt"),
    ]
    resolve = AsyncMock(return_value=_FakeTarget())
    refresh = AsyncMock(
        side_effect=AmbiguousConnectorResolution(
            "resolution ambiguous after tie-break ladder", candidates=candidates
        )
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.resolve_target", resolve),
        patch("meho_backplane.api.v1.topology.refresh_target_topology", refresh),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post("/api/v1/topology/refresh/vc-1", headers=_authed(token))
    assert resp.status_code == 409, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    detail = resp.json()["detail"]
    assert detail["error"] == "ambiguous_connector"
    assert detail["product"] == "vmware-rest"
    assert detail["candidates"] == [
        {"product": "vmware-rest", "version": "8", "impl_id": "vmware-rest"},
        {"product": "vmware-rest", "version": "8", "impl_id": "vmware-rest-alt"},
    ]


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


# ---------------------------------------------------------------------------
# G9.2-T5 (#597) — curated-edge routes
# ---------------------------------------------------------------------------


def _admin_token_value(*, tenant_id: UUID = _TENANT_ID, sub: str = "admin-1") -> tuple[Any, str]:
    """Convenience for an admin-role JWT pinned to the default tenant."""
    return _token(TenantRole.TENANT_ADMIN, tenant_id=tenant_id, sub=sub)


def _seeded_graph_edge(
    *,
    edge_id: uuid.UUID,
    from_node_id: uuid.UUID,
    to_node_id: uuid.UUID,
    kind: str = "depends-on",
    source: str = "curated",
    tenant_id: UUID = _TENANT_ID,
    note: str | None = "rebuilt-from-test",
) -> GraphEdge:
    """Construct an unattached ``GraphEdge`` row the ``annotate_edge`` mock
    can return so ``_edge_to_response`` has something to look up."""
    return GraphEdge(
        id=edge_id,
        tenant_id=tenant_id,
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        kind=kind,
        source=source,
        properties={"note": note} if note else {},
        discovered_by="admin-1",
    )


def _make_graph_node(
    *,
    name: str,
    kind: str = "vm",
    tenant_id: UUID = _TENANT_ID,
) -> GraphNode:
    return GraphNode(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        kind=kind,
        name=name,
        properties={},
        target_id=None,
        discovered_by="probe",
    )


async def _persist_node(node: GraphNode) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(node)


async def _persist_edge(edge: GraphEdge) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(edge)


# --- Route mounting --------------------------------------------------------


def test_curated_edge_routes_mounted_on_main_app() -> None:
    """The three curated-edge routes appear on the prod app + OpenAPI doc."""
    from meho_backplane.main import app

    paths = app.openapi()["paths"]

    expected = {
        "/api/v1/topology/edges",
        "/api/v1/topology/edges/{edge_id}",
    }
    assert not (expected - paths.keys()), f"missing: {expected - paths.keys()}"

    assert "post" in paths["/api/v1/topology/edges"]
    assert "get" in paths["/api/v1/topology/edges"]
    assert "delete" in paths["/api/v1/topology/edges/{edge_id}"]


# --- Unauthenticated → 401 -------------------------------------------------


def test_annotate_edge_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/topology/edges",
        json={"from": {"name": "a"}, "kind": "depends-on", "to": {"name": "b"}},
    )
    assert resp.status_code == 401


def test_unannotate_edge_unauthenticated_returns_401(client: TestClient) -> None:
    edge_id = uuid.uuid4()
    resp = client.delete(f"/api/v1/topology/edges/{edge_id}")
    assert resp.status_code == 401


def test_list_edges_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.get("/api/v1/topology/edges")
    assert resp.status_code == 401


# --- RBAC ------------------------------------------------------------------


def test_annotate_edge_operator_returns_403(client: TestClient) -> None:
    """``operator``-level principal is below ``tenant_admin``; POST must 403."""
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges",
            headers=_authed(token),
            json={"from": {"name": "a"}, "kind": "depends-on", "to": {"name": "b"}},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "insufficient_role"


def test_unannotate_edge_operator_returns_403(client: TestClient) -> None:
    """``operator``-level principal must not delete curated edges."""
    key, token = _token(TenantRole.OPERATOR)
    edge_id = uuid.uuid4()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.delete(
            f"/api/v1/topology/edges/{edge_id}",
            headers=_authed(token),
        )
    assert resp.status_code == 403


def test_list_edges_readonly_returns_403(client: TestClient) -> None:
    """``read_only`` is below ``operator``; GET must 403."""
    key, token = _token(TenantRole.READ_ONLY)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get("/api/v1/topology/edges", headers=_authed(token))
    assert resp.status_code == 403


def test_list_edges_operator_succeeds(client: TestClient) -> None:
    """``operator`` is sufficient for the read; the helper is called."""
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.list_edges", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get("/api/v1/topology/edges", headers=_authed(token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    fake.assert_awaited_once()


# --- POST /edges — happy path + 422 + error mapping ------------------------


async def test_annotate_edge_admin_round_trip(client: TestClient) -> None:
    """``tenant_admin`` POST hits :func:`annotate_edge`, returns the wire shape."""
    key, token = _admin_token_value()
    from_node = _make_graph_node(name="svc-A", kind="vm")
    to_node = _make_graph_node(name="db-1", kind="service")
    edge_id = uuid.uuid4()
    edge = _seeded_graph_edge(
        edge_id=edge_id,
        from_node_id=from_node.id,
        to_node_id=to_node.id,
    )
    # Persist the endpoint rows so ``session.get(GraphNode, ...)`` in
    # ``_edge_to_response`` returns the real rows from the same DB the
    # mock returned the (unattached) edge against.
    await _persist_node(from_node)
    await _persist_node(to_node)

    fake = AsyncMock(return_value=edge)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.annotate_edge", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges",
            headers=_authed(token),
            json={
                "from": {"name": "svc-A", "kind": "vm"},
                "kind": "depends-on",
                "to": {"name": "db-1", "kind": "service"},
                "note": "rebuilt-from-test",
                "evidence_url": "https://example/INVENTORY.md#A",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == str(edge_id)
    assert body["kind"] == "depends-on"
    assert body["source"] == "curated"
    assert body["from"]["name"] == "svc-A"
    assert body["from"]["kind"] == "vm"
    assert body["to"]["name"] == "db-1"

    # The service is called with NodeRef wrappers carrying the JSON
    # payload's name + kind hints; route owns the wire→dataclass coercion.
    args = fake.call_args
    assert args.args[2] == NodeRef(name="svc-A", kind="vm")
    assert args.args[3] == "depends-on"
    assert args.args[4] == NodeRef(name="db-1", kind="service")
    assert args.kwargs == {
        "note": "rebuilt-from-test",
        "evidence_url": "https://example/INVENTORY.md#A",
    }


def test_annotate_edge_malformed_kind_returns_422(client: TestClient) -> None:
    """The Pydantic slug pattern rejects a malformed ``kind`` before the service runs.

    T1 #2534: the vocabulary is open — rejection is by slug shape
    (pattern + length), not membership. Uppercase / punctuated and
    over-long kinds both 422 at the boundary.
    """
    key, token = _admin_token_value()
    fake = AsyncMock()
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.annotate_edge", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        for bad_kind in ("Made Up Kind!", "a" * 64):
            resp = client.post(
                "/api/v1/topology/edges",
                headers=_authed(token),
                json={
                    "from": {"name": "a"},
                    "kind": bad_kind,
                    "to": {"name": "b"},
                },
            )
            assert resp.status_code == 422, bad_kind
    fake.assert_not_awaited()


def test_annotate_edge_novel_kind_passes_boundary(client: TestClient) -> None:
    """A well-formed novel ``kind`` (`resolves-to`) reaches the service.

    The open-vocabulary counterpart of the malformed-kind 422: the
    boundary must not re-close the kind space, so a slug outside the
    well-known set flows through to ``annotate_edge`` verbatim.
    """
    key, token = _admin_token_value()
    fake = AsyncMock(side_effect=NodeNotFoundError("a", None))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.annotate_edge", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges",
            headers=_authed(token),
            json={
                "from": {"name": "a"},
                "kind": "resolves-to",
                "to": {"name": "b"},
            },
        )
    # 404 proves the request cleared Pydantic validation and invoked
    # the (mocked) service with the novel kind.
    assert resp.status_code == 404
    assert fake.call_args.args[3] == "resolves-to"


def test_annotate_edge_unknown_field_returns_422(client: TestClient) -> None:
    """``extra='forbid'`` rejects typo'd body keys at the boundary."""
    key, token = _admin_token_value()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges",
            headers=_authed(token),
            json={
                "from": {"name": "a"},
                "kind": "depends-on",
                "to": {"name": "b"},
                "evidnce_url": "https://typo",  # typo'd field name
            },
        )
    assert resp.status_code == 422


def test_annotate_edge_ambiguous_endpoint_returns_409(client: TestClient) -> None:
    """``AmbiguousNodeError`` from the service maps to 409 ``ambiguous_node``."""
    key, token = _admin_token_value()
    fake = AsyncMock(side_effect=AmbiguousNodeError("svc-A", ["vm", "service"]))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.annotate_edge", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges",
            headers=_authed(token),
            json={
                "from": {"name": "svc-A"},
                "kind": "depends-on",
                "to": {"name": "db-1"},
            },
        )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "ambiguous_node"
    assert detail["name"] == "svc-A"
    assert detail["kinds"] == ["service", "vm"]


def test_annotate_edge_missing_endpoint_returns_404(client: TestClient) -> None:
    """``NodeNotFoundError`` maps to 404 with the requested name + kind."""
    key, token = _admin_token_value()
    fake = AsyncMock(side_effect=NodeNotFoundError("ghost-host", "vm"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.annotate_edge", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges",
            headers=_authed(token),
            json={
                "from": {"name": "ghost-host", "kind": "vm"},
                "kind": "depends-on",
                "to": {"name": "db-1"},
            },
        )
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error"] == "node_not_found"
    assert detail["name"] == "ghost-host"
    assert detail["kind"] == "vm"


# --- DELETE /edges/{edge_id} — 204 + 409 + 404 -----------------------------


async def test_unannotate_edge_admin_round_trip(client: TestClient) -> None:
    """``tenant_admin`` DELETE invokes ``unannotate_edge`` by id, 204 on ok."""
    key, token = _admin_token_value()
    edge_id = uuid.uuid4()
    fake = AsyncMock(return_value=edge_id)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.unannotate_edge", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.delete(
            f"/api/v1/topology/edges/{edge_id}",
            headers=_authed(token),
        )
    assert resp.status_code == 204
    # The service is keyed on ``edge_id``; the route never passes the
    # triple form (the path-param surface is id-only).
    assert fake.call_args.kwargs == {"edge_id": edge_id}


def test_unannotate_edge_auto_source_returns_409(client: TestClient) -> None:
    """``AutoEdgeDeletionError`` maps to 409 with the auto edge's id + message."""
    key, token = _admin_token_value()
    edge_id = uuid.uuid4()
    fake = AsyncMock(side_effect=AutoEdgeDeletionError(edge_id))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.unannotate_edge", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.delete(
            f"/api/v1/topology/edges/{edge_id}",
            headers=_authed(token),
        )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "auto_edge_deletion"
    assert detail["edge_id"] == str(edge_id)
    assert "auto" in detail["message"].lower()


def test_unannotate_edge_missing_returns_404(client: TestClient) -> None:
    """``ValueError`` from the service collapses to 404 (cross-tenant id same)."""
    key, token = _admin_token_value()
    edge_id = uuid.uuid4()
    fake = AsyncMock(side_effect=ValueError("graph_edge not found in this tenant"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.unannotate_edge", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.delete(
            f"/api/v1/topology/edges/{edge_id}",
            headers=_authed(token),
        )
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error"] == "edge_not_found"
    assert detail["edge_id"] == str(edge_id)


def test_unannotate_edge_invalid_uuid_returns_422(client: TestClient) -> None:
    """A non-UUID path segment is a 422 — FastAPI rejects before the handler."""
    key, token = _admin_token_value()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.delete(
            "/api/v1/topology/edges/not-a-uuid",
            headers=_authed(token),
        )
    assert resp.status_code == 422


# --- GET /edges — filters + 409 + serialisation ----------------------------


def test_list_edges_forwards_filters(client: TestClient) -> None:
    """Every query param is forwarded as a keyword arg to ``list_edges``."""
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.list_edges", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/edges?kind=depends-on&source=curated"
            "&from=svc-A&to=db-1&conflicts=true&limit=50&offset=25",
            headers=_authed(token),
        )
    assert resp.status_code == 200, resp.text
    kwargs = fake.call_args.kwargs
    assert kwargs == {
        "kind": "depends-on",
        "source": "curated",
        "from_ref": "svc-A",
        "to_ref": "db-1",
        "conflicts_only": True,
        "limit": 50,
        "offset": 25,
    }
    # The tenant_id positional arg is taken from the JWT, not a query
    # param — non-overrideable.
    assert fake.call_args.args[1] == _TENANT_ID


def test_list_edges_default_filters(client: TestClient) -> None:
    """Defaults: ``limit=200``, ``offset=0``, ``conflicts_only=False``."""
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.list_edges", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get("/api/v1/topology/edges", headers=_authed(token))
    assert resp.status_code == 200, resp.text
    kwargs = fake.call_args.kwargs
    assert kwargs["limit"] == 200
    assert kwargs["offset"] == 0
    assert kwargs["conflicts_only"] is False
    assert kwargs["kind"] is None
    assert kwargs["source"] is None


def test_list_edges_invalid_kind_returns_422(client: TestClient) -> None:
    """Pydantic rejects a malformed ``kind`` query param (slug pattern).

    T1 #2534: any well-formed slug is a legal filter (the vocabulary
    is open), so only a shape-violating value 422s here.
    """
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/edges?kind=Not%20A%20Kind!",
            headers=_authed(token),
        )
    assert resp.status_code == 422


def test_list_edges_invalid_source_returns_422(client: TestClient) -> None:
    """``source`` pattern restricts to ``auto`` / ``curated``."""
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/edges?source=banned",
            headers=_authed(token),
        )
    assert resp.status_code == 422


def test_list_edges_limit_above_ceiling_returns_422(client: TestClient) -> None:
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/edges?limit=10000",
            headers=_authed(token),
        )
    assert resp.status_code == 422


def test_list_edges_serialises_wire_shape(client: TestClient) -> None:
    """The response body carries ``from`` / ``to`` endpoint objects + props."""
    key, token = _token(TenantRole.OPERATOR)
    edge_id = uuid.uuid4()
    from_id, to_id = uuid.uuid4(), uuid.uuid4()
    edge = TopologyEdge(
        id=edge_id,
        from_endpoint=TopologyEdgeEndpoint(id=from_id, kind="vm", name="svc-A"),
        to_endpoint=TopologyEdgeEndpoint(id=to_id, kind="service", name="db-1"),
        kind="depends-on",
        source="curated",
        properties={"note": "n", "conflicts_with": [str(uuid.uuid4())]},
        last_seen=None,
    )
    fake = AsyncMock(return_value=[edge])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.list_edges", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get("/api/v1/topology/edges", headers=_authed(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["id"] == str(edge_id)
    assert row["from"]["name"] == "svc-A"
    assert row["to"]["name"] == "db-1"
    assert row["kind"] == "depends-on"
    assert row["source"] == "curated"
    # ``properties`` round-trips as a plain JSON dict; frozen at the
    # Pydantic layer is irrelevant on the wire.
    assert row["properties"]["note"] == "n"
    assert isinstance(row["properties"]["conflicts_with"], list)


def test_list_edges_ambiguous_from_returns_409(client: TestClient) -> None:
    """An ambiguous ``from`` query param surfaces as 409 ``ambiguous_node``."""
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(side_effect=AmbiguousNodeError("svc-A", ["vm", "service"]))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.list_edges", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get(
            "/api/v1/topology/edges?from=svc-A",
            headers=_authed(token),
        )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "ambiguous_node"
    assert detail["name"] == "svc-A"


# --- Audit op-id binding (write class) -------------------------------------


async def test_annotate_route_binds_write_op_id(client: TestClient) -> None:
    """``POST /edges`` writes an ``audit_log`` row with op_id=topology.annotate +
    op_class=write."""
    key, token = _admin_token_value()
    from_node = _make_graph_node(name="svc-A", kind="vm")
    to_node = _make_graph_node(name="db-1", kind="service")
    await _persist_node(from_node)
    await _persist_node(to_node)
    edge = _seeded_graph_edge(
        edge_id=uuid.uuid4(),
        from_node_id=from_node.id,
        to_node_id=to_node.id,
    )
    fake = AsyncMock(return_value=edge)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.annotate_edge", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges",
            headers=_authed(token),
            json={
                "from": {"name": "svc-A", "kind": "vm"},
                "kind": "depends-on",
                "to": {"name": "db-1", "kind": "service"},
            },
        )
    assert resp.status_code == 201, resp.text

    rows = await _audit_rows_for_path("/api/v1/topology/edges")
    # Only the POST has been issued against this path in this test;
    # AuditMiddleware writes one row per request.
    assert len(rows) == 1
    payload = rows[0].payload or {}
    assert payload.get("op_id") == "topology.annotate"
    assert payload.get("op_class") == "write"


async def test_unannotate_route_binds_write_op_id(client: TestClient) -> None:
    """``DELETE /edges/{id}`` audit row carries op_id=topology.unannotate +
    op_class=write."""
    key, token = _admin_token_value()
    edge_id = uuid.uuid4()
    fake = AsyncMock(return_value=edge_id)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.unannotate_edge", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.delete(
            f"/api/v1/topology/edges/{edge_id}",
            headers=_authed(token),
        )
    assert resp.status_code == 204

    rows = await _audit_rows_for_path(f"/api/v1/topology/edges/{edge_id}")
    assert len(rows) == 1
    payload = rows[0].payload or {}
    assert payload.get("op_id") == "topology.unannotate"
    assert payload.get("op_class") == "write"


async def test_list_edges_route_binds_read_op_id(client: TestClient) -> None:
    """``GET /edges`` audit row carries op_id=topology.list_edges +
    op_class=read."""
    key, token = _token(TenantRole.OPERATOR)
    fake = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.list_edges", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.get("/api/v1/topology/edges", headers=_authed(token))
    assert resp.status_code == 200

    rows = await _audit_rows_for_path("/api/v1/topology/edges")
    assert len(rows) == 1
    payload = rows[0].payload or {}
    assert payload.get("op_id") == "topology.list_edges"
    assert payload.get("op_class") == "read"


# ---------------------------------------------------------------------------
# G9.2-T8 (#600) — bulk import
# ---------------------------------------------------------------------------


def test_bulk_import_route_mounted_on_main_app() -> None:
    """``POST /api/v1/topology/edges/bulk`` appears in the OpenAPI document."""
    from meho_backplane.main import app as main_app

    paths = main_app.openapi()["paths"]
    assert "/api/v1/topology/edges/bulk" in paths
    assert "post" in paths["/api/v1/topology/edges/bulk"]


def test_bulk_import_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/topology/edges/bulk",
        json={"edges": [{"from": {"name": "a"}, "kind": "depends-on", "to": {"name": "b"}}]},
    )
    assert resp.status_code == 401


def test_bulk_import_operator_returns_403(client: TestClient) -> None:
    """``operator`` is below ``tenant_admin``; bulk must 403."""
    key, token = _token(TenantRole.OPERATOR)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges/bulk",
            headers=_authed(token),
            json={
                "edges": [
                    {"from": {"name": "a"}, "kind": "depends-on", "to": {"name": "b"}},
                ]
            },
        )
    assert resp.status_code == 403


def test_bulk_import_empty_edges_returns_422(client: TestClient) -> None:
    """``edges`` is non-empty (``min_length=1``)."""
    key, token = _admin_token_value()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges/bulk",
            headers=_authed(token),
            json={"edges": []},
        )
    assert resp.status_code == 422


def test_bulk_import_too_many_edges_returns_422(client: TestClient) -> None:
    """The route caps the batch size at 1000 rows."""
    key, token = _admin_token_value()
    edges = [
        {"from": {"name": f"a-{i}"}, "kind": "depends-on", "to": {"name": f"b-{i}"}}
        for i in range(1001)
    ]
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges/bulk",
            headers=_authed(token),
            json={"edges": edges},
        )
    assert resp.status_code == 422


def test_bulk_import_unknown_kind_returns_422(client: TestClient) -> None:
    """A typo'd ``kind`` is caught at the Pydantic boundary."""
    key, token = _admin_token_value()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges/bulk",
            headers=_authed(token),
            json={
                "edges": [
                    {"from": {"name": "a"}, "kind": "made-up", "to": {"name": "b"}},
                ]
            },
        )
    assert resp.status_code == 422


def test_bulk_import_unknown_field_returns_422(client: TestClient) -> None:
    """``extra='forbid'`` rejects typo'd field names at the boundary."""
    key, token = _admin_token_value()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges/bulk",
            headers=_authed(token),
            json={
                "edges": [
                    {
                        "from": {"name": "a"},
                        "kind": "depends-on",
                        "to": {"name": "b"},
                        "evidnce_url": "https://typo",
                    }
                ]
            },
        )
    assert resp.status_code == 422


def test_bulk_import_admin_round_trip(client: TestClient) -> None:
    """``tenant_admin`` POST calls ``bulk_import_edges`` and renders the response.

    The service helper is stubbed out — the surface under test is the
    route's coercion of the JSON body into :class:`BulkImportRow`
    instances plus the response model shape. The integration suite
    (T9-style) exercises the real service against a live DB; here we
    only verify that the route's wire-shape contract holds.
    """
    from meho_backplane.topology.bulk_import import BulkEdgeResult, BulkImportResult

    key, token = _admin_token_value()
    fake_result = BulkImportResult(
        dry_run=False,
        created=1,
        updated=1,
        conflicts=0,
        rows=[
            BulkEdgeResult(
                index=0,
                action="create",
                edge_id=str(uuid.uuid4()),
                from_name="sa-a",
                from_kind="principal",
                to_name="vr-a",
                to_kind="vault-role",
                kind="authenticates-via",
                superseded=[],
                conflicts=[],
            ),
            BulkEdgeResult(
                index=1,
                action="update",
                edge_id=str(uuid.uuid4()),
                from_name="svc-orders",
                from_kind="service",
                to_name="db-orders",
                to_kind="service",
                kind="depends-on",
                superseded=[],
                conflicts=[],
            ),
        ],
    )
    fake = AsyncMock(return_value=fake_result)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.bulk_import_edges", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges/bulk",
            headers=_authed(token),
            json={
                "edges": [
                    {
                        "from": {"name": "sa-a", "kind": "principal"},
                        "kind": "authenticates-via",
                        "to": {"name": "vr-a", "kind": "vault-role"},
                    },
                    {
                        "from": {"name": "svc-orders", "kind": "service"},
                        "kind": "depends-on",
                        "to": {"name": "db-orders", "kind": "service"},
                    },
                ]
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is False
    assert body["created"] == 1
    assert body["updated"] == 1
    assert len(body["rows"]) == 2
    assert body["rows"][0]["action"] == "create"
    fake.assert_awaited_once()


def test_bulk_import_dry_run_forwards_flag(client: TestClient) -> None:
    """``dry_run=true`` is forwarded to the service kwarg."""
    from meho_backplane.topology.bulk_import import BulkImportResult

    key, token = _admin_token_value()
    fake_result = BulkImportResult(dry_run=True, created=0, updated=0, conflicts=0, rows=[])
    fake = AsyncMock(return_value=fake_result)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.bulk_import_edges", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges/bulk",
            headers=_authed(token),
            json={
                "edges": [
                    {"from": {"name": "a"}, "kind": "depends-on", "to": {"name": "b"}},
                ],
                "dry_run": True,
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["dry_run"] is True
    assert fake.call_args.kwargs == {"dry_run": True}


def test_bulk_import_validation_error_returns_422(client: TestClient) -> None:
    """``BulkImportValidationError`` maps to 422 with the per-row error list."""
    from meho_backplane.topology.bulk_import import (
        BulkImportRowError,
        BulkImportValidationError,
    )

    key, token = _admin_token_value()
    fake = AsyncMock(
        side_effect=BulkImportValidationError(
            [
                BulkImportRowError(
                    index=1,
                    error="node_not_found",
                    message="node 'ghost' not found",
                    name="ghost",
                    kind="vm",
                ),
            ]
        )
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.topology.bulk_import_edges", fake),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        resp = client.post(
            "/api/v1/topology/edges/bulk",
            headers=_authed(token),
            json={
                "edges": [
                    {"from": {"name": "a"}, "kind": "depends-on", "to": {"name": "b"}},
                    {"from": {"name": "ghost"}, "kind": "depends-on", "to": {"name": "z"}},
                ]
            },
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_bulk"
    assert len(detail["errors"]) == 1
    assert detail["errors"][0]["index"] == 1
    assert detail["errors"][0]["error"] == "node_not_found"
