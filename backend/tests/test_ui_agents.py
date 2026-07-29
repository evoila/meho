# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Agents console UI surface.

Initiative #1824 (G10.8 Agents console), Task #1825 (T1). The
acceptance criteria on issue #1825 are:

* ``/ui/agents`` lists the tenant's agent definitions; the list shows
  name / model-tier / enabled / identity_ref / turn_budget /
  created_by / updated_at; a non-existent agent -> 404.
* Operators see read-only views; create / edit / enable-disable /
  delete affordances are hidden for non-admins (soft) AND 403
  server-side (hard).
* Create / edit go through a CSRF-double-submit BFF; a duplicate name
  or an unknown identity_ref re-renders the modal inline -- a
  top-of-form error banner + per-field messages -- not as a generic
  error. The re-render is a 200 fragment so HTMX swaps it back in place;
  a non-2xx fragment would be silently dropped and the error never shown
  (#2346).

Suite shape mirrors :mod:`backend.tests.test_ui_memory_list`: a minimal
FastAPI app wired with the chassis middlewares + the BFF auth router +
the UI router; a ``web_session`` row seeded with a real Keycloak-minted
access token so the ``resolve_operator_or_403`` write dep can re-verify
the token and pick up the right :class:`TenantRole`.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import timedelta
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
from meho_backplane.db.models import AgentDefinition, AgentPrincipal, Tenant
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
from tests._oidc_jwt_helpers import (
    AUDIENCE as _DEFAULT_AUDIENCE,
)
from tests._oidc_jwt_helpers import (
    ISSUER as _DEFAULT_ISSUER,
)
from tests._oidc_jwt_helpers import (
    make_rsa_keypair as _make_rsa_keypair,
)
from tests._oidc_jwt_helpers import (
    mint_token as _mint_token,
)
from tests._oidc_jwt_helpers import (
    mock_discovery_and_jwks as _mock_discovery_and_jwks,
)
from tests._oidc_jwt_helpers import (
    public_jwks as _public_jwks,
)

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OP_A = "op-alice"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the memory suite)."""
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
    """Construct a minimal FastAPI app wired for the agents UI tests."""
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
    """Insert one ``tenant`` row so the agent-definition FK resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_agent(
    *,
    tenant_id: uuid.UUID,
    name: str,
    identity_ref: str = "agent-bot",
    model_tier: str = "standard",
    system_prompt: str = "You are a helpful ops agent.",
    toolset: dict[str, Any] | None = None,
    turn_budget: int = 12,
    enabled: bool = True,
    created_by_sub: str = _OP_A,
) -> uuid.UUID:
    """Persist one ``agent_definition`` row directly.

    Bypasses :meth:`AgentDefinitionService.create` so the seed doesn't
    need a matching :class:`AgentPrincipal` for the identity_ref
    write-boundary validator (which only runs on the service create /
    update paths, not on a direct ORM insert).
    """
    agent_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AgentDefinition(
                    id=agent_id,
                    tenant_id=tenant_id,
                    name=name,
                    identity_ref=identity_ref,
                    model_tier=model_tier,
                    system_prompt=system_prompt,
                    toolset=toolset if toolset is not None else {},
                    turn_budget=turn_budget,
                    output_schema=None,
                    enabled=enabled,
                    created_by_sub=created_by_sub,
                ),
            )

    asyncio.run(_do())
    return agent_id


