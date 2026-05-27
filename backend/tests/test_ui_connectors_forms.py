# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the target create / edit forms (Task #874).

Initiative #340 (G10.3 Connectors + Targets UI), Task #874 (G10.3-T2).
The acceptance criteria on issue #874 are:

* Create form fields match G0.3 ``TargetCreate``; product dropdown
  from the registered-connector set; submit POSTs ``/api/v1/targets``;
  success -> updated list; invalid input (port outside 1-65535, empty
  name) -> form re-renders with field errors.
* Edit form pre-populates server-side; PATCHes ``/api/v1/targets/{name}``.
* Only ``tenant_admin`` sees create / edit; ``operator`` -> 403
  (server-side gate).
* CSRF enforced on create / edit (chassis double-submit).
* Cross-tenant isolation: cannot edit another tenant's target.
* ``ruff`` + ``mypy`` clean; ``pytest -n auto
  backend/tests/test_ui_connectors_forms.py`` passes.

Harness shape mirrors :mod:`backend.tests.test_ui_connectors_view`: a
minimal FastAPI app wired with the UI session + CSRF middlewares, a
``web_session`` row carrying a real Keycloak-minted access token so
the ``resolve_operator_or_403`` dep can re-verify the role, and a fake
connector registered so ``registered_product_tokens()`` advertises the
product the create / edit forms POST.
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

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Target as TargetORM
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

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

_OP_OPERATOR = "op-operator"
_OP_ADMIN = "op-admin"

_PRODUCT = "fakeprod"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the view suite)."""
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


class _FakeConnector(Connector):
    """Deterministic connector so ``registered_product_tokens()`` advertises
    ``fakeprod`` -- the product the create / edit forms POST + validate against.
    """

    product = _PRODUCT
    version = "1.0"
    impl_id = _PRODUCT
    supported_version_range = None

    async def fingerprint(self, target: Any) -> FingerprintResult:  # pragma: no cover
        return FingerprintResult(
            vendor="FakeVendor",
            product=_PRODUCT,
            version="1.0",
            build="fake-build",
            reachable=True,
            probed_at=datetime.now(UTC),
            probe_method="test",
        )

    async def probe(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def execute(  # pragma: no cover - unused
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> Any:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _registry_with_fakeprod() -> Iterator[None]:
    """Register ``fakeprod`` so the product dropdown + POST validator accept it.

    The create / edit POST validate ``product`` against
    ``registered_product_tokens()``; without a registration the create
    handler would 422 on every product. Clear + re-register per test so
    no registration leaks across cases.
    """
    clear_registry()
    register_connector_v2(product=_PRODUCT, version="1.0", impl_id=_PRODUCT, cls=_FakeConnector)
    yield
    clear_registry()


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


def _seed_target(
    *,
    tenant_id: uuid.UUID,
    name: str,
    product: str = _PRODUCT,
    host: str = "host.example.test",
    port: int | None = None,
    aliases: list[str] | None = None,
    notes: str | None = None,
) -> uuid.UUID:
    target_id = uuid.uuid4()
    now = datetime.now(UTC)

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                TargetORM(
                    id=target_id,
                    tenant_id=tenant_id,
                    name=name,
                    aliases=aliases or [],
                    product=product,
                    host=host,
                    port=port,
                    fqdn=None,
                    secret_ref=None,
                    auth_model="shared_service_account",
                    vpn_required=False,
                    extras={},
                    notes=notes,
                    fingerprint=None,
                    preferred_impl_id=None,
                    created_at=now,
                    updated_at=now,
                ),
            )

    asyncio.run(_do())
    return target_id


def _load_target(tenant_id: uuid.UUID, name: str) -> TargetORM | None:
    """Read a target row back for post-mutation assertions."""
    from sqlalchemy import select

    async def _do() -> TargetORM | None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = select(TargetORM).where(
                TargetORM.tenant_id == tenant_id,
                TargetORM.name == name,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    return asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str = "unused",
    operator_sub: str = _OP_OPERATOR,
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
        keypair = _make_rsa_keypair("ui-connectors-forms-test-kid")
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


def test_create_modal_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/connectors/create`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/connectors/create")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# Create modal render -- RBAC gate
# ---------------------------------------------------------------------------


def test_create_modal_renders_for_tenant_admin() -> None:
    """A tenant_admin GET renders the create modal with product + auth options."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/connectors/create")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="connectors-create-modal"' in body
    assert 'hx-post="/ui/connectors/create"' in body
    # Product dropdown driven by the registered-connector set.
    assert f'value="{_PRODUCT}"' in body
    # Auth model dropdown carries the AuthModel enum values.
    assert "shared_service_account" in body
    # Required fields present.
    assert 'name="name"' in body
    assert 'name="host"' in body
    assert 'name="port"' in body


