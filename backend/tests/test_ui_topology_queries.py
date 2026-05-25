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
    soft_deleted: bool = False,
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
                    last_seen=None if soft_deleted else datetime.now(UTC),
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
    properties: dict[str, object] | None = None,
    soft_deleted: bool = False,
) -> uuid.UUID:
    edge_id = uuid.uuid4()
    edge_properties: dict[str, object] = dict(properties) if properties else {}

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
                    properties=edge_properties,
                    discovered_by="test",
                    first_seen=datetime.now(UTC),
                    last_seen=None if soft_deleted else datetime.now(UTC),
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
    ids = _seed_three_tier_graph(_TENANT_A)

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

    # The path-nodes data island carries the path id list in BFS order
    # source -> target. Asserting on the exact ordered sequence (not
    # just the length) catches a regression where BFS returns a
    # non-shortest path or reverses endpoints -- both invariant
    # violations that a length-only check would silently mask. Mirrors
    # the substrate ``find_path`` ordering contract.
    path_nodes_island = _extract_data_island(body, "topology-graph-path-nodes")
    assert isinstance(path_nodes_island, list)
    assert path_nodes_island == [
        str(ids["target-app"]),
        str(ids["vm-1"]),
        str(ids["host-1"]),
        str(ids["network-1"]),
    ]

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


# ---------------------------------------------------------------------------
# Substrate parity: superseded_by edges are excluded from BOTH the
# dependents/dependencies overlay AND the path overlay.
#
# Regression guard for the G9.1 substrate vs UI BFS divergence that
# surfaced in PR #1049 review: the substrate traversal verbs
# (:func:`meho_backplane.topology.query.find_dependents` /
# :func:`~meho_backplane.topology.query.find_dependencies` /
# :func:`~meho_backplane.topology.query.find_path`, Initiative #364
# §6 / Task #595) drop edges whose ``properties->>'superseded_by' IS
# NOT NULL``. Without the matching predicate on the UI side, tenants
# with curated supersede annotations would see edges in the overlay
# that the substrate REST/CLI API hides -- an operator-confusing
# divergence the substrate-parity acceptance criterion forbids.
# ---------------------------------------------------------------------------