def _seed_principal(
    *,
    tenant_id: uuid.UUID,
    keycloak_client_id: str,
    revoked: bool = False,
) -> None:
    """Insert one non-revoked ``agent_principal`` so a create's identity_ref validates."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AgentPrincipal(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    name=keycloak_client_id,
                    keycloak_client_id=keycloak_client_id,
                    keycloak_internal_id=f"kc-{uuid.uuid4()}",
                    owner_sub=_OP_A,
                    revoked=revoked,
                    created_by_sub=_OP_A,
                ),
            )

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str,
    operator_sub: str,
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row carrying *access_token* and return its UUID."""

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token=access_token,
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_do())


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    """Mint a stable RSA-2048 keypair + matching JWKS document."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-agents-test-kid")
    return keypair, _public_jwks(keypair)


def _authenticated_client(
    *,
    session_id: uuid.UUID,
    jwks: dict[str, Any],
    with_csrf: bool = False,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + an open respx mock + a CSRF token."""
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    if with_csrf:
        client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _admin_session(jwks_keypair: Any) -> str:
    """Mint a tenant_admin access token for the standard operator."""
    return _mint_token(
        jwks_keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


def _operator_session(jwks_keypair: Any) -> str:
    """Mint an operator (non-admin) access token for the standard operator."""
    return _mint_token(
        jwks_keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )


def _csrf_headers(token: str) -> dict[str, str]:
    """Headers a state-changing HTMX request carries (token + HX-Request)."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_list_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/agents`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/agents")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------


def test_list_full_page_renders_empty_state() -> None:
    """``GET /ui/agents`` with no agents renders the empty state + chrome."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Agents" in body
    assert 'id="agents-cards"' in body
    assert "No agents defined yet" in body
    assert CSRF_COOKIE_NAME in response.cookies


def test_list_renders_cards_with_summary_columns() -> None:
    """Seeded agents render as cards with the scannable columns."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(
        tenant_id=_TENANT_A,
        name="incident-triage",
        identity_ref="agent-incident",
        model_tier="deep",
        system_prompt="Triage incidents.\nSecond line that must not show.",
        toolset={"k8s": {}, "vault": {}},
        turn_budget=20,
        enabled=True,
    )
    _seed_agent(
        tenant_id=_TENANT_A,
        name="vm-inventory",
        identity_ref="agent-vm",
        enabled=False,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert "incident-triage" in body
    assert "vm-inventory" in body
    # model tier badge + enabled/disabled pills.
    assert ">deep<" in body or "deep" in body
    assert "enabled" in body
    assert "disabled" in body
    # identity_ref + turn budget surfaced.
    assert "agent-incident" in body
    assert "20 turns" in body
    # System prompt summary is first line only -- never the second line.
    assert "Triage incidents." in body
    assert "must not show" not in body
    # toolset is summarised as a count, never dumped.
    assert "2 tools" in body


def test_list_is_tenant_scoped() -> None:
    """Tenant B's agents never appear in tenant A's list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_agent(tenant_id=_TENANT_A, name="mine")
    _seed_agent(tenant_id=_TENANT_B, name="not-mine")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents")
    finally:
        mock.stop()
    assert response.status_code == 200
    assert "mine" in response.text
    assert "not-mine" not in response.text


def test_list_htmx_fragment_returns_cards_only() -> None:
    """HTMX request to ``/ui/agents`` returns the cards fragment, no chrome."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="solo")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert 'id="agents-cards"' in body
    assert "<title>" not in body


def test_list_hides_new_agent_button_for_operator() -> None:
    """A non-admin operator does not see the create affordance (soft gate)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents")
    finally:
        mock.stop()
    assert response.status_code == 200
    assert 'hx-get="/ui/agents/create"' not in response.text


def test_list_shows_new_agent_button_for_admin() -> None:
    """A tenant_admin sees the create affordance."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents")
    finally:
        mock.stop()
    assert response.status_code == 200
    assert 'hx-get="/ui/agents/create"' in response.text


def test_list_card_shows_toggle_for_admin_with_flipped_state() -> None:
    """Each card carries a one-click enable/disable toggle for a tenant_admin.

    The card toggle POSTs the *flipped* ``enabled`` value to the same
    ``/ui/agents/{name}/toggle`` route the detail view uses, so an admin can
    flip an agent's state without opening the detail page (#2347).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="on-agent", enabled=True)
    _seed_agent(tenant_id=_TENANT_A, name="off-agent", enabled=False)
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert 'data-action="toggle"' in body
    # An enabled agent's toggle disables it; a disabled agent's enables it.
    assert 'hx-post="/ui/agents/on-agent/toggle"' in body
    assert 'hx-post="/ui/agents/off-agent/toggle"' in body
    assert '"enabled": "false"' in body  # on-agent -> disable
    assert '"enabled": "true"' in body  # off-agent -> enable


def test_list_card_hides_toggle_for_operator() -> None:
    """A non-admin operator sees no card-level toggle (soft gate)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="on-agent", enabled=True)
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents")
    finally:
        mock.stop()
    assert response.status_code == 200
    assert 'data-action="toggle"' not in response.text
    assert "/ui/agents/on-agent/toggle" not in response.text


def test_list_card_carries_view_link_and_visible_identity_for_operator() -> None:
    """Each card offers the converged detail-nav pair, for every role (#2463).

    The name heading is a visible ``link link-primary`` (not the invisible
    ``link link-hover``) to the detail page, and ``card-actions`` carries a
    ``View`` link -- rendered even for a non-admin operator (outside the
    ``can_write`` toggle guard) so the affordance never depends on write
    privileges.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="on-agent", enabled=True)
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    # Visible identity link on the name heading (never the hover-only style).
    assert 'class="link link-primary font-mono font-semibold break-all"' in body
    assert "link link-hover" not in body
    # A View link in card-actions to the detail page -- rendered for the
    # operator despite the toggle being hidden (no write privileges).
    assert 'data-action="toggle"' not in body  # toggle stays admin-only
    assert 'aria-label="View agent on-agent"' in body
    assert 'href="/ui/agents/on-agent"' in body


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------


def test_detail_renders_full_definition() -> None:
    """Detail page renders the full definition incl. read-only system prompt."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(
        tenant_id=_TENANT_A,
        name="incident-triage",
        system_prompt="Investigate the alert and propose a fix.",
        toolset={"k8s": {}},
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/incident-triage")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Agent: incident-triage" in body
    assert "Investigate the alert and propose a fix." in body
    assert "System prompt" in body
    assert "Toolset" in body


def test_detail_missing_agent_returns_404() -> None:
    """A non-existent agent name returns 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/ghost")
    finally:
        mock.stop()
    assert response.status_code == 404


def test_detail_cross_tenant_returns_404() -> None:
    """Tenant A's agent is invisible (404) to a tenant B operator."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_agent(tenant_id=_TENANT_A, name="a-agent")
    _, jwks = _make_keypair_and_jwks()
    session_id_b = _seed_session_sync(
        tenant_id=_TENANT_B, access_token="unused", operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id_b, jwks=jwks)
    try:
        response = client.get("/ui/agents/a-agent")
    finally:
        mock.stop()
    assert response.status_code == 404


def test_detail_hides_write_affordances_for_operator() -> None:
    """A non-admin operator sees no edit / toggle / delete buttons."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="locked")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/locked")
    finally:
        mock.stop()
    assert response.status_code == 200
    assert 'hx-get="/ui/agents/locked/edit"' not in response.text
    assert "/ui/agents/locked/toggle" not in response.text


