# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``/ui/runners`` console surface (#2589).

Initiative #2415 (parent goal #221), Task #2589. Acceptance:

* The list is operator-readable (session-authenticated); an unauthenticated
  request 302s to the BFF login.
* The list renders each runner's name + liveness badge + ``last_seen_at`` +
  revoked + created_at; an ``HX-Request`` returns only the rows fragment; the
  full page arms the 30s auto-refresh.
* A runner whose ``runner_assignments`` row has ``stale_at`` set renders the
  dead-man ``unknown`` badge; a fresh one renders a relative ``last_seen_at``.
* The surface is tenant-scoped (a foreign tenant's runners never render) and
  read-only (no register / revoke controls).

Harness mirrors :mod:`tests.test_ui_checks`: a minimal FastAPI app wired with
the UI session + CSRF middlewares and a ``web_session`` row carrying a real
Keycloak-minted access token, so ``require_ui_session`` re-verifies the
session end-to-end. Runner principals are seeded directly as ORM rows (the
lifecycle service needs a live Keycloak); the fleet read path
(``RunnerPrincipalService.list_`` + ``repository.get_stale_markers``) is a
pure DB read that needs no Keycloak.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import RunnerAssignmentRow, RunnerPrincipal, Tenant
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


def _seed_runner(
    *,
    tenant_id: uuid.UUID,
    name: str,
    revoked: bool = False,
    last_seen_ago: timedelta = timedelta(seconds=10),
) -> uuid.UUID:
    """Insert a runner-principal ORM row directly (no Keycloak round-trip)."""
    runner_id = uuid.uuid4()
    last_seen = datetime.now(UTC) - last_seen_ago

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                RunnerPrincipal(
                    id=runner_id,
                    tenant_id=tenant_id,
                    name=name,
                    keycloak_client_id=f"runner:{name}",
                    keycloak_internal_id=f"kc-{runner_id}",
                    owner_sub="op-admin",
                    revoked=revoked,
                    created_by_sub="op-admin",
                    last_seen_at=last_seen,
                )
            )

    asyncio.run(_do())
    return runner_id


def _seed_assignment(*, tenant_id: uuid.UUID, runner_name: str, stale: bool) -> None:
    """Insert the runner's ``runner_assignments`` row, optionally dead-man-flipped."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                RunnerAssignmentRow(
                    tenant_id=tenant_id,
                    runner_name=runner_name,
                    items=[],
                    stale_at=datetime.now(UTC) if stale else None,
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


def _client_with_role(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = _OP_OPERATOR,
    role: TenantRole = TenantRole.OPERATOR,
) -> tuple[TestClient, respx.MockRouter]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-runners-test-kid")
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
    """``GET /ui/runners`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/runners")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# GET /ui/runners -- list
# ---------------------------------------------------------------------------


def test_list_renders_fleet_for_operator() -> None:
    """An operator reads the fleet; the runner name + live badge + last-seen render."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_runner(tenant_id=_TENANT_A, name="edge-runner")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/runners")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "Runners" in body
    assert "edge-runner" in body
    assert "live" in body  # the liveness badge for a fresh runner


def test_list_renders_dead_man_unknown_badge() -> None:
    """A runner whose assignment row is ``stale_at``-flipped renders the UNKNOWN badge."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_runner(tenant_id=_TENANT_A, name="dark-runner")
    _seed_assignment(tenant_id=_TENANT_A, runner_name="dark-runner", stale=True)
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/runners")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "dark-runner" in body
    assert "unknown" in body  # dead-man badge
    assert "badge-ghost" in body  # reuses the checks five-state unknown vocab


def test_list_fresh_runner_shows_relative_last_seen() -> None:
    """A fresh runner (no stale flip) renders a relative ``last_seen_at``, not UNKNOWN."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_runner(tenant_id=_TENANT_A, name="lively", last_seen_ago=timedelta(minutes=5))
    _seed_assignment(tenant_id=_TENANT_A, runner_name="lively", stale=False)
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/runners")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "lively" in body
    assert "5 min ago" in body


def test_list_shows_revoked_runner() -> None:
    """A revoked runner still appears in the fleet, badged revoked."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_runner(tenant_id=_TENANT_A, name="gone-runner", revoked=True)
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/runners")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "gone-runner" in body
    assert "revoked" in body


def test_list_full_page_arms_auto_refresh() -> None:
    """The full page arms the 30s auto-refresh poll on the table."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/runners")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert 'hx-trigger="every 30s"' in response.text


def test_list_htmx_request_returns_fragment_not_full_page() -> None:
    """An ``HX-Request`` GET returns the table-rows fragment (no chrome)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_runner(tenant_id=_TENANT_A, name="edge-runner")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/runners", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert 'id="runners-table-body"' in response.text
    assert "<!doctype html>" not in response.text.lower()


def test_list_has_no_write_controls() -> None:
    """The fleet page is read-only: no register / revoke forms or POST controls."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_runner(tenant_id=_TENANT_A, name="edge-runner")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/runners")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "<form" not in body
    assert "hx-post" not in body
    assert "hx-delete" not in body


def test_list_is_tenant_isolated() -> None:
    """A runner in tenant B never appears in tenant A's fleet."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_runner(tenant_id=_TENANT_B, name="b-only-runner")
    client, mock = _client_with_role(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/runners")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "b-only-runner" not in response.text
