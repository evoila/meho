# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Connector Registry list surface.

Initiative #1839 (G10.13 Connector ingest & curation registry UI),
Task #1885 (T1). Covers the ``GET /ui/connectors/registry`` role-scoped
list (distinct from the ``/ui/connectors`` targets list) and the
confirm-gated per-row enable / disable / enable-reads / delete verbs.

Acceptance criteria on issue #1885:

* ``GET /ui/connectors/registry`` returns 200 for an authenticated
  operator and renders one row per item from ``list_ingested_connectors``;
  a seeded connector's ``connector_id`` + ``state`` pill appear and a
  cross-tenant row does NOT
  (``test_registry_list_renders_rows``).
* The literal ``/ui/connectors/registry`` route is registered BEFORE
  ``/ui/connectors/{name}`` in the built app router (first-match-wins)
  (``test_registry_route_registered_before_detail``).
* The four per-row verbs reject a non-tenant_admin operator with 403; the
  action buttons are absent for a non-admin (soft-hide) and present for a
  tenant_admin (``test_per_row_verbs_rbac``).
* An ``enable-reads`` against a connector that maps to both a tenant and a
  built-in row renders the ``409 connector_scope_ambiguous`` ``candidates``
  panel inline (not a 500); an ``InvalidStateTransitionError`` 409 renders
  an inline panel
  (``test_enable_reads_ambiguous_scope_panel`` /
  ``test_invalid_state_transition_panel``).
* Every write verb requires the CSRF double-submit token (a POST without
  ``X-CSRF-Token`` is rejected by ``CSRFMiddleware``)
  (``test_write_verbs_require_csrf``).

Harness shape mirrors :mod:`backend.tests.test_ui_corpus_collection_detail`
(a real Keycloak-minted access token so ``resolve_role_probe`` /
``resolve_operator_or_403`` re-verify the role; the connector rows seeded
into the autouse SQLite engine via ``OperationGroup`` /
``EndpointDescriptor``, the same triple
:mod:`backend.tests.test_api_v1_connectors_enable_reads` uses so the rows
resolve through the dispatcher).
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.requests import Request

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, Tenant
from meho_backplane.operations.ingest import InvalidStateTransitionError
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
from meho_backplane.ui.routes.connectors import build_router as build_connectors_router
from meho_backplane.ui.routes.connectors.registry_actions import (
    _panel_from_http_exception,
)
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import AUDIENCE as _DEFAULT_AUDIENCE
from tests._oidc_jwt_helpers import ISSUER as _DEFAULT_ISSUER
from tests._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from tests._oidc_jwt_helpers import mint_token as _mint_token
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from tests._oidc_jwt_helpers import public_jwks as _public_jwks

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_OP_OPERATOR = "op-operator"
_OP_ADMIN = "op-admin"

#: The connector triple the enable-reads REST suite uses; it resolves
#: through the dispatcher so the seeded rows surface in the listing.
_PRODUCT = "vmware"
_VERSION = "9.0"
_IMPL_ID = "vmware-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the corpus suite)."""
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


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_connector(
    *,
    tenant_id: uuid.UUID | None,
    product: str = _PRODUCT,
    version: str = _VERSION,
    impl_id: str = _IMPL_ID,
    review_status: str = "staged",
    methods: tuple[str, ...] = ("GET", "POST"),
    enabled: bool = False,
) -> None:
    """Seed one group + one ingested op per HTTP verb under *tenant_id*.

    ``tenant_id=None`` seeds a built-in / global connector. The triple
    resolves through the dispatcher (a registered v2 connector class), so
    the row surfaces in ``list_ingested_connectors``. ``enabled`` sets the
    per-op ``is_enabled`` flag on every seeded op (drives the row's
    ``enabled_operation_count`` -- a signature the cross-tenant test reads).
    """

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            group_id = uuid.uuid4()
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    group_key="resources",
                    name="Resources",
                    when_to_use="Use for resource ops.",
                    review_status=review_status,
                ),
            )
            for method in methods:
                session.add(
                    EndpointDescriptor(
                        tenant_id=tenant_id,
                        product=product,
                        version=version,
                        impl_id=impl_id,
                        op_id=f"{method}:/api/v1/resource",
                        source_kind="ingested",
                        method=method,
                        path="/api/v1/resource",
                        summary=f"{method} resource",
                        group_id=group_id,
                        tags=["test"],
                        parameter_schema={"type": "object"},
                        safety_level="safe",
                        requires_approval=False,
                        is_enabled=enabled,
                    ),
                )

    asyncio.run(_do())