# ---------------------------------------------------------------------------
# Create -- RBAC gate + happy path + 409 + 422
# ---------------------------------------------------------------------------


def test_create_modal_requires_tenant_admin() -> None:
    """``GET /ui/agents/create`` 403s for a non-admin operator (hard gate)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/create")
    finally:
        mock.stop()
    assert response.status_code == 403


def test_create_modal_renders_for_admin() -> None:
    """A tenant_admin gets the create modal fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/create")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert 'id="agents-create-modal"' in body
    assert 'name="identity_ref"' in body
    assert 'name="system_prompt"' in body
    # A fresh (error-free) render carries no error summary banner.
    assert "data-form-error-summary" not in body


def test_create_persists_and_redirects() -> None:
    """A valid create persists the agent and HX-Redirects to the list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-new")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/create",
            data={
                "name": "new-agent",
                "identity_ref": "agent-new",
                "model_tier": "standard",
                "system_prompt": "You are a new agent.",
                "turn_budget": "10",
                "enabled": "true",
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers.get("HX-Redirect") == "/ui/agents"

    # Confirm the row landed via the list render.
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    with respx.mock(assert_all_called=False) as m2:
        _mock_discovery_and_jwks(m2, jwks)
        follow = client.get("/ui/agents")
    assert follow.status_code == 200
    assert "new-agent" in follow.text


def test_create_duplicate_name_renders_inline() -> None:
    """A duplicate (tenant, name) re-renders the modal inline with a name error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-dup")
    _seed_agent(tenant_id=_TENANT_A, name="dup", identity_ref="agent-dup")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/create",
            data={
                "name": "dup",
                "identity_ref": "agent-dup",
                "model_tier": "standard",
                "system_prompt": "Another agent named dup.",
                "turn_budget": "10",
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    # 200 (not 409) so HTMX swaps the re-rendered modal back in place;
    # a non-2xx fragment would be silently dropped and the error never
    # shown (#2346).
    assert response.status_code == 200, response.text
    body = response.text
    # The modal re-renders inline with a top-of-form summary banner and a
    # field-level error, not a generic error page.
    assert "data-form-error-summary" in body
    assert 'data-error-for="name"' in body
    assert "already exists" in body


def test_create_unknown_identity_ref_renders_inline() -> None:
    """An identity_ref with no matching principal re-renders the modal inline."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/create",
            data={
                "name": "orphan",
                "identity_ref": "nope-not-registered",
                "model_tier": "standard",
                "system_prompt": "Agent with a bogus identity.",
                "turn_budget": "10",
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "data-form-error-summary" in body
    assert 'data-error-for="identity_ref"' in body


def test_create_invalid_turn_budget_renders_inline() -> None:
    """An out-of-range turn_budget re-renders the modal with a field error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/create",
            data={
                "name": "bad-budget",
                "identity_ref": "agent-x",
                "model_tier": "standard",
                "system_prompt": "x",
                "turn_budget": "0",  # below the >=1 floor
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "data-form-error-summary" in response.text
    assert 'data-error-for="turn_budget"' in response.text


# ---------------------------------------------------------------------------
# Edit / toggle / delete -- RBAC + happy path
# ---------------------------------------------------------------------------


def test_edit_modal_requires_tenant_admin() -> None:
    """``GET /ui/agents/{name}/edit`` 403s for a non-admin operator."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="locked")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/locked/edit")
    finally:
        mock.stop()
    assert response.status_code == 403


def test_edit_persists_and_redirects() -> None:
    """A valid PATCH persists the change and HX-Redirects to the list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-edit")
    _seed_agent(
        tenant_id=_TENANT_A,
        name="editable",
        identity_ref="agent-edit",
        turn_budget=5,
    )
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.request(
            "PATCH",
            "/ui/agents/editable",
            data={
                "identity_ref": "agent-edit",
                "model_tier": "fast",
                "system_prompt": "Updated prompt.",
                "turn_budget": "99",
                "enabled": "true",
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers.get("HX-Redirect") == "/ui/agents"


def test_toggle_disable_requires_admin_and_flips_enabled() -> None:
    """A tenant_admin can disable an agent; an operator is 403'd."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="toggler", enabled=True)
    keypair, jwks = _make_keypair_and_jwks()
    # Operator -> 403.
    op_token = _operator_session(keypair)
    op_session = _seed_session_sync(tenant_id=_TENANT_A, access_token=op_token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=op_session, jwks=jwks, with_csrf=True)
    try:
        denied = client.post(
            "/ui/agents/toggler/toggle",
            data={"enabled": "false"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert denied.status_code == 403

    # Admin -> 204 + redirect.
    admin_token = _admin_session(keypair)
    admin_session = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=admin_token, operator_sub=_OP_A
    )
    client2, mock2, csrf2 = _authenticated_client(
        session_id=admin_session, jwks=jwks, with_csrf=True
    )
    try:
        ok = client2.post(
            "/ui/agents/toggler/toggle",
            data={"enabled": "false"},
            headers=_csrf_headers(csrf2),
        )
    finally:
        mock2.stop()
    assert ok.status_code == 204, ok.text
    assert ok.headers.get("HX-Redirect") == "/ui/agents"


def test_delete_requires_admin_and_removes_row() -> None:
    """A tenant_admin can delete an agent; an operator is 403'd."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="doomed")
    keypair, jwks = _make_keypair_and_jwks()
    op_token = _operator_session(keypair)
    op_session = _seed_session_sync(tenant_id=_TENANT_A, access_token=op_token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=op_session, jwks=jwks, with_csrf=True)
    try:
        denied = client.post("/ui/agents/doomed/delete", headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert denied.status_code == 403

    admin_token = _admin_session(keypair)
    admin_session = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=admin_token, operator_sub=_OP_A
    )
    client2, mock2, csrf2 = _authenticated_client(
        session_id=admin_session, jwks=jwks, with_csrf=True
    )
    try:
        ok = client2.post("/ui/agents/doomed/delete", headers=_csrf_headers(csrf2))
    finally:
        mock2.stop()
    assert ok.status_code == 204, ok.text
    assert ok.headers.get("HX-Redirect") == "/ui/agents"

    # Confirm absence via the list.
    client2.cookies.set(CSRF_COOKIE_NAME, csrf2)
    with respx.mock(assert_all_called=False) as m3:
        _mock_discovery_and_jwks(m3, jwks)
        follow = client2.get("/ui/agents")
    assert follow.status_code == 200
    assert "doomed" not in follow.text


def test_delete_missing_agent_returns_404() -> None:
    """Deleting a non-existent agent returns 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post("/ui/agents/never-existed/delete", headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Stub retirement
# ---------------------------------------------------------------------------


def test_ui_agents_is_not_a_chassis_stub() -> None:
    """The real agents router serves ``/ui/agents`` (no 'Coming soon')."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents")
    finally:
        mock.stop()
    assert response.status_code == 200
    assert "Coming soon" not in response.text
