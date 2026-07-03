# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Topology UI tabular surface + drawer.

Initiative #342 (G10.5 Topology UI), Task #880 (G10.5-T1). The
acceptance criteria on issue #880 are:

* ``/ui/topology?view=table`` lists tenant nodes with sort + HTMX
  filter + multi-row checkbox select.
* ``/ui/topology/node/<id>`` drawer shows properties + incoming /
  outgoing edges + recent ops + a "show dependents" link.
* Cross-tenant isolation: another tenant's node never renders.
* ``ruff`` + ``mypy`` clean; ``pytest -n auto`` passes.

Suite shape:

* :func:`_build_app` constructs a minimal FastAPI app wired the
  same way :mod:`backend.tests.test_ui_chassis_smoke._build_app`
  does (UI session + CSRF middlewares, BFF auth router, UI router).
* :func:`_seed_tenant_and_nodes` populates two tenants with disjoint
  graph nodes + edges + audit rows so the cross-tenant assertion
  has concrete state to lean on.
* Test cases cover full-page render, HTMX fragment render,
  per-column sort, per-kind filter, name-substring filter, drawer
  happy path, drawer 404, and cross-tenant drawer isolation.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog, GraphEdge, GraphNode, Tenant
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
from meho_backplane.ui.templating import reset_templating_for_testing
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


_BACKPLANE_URL = "https://meho.test"

# Two stable tenant ids -- the same shape ``DEFAULT_TENANT_ID`` from
# :mod:`tests.conftest` uses, but distinct values so the cross-tenant
# isolation assertion has concrete state.
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test.

    Mirrors :func:`backend.tests.test_ui_chassis_smoke._bff_env` so the
    same chassis Keycloak / Vault / DB / encryption-key baseline holds
    here. Cache + global-state resets happen on both setup + teardown
    so a failing test cannot leak ``_TEMPLATES`` / session-engine state
    into the next case.
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
    """Construct a minimal FastAPI app wired for the topology UI tests.

    Mirrors the production wiring + the chassis smoke test:
    StaticFiles at ``/ui/static``, BFF auth router + UI surface
    router (which now includes the topology routes ahead of the
    stubs), ``UISessionMiddleware`` outermost + ``CSRFMiddleware``
    next. Audit / RequestContext middlewares are skipped -- the
    topology table is read-only so the audit row plumbing is out of
    scope here (covered by the chassis smoke suite for ``/ui/`` and
    by :mod:`backend.tests.test_audit_middleware` for ``/api/*``).
    """
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
    first_seen: datetime | None = None,
    last_seen: datetime | None = None,
    properties: dict[str, object] | None = None,
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
                    properties=properties or {},
                    discovered_by=discovered_by,
                    first_seen=first_seen or datetime.now(UTC),
                    last_seen=last_seen if last_seen is not None else datetime.now(UTC),
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
    discovered_by: str = "test",
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
                    discovered_by=discovered_by,
                    first_seen=datetime.now(UTC),
                    last_seen=datetime.now(UTC),
                ),
            )

    asyncio.run(_do())
    return edge_id


def _seed_audit_row(
    *,
    tenant_id: uuid.UUID,
    target_id: uuid.UUID,
    method: str = "GET",
    path: str = "/api/v1/example",
    status_code: int = 200,
) -> None:
    """Insert one ``audit_log`` row associated with *target_id*."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AuditLog(
                    id=uuid.uuid4(),
                    occurred_at=datetime.now(UTC),
                    operator_sub="op-1",
                    method=method,
                    path=path,
                    status_code=status_code,
                    request_id=uuid.uuid4(),
                    duration_ms=Decimal("12.50"),
                    payload={},
                    tenant_id=tenant_id,
                    target_id=target_id,
                ),
            )

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = "op-42",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row directly and return its UUID.

    Bypasses the BFF callback round-trip; the route only needs the
    session row to be loadable + decryptable, which
    :func:`create_session` provides synchronously.
    """

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


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_table_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/topology`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/topology")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_drawer_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/topology/node/<id>`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(f"/ui/topology/node/{uuid.uuid4()}")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# Full-page render -- 200 + chrome + table + multi-select markup
# ---------------------------------------------------------------------------


