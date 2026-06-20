# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Vault console WRITE UI surface.

Initiative #1942 (G10.18 Vault / secrets console), Task #1957 (T2). The
confirm-gated KV write verbs over the T1 (#1956) read-only browser:
``vault.kv.put`` (CAS-aware), ``vault.kv.delete`` (soft-delete versions),
and ``secret.move`` (references-only, a SEPARATE ``secret-broker-1.x``
connector). The acceptance criteria on issue #1957 are:

* a confirmed ``POST /ui/vault/put`` whose dispatch returns
  ``status="awaiting_approval"`` renders the approval banner with
  ``extras["approval_request_id"]`` AND a link to ``/ui/approvals`` -- the
  operator is NEVER shown a silent / empty success.
* the ``vault.kv.delete`` confirm modal renders the ``dangerous`` /
  ``requires_approval`` banner; ``POST /ui/vault/delete`` without the CSRF
  token -> 403, with it -> 200; the literal ``GET /ui/vault/put/confirm``
  resolves to the confirm handler, not a T1 ``{param}`` route.
* ``POST /ui/vault/put`` carries ``cas`` in the dispatched params ONLY when
  the operator set it (the CLI ``Changed("cas")`` rule).
* ``POST /ui/vault/move`` dispatches ``connector_id="secret-broker-1.x"`` op
  ``secret.move`` with ``from`` / ``to`` references and NO value field; the
  rendered result contains only ``status`` / ``value_sha256`` / ``length``;
  the move form has no value/secret input.
* a valid **operator** session can run all three writes; no tenant_admin
  hard-403 on the write POST; tenant scoping derives from the session.

The harness mirrors :mod:`backend.tests.test_ui_vault`: a minimal FastAPI app
with the UI session + CSRF middlewares, a ``web_session`` row carrying a real
Keycloak-minted access token, seeded ``operation_group`` / ``endpoint_descriptor``
rows so the pickers resolve ``vault-1.x`` + ``secret-broker-1.x`` as ingested,
the registered vault + secret-broker typed ops, and the shared in-process
Vault fake. The awaiting_approval branches drive the REAL dispatch (the policy
gate parks a ``requires_approval`` op for a human OPERATOR, G11.7-T1 #1401);
the argument-shape branches patch the route module's ``call_operation`` to
capture the dispatched arguments (mirroring ``test_ui_operations``).
"""

from __future__ import annotations

import asyncio
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

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.connectors.vault.ops import register_vault_typed_operations
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, Tenant
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
from tests._vault_fakes import install_fake_client

_BACKPLANE_URL = "https://meho.test"
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OP_OPERATOR = "op-operator"

#: A secret-value sentinel: no write render may carry it, and a move's
#: value-free response must never echo it.
_SECRET_VALUE = "SUPER-SECRET-VALUE-do-not-leak-9f3a"

_FROM_REF = "vault:secret/db/prod#password"
_TO_REF = "vault:secret/db/replica#password"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF + Vault env vars (mirrors the vault UI suite)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    # Default-on tenant scope (the v0.15.0 #1725 production default).
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
    """Construct a minimal FastAPI app wired for the vault write UI tests."""
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
    so the put / delete pickers + connector-id resolver find it. The actual
    dispatch goes through the registered typed ops, not this row.
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
                    op_id="vault.kv.put",
                    source_kind="typed",
                    method="PUT",
                    path="/v1/secret/data",
                    summary="Write a KV secret.",
                    description="writes a secret value",
                    group_id=group_id,
                    parameter_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                    llm_instructions=None,
                    safety_level="caution",
                    requires_approval=True,
                    is_enabled=True,
                ),
            )

    asyncio.run(_do())


def _seed_secret_broker_descriptor() -> None:
    """Seed an enabled ``secret-broker`` ``operation_group`` + descriptor.

    Makes the connector listing report ``secret-broker-1.x`` as
    ``state="ingested"`` so the move connector-id resolver finds it. The
    actual dispatch goes through the registered ``secret.move`` typed op.
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
                    product="secret",
                    version="1.x",
                    impl_id="secret-broker",
                    group_key="broker",
                    name="Secret broker",
                    when_to_use="move a credential between stores",
                    review_status="enabled",
                ),
            )
            session.add(
                EndpointDescriptor(
                    id=desc_id,
                    tenant_id=None,
                    product="secret",
                    version="1.x",
                    impl_id="secret-broker",
                    op_id="secret.move",
                    source_kind="typed",
                    method="POST",
                    path="/secret/move",
                    summary="Move a credential.",
                    description="moves a credential value-free",
                    group_id=group_id,
                    parameter_schema={
                        "type": "object",
                        "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
                    },
                    llm_instructions=None,
                    safety_level="dangerous",
                    requires_approval=True,
                    is_enabled=True,
                ),
            )

    asyncio.run(_do())


