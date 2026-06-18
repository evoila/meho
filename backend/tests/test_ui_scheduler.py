# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the scheduler UI surface (Task #1826, G10.8-T6).

Initiative #1824 (G10.8 Autonomous execution control plane). Acceptance
criteria on issue #1826:

* List / detail are operator-readable; create / cancel are tenant_admin
  (soft-hide affordance + hard 403 via the connectors role gates).
* The cron field has live validation + a ``next_fire_at`` preview -- no
  free-text cron with no feedback.
* Cancel is a terminal confirm dialog (permanent; row kept for audit;
  the schedule will never fire again); 409 ``trigger_already_fired`` +
  404 ``trigger_not_found`` handled gracefully in the HTMX response.
* Timestamps coerce to UTC-aware so the SQLite test path does not
  ``TypeError`` on the relative-time arithmetic.

Harness shape mirrors :mod:`backend.tests.test_ui_connectors_forms`: a
minimal FastAPI app wired with the UI session + CSRF middlewares, a
``web_session`` row carrying a real Keycloak-minted access token so the
``resolve_role_probe`` / ``resolve_operator_or_403`` deps re-verify the
role end-to-end (no patching of the JWT lift), and seeded ``tenant`` /
``agent_definition`` / ``scheduled_trigger`` rows in the autouse SQLite
engine.
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
from meho_backplane.db.models import (
    AgentDefinition,
    ScheduledTrigger,
    ScheduledTriggerInFlightPolicy,
    ScheduledTriggerKind,
    ScheduledTriggerStatus,
    Tenant,
)
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

_AGENT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the connectors suite)."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _seed_agent(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID = _AGENT_ID,
    name: str = "nightly-summariser",
) -> uuid.UUID:
    now = datetime.now(UTC)

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AgentDefinition(
                    id=agent_id,
                    tenant_id=tenant_id,
                    name=name,
                    identity_ref="__scheduler__",
                    model_tier="standard",
                    system_prompt="be helpful",
                    toolset={},
                    turn_budget=10,
                    output_schema=None,
                    enabled=True,
                    created_by_sub=_OP_ADMIN,
                    created_at=now,
                    updated_at=now,
                ),
            )

    asyncio.run(_do())
    return agent_id


def _seed_trigger(
    *,
    tenant_id: uuid.UUID,
    trigger_id: uuid.UUID | None = None,
    agent_id: uuid.UUID = _AGENT_ID,
    kind: str = ScheduledTriggerKind.CRON.value,
    cron_expr: str | None = "*/15 * * * *",
    fire_at: datetime | None = None,
    status_value: str = ScheduledTriggerStatus.ACTIVE.value,
    next_fire_at: datetime | None = None,
    work_ref: str | None = None,
) -> uuid.UUID:
    tid = trigger_id or uuid.uuid4()
    now = datetime.now(UTC)

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                ScheduledTrigger(
                    id=tid,
                    tenant_id=tenant_id,
                    agent_definition_id=agent_id,
                    kind=kind,
                    cron_expr=cron_expr,
                    timezone="UTC",
                    fire_at=fire_at,
                    event_filter=None,
                    status=status_value,
                    in_flight_policy=ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT.value,
                    next_fire_at=next_fire_at or (now + timedelta(hours=1)),
                    last_fired_at=None,
                    inputs={"prompt": "summarise"},
                    identity_sub="__scheduler__",
                    created_by_sub=_OP_ADMIN,
                    work_ref=work_ref,
                    created_at=now,
                    updated_at=now,
                ),
            )
        return tid

    return asyncio.run(_do())


