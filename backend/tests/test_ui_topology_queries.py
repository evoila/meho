# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Topology UI subgraph + path query overlays.

Initiative #342 (G10.5 Topology UI), Task #882 (G10.5-T3). Acceptance
criteria from the issue body:

* ``?from=<name>&depth=N`` returns the correct dependents subgraph
  (matches the G9.1 ``find_dependents`` API) and Cytoscape re-renders
  it.
* ``?from=<name>&direction=dependencies&depth=N`` returns the
  correct dependencies subgraph (matches G9.1
  ``find_dependencies``).
* ``?from=A&to=B`` returns + highlights the path (matches
  ``meho topology path``); edge labels shown on the highlighted
  path.
* The "show dependents" link from the node drawer (#880) drives the
  ``?from=`` subgraph view.
* Polling-refresh (``hx-trigger="every 30s"``) re-pulls the current
  view's graph JSON without losing pan/zoom.
* Cross-tenant isolation holds for all query overlays.
* Ambiguous bare-name -> 409 with a kind-disambiguation hint.
* Unknown name -> 404.

Suite shape mirrors
:mod:`backend.tests.test_ui_topology_graph`: the same minimal
FastAPI app, the same per-tenant seed helpers, the same data-island
extraction (the overlays emit the same Cytoscape elements
structure as the full graph view).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import GraphEdge, GraphNode, Tenant
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
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


_BACKPLANE_URL = "https://meho.test"

# Stable tenant ids -- same shape as the T1 / T2 suites. Cross-tenant
# isolation assertion uses them directly.
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test."""
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


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the topology query tests."""
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(static_root_dir()), check_dir=False),
        name="ui_static",
    )
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    return app


def _seed_tenant_row(tenant_id: uuid.UUID, slug: str) -> None:
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_node(
    *,
    tenant_id: uuid.UUID,
    kind: str,
    name: str,
    target_id: uuid.UUID | None = None,
    discovered_by: str = "test",
) -> uuid.UUID:
    node_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                GraphNode(
                    id=node_id,
                    tenant_id=tenant_id,
                    kind=kind,
                    name=name,
                    target_id=target_id,
                    properties={},
                    discovered_by=discovered_by,
                    first_seen=datetime.now(UTC),
                    last_seen=datetime.now(UTC),
                ),
            )

    asyncio.run(_do())
    return node_id


def _seed_edge(
    *,
    tenant_id: uuid.UUID,
    from_node_id: uuid.UUID,
    to_node_id: uuid.UUID,
    kind: str = "runs-on",
    source: str = "auto",
) -> uuid.UUID:
    edge_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                GraphEdge(
                    id=edge_id,
                    tenant_id=tenant_id,
                    from_node_id=from_node_id,
                    to_node_id=to_node_id,
                    kind=kind,
                    source=source,
                    properties={},
                    discovered_by="test",
                    first_seen=datetime.now(UTC),
                    last_seen=datetime.now(UTC),
                ),
            )

    asyncio.run(_do())
    return edge_id


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = "op-42",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token="access-token-plaintext",
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_do())


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _extract_data_island(body: str, island_id: str) -> object:
    """Extract + JSON-parse a ``<script type="application/json">`` data island."""
    pattern = re.compile(
        rf'<script type="application/json" id="{re.escape(island_id)}">(.*?)</script>',
        re.DOTALL,
    )
    match = pattern.search(body)
    assert match is not None, f"data island {island_id!r} not found in body"
    return json.loads(match.group(1))