def _register_vault(stub_embedding_service: AsyncMock) -> None:
    """Register the vault v2 connector + upsert the typed-op descriptors."""
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)

    async def _do() -> None:
        await register_vault_typed_operations(embedding_service=stub_embedding_service)

    asyncio.run(_do())


def _register_secret_broker(stub_embedding_service: AsyncMock) -> None:
    """Upsert the ``secret.move`` typed op (the synthetic broker connector).

    ``secret-broker`` registers neither ``register_connector`` nor
    ``register_connector_v2`` -- it is a typed-op-only synthetic connector the
    dispatcher resolves from the registered op alone.
    """

    async def _do() -> None:
        await register_secret_broker_operations(embedding_service=stub_embedding_service)

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
        keypair = _make_rsa_keypair(f"ui-vault-writes-test-kid-{uuid.uuid4().hex[:8]}")
    return keypair, _public_jwks(keypair)


def _client_with_role_and_csrf(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + respx mock + minted CSRF token for the write routes.

    Sets BOTH the session cookie and the ``meho_csrf`` double-submit cookie so
    a state-changing POST carrying the matching ``X-CSRF-Token`` header passes
    the middleware (mirrors ``test_ui_operations._client_with_role_and_csrf``).
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
    csrf_token = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _csrf_headers(token: str) -> dict[str, str]:
    """Headers for an HTMX state-changing request -- CSRF + HX-Request."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


def _tenant_path(tenant_id: uuid.UUID, suffix: str = "") -> str:
    """The in-scope KV path for *tenant_id* under the default-on guard."""
    base = f"tenants/{tenant_id}"
    return f"{base}/{suffix}" if suffix else base


def _patch_call_operation(
    monkeypatch: pytest.MonkeyPatch, envelope: dict[str, Any]
) -> list[dict[str, Any]]:
    """Patch the write route module's ``call_operation`` to return *envelope*.

    Records every ``arguments`` dict so a test can assert the BFF threaded the
    connector_id / op_id / target / params (incl. the CAS rule + the value-free
    move shape) through unchanged. The dispatch contract itself is covered by
    the connector suites; these BFF tests verify route wiring + render branches.
    """
    received: list[dict[str, Any]] = []

    async def _fake_call_operation(operator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        received.append(arguments)
        return envelope

    monkeypatch.setattr(
        "meho_backplane.ui.routes.vault.writes.call_operation",
        _fake_call_operation,
    )
    return received


def _awaiting_envelope(op_id: str) -> dict[str, Any]:
    """A canonical ``awaiting_approval`` envelope with a pending-row id."""
    return {
        "status": "awaiting_approval",
        "op_id": op_id,
        "result": None,
        "error": f"awaiting_approval: {op_id!r} requires approval before execution",
        "duration_ms": 3.0,
        "handle": None,
        "extras": {
            "error_code": "awaiting_approval",
            "approval_request_id": str(uuid.uuid4()),
        },
    }


# ---------------------------------------------------------------------------
# Confirm modals: unmissable safety banner + CSRF re-mint
# ---------------------------------------------------------------------------


def test_vault_put_confirm_renders_caution_requires_approval_banner(
    _stub_embedding_service: AsyncMock,
) -> None:
    """``GET /ui/vault/put/confirm`` renders the caution / requires-approval banner."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_stub_embedding_service)
    client, mock, _csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(
            "/ui/vault/put/confirm",
            params={"mount": "secret", "path": _tenant_path(_TENANT_A, "app")},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-vault-write-gate="confirm"' in body
    assert 'data-safety-level="caution"' in body
    assert "data-vault-write-requires-approval" in body
    # The confirm POST carries its own CSRF header echo + disables itself.
    assert 'hx-post="/ui/vault/put"' in body
    assert "X-CSRF-Token" in body
    assert "hx-disabled-elt" in body
    # The path defaulted from the supplied value (prefilled).
    assert _tenant_path(_TENANT_A, "app") in body
    # A fresh CSRF cookie was re-set on the modal render (cookie-desync defence).
    assert CSRF_COOKIE_NAME in response.headers.get("set-cookie", "")


def test_vault_delete_confirm_renders_dangerous_requires_approval_banner(
    _stub_embedding_service: AsyncMock,
) -> None:
    """``GET /ui/vault/delete/confirm`` renders the dangerous / requires-approval banner.

    (Acceptance criterion: the delete confirm modal renders the dangerous /
    requires_approval banner.)
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_stub_embedding_service)
    client, mock, _csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(
            "/ui/vault/delete/confirm",
            params={"mount": "secret", "path": _tenant_path(_TENANT_A, "app")},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-vault-write-gate="confirm"' in body
    assert 'data-safety-level="dangerous"' in body
    assert "data-vault-write-requires-approval" in body
    assert 'hx-post="/ui/vault/delete"' in body


def test_vault_move_confirm_renders_value_free_form_no_value_input(
    _stub_embedding_service: AsyncMock,
) -> None:
    """``GET /ui/vault/move/confirm`` renders from/to refs + reason, NO value input.

    (Acceptance criterion, part: assert the move form has no value/secret
    input.)
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_secret_broker_descriptor()
    _register_secret_broker(_stub_embedding_service)
    client, mock, _csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get("/ui/vault/move/confirm", headers={"HX-Request": "true"})
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-safety-level="dangerous"' in body
    assert 'hx-post="/ui/vault/move"' in body
    # Reference inputs + reason are present.
    assert 'name="from"' in body
    assert 'name="to"' in body
    assert 'name="reason"' in body
    # NO value / secret input field exists (the no-``--value`` invariant).
    assert 'name="value"' not in body
    assert 'name="secret"' not in body
    assert 'name="data"' not in body
    assert "references only" in body.lower()


# ---------------------------------------------------------------------------
# awaiting_approval: the silent-success trap (acceptance criterion)
# ---------------------------------------------------------------------------


def test_vault_put_awaiting_approval_renders_banner_and_approvals_link() -> None:
    """A confirmed put parking at ``awaiting_approval`` surfaces the banner + link.

    (Acceptance criterion #1.) End-to-end through the REAL dispatch: a human
    OPERATOR running a ``requires_approval`` op is routed to the approval queue
    (G11.7-T1 #1401), which returns ``status="awaiting_approval"`` with
    ``extras["approval_request_id"]``. The fragment must surface that id with a
    deep-link to ``/ui/approvals`` -- NEVER a silent empty success.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    # Register the connector + ops + the in-process Vault fake so the parked
    # dispatch reaches the real policy gate (the handler never runs on a park).
    with pytest.MonkeyPatch.context() as mp:
        install_fake_client(mp, secret={"password": _SECRET_VALUE})
        _register_vault(_make_stub())
        client, mock, csrf = _client_with_role_and_csrf(
            tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
        )
        with mock:
            response = client.post(
                "/ui/vault/put",
                headers=_csrf_headers(csrf),
                data={
                    "target": "",
                    "mount": "secret",
                    "path": _tenant_path(_TENANT_A, "app/db"),
                    "data": '{"password": "new-value"}',
                },
            )
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-vault-write-status="awaiting_approval"' in body
    assert "data-approval-request-id" in body
    assert "/ui/approvals/" in body
    assert "data-approval-deep-link" in body
    # The operator is NOT shown a silent success.
    assert 'data-vault-write-status="ok"' not in body


def test_vault_move_awaiting_approval_renders_banner_and_approvals_link(
    _stub_embedding_service: AsyncMock,
) -> None:
    """A confirmed move parking at ``awaiting_approval`` surfaces the banner + link.

    The real ``secret.move`` change-class gate parks a USER operator's move at
    ``awaiting_approval`` (the handler never runs). The render must surface the
    approval handoff, never a silent success.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_secret_broker_descriptor()
    _register_secret_broker(_stub_embedding_service)
    with pytest.MonkeyPatch.context() as mp:
        install_fake_client(mp, secret={"password": _SECRET_VALUE})
        client, mock, csrf = _client_with_role_and_csrf(
            tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
        )
        with mock:
            response = client.post(
                "/ui/vault/move",
                headers=_csrf_headers(csrf),
                data={"from": _FROM_REF, "to": _TO_REF, "reason": "promote replica"},
            )
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-vault-write-status="awaiting_approval"' in body
    assert "/ui/approvals/" in body
    # No secret value reaches the render.
    assert _SECRET_VALUE not in body


# ---------------------------------------------------------------------------
# CSRF gate + route ordering (acceptance criterion)
# ---------------------------------------------------------------------------


def test_vault_delete_post_without_csrf_token_is_403(
    _stub_embedding_service: AsyncMock,
) -> None:
    """``POST /ui/vault/delete`` WITHOUT the CSRF token is rejected (403).

    (Acceptance criterion, part: without CSRF -> 403, with it -> 200.)
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_stub_embedding_service)
    client, mock, _csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        # Drop the CSRF cookie and omit the header so the double-submit fails.
        client.cookies.delete(CSRF_COOKIE_NAME)
        response = client.post(
            "/ui/vault/delete",
            headers={"HX-Request": "true"},
            data={
                "mount": "secret",
                "path": _tenant_path(_TENANT_A, "app/db"),
                "versions": "3,4",
            },
        )
    assert response.status_code == 403, response.text


def test_vault_delete_post_with_csrf_token_is_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /ui/vault/delete`` WITH the CSRF token dispatches + renders (200).

    (Acceptance criterion, part.) The dispatch is patched to a canonical
    awaiting_approval envelope so the assertion isolates the CSRF + render
    wiring from the connector.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_make_stub())
    _patch_call_operation(monkeypatch, _awaiting_envelope("vault.kv.delete"))
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/vault/delete",
            headers=_csrf_headers(csrf),
            data={
                "mount": "secret",
                "path": _tenant_path(_TENANT_A, "app/db"),
                "versions": "3,4",
            },
        )
    assert response.status_code == 200, response.text
    assert 'data-vault-write-status="awaiting_approval"' in response.text


def test_vault_put_confirm_route_resolves_to_confirm_handler_not_param(
    _stub_embedding_service: AsyncMock,
) -> None:
    """``GET /ui/vault/put/confirm`` resolves to the confirm handler, not a T1 param route.

    (Acceptance criterion, part: first-match-wins -- literals before params.)
    The literal ``put/confirm`` segment renders the confirm modal (200), not a
    404/422 from a hypothetical T1 ``{param}`` route on the shared router.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_stub_embedding_service)
    client, mock, _csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get("/ui/vault/put/confirm", headers={"HX-Request": "true"})
    assert response.status_code == 200, response.text
    assert 'hx-post="/ui/vault/put"' in response.text


# ---------------------------------------------------------------------------
# CAS rule: carried only when the operator set it (acceptance criterion)
# ---------------------------------------------------------------------------


def test_vault_put_carries_cas_only_when_operator_set_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST /ui/vault/put`` includes ``cas`` in params ONLY when set.

    (Acceptance criterion #3 -- the CLI ``Changed("cas")`` rule.) Two dispatches
    against the same patched ``call_operation``: one with an explicit CAS, one
    omitting it. The dispatched params carry ``cas`` only in the first.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_make_stub())
    received = _patch_call_operation(monkeypatch, _awaiting_envelope("vault.kv.put"))
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        # (a) explicit CAS=0 (must-not-exist).
        client.post(
            "/ui/vault/put",
            headers=_csrf_headers(csrf),
            data={
                "mount": "secret",
                "path": _tenant_path(_TENANT_A, "app/db"),
                "data": '{"password": "v"}',
                "cas": "0",
            },
        )
        # (b) CAS field omitted entirely (write unconditionally).
        client.post(
            "/ui/vault/put",
            headers=_csrf_headers(csrf),
            data={
                "mount": "secret",
                "path": _tenant_path(_TENANT_A, "app/db"),
                "data": '{"password": "v"}',
            },
        )
    assert len(received) == 2
    with_cas, without_cas = received
    # (a) carries cas (explicit 0, the must-not-exist guard).
    assert with_cas["params"]["cas"] == 0
    assert with_cas["params"]["data"] == {"password": "v"}
    assert with_cas["connector_id"] == "vault-1.x"
    assert with_cas["op_id"] == "vault.kv.put"
    # (b) carries NO cas key -- an unset field is "flag absent", not cas=0.
    assert "cas" not in without_cas["params"]


def test_vault_put_blank_cas_is_unconditional_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace/blank CAS field is "flag absent", not ``cas=0``."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_make_stub())
    received = _patch_call_operation(monkeypatch, _awaiting_envelope("vault.kv.put"))
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        client.post(
            "/ui/vault/put",
            headers=_csrf_headers(csrf),
            data={
                "mount": "secret",
                "path": _tenant_path(_TENANT_A, "app/db"),
                "data": '{"password": "v"}',
                "cas": "   ",
            },
        )
    assert len(received) == 1
    assert "cas" not in received[0]["params"]


# ---------------------------------------------------------------------------
# secret.move value-free contract (acceptance criterion #4)
# ---------------------------------------------------------------------------


def test_vault_move_dispatches_secret_broker_refs_only_no_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST /ui/vault/move`` dispatches secret-broker-1.x with refs + NO value.

    (Acceptance criterion #4.) The dispatched params carry ``from`` / ``to``
    (+ reason) and NO value field; the result renders only ``status`` /
    ``value_sha256`` / ``length`` and no secret value text.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_secret_broker_descriptor()
    _register_secret_broker(_make_stub())
    moved_envelope = {
        "status": "ok",
        "op_id": "secret.move",
        "result": {
            "status": "moved",
            "value_sha256": "a" * 64,
            "length": 21,
        },
        "error": None,
        "duration_ms": 9.0,
        "handle": None,
        "extras": {},
    }
    received = _patch_call_operation(monkeypatch, moved_envelope)
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/vault/move",
            headers=_csrf_headers(csrf),
            # A smuggled ``value`` field must NOT reach the dispatched params.
            data={
                "from": _FROM_REF,
                "to": _TO_REF,
                "reason": "promote replica",
                "value": _SECRET_VALUE,
            },
        )
    assert response.status_code == 200, response.text
    # The dispatched connector + op + params are references only.
    assert len(received) == 1
    args = received[0]
    assert args["connector_id"] == "secret-broker-1.x"
    assert args["op_id"] == "secret.move"
    assert args["params"] == {"from": _FROM_REF, "to": _TO_REF, "reason": "promote replica"}
    # No value field was forwarded, even though one was smuggled into the form.
    assert "value" not in args["params"]
    assert _SECRET_VALUE not in str(args["params"])
    # The render carries only the value-free triple.
    body = response.text
    assert "data-vault-move-result" in body
    assert "data-move-value-sha256" in body
    assert "data-move-length" in body
    assert "a" * 64 in body
    assert _SECRET_VALUE not in body


def test_vault_move_missing_from_or_to_is_inline_form_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A move with a blank ``from`` or ``to`` renders an inline 400 form error.

    No dispatch happens (the schema requires both references).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_secret_broker_descriptor()
    _register_secret_broker(_make_stub())
    received = _patch_call_operation(monkeypatch, _awaiting_envelope("secret.move"))
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/vault/move",
            headers=_csrf_headers(csrf),
            data={"from": _FROM_REF, "to": "", "reason": "x"},
        )
    assert response.status_code == 400, response.text
    assert 'data-vault-write-status="form_error"' in response.text
    # The op was never dispatched.
    assert received == []