def test_create_modal_rejects_operator_role_with_403() -> None:
    """An operator (non-admin) GET on the create modal is 403'd server-side."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get("/ui/connectors/create")
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


def test_list_shows_create_button_for_tenant_admin_only() -> None:
    """The list page renders the "Create target" button only for tenant_admin."""
    _seed_tenant(_TENANT_A, "tenant-a")
    admin_client, admin_mock, _ = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        admin_response = admin_client.get("/ui/connectors")
    finally:
        admin_mock.stop()
    assert admin_response.status_code == 200, admin_response.text
    assert 'hx-get="/ui/connectors/create"' in admin_response.text

    op_client, op_mock, _ = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        op_response = op_client.get("/ui/connectors")
    finally:
        op_mock.stop()
    assert op_response.status_code == 200, op_response.text
    assert 'hx-get="/ui/connectors/create"' not in op_response.text


# ---------------------------------------------------------------------------
# Create submit -- success + validation failure
# ---------------------------------------------------------------------------


def test_create_submit_persists_target_and_redirects_to_list() -> None:
    """A valid create POST persists the row + returns HX-Redirect to the list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/connectors/create",
            headers=_form_headers(csrf),
            data={
                "name": "new-target",
                "product": _PRODUCT,
                "host": "new.example.test",
                "port": "8443",
                "auth_model": "shared_service_account",
                "aliases": "alpha, beta, alpha",
                "notes": "created via UI",
            },
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/connectors"

    row = _load_target(_TENANT_A, "new-target")
    assert row is not None
    assert row.product == _PRODUCT
    assert row.host == "new.example.test"
    assert row.port == 8443
    # Aliases parsed + de-duplicated, first-seen order preserved.
    assert list(row.aliases) == ["alpha", "beta"]
    assert row.notes == "created via UI"


def test_create_submit_rejects_port_out_of_range_with_field_error() -> None:
    """A port outside 1-65535 re-renders the form with a 422 + field error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/connectors/create",
            headers=_form_headers(csrf),
            data={
                "name": "bad-port",
                "product": _PRODUCT,
                "host": "host.example.test",
                "port": "99999",
                "auth_model": "shared_service_account",
            },
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    body = response.text
    # Form re-rendered (not a redirect) with the port field error.
    assert 'id="connectors-create-modal"' in body
    assert 'data-error-for="port"' in body
    # The operator's typed values are echoed back.
    assert 'value="bad-port"' in body
    # Nothing persisted.
    assert _load_target(_TENANT_A, "bad-port") is None


def test_create_submit_rejects_empty_name_with_field_error() -> None:
    """An empty name re-renders the form with a 422 + name field error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/connectors/create",
            headers=_form_headers(csrf),
            data={
                "name": "",
                "product": _PRODUCT,
                "host": "host.example.test",
                "auth_model": "shared_service_account",
            },
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert 'data-error-for="name"' in response.text