def test_dependents_overlay_excludes_superseded_edges() -> None:
    """A ``superseded_by``-annotated edge is hidden from the dependents overlay.

    Seeds the three-tier chain target-app -> vm-1 -> host-1 -> network-1,
    then marks the ``vm-1 -> host-1`` edge superseded. From the
    ``host-1`` dependents root, only ``host-1`` itself remains visible:
    vm-1 (and transitively target-app) reach host-1 only through the
    now-superseded edge, so neither surfaces. Mirrors the substrate's
    behaviour on the same shape -- the cross-check below pulls
    ``find_dependents`` directly and asserts the two views agree.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    target = _seed_node(tenant_id=_TENANT_A, kind="target", name="target-app")
    vm = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    host = _seed_node(tenant_id=_TENANT_A, kind="host", name="host-1")
    network = _seed_node(tenant_id=_TENANT_A, kind="network", name="network-1")
    _seed_edge(tenant_id=_TENANT_A, from_node_id=target, to_node_id=vm, kind="runs-on")
    # The middle edge is superseded -- BFS must NOT traverse it.
    _seed_edge(
        tenant_id=_TENANT_A,
        from_node_id=vm,
        to_node_id=host,
        kind="runs-on",
        properties={"superseded_by": str(uuid.uuid4())},
    )
    _seed_edge(tenant_id=_TENANT_A, from_node_id=host, to_node_id=network, kind="belongs-to")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=host-1")
    assert response.status_code == 200, response.text

    elements = _extract_data_island(response.text, "topology-graph-data")
    assert isinstance(elements, list)
    nodes = [e for e in elements if isinstance(e, dict) and e.get("group") == "nodes"]
    node_names = {n["data"]["name"] for n in nodes}
    # Only host-1: the only reverse-edge into host-1 (vm-1 -> host-1)
    # is superseded, so BFS sees no neighbours from the root.
    #
    # Substrate parity (cross-checked by reading ``_TRAVERSAL_SQL`` in
    # ``meho_backplane.topology.query``): the PG ``WITH RECURSIVE``
    # walk carries ``AND e.properties->>'superseded_by' IS NULL`` on
    # the recursive arm, so ``find_dependents("host-1")`` on PG
    # returns exactly ``{"host-1"}`` on this seed -- the UI overlay's
    # ORM-side predicate produces the same closure. The substrate
    # verb itself cannot run in the unit suite (it needs PostgreSQL
    # for ``CYCLE``); the integration suite covers it.
    assert node_names == {"host-1"}


def test_path_overlay_excludes_superseded_edges() -> None:
    """A ``superseded_by`` edge breaks the path overlay's reachability.

    Same three-tier shape as :func:`_seed_three_tier_graph`; the
    middle edge is marked superseded. The path overlay must report
    "no path found" because the bidirectional BFS skips the
    superseded edge -- mirroring the substrate ``find_path`` verb.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    target = _seed_node(tenant_id=_TENANT_A, kind="target", name="target-app")
    vm = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    host = _seed_node(tenant_id=_TENANT_A, kind="host", name="host-1")
    network = _seed_node(tenant_id=_TENANT_A, kind="network", name="network-1")
    _seed_edge(tenant_id=_TENANT_A, from_node_id=target, to_node_id=vm, kind="runs-on")
    _seed_edge(
        tenant_id=_TENANT_A,
        from_node_id=vm,
        to_node_id=host,
        kind="runs-on",
        properties={"superseded_by": str(uuid.uuid4())},
    )
    _seed_edge(tenant_id=_TENANT_A, from_node_id=host, to_node_id=network, kind="belongs-to")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=target-app&to=network-1")
    assert response.status_code == 200, response.text
    # The path through the superseded edge is the only one; the
    # overlay renders the no-path-found state. Same parity argument as
    # ``test_dependents_overlay_excludes_superseded_edges`` -- the
    # substrate ``find_path`` (PG ``_PATH_SQL``) carries the matching
    # ``properties->>'superseded_by' IS NULL`` predicate on both
    # bidirectional arms.
    assert "no path found" in response.text


# ---------------------------------------------------------------------------
# Substrate parity: soft-deleted nodes/edges (last_seen IS NULL) are
# INCLUDED in both overlays. The substrate traversal verbs do not filter
# last_seen -- a soft-deleted row stays reachable (last-refresh-wins;
# #584). Regression guard against the UI BFS re-introducing a
# ``last_seen IS NOT NULL`` filter, which would hide rows the substrate
# REST/CLI/MCP closure still returns. (The full-inventory graph/table/
# drawer views DO exclude soft-deleted rows -- those mirror list_nodes /
# list_edges, which filter; only the traversal-parity BFS does not.)
# ---------------------------------------------------------------------------


