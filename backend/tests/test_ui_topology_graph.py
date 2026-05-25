# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Topology UI Cytoscape graph view.

Initiative #342 (G10.5 Topology UI), Task #881 (G10.5-T2). The
acceptance criteria on issue #881 are:

* ``/ui/topology?view=graph`` renders a Cytoscape graph (vendored
  Cytoscape + cose-bilkent + dagre, SHA256-pinned) for up to 500
  nodes; pan + zoom + node-click work.
* Node click opens the detail drawer (reusing the #880
  ``/ui/topology/node/<id>`` partial).
* Layout switcher toggles cose-bilkent / dagre / circle via
  ``cy.layout(...).run()``.
* Tabular <-> graph cross-link: selecting a table row centers +
  selects the node in the graph; a graph tap navigates back to the
  table with the row scrolled into view; selection preserved via
  query params.
* The 500-node cap is enforced (beyond it, the page prompts to
  narrow via the T3 subgraph query) and documented.
* Cross-tenant isolation holds; ``ruff`` + ``mypy`` clean;
  ``pytest -n auto backend/tests/test_ui_topology_graph.py`` passes.

Suite shape:

* :func:`_build_app` constructs a minimal FastAPI app the same way
  :mod:`backend.tests.test_ui_topology_table` does -- UI session +
  CSRF middlewares, BFF auth router, UI router.
* :func:`_seed_*` helpers populate two tenants with disjoint graph
  nodes + edges so cross-tenant assertions have concrete state.
* Test cases cover: full-page render, Cytoscape script wiring +
  data-island shape, layout switcher options, node/edge JSON
  emission, 500-node cap + truncation banner, view toggle preserves
  filters, cross-link ``?selected=<id>`` round-trip, cross-tenant
  isolation, drawer route (the existing T1 route) still reachable
  from the graph context.
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
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.topology.graph import GRAPH_NODE_CAP
from meho_backplane.ui.templating import reset_templating_for_testing
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


_BACKPLANE_URL = "https://meho.test"