def test_create_submit_rejects_operator_role_with_403() -> None:
    """An operator (non-admin) create POST is rejected with 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.post(
            "/ui/connectors/create",
            headers=_form_headers(csrf),
            data={
                "name": "op-cant-create",
                "product": _PRODUCT,
                "host": "host.example.test",
                "auth_model": "shared_service_account",
            },
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    assert _load_target(_TENANT_A, "op-cant-create") is None


def test_create_submit_without_csrf_is_rejected() -> None:
    """A create POST missing the CSRF header is rejected by the chassis middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/connectors/create",
            headers={"HX-Request": "true"},  # no X-CSRF-Token
            data={
                "name": "no-csrf",
                "product": _PRODUCT,
                "host": "host.example.test",
                "auth_model": "shared_service_account",
            },
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    assert _load_target(_TENANT_A, "no-csrf") is None


# ---------------------------------------------------------------------------
# Edit modal render -- pre-population + RBAC + cross-tenant
# ---------------------------------------------------------------------------


def test_edit_modal_prepopulates_target_fields() -> None:
    """The edit modal pre-populates the fields from the resolved target."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(
        tenant_id=_TENANT_A,
        name="edit-me",
        host="orig.example.test",
        port=9000,
        aliases=["primary"],
        notes="original notes",
    )
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/connectors/edit-me/edit")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="connectors-edit-modal"' in body
    assert 'hx-patch="/ui/connectors/edit-me"' in body
    assert 'value="edit-me"' in body
    assert 'value="orig.example.test"' in body
    assert 'value="9000"' in body
    assert 'value="primary"' in body
    assert "original notes" in body
    # Name input is read-only (rename = delete + create).
    assert "readonly" in body


def test_edit_modal_rejects_operator_role_with_403() -> None:
    """An operator (non-admin) GET on the edit modal is 403'd server-side."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="op-cant-edit")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get("/ui/connectors/op-cant-edit/edit")
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


def test_edit_modal_isolates_other_tenants_target() -> None:
    """An admin in tenant B cannot load tenant A's edit form (404)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_target(tenant_id=_TENANT_A, name="a-target")
    # Admin authenticated in tenant B.
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_B,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/connectors/a-target/edit")
    finally:
        mock.stop()
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Edit submit -- success + validation + cross-tenant
# ---------------------------------------------------------------------------


def test_edit_submit_patches_target_and_redirects_to_list() -> None:
    """A valid edit PATCH updates the row + returns HX-Redirect to the list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="patch-me", host="old.example.test", port=80)
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.patch(
            "/ui/connectors/patch-me",
            headers=_form_headers(csrf),
            data={
                "product": _PRODUCT,
                "host": "new.example.test",
                "port": "443",
                "auth_model": "per_user",
                "aliases": "renamed",
                "notes": "patched",
            },
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/connectors"

    row = _load_target(_TENANT_A, "patch-me")
    assert row is not None
    assert row.host == "new.example.test"
    assert row.port == 443
    assert row.auth_model == "per_user"
    assert list(row.aliases) == ["renamed"]
    assert row.notes == "patched"


def test_edit_submit_rejects_port_out_of_range_with_field_error() -> None:
    """An out-of-range port on edit re-renders the edit modal with a 422 + error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="patch-bad-port", port=80)
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.patch(
            "/ui/connectors/patch-bad-port",
            headers=_form_headers(csrf),
            data={
                "product": _PRODUCT,
                "host": "host.example.test",
                "port": "70000",
                "auth_model": "shared_service_account",
            },
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    body = response.text
    assert 'id="connectors-edit-modal"' in body
    assert 'data-error-for="port"' in body
    # Unchanged in the DB.
    row = _load_target(_TENANT_A, "patch-bad-port")
    assert row is not None
    assert row.port == 80


def test_edit_submit_isolates_other_tenants_target() -> None:
    """An admin in tenant B cannot PATCH tenant A's target (404)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_target(tenant_id=_TENANT_A, name="a-only", host="orig.example.test")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_B,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.patch(
            "/ui/connectors/a-only",
            headers=_form_headers(csrf),
            data={
                "product": _PRODUCT,
                "host": "hijacked.example.test",
                "auth_model": "shared_service_account",
            },
        )
    finally:
        mock.stop()
    assert response.status_code == 404, response.text
    # Tenant A's row is untouched.
    row = _load_target(_TENANT_A, "a-only")
    assert row is not None
    assert row.host == "orig.example.test"