# ---------------------------------------------------------------------------
# RBAC: operator-tier, no tenant_admin hard-403 (acceptance criterion #5)
# ---------------------------------------------------------------------------


def test_vault_writes_operator_tier_no_tenant_admin_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain OPERATOR session can run all three writes -- no tenant_admin 403.

    (Acceptance criterion #5.) The gate is the policy/approval gate, not a role
    tier; the write POSTs carry NO ``tenant_admin`` hard-403. Tenant scoping is
    derived from the session, never a form field.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _seed_secret_broker_descriptor()
    _register_vault(_make_stub())
    _register_secret_broker(_make_stub())
    # Patch both dispatch surfaces (vault + secret-broker share the writes
    # module's ``call_operation``) to a canonical awaiting_approval envelope.
    _patch_call_operation(monkeypatch, _awaiting_envelope("vault.kv.put"))
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        put_resp = client.post(
            "/ui/vault/put",
            headers=_csrf_headers(csrf),
            data={
                "mount": "secret",
                "path": _tenant_path(_TENANT_A, "app"),
                "data": '{"k": "v"}',
            },
        )
        delete_resp = client.post(
            "/ui/vault/delete",
            headers=_csrf_headers(csrf),
            data={"mount": "secret", "path": _tenant_path(_TENANT_A, "app"), "versions": "1"},
        )
        move_resp = client.post(
            "/ui/vault/move",
            headers=_csrf_headers(csrf),
            data={"from": _FROM_REF, "to": _TO_REF},
        )
    # None of the three is a 403 -- the operator runs every write.
    assert put_resp.status_code == 200, put_resp.text
    assert delete_resp.status_code == 200, delete_resp.text
    assert move_resp.status_code == 200, move_resp.text


# ---------------------------------------------------------------------------
# Tenant-scope guard: out-of-scope write -> friendly message, not a raw 403
# ---------------------------------------------------------------------------


def test_vault_put_tenant_scope_error_renders_friendly_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A put whose dispatch returns a ``VaultTenantScopeError`` fault renders the scope message.

    The default-on guard (``connectors/vault/tenant_scope.py``) raises
    ``VaultTenantScopeError`` when an APPROVED write re-dispatches against a
    path outside ``secret/tenants/{tenant_id}/``; the dispatcher wraps it as
    ``status="error"`` with ``extras.exception_class == "VaultTenantScopeError"``.
    The BFF render must surface the "outside your tenant scope" message, NOT a
    raw 403 (the structured fault rides on a 200 body). The dispatch is patched
    to that error shape so the assertion isolates the BFF's render branch (the
    real guard's behaviour is covered by the connector suites).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_make_stub())
    scope_error_envelope = {
        "status": "error",
        "op_id": "vault.kv.put",
        "result": None,
        "error": "secret/tenants/99999999.../x is outside the tenant scope",
        "duration_ms": 1.0,
        "handle": None,
        "extras": {
            "error_code": "connector_error",
            "exception_class": "VaultTenantScopeError",
        },
    }
    _patch_call_operation(monkeypatch, scope_error_envelope)
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/vault/put",
            headers=_csrf_headers(csrf),
            data={
                "mount": "secret",
                "path": "tenants/99999999-0000-0000-0000-000000000000/x",
                "data": '{"k": "v"}',
            },
        )
    assert response.status_code == 200, response.text
    assert 'data-vault-error="tenant_scope"' in response.text
    assert "Outside your tenant scope" in response.text