def test_table_full_page_renders_seeded_nodes() -> None:
    """``GET /ui/topology`` with a session returns the full page + seeded rows."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    target_id = uuid.uuid4()
    _seed_node(tenant_id=_TENANT_A, kind="target", name="vmware-prod", target_id=target_id)
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-app-01")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-app-02")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?view=table")
    assert response.status_code == 200, response.text
    body = response.text
    # Full-page chrome.
    assert "<title>Topology" in body
    assert 'href="/ui/topology"' in body  # sidebar link
    # All three seeded names render.
    assert "vmware-prod" in body
    assert "vm-app-01" in body
    assert "vm-app-02" in body
    # The multi-select master + per-row checkboxes are present.
    assert 'aria-label="Select all visible nodes"' in body
    assert body.count('type="checkbox"') >= 4  # master + 3 rows
    # Per-row "View" button drives the drawer HTMX swap.
    assert 'hx-target="#node-drawer"' in body
    # The drawer placeholder slot is in place for the swap.
    assert 'id="node-drawer"' in body
    # CSRF cookie set by the route.
    assert CSRF_COOKIE_NAME in response.cookies


def test_table_full_page_handles_empty_inventory() -> None:
    """An empty tenant inventory renders the "no nodes" empty-state row."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology")
    assert response.status_code == 200, response.text
    assert "No nodes match the current filter." in response.text


# ---------------------------------------------------------------------------
# HTMX fragment render -- the partial swaps just the <tbody>
# ---------------------------------------------------------------------------


def test_table_htmx_request_returns_fragment_only() -> None:
    """``HX-Request: true`` returns the ``_table_rows.html`` partial only."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-fragment")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology", headers={"HX-Request": "true"})
    assert response.status_code == 200, response.text
    body = response.text
    # Fragment starts with the <tbody> and has no <html>/<body> chrome.
    assert "<tbody" in body
    assert "<html" not in body.lower()
    assert "<title>" not in body.lower()
    # The seeded row is rendered inside the fragment.
    assert "vm-fragment" in body


# ---------------------------------------------------------------------------
# Sort
# ---------------------------------------------------------------------------


def test_table_sort_by_name_ascending_is_default() -> None:
    """Default sort is name ascending -- ``apple`` renders before ``banana``."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="banana")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="apple")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology")
    body = response.text
    assert body.index("apple") < body.index("banana"), (
        "expected ascending name sort to place 'apple' before 'banana'"
    )


def test_table_sort_by_name_descending_inverts_order() -> None:
    """``direction=desc`` flips the order -- ``banana`` before ``apple``."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="apple")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="banana")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?sort=name&direction=desc")
    body = response.text
    assert body.index("banana") < body.index("apple")


def test_table_sort_by_unknown_column_returns_422() -> None:
    """An out-of-enum ``sort`` value is rejected by Pydantic with 422."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?sort=bogus")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


def test_table_filter_by_kind_narrows_results() -> None:
    """``?kind=vm`` returns only ``vm`` rows."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-keep")
    _seed_node(tenant_id=_TENANT_A, kind="target", name="target-hide")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?kind=vm")
    body = response.text
    assert "vm-keep" in body
    assert "target-hide" not in body


def test_table_filter_by_name_substring_narrows_results() -> None:
    """``?q=prod`` returns only rows whose name contains ``prod``."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-prod-01")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-staging-01")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?q=prod")
    body = response.text
    assert "vm-prod-01" in body
    assert "vm-staging-01" not in body


def test_table_empty_kind_filter_returns_all_rows() -> None:
    """An empty ``?kind=`` ("All kinds") returns the full grid, not zero rows.

    The filter bar's default option is ``<option value="">``, so picking
    "All kinds" submits ``?kind=``. Without the empty-string coercion the
    exact-match filter applied ``WHERE kind = ''`` and wiped the grid.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-keep")
    _seed_node(tenant_id=_TENANT_A, kind="target", name="target-keep")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?kind=", headers={"HX-Request": "true"})
    assert response.status_code == 200, response.text
    body = response.text
    assert "vm-keep" in body
    assert "target-keep" in body


def test_table_empty_kind_with_name_search_still_narrows() -> None:
    """``?q=prod&kind=`` applies the name filter only.

    The filter ``<form>`` co-submits ``kind`` on every search keystroke
    (``hx-include="closest form"``), so a name search always rides with
    the (usually empty) ``kind``. The empty ``kind`` must be a no-op --
    previously it zeroed every search result.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-prod-01")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-staging-01")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            "/ui/topology", params={"q": "prod", "kind": ""}, headers={"HX-Request": "true"}
        )
    assert response.status_code == 200, response.text
    body = response.text
    assert "vm-prod-01" in body
    assert "vm-staging-01" not in body


