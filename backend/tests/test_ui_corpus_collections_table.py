# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Docs Corpus admin Collections lifecycle table.

Initiative #1836 (G10.10 Doc Collections lifecycle UI), Task #1882 (T1).
Acceptance criteria on issue #1882:

* ``GET /ui/corpus/collections`` as a ``tenant_admin`` renders a row for a
  collection the operator does NOT hold ``meho-docs:<key>`` for -- proving
  the table is the FULL tenant registry, NOT the entitlement-filtered
  search catalogue (``test_table_lists_unentitled_rows``).
* ``POST /ui/corpus/collections/register`` with a valid body creates a row
  via the in-process service (DB row at ``status=provisioning``) and returns
  ``HX-Redirect: /ui/corpus/collections``; a duplicate ``collection_key``
  re-renders the modal with a 409 conflict message, not a stack trace
  (``test_register_creates_and_conflict``).
* A non-``tenant_admin`` operator gets the table WITHOUT the "Register
  collection" button, and a crafted register POST from a non-admin returns
  403 (``test_register_button_hidden_and_post_403_for_operator``).

Harness shape mirrors :mod:`backend.tests.test_ui_connectors_forms` (a real
Keycloak-minted access token so the ``resolve_role_probe`` /
``resolve_operator_or_403`` deps re-verify the role) combined with
:mod:`backend.tests.test_ui_corpus` (the doc-collection registry is seeded
into the autouse SQLite engine; the search-backend registry self-registers
``corpus-http`` at import time so the register POST's backend-type check
passes).
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import timedelta
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
from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.db.models import Tenant
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
) -> None:
    """Insert a doc collection row for the registry table."""

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
                    backend={"type": "corpus-http", "ref": {}},
                    status=status_value,
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
        keypair = _make_rsa_keypair("ui-corpus-collections-test-kid")
    return keypair, _public_jwks(keypair)


def _client_with_role(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
    capabilities: list[str] | None = None,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + respx mock + csrf token for the role-gated routes.

    The minted JWT drives BOTH the ``resolve_role_probe`` /
    ``resolve_operator_or_403`` role gate AND the corpus
    ``_resolve_operator`` tenant-scope seam, so one token wires the whole
    handler without a patch.
    """
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
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _form_headers(token: str) -> dict[str, str]:
    """Headers for an HTMX form submit -- CSRF + HX-Request."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_collections_table_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/corpus/collections`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/corpus/collections")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# GET /ui/corpus/collections -- table is the FULL registry, not entitlement-filtered
# ---------------------------------------------------------------------------


def test_table_lists_unentitled_rows() -> None:
    """The admin table lists a collection the operator is NOT entitled to search.

    Seed a tenant collection, give the admin NO ``meho-docs:vmware``
    capability, and assert the row still appears -- proving the table queries
    the FULL tenant registry, not the entitlement-filtered search catalogue.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    # A tenant-scoped collection the admin will manage but cannot personally
    # search (no ``meho-docs:vmware`` capability granted below).
    _seed_collection(collection_key="vmware", tenant_id=_TENANT_A)
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
        capabilities=[],  # deliberately NOT entitled to meho-docs:vmware
    )
    try:
        response = client.get("/ui/corpus/collections")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The row is rendered despite the missing entitlement -> NOT filtered.
    assert "vmware" in body
    assert 'data-collection-key="vmware"' in body
    # The Collections tab is active + the Search tab is reachable.
    assert 'href="/ui/corpus"' in body
    # The admin sees the Register affordance.
    assert "Register collection" in body
    assert 'hx-get="/ui/corpus/collections/register"' in body


def test_table_renders_status_pill_for_each_lifecycle_state() -> None:
    """The status column renders a pill for each lifecycle state."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="alpha", tenant_id=_TENANT_A, status_value="ready")
    _seed_collection(collection_key="beta", tenant_id=_TENANT_A, status_value="provisioning")
    _seed_collection(collection_key="gamma", tenant_id=_TENANT_A, status_value="disabled")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/corpus/collections")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert "badge-success" in body  # ready
    assert "badge-info" in body  # provisioning
    assert "badge-ghost" in body  # disabled


