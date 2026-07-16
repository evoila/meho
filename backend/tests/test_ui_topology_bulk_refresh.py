# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the topology console **batch** surface (bulk + refresh).

Initiative #1941 (G10.17 Topology console writes), Task #1954 (T2). T1
(#1953) shipped the single-edge write surface; this module covers the two
batch verbs the console gained:

* **Bulk import** (``tenant_admin``) — a CSRF-bearing
  ``POST /ui/topology/edges/bulk`` with ``dry_run=true`` renders the per-row
  ``create`` / ``update`` / ``conflict`` plan and persists **nothing**; the
  same with ``dry_run=false`` applies every row all-or-nothing.
* **invalid_bulk** — a batch with a bad row re-renders the panel with
  **every** row's error surfaced together (≥2 distinct row errors), not a
  first-error abort.
* **Refresh** (``operator``) — a ``POST /ui/topology/refresh/{target_name}``
  renders all six :class:`RefreshResult` counts inline; an unknown target
  renders the 404 + near-miss hint, not an empty 200.
* **No-populator callout** (#2093 / #2210) — a refresh against a product
  whose connector inherits the base ``discover_topology`` no-op renders
  the coverage-gap callout (naming the product + the populated
  alternatives); a populator-backed refresh renders **no** callout.
* **RBAC split** — the bulk panel + apply are hard-403'd for a plain
  ``operator`` (bulk = ``tenant_admin``); refresh succeeds for a plain
  ``operator`` (refresh = ``operator``) — verified against the real gate.
* **Route order** — ``/ui/topology/edges/bulk`` (literal) +
  ``/ui/topology/refresh/{target_name}`` (a ``{param}`` route) register
  ahead of ``detail.py``'s ``node/{node_id}`` param route.

The role-bearing JWKS client + CSRF header helpers mirror
:mod:`backend.tests.test_ui_topology_annotate`; the fake-connector + target
seed for the refresh path mirror :mod:`backend.tests.test_topology_refresh`.
"""

from __future__ import annotations

import asyncio
import re
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import EdgeHint, NodeHint, TopologyHints
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import GraphEdge, GraphNode, Target, Tenant
from meho_backplane.operations._handler_resolve import reset_connector_instance_cache
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.topology import build_router as build_topology_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import (
    AUDIENCE as _DEFAULT_AUDIENCE,
)
from tests._oidc_jwt_helpers import (
    ISSUER as _DEFAULT_ISSUER,
)
from tests._oidc_jwt_helpers import (
    make_rsa_keypair as _make_rsa_keypair,
)
from tests._oidc_jwt_helpers import (
    mint_token as _mint_token,
)
from tests._oidc_jwt_helpers import (
    mock_discovery_and_jwks as _mock_discovery_and_jwks,
)
from tests._oidc_jwt_helpers import (
    public_jwks as _public_jwks,
)
from tests._route_tree_helpers import iter_routes

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")

_OP_OPERATOR = "op-operator"
_OP_ADMIN = "op-admin"

#: Patch target for the refresh service's fail-open broadcast publish, so the
#: refresh path runs without a live broadcaster.
_PUBLISH = "meho_backplane.topology.refresh.publish_event"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the T1 harness)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
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
    clear_registry()
    reset_connector_instance_cache()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    clear_registry()
    reset_connector_instance_cache()


def _build_app() -> FastAPI:
    """Construct the minimal FastAPI app wired for the topology batch tests."""
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
                    discovered_by="test",
                    first_seen=datetime.now(UTC),
                    last_seen=datetime.now(UTC),
                ),
            )

    asyncio.run(_do())
    return node_id


def _seed_target(*, tenant_id: uuid.UUID, name: str, product: str) -> uuid.UUID:
    target_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                Target(
                    id=target_id,
                    tenant_id=tenant_id,
                    name=name,
                    aliases=[],
                    product=product,
                    host="vc.example.test",
                )
            )

    asyncio.run(_do())
    return target_id


def _count_edges(tenant_id: uuid.UUID, *, source: str | None = None) -> int:
    async def _do() -> int:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)
            if source is not None:
                stmt = stmt.where(GraphEdge.source == source)
            return len((await session.execute(stmt)).scalars().all())

    return asyncio.run(_do())


def _count_nodes(tenant_id: uuid.UUID) -> int:
    async def _do() -> int:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = select(GraphNode).where(GraphNode.tenant_id == tenant_id)
            return len((await session.execute(stmt)).scalars().all())

    return asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    access_token: str = "unused",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token=access_token,
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_do())


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-topology-batch-test-kid")
    return keypair, _public_jwks(keypair)


def _authenticated_client_with_role_jwks(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + respx mock + csrf token for the role-gated routes.

    Both the bulk gate (``_require_topology_admin``) and the refresh gate
    (``_require_topology_operator``) re-validate the BFF session's access
    token through the JWT chain, which needs the JWKS endpoint mocked. The
    caller stops the mock in a ``finally``.
    """
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=operator_sub,
        tenant_id=str(tenant_id),
        tenant_role=role.value,
    )
    session_id = _seed_session_sync(
        tenant_id=tenant_id,
        access_token=access_token,
        operator_sub=operator_sub,
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _csrf_headers(token: str) -> dict[str, str]:
    """Headers for an HTMX state-changing request -- CSRF + HX-Request."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


# ---------------------------------------------------------------------------
# Fake connector for the refresh path (mirrors test_topology_refresh)
# ---------------------------------------------------------------------------


class _FakeConnector(Connector):
    """Connector whose ``discover_topology`` returns a class-level snapshot."""

    product = "faketopo"

    hints: ClassVar[TopologyHints] = TopologyHints(discovered_at=datetime.now(UTC))

    async def fingerprint(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def execute(
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def discover_topology(self, target: Any) -> TopologyHints:
        return type(self).hints


def _register_fake() -> None:
    register_connector_v2(product="faketopo", version="", impl_id="", cls=_FakeConnector)


class _NoPopulatorConnector(Connector):
    """Connector inheriting the base ``discover_topology`` no-op default.

    Mirrors :mod:`backend.tests.test_topology_refresh`'s no-populator
    stand-in — the class the #2093 coverage-gap signal discriminates
    against a populator that ran clean.
    """

    product = "nopop"

    async def fingerprint(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def execute(
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


def _register_no_populator() -> None:
    register_connector_v2(product="nopop", version="", impl_id="", cls=_NoPopulatorConnector)


def _hints_2n1e() -> TopologyHints:
    return TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="vm", name="vm-a", properties={"power": "on"}),
            NodeHint(kind="datastore", name="ds-1"),
        ),
        edges=(
            EdgeHint(
                from_kind="vm",
                from_name="vm-a",
                to_kind="datastore",
                to_name="ds-1",
                kind="mounts",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Route ordering (acceptance criterion)
# ---------------------------------------------------------------------------


def test_topology_ui_batch_routes_register_before_node_param() -> None:
    """The bulk + refresh routes resolve ahead of ``node/{node_id}``.

    ``/ui/topology/edges/bulk`` (literal) and
    ``/ui/topology/refresh/{target_name}`` (itself a ``{param}`` route) must
    register BEFORE ``detail.py``'s ``/ui/topology/node/{node_id}`` param
    route so the first-match-wins lookup never binds them as a node id.
    """
    router = build_topology_router()
    # 0.137+ nests included routers; iter_routes flattens the tree in
    # registration order so the first-match-wins ordering check still holds.
    paths = [route.path for route in iter_routes(router.routes) if hasattr(route, "methods")]
    bulk_idx = paths.index("/ui/topology/edges/bulk")
    refresh_idx = paths.index("/ui/topology/refresh/{target_name}")
    node_idx = paths.index("/ui/topology/node/{node_id}")
    assert bulk_idx < node_idx, paths
    assert refresh_idx < node_idx, paths

    # Resolve through a real app: the routes must be registered with their
    # expected methods (the role gate fires inside the handlers, not here).
    app = _build_app()
    by_path: dict[str, set[str]] = {}
    for route in iter_routes(app.routes):
        path = getattr(route, "path", None)
        if path in ("/ui/topology/edges/bulk", "/ui/topology/refresh/{target_name}"):
            by_path.setdefault(path, set()).update(route.methods)  # type: ignore[attr-defined]
    assert "GET" in by_path["/ui/topology/edges/bulk"]
    assert "POST" in by_path["/ui/topology/edges/bulk"]
    assert "POST" in by_path["/ui/topology/refresh/{target_name}"]


# ---------------------------------------------------------------------------
# Bulk import: dry-run preview persists nothing (acceptance criterion)
# ---------------------------------------------------------------------------


def test_topology_ui_bulk_dry_run_renders_plan_and_persists_nothing() -> None:
    """A tenant_admin dry-run renders the per-row plan and writes nothing."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="principal", name="sa-foo")
    _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")

    rows = (
        "edges:\n"
        "  - from: { name: sa-foo, kind: principal }\n"
        "    kind: authenticates-via\n"
        "    to: { name: role-bar, kind: vault-role }\n"
    )

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/topology/edges/bulk",
            data={"rows_text": rows, "dry_run": "true"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # Per-row preview rendered with a create action.
    assert 'data-test="bulk-preview"' in body
    assert 'data-action="create"' in body
    assert "sa-foo" in body
    assert "role-bar" in body
    assert "authenticates-via" in body
    # Nothing persisted: no curated edge, and NO graph-refresh trigger.
    assert _count_edges(_TENANT_A) == 0
    assert response.headers.get("HX-Trigger") is None


def test_topology_ui_bulk_apply_creates_all_rows() -> None:
    """A tenant_admin apply (dry_run=false) writes every row + fires the trigger."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="principal", name="sa-foo")
    _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    _seed_node(tenant_id=_TENANT_A, kind="host", name="host-1")

    rows = (
        "edges:\n"
        "  - from: { name: sa-foo, kind: principal }\n"
        "    kind: authenticates-via\n"
        "    to: { name: role-bar, kind: vault-role }\n"
        "  - from: { name: vm-1, kind: vm }\n"
        "    kind: runs-on\n"
        "    to: { name: host-1, kind: host }\n"
    )

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/topology/edges/bulk",
            data={"rows_text": rows, "dry_run": "false"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-test="bulk-applied"' in body
    # Both rows applied as curated edges.
    assert _count_edges(_TENANT_A, source="curated") == 2
    # The apply fired the graph-refresh trigger so an open graph view re-pulls.
    assert response.headers.get("HX-Trigger") == "meho:topology-edge-changed"


# ---------------------------------------------------------------------------
# invalid_bulk: every bad row surfaced together (acceptance criterion)
# ---------------------------------------------------------------------------


def test_topology_ui_bulk_apply_via_multipart_file_upload() -> None:
    """An uploaded file (real multipart) applies; CSRF rides the header.

    The browser form posts ``multipart/form-data`` (``hx-encoding``) so the
    uploaded ``edges:`` doc rides as a file part. CSRF can't fall back to the
    form field on multipart — it must come from the ``X-CSRF-Token`` header —
    so this also exercises the multipart CSRF path.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    _seed_node(tenant_id=_TENANT_A, kind="host", name="host-1")

    rows = (
        "edges:\n"
        "  - from: { name: vm-1, kind: vm }\n"
        "    kind: runs-on\n"
        "    to: { name: host-1, kind: host }\n"
    )

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/topology/edges/bulk",
            data={"dry_run": "false"},
            files={"upload": ("edges.yaml", rows.encode(), "application/yaml")},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    assert 'data-test="bulk-applied"' in response.text
    assert _count_edges(_TENANT_A, source="curated") == 1


def test_topology_ui_bulk_invalid_rows_surface_every_error_together() -> None:
    """A batch with multiple bad rows re-renders with ALL row errors (>=2)."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="principal", name="sa-foo")
    _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")

    # Row 0: malformed edge-kind slug (T1 #2534: rejection is by shape,
    # not membership). Row 1: unresolvable endpoint. Two DISTINCT
    # row failures -> the panel must surface BOTH, not abort on the first.
    rows = (
        "edges:\n"
        "  - from: { name: sa-foo, kind: principal }\n"
        "    kind: 'Made Up Kind!'\n"
        "    to: { name: role-bar, kind: vault-role }\n"
        "  - from: { name: ghost-node, kind: principal }\n"
        "    kind: authenticates-via\n"
        "    to: { name: role-bar, kind: vault-role }\n"
    )

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/topology/edges/bulk",
            data={"rows_text": rows, "dry_run": "false"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 422, response.text
    body = response.text
    assert 'data-test="bulk-row-errors"' in body
    # Both row errors render together (assert >=2 distinct row-index errors).
    indices = set(re.findall(r'data-test="bulk-row-error" data-row-index="(\d+)"', body))
    assert indices >= {"0", "1"}, indices
    # The invalid-kind row names the bad kind; the missing-node row is present.
    assert "Made Up Kind!" in body
    assert "ghost-node" in body
    # Nothing persisted.
    assert _count_edges(_TENANT_A) == 0


def test_topology_ui_bulk_malformed_paste_renders_parse_banner() -> None:
    """A paste that is not an ``edges:`` doc re-renders a typed parse banner."""
    _seed_tenant_row(_TENANT_A, "tenant-a")

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/topology/edges/bulk",
            data={"rows_text": "this is not a yaml edges document", "dry_run": "true"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 422, response.text
    assert 'data-test="bulk-parse-error"' in response.text


# ---------------------------------------------------------------------------
# Bulk RBAC: hard-403 for a plain operator (acceptance criterion)
# ---------------------------------------------------------------------------


def test_topology_ui_bulk_panel_open_forbidden_for_operator() -> None:
    """The bulk-panel GET is hard-403'd for a plain operator."""
    _seed_tenant_row(_TENANT_A, "tenant-a")

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get("/ui/topology/edges/bulk")
    finally:
        mock.stop()

    assert response.status_code == 403, response.text


def test_topology_ui_bulk_apply_forged_from_operator_is_403() -> None:
    """A forged bulk POST from a non-admin gets a hard server-side 403."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="principal", name="sa-foo")
    _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")

    rows = (
        "edges:\n"
        "  - from: { name: sa-foo, kind: principal }\n"
        "    kind: authenticates-via\n"
        "    to: { name: role-bar, kind: vault-role }\n"
    )

    # Operator session carries a valid CSRF token (forged past the soft-hidden
    # button) -- the hard gate is the authority.
    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.post(
            "/ui/topology/edges/bulk",
            data={"rows_text": rows, "dry_run": "false"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    # No edge was written despite a valid CSRF token.
    assert _count_edges(_TENANT_A) == 0


def test_topology_ui_table_bulk_button_hidden_for_operator() -> None:
    """The table page does NOT render the bulk-import button for an operator."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get("/ui/topology?view=table")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # Neither the soft-hidden bulk button nor its mount slot render.
    assert 'data-test="bulk-import-open"' not in body
    assert 'data-test="bulk-modal-slot"' not in body


def test_topology_ui_table_bulk_button_shown_for_tenant_admin() -> None:
    """The table page renders the bulk-import button for a tenant_admin."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/topology?view=table")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-test="bulk-import-open"' in body
    assert 'data-test="bulk-modal-slot"' in body


def test_topology_ui_bulk_panel_renders_for_admin_with_csrf() -> None:
    """The bulk-panel GET renders the form + sets the meho_csrf cookie."""
    _seed_tenant_row(_TENANT_A, "tenant-a")

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/topology/edges/bulk")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="topology-bulk-modal"' in body
    assert 'data-test="bulk-dry-run"' in body
    assert 'data-test="bulk-apply"' in body
    assert CSRF_COOKIE_NAME in response.cookies


# ---------------------------------------------------------------------------
# Refresh: all six counts inline (acceptance criterion)
# ---------------------------------------------------------------------------


def test_topology_ui_refresh_renders_all_six_counts_for_operator() -> None:
    """A plain operator refresh renders all six RefreshResult counts inline."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="vcenter-a", product="faketopo")
    _register_fake()
    _FakeConnector.hints = _hints_2n1e()

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        with patch(_PUBLISH, new=AsyncMock()):
            response = client.post(
                "/ui/topology/refresh/vcenter-a",
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-test="refresh-result"' in body
    # All six disjoint counts render (the issue's "assert each appears").
    for hook in (
        "refresh-added-nodes",
        "refresh-added-edges",
        "refresh-updated-nodes",
        "refresh-updated-edges",
        "refresh-removed-nodes",
        "refresh-removed-edges",
    ):
        assert f'data-test="{hook}"' in body, hook
    # 2 nodes + 1 edge discovered -> they were written under this tenant.
    assert _count_nodes(_TENANT_A) == 2
    assert _count_edges(_TENANT_A) == 1
    # The reconcile changed the graph -> the refresh fired the re-pull trigger.
    assert response.headers.get("HX-Trigger") == "meho:topology-edge-changed"
    # A populator ran -> no_populator_for_product is null -> the coverage-gap
    # callout must NOT render (#2093 / #2210: null branch stays unchanged).
    assert 'data-test="refresh-no-populator"' not in body
    assert 'data-test="refresh-populated-products"' not in body


def test_topology_ui_refresh_no_populator_renders_coverage_gap_callout() -> None:
    """A populator-less product's refresh renders the coverage-gap callout.

    #2093 gave :class:`RefreshResult` the ``no_populator_for_product`` +
    ``populated_products`` signal and the CLI its coverage-gap note; #2210
    surfaces the same signal in the console's refresh partial. Registers one
    populated product (``faketopo``) and one populator-less product
    (``nopop``), refreshes a ``nopop`` target, and asserts the callout names
    the gap product + lists the populated alternative next to the (still
    rendered) all-zero counts.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="argocd-style-target", product="nopop")
    _register_fake()
    _register_no_populator()

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        with patch(_PUBLISH, new=AsyncMock()):
            response = client.post(
                "/ui/topology/refresh/argocd-style-target",
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The counts still render (the callout supplements, never replaces them).
    assert 'data-test="refresh-result"' in body
    assert 'data-test="refresh-added-nodes"' in body
    # The coverage-gap callout names the populator-less product...
    assert 'data-test="refresh-no-populator"' in body
    assert "nopop" in body
    assert "has no topology populator" in body
    # ...and lists the registered products that DO ship a populator.
    assert 'data-test="refresh-populated-products"' in body
    assert "faketopo" in body
    # The base no-op discovered nothing and nothing pre-existed -> the graph
    # is unchanged, so no re-pull trigger fired.
    assert _count_nodes(_TENANT_A) == 0
    assert response.headers.get("HX-Trigger") is None


def test_topology_ui_refresh_unknown_target_renders_404_near_miss() -> None:
    """An unknown target_name renders the 404 + near-miss hint, not empty 200.

    The resolver's near-miss is a prefix-ILIKE (``name ILIKE '<query>%'``),
    so the unresolved query must be a prefix of an existing target name for a
    suggestion to surface. ``vcenter`` is a prefix of ``vcenter-prod`` but is
    not an exact name match -> the resolver 404s with ``vcenter-prod`` in the
    near-miss list.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    # A target whose name the unresolved query is a prefix of -> near-miss.
    _seed_target(tenant_id=_TENANT_A, name="vcenter-prod", product="faketopo")
    _register_fake()

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.post(
            "/ui/topology/refresh/vcenter",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    # 404 (NOT an empty 200) with the typed not-found fragment.
    assert response.status_code == 404, response.text
    body = response.text
    assert 'data-test="refresh-not-found"' in body
    # The near-miss hint surfaces the candidate target name.
    assert 'data-test="refresh-near-misses"' in body
    assert "vcenter-prod" in body


def test_topology_ui_drawer_refresh_button_shown_for_target_node_operator() -> None:
    """The drawer renders the Refresh button on a target node for an operator.

    Refresh is an ``operator``-tier verb, so the affordance is visible to a
    plain operator (unlike the tenant_admin-only annotate / bulk controls).
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    target_id = _seed_target(tenant_id=_TENANT_A, name="vcenter-a", product="faketopo")
    node_id = _seed_node(tenant_id=_TENANT_A, kind="target", name="vcenter-a", target_id=target_id)

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get(f"/ui/topology/node/{node_id}")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The refresh affordance + its result slot render for the target node.
    assert 'data-test="refresh-target"' in body
    assert 'data-test="refresh-result-slot"' in body
    # The annotate / remove (tenant_admin) controls stay hidden for the operator.
    assert 'data-test="annotate-edge-open"' not in body


def test_topology_ui_drawer_refresh_button_absent_for_non_target_node() -> None:
    """A non-target node's drawer carries no Refresh button."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    node_id = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get(f"/ui/topology/node/{node_id}")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    assert 'data-test="refresh-target"' not in response.text


def test_topology_ui_refresh_without_csrf_is_403() -> None:
    """The refresh POST without the CSRF token is rejected by the middleware."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="vcenter-a", product="faketopo")
    _register_fake()

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        client.cookies.delete(CSRF_COOKIE_NAME)
        response = client.post(
            "/ui/topology/refresh/vcenter-a",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