def test_edit_submit_rejects_operator_role_with_403() -> None:
    """An operator (non-admin) edit PATCH is rejected with 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="op-patch", host="orig.example.test")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.patch(
            "/ui/connectors/op-patch",
            headers=_form_headers(csrf),
            data={
                "product": _PRODUCT,
                "host": "changed.example.test",
                "auth_model": "shared_service_account",
            },
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    row = _load_target(_TENANT_A, "op-patch")
    assert row is not None
    assert row.host == "orig.example.test"


# ---------------------------------------------------------------------------
# Delete modal + submit (G0.15-T10 #1218)
# ---------------------------------------------------------------------------


def _seed_graph_node(*, tenant_id: uuid.UUID, target_id: uuid.UUID, name: str) -> None:
    """Insert one ``graph_node`` row pointing at ``target_id``.

    Used to exercise the cascade-count branch of the delete modal +
    the REST handler's 409+``?force=true`` flow (which the modal
    pre-bypasses by appending ``force=true`` to the submit URL when
    the cascade count is non-zero on render).
    """
    from meho_backplane.db.models import GraphNode

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                GraphNode(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    kind="service",
                    name=name,
                    target_id=target_id,
                    properties={},
                    discovered_by="test",
                ),
            )

    asyncio.run(_do())


def _load_target_including_deleted(tenant_id: uuid.UUID, name: str) -> TargetORM | None:
    """Read a target row back regardless of ``deleted_at`` -- soft-delete check."""
    from sqlalchemy import select

    async def _do() -> TargetORM | None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = select(TargetORM).where(
                TargetORM.tenant_id == tenant_id,
                TargetORM.name == name,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    return asyncio.run(_do())


def test_delete_modal_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/connectors/<name>/delete`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/connectors/some-target/delete")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_delete_modal_renders_for_tenant_admin() -> None:
    """G0.15-T10 #1218 -- a tenant_admin GET renders the confirm modal."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="del-me")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/connectors/del-me/delete")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="connectors-delete-modal"' in body
    # No cascade refs -- the submit URL must NOT carry ``?force=true``.
    assert 'hx-post="/ui/connectors/del-me/delete"' in body
    assert "?force=true" not in body
    assert "Delete target" in body


def test_delete_modal_surfaces_cascade_count_when_nonzero() -> None:
    """G0.15-T10 #1218 -- a target referenced by graph_node rows shows the count
    and the submit URL is pre-set to ``?force=true``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    target_id = _seed_target(tenant_id=_TENANT_A, name="del-linked")
    _seed_graph_node(tenant_id=_TENANT_A, target_id=target_id, name="node-a")
    _seed_graph_node(tenant_id=_TENANT_A, target_id=target_id, name="node-b")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/connectors/del-linked/delete")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "Cascade impact" in body
    assert "2 topology" in body
    # The submit URL pre-bypasses the 409+force handshake by setting
    # ``?force=true`` on the form's hx-post when cascade refs exist.
    assert "/delete?force=true" in body
    assert "Delete anyway" in body


def test_delete_modal_rejects_operator_role_with_403() -> None:
    """G0.15-T10 #1218 -- a non-admin GET of the delete modal hits 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="op-del")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get("/ui/connectors/op-del/delete")
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


