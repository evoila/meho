# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the bulk targets.yaml import UI (Task #875).

Initiative #340 (G10.3 Connectors + Targets UI), Task #875 (G10.3-T3).
The acceptance criteria on issue #875 are:

* Paste or upload of a ``targets.yaml`` renders a server-side preview
  table marking each target CREATE-vs-UPDATE (HTMX); YAML parse errors
  render inline (no 500).
* Confirm applies the plan in-process via the REST ``create_target`` /
  ``update_target`` handlers; a result summary (N created, M updated) is
  shown.
* Classification + key-mapping match ``meho targets import`` (#257):
  known keys -> columns, unknown -> ``extras``, ``fingerprint`` dropped,
  CREATE vs UPDATE decided by existing-name lookup.
* ``tenant_admin`` only (server-side 403 for operators); CSRF enforced;
  cross-tenant isolation (import only into the caller's tenant).

Harness shape mirrors :mod:`backend.tests.test_ui_connectors_forms`: a
minimal FastAPI app wired with the UI session + CSRF middlewares, a
``web_session`` row carrying a real Keycloak-minted access token so the
``resolve_operator_or_403`` dep can re-verify the role, and a fake
connector registered so ``registered_product_tokens()`` advertises the
product the imported entries POST.
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
from meho_backplane.ui.routes.connectors.import_view import ImportParseError, build_plan
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
    """Pin chassis + BFF env vars for every test (mirrors the forms suite)."""
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
    ``fakeprod`` -- the product the imported entries POST + validate against.
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
    """Register ``fakeprod`` so the imported entries' product validates."""
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
    extras: dict[str, Any] | None = None,
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
                    extras=extras or {},
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


def _count_targets(tenant_id: uuid.UUID) -> int:
    from sqlalchemy import func, select

    async def _do() -> int:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = (
                select(func.count())
                .select_from(TargetORM)
                .where(
                    TargetORM.tenant_id == tenant_id,
                )
            )
            return int((await session.execute(stmt)).scalar_one())

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
        keypair = _make_rsa_keypair("ui-connectors-import-test-kid")
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


_TWO_ENTRY_YAML = """
targets:
  - name: alpha
    product: fakeprod
    host: alpha.example.test
    port: 8443
  - name: beta
    product: fakeprod
    host: beta.example.test
    sso_realm: corp
    fingerprint:
      vendor: stale
""".strip()


# ---------------------------------------------------------------------------
# build_plan -- mapping + classification parity (unit-level, no app)
# ---------------------------------------------------------------------------


def test_build_plan_classifies_create_and_update() -> None:
    """Existing names plan UPDATE; new names plan CREATE; order preserved."""
    entries = [
        {"name": "alpha", "product": _PRODUCT, "host": "a.test"},
        {"name": "beta", "product": _PRODUCT, "host": "b.test"},
    ]
    plan = build_plan(entries, existing={"alpha"})
    assert [(p.name, p.action) for p in plan] == [("alpha", "UPDATE"), ("beta", "CREATE")]


def test_build_plan_spills_unknown_keys_into_extras() -> None:
    """Unknown YAML keys land in ``extras``; known keys stay top-level."""
    entries = [{"name": "g", "product": _PRODUCT, "host": "h.test", "account": "acct-1"}]
    plan = build_plan(entries, existing=set())
    body = plan[0].body
    assert body.host == "h.test"
    assert body.extras == {"account": "acct-1"}
    assert plan[0].fields == ["extras", "host", "name", "product"]


def test_build_plan_merges_explicit_extras_with_spilled() -> None:
    """An explicit ``extras:`` block merges with the unknown-key spill."""
    entries = [
        {
            "name": "g",
            "product": _PRODUCT,
            "host": "h.test",
            "extras": {"region": "eu"},
            "project_id": "proj-7",
        }
    ]
    plan = build_plan(entries, existing=set())
    assert plan[0].body.extras == {"region": "eu", "project_id": "proj-7"}


def test_build_plan_drops_fingerprint_with_warning() -> None:
    """``fingerprint`` is dropped from the body and a warning is recorded."""
    entries = [{"name": "g", "product": _PRODUCT, "host": "h.test", "fingerprint": {"vendor": "x"}}]
    plan = build_plan(entries, existing=set())
    assert "fingerprint" not in plan[0].fields
    assert any("fingerprint" in w for w in plan[0].warnings)


def test_build_plan_update_body_is_sparse_and_strips_name_product() -> None:
    """The UPDATE body omits ``name`` / ``product`` and only carries YAML keys."""
    entries = [{"name": "alpha", "product": _PRODUCT, "host": "a.test", "notes": "n"}]
    plan = build_plan(entries, existing={"alpha"})
    body = plan[0].body
    # Sparse PATCH: only keys present in the YAML are set on the model.
    assert body.model_dump(exclude_unset=True) == {"host": "a.test", "notes": "n"}
    assert "name" not in plan[0].fields
    assert "product" not in plan[0].fields


def test_build_plan_raises_on_schema_invalid_auth_model() -> None:
    """A structurally-valid entry with a bad ``auth_model`` enum fails the plan."""
    entries = [
        {"name": "g", "product": _PRODUCT, "host": "h.test", "auth_model": "NOT_A_VALID_ENUM"}
    ]
    with pytest.raises(ImportParseError) as excinfo:
        build_plan(entries, existing=set())
    assert "auth_model" in str(excinfo.value) or "NOT_A_VALID_ENUM" in str(excinfo.value)


def test_build_plan_raises_on_out_of_range_port() -> None:
    """An out-of-range ``port`` fails the plan before any write is attempted."""
    entries = [{"name": "g", "product": _PRODUCT, "host": "h.test", "port": 70000}]
    with pytest.raises(ImportParseError) as excinfo:
        build_plan(entries, existing=set())
    assert "port" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Authentication / RBAC boundary
# ---------------------------------------------------------------------------


def test_import_page_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/connectors/import`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/connectors/import")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_import_page_renders_for_tenant_admin() -> None:
    """A tenant_admin GET renders the import page with paste + upload controls."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.get("/ui/connectors/import")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'hx-post="/ui/connectors/import"' in body
    assert 'name="pasted"' in body
    assert 'name="upload"' in body
    assert 'type="file"' in body


def test_import_page_rejects_operator_role_with_403() -> None:
    """An operator (non-admin) GET on the import page is 403'd server-side."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.get("/ui/connectors/import")
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


def test_import_preview_rejects_operator_role_with_403() -> None:
    """A non-admin preview POST is 403'd before any parse."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.post(
            "/ui/connectors/import",
            headers=_form_headers(csrf),
            data={"pasted": _TWO_ENTRY_YAML},
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


def test_import_preview_rejects_missing_csrf() -> None:
    """A preview POST without the CSRF token is 403'd by the chassis middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/connectors/import",
            headers={"HX-Request": "true"},
            data={"pasted": _TWO_ENTRY_YAML},
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# Preview -- paste, upload, parse error
# ---------------------------------------------------------------------------


def test_preview_paste_renders_create_and_update_table() -> None:
    """A pasted YAML renders a preview classifying CREATE vs UPDATE."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # ``alpha`` already exists -> UPDATE; ``beta`` is new -> CREATE.
    _seed_target(tenant_id=_TENANT_A, name="alpha", host="old.example.test")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/connectors/import",
            headers=_form_headers(csrf),
            data={"pasted": _TWO_ENTRY_YAML},
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-name="alpha"' in body
    assert 'data-name="beta"' in body
    # alpha -> UPDATE, beta -> CREATE
    assert 'data-action="UPDATE"' in body
    assert 'data-action="CREATE"' in body
    # fingerprint on beta surfaces as a preview warning, not a 500.
    assert "fingerprint" in body
    # No write happened on a preview.
    row = _load_target(_TENANT_A, "beta")
    assert row is None


def test_preview_upload_file_renders_table() -> None:
    """An uploaded targets.yaml is parsed and previewed (file path)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/connectors/import",
            headers=_form_headers(csrf),
            files={"upload": ("targets.yaml", _TWO_ENTRY_YAML.encode(), "application/x-yaml")},
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert 'data-name="alpha"' in response.text
    assert 'data-name="beta"' in response.text


def test_preview_malformed_yaml_renders_inline_error_not_500() -> None:
    """A YAML syntax error renders an inline error with 422 (never a 500)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/connectors/import",
            headers=_form_headers(csrf),
            data={"pasted": "targets:\n  - name: a\n    product: [unclosed"},
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert "data-import-error" in response.text


def test_preview_missing_required_field_renders_inline_error() -> None:
    """An entry missing ``host`` fails fast with an inline error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/connectors/import",
            headers=_form_headers(csrf),
            data={"pasted": "targets:\n  - name: a\n    product: fakeprod"},
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert "data-import-error" in response.text
    assert "host" in response.text


def test_preview_schema_invalid_value_renders_inline_error() -> None:
    """A structurally-valid entry with a schema-invalid value (bad ``auth_model``
    enum) surfaces inline on preview -- the preview must not green-light a plan
    that confirm would reject.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    yaml_text = (
        "targets:\n"
        "  - name: a\n"
        "    product: fakeprod\n"
        "    host: a.example.test\n"
        "    auth_model: NOT_A_VALID_ENUM"
    )
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/connectors/import",
            headers=_form_headers(csrf),
            data={"pasted": yaml_text},
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert "data-import-error" in response.text


# ---------------------------------------------------------------------------
# Confirm -- in-process create/update + result summary
# ---------------------------------------------------------------------------


def test_confirm_creates_and_updates_in_process() -> None:
    """Confirm POSTs new targets + PATCHes existing ones, shows the summary."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="alpha", host="old.example.test", notes="orig")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/connectors/import/confirm",
            headers=_form_headers(csrf),
            data={"pasted": _TWO_ENTRY_YAML},
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "data-import-result" in body
    # 1 created (beta), 1 updated (alpha).
    assert "data-created>1" in body
    assert "data-updated>1" in body

    # beta created.
    beta = _load_target(_TENANT_A, "beta")
    assert beta is not None
    assert beta.host == "beta.example.test"
    # unknown key spilled into extras.
    assert beta.extras == {"sso_realm": "corp"}
    # fingerprint dropped (server-managed).
    assert beta.fingerprint is None

    # alpha updated -- host patched, product unchanged (sparse PATCH).
    alpha = _load_target(_TENANT_A, "alpha")
    assert alpha is not None
    assert alpha.host == "alpha.example.test"
    assert alpha.port == 8443
    assert alpha.product == _PRODUCT


def test_confirm_update_is_sparse_does_not_wipe_omitted_columns() -> None:
    """A sparse YAML on UPDATE leaves columns it omits untouched."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(
        tenant_id=_TENANT_A,
        name="alpha",
        host="old.example.test",
        notes="keep-me",
        aliases=["a1"],
    )
    yaml_text = "targets:\n  - name: alpha\n    product: fakeprod\n    host: new.example.test"
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/connectors/import/confirm",
            headers=_form_headers(csrf),
            data={"pasted": yaml_text},
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    alpha = _load_target(_TENANT_A, "alpha")
    assert alpha is not None
    assert alpha.host == "new.example.test"
    # notes + aliases were not in the YAML -> sparse PATCH leaves them.
    assert alpha.notes == "keep-me"
    assert list(alpha.aliases) == ["a1"]


def test_confirm_malformed_yaml_renders_inline_error_no_write() -> None:
    """A malformed YAML on confirm renders the inline error and writes nothing."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/connectors/import/confirm",
            headers=_form_headers(csrf),
            data={"pasted": "targets:\n  - name: a\n    product: [unclosed"},
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert "data-import-error" in response.text
    assert _count_targets(_TENANT_A) == 0


def test_confirm_schema_invalid_entry_422_writes_nothing() -> None:
    """A schema-invalid entry mid-list aborts the whole import (no partial write).

    The first entry is valid; the second carries an out-of-range ``port``.
    Because the full plan is built and validated before the write loop, the
    valid first entry must NOT be committed -- confirm renders the inline 422
    and writes zero rows (parity with the CLI's no-partial-write contract).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    yaml_text = (
        "targets:\n"
        "  - name: valid-first\n"
        "    product: fakeprod\n"
        "    host: valid.example.test\n"
        "  - name: bad-port\n"
        "    product: fakeprod\n"
        "    host: bad.example.test\n"
        "    port: 70000"
    )
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/connectors/import/confirm",
            headers=_form_headers(csrf),
            data={"pasted": yaml_text},
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert "data-import-error" in response.text
    # No partial import: neither the valid first row nor the bad second row landed.
    assert _count_targets(_TENANT_A) == 0
    assert _load_target(_TENANT_A, "valid-first") is None


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


def test_confirm_imports_only_into_callers_tenant() -> None:
    """A confirm by tenant A's admin lands the row in tenant A only."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    yaml_text = "targets:\n  - name: gamma\n    product: fakeprod\n    host: g.example.test"
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/connectors/import/confirm",
            headers=_form_headers(csrf),
            data={"pasted": yaml_text},
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert _load_target(_TENANT_A, "gamma") is not None
    assert _load_target(_TENANT_B, "gamma") is None


def test_preview_classification_scoped_to_caller_tenant() -> None:
    """A same-named target in another tenant does not flip CREATE to UPDATE."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    # ``delta`` exists in tenant B but NOT in tenant A.
    _seed_target(tenant_id=_TENANT_B, name="delta", host="b.example.test")
    yaml_text = "targets:\n  - name: delta\n    product: fakeprod\n    host: a.example.test"
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/connectors/import",
            headers=_form_headers(csrf),
            data={"pasted": yaml_text},
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    # Tenant A has no ``delta`` -> the entry classifies as CREATE.
    assert 'data-action="CREATE"' in response.text
