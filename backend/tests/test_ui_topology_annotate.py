# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the topology console curated-edge **write** surface.

Initiative #1941 (G10.17 Topology console writes), Task #1953 (T1).
Acceptance criteria from the issue body:

* A CSRF-bearing ``POST /ui/topology/edges`` as a ``tenant_admin`` session
  creates a curated edge (note + evidence_url persisted) and re-renders the
  edge inline; the same POST WITHOUT the CSRF token -> 403.
* A confirm-gated ``DELETE /ui/topology/edges/{id}`` against a
  ``source='auto'`` edge surfaces the 409 ``auto_edge_deletion`` path as a
  recoverable typed banner ("this is an auto edge; annotate over it first"),
  NOT a dead 500/empty error.
* Annotating with a bare ``from`` / ``to`` name that resolves to >1 kind
  re-renders the modal with the candidate ``kinds`` listed (not a dead 409).
* A plain ``operator`` session does NOT see the annotate / remove controls
  in the rendered drawer (soft-hide), AND a forged ``POST
  /ui/topology/edges`` from a non-admin gets a hard 403 (server-side
  ``tenant_admin`` gate).
* A route-order test (FastAPI test client at construction) asserts
  ``/ui/topology/edges/annotate`` resolves to the annotate handler, NOT
  ``detail.py``'s ``node/{node_id}`` param route.

The role-bearing JWKS client + CSRF header helpers mirror
:mod:`backend.tests.test_ui_connectors_view`; the per-tenant graph seed
helpers mirror :mod:`backend.tests.test_ui_topology_queries`.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import GraphEdge, GraphNode, Tenant
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
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

_OP_OPERATOR = "op-operator"
_OP_ADMIN = "op-admin"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test.

    Mirrors the UI-surface baseline (``test_ui_topology_queries`` /
    ``test_ui_connectors_view``): chassis Keycloak / Vault / DB /
    encryption-key env + cache resets on both setup and teardown so a
    failing test cannot leak template / session-engine state.
    """
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
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def _build_app() -> FastAPI:
    """Construct the minimal FastAPI app wired for the topology write tests."""
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


def _fetch_edge(edge_id: uuid.UUID) -> GraphEdge | None:
    async def _do() -> GraphEdge | None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(select(GraphEdge).where(GraphEdge.id == edge_id))
            return result.scalar_one_or_none()

    return asyncio.run(_do())


def _fetch_curated_edge_id(tenant_id: uuid.UUID) -> uuid.UUID | None:
    """Return the id of the single curated edge in *tenant_id*, if any."""

    async def _do() -> uuid.UUID | None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(GraphEdge.id).where(
                    GraphEdge.tenant_id == tenant_id,
                    GraphEdge.source == "curated",
                )
            )
            return result.scalars().first()

    return asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = _OP_OPERATOR,
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
        keypair = _make_rsa_keypair("ui-topology-write-test-kid")
    return keypair, _public_jwks(keypair)


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    """Return a TestClient with the session cookie pre-set (no JWKS mock)."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _authenticated_client_with_role_jwks(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + respx mock + csrf token for the role-gated routes.

    The write gate (``_require_topology_admin``) re-validates the BFF
    session's access token through the JWT chain, which needs the JWKS
    endpoint mocked. The caller stops the mock in a ``finally``.
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
# Route ordering (acceptance criterion: literal beats the {node_id} param)
# ---------------------------------------------------------------------------


