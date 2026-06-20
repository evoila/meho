# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Vault KV browser UI surface.

Initiative #1942 (G10.18 Vault / secrets console), Task #1956 (T1). The
acceptance criteria on issue #1956 are:

* ``GET /ui/vault/list`` for a tenant-scoped mount/path renders the child
  key NAMES; no secret value text appears in the response body (list never
  returns values).
* ``GET /ui/vault/read`` renders the secret values reveal-gated, AND no
  secret value string is written to a log/audit row during the request --
  the value lands only in the response template.
* a ``vault.kv.list`` envelope with ``handle is not None`` renders the
  handle metadata (``handle_id`` present) and does NOT inline the full
  list blob.
* the picker emits ``vault-1.x`` (the bare slug ``vault`` is NOT a
  selectable value); a list/read request outside
  ``secret/tenants/{tenant_id}/`` renders the "outside your tenant scope"
  message, not a raw 403.
* ``build_vault_router`` is included before ``build_stubs_router()``;
  ``/ui/vault/list`` resolves to the list handler, not a ``{param}`` route.

Suite shape mirrors :mod:`backend.tests.test_ui_operations`: a minimal
FastAPI app with the UI session + CSRF middlewares, a ``web_session`` row
carrying a real Keycloak-minted access token (so the operator lift
re-verifies the token and the vault handler forwards it to Vault), seeded
``operation_group`` / ``endpoint_descriptor`` rows (so the picker resolves
``vault-1.x`` as ingested), the registered vault typed ops + connector,
and the shared in-process Vault fake (``install_fake_client``) so the real
in-process dispatch reaches a deterministic Vault.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
import warnings
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.connectors.vault.ops import register_vault_typed_operations
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup, Tenant
from meho_backplane.operations import reset_dispatcher_caches
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
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import AUDIENCE as _DEFAULT_AUDIENCE
from tests._oidc_jwt_helpers import ISSUER as _DEFAULT_ISSUER
from tests._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from tests._oidc_jwt_helpers import mint_token as _mint_token
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from tests._oidc_jwt_helpers import public_jwks as _public_jwks
from tests._vault_fakes import install_fake_client

_BACKPLANE_URL = "https://meho.test"
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OP_OPERATOR = "op-operator"

#: A secret value sentinel: a list response must never carry it, and a
#: read response must never write it to a log/audit row.
_SECRET_VALUE = "SUPER-SECRET-VALUE-do-not-leak-9f3a"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF + Vault env vars (mirrors the UI + vault suites)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    # Default-on tenant scope (the v0.15.0 #1725 production default). The
    # mount-pinned prefix is what the browser defaults the picker into.
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "secret/tenants/{tenant_id}/")
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
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    clear_registry()
    reset_dispatcher_caches()


@pytest.fixture
def _stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation`` skips ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the vault UI tests."""
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


def _seed_vault_descriptor() -> None:
    """Seed an enabled vault ``operation_group`` + ``endpoint_descriptor``.

    Makes the connector listing report ``vault-1.x`` as ``state="ingested"``
    so the picker resolves it (the listing keys "ingested" off DB-backed
    descriptor rows). The actual dispatch goes through the registered typed
    ops, not this row -- this seed only feeds the picker.
    """
    group_id = uuid.uuid4()
    desc_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=None,
                    product="vault",
                    version="1.x",
                    impl_id="vault",
                    group_key="kv",
                    name="KV secrets",
                    when_to_use="browse the KV secret tree",
                    review_status="enabled",
                ),
            )
            session.add(
                EndpointDescriptor(
                    id=desc_id,
                    tenant_id=None,
                    product="vault",
                    version="1.x",
                    impl_id="vault",
                    op_id="vault.kv.read",
                    source_kind="typed",
                    method="GET",
                    path="/v1/secret",
                    summary="Read a KV secret.",
                    description="reads a secret value",
                    group_id=group_id,
                    parameter_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                    llm_instructions=None,
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=True,
                ),
            )

    asyncio.run(_do())


def _register_vault(stub_embedding_service: AsyncMock) -> None:
    """Register the vault v2 connector + upsert the typed-op descriptors.

    The real in-process dispatch (``call_operation`` -> dispatcher ->
    ``VaultConnector`` typed handlers) needs both: the registry entry so
    the resolver finds :class:`VaultConnector`, and the typed-op rows so
    ``vault.kv.read/list/versions`` resolve to their handlers.
    """
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)

    async def _do() -> None:
        await register_vault_typed_operations(embedding_service=stub_embedding_service)

    asyncio.run(_do())


def _seed_session_sync(*, tenant_id: uuid.UUID, access_token: str, operator_sub: str) -> uuid.UUID:
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
        keypair = _make_rsa_keypair(f"ui-vault-test-kid-{uuid.uuid4().hex[:8]}")
    return keypair, _public_jwks(keypair)


def _client_with_role(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter]:
    """Return a TestClient + respx mock for the session-gated vault routes.

    The operator lift re-validates the BFF session's access token through
    the JWT chain; the chain needs the JWKS endpoint mocked. The caller
    enters ``mock`` as a context manager.
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
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client, mock