def _seed_three_tier_graph(tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Seed a small representative graph for the overlay tests.

    Shape::

        network-1
            ^
            | belongs-to
            |
        host-1
            ^
            | runs-on
            |
        vm-1
            ^
            | runs-on
            |
        target-app

    Edge-direction reads "A depends on B" for ``A --kind--> B``.
    Returns the seeded ids by name for the tests to reference.
    """
    target = _seed_node(tenant_id=tenant_id, kind="target", name="target-app")
    vm = _seed_node(tenant_id=tenant_id, kind="vm", name="vm-1")
    host = _seed_node(tenant_id=tenant_id, kind="host", name="host-1")
    network = _seed_node(tenant_id=tenant_id, kind="network", name="network-1")
    _seed_edge(tenant_id=tenant_id, from_node_id=target, to_node_id=vm, kind="runs-on")
    _seed_edge(tenant_id=tenant_id, from_node_id=vm, to_node_id=host, kind="runs-on")
    _seed_edge(tenant_id=tenant_id, from_node_id=host, to_node_id=network, kind="belongs-to")
    return {
        "target-app": target,
        "vm-1": vm,
        "host-1": host,
        "network-1": network,
    }


# ---------------------------------------------------------------------------
# Dependents subgraph
# ---------------------------------------------------------------------------


def test_dependents_subgraph_returns_reverse_closure() -> None:
    """``?from=host-1`` returns host-1 + everything that depends on it."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    ids = _seed_three_tier_graph(_TENANT_A)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=host-1")
    assert response.status_code == 200, response.text
    body = response.text

    elements = _extract_data_island(body, "topology-graph-data")
    assert isinstance(elements, list)
    nodes = [e for e in elements if isinstance(e, dict) and e.get("group") == "nodes"]
    node_names = {n["data"]["name"] for n in nodes}
    # host-1 + things depending on it: vm-1 (direct), target-app (transitive).
    # network-1 is NOT here (host-1 depends on network-1, not the reverse).
    assert node_names == {"host-1", "vm-1", "target-app"}

    # Edges connect the nodes within the subgraph.
    edges = [e for e in elements if isinstance(e, dict) and e.get("group") == "edges"]
    edge_pairs = {(e["data"]["source"], e["data"]["target"]) for e in edges}
    assert (str(ids["target-app"]), str(ids["vm-1"])) in edge_pairs
    assert (str(ids["vm-1"]), str(ids["host-1"])) in edge_pairs

    # Overlay status pill surfaces the mode + root + depth.
    assert 'data-test="overlay-status"' in body
    assert "host-1" in body
    assert "Dependents" in body


def test_dependents_subgraph_respects_depth_bound() -> None:
    """``?from=host-1&depth=1`` stops at immediate dependents."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_three_tier_graph(_TENANT_A)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=host-1&depth=1")
    assert response.status_code == 200, response.text

    elements = _extract_data_island(response.text, "topology-graph-data")
    assert isinstance(elements, list)
    nodes = [e for e in elements if isinstance(e, dict) and e.get("group") == "nodes"]
    names = {n["data"]["name"] for n in nodes}
    # At depth=1 we visit host-1 (root) and vm-1 (direct dependent) -- target-app
    # is two hops away and excluded.
    assert names == {"host-1", "vm-1"}


def test_root_node_is_marked_in_subgraph() -> None:
    """The anchor node carries the ``root`` class so the stylesheet can highlight it."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_three_tier_graph(_TENANT_A)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=host-1")

    elements = _extract_data_island(response.text, "topology-graph-data")
    assert isinstance(elements, list)
    root_elements = [
        e
        for e in elements
        if isinstance(e, dict) and e.get("group") == "nodes" and "root" in str(e.get("classes", ""))
    ]
    assert len(root_elements) == 1
    assert root_elements[0]["data"]["name"] == "host-1"


# ---------------------------------------------------------------------------
# Dependencies subgraph
# ---------------------------------------------------------------------------