def test_topology_ui_annotate_route_registers_before_node_param() -> None:
    """``/ui/topology/edges/annotate`` resolves to the annotate handler.

    The literal ``edges`` / ``annotate`` segments must register BEFORE
    ``detail.py``'s ``/ui/topology/node/{node_id}`` param route so the
    first-match-wins lookup never binds them as a node id. Verified at
    construction with a FastAPI test client matching the route, per the
    convention ``topology/__init__.py`` documents.
    """
    router = build_topology_router()
    # 0.137+ nests included routers; iter_routes flattens the tree in
    # registration order so the first-match-wins ordering check still holds.
    paths = [route.path for route in iter_routes(router.routes) if hasattr(route, "methods")]
    annotate_idx = paths.index("/ui/topology/edges/annotate")
    node_idx = paths.index("/ui/topology/node/{node_id}")
    assert annotate_idx < node_idx, paths

    # Resolve through a real app: the GET annotate route must not be the
    # node-detail param route. A non-admin session is enough to confirm the
    # route binding (the admin gate fires inside the annotate handler, never
    # the node-detail one).
    app = _build_app()
    matched = [
        route
        for route in iter_routes(app.routes)
        if getattr(route, "path", None) == "/ui/topology/edges/annotate"
    ]
    assert matched, "annotate route not registered on the app"
    assert "GET" in matched[0].methods  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Annotate happy path + CSRF gate
# ---------------------------------------------------------------------------