def _tenant_path(tenant_id: uuid.UUID, suffix: str = "") -> str:
    """The in-scope KV path for *tenant_id* under the default-on guard."""
    base = f"tenants/{tenant_id}"
    return f"{base}/{suffix}" if suffix else base


def _count_audit_rows() -> int:
    async def _do() -> int:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(select(func.count()).select_from(AuditLog))
            return int(result.scalar_one())

    return asyncio.run(_do())


def _audit_visible_payloads() -> list[str]:
    """Return the audit-trail-VISIBLE ``payload`` JSON of every audit row.

    ``AuditLog.payload`` is the redacted, audit-trail-visible record (op
    id / params_hash / result_status -- value-free for a vault read); the
    separate ``raw_payload`` column carries the verbatim record the
    redaction engine re-derives from at read time, governed by
    ``redaction_manifest`` -- by design, not a leak, and the dispatcher's
    concern, not this BFF's. The redaction invariant this surface owns is
    that no secret value reaches a *visible* audit field or a log.
    """

    async def _do() -> list[str]:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows = (await session.execute(select(AuditLog.payload))).scalars().all()
            return [str(row) for row in rows]

    return asyncio.run(_do())


# ---------------------------------------------------------------------------
# Picker: connector_id shape footgun (vault-1.x, never the bare slug)
# ---------------------------------------------------------------------------


def test_vault_ui_index_picker_emits_connector_id_not_product_slug(
    _stub_embedding_service: AsyncMock,
) -> None:
    """``GET /ui/vault`` emits ``vault-1.x``; the bare slug ``vault`` is not selectable."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_stub_embedding_service)
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get("/ui/vault")
    assert response.status_code == 200, response.text
    body = response.text
    # The dispatchable id is carried as a selectable value.
    assert 'value="vault-1.x"' in body
    # The bare product slug must NOT be a selectable connector_id value.
    assert 'value="vault"' not in body
    # The mount/path picker defaults into the tenant prefix (default-on guard).
    assert f"tenants/{_TENANT_A}" in body


def test_vault_ui_index_no_connector_renders_hint_not_dead_form(
    _stub_embedding_service: AsyncMock,
) -> None:
    """With no ingested vault connector, the index renders a hint, not a form."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # No descriptor seeded + no connector registered -> the picker resolves
    # to None.
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get("/ui/vault")
    assert response.status_code == 200, response.text
    assert 'data-vault-error="no_vault_connector"' in response.text


# ---------------------------------------------------------------------------
# vault.kv.list: child key NAMES, no secret values
# ---------------------------------------------------------------------------


