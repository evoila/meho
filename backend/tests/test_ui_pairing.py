# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``/ui/pairing`` console surface (#3025).

Acceptance:

* The registry is session-authenticated; an unauthenticated request 302s to
  the BFF login.
* ``GET /ui/pairing`` renders every active pairing in the tenant with its
  negotiated contract version and a live contract-compatibility badge.
* A pairing whose add-on out-requires this backplane renders ``incompatible``
  (the both-direction skew re-evaluation, surfaced in the console).
* The surface is tenant-scoped and read-only (no pair / unpair controls).

Harness mirrors :mod:`tests.test_ui_sensors`: a minimal FastAPI app wired with
the UI session + CSRF middlewares and a ``web_session`` row carrying a real
Keycloak-minted token, so ``require_ui_session`` re-verifies end-to-end.
Pairings are seeded directly as ORM rows — the read path is a pure DB read.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import timedelta

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AddonPairing, Tenant
from meho_backplane.operations.addon_pairing_contract import BACKPLANE_CONTRACT_VERSION
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import clear_discovery_cache, reset_verifier_store_for_testing
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing

from ._oidc_jwt_helpers import AUDIENCE as _DEFAULT_AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _DEFAULT_ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
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


def _seed_pairing(
    *,
    tenant_id: uuid.UUID,
    name: str,
    addon_min_backplane_version: int = BACKPLANE_CONTRACT_VERSION,
) -> None:
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AddonPairing(
                    tenant_id=tenant_id,
                    name=name,
                    keycloak_client_id=f"addon:{name}",
                    keycloak_internal_id="kc-internal",
                    owner_sub="op-admin",
                    contract_version=BACKPLANE_CONTRACT_VERSION,
                    addon_contract_version=BACKPLANE_CONTRACT_VERSION,
                    addon_min_backplane_version=addon_min_backplane_version,
                    created_by_sub="op-admin",
                )
            )

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


def _client(tenant_id: uuid.UUID = _TENANT_A) -> tuple[TestClient, respx.MockRouter]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-pairing-test-kid")
    jwks = _public_jwks(keypair)
    access_token = _mint_token(
        keypair, sub="op-operator", tenant_id=str(tenant_id), tenant_role=TenantRole.OPERATOR.value
    )
    session_id = _seed_session_sync(
        tenant_id=tenant_id, access_token=access_token, operator_sub="op-operator"
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client, mock


def test_list_unauthenticated_redirects_to_login() -> None:
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/pairing")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_list_renders_paired_addon_with_compatibility() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_pairing(tenant_id=_TENANT_A, name="automation")
    client, mock = _client()
    try:
        response = client.get("/ui/pairing")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "Pairing" in body
    assert "automation" in body
    assert f"v{BACKPLANE_CONTRACT_VERSION}" in body
    assert "compatible" in body


def test_list_flags_incompatible_pairing() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_pairing(
        tenant_id=_TENANT_A,
        name="ssp",
        addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION + 1,
    )
    client, mock = _client()
    try:
        response = client.get("/ui/pairing")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "incompatible" in response.text


def test_empty_state_when_no_pairings() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock = _client()
    try:
        response = client.get("/ui/pairing")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "No add-ons paired" in response.text
