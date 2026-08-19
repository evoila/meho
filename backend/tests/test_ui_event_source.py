# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the event_source admin UI (#2880).

Harness mirrors :mod:`tests.test_ui_conventions_write` (a real RSA-signed
``tenant_admin`` JWT lifted through the BFF session so
``resolve_operator_or_403`` passes). Covers the list render, the RBAC +
CSRF gates on writes, create (incl. Vault secret custody through the
reused REST handler), pause-via-edit, and soft-delete.
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
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import EventSource as EventSourceORM
from meho_backplane.db.models import Tenant
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import clear_discovery_cache, reset_verifier_store_for_testing
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    mint_csrf_token,
)
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing

from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OPERATOR_SUB = "op-alice"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
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


def _seed_tenant(tenant_id: uuid.UUID = _TENANT_A) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug="tenant-a", name="Tenant A"))

    asyncio.run(_do())


def _seed_event_source(
    *, slug: str, status: str = "active", tenant_id: uuid.UUID = _TENANT_A
) -> None:
    now = datetime.now(UTC)

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                EventSourceORM(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    name=f"src {slug}",
                    slug=slug,
                    kind="alertmanager",
                    auth_strategy="hmac-sha256",
                    secret_ref=None,
                    status=status,
                    extras={},
                    created_by_sub=_OPERATOR_SUB,
                    created_at=now,
                    updated_at=now,
                )
            )

    asyncio.run(_do())


def _get_row(slug: str) -> EventSourceORM | None:
    async def _do() -> EventSourceORM | None:
        from sqlalchemy import select

        async with get_sessionmaker()() as session:
            stmt = select(EventSourceORM).where(EventSourceORM.slug == slug)
            return (await session.execute(stmt)).scalar_one_or_none()

    return asyncio.run(_do())


def _seed_session(tenant_id: uuid.UUID, access_token: str) -> uuid.UUID:
    async def _do() -> uuid.UUID:
        async with get_sessionmaker()() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=_OPERATOR_SUB,
                tenant_id=tenant_id,
                access_token=access_token,
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    return asyncio.run(_do())


def _role_session(role: TenantRole) -> tuple[uuid.UUID, dict[str, Any]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = make_rsa_keypair("ui-event-source-test-kid")
    jwks = public_jwks(keypair)
    token = mint_token(keypair, sub=_OPERATOR_SUB, tenant_id=str(_TENANT_A), tenant_role=role.value)
    return _seed_session(_TENANT_A, token), jwks


def _client(session_id: uuid.UUID, jwks: dict[str, Any]) -> tuple[TestClient, respx.MockRouter]:
    mock = respx.mock(assert_all_called=False)
    mock.start()
    mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client, mock


def _csrf(session_id: uuid.UUID) -> dict[str, Any]:
    token = mint_csrf_token(str(session_id))
    return {"headers": {CSRF_HEADER_NAME: token}, "cookies": {CSRF_COOKIE_NAME: token}}


def test_list_page_renders_with_new_button_for_admin() -> None:
    _seed_tenant()
    _seed_event_source(slug="prod-am")
    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)
    client, mock = _client(session_id, jwks)
    try:
        resp = client.get("/ui/event-sources")
    finally:
        mock.stop()
    assert resp.status_code == 200, resp.text
    assert "Event Sources" in resp.text
    assert "prod-am" in resp.text
    assert "New event source" in resp.text


def test_list_operator_hides_write_affordances() -> None:
    _seed_tenant()
    _seed_event_source(slug="prod-am")
    session_id, jwks = _role_session(TenantRole.OPERATOR)
    client, mock = _client(session_id, jwks)
    try:
        resp = client.get("/ui/event-sources")
    finally:
        mock.stop()
    assert resp.status_code == 200
    assert "New event source" not in resp.text


def test_create_persists_and_redirects_with_secret_custody(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    async def _rec(operator: object, secret_ref: str, secret: object) -> None:
        calls.append((secret_ref, secret.get_secret_value()))  # type: ignore[attr-defined]

    monkeypatch.setattr("meho_backplane.api.v1.event_source.store_event_source_secret", _rec)
    _seed_tenant()
    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)
    client, mock = _client(session_id, jwks)
    try:
        resp = client.post(
            "/ui/event-sources",
            data={
                "name": "Prod Alertmanager",
                "slug": "prod-am",
                "kind": "alertmanager",
                "auth_strategy": "hmac-sha256",
                "status": "active",
                "extras": "",
                "secret": "hmac-signing-key",
            },
            **_csrf(session_id),
        )
    finally:
        mock.stop()
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/ui/event-sources"
    row = _get_row("prod-am")
    assert row is not None
    assert row.secret_ref == f"tenants/{_TENANT_A}/event-sources/prod-am"
    assert calls == [(f"tenants/{_TENANT_A}/event-sources/prod-am", "hmac-signing-key")]


def test_create_duplicate_slug_rerenders_error_no_redirect() -> None:
    _seed_tenant()
    _seed_event_source(slug="prod-am")
    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)
    client, mock = _client(session_id, jwks)
    try:
        resp = client.post(
            "/ui/event-sources",
            data={
                "name": "Another",
                "slug": "prod-am",
                "kind": "grafana",
                "auth_strategy": "basic",
                "status": "active",
                "extras": "",
                "secret": "",
            },
            **_csrf(session_id),
        )
    finally:
        mock.stop()
    assert resp.status_code == 200
    assert "location" not in resp.headers
    assert "data-write-error" in resp.text


def test_create_non_admin_gets_403() -> None:
    _seed_tenant()
    session_id, jwks = _role_session(TenantRole.OPERATOR)
    client, mock = _client(session_id, jwks)
    try:
        resp = client.post(
            "/ui/event-sources",
            data={
                "name": "N",
                "slug": "s",
                "kind": "alertmanager",
                "auth_strategy": "hmac-sha256",
                "status": "active",
                "extras": "",
                "secret": "",
            },
            **_csrf(session_id),
        )
    finally:
        mock.stop()
    assert resp.status_code == 403
    assert _get_row("s") is None


def test_create_missing_csrf_blocked() -> None:
    _seed_tenant()
    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)
    client, mock = _client(session_id, jwks)
    try:
        resp = client.post(
            "/ui/event-sources",
            data={
                "name": "N",
                "slug": "s",
                "kind": "alertmanager",
                "auth_strategy": "hmac-sha256",
                "status": "active",
                "extras": "",
                "secret": "",
            },
        )
    finally:
        mock.stop()
    assert resp.status_code == 403


def test_edit_pauses_source() -> None:
    _seed_tenant()
    _seed_event_source(slug="prod-am", status="active")
    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)
    client, mock = _client(session_id, jwks)
    try:
        resp = client.post(
            "/ui/event-sources/prod-am/edit",
            data={
                "kind": "alertmanager",
                "auth_strategy": "hmac-sha256",
                "status": "paused",
                "extras": "",
                "secret": "",
            },
            **_csrf(session_id),
        )
    finally:
        mock.stop()
    assert resp.status_code == 303
    row = _get_row("prod-am")
    assert row is not None and row.status == "paused"


def test_delete_soft_removes_source() -> None:
    _seed_tenant()
    _seed_event_source(slug="prod-am")
    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)
    client, mock = _client(session_id, jwks)
    try:
        resp = client.post("/ui/event-sources/prod-am/delete", **_csrf(session_id))
    finally:
        mock.stop()
    assert resp.status_code == 303
    row = _get_row("prod-am")
    assert row is not None and row.deleted_at is not None