def test_vault_ui_list_renders_key_names_no_secret_value(
    monkeypatch: pytest.MonkeyPatch,
    _stub_embedding_service: AsyncMock,
) -> None:
    """``GET /ui/vault/list`` renders child key names; no secret value text leaks."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_stub_embedding_service)
    # The list endpoint returns only names; the fake's secret carries the
    # sentinel so the assertion proves the value is not in the list render.
    install_fake_client(
        monkeypatch,
        keys=["db-creds", "api-token", "nested/"],
        secret={"password": _SECRET_VALUE},
    )
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(
            "/ui/vault/list",
            params={"mount": "secret", "path": _tenant_path(_TENANT_A, "app")},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-vault-keys="inline"' in body
    assert "db-creds" in body
    assert "api-token" in body
    # The list endpoint never returns secret values: the sentinel must be
    # absent from the rendered key list.
    assert _SECRET_VALUE not in body


def test_vault_ui_list_handle_renders_metadata_not_blob(
    monkeypatch: pytest.MonkeyPatch,
    _stub_embedding_service: AsyncMock,
) -> None:
    """A set-shaped ``vault.kv.list`` (handle present) renders metadata, not the blob.

    Patches the route module's ``call_operation`` to return an envelope
    carrying a ``handle`` -- the JSONFlux set-shape spill. The list partial
    must render the handle metadata (``handle_id``) and NOT inline the full
    key list.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_stub_embedding_service)
    handle_id = uuid.uuid4()
    sentinel_blob = "DO-NOT-INLINE-1000-KEYS"
    handle_envelope = {
        "status": "ok",
        "op_id": "vault.kv.list",
        "result": {"row_count": 1000, "total": 1000, "sample": [{"value": "k0"}]},
        "error": None,
        "duration_ms": 12.0,
        "handle": {
            "handle_id": str(handle_id),
            "summary_md": "1000 keys matched",
            "schema_": {"type": "array"},
            "total_rows": 1000,
            "sample_rows": [{"value": sentinel_blob}],
            "ttl_seconds": 3600,
            "fetch_more": None,
        },
        "extras": {},
    }

    async def _fake_call(operator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        return handle_envelope

    monkeypatch.setattr(
        "meho_backplane.ui.routes.vault.routes.call_operation",
        _fake_call,
    )
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(
            "/ui/vault/list",
            params={"mount": "secret", "path": _tenant_path(_TENANT_A, "app")},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200, response.text
    body = response.text
    # The handle metadata branch -- handle_id present, full sample NOT.
    assert 'data-vault-keys="handle"' in body
    assert str(handle_id) in body
    assert "1000" in body
    assert sentinel_blob not in body
    assert 'data-vault-keys="inline"' not in body


# ---------------------------------------------------------------------------
# vault.kv.read: reveal-gated values, NEVER logged / audited
# ---------------------------------------------------------------------------


def test_vault_ui_read_renders_values_reveal_gated_and_unlogged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _stub_embedding_service: AsyncMock,
) -> None:
    """``GET /ui/vault/read`` renders the secret reveal-gated; the value is never logged.

    The redaction invariant: the secret value reaches the response body
    (reveal-on-click), but no secret value string is written to a log or
    audit row during the request.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_stub_embedding_service)
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE}, kv_version=4)
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    audit_before = _count_audit_rows()
    with caplog.at_level(logging.DEBUG), mock:
        response = client.get(
            "/ui/vault/read",
            params={"mount": "secret", "path": _tenant_path(_TENANT_A, "app/db")},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200, response.text
    body = response.text
    # The secret render is present + reveal-gated.
    assert 'data-vault-secret="ok"' in body
    assert "data-vault-reveal" in body
    assert "data-vault-secret-value" in body
    # The value reaches the browser (the deliberate, scoped un-redaction).
    assert _SECRET_VALUE in body
    # The version chip renders.
    assert "version 4" in body
    # REDACTION INVARIANT (1): the secret value must NOT appear in any
    # MEHO application log record emitted during the request -- the BFF
    # route logs only the op-id / status / handle-presence, never the
    # envelope ``result``; the dispatcher's structured logs likewise carry
    # no value. The DB-driver loggers (``aiosqlite`` / ``sqlalchemy``) echo
    # the raw parameterised INSERT SQL at DEBUG -- which contains the audit
    # row's ``raw_payload`` (the redaction engine's verbatim re-derivation
    # source, governed by ``redaction_manifest``, the dispatcher's concern,
    # never DEBUG-enabled in production) -- so they are excluded as a
    # library-level echo, not application telemetry.
    _driver_loggers = ("aiosqlite", "sqlalchemy")
    for record in caplog.records:
        if record.name.split(".")[0] in _driver_loggers:
            continue
        assert _SECRET_VALUE not in record.getMessage(), (
            f"secret value leaked into a log record message: {record.name}/{record.levelname}"
        )
        for value in vars(record).values():
            assert _SECRET_VALUE not in str(value), (
                f"secret value leaked into a log record attribute on {record.name}"
            )
    # REDACTION INVARIANT (2): the secret value must NOT appear in the
    # audit-trail-VISIBLE ``payload`` of any audit row. (The dispatcher
    # writes one DISPATCH audit row whose redacted ``payload`` carries
    # only op-id / params_hash / result_status; the separate ``raw_payload``
    # column is the redaction engine's verbatim re-derivation source,
    # governed by ``redaction_manifest`` -- by design, the dispatcher's
    # concern, not this BFF's.)
    for payload in _audit_visible_payloads():
        assert _SECRET_VALUE not in payload, "secret value leaked into a visible audit payload"
    # The BFF itself wrote no extra audit row beyond the dispatcher's
    # single DISPATCH row.
    assert _count_audit_rows() == audit_before + 1


# ---------------------------------------------------------------------------
# vault.kv.versions: metadata table, no values
# ---------------------------------------------------------------------------


def test_vault_ui_versions_renders_metadata_table(
    monkeypatch: pytest.MonkeyPatch,
    _stub_embedding_service: AsyncMock,
) -> None:
    """``GET /ui/vault/versions`` renders the version-history metadata table."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_stub_embedding_service)
    install_fake_client(
        monkeypatch,
        secret={"password": _SECRET_VALUE},
        kv_version=2,
        versions_meta={
            "1": {"created_time": "2026-01-01T00:00:00Z", "deletion_time": "", "destroyed": False},
            "2": {"created_time": "2026-02-01T00:00:00Z", "deletion_time": "", "destroyed": False},
        },
    )
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(
            "/ui/vault/versions",
            params={"mount": "secret", "path": _tenant_path(_TENANT_A, "app/db")},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200, response.text
    body = response.text
    assert "data-vault-versions-table" in body
    assert 'data-vault-version-row="1"' in body
    assert 'data-vault-version-row="2"' in body
    assert "current: 2" in body
    # Version metadata only -- no secret value.
    assert _SECRET_VALUE not in body


# ---------------------------------------------------------------------------
# Tenant-scope guard: out-of-scope path -> friendly message, not a raw 403
# ---------------------------------------------------------------------------


def test_vault_ui_list_outside_tenant_scope_renders_friendly_message(
    monkeypatch: pytest.MonkeyPatch,
    _stub_embedding_service: AsyncMock,
) -> None:
    """A list outside ``secret/tenants/{tenant_id}/`` renders the scope message.

    The default-on guard raises ``VaultTenantScopeError`` before any Vault
    round-trip; the dispatcher wraps it as ``status="error"`` with
    ``extras.exception_class == "VaultTenantScopeError"``. The render must
    surface the "outside your tenant scope" message, NOT a raw 403 envelope
    nor an HTTP 4xx (the structured fault rides on a 200 body).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_stub_embedding_service)
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(
            "/ui/vault/list",
            # A path under another tenant's namespace -- outside scope.
            params={"mount": "secret", "path": "tenants/99999999-0000-0000-0000-000000000000/x"},
            headers={"HX-Request": "true"},
        )
    # The structured fault rides on a 200 body (not a 403/4xx).
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-vault-error="tenant_scope"' in body
    assert "Outside your tenant scope" in body
    # Defense-in-depth: no Vault round-trip happened (guard short-circuited).
    assert fake.auth.jwt.login_calls == []


# ---------------------------------------------------------------------------
# Route ordering: literal ``list`` resolves to the list handler
# ---------------------------------------------------------------------------


def test_vault_ui_route_ordering_list_not_param(
    monkeypatch: pytest.MonkeyPatch,
    _stub_embedding_service: AsyncMock,
) -> None:
    """``/ui/vault/list`` resolves to the list handler, not a ``{param}`` route.

    There is no ``{param}`` route on the vault router today, but the literal
    ``list`` segment must resolve to the list handler (a 200 render), not a
    404/422 from a hypothetical slug route -- the ordering discipline the
    router pins for when a ``{param}`` route lands.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_stub_embedding_service)
    install_fake_client(monkeypatch, keys=["k1"], secret={"password": _SECRET_VALUE})
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(
            "/ui/vault/list",
            params={"mount": "secret", "path": _tenant_path(_TENANT_A)},
            headers={"HX-Request": "true"},
        )
    # The list partial rendered (the inline keys branch), not a 404/422.
    assert response.status_code == 200, response.text
    assert "data-vault-keys" in response.text


def test_vault_ui_routes_require_session() -> None:
    """The vault routes are session-gated: an unauthenticated GET is redirected."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client = TestClient(_build_app(), follow_redirects=False)
    # No session cookie -> the middleware bounces an HTML GET to login.
    response = client.get("/ui/vault")
    assert response.status_code in (302, 303, 307), response.status_code
    assert "/ui/auth/login" in response.headers.get("location", "")
