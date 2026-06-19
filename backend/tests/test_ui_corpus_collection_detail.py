# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Docs Corpus per-collection detail surface.

Initiative #1836 (G10.10 Doc Collections lifecycle UI), Task #1883 (T2).
Covers the per-collection detail page, the HTMX re-probe (readiness-card
swap), and the confirm-gated enable / disable verbs that T2 layers on top of
the T1 (#1882) admin Collections table.

Acceptance criteria on issue #1883:

* ``POST /ui/corpus/collections/{key}/probe`` as a ``tenant_admin`` against a
  reachable index re-renders the readiness card (swap target
  ``#collection-readiness-card``) with ``status=ready`` + the new
  ``doc_count``; an unreachable backend yields a 503 alert fragment while the
  DB row's ``status`` is left UNCHANGED (success-only write-back)
  (``test_reprobe_card_swap_and_503``).
* ``POST /ui/corpus/collections/{key}/disable`` transitions the row to
  ``disabled``; a second disable is the idempotent no-op (no error); a
  forbidden transition surfaces a legible ``409 invalid_collection_transition``
  alert (not a 500) (``test_disable_idempotent_and_409``).
* The detail page shows ``backend{type, ref}`` for a ``tenant_admin`` and
  HIDES the ``ref`` value for a plain ``operator`` (``test_backend_ref_admin_only``).
* A non-``tenant_admin`` crafted ``POST .../probe`` and ``.../disable`` both
  return 403 (the server-side ``resolve_operator_or_403`` gate), and the
  re-probe button carries ``hx-disabled-elt`` (``test_probe_disable_403_and_pending_state``).
* Route-ordering first-match-wins: the literal ``GET
  /ui/corpus/collections/register`` (the T1 literal) resolves to the register
  modal, NOT the detail handler with ``collection_key="register"``; the action
  POSTs resolve to their literal-suffixed handlers
  (``test_literal_routes_not_shadowed_by_param``).

Harness shape mirrors :mod:`backend.tests.test_ui_corpus_collections_table`
(a real Keycloak-minted access token so the ``resolve_role_probe`` /
``resolve_operator_or_403`` deps re-verify the role; the doc-collection
registry seeded into the autouse SQLite engine) plus a stub
:class:`~meho_backplane.docs_search.backends.base.SearchBackend` registered
under a test ``backend.type`` so the probe verb exercises the real in-process
service + transaction without an outbound HTTP call.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.db.models import Tenant
from meho_backplane.docs_search.backends import registry as registry_mod
from meho_backplane.docs_search.backends.base import BackendReadiness, SearchBackend
from meho_backplane.docs_search.backends.registry import all_backends, register_backend
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
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import AUDIENCE as _DEFAULT_AUDIENCE
from tests._oidc_jwt_helpers import ISSUER as _DEFAULT_ISSUER
from tests._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from tests._oidc_jwt_helpers import mint_token as _mint_token
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from tests._oidc_jwt_helpers import public_jwks as _public_jwks

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

_OP_OPERATOR = "op-operator"
_OP_ADMIN = "op-admin"

#: The stub backend type the test collections route to. A distinct type (not
#: ``corpus-http``) so the stub never collides with the import-time registry.
_STUB_BACKEND_TYPE = "test-stub-rag"
#: A sentinel value embedded in ``backend.ref`` so a test can grep the
#: response HTML for the ref's presence / absence (the admin-only assertion).
_REF_SENTINEL = "ref-sentinel-9f3a"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the table suite)."""
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


class _StubBackend(SearchBackend):
    """A stand-in search backend whose probe is configured per instance.

    Registered under :data:`_STUB_BACKEND_TYPE` so a seeded collection routes
    to it. ``probe`` returns the configured :class:`BackendReadiness` or
    raises :class:`CorpusUnavailable` (the 503 arm) without any network I/O —
    the test exercises the real in-process ``probe_collection`` service +
    route transaction, just with a deterministic backend.
    """

    backend_type = _STUB_BACKEND_TYPE

    def __init__(self, *, readiness: BackendReadiness | None, unavailable: bool) -> None:
        self._readiness = readiness
        self._unavailable = unavailable

    async def search(self, operator: Operator, query: str, **kwargs: Any) -> Any:
        raise NotImplementedError("stub backend is probe-only")

    async def probe(
        self,
        operator: Operator,
        *,
        backend_ref: Mapping[str, Any] | None = None,
    ) -> BackendReadiness:
        if self._unavailable:
            raise CorpusUnavailable("stub backend unreachable", status=503)
        assert self._readiness is not None
        return self._readiness


@pytest.fixture
def _restore_registry() -> Iterator[None]:
    """Snapshot the backend registry and restore it after the test.

    A test that registers the stub backend must not leak it into the
    process-wide registry the other tests (and the real seam) read.
    """
    snapshot = all_backends()
    yield
    registry_mod._BACKENDS.clear()
    registry_mod._BACKENDS.update(snapshot)


def _register_stub(*, readiness: BackendReadiness | None, unavailable: bool = False) -> None:
    """Register the stub backend under the test type, replacing any prior."""
    registry_mod._BACKENDS.pop(_STUB_BACKEND_TYPE, None)
    register_backend(
        _STUB_BACKEND_TYPE,
        _StubBackend(readiness=readiness, unavailable=unavailable),
    )


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_collection(
    *,
    collection_key: str,
    status_value: str = "ready",
    tenant_id: uuid.UUID | None = None,
    vendor: str = "VMware by Broadcom",
    backend_type: str = _STUB_BACKEND_TYPE,
    backend_ref: dict[str, Any] | None = None,
    doc_count: int | None = None,
) -> None:
    """Insert a doc collection row routing to the stub backend."""
    ref = backend_ref if backend_ref is not None else {"endpoint": _REF_SENTINEL}

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                DocCollectionORM(
                    tenant_id=tenant_id,
                    collection_key=collection_key,
                    vendor=vendor,
                    products=["vsphere"],
                    description=f"{vendor} docs.",
                    when_to_use="Vendor product questions.",
                    backend={"type": backend_type, "ref": ref},
                    status=status_value,
                    doc_count=doc_count,
                ),
            )

    asyncio.run(_do())


def _load_collection(tenant_id: uuid.UUID | None, collection_key: str) -> DocCollectionORM | None:
    """Read a collection row back for post-mutation assertions."""

    async def _do() -> DocCollectionORM | None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = select(DocCollectionORM).where(
                DocCollectionORM.collection_key == collection_key,
            )
            if tenant_id is None:
                stmt = stmt.where(DocCollectionORM.tenant_id.is_(None))
            else:
                stmt = stmt.where(DocCollectionORM.tenant_id == tenant_id)
            return (await session.execute(stmt)).scalar_one_or_none()

    return asyncio.run(_do())


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
        keypair = _make_rsa_keypair("ui-corpus-detail-test-kid")
    return keypair, _public_jwks(keypair)


def _client_with_role(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
    capabilities: list[str] | None = None,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + respx mock + csrf token for the role-gated routes."""
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=operator_sub,
        tenant_id=str(tenant_id),
        tenant_role=role.value,
        capabilities=capabilities,
    )
    session_id = _seed_session_sync(
        tenant_id=tenant_id,
        access_token=access_token,
        operator_sub=operator_sub,
    )
    # Drop any JWKS cached by a prior client in the same test -- each
    # ``_client_with_role`` mints a fresh RSA keypair, so a stale cache from an
    # earlier client (e.g. the admin client created before the operator client
    # in the same test body) would fail this token's signature verification.
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


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_detail_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/corpus/collections/{key}`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/corpus/collections/vmware")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_detail_unknown_key_404() -> None:
    """An unknown / cross-tenant collection key resolves to a 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/corpus/collections/does-not-exist")
    finally:
        mock.stop()
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# AC: re-probe card swap (reachable -> ready + doc_count) and 503 (unchanged)
# ---------------------------------------------------------------------------


def test_reprobe_card_swap_and_503(_restore_registry: None) -> None:
    """A reachable re-probe re-renders the readiness card; a 503 leaves the row."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(
        collection_key="vmware",
        tenant_id=_TENANT_A,
        status_value="provisioning",
        doc_count=None,
    )

    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        # --- reachable backend: status -> ready, new doc_count, card swapped ---
        _register_stub(
            readiness=BackendReadiness(
                reachable=True,
                index_built=True,
                doc_count=4242,
                last_ingested_at=datetime(2026, 6, 19, tzinfo=UTC),
                detail={"probe_method": "stub"},
            ),
        )
        ok = client.post(
            "/ui/corpus/collections/vmware/probe",
            headers=_form_headers(csrf),
        )
        assert ok.status_code == 200, ok.text
        body = ok.text
        # The swapped fragment is the readiness card (swap target id present).
        assert 'id="collection-readiness-card"' in body
        assert "badge-success" in body  # ready pill
        assert "4242" in body  # the new doc_count
        # The DB row was persisted to ready + the new count (success write-back).
        row = _load_collection(_TENANT_A, "vmware")
        assert row is not None
        assert row.status == "ready"
        assert row.doc_count == 4242

        # --- unreachable backend: 503 alert, row status LEFT UNCHANGED ---
        # Re-register the SAME backend type as an unavailable stub (no new
        # keypair / client so the JWKS cache from this client stays valid).
        _register_stub(readiness=None, unavailable=True)
        unavail = client.post(
            "/ui/corpus/collections/vmware/probe",
            headers=_form_headers(csrf),
        )
        assert unavail.status_code == 503, unavail.text
        alert_body = unavail.text
        # The 503 renders an alert fragment into the same card slot, not a card.
        assert 'id="collection-readiness-card"' in alert_body
        assert "Backend unavailable" in alert_body
        assert "Traceback" not in alert_body
        # Success-only write-back: the row's status is unchanged by the probe.
        row_after = _load_collection(_TENANT_A, "vmware")
        assert row_after is not None
        assert row_after.status == "ready"
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# AC: disable transitions to disabled; second disable is no-op; 409 legible
# ---------------------------------------------------------------------------


def test_disable_idempotent_and_409(_restore_registry: None) -> None:
    """Disable -> disabled; second disable is a no-op; an enable on a live row 409s."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", tenant_id=_TENANT_A, status_value="ready")
    _register_stub(readiness=BackendReadiness(reachable=True, index_built=True))

    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        # First disable: ready -> disabled, 204 + HX-Redirect.
        first = client.post(
            "/ui/corpus/collections/vmware/disable",
            headers=_form_headers(csrf),
        )
        assert first.status_code == 204, first.text
        assert first.headers["HX-Redirect"] == "/ui/corpus/collections/vmware"
        row = _load_collection(_TENANT_A, "vmware")
        assert row is not None
        assert row.status == "disabled"

        # Second disable: idempotent no-op -- still 204, no error.
        second = client.post(
            "/ui/corpus/collections/vmware/disable",
            headers=_form_headers(csrf),
        )
        assert second.status_code == 204, second.text
        assert _load_collection(_TENANT_A, "vmware").status == "disabled"

        # A forbidden transition: enable is only legal from disabled
        # (-> provisioning); enabling a *ready* row is forbidden. Seed a second
        # ready row and POST enable -> a legible 409, NOT a 500.
        _seed_collection(collection_key="nsx", tenant_id=_TENANT_A, status_value="ready")
        forbidden = client.post(
            "/ui/corpus/collections/nsx/enable",
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()

    assert forbidden.status_code == 409, forbidden.text
    body = forbidden.text
    assert "invalid_collection_transition" in body
    assert "Traceback" not in body
    # The forbidden enable did not mutate the row.
    assert _load_collection(_TENANT_A, "nsx").status == "ready"


# ---------------------------------------------------------------------------
# AC: backend{type, ref} shown for tenant_admin, hidden for plain operator
# ---------------------------------------------------------------------------


def test_backend_ref_admin_only() -> None:
    """The detail page shows backend ref for an admin and hides it for an operator."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(
        collection_key="vmware",
        tenant_id=_TENANT_A,
        backend_ref={"endpoint": _REF_SENTINEL},
    )

    # --- tenant_admin: the backend card + the ref sentinel are present ---
    admin_client, admin_mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        admin_resp = admin_client.get("/ui/corpus/collections/vmware")
    finally:
        admin_mock.stop()
    assert admin_resp.status_code == 200, admin_resp.text
    admin_body = admin_resp.text
    assert 'id="collection-backend-card"' in admin_body
    assert _STUB_BACKEND_TYPE in admin_body  # backend type rendered
    assert _REF_SENTINEL in admin_body  # the ref VALUE rendered for admin

    # --- plain operator: NO backend card, and the ref VALUE is absent ---
    op_client, op_mock, _csrf2 = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        op_resp = op_client.get("/ui/corpus/collections/vmware")
    finally:
        op_mock.stop()
    assert op_resp.status_code == 200, op_resp.text
    op_body = op_resp.text
    assert 'id="collection-backend-card"' not in op_body
    # The server-side-only ref value must NOT leak to a plain operator.
    assert _REF_SENTINEL not in op_body


# ---------------------------------------------------------------------------
# AC: non-admin probe/disable 403; re-probe button carries hx-disabled-elt
# ---------------------------------------------------------------------------


def test_probe_disable_403_and_pending_state(_restore_registry: None) -> None:
    """A non-admin probe/disable 403s; the admin re-probe button has hx-disabled-elt."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", tenant_id=_TENANT_A, status_value="ready")
    _register_stub(readiness=BackendReadiness(reachable=True, index_built=True))

    # --- a crafted non-admin probe + disable both hit the server-side 403 ---
    op_client, op_mock, op_csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        probe_403 = op_client.post(
            "/ui/corpus/collections/vmware/probe",
            headers=_form_headers(op_csrf),
        )
        disable_403 = op_client.post(
            "/ui/corpus/collections/vmware/disable",
            headers=_form_headers(op_csrf),
        )
    finally:
        op_mock.stop()
    assert probe_403.status_code == 403, probe_403.text
    assert disable_403.status_code == 403, disable_403.text
    # The forbidden disable did not mutate the row.
    assert _load_collection(_TENANT_A, "vmware").status == "ready"

    # --- the admin detail page's re-probe button carries the pending state ---
    admin_client, admin_mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        detail = admin_client.get("/ui/corpus/collections/vmware")
    finally:
        admin_mock.stop()
    assert detail.status_code == 200, detail.text
    body = detail.text
    # A probe can serialize behind a rebuild, so the button MUST disable itself
    # for the request duration + show a pending indicator.
    assert "hx-disabled-elt" in body
    assert "hx-indicator" in body
    assert 'hx-post="/ui/corpus/collections/vmware/probe"' in body


# ---------------------------------------------------------------------------
# AC: route ordering -- literal routes are not shadowed by {collection_key}
# ---------------------------------------------------------------------------


def test_literal_routes_not_shadowed_by_param(_restore_registry: None) -> None:
    """The literal /register + the action sub-routes are not bound as a key."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", tenant_id=_TENANT_A, status_value="ready")
    _register_stub(readiness=BackendReadiness(reachable=True, index_built=True))

    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        # The T1 literal: GET /register resolves to the register MODAL, never
        # to the detail handler with collection_key="register". The modal does
        # not need a row to exist; the detail handler would 404 on "register".
        register = client.get("/ui/corpus/collections/register")
        assert register.status_code == 200, register.text
        assert 'id="corpus-register-modal"' in register.text
        # If "register" had bound as {collection_key}, the detail page (not the
        # modal) would render -- assert the detail page markers are absent.
        assert 'id="collection-readiness-card"' not in register.text

        # The action POSTs resolve to their literal-suffixed handlers, not the
        # bare {collection_key} GET (which is GET-only anyway): probe swaps the
        # readiness card; disable swaps the modal-form/redirect.
        probe = client.post(
            "/ui/corpus/collections/vmware/probe",
            headers=_form_headers(csrf),
        )
        assert probe.status_code == 200, probe.text
        assert 'id="collection-readiness-card"' in probe.text

        disable = client.post(
            "/ui/corpus/collections/vmware/disable",
            headers=_form_headers(csrf),
        )
        assert disable.status_code == 204, disable.text
        assert disable.headers["HX-Redirect"] == "/ui/corpus/collections/vmware"

        # And the bare {collection_key} GET still resolves the detail page for a
        # real key (proving the param route is reachable, just registered last).
        detail = client.get("/ui/corpus/collections/vmware")
        assert detail.status_code == 200, detail.text
        assert 'id="collection-readiness-card"' in detail.text
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# Detail page: table row links to the detail page (T1 -> T2 wiring)
# ---------------------------------------------------------------------------


def test_table_row_links_to_detail() -> None:
    """The T1 Collections table row links to the per-collection detail page."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", tenant_id=_TENANT_A)
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        table = client.get("/ui/corpus/collections")
    finally:
        mock.stop()
    assert table.status_code == 200, table.text
    assert 'href="/ui/corpus/collections/vmware"' in table.text


# ---------------------------------------------------------------------------
# Empty state repoint: unprovisioned arm links the in-console register flow
# ---------------------------------------------------------------------------


def test_empty_state_links_register_flow_for_admin() -> None:
    """An unprovisioned tenant_admin sees a 'Register a collection' CTA."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # No collections seeded -> genuinely unprovisioned.
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        page = client.get("/ui/corpus")
    finally:
        mock.stop()
    assert page.status_code == 200, page.text
    body = page.text
    assert "Register a collection" in body
    assert 'href="/ui/corpus/collections"' in body
    # The old dead-end copy is gone from the unprovisioned arm.
    assert "ask an administrator to register and entitle a collection" not in body