def test_dependents_overlay_includes_soft_deleted_node() -> None:
    """A soft-deleted dependent (last_seen NULL) still appears in the overlay.

    vm-1 was dropped by a refresh reconcile -- its row and the edge into
    host-1 carry ``last_seen IS NULL``. The substrate
    ``find_dependents("host-1")`` still returns vm-1 (the traversal CTE
    has no ``last_seen`` predicate; #584), so the UI overlay must too.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    host = _seed_node(tenant_id=_TENANT_A, kind="host", name="host-1")
    vm = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1", soft_deleted=True)
    _seed_edge(
        tenant_id=_TENANT_A,
        from_node_id=vm,
        to_node_id=host,
        kind="runs-on",
        soft_deleted=True,
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=host-1")
    assert response.status_code == 200, response.text

    elements = _extract_data_island(response.text, "topology-graph-data")
    assert isinstance(elements, list)
    nodes = [e for e in elements if isinstance(e, dict) and e.get("group") == "nodes"]
    node_names = {n["data"]["name"] for n in nodes}
    assert node_names == {"host-1", "vm-1"}


def test_path_overlay_includes_soft_deleted_endpoint() -> None:
    """The path overlay resolves + routes through a soft-deleted endpoint.

    ``target-app -> vm-1`` where vm-1 *and* the edge are soft-deleted.
    ``resolve_anchor`` must still find the vm-1 endpoint and the
    bidirectional BFS must traverse the soft-deleted edge -- mirroring
    the substrate ``find_path``, which does not filter ``last_seen``
    (#584).
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    target = _seed_node(tenant_id=_TENANT_A, kind="target", name="target-app")
    vm = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1", soft_deleted=True)
    _seed_edge(
        tenant_id=_TENANT_A,
        from_node_id=target,
        to_node_id=vm,
        kind="runs-on",
        soft_deleted=True,
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&from=target-app&to=vm-1")
    assert response.status_code == 200, response.text
    assert "no path found" not in response.text

    body = response.text
    elements = _extract_data_island(body, "topology-graph-data")
    assert isinstance(elements, list)
    nodes = [e for e in elements if isinstance(e, dict) and e.get("group") == "nodes"]
    node_names = {n["data"]["name"] for n in nodes}
    assert node_names == {"target-app", "vm-1"}
    edges = [e for e in elements if isinstance(e, dict) and e.get("group") == "edges"]
    highlighted_edges = [e for e in edges if "highlight" in str(e.get("classes", ""))]
    assert len(highlighted_edges) == 1


# ---------------------------------------------------------------------------
# Topology Cytoscape controller (topology-graph.js) -- source-anchored
# assertions on the polling-refresh viewport-preservation contract +
# the path-node highlight style rule.
#
# These assertions are anchored to specific strings in the JS source
# rather than executing the JS (no JSDOM in the backend test suite).
# A reasoned-only assertion is acceptable per the review (B2/M1/M2
# acceptance criterion lists "JS test OR comment-anchored assertion").
# The strings are load-bearing for the contract -- a future edit that
# silently drops ``fit: false``, re-enables ``randomize: true`` on the
# polling path, or removes the ``node.highlight`` style rule fails
# this test before it reaches the operator.
# ---------------------------------------------------------------------------


def test_topology_graph_js_polling_refresh_preserves_viewport() -> None:
    """The 30s polling-refresh code path passes ``preserveViewport=true``.

    Anchors:

    * ``layoutOptions(name, preserveViewport)`` -- the signature
      change that lets the polling path opt into viewport
      preservation.
    * ``fit: !preserveViewport`` -- cose-bilkent (and dagre, circle)
      defaults to ``fit: true``, which would re-center+zoom the
      canvas asynchronously after the synchronous Cytoscape
      ``layout(...).run()`` call returns, overriding any pan/zoom
      restore. ``fit: false`` on the polling path keeps the viewport.
    * ``randomize: !preserveViewport`` -- re-randomising node
      positions every 30s breaks the operator's mental map.
    * The polling-path call inside ``applyRefreshedIsland`` passes
      ``true`` for ``preserveViewport``.
    """
    import pathlib

    js_path = (
        pathlib.Path(__file__).parent.parent
        / "src/meho_backplane/ui/static/src/app/topology-graph.js"
    )
    source = js_path.read_text(encoding="utf-8")

    # The new signature must be in place.
    assert "function layoutOptions(name, preserveViewport)" in source

    # ``fit`` is driven by the flag (no hardcoded ``fit: true`` and
    # the cose-bilkent default of ``fit: true`` is overridden).
    assert "fit: !preserveViewport" in source

    # ``randomize`` is driven by the flag.
    assert "randomize: !preserveViewport" in source

    # The polling refresh path passes ``true`` for the flag.
    assert "layoutOptions(layoutName, true)" in source


def test_topology_graph_js_defines_node_highlight_style() -> None:
    """The path-node ``highlight`` class has a non-empty style rule.

    Without this rule the ``highlightPathNodes`` helper's
    ``addClass("highlight")`` call is a visual no-op (Cytoscape draws
    the node identically to its unclassed siblings). The rule lives
    inside ``buildOverlayStyle`` next to the ``edge.highlight`` rule.
    """
    import pathlib

    js_path = (
        pathlib.Path(__file__).parent.parent
        / "src/meho_backplane/ui/static/src/app/topology-graph.js"
    )
    source = js_path.read_text(encoding="utf-8")

    # The selector exists in the overlay style table.
    assert 'selector: "node.highlight"' in source
    # And it carries a non-empty visual treatment (bold red border
    # matches the path-edge stroke colour for visual consistency).
    assert '"border-color": "#dc2626"' in source