def test_topology_ui_annotate_creates_curated_edge_with_note_and_evidence() -> None:
    """A CSRF-bearing admin POST creates a curated edge + re-renders inline."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="principal", name="sa-foo")
    _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/topology/edges",
            data={
                "from_name": "sa-foo",
                "from_kind": "principal",
                "kind": "authenticates-via",
                "to_name": "role-bar",
                "to_kind": "vault-role",
                "note": "asserted from INVENTORY.md",
                "evidence_url": "https://example.test/evidence",
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The created edge is re-rendered inline.
    assert 'data-test="edge-created"' in body
    assert "sa-foo" in body
    assert "role-bar" in body
    assert "authenticates-via" in body
    # The graph-refresh trigger fired so an open graph view re-pulls.
    assert response.headers.get("HX-Trigger") == "meho:topology-edge-changed"

    # note + evidence_url persisted on the curated row.
    edge_id = _fetch_curated_edge_id(_TENANT_A)
    assert edge_id is not None
    edge = _fetch_edge(edge_id)
    assert edge is not None
    assert edge.source == "curated"
    assert edge.kind == "authenticates-via"
    assert edge.properties.get("note") == "asserted from INVENTORY.md"
    assert edge.properties.get("evidence_url") == "https://example.test/evidence"


def test_topology_ui_annotate_without_csrf_token_is_403() -> None:
    """The same admin POST WITHOUT the CSRF token is rejected by the middleware."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="principal", name="sa-foo")
    _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        # No X-CSRF-Token header, and drop the cookie so the double-submit
        # cannot be satisfied from either source.
        client.cookies.delete(CSRF_COOKIE_NAME)
        response = client.post(
            "/ui/topology/edges",
            data={
                "from_name": "sa-foo",
                "kind": "authenticates-via",
                "to_name": "role-bar",
            },
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    # The CSRF middleware short-circuits before the edge is ever written.
    assert _fetch_curated_edge_id(_TENANT_A) is None


# ---------------------------------------------------------------------------
# Ambiguous-node re-render (acceptance criterion)
# ---------------------------------------------------------------------------


def test_topology_ui_annotate_ambiguous_name_rerenders_modal_with_kinds() -> None:
    """A bare name resolving to >1 kind re-renders the modal with candidates."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    # ``app`` exists as two kinds -> bare-name resolution is ambiguous.
    _seed_node(tenant_id=_TENANT_A, kind="target", name="app")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="app")
    _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/topology/edges",
            data={
                "from_name": "app",  # bare, no from_kind -> ambiguous
                "kind": "depends-on",
                "to_name": "role-bar",
                "to_kind": "vault-role",
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 409, response.text
    body = response.text
    # The modal re-renders (not a dead 409) with the candidate kinds so the
    # operator can re-submit with a disambiguating kind.
    assert 'id="topology-edge-modal"' in body
    assert 'data-test="ambiguous-kinds"' in body
    assert "target" in body
    assert "vm" in body
    # No edge was created.
    assert _fetch_curated_edge_id(_TENANT_A) is None


def test_topology_ui_annotate_ambiguous_resolves_with_kind_pin() -> None:
    """Re-submitting with the disambiguating ``from_kind`` then succeeds."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="target", name="app")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="app")
    _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/topology/edges",
            data={
                "from_name": "app",
                "from_kind": "vm",  # disambiguated
                "kind": "depends-on",
                "to_name": "role-bar",
                "to_kind": "vault-role",
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    assert 'data-test="edge-created"' in response.text
    assert _fetch_curated_edge_id(_TENANT_A) is not None


# ---------------------------------------------------------------------------
# Unannotate: auto_edge_deletion recoverable banner (acceptance criterion)
# ---------------------------------------------------------------------------


def test_topology_ui_annotate_delete_auto_edge_surfaces_recoverable_banner() -> None:
    """DELETE of a ``source='auto'`` edge surfaces the 409 recoverable banner."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    a = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    b = _seed_node(tenant_id=_TENANT_A, kind="host", name="host-1")
    auto_edge = _seed_edge(
        tenant_id=_TENANT_A,
        from_node_id=a,
        to_node_id=b,
        kind="runs-on",
        source="auto",
    )

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.request(
            "DELETE",
            f"/ui/topology/edges/{auto_edge}",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 409, response.text
    body = response.text
    # Recoverable typed banner, NOT a dead 500/empty error.
    assert 'data-error-kind="auto_edge_deletion"' in body
    assert "annotate over the auto edge first" in body.lower()
    assert "no-op" in body.lower()
    # The auto edge is untouched (it would resurrect on refresh anyway).
    assert _fetch_edge(auto_edge) is not None


def test_topology_ui_annotate_delete_curated_edge_succeeds() -> None:
    """DELETE of a curated edge removes it + fires the graph-refresh trigger."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    a = _seed_node(tenant_id=_TENANT_A, kind="principal", name="sa-foo")
    b = _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")
    curated = _seed_edge(
        tenant_id=_TENANT_A,
        from_node_id=a,
        to_node_id=b,
        kind="authenticates-via",
        source="curated",
    )

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.request(
            "DELETE",
            f"/ui/topology/edges/{curated}",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    assert 'data-test="edge-removed"' in response.text
    assert response.headers.get("HX-Trigger") == "meho:topology-edge-changed"
    # The curated row is gone.
    assert _fetch_edge(curated) is None


# ---------------------------------------------------------------------------
# RBAC: soft-hide + hard-403 (acceptance criterion — same-template trap)
# ---------------------------------------------------------------------------


def test_topology_ui_annotate_controls_hidden_for_operator_in_drawer() -> None:
    """An operator session does NOT see the annotate / remove controls."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    a = _seed_node(tenant_id=_TENANT_A, kind="principal", name="sa-foo")
    b = _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")
    _seed_edge(
        tenant_id=_TENANT_A,
        from_node_id=a,
        to_node_id=b,
        kind="authenticates-via",
        source="curated",
    )

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get(f"/ui/topology/node/{a}")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # Neither the annotate-open button nor any per-edge remove control.
    assert 'data-test="annotate-edge-open"' not in body
    assert 'data-test="edge-remove"' not in body


def test_topology_ui_annotate_controls_shown_for_tenant_admin_in_drawer() -> None:
    """A tenant_admin session DOES see the annotate + remove controls."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    a = _seed_node(tenant_id=_TENANT_A, kind="principal", name="sa-foo")
    b = _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")
    _seed_edge(
        tenant_id=_TENANT_A,
        from_node_id=a,
        to_node_id=b,
        kind="authenticates-via",
        source="curated",
    )

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get(f"/ui/topology/node/{a}")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-test="annotate-edge-open"' in body
    # The curated outgoing edge carries a Remove control.
    assert 'data-test="edge-remove"' in body


def test_topology_ui_annotate_forged_post_from_operator_is_403() -> None:
    """A forged annotate POST from a non-admin gets a hard server-side 403."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_node(tenant_id=_TENANT_A, kind="principal", name="sa-foo")
    _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")

    # Operator session carries a valid CSRF token (they forged past the
    # soft-hidden button) -- the hard gate is the authority.
    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.post(
            "/ui/topology/edges",
            data={
                "from_name": "sa-foo",
                "from_kind": "principal",
                "kind": "authenticates-via",
                "to_name": "role-bar",
                "to_kind": "vault-role",
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    # No edge was written despite a valid CSRF token.
    assert _fetch_curated_edge_id(_TENANT_A) is None


def test_topology_ui_annotate_forged_delete_from_operator_is_403() -> None:
    """A forged unannotate DELETE from a non-admin gets a hard 403."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    a = _seed_node(tenant_id=_TENANT_A, kind="principal", name="sa-foo")
    b = _seed_node(tenant_id=_TENANT_A, kind="vault-role", name="role-bar")
    curated = _seed_edge(
        tenant_id=_TENANT_A,
        from_node_id=a,
        to_node_id=b,
        kind="authenticates-via",
        source="curated",
    )

    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.request(
            "DELETE",
            f"/ui/topology/edges/{curated}",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    # The curated row survives the forged delete.
    assert _fetch_edge(curated) is not None


def test_topology_ui_annotate_modal_open_requires_tenant_admin() -> None:
    """The annotate-modal GET is hard-gated to tenant_admin (defense in depth)."""
    _seed_tenant_row(_TENANT_A, "tenant-a")

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get("/ui/topology/edges/annotate")
    finally:
        mock.stop()

    assert response.status_code == 403, response.text


def test_topology_ui_annotate_modal_open_renders_for_admin_with_csrf() -> None:
    """The annotate-modal GET renders the form + sets the meho_csrf cookie."""
    _seed_tenant_row(_TENANT_A, "tenant-a")

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/topology/edges/annotate")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="topology-edge-modal"' in body
    assert 'data-test="annotate-submit"' in body
    # The modal mints + sets a fresh meho_csrf cookie so the POST carries a
    # valid double-submit token.
    assert CSRF_COOKIE_NAME in response.cookies
    # The well-known edge kinds are surfaced as datalist suggestions.
    assert "authenticates-via" in body


def test_topology_ui_annotate_modal_kind_is_free_text_with_datalist() -> None:
    """The modal's kind field is a free-text input + datalist, not a closed select.

    T1 #2534 acceptance criterion: the vocabulary is open, so the UI
    must accept a novel kind (`resolves-to`, `same-as`, ...) while
    offering the well-known kinds as ``datalist`` suggestions. A
    ``<select name="kind">`` would silently re-close the vocabulary at
    the console boundary.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")

    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/topology/edges/annotate")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # Free-text input bound to the suggestions datalist...
    assert 'name="kind"' in body
    assert 'list="edge-kind-suggestions"' in body
    assert '<datalist id="edge-kind-suggestions">' in body
    # ...and no closed <select> for the kind field.
    assert '<select\n          name="kind"' not in body
    assert '<select name="kind"' not in body
    # Every well-known kind renders as a suggestion option.
    from meho_backplane.db.models import GraphEdgeKind

    for kind in GraphEdgeKind:
        assert f'<option value="{kind.value}"' in body


# ---------------------------------------------------------------------------
# Tenant isolation on the write surface
# ---------------------------------------------------------------------------


def test_topology_ui_annotate_delete_isolates_other_tenants_edge() -> None:
    """An admin cannot delete another tenant's curated edge (reads as 404)."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    _seed_tenant_row(_TENANT_B, "tenant-b")
    a = _seed_node(tenant_id=_TENANT_B, kind="principal", name="sa-foo")
    b = _seed_node(tenant_id=_TENANT_B, kind="vault-role", name="role-bar")
    other_edge = _seed_edge(
        tenant_id=_TENANT_B,
        from_node_id=a,
        to_node_id=b,
        kind="authenticates-via",
        source="curated",
    )

    # Admin in tenant A tries to delete tenant B's edge by id.
    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.request(
            "DELETE",
            f"/ui/topology/edges/{other_edge}",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    # Cross-tenant id is indistinguishable from missing -> 404 not-found
    # banner; the row survives.
    assert response.status_code == 404, response.text
    assert 'data-error-kind="edge_not_found"' in response.text
    assert _fetch_edge(other_edge) is not None