# ---------------------------------------------------------------------------
# Malformed-input inline errors (no 422; the operator stays in the modal)
# ---------------------------------------------------------------------------


def test_vault_put_malformed_data_json_is_inline_form_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A put with malformed ``data`` JSON renders an inline 400 form error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_make_stub())
    received = _patch_call_operation(monkeypatch, _awaiting_envelope("vault.kv.put"))
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/vault/put",
            headers=_csrf_headers(csrf),
            data={
                "mount": "secret",
                "path": _tenant_path(_TENANT_A, "app"),
                "data": "{not json",
            },
        )
    assert response.status_code == 400, response.text
    assert 'data-vault-write-status="form_error"' in response.text
    assert received == []


def test_vault_delete_empty_versions_is_inline_form_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delete with an empty ``versions`` field renders an inline 400 form error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(_make_stub())
    received = _patch_call_operation(monkeypatch, _awaiting_envelope("vault.kv.delete"))
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/vault/delete",
            headers=_csrf_headers(csrf),
            data={"mount": "secret", "path": _tenant_path(_TENANT_A, "app"), "versions": ""},
        )
    assert response.status_code == 400, response.text
    assert 'data-vault-write-status="form_error"' in response.text
    assert received == []


# ---------------------------------------------------------------------------
# Session gate (the writes are session-gated like the reads)
# ---------------------------------------------------------------------------


def test_vault_write_confirm_requires_session() -> None:
    """An unauthenticated GET on a write confirm route is redirected to login."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client = TestClient(_build_app(), follow_redirects=False)
    response = client.get("/ui/vault/put/confirm")
    assert response.status_code in (302, 303, 307), response.status_code
    assert "/ui/auth/login" in response.headers.get("location", "")


def _make_stub() -> AsyncMock:
    """A deterministic embedding stub (module-level helper for non-fixture use)."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service