def test_dependencies_subgraph_returns_forward_closure() -> None:
    """``?from=vm-1&direction=dependencies`` walks forward from vm-1."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_three_tier_graph(_TENANT_A)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=vm-1&direction=dependencies")
    assert response.status_code == 200, response.text

    elements = _extract_data_island(response.text, "topology-graph-data")
    assert isinstance(elements, list)
    nodes = [e for e in elements if isinstance(e, dict) and e.get("group") == "nodes"]
    node_names = {n["data"]["name"] for n in nodes}
    # vm-1 depends on host-1; host-1 depends on network-1.
    assert node_names == {"vm-1", "host-1", "network-1"}
    # target-app is NOT here (it depends on vm-1, not the reverse).
    assert "target-app" not in node_names

    # Overlay status pill surfaces the right mode.
    assert "Dependencies" in response.text


# ---------------------------------------------------------------------------
# Path overlay
# ---------------------------------------------------------------------------


def test_path_overlay_returns_shortest_path_with_highlighted_edges() -> None:
    """``?from=target-app&to=network-1`` returns the shortest path + highlight."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_three_tier_graph(_TENANT_A)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=target-app&to=network-1")
    assert response.status_code == 200, response.text

    body = response.text
    elements = _extract_data_island(body, "topology-graph-data")
    assert isinstance(elements, list)
    nodes = [e for e in elements if isinstance(e, dict) and e.get("group") == "nodes"]
    edges = [e for e in elements if isinstance(e, dict) and e.get("group") == "edges"]
    # The path target-app -> vm-1 -> host-1 -> network-1 is 3 hops.
    node_names = [n["data"]["name"] for n in nodes]
    assert sorted(node_names) == sorted(["target-app", "vm-1", "host-1", "network-1"])
    # All path edges carry the ``highlight`` class.
    highlighted_edges = [e for e in edges if "highlight" in str(e.get("classes", ""))]
    assert len(highlighted_edges) == 3

    # The path-nodes data island carries the ordered path id list.
    path_nodes_island = _extract_data_island(body, "topology-graph-path-nodes")
    assert isinstance(path_nodes_island, list)
    assert len(path_nodes_island) == 4

    # Overlay status pill surfaces the path mode + hop count.
    assert "Path from" in body
    assert "3 hop" in body


def test_path_overlay_unreachable_returns_no_path_state() -> None:
    """An unreachable target renders the no-path-found message."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    # Two disconnected nodes -- there's no path.
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-island-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-island-b")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=vm-island-a&to=vm-island-b")
    assert response.status_code == 200, response.text
    assert "no path found" in response.text


# ---------------------------------------------------------------------------
# 30s polling-refresh endpoint shape (HTMX fragment)
# ---------------------------------------------------------------------------


def test_graph_full_page_includes_polling_trigger() -> None:
    """The data-island wrapper carries ``hx-trigger="every 30s"``."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph")
    body = response.text
    assert 'id="topology-graph-data-wrapper"' in body
    assert 'hx-trigger="every 30s"' in body
    assert 'hx-swap="outerHTML"' in body
    # The refresh URL re-fetches the same view.
    assert 'hx-get="/ui/topology?view=graph' in body


def test_htmx_fragment_returns_data_island_wrapper_only() -> None:
    """An ``HX-Request: true`` GET returns just the data-island wrapper.

    The polling trigger fires this exact shape every 30s; the JS
    listens for ``htmx:afterSwap`` and re-renders Cytoscape preserving
    pan/zoom.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            "/ui/topology?view=graph",
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200, response.text
    body = response.text
    # The fragment is the wrapper only -- no <title>, no <body>, no nav chrome.
    assert "<title>" not in body
    assert "navbar" not in body
    # But it carries the data islands the controller reads.
    assert 'id="topology-graph-data-wrapper"' in body
    assert 'id="topology-graph-data"' in body
    assert 'hx-trigger="every 30s"' in body


def test_htmx_fragment_carries_overlay_data() -> None:
    """The HTMX fragment for a subgraph overlay carries the overlay payload."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_three_tier_graph(_TENANT_A)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            "/ui/topology?view=graph&from=host-1",
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200, response.text
    elements = _extract_data_island(response.text, "topology-graph-data")
    assert isinstance(elements, list)
    nodes = [e for e in elements if isinstance(e, dict) and e.get("group") == "nodes"]
    names = {n["data"]["name"] for n in nodes}
    # Same subgraph as the full-page render.
    assert names == {"host-1", "vm-1", "target-app"}
    # The wrapper's data attribute carries the overlay mode.
    assert 'data-overlay-mode="dependents"' in response.text


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