def _load_trigger_status(tenant_id: uuid.UUID, trigger_id: uuid.UUID) -> str | None:
    from sqlalchemy import select

    async def _do() -> str | None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = select(ScheduledTrigger.status).where(
                ScheduledTrigger.tenant_id == tenant_id,
                ScheduledTrigger.id == trigger_id,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    return asyncio.run(_do())


def _trigger_count(tenant_id: uuid.UUID) -> int:
    from sqlalchemy import func, select

    async def _do() -> int:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = (
                select(func.count())
                .select_from(ScheduledTrigger)
                .where(ScheduledTrigger.tenant_id == tenant_id)
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
        keypair = _make_rsa_keypair("ui-scheduler-test-kid")
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


def test_list_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/scheduler`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/scheduler")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_create_submit_unauthenticated_is_blocked() -> None:
    """``POST /ui/scheduler/create`` without a session never reaches the handler."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.post("/ui/scheduler/create")
    assert response.status_code in (302, 403)


# ---------------------------------------------------------------------------
# GET /ui/scheduler -- list (operator-readable)
# ---------------------------------------------------------------------------


def test_list_renders_for_operator() -> None:
    """An operator can read the list; the row + relative next-fire render."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    _seed_trigger(tenant_id=_TENANT_A)
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.get("/ui/scheduler")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "Scheduler" in body
    # The schedule summary + resolved agent name render.
    assert "*/15 * * * *" in body
    assert "nightly-summariser" in body


def test_list_hides_create_button_from_operator() -> None:
    """The "Create trigger" button surfaces only for tenant_admin."""
    _seed_tenant(_TENANT_A, "tenant-a")
    admin_client, admin_mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        admin_resp = admin_client.get("/ui/scheduler")
    finally:
        admin_mock.stop()
    assert admin_resp.status_code == 200, admin_resp.text
    assert 'hx-get="/ui/scheduler/create"' in admin_resp.text

    op_client, op_mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        op_resp = op_client.get("/ui/scheduler")
    finally:
        op_mock.stop()
    assert op_resp.status_code == 200, op_resp.text
    assert 'hx-get="/ui/scheduler/create"' not in op_resp.text


def test_list_status_filter_narrows_rows() -> None:
    """A status filter only renders triggers in that status."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    active = _seed_trigger(tenant_id=_TENANT_A, status_value=ScheduledTriggerStatus.ACTIVE.value)
    cancelled = _seed_trigger(
        tenant_id=_TENANT_A,
        cron_expr="0 0 * * *",
        status_value=ScheduledTriggerStatus.CANCELLED.value,
    )
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.get("/ui/scheduler?status=active")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert str(active) in response.text
    assert str(cancelled) not in response.text


def test_list_htmx_request_returns_fragment_not_full_page() -> None:
    """An ``HX-Request`` GET returns the table-rows fragment (no chrome)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    _seed_trigger(tenant_id=_TENANT_A)
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.get("/ui/scheduler", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert 'id="scheduler-table-body"' in response.text
    # No full-page chrome in the fragment.
    assert "<!doctype html>" not in response.text.lower()


def test_list_invalid_kind_filter_is_422() -> None:
    """An out-of-enum ``kind`` query value is rejected at the boundary."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.get("/ui/scheduler?kind=bogus")
    finally:
        mock.stop()
    assert response.status_code == 422, response.text


def test_list_is_tenant_isolated() -> None:
    """A trigger in tenant B never appears in tenant A's list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_agent(tenant_id=_TENANT_B)
    other = _seed_trigger(tenant_id=_TENANT_B)
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.get("/ui/scheduler")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert str(other) not in response.text


# ---------------------------------------------------------------------------
# GET /ui/scheduler/{id} -- detail
# ---------------------------------------------------------------------------


def test_detail_renders_full_row_for_operator() -> None:
    """The detail page renders the governance + schedule fields."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    tid = _seed_trigger(tenant_id=_TENANT_A, work_ref="gh:evoila/meho#1826")
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.get(f"/ui/scheduler/{tid}")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "*/15 * * * *" in body
    assert "__scheduler__" in body  # identity_sub
    assert "fail_into_audit" in body  # in_flight_policy
    assert "gh:evoila/meho#1826" in body  # work_ref chip + recent-fires link


def test_detail_cross_tenant_is_404() -> None:
    """A trigger id belonging to another tenant is 404 (no existence leak)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_agent(tenant_id=_TENANT_B)
    other = _seed_trigger(tenant_id=_TENANT_B)
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.get(f"/ui/scheduler/{other}")
    finally:
        mock.stop()
    assert response.status_code == 404, response.text


def test_detail_hides_cancel_on_terminal_trigger() -> None:
    """A cancelled trigger's detail page does not render the cancel button."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    tid = _seed_trigger(tenant_id=_TENANT_A, status_value=ScheduledTriggerStatus.CANCELLED.value)
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.get(f"/ui/scheduler/{tid}")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert f'hx-get="/ui/scheduler/{tid}/cancel"' not in response.text
    assert "terminal" in response.text.lower()


# ---------------------------------------------------------------------------
# Create modal -- RBAC gate
# ---------------------------------------------------------------------------


def test_create_modal_renders_for_tenant_admin() -> None:
    """A tenant_admin GET renders the create modal with the agent dropdown."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.get("/ui/scheduler/create")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="scheduler-create-modal"' in body
    assert 'hx-post="/ui/scheduler/create"' in body
    assert str(_AGENT_ID) in body  # agent dropdown option
    assert 'hx-post="/ui/scheduler/validate-cron"' in body  # live cron validation


def test_create_modal_rejects_operator_with_403() -> None:
    """An operator (non-admin) GET on the create modal is 403'd server-side."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.get("/ui/scheduler/create")
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# POST /ui/scheduler/validate-cron -- live validation + preview
# ---------------------------------------------------------------------------


def test_validate_cron_valid_renders_next_fire() -> None:
    """A valid cron expression renders a success line with a next-fire preview."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/scheduler/validate-cron",
            data={"cron_expr": "*/15 * * * *", "timezone": "UTC"},
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert 'data-cron-valid="true"' in response.text


def test_validate_cron_invalid_renders_error() -> None:
    """An invalid (6-field) cron expression renders the typed error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/scheduler/validate-cron",
            data={"cron_expr": "* * * * * *", "timezone": "UTC"},
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert 'data-cron-valid="false"' in response.text


def test_validate_cron_rejects_operator_with_403() -> None:
    """The validate-cron endpoint is part of the create flow -- tenant_admin only."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.post(
            "/ui/scheduler/validate-cron",
            data={"cron_expr": "*/15 * * * *", "timezone": "UTC"},
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# POST /ui/scheduler/create -- submit
# ---------------------------------------------------------------------------


def test_create_submit_persists_cron_trigger_and_redirects() -> None:
    """A valid cron create persists the trigger and HX-Redirects to the list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/scheduler/create",
            data={
                "kind": "cron",
                "agent_definition_id": str(_AGENT_ID),
                "cron_expr": "0 9 * * *",
                "timezone": "UTC",
                "in_flight_policy": "fail_into_audit",
            },
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/scheduler"
    assert _trigger_count(_TENANT_A) == 1


def test_create_submit_invalid_cron_rerenders_modal_with_error() -> None:
    """An invalid cron expression re-renders the modal with a typed banner."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/scheduler/create",
            data={
                "kind": "cron",
                "agent_definition_id": str(_AGENT_ID),
                "cron_expr": "not a cron",
                "timezone": "UTC",
                "in_flight_policy": "fail_into_audit",
            },
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()
    # Re-rendered modal (200, not 204) with the alert banner; nothing persisted.
    assert response.status_code == 200, response.text
    assert 'id="scheduler-create-modal"' in response.text
    assert "alert-error" in response.text
    assert _trigger_count(_TENANT_A) == 0


def test_create_submit_rejects_operator_with_403() -> None:
    """An operator POST to create is 403'd server-side (the hidden button is UX only)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.post(
            "/ui/scheduler/create",
            data={
                "kind": "cron",
                "agent_definition_id": str(_AGENT_ID),
                "cron_expr": "0 9 * * *",
                "timezone": "UTC",
                "in_flight_policy": "fail_into_audit",
            },
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    assert _trigger_count(_TENANT_A) == 0


def test_create_submit_unknown_agent_rerenders_modal() -> None:
    """A create referencing an absent agent_definition re-renders with an error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # No agent seeded -- the FK pre-flight fails.
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/scheduler/create",
            data={
                "kind": "cron",
                "agent_definition_id": str(_AGENT_ID),
                "cron_expr": "0 9 * * *",
                "timezone": "UTC",
                "in_flight_policy": "fail_into_audit",
            },
            headers=_form_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "alert-error" in response.text
    assert _trigger_count(_TENANT_A) == 0


def test_create_submit_without_csrf_is_403() -> None:
    """A create POST without the double-submit token is rejected by the middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(
            "/ui/scheduler/create",
            data={
                "kind": "cron",
                "agent_definition_id": str(_AGENT_ID),
                "cron_expr": "0 9 * * *",
                "timezone": "UTC",
                "in_flight_policy": "fail_into_audit",
            },
            headers={"HX-Request": "true"},  # no X-CSRF-Token
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# Cancel -- confirm modal + terminal submit
# ---------------------------------------------------------------------------


def test_cancel_modal_renders_terminal_confirm() -> None:
    """The cancel modal spells out that the action is permanent."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    tid = _seed_trigger(tenant_id=_TENANT_A)
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.get(f"/ui/scheduler/{tid}/cancel")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="scheduler-cancel-modal"' in body
    assert "permanent" in body.lower()
    assert "never fire again" in body.lower()


def test_cancel_submit_cancels_trigger_and_redirects() -> None:
    """A tenant_admin cancel transitions the trigger to cancelled + HX-Redirects."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    tid = _seed_trigger(tenant_id=_TENANT_A)
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(f"/ui/scheduler/{tid}/cancel", headers=_form_headers(csrf))
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/scheduler"
    assert _load_trigger_status(_TENANT_A, tid) == ScheduledTriggerStatus.CANCELLED.value


def test_cancel_submit_on_fired_trigger_is_409_banner() -> None:
    """Cancelling an already-fired trigger re-renders the modal with the 409 banner."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    tid = _seed_trigger(
        tenant_id=_TENANT_A,
        kind=ScheduledTriggerKind.ONE_OFF.value,
        cron_expr=None,
        fire_at=datetime(2026, 1, 1, tzinfo=UTC),
        status_value=ScheduledTriggerStatus.FIRED.value,
    )
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(f"/ui/scheduler/{tid}/cancel", headers=_form_headers(csrf))
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "already fired" in response.text.lower()
    # Status unchanged -- a fired trigger is terminal.
    assert _load_trigger_status(_TENANT_A, tid) == ScheduledTriggerStatus.FIRED.value


def test_cancel_submit_rejects_operator_with_403() -> None:
    """An operator POST to cancel is 403'd server-side; the trigger survives."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A)
    tid = _seed_trigger(tenant_id=_TENANT_A)
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.post(f"/ui/scheduler/{tid}/cancel", headers=_form_headers(csrf))
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    assert _load_trigger_status(_TENANT_A, tid) == ScheduledTriggerStatus.ACTIVE.value


def test_cancel_submit_cross_tenant_is_404() -> None:
    """A cancel against another tenant's trigger id is 404 (no existence leak)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_agent(tenant_id=_TENANT_B)
    other = _seed_trigger(tenant_id=_TENANT_B)
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.post(f"/ui/scheduler/{other}/cancel", headers=_form_headers(csrf))
    finally:
        mock.stop()
    assert response.status_code == 404, response.text
    assert _load_trigger_status(_TENANT_B, other) == ScheduledTriggerStatus.ACTIVE.value