def test_table_bogus_kind_filter_still_returns_no_rows() -> None:
    """Empty != invalid: a non-existent ``?kind=`` value still filters to zero rows."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-keep")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology?kind=not-a-kind", headers={"HX-Request": "true"})
    assert response.status_code == 200, response.text
    assert "vm-keep" not in response.text


# ---------------------------------------------------------------------------
# Cross-tenant isolation -- the table
# ---------------------------------------------------------------------------


def test_table_isolates_other_tenants_nodes() -> None:
    """Tenant A's session never sees tenant B's nodes in the table."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_tenant_row(_TENANT_B, "tenant-b")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-tenant-a-only")
    _seed_node(tenant_id=_TENANT_B, kind="vm", name="vm-tenant-b-only")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology")
    body = response.text
    assert "vm-tenant-a-only" in body
    assert "vm-tenant-b-only" not in body


# ---------------------------------------------------------------------------
# Drawer -- happy path + edges + recent ops + show-dependents link
# ---------------------------------------------------------------------------


def test_drawer_renders_node_properties_and_edges() -> None:
    """The drawer surfaces node properties + outgoing/incoming edges."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    parent_id = _seed_node(tenant_id=_TENANT_A, kind="host", name="host-1")
    child_id = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-on-host-1")
    _seed_edge(tenant_id=_TENANT_A, from_node_id=child_id, to_node_id=parent_id, kind="runs-on")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/topology/node/{child_id}")
    assert response.status_code == 200, response.text
    body = response.text
    # Node identity surfaces.
    assert "vm-on-host-1" in body
    assert str(child_id) in body
    # Outgoing edge surfaces (vm -> host-1).
    assert "host-1" in body
    assert "runs-on" in body
    # The "show dependents" link points at the T3 (#882) graph
    # overlay. The URL contract is ``?from=<name>&from_kind=<kind>``;
    # the link target carries both. ``&`` is HTML-escaped to ``&amp;``
    # by Jinja's ``href`` autoescape (browsers decode both forms).
    assert "Show dependents" in body
    assert "view=graph" in body
    assert "from=vm-on-host-1" in body


def test_drawer_renders_recent_ops_for_target_backed_node() -> None:
    """A node with ``target_id`` surfaces its recent audit_log rows."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    target_id = uuid.uuid4()
    node_id = _seed_node(
        tenant_id=_TENANT_A,
        kind="target",
        name="vmware-prod",
        target_id=target_id,
    )
    _seed_audit_row(
        tenant_id=_TENANT_A,
        target_id=target_id,
        method="POST",
        path="/api/v1/targets/vmware-prod/probe",
        status_code=200,
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/topology/node/{node_id}")
    assert response.status_code == 200, response.text
    body = response.text
    assert "Recent operations" in body
    assert "POST" in body
    assert "/api/v1/targets/vmware-prod/probe" in body


def test_drawer_shows_inner_node_has_no_audit_trail() -> None:
    """A node with no ``target_id`` shows the explanatory empty message."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    inner_id = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-inner")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/topology/node/{inner_id}")
    assert response.status_code == 200, response.text
    assert "Inner graph node" in response.text


# ---------------------------------------------------------------------------
# Drawer -- 404 + cross-tenant isolation
# ---------------------------------------------------------------------------


def test_drawer_returns_404_for_unknown_node() -> None:
    """A node id with no matching row in the tenant returns the 404 fragment."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/topology/node/{uuid.uuid4()}")
    assert response.status_code == 404, response.text
    assert "Node not found" in response.text


