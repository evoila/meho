# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``/ui/checks`` console surface (#2506).

Initiative #2416 (parent goal #221), Task #2506. Acceptance:

* List / detail are operator-readable (session-authenticated); an
  unauthenticated request 302s to the BFF login.
* The list renders the dashboard name + five-state badge + member count; an
  ``HX-Request`` returns only the rows fragment; the full page arms the 30s
  auto-refresh.
* The detail renders the member table with each member's states; a
  cross-tenant / absent id is 404.

Harness mirrors :mod:`tests.test_ui_scheduler`: a minimal FastAPI app wired
with the UI session + CSRF middlewares and a ``web_session`` row carrying a
real Keycloak-minted access token, so ``require_ui_session`` re-verifies the
session end-to-end.
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
from meho_backplane.checks.dashboard_schemas import DashboardCreate
from meho_backplane.checks.dashboard_service import CheckDashboardAdminService
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Sensor, Tenant
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

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

_OP_OPERATOR = "op-operator"

_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the scheduler suite)."""
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


def _seed_sensor(*, tenant_id: uuid.UUID, name: str, last_state: str = "ok") -> uuid.UUID:
    now = datetime.now(UTC)
    sensor_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                Sensor(
                    id=sensor_id,
                    tenant_id=tenant_id,
                    name=name,
                    connector_id="vmware-rest-9.0",
                    op_id="vmware.vm.list",
                    target=None,
                    params={},
                    assertion=_ASSERTION,
                    status="active",
                    cadence_kind="interval",
                    interval_seconds=60,
                    cron_expr=None,
                    timezone="UTC",
                    next_fire_at=now + timedelta(seconds=60),
                    severity="critical",
                    for_seconds=0,
                    last_state=last_state,
                    last_value=3,
                    last_evidence={"observed": 3},
                    last_evaluated_at=now - timedelta(seconds=30),
                    state_since=now - timedelta(hours=1),
                    identity_sub="__sensor__",
                    created_by_sub="op-admin",
                )
            )

    asyncio.run(_do())
    return sensor_id


def _seed_dashboard(*, tenant_id: uuid.UUID, name: str, sensor_ids: list[uuid.UUID]) -> uuid.UUID:
    async def _do() -> uuid.UUID:
        service = CheckDashboardAdminService()
        detail = await service.create(
            tenant_id=tenant_id,
            created_by_sub="op-admin",
            payload=DashboardCreate(name=name, sensor_ids=sensor_ids),
        )
        return detail.id

    return asyncio.run(_do())


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


def _client_with_role(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = _OP_OPERATOR,
    role: TenantRole = TenantRole.OPERATOR,
) -> tuple[TestClient, respx.MockRouter]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-checks-test-kid")
    jwks = _public_jwks(keypair)
    access_token = _mint_token(
        keypair, sub=operator_sub, tenant_id=str(tenant_id), tenant_role=role.value
    )
    session_id = _seed_session_sync(
        tenant_id=tenant_id, access_token=access_token, operator_sub=operator_sub
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client, mock


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_list_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/checks`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/checks")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# GET /ui/checks -- list
# ---------------------------------------------------------------------------


def test_list_renders_for_operator() -> None:
    """An operator reads the list; the dashboard name, state badge, and count render."""
    _seed_tenant(_TENANT_A, "tenant-a")
    sid = _seed_sensor(tenant_id=_TENANT_A, name="disk", last_state="critical")
    _seed_dashboard(tenant_id=_TENANT_A, name="prod-health", sensor_ids=[sid])
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/checks")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "Checks" in body
    assert "prod-health" in body
    assert "critical" in body  # the rolled-up state badge


def test_list_full_page_arms_auto_refresh() -> None:
    """The full page arms the 30s auto-refresh poll on the table."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/checks")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert 'hx-trigger="every 30s"' in response.text


def test_list_htmx_request_returns_fragment_not_full_page() -> None:
    """An ``HX-Request`` GET returns the table-rows fragment (no chrome)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    sid = _seed_sensor(tenant_id=_TENANT_A, name="disk")
    _seed_dashboard(tenant_id=_TENANT_A, name="prod-health", sensor_ids=[sid])
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/checks", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert 'id="checks-table-body"' in response.text
    assert "<!doctype html>" not in response.text.lower()


def test_list_is_tenant_isolated() -> None:
    """A dashboard in tenant B never appears in tenant A's list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_dashboard(tenant_id=_TENANT_B, name="b-only", sensor_ids=[])
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/checks")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "b-only" not in response.text


# ---------------------------------------------------------------------------
# GET /ui/checks/{id} -- detail
# ---------------------------------------------------------------------------


def test_detail_renders_member_table() -> None:
    """The detail page renders the rollup + member states."""
    _seed_tenant(_TENANT_A, "tenant-a")
    sid = _seed_sensor(tenant_id=_TENANT_A, name="disk-space", last_state="degraded")
    dash_id = _seed_dashboard(tenant_id=_TENANT_A, name="prod-health", sensor_ids=[sid])
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get(f"/ui/checks/{dash_id}")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "prod-health" in body
    assert "disk-space" in body  # member sensor name
    assert "vmware.vm.list" in body  # member op id
    assert "degraded" in body  # the member + rollup badge


def test_detail_cross_tenant_is_404() -> None:
    """A dashboard id belonging to another tenant is 404 (no existence leak)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    other = _seed_dashboard(tenant_id=_TENANT_B, name="b-secret", sensor_ids=[])
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get(f"/ui/checks/{other}")
    finally:
        mock.stop()
    assert response.status_code == 404, response.text
