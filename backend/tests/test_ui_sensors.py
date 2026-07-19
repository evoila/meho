# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``/ui/sensors`` console surface (#2591).

Initiative #2416 (parent goal #221), Task #2591. Acceptance:

* The registry is operator-readable (session-authenticated); an
  unauthenticated request 302s to the BFF login.
* ``GET /ui/sensors`` renders **all** tenant Sensors, including ones on no
  Dashboard (the gap the page exists to close) -- with each Sensor's
  latest-result projection (``last_state`` badge / ``last_value`` /
  ``last_evaluated_at``).
* ``?status=`` and ``?cadence_kind=`` filter server-side via the existing
  list-route filters, with an HTMX partial swap; a stale / unknown filter value
  renders unfiltered rather than erroring.
* ``last_state`` renders with the existing five-state badge classes.
* The surface is tenant-scoped (a foreign tenant's Sensors never render) and
  read-only (no create / delete controls).

Harness mirrors :mod:`tests.test_ui_checks`: a minimal FastAPI app wired with
the UI session + CSRF middlewares and a ``web_session`` row carrying a real
Keycloak-minted access token, so ``require_ui_session`` re-verifies the session
end-to-end. Sensors are seeded directly as ORM rows -- the registry read path
(``SensorAdminService.list_``) is a pure DB read that needs no connector /
descriptor resolution.
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
    """Pin chassis + BFF env vars for every test (mirrors the checks suite)."""
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


def _seed_sensor(
    *,
    tenant_id: uuid.UUID,
    name: str,
    status: str = "active",
    cadence_kind: str = "interval",
    last_state: str = "ok",
    last_value: Any = 3,
    op_id: str = "vmware.vm.list",
    evaluated: bool = True,
) -> uuid.UUID:
    """Insert a Sensor ORM row directly (no connector / descriptor round-trip)."""
    now = datetime.now(UTC)
    sensor_id = uuid.uuid4()
    interval_seconds = 60 if cadence_kind == "interval" else None
    cron_expr = None if cadence_kind == "interval" else "0 * * * *"

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                Sensor(
                    id=sensor_id,
                    tenant_id=tenant_id,
                    name=name,
                    connector_id="vmware-rest-9.0",
                    op_id=op_id,
                    target=None,
                    params={},
                    assertion=_ASSERTION,
                    status=status,
                    cadence_kind=cadence_kind,
                    interval_seconds=interval_seconds,
                    cron_expr=cron_expr,
                    timezone="UTC",
                    next_fire_at=now + timedelta(seconds=60),
                    severity="critical",
                    for_seconds=0,
                    last_state=last_state,
                    last_value=last_value if evaluated else None,
                    last_evidence={"observed": last_value} if evaluated else None,
                    last_evaluated_at=(now - timedelta(seconds=30)) if evaluated else None,
                    state_since=(now - timedelta(hours=1)) if evaluated else None,
                    identity_sub="__sensor__",
                    created_by_sub="op-admin",
                )
            )

    asyncio.run(_do())
    return sensor_id


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
        keypair = _make_rsa_keypair("ui-sensors-test-kid")
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
    """``GET /ui/sensors`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/sensors")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# GET /ui/sensors -- list + latest-result projection
# ---------------------------------------------------------------------------


def test_list_renders_registry_with_latest_result() -> None:
    """An operator reads the registry; the name + op + latest-result projection render."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_sensor(tenant_id=_TENANT_A, name="vm-count", last_state="ok", last_value=42)
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/sensors")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "Sensors" in body
    assert "vm-count" in body
    assert "vmware.vm.list" in body  # op identity
    assert "42" in body  # last_value projection
    assert "every 60s" in body  # rendered cadence


def test_list_renders_uncomposed_sensor() -> None:
    """The regression this page exists to close: a Sensor on no Dashboard renders.

    The registry read is Dashboard-independent -- no Dashboard is seeded here,
    yet the Sensor must still appear.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_sensor(tenant_id=_TENANT_A, name="orphan-sensor")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/sensors")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "orphan-sensor" in response.text


def test_list_last_state_uses_five_state_badge() -> None:
    """``last_state`` renders with the existing five-state badge classes."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_sensor(tenant_id=_TENANT_A, name="failing", last_state="critical")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/sensors")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "critical" in body
    assert "badge-error" in body  # reuses the checks five-state critical vocab


def test_list_never_evaluated_sensor_renders_placeholder() -> None:
    """A never-evaluated Sensor (null timestamps) renders defensively, not a 500."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_sensor(tenant_id=_TENANT_A, name="fresh", last_state="unknown", evaluated=False)
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/sensors")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "fresh" in body
    assert "unknown" in body
    assert "badge-ghost" in body  # five-state unknown badge


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_status_filter_narrows_server_side() -> None:
    """``?status=paused`` returns only paused Sensors (server-side filter)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_sensor(tenant_id=_TENANT_A, name="active-one", status="active")
    _seed_sensor(tenant_id=_TENANT_A, name="paused-one", status="paused")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/sensors?status=paused")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "paused-one" in body
    assert "active-one" not in body


def test_cadence_filter_narrows_server_side() -> None:
    """``?cadence_kind=cron`` returns only cron-cadence Sensors."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_sensor(tenant_id=_TENANT_A, name="interval-one", cadence_kind="interval")
    _seed_sensor(tenant_id=_TENANT_A, name="cron-one", cadence_kind="cron")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/sensors?cadence_kind=cron")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "cron-one" in body
    assert "interval-one" not in body


def test_unknown_filter_value_renders_unfiltered() -> None:
    """A stale / hand-typed filter value clamps to unfiltered rather than 422-ing."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_sensor(tenant_id=_TENANT_A, name="a-sensor", status="active")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/sensors?status=bogus&cadence_kind=nope")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "a-sensor" in response.text


def test_full_page_arms_auto_refresh() -> None:
    """The full page arms the 30s auto-refresh poll on the table."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/sensors")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert 'hx-trigger="every 30s"' in response.text


def test_htmx_request_returns_fragment_not_full_page() -> None:
    """An ``HX-Request`` GET returns the table-rows fragment (no chrome)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_sensor(tenant_id=_TENANT_A, name="vm-count")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/sensors", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert 'id="sensors-table-body"' in response.text
    assert "<!doctype html>" not in response.text.lower()


# ---------------------------------------------------------------------------
# Read-only + tenant isolation
# ---------------------------------------------------------------------------


def test_list_has_no_write_controls() -> None:
    """The registry is read-only: no create / delete forms or write verbs.

    The one ``<form>`` present is the filter bar (an ``hx-get`` search form);
    the read-only contract is the absence of state-changing HTMX verbs and
    create / delete affordances.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_sensor(tenant_id=_TENANT_A, name="vm-count")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/sensors")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "hx-post" not in body
    assert "hx-delete" not in body
    assert "hx-put" not in body


def test_list_is_tenant_isolated() -> None:
    """A Sensor in tenant B never appears in tenant A's registry."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_sensor(tenant_id=_TENANT_B, name="b-only-sensor")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/sensors")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "b-only-sensor" not in response.text