def test_dependents_overlay_isolates_other_tenants_graph() -> None:
    """Tenant A's overlay never surfaces tenant B's nodes or edges.

    Tenant B is seeded with the same-named ``host-1`` + an extra
    dependent unique to tenant B; tenant A's overlay rooted at
    ``host-1`` must never carry tenant B's row.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_tenant_row(_TENANT_B, "tenant-b")
    _seed_three_tier_graph(_TENANT_A)
    # Tenant B has its own host-1 plus a dependent unique to B.
    b_host = _seed_node(tenant_id=_TENANT_B, kind="host", name="host-1")
    b_vm = _seed_node(tenant_id=_TENANT_B, kind="vm", name="vm-tenant-b-only")
    _seed_edge(
        tenant_id=_TENANT_B,
        from_node_id=b_vm,
        to_node_id=b_host,
        kind="runs-on",
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=host-1")
    body = response.text
    # Tenant B's nodes never surface.
    assert "vm-tenant-b-only" not in body
    elements = _extract_data_island(body, "topology-graph-data")
    assert isinstance(elements, list)
    node_ids = {
        e["data"]["id"] for e in elements if isinstance(e, dict) and e.get("group") == "nodes"
    }
    assert str(b_host) not in node_ids
    assert str(b_vm) not in node_ids


def test_path_overlay_isolates_other_tenants_graph() -> None:
    """Cross-tenant path attempts return not-found (the endpoints don't resolve)."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_tenant_row(_TENANT_B, "tenant-b")
    # Tenant A: source only. Tenant B: target only.
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-source")
    _seed_node(tenant_id=_TENANT_B, kind="vm", name="vm-target")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        # Tenant A cannot see ``vm-target`` (it lives in tenant B); the
        # endpoint resolution returns 404 because the *to* anchor does
        # not exist in tenant A's scope.
        response = client.get("/ui/topology?view=graph&from=vm-source&to=vm-target")
    assert response.status_code == 404
    assert "not found" in response.text


# ---------------------------------------------------------------------------
# Ambiguous-name 409 / unknown-name 404
# ---------------------------------------------------------------------------


def test_dependents_unknown_name_returns_404() -> None:
    """An unknown ``?from=`` name returns 404 + the not-found error fragment."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=does-not-exist")
    assert response.status_code == 404
    assert "Node not found" in response.text


def test_dependents_ambiguous_name_returns_409() -> None:
    """A bare name resolving to multiple kinds returns 409 + disambiguation hint."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    # Two rows share the same name across kinds (target + vm).
    _seed_node(tenant_id=_TENANT_A, kind="target", name="app")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="app")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=app")
    assert response.status_code == 409
    body = response.text
    assert "Ambiguous" in body
    # The hint surfaces the candidate kinds so the operator can re-issue
    # with ``from_kind=<one>``.
    assert "target" in body
    assert "vm" in body


def test_dependents_kind_qualified_anchor_resolves_unambiguously() -> None:
    """``?from=app&from_kind=vm`` pins the anchor to the vm kind."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="target", name="app")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="app")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=app&from_kind=vm")
    assert response.status_code == 200, response.text


def test_path_overlay_unknown_endpoint_returns_404() -> None:
    """An unknown ``?to=`` endpoint surfaces 404."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=vm-1&to=ghost")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Drawer cross-link (G10.5-T1 #880 link target)
# ---------------------------------------------------------------------------


def test_node_drawer_dependents_link_uses_from_query_param() -> None:
    """The drawer's "Show dependents" link drives the new ``?from=`` overlay.

    Acceptance criterion (issue #882): the "show dependents" link
    from the node drawer (#880) drives the ``?from=`` subgraph view.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    node_id = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/topology/node/{node_id}")
    assert response.status_code == 200, response.text
    body = response.text
    # The link target uses ``?from=<name>&from_kind=<kind>``. The
    # ``&`` is HTML-escaped to ``&amp;`` in the Jinja-autoescape
    # ``href`` context -- the browser decodes both forms identically.
    assert "/ui/topology?view=graph&amp;from=vm-1" in body
    assert "from_kind=vm" in body


# ---------------------------------------------------------------------------
# Depth validation (route boundary)
# ---------------------------------------------------------------------------


def test_overlay_rejects_depth_outside_range() -> None:
    """``?depth=0`` is rejected at the FastAPI Query boundary (422)."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=vm-1&depth=0")
    assert response.status_code == 422