# Two stable tenant ids -- same shape as the T1 suite. Cross-tenant
# isolation assertion uses them directly.
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test.

    Mirrors :func:`backend.tests.test_ui_topology_table._bff_env` so
    cache + global-state resets happen on both setup + teardown.
    """
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
    """Construct a minimal FastAPI app wired for the topology graph tests."""
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
    """Insert one ``tenant`` row so the graph FKs resolve."""

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
    """Insert one ``graph_node`` row and return its id."""
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
    """Insert one ``graph_edge`` row and return its id."""
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
    """Create a ``web_session`` row directly and return its UUID."""

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
    """Return a TestClient with the session cookie pre-set."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _extract_data_island(body: str, island_id: str) -> object:
    """Extract + JSON-parse a ``<script type="application/json">`` data island.

    Each island in the graph template carries the elements (or the
    selected-id payload) the controller reads on init. Tests rely on
    the parsed JSON to verify the server-emitted shape independent of
    HTML formatting whitespace.
    """
    pattern = re.compile(
        rf'<script type="application/json" id="{re.escape(island_id)}">(.*?)</script>',
        re.DOTALL,
    )
    match = pattern.search(body)
    assert match is not None, f"data island {island_id!r} not found in body"
    return json.loads(match.group(1))


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_graph_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/topology?view=graph`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/topology?view=graph")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# Full-page render -- 200 + Cytoscape mount + script wiring
# ---------------------------------------------------------------------------


def test_graph_full_page_renders_cytoscape_mount() -> None:
    """``view=graph`` returns the full page with the Cytoscape mount + script chain."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph")
    assert response.status_code == 200, response.text
    body = response.text
    # Page chrome.
    assert "<title>Topology" in body
    # Cytoscape mount div.
    assert 'id="cy"' in body
    # The vendored layout-plugin chain is wired in load-bearing order
    # (see VENDOR.md "Cytoscape layout plugins"); each src appears
    # exactly once.
    assert "/ui/static/src/vendor/cytoscape.min.js" in body
    assert "/ui/static/src/vendor/layout-base.js" in body
    assert "/ui/static/src/vendor/cose-base.js" in body
    assert "/ui/static/src/vendor/cytoscape-cose-bilkent.js" in body
    assert "/ui/static/src/vendor/cytoscape-dagre.js" in body
    # Per-page controller.
    assert "/ui/static/src/app/topology-graph.js" in body
    # CSRF cookie set by the route (matches the table route's posture).
    assert CSRF_COOKIE_NAME in response.cookies


def test_graph_full_page_emits_node_and_edge_elements() -> None:
    """The Cytoscape elements data island carries seeded nodes + edges."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    host_id = _seed_node(tenant_id=_TENANT_A, kind="host", name="host-1")
    vm_id = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    _seed_edge(tenant_id=_TENANT_A, from_node_id=vm_id, to_node_id=host_id, kind="runs-on")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph")
    assert response.status_code == 200, response.text

    elements = _extract_data_island(response.text, "topology-graph-data")
    assert isinstance(elements, list)
    # Two nodes + one edge.
    nodes = [e for e in elements if isinstance(e, dict) and e.get("group") == "nodes"]
    edges = [e for e in elements if isinstance(e, dict) and e.get("group") == "edges"]
    assert len(nodes) == 2
    assert len(edges) == 1
    node_names = {n["data"]["name"] for n in nodes}
    assert node_names == {"host-1", "vm-1"}
    # Edge endpoints reference the node UUIDs by string.
    edge = edges[0]
    assert edge["data"]["source"] == str(vm_id)
    assert edge["data"]["target"] == str(host_id)
    assert edge["data"]["kind"] == "runs-on"
    # Per-kind classes drive the Cytoscape stylesheet selectors.
    host_element = next(n for n in nodes if n["data"]["name"] == "host-1")
    assert host_element["classes"] == "kind-host"


def test_graph_full_page_lists_node_and_edge_counts() -> None:
    """The status row surfaces the rendered node + edge counts."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-2")
    _seed_node(tenant_id=_TENANT_A, kind="host", name="host-1")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph")
    assert response.status_code == 200, response.text
    body = response.text
    assert "Rendering" in body
    # The status row shows "Rendering 3 nodes / 0 edges" (no edges seeded).
    assert ">3</span>" in body
    assert ">0</span>" in body


# ---------------------------------------------------------------------------
# Layout switcher
# ---------------------------------------------------------------------------


def test_graph_layout_switcher_offers_three_layouts() -> None:
    """The layout ``<select>`` exposes cose-bilkent / dagre / circle."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph")
    body = response.text
    assert 'id="topology-graph-layout"' in body
    assert '<option value="cose-bilkent"' in body
    assert '<option value="dagre"' in body
    assert '<option value="circle"' in body
    # cose-bilkent is the default per the task body.
    assert '<option value="cose-bilkent" selected>' in body


# ---------------------------------------------------------------------------
# 500-node cap + truncation banner
# ---------------------------------------------------------------------------


def test_graph_caps_render_at_500_nodes() -> None:
    """The graph renders at most 500 nodes and surfaces a truncation banner.

    Acceptance criterion: "The 500-node cap is enforced (beyond it,
    the page prompts to narrow via the T3 subgraph query) and
    documented" (issue #881). The cap is enforced by passing
    ``limit=GRAPH_NODE_CAP`` into the substrate ``list_nodes``; the
    banner is rendered when the returned list saturates the cap.
    """
    assert GRAPH_NODE_CAP == 500  # The contract; bumping it is a doc + perf review.

    _seed_tenant_row(_TENANT_A, "tenant-a")
    # Seed one row past the cap. The route caps at 500, so the
    # 501st node is silently dropped and the banner flips on.
    for index in range(GRAPH_NODE_CAP + 1):
        _seed_node(tenant_id=_TENANT_A, kind="vm", name=f"vm-{index:04d}")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph")
    assert response.status_code == 200, response.text
    body = response.text

    elements = _extract_data_island(body, "topology-graph-data")
    assert isinstance(elements, list)
    nodes = [e for e in elements if isinstance(e, dict) and e.get("group") == "nodes"]
    assert len(nodes) == GRAPH_NODE_CAP, (
        f"expected the cap to truncate to {GRAPH_NODE_CAP} nodes, got {len(nodes)}"
    )
    # Truncation banner copy.
    assert f"Capped at {GRAPH_NODE_CAP}" in body
    # And the call-out to T3.
    assert "dependents query" in body or "dependents" in body


def test_graph_does_not_show_truncation_banner_under_the_cap() -> None:
    """Inventories below the cap render without the banner."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph")
    assert response.status_code == 200, response.text
    assert f"Capped at {GRAPH_NODE_CAP}" not in response.text


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_graph_filter_by_kind_narrows_results() -> None:
    """``?view=graph&kind=vm`` returns only ``vm`` nodes."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-keep")
    _seed_node(tenant_id=_TENANT_A, kind="target", name="target-hide")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&kind=vm")
    assert response.status_code == 200, response.text

    elements = _extract_data_island(response.text, "topology-graph-data")
    assert isinstance(elements, list)
    names = {
        e["data"]["name"] for e in elements if isinstance(e, dict) and e.get("group") == "nodes"
    }
    assert names == {"vm-keep"}


def test_graph_filter_by_name_substring_narrows_results() -> None:
    """``?view=graph&q=prod`` returns only nodes whose name contains ``prod``."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-prod-01")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-staging-01")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&q=prod")

    elements = _extract_data_island(response.text, "topology-graph-data")
    assert isinstance(elements, list)
    names = {
        e["data"]["name"] for e in elements if isinstance(e, dict) and e.get("group") == "nodes"
    }
    assert names == {"vm-prod-01"}


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


def test_graph_isolates_other_tenants_nodes() -> None:
    """Tenant A's graph view never carries tenant B's nodes."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_tenant_row(_TENANT_B, "tenant-b")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-tenant-a")
    _seed_node(tenant_id=_TENANT_B, kind="vm", name="vm-tenant-b")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph")
    body = response.text
    assert "vm-tenant-a" in body
    assert "vm-tenant-b" not in body


def test_graph_excludes_cross_tenant_edges() -> None:
    """Edges with cross-tenant endpoints never surface on the graph view.

    Tenant A has its own host + vm + edge. Tenant B has a separate
    edge tenant A's session should never see, even though both
    tenants have nodes named identically.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_tenant_row(_TENANT_B, "tenant-b")
    a_host = _seed_node(tenant_id=_TENANT_A, kind="host", name="host-shared")
    a_vm = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-shared")
    _seed_edge(
        tenant_id=_TENANT_A,
        from_node_id=a_vm,
        to_node_id=a_host,
        kind="runs-on",
    )

    b_host = _seed_node(tenant_id=_TENANT_B, kind="host", name="host-shared")
    b_vm = _seed_node(tenant_id=_TENANT_B, kind="vm", name="vm-shared")
    b_edge = _seed_edge(
        tenant_id=_TENANT_B,
        from_node_id=b_vm,
        to_node_id=b_host,
        kind="runs-on",
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph")
    elements = _extract_data_island(response.text, "topology-graph-data")
    assert isinstance(elements, list)
    # Tenant A sees its own edge (one edge, source vm_a -> target host_a).
    edges = [e for e in elements if isinstance(e, dict) and e.get("group") == "edges"]
    assert len(edges) == 1
    assert edges[0]["data"]["source"] == str(a_vm)
    assert edges[0]["data"]["target"] == str(a_host)
    # Tenant B's edge id never surfaces.
    edge_ids = {e["data"]["id"] for e in edges}
    assert str(b_edge) not in edge_ids


# ---------------------------------------------------------------------------
# View toggle (table <-> graph)
# ---------------------------------------------------------------------------


def test_graph_page_links_back_to_table_view() -> None:
    """The graph page header renders a link to the tabular view."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph")
    body = response.text
    assert 'href="/ui/topology?view=table' in body


def test_table_page_links_back_to_graph_view() -> None:
    """The table page header renders a link to the graph view."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology")
    body = response.text
    assert 'href="/ui/topology?view=graph' in body


def test_view_toggle_preserves_active_filter() -> None:
    """A toggle from graph to table carries the active filter values."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&kind=vm&q=foo")
    body = response.text
    # The toggle link includes the active kind + name filter.
    assert "view=table" in body
    assert "kind=vm" in body
    assert "q=foo" in body


# ---------------------------------------------------------------------------
# Cross-link (table <-> graph) via ?selected=<id>
# ---------------------------------------------------------------------------


def test_graph_selected_id_round_trips_into_data_island() -> None:
    """``?selected=<id>`` lands as a non-empty data island the JS reads on init."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    node_id = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-target")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/topology?view=graph&selected={node_id}")
    assert response.status_code == 200, response.text
    payload = _extract_data_island(response.text, "topology-graph-selected")
    # Server emits a string id; the JS uses it to look up the
    # matching Cytoscape node on init.
    assert payload == str(node_id)


def test_graph_without_selection_emits_empty_island() -> None:
    """The selected-id island is empty when no ``?selected=`` is present."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph")
    payload = _extract_data_island(response.text, "topology-graph-selected")
    assert payload == ""


def test_graph_invalid_selected_uuid_returns_422() -> None:
    """A non-UUID ``?selected=`` value 422s at the Pydantic boundary.

    The route declares ``selected: uuid.UUID | None`` so a malformed
    payload is rejected with a 422 + diagnostic context, NOT silently
    decayed to ``None`` (which would mask a misuse).
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=graph&selected=not-a-uuid")
    assert response.status_code == 422


def test_table_marks_selected_row() -> None:
    """``?view=table&selected=<id>`` marks the matching row for scroll-into-view."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-not-selected")
    target_id = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-target")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/topology?view=table&selected={target_id}")
    body = response.text
    # The matching row carries the marker the scroll-into-view helper picks up.
    assert 'data-selected="true"' in body
    # And only one row -- the one with the matching id.
    selected_row_count = body.count('data-selected="true"')
    assert selected_row_count == 1, f"expected exactly one selected row, got {selected_row_count}"
    # The cross-link script tag is wired.
    assert "/ui/static/src/app/topology-table.js" in body


def test_table_rows_include_show_in_graph_link() -> None:
    """Every table row carries a "Graph" cross-link to the graph view."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    node_id = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology")
    body = response.text
    # The Graph link href targets the graph view with this node selected.
    assert f"/ui/topology?view=graph&selected={node_id}" in body


# ---------------------------------------------------------------------------
# Drawer reuse -- the existing T1 route is the drawer target from the graph
# ---------------------------------------------------------------------------


def test_drawer_route_still_serves_node_detail_for_graph_tap() -> None:
    """``GET /ui/topology/node/<id>`` (T1 #880) returns the drawer fragment.

    The graph view's ``cy.on('tap', 'node', ...)`` handler issues
    ``htmx.ajax`` GET against this exact path -- regression-pinning
    here so a future refactor cannot break the cross-surface drawer
    contract without taking the suite down.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    node_id = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-drawer-test")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/topology/node/{node_id}")
    assert response.status_code == 200, response.text
    assert "vm-drawer-test" in response.text


# ---------------------------------------------------------------------------
# OpenAPI schema -- the view enum surfaces on the table route's schema
# ---------------------------------------------------------------------------


def test_view_enum_includes_graph_in_openapi_schema() -> None:
    """The ``view`` query param's enum carries ``graph`` (T2 contract).

    Regression-pin: a future refactor that drops the enum (e.g. swaps
    to a bare ``str``) would mask invalid mode values; the OpenAPI
    schema breaks on `view=bogus` returning 422 -- the test asserts
    the generated schema documents the closed enum.
    """
    app = _build_app()
    schema = app.openapi()
    topology_op = schema["paths"]["/ui/topology"]["get"]
    view_param = next(p for p in topology_op["parameters"] if p["name"] == "view")
    # The schema for an enum query param either inlines the enum or
    # references it via $ref -- accept both. The inlined form puts
    # ``enum`` directly under ``schema``; the $ref form references a
    # component schema. We assert at least one of the resolution paths
    # carries the ``graph`` member.
    schema_obj = view_param.get("schema", {})
    enum_values: list[str] = []
    if "enum" in schema_obj:
        enum_values = list(schema_obj["enum"])
    elif "$ref" in schema_obj:
        ref_path = schema_obj["$ref"].lstrip("#/").split("/")
        target = schema
        for step in ref_path:
            target = target[step]
        enum_values = list(target.get("enum", []))
    assert "graph" in enum_values
    assert "table" in enum_values


def test_invalid_view_mode_returns_422() -> None:
    """An out-of-enum ``view`` value is rejected by Pydantic with 422."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=hologram")
    assert response.status_code == 422