def _build_app() -> FastAPI:
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


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str,
    operator_sub: str,
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
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    return asyncio.run(_do())


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-connectors-registry-test-kid")
    return keypair, _public_jwks(keypair)


def _client_with_role(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + respx mock + csrf token for the role-gated routes."""
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
    clear_jwks_cache()
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _form_headers(token: str) -> dict[str, str]:
    """Headers for an HTMX state-changing request -- CSRF + HX-Request."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


def _bare_request() -> Request:
    """Build a minimal :class:`Request` for a standalone fragment render.

    The error-panel fragment is a context-processor-safe standalone
    template (no ``base.html`` extension, no ``url_for``); the chassis
    context processor reads ``request.state`` via ``getattr`` defaults, so
    an empty ``state`` scope renders cleanly without a live session.
    """
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/ui/connectors/registry/x/enable",
            "headers": [],
            "query_string": b"",
            "state": {},
        }
    )


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_registry_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/connectors/registry`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/connectors/registry")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# AC: list renders one row per item; seeded row present, cross-tenant absent
# ---------------------------------------------------------------------------


def test_registry_list_renders_rows() -> None:
    """The list renders the seeded connector_id + state pill; cross-tenant absent.

    Tenant A and tenant B both seed the same resolvable triple
    (``vmware-rest-9.0``) under their own tenant; tenant B's rows carry a
    distinctive enabled-op signature (``2/2`` ops enabled). Tenant A must
    see its OWN row (scope chip "tenant", ``ingested``, 0 ops enabled) and
    NOT tenant B's signature -- the service's SQL-level tenant filter never
    aggregates the cross-tenant rows into tenant A's row.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    # Tenant A's own connector -- staged, no ops enabled.
    _seed_connector(tenant_id=_TENANT_A, review_status="staged")
    # Tenant B's connector under the SAME triple, with BOTH ops enabled --
    # a "2/2 ops enabled" signature tenant A must never see (cross-tenant
    # isolation: tenant B's rows are filtered out of tenant A's aggregation).
    _seed_connector(
        tenant_id=_TENANT_B,
        review_status="enabled",
        methods=("GET", "HEAD"),
        enabled=True,
    )

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get("/ui/connectors/registry")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The seeded tenant-A connector + its state pill render.
    assert _CONNECTOR_ID in body
    assert f'data-connector-id="{_CONNECTOR_ID}"' in body
    assert "ingested" in body  # the state pill
    # Tenant A's own row is tenant-scoped with no ops enabled.
    assert "ops: 0 / 2" in body
    # Tenant B's distinctive "2/2 ops enabled" signature must NOT leak into
    # tenant A's view -- the row tenant A sees carries only its own counts.
    assert "ops: 2 / 2" not in body


def test_registry_list_status_filter_all_sentinel_renders() -> None:
    """The ``?status=all`` sentinel renders 200 (never 422); a bad value 422s."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A)

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        # The real "all" sentinel renders the full set (the no-narrowing case).
        ok = client.get("/ui/connectors/registry?status=all", headers={"HX-Request": "true"})
        assert ok.status_code == 200, ok.text
        assert _CONNECTOR_ID in ok.text
        # A narrowing filter that excludes the staged row hides it.
        enabled_only = client.get(
            "/ui/connectors/registry?status=enabled",
            headers={"HX-Request": "true"},
        )
        assert enabled_only.status_code == 200, enabled_only.text
        assert _CONNECTOR_ID not in enabled_only.text
        # An out-of-range status 422s at the enum boundary.
        bad = client.get("/ui/connectors/registry?status=bogus")
        assert bad.status_code == 422, bad.text
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# AC: route ordering -- registry registered before {name} detail
# ---------------------------------------------------------------------------


def test_registry_route_registered_before_detail() -> None:
    """First-match-wins: ``/ui/connectors/registry`` precedes ``/ui/connectors/{name}``."""
    router = build_connectors_router()
    paths = [route.path for route in router.routes]

    registry_index = paths.index("/ui/connectors/registry")
    # The detail GET route -- distinguished from the PATCH on the same path.
    detail_index = next(
        i
        for i, route in enumerate(router.routes)
        if route.path == "/ui/connectors/{name}" and "GET" in (route.methods or set())
    )
    assert registry_index < detail_index, (
        "the literal /ui/connectors/registry route must register before the "
        "parametrised /ui/connectors/{name} detail route (first-match-wins)"
    )
    # Each literal-suffixed action route also precedes the bare detail GET.
    for action in ("enable", "enable-reads", "disable"):
        action_index = paths.index(f"/ui/connectors/registry/{{connector_id}}/{action}")
        assert action_index < detail_index


# ---------------------------------------------------------------------------
# AC: per-row verbs RBAC -- 403 for non-admin; buttons soft-hidden/shown
# ---------------------------------------------------------------------------


def test_per_row_verbs_rbac() -> None:
    """Non-admin: 403 on every verb + no action buttons. Admin: buttons present."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A)

    # --- a plain operator: every write verb 403s, and the buttons are hidden ---
    op_client, op_mock, op_csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        list_resp = op_client.get("/ui/connectors/registry")
        enable_403 = op_client.post(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/enable",
            headers=_form_headers(op_csrf),
        )
        enable_reads_403 = op_client.post(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/enable-reads",
            headers=_form_headers(op_csrf),
        )
        disable_403 = op_client.post(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/disable",
            headers=_form_headers(op_csrf),
        )
        delete_403 = op_client.delete(
            f"/ui/connectors/registry/{_CONNECTOR_ID}",
            headers=_form_headers(op_csrf),
        )
    finally:
        op_mock.stop()

    assert list_resp.status_code == 200, list_resp.text
    # Soft-hide: the per-row action buttons are absent for a plain operator.
    assert "data-row-actions" not in list_resp.text
    assert f"/ui/connectors/registry/{_CONNECTOR_ID}/enable" not in list_resp.text
    for resp in (enable_403, enable_reads_403, disable_403, delete_403):
        assert resp.status_code == 403, resp.text

    # --- a tenant_admin: the action buttons are present ---
    admin_client, admin_mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        admin_list = admin_client.get("/ui/connectors/registry")
    finally:
        admin_mock.stop()
    assert admin_list.status_code == 200, admin_list.text
    body = admin_list.text
    assert "data-row-actions" in body
    assert f'hx-get="/ui/connectors/registry/{_CONNECTOR_ID}/enable"' in body
    assert f'hx-get="/ui/connectors/registry/{_CONNECTOR_ID}/delete"' in body


def test_confirm_modal_loads_for_admin() -> None:
    """The admin enable modal loads, names the blast radius + double-fire guard."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A)

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        enable_modal = client.get(f"/ui/connectors/registry/{_CONNECTOR_ID}/enable")
        delete_modal = client.get(f"/ui/connectors/registry/{_CONNECTOR_ID}/delete")
    finally:
        mock.stop()

    assert enable_modal.status_code == 200, enable_modal.text
    enable_body = enable_modal.text
    assert 'class="modal"' in enable_body
    assert "loosens the safety boundary" in enable_body
    assert "hx-disabled-elt" in enable_body
    assert f'hx-post="/ui/connectors/registry/{_CONNECTOR_ID}/enable"' in enable_body

    # The delete modal is type-to-confirm and surfaces the enabled-ops advisory.
    assert delete_modal.status_code == 200, delete_modal.text
    delete_body = delete_modal.text
    assert "Type" in delete_body
    assert f'data-expected="{_CONNECTOR_ID}"' in delete_body
    assert f'hx-delete="/ui/connectors/registry/{_CONNECTOR_ID}"' in delete_body


# ---------------------------------------------------------------------------
# AC: enable verbs succeed; row re-renders with the new state
# ---------------------------------------------------------------------------


def test_enable_verb_succeeds_and_swaps_row() -> None:
    """A tenant_admin enable transitions the groups + returns the OOB row swap."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A, review_status="staged")

    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        resp = client.post(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/enable",
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()

    assert resp.status_code == 200, resp.text
    body = resp.text
    # The OOB row swap carries the row id and the now-enabled state.
    assert f'id="registry-row-{_CONNECTOR_ID}"' in body
    assert 'hx-swap-oob="true"' in body
    assert "enabled" in body


def test_enable_reads_verb_succeeds() -> None:
    """A tenant_admin enable-reads flips read-class ops + returns the OOB row swap."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A, methods=("GET", "POST"))

    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        resp = client.post(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/enable-reads",
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert f'id="registry-row-{_CONNECTOR_ID}"' in body
    # One of two ingested ops (the GET) flipped to enabled.
    assert "ops: 1 / 2" in body


def test_delete_verb_removes_row() -> None:
    """A tenant_admin delete removes the ingested rows + returns the empty OOB stub.

    ``vmware-rest`` is a registered v2 connector class, so after the DB
    rows are deleted the triple reappears as a class-side ``registered``
    (zero-count) STUB -- not the deleted ``ingested`` row. The test
    asserts the delete's OOB row-removal swap is correct, and that the
    ingested data is genuinely gone (the relisted row, if present, is the
    ``registered`` stub with 0 / 0 ops, not the deleted ``ingested`` row).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A)

    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        resp = client.delete(
            f"/ui/connectors/registry/{_CONNECTOR_ID}",
            headers=_form_headers(csrf),
        )
        relist = client.get("/ui/connectors/registry")
    finally:
        mock.stop()

    assert resp.status_code == 200, resp.text
    # The OOB stub carries the row id (to remove the old row) and is marked oob.
    assert f'id="registry-row-{_CONNECTOR_ID}"' in resp.text
    assert 'hx-swap-oob="true"' in resp.text
    assert relist.status_code == 200, relist.text
    # The ingested data is gone: the seeded ``ops: 0 / 2`` signature no
    # longer renders (the row, if present, is the zero-count class stub).
    assert "ops: 0 / 2" not in relist.text


# ---------------------------------------------------------------------------
# AC: 409 connector_scope_ambiguous panel; invalid-state-transition panel
# ---------------------------------------------------------------------------


def test_enable_reads_ambiguous_scope_panel() -> None:
    """A label mapping to both a tenant + built-in row renders the 409 candidates panel."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # Seed BOTH a tenant-A row and a built-in row for the same triple so the
    # label resolves ambiguously (the #1801 scope-ambiguous case).
    _seed_connector(tenant_id=_TENANT_A)
    _seed_connector(tenant_id=None)

    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        resp = client.post(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/enable-reads",
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()

    assert resp.status_code == 409, resp.text
    body = resp.text
    # The inline panel, not a 500 / stack trace.
    assert "data-registry-error" in body
    assert "ambiguous" in body.lower()
    assert "Traceback" not in body
    # The candidates list enumerates the built-in + tenant rows.
    assert "built-in" in body
    assert f"tenant_id={_TENANT_A}" in body


def test_invalid_state_transition_panel() -> None:
    """An InvalidStateTransitionError 409 maps to a legible inline panel, not a 500.

    ``enable_connector`` / ``disable_connector`` raise
    :class:`InvalidStateTransitionError` only on a group in a state outside
    the bounded ``staged`` / ``enabled`` / ``disabled`` enum -- not
    reachable from a clean DB seed (every valid state is either the target
    or an allowed source). The panel-rendering branch is still real handler
    code, so it is exercised by driving the mapper with the exact
    ``HTTPException`` shape the REST handlers raise
    (``detail=str(InvalidStateTransitionError(...))`` at 409).
    """
    exc = HTTPException(
        status_code=409,
        detail=str(
            InvalidStateTransitionError(
                current_status="enabled",
                requested_status="enabled",
                group_key="resources",
            )
        ),
    )
    request = _bare_request()
    response = _panel_from_http_exception(request, exc, connector_id=_CONNECTOR_ID)

    assert response.status_code == 409
    body = response.body.decode()
    assert "data-registry-error" in body
    assert "transition" in body.lower()
    assert "Traceback" not in body


# ---------------------------------------------------------------------------
# AC: every write verb requires the CSRF double-submit token
# ---------------------------------------------------------------------------


def test_write_verbs_require_csrf() -> None:
    """A POST/DELETE without ``X-CSRF-Token`` is rejected by CSRFMiddleware (403)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A)

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        # No X-CSRF-Token header + no csrf_token form field -> middleware 403
        # before the route (and its RBAC gate) even runs.
        enable_no_csrf = client.post(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/enable",
            headers={"HX-Request": "true"},
        )
        delete_no_csrf = client.delete(
            f"/ui/connectors/registry/{_CONNECTOR_ID}",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert enable_no_csrf.status_code == 403, enable_no_csrf.text
    assert delete_no_csrf.status_code == 403, delete_no_csrf.text
    # The CSRF rejection is the middleware's, not the route's RBAC 403.
    assert enable_no_csrf.headers.get("x-csrf-rejection-reason") is not None


# ---------------------------------------------------------------------------
# AC (#1980): the list shows + filters on connector kind
# ---------------------------------------------------------------------------


def test_registry_list_renders_kind_chip() -> None:
    """The list renders the per-row authoring-mode kind chip (#1980).

    ``vmware-rest`` is a hand-coded v2 connector class, so the resolver maps
    it to ``kind="typed"`` (dispatchable). The row must carry the typed chip
    + the Kind column header.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A)

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get("/ui/connectors/registry")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The Kind column header + the typed chip for the hand-coded connector.
    assert "<th>Kind</th>" in body
    assert 'badge-primary badge-sm" title="Hand-coded connector class' in body
    # The kind filter <select> is present with the all-sentinel option.
    assert 'name="kind"' in body
    assert 'value="all"' in body
    assert 'value="profiled-but-unreviewed"' in body


def test_registry_list_kind_filter_narrows() -> None:
    """``?kind=`` narrows the rows in-process; a bad value 422s at the enum.

    The seeded ``vmware-rest`` resolves to ``kind="typed"``, so ``?kind=typed``
    keeps it and ``?kind=ingested-shim`` (or ``?kind=profiled``) hides it. An
    out-of-range ``?kind=`` 422s at the ``_KindFilter`` enum boundary (the
    filter contract), exactly like ``?status=``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A)

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        # The all-sentinel keeps the typed row.
        all_kinds = client.get("/ui/connectors/registry?kind=all", headers={"HX-Request": "true"})
        # A matching kind keeps it.
        typed_only = client.get(
            "/ui/connectors/registry?kind=typed", headers={"HX-Request": "true"}
        )
        # A non-matching kind drops it.
        shim_only = client.get(
            "/ui/connectors/registry?kind=ingested-shim",
            headers={"HX-Request": "true"},
        )
        # An out-of-range kind 422s at the enum boundary.
        bad = client.get("/ui/connectors/registry?kind=bogus")
    finally:
        mock.stop()

    assert all_kinds.status_code == 200, all_kinds.text
    assert _CONNECTOR_ID in all_kinds.text
    assert typed_only.status_code == 200, typed_only.text
    assert _CONNECTOR_ID in typed_only.text
    assert shim_only.status_code == 200, shim_only.text
    assert _CONNECTOR_ID not in shim_only.text
    assert bad.status_code == 422, bad.text


def test_row_context_carries_kind_and_dispatchable() -> None:
    """``_row_context`` flattens ``ConnectorListItem.kind`` / ``dispatchable`` (#1980)."""
    from meho_backplane.operations.ingest import ConnectorListItem
    from meho_backplane.ui.routes.connectors.registry_list import _row_context

    item = ConnectorListItem(
        connector_id="acme-rest-1.0",
        product="acme",
        version="1.0",
        impl_id="acme-rest",
        tenant_id=None,
        group_count=1,
        staged_group_count=1,
        enabled_group_count=0,
        disabled_group_count=0,
        operation_count=3,
        enabled_operation_count=0,
        kind="profiled-but-unreviewed",
        dispatchable=False,
    )
    ctx = _row_context(item)
    assert ctx["kind"] == "profiled-but-unreviewed"
    assert ctx["dispatchable"] is False


def test_registry_row_renders_each_kind_chip() -> None:
    """Every ``kind`` value renders its distinct chip in the row fragment (#1980).

    Exercises the four template branches directly via ``render_registry_table``
    (no full pipeline seed needed for the non-``typed`` kinds): typed / profiled
    are dispatchable, ingested-shim is the bare dead end, profiled-but-unreviewed
    badges the not-yet-cleared profile-backed sub-state.
    """
    from meho_backplane.operations.ingest import ConnectorListItem
    from meho_backplane.ui.routes.connectors.registry_list import (
        _KindFilter,
        _StatusFilter,
        render_registry_table,
    )

    def _item(kind: str, dispatchable: bool) -> ConnectorListItem:
        return ConnectorListItem(
            connector_id=f"{kind}-rest-1.0",
            product=kind,
            version="1.0",
            impl_id=f"{kind}-rest",
            tenant_id=None,
            group_count=1,
            staged_group_count=1,
            enabled_group_count=0,
            disabled_group_count=0,
            operation_count=1,
            enabled_operation_count=0,
            kind=kind,  # type: ignore[arg-type]
            dispatchable=dispatchable,
        )

    items = [
        _item("typed", True),
        _item("profiled", True),
        _item("ingested-shim", False),
        _item("profiled-but-unreviewed", False),
    ]

    session_ctx = SimpleNamespace(session_id=uuid.uuid4())

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/ui/connectors/registry",
            "headers": [],
            "query_string": b"",
            "state": {},
        }
    )
    response = render_registry_table(
        request,
        items=items,
        status_filter=_StatusFilter.ALL,
        product_filter=None,
        kind_filter=_KindFilter.ALL,
        session_ctx=session_ctx,  # type: ignore[arg-type]
        is_tenant_admin=False,
    )
    body = response.body.decode()
    assert "badge-primary badge-sm" in body  # typed
    assert "badge-info badge-sm" in body  # profiled
    assert "ingested shim" in body  # ingested-shim
    assert "profiled (staged)" in body  # profiled-but-unreviewed