def test_table_htmx_fragment_is_tbody_not_full_page() -> None:
    """An HTMX-flagged GET returns the table fragment, not the full page."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", tenant_id=_TENANT_A)
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/corpus/collections", headers={"HX-Request": "true"})
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="corpus-collections-table-body"' in body
    assert "<!doctype html>" not in body.lower()


# ---------------------------------------------------------------------------
# POST /ui/corpus/collections/register -- create + conflict
# ---------------------------------------------------------------------------


def test_register_creates_and_conflict() -> None:
    """A valid register creates a provisioning row + HX-Redirect; a dup 409s legibly."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        create = client.post(
            "/ui/corpus/collections/register",
            data={
                "collection_key": "netapp",
                "vendor": "NetApp",
                "products": "ontap, e-series",
                "backend_type": "corpus-http",
                "backend_ref": '{"endpoint": "https://corpus/v1/search"}',
            },
            headers=_form_headers(csrf),
        )

        # Success: 204 + HX-Redirect back to the table.
        assert create.status_code == 204, create.text
        assert create.headers["HX-Redirect"] == "/ui/corpus/collections"

        # The DB row exists at status=provisioning under the tenant scope.
        row = _load_collection(_TENANT_A, "netapp")
        assert row is not None
        assert row.status == "provisioning"
        assert row.vendor == "NetApp"
        assert list(row.products) == ["ontap", "e-series"]
        assert row.backend == {
            "type": "corpus-http",
            "ref": {"endpoint": "https://corpus/v1/search"},
        }

        # A duplicate key re-renders the modal with a 409 conflict message.
        conflict = client.post(
            "/ui/corpus/collections/register",
            data={
                "collection_key": "netapp",
                "vendor": "NetApp",
                "backend_type": "corpus-http",
            },
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()

    assert conflict.status_code == 409, conflict.text
    body = conflict.text
    # The modal is re-rendered (a legible field error, not a stack trace).
    assert 'id="corpus-register-modal"' in body
    assert "already exists" in body
    assert 'data-error-for="collection_key"' in body
    assert "Traceback" not in body


def test_register_unknown_backend_type_renders_422() -> None:
    """An unregistered ``backend.type`` re-renders the modal with a 422 field error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/corpus/collections/register",
            data={
                "collection_key": "ghost",
                "vendor": "Ghost",
                "backend_type": "does-not-exist",
            },
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 422, response.text
    body = response.text
    assert 'id="corpus-register-modal"' in body
    assert 'data-error-for="backend_type"' in body
    # The row was not created.
    assert _load_collection(_TENANT_A, "ghost") is None


def test_register_malformed_backend_ref_json_renders_422() -> None:
    """Malformed ``backend.ref`` JSON re-renders the modal with a field error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/corpus/collections/register",
            data={
                "collection_key": "broken",
                "vendor": "Broken",
                "backend_type": "corpus-http",
                "backend_ref": "{not valid json",
            },
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 422, response.text
    body = response.text
    assert 'data-error-for="backend_ref"' in body
    assert _load_collection(_TENANT_A, "broken") is None


def test_register_rejected_without_csrf_token() -> None:
    """A register POST without the CSRF header is rejected 403 by the middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/corpus/collections/register",
            data={"collection_key": "x", "vendor": "X", "backend_type": "corpus-http"},
            headers={"HX-Request": "true"},  # no X-CSRF-Token
        )
    finally:
        mock.stop()

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# RBAC: non-admin sees no Register button + a crafted POST is 403'd
# ---------------------------------------------------------------------------


def test_register_button_hidden_and_post_403_for_operator() -> None:
    """A non-admin operator gets the table without the Register button + a 403 POST."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", tenant_id=_TENANT_A)
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        # The read-only table renders (the registry is not entitlement-scoped),
        # but the mutating "Register collection" affordance is hidden.
        table = client.get("/ui/corpus/collections")
        assert table.status_code == 200, table.text
        body = table.text
        assert 'data-collection-key="vmware"' in body  # row still visible
        assert "Register collection" not in body
        assert 'hx-get="/ui/corpus/collections/register"' not in body

        # A crafted POST from the non-admin hits the server-side 403 gate.
        post = client.post(
            "/ui/corpus/collections/register",
            data={
                "collection_key": "sneaky",
                "vendor": "Sneaky",
                "backend_type": "corpus-http",
            },
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()

    assert post.status_code == 403, post.text
    # The collection was not created.
    assert _load_collection(_TENANT_A, "sneaky") is None


def test_register_modal_renders_for_tenant_admin() -> None:
    """A tenant_admin GET renders the register modal with the field set."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/corpus/collections/register")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="corpus-register-modal"' in body
    assert 'hx-post="/ui/corpus/collections/register"' in body
    # The form carries its OWN hx-headers X-CSRF-Token (#1693 class).
    assert "X-CSRF-Token" in body
    assert 'name="collection_key"' in body
    assert 'name="vendor"' in body
    assert 'name="backend_type"' in body
    assert 'name="backend_ref"' in body
    # The CSRF cookie is set so the double-submit pair lines up.
    assert CSRF_COOKIE_NAME in response.cookies


def test_register_modal_rejects_operator_role_with_403() -> None:
    """An operator (non-admin) GET on the register modal is 403'd server-side."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get("/ui/corpus/collections/register")
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