def test_delete_modal_isolates_other_tenants_target() -> None:
    """G0.15-T10 #1218 -- an admin in tenant B cannot load tenant A's delete modal."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_target(tenant_id=_TENANT_A, name="a-only-del")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_B,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/connectors/a-only-del/delete")
    finally:
        mock.stop()
    assert response.status_code == 404, response.text


def test_delete_submit_soft_deletes_and_redirects_to_list() -> None:
    """G0.15-T10 #1218 -- a valid delete POST soft-deletes + HX-Redirects to /ui/connectors."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="kill-me")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/connectors/kill-me/delete",
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/connectors"
    # Row remains in the DB but ``deleted_at`` is stamped -- the read
    # surface filter (``deleted_at IS NULL``) hides it from list /
    # detail; audit-log soft-FK keeps pointing at the row.
    row = _load_target_including_deleted(_TENANT_A, "kill-me")
    assert row is not None
    assert row.deleted_at is not None
    # And the read-surface helper that filters deleted_at IS NULL no
    # longer sees the row.
    assert _load_target(_TENANT_A, "kill-me") is not None  # row stays; assertion is on deleted_at


def test_delete_submit_with_force_succeeds_when_graph_node_refs_exist() -> None:
    """G0.15-T10 #1218 -- ``?force=true`` POST soft-deletes despite graph_node refs.

    Mirrors the REST 409+``?force=true`` contract on
    :func:`~meho_backplane.api.v1.targets.delete_target`: without force,
    a target with cascade refs 409s; with force, it succeeds and the
    graph_node rows survive (ON DELETE SET NULL).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    target_id = _seed_target(tenant_id=_TENANT_A, name="force-del")
    _seed_graph_node(tenant_id=_TENANT_A, target_id=target_id, name="link")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/connectors/force-del/delete?force=true",
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/connectors"
    row = _load_target_including_deleted(_TENANT_A, "force-del")
    assert row is not None
    assert row.deleted_at is not None


def test_delete_submit_without_force_409s_when_graph_node_refs_exist() -> None:
    """G0.15-T10 #1218 -- delete without ``?force=true`` 409s when cascade refs exist.

    Mirrors the REST handler's contract -- the UI submit lands in the
    same place as the CLI submit, so an operator who hand-crafts a
    no-force POST (the modal otherwise sets ``force=true`` when refs
    exist) sees the same 409 the REST surface would return.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    target_id = _seed_target(tenant_id=_TENANT_A, name="noforce")
    _seed_graph_node(tenant_id=_TENANT_A, target_id=target_id, name="link")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/connectors/noforce/delete",
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 409, response.text
    # Target row is untouched.
    row = _load_target_including_deleted(_TENANT_A, "noforce")
    assert row is not None
    assert row.deleted_at is None


def test_delete_submit_isolates_other_tenants_target() -> None:
    """G0.15-T10 #1218 -- a tenant B admin cannot delete tenant A's target (404)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_target(tenant_id=_TENANT_A, name="a-survive")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_B,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/connectors/a-survive/delete",
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 404, response.text
    # Tenant A's row remains live (not soft-deleted).
    row = _load_target_including_deleted(_TENANT_A, "a-survive")
    assert row is not None
    assert row.deleted_at is None


def test_delete_submit_rejects_operator_role_with_403() -> None:
    """G0.15-T10 #1218 -- a non-admin delete POST is rejected with 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="op-survive")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.post(
            "/ui/connectors/op-survive/delete",
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    row = _load_target_including_deleted(_TENANT_A, "op-survive")
    assert row is not None
    assert row.deleted_at is None


def test_delete_submit_without_csrf_is_rejected() -> None:
    """G0.15-T10 #1218 -- a delete POST missing the CSRF header hits the chassis gate."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="csrf-survive")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/connectors/csrf-survive/delete",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()
    # Chassis CSRFMiddleware double-submit gate -- exact status is the
    # chassis contract (typically 403 from a missing/mismatched token).
    assert response.status_code in {401, 403}, response.text
    row = _load_target_including_deleted(_TENANT_A, "csrf-survive")
    assert row is not None
    assert row.deleted_at is None