def test_drawer_isolates_other_tenants_node_id() -> None:
    """A node id that belongs to tenant B is invisible to tenant A's session."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_tenant_row(_TENANT_B, "tenant-b")
    other_tenant_node = _seed_node(tenant_id=_TENANT_B, kind="vm", name="secret-vm")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/topology/node/{other_tenant_node}")
    # Cross-tenant ids surface as 404, never as a successful render
    # of the other tenant's data.
    assert response.status_code == 404
    assert "secret-vm" not in response.text


# ---------------------------------------------------------------------------
# URL encoding -- node names with reserved characters round-trip safely
# ---------------------------------------------------------------------------


def test_drawer_dependents_href_percent_encodes_node_name_with_reserved_chars() -> None:
    """A node name containing ``&`` and a space renders a percent-encoded href.

    Regression test for the pre-fix ``f"...&from={node.name}&from_kind={node.kind}"``
    builder: a connector-populated ``graph_node.name`` containing reserved URL
    characters (``&`` ``?`` ``#`` ``+`` ``%`` space) would silently corrupt
    the dependents-view query string. The route now wraps both segments in
    ``urllib.parse.quote(safe='')`` so reserved bytes percent-encode the
    same way Jinja2's ``urlencode`` filter handles operator-typed filter
    values on the table page.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    tricky_node = _seed_node(
        tenant_id=_TENANT_A,
        kind="vm",
        name="vm prod & staging",
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/topology/node/{tricky_node}")
    assert response.status_code == 200, response.text
    body = response.text
    # The raw "&" inside the name would break the URL's query parsing
    # if interpolated verbatim. The percent-encoded form survives.
    assert "from=vm%20prod%20%26%20staging" in body
    assert "from_kind=vm" in body
    # Negative assertion: the unencoded form must NOT appear in the
    # dependents href -- the regression we are guarding against.
    assert 'href="/ui/topology?view=graph&from=vm prod & staging' not in body


def test_table_filter_href_percent_encodes_filters_with_reserved_chars() -> None:
    """Sort-column hrefs URL-encode active ``kind`` / ``q`` filters.

    Regression test for ``table.html``: the previous template
    interpolated ``kind_filter`` and ``name_filter`` raw into the
    sort-column ``href`` / ``hx-get`` attributes, so a search string
    like ``prod & dev`` (or a kind containing reserved chars on a
    future vocabulary extension) would silently corrupt the URL.
    The template now pipes both through Jinja2's ``urlencode`` filter.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-prod-and-dev")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        # ``q`` contains an ``&`` and a space -- if interpolated raw
        # into the sort-column href, the resulting URL would parse
        # back as ``q=prod`` plus a stray ``dev`` segment.
        response = client.get("/ui/topology?q=prod%20%26%20dev")
    assert response.status_code == 200, response.text
    body = response.text
    # The sort-column hrefs carry the active filter back into the URL.
    # Both forms (literal+ and percent) are acceptable urlencode outputs;
    # Jinja2's filter uses ``%20`` for space rather than ``+``.
    assert "q=prod%20%26%20dev" in body
    # The raw form (with a bare ``&``) must NOT appear -- that would
    # mean the regression slipped back in.
    assert "q=prod & dev" not in body


# ---------------------------------------------------------------------------
# Filter form preserves the cross-link selection (?selected=) across keystrokes
# ---------------------------------------------------------------------------


def test_filter_form_preserves_selected_id_across_filter_keystrokes() -> None:
    """The filter form carries ``selected`` as a hidden input.

    Cross-link AC (issue #881): a graph -> table click sets
    ``?selected=<id>``; the table page highlights + scrolls the
    matching row. The filter form is HTMX-wired with
    ``hx-include="closest form"`` + ``hx-push-url="true"``, so on
    the first filter keystroke HTMX collects only the inputs that
    live inside the form. Without an explicit ``selected`` hidden
    input the cross-link payload is dropped from the rebuilt URL
    and the row highlight is lost on the very first keystroke,
    directly subverting the cross-link AC.

    This test seeds a node, requests the table with the matching
    ``?selected=`` payload, and asserts the rendered filter form
    carries the id as a hidden input alongside ``sort`` / ``direction``
    so HTMX picks it up on every swap.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    target_id = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-keep-selected")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/topology?selected={target_id}")
    assert response.status_code == 200, response.text
    body = response.text
    # The selected hidden input rides inside the filter form. The
    # exact tag shape mirrors the existing sort/direction hidden
    # inputs so HTMX collects it on every swap.
    expected = f'<input type="hidden" name="selected" value="{target_id}" />'
    assert expected in body, (
        "filter form is missing the selected hidden input -- "
        "cross-link will be dropped on first keystroke"
    )


# ---------------------------------------------------------------------------
# Kind-filter dropdown -- sourced from the closed enum, not the current page
# ---------------------------------------------------------------------------


def test_kind_dropdown_lists_all_kinds_regardless_of_active_filter() -> None:
    """The kind dropdown surfaces the closed enum even when a filter narrows rows.

    Before the fix the dropdown was derived from the current page's
    rows (``sorted({node.kind for node in nodes})``); applying
    ``?kind=vm`` left the dropdown with only ``vm`` as an option,
    blocking the operator from switching kinds without manually
    editing the URL. The dropdown is now sourced from
    ``_GRAPH_NODE_KINDS`` so the full vocabulary stays available.

    The acceptance shape also covers the "paged out" sibling failure:
    51 ``vm`` rows + 1 ``target`` row at the default limit=50 would
    show only ``vm`` in the dropdown under the old derivation.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    # Seed enough vm rows to push past the default page limit (50)
    # plus one target row that would otherwise be paged off.
    for index in range(51):
        _seed_node(tenant_id=_TENANT_A, kind="vm", name=f"vm-{index:03d}")
    _seed_node(tenant_id=_TENANT_A, kind="target", name="target-paged-off")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        # Apply ``?kind=vm`` -- under the old derivation this would
        # collapse the dropdown to ``vm`` alone.
        response = client.get("/ui/topology?kind=vm")
    assert response.status_code == 200, response.text
    body = response.text
    # Both kinds plus several others from the closed enum render as
    # ``<option>`` rows.
    assert '<option value="vm"' in body
    assert '<option value="target"' in body
    # Sanity: a few more enum members from db.models._GRAPH_NODE_KINDS.
    assert '<option value="host"' in body
    assert '<option value="datastore"' in body


# ---------------------------------------------------------------------------
# Route mounting -- the real router wins the path lookup
# ---------------------------------------------------------------------------


def test_real_topology_route_wins_over_stub() -> None:
    """The G10.5-T1 route is registered before the chassis stub.

    Without the explicit ``include_router`` ordering in
    :func:`meho_backplane.ui.routes.build_router`, FastAPI's
    first-match-wins lookup would route ``/ui/topology`` to the
    chassis "Coming soon" stub instead of the real table view. This
    test pins the ordering invariant.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-route-pinner")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/topology")
    assert response.status_code == 200
    # The real view renders the seeded row + the H1; the stub
    # renders "Coming soon". Either marker would do; checking the
    # H1 marker is the cheapest discriminator.
    assert "vm-route-pinner" in response.text
    assert "Coming soon" not in response.text


def test_topology_route_not_shadowed_in_openapi_schema() -> None:
    """Regression: the real /ui/topology route must own the OpenAPI schema.

    Runtime first-match-wins serves the real table route (pinned by
    :func:`test_real_topology_route_wins_over_stub`), but FastAPI's
    ``get_openapi()`` lets a later-registered route at the same path
    *overwrite* an earlier one in the schema. The chassis ``topology``
    stub used to register ``GET /ui/topology`` and shadowed the real
    route in the generated ``cli/api/openapi.json`` (the CLI front-end
    of the feature). The stub entry was removed; this asserts the
    generated schema documents the real table route's query params and
    that the node-detail route declares its 404.
    """
    app = _build_app()
    schema = app.openapi()

    topology_op = schema["paths"]["/ui/topology"]["get"]
    param_names = {p["name"] for p in topology_op.get("parameters", [])}
    assert {"sort", "direction", "kind", "q", "limit", "view"} <= param_names, (
        f"GET /ui/topology OpenAPI op is missing the real table query params; "
        f"got {sorted(param_names)!r} — the chassis stub is shadowing the real "
        "route in the schema again."
    )

    node_op = schema["paths"]["/ui/topology/node/{node_id}"]["get"]
    assert "404" in node_op["responses"], (
        "GET /ui/topology/node/{node_id} must declare a 404 response in the "
        "OpenAPI schema (the handler returns 404 for missing/cross-tenant ids)."
    )
