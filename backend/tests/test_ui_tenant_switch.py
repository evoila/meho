# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant picker, tenant switch and old-tab check in the web interface (#5240).

A person can be in several tenants (one login lists them all). The web
session works in one current tenant, stored on the ``web_session`` row.
These tests pin:

* after login, a person with several tenants sees the picker first; a
  person with one tenant goes straight on and the sidebar looks as before;
* a switch runs only for a tenant the token allows, moves the session,
  writes an audit row and opens the dashboard; a refused switch changes
  nothing;
* targets, approvals and the dashboard show only the current tenant;
* a request from a page made for another tenant is refused and nothing runs;
* after removal from a tenant, the next token check takes the person out;
* a platform admin sees every existing tenant in the picker;
* the account page lists the person's tenants with the role in each.

The harness follows ``test_ui_account.py``: a small FastAPI app with the
real UI session, CSRF and audit middlewares, real JWKS-signed tokens on
the session row and a respx-mocked discovery + JWKS endpoint.
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
import structlog
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException

from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    AuditLog,
    Target,
    Tenant,
    WebSession,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import (
    SESSION_COOKIE_NAME,
    UISessionMiddleware,
    ui_session_expired_exception_handler,
)
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth import routes as ui_auth_routes
from meho_backplane.ui.auth.flow import (
    TokenExchangeResult,
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.revalidation import reset_read_revalidation_cache_for_testing
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
    rotate_refresh,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import AUDIENCE, ISSUER, make_rsa_keypair, mint_token, public_jwks
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks

_TENANT_A = uuid.UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb")
_TENANT_C = uuid.UUID("cccccccc-3333-3333-3333-cccccccccccc")

_ENTRY_A_READERS = f"{_TENANT_A}:team-a:Readers"
_ENTRY_B_OPERATORS = f"{_TENANT_B}:team-b:Operators"

_PERSON = "person-two-tenants"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the account suite)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    _reset_caches()
    yield
    _reset_caches()


def _reset_caches() -> None:
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_read_revalidation_cache_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def keypair() -> Iterator[Any]:
    """An RSA keypair whose public half the mocked JWKS endpoint serves."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        key = make_rsa_keypair("ui-tenant-switch-kid")
    with respx.mock(assert_all_called=False) as mock:
        _mock_discovery_and_jwks(mock, public_jwks(key))
        yield key


def _build_app() -> FastAPI:
    """The chassis order: UI session outermost, then CSRF, then audit."""
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.add_exception_handler(StarletteHTTPException, ui_session_expired_exception_handler)
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(static_root_dir()), check_dir=False),
        name="ui_static",
    )
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    return app


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _seed_tenants(*tenants: tuple[uuid.UUID, str]) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            for tenant_id, slug in tenants:
                session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    _run(_do())


def _two_tenant_token(key: Any, **extra: Any) -> str:
    """A token that lists tenant A (Readers) and tenant B (Operators)."""
    claims = {"meho_tenants": [_ENTRY_A_READERS, _ENTRY_B_OPERATORS]}
    claims.update(extra.pop("extra_claims", {}))
    return mint_token(key, sub=_PERSON, extra_claims=claims, **extra)


def _seed_session(
    access_token: str,
    *,
    tenant_id: uuid.UUID,
    tenant_chosen: bool = True,
    can_switch: bool = True,
    operator_sub: str = _PERSON,
) -> uuid.UUID:
    async def _do() -> uuid.UUID:
        async with get_sessionmaker()() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token=access_token,
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
                tenant_chosen=tenant_chosen,
                can_switch_tenant=can_switch,
            )
            return decrypted.id

    return _run(_do())


def _session_row(session_id: uuid.UUID) -> WebSession:
    async def _do() -> WebSession:
        async with get_sessionmaker()() as session:
            row = await session.get(WebSession, session_id)
            assert row is not None
            return row

    return _run(_do())


def _replace_token(session_id: uuid.UUID, access_token: str) -> None:
    """Rotate the row's tokens, as a token refresh does."""

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            await rotate_refresh(
                session,
                session_id,
                presented_refresh="refresh-token-plaintext",
                new_access_token=access_token,
                new_refresh_token="refresh-token-plaintext-2",
            )

    _run(_do())


def _client(session_id: uuid.UUID) -> TestClient:
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, mint_csrf_token(str(session_id)))
    return client


def _switch(client: TestClient, session_id: uuid.UUID, tenant: str) -> Any:
    return client.post(
        "/ui/tenant/switch",
        data={"csrf_token": mint_csrf_token(str(session_id)), "tenant": tenant},
    )


def _audit_rows(op_id: str) -> list[AuditLog]:
    async def _do() -> list[AuditLog]:
        async with get_sessionmaker()() as session:
            rows = (await session.execute(select(AuditLog))).scalars().all()
            return [row for row in rows if (row.payload or {}).get("op_id") == op_id]

    return _run(_do())


# ---------------------------------------------------------------------------
# Login: picker for several tenants, none for one
# ---------------------------------------------------------------------------


def _login(monkeypatch: pytest.MonkeyPatch, access_token: str) -> tuple[Any, WebSession]:
    async def _fake_exchange(**_kwargs: Any) -> TokenExchangeResult:
        return TokenExchangeResult(
            access_token=access_token,
            refresh_token="refresh-token-plaintext",
            expires_in=300,
            return_to="/ui/connectors",
        )

    monkeypatch.setattr(ui_auth_routes, "_exchange_or_translate", _fake_exchange)
    client = TestClient(_build_app(), follow_redirects=False)
    response = client.get("/ui/auth/callback", params={"code": "c", "state": "s"})
    session_id = uuid.UUID(response.cookies[SESSION_COOKIE_NAME])
    return response, _session_row(session_id)


def test_login_with_two_tenants_opens_the_picker(
    monkeypatch: pytest.MonkeyPatch, keypair: Any
) -> None:
    _seed_tenants((_TENANT_A, "team-a"), (_TENANT_B, "team-b"))
    response, row = _login(monkeypatch, _two_tenant_token(keypair))

    assert response.status_code == 302
    assert response.headers["location"] == "/ui/tenant"
    assert row.tenant_chosen is False
    assert row.can_switch_tenant is True

    client = _client(row.id)
    # Any other page sends the person to the picker first.
    assert client.get("/ui/connectors").headers["location"] == "/ui/tenant"
    picker = client.get("/ui/tenant")
    assert picker.status_code == 200, picker.text
    assert "Choose a tenant" in picker.text
    assert "Tenant team-a" in picker.text
    assert "Tenant team-b" in picker.text
    assert "Readers" in picker.text
    assert "Operators" in picker.text


def test_login_with_one_tenant_shows_no_picker(
    monkeypatch: pytest.MonkeyPatch, keypair: Any
) -> None:
    _seed_tenants((_TENANT_A, "team-a"))
    token = mint_token(keypair, sub=_PERSON, tenant_id=str(_TENANT_A), tenant_role="operator")
    response, row = _login(monkeypatch, token)

    assert response.headers["location"] == "/ui/connectors"
    assert row.tenant_chosen is True
    assert row.can_switch_tenant is False
    page = _client(row.id).get("/ui/connectors")
    assert page.status_code == 200, page.text
    # The sidebar tenant box stays read-only, as before.
    assert 'data-testid="tenant-switch"' not in page.text
    assert 'aria-label="Active tenant"' in page.text


# ---------------------------------------------------------------------------
# Switch
# ---------------------------------------------------------------------------


def test_switch_moves_the_session_and_writes_an_audit_row(keypair: Any) -> None:
    _seed_tenants((_TENANT_A, "team-a"), (_TENANT_B, "team-b"))
    session_id = _seed_session(_two_tenant_token(keypair), tenant_id=_TENANT_A)
    client = _client(session_id)

    response = _switch(client, session_id, str(_TENANT_B))

    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/ui/"
    row = _session_row(session_id)
    assert row.tenant_id == _TENANT_B
    assert row.tenant_chosen is True
    rows = _audit_rows("meho.ui.tenant.switch")
    assert len(rows) == 1
    assert rows[0].tenant_id == _TENANT_B
    assert rows[0].payload["from_tenant_id"] == str(_TENANT_A)
    assert rows[0].payload["to_tenant_id"] == str(_TENANT_B)
    assert "switched from" in rows[0].payload["summary"]


def test_switch_from_the_picker_after_login(keypair: Any) -> None:
    _seed_tenants((_TENANT_A, "team-a"), (_TENANT_B, "team-b"))
    session_id = _seed_session(_two_tenant_token(keypair), tenant_id=_TENANT_A, tenant_chosen=False)
    client = _client(session_id)

    response = _switch(client, session_id, "team-b")

    assert response.status_code == 303, response.text
    row = _session_row(session_id)
    assert (row.tenant_id, row.tenant_chosen) == (_TENANT_B, True)
    assert _audit_rows("meho.ui.tenant.switch")[0].payload.get("from_tenant_id") is None
    assert client.get("/ui/connectors").status_code == 200


def test_switch_to_a_tenant_not_in_the_token_is_refused(keypair: Any) -> None:
    _seed_tenants((_TENANT_A, "team-a"), (_TENANT_B, "team-b"), (_TENANT_C, "team-c"))
    session_id = _seed_session(_two_tenant_token(keypair), tenant_id=_TENANT_A)
    client = _client(session_id)

    response = _switch(client, session_id, str(_TENANT_C))

    assert response.status_code == 403
    assert "Nothing changed" in response.text
    row = _session_row(session_id)
    assert (row.tenant_id, row.tenant_chosen) == (_TENANT_A, True)
    assert _audit_rows("meho.ui.tenant.switch") == []


def test_switch_needs_the_form_protection(keypair: Any) -> None:
    _seed_tenants((_TENANT_A, "team-a"), (_TENANT_B, "team-b"))
    session_id = _seed_session(_two_tenant_token(keypair), tenant_id=_TENANT_A)
    client = _client(session_id)

    response = client.post("/ui/tenant/switch", data={"tenant": str(_TENANT_B)})

    assert response.status_code == 403
    assert response.json()["detail"] == "csrf_token_invalid"
    assert _session_row(session_id).tenant_id == _TENANT_A


def test_platform_admin_sees_all_tenants_and_can_switch_into_one(keypair: Any) -> None:
    _seed_tenants((_TENANT_A, "team-a"), (_TENANT_B, "team-b"), (_TENANT_C, "team-c"))
    token = mint_token(
        keypair,
        sub=_PERSON,
        platform_admin=True,
        extra_claims={"meho_tenants": [_ENTRY_A_READERS]},
    )
    session_id = _seed_session(token, tenant_id=_TENANT_A)
    client = _client(session_id)

    picker = client.get("/ui/tenant")
    assert picker.status_code == 200, picker.text
    for slug in ("team-a", "team-b", "team-c"):
        assert f"Tenant {slug}" in picker.text
    assert "platform admin" in picker.text

    response = _switch(client, session_id, str(_TENANT_C))
    assert response.status_code == 303, response.text
    assert _session_row(session_id).tenant_id == _TENANT_C
    rows = _audit_rows("meho.ui.tenant.switch")
    assert rows[0].payload["tenant_role_source"] == "platform_admin"


# ---------------------------------------------------------------------------
# Only the current tenant shows
# ---------------------------------------------------------------------------


def _seed_target_and_approval(tenant_id: uuid.UUID, name: str) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                Target(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    name=name,
                    product="vsphere",
                    version="8.0",
                    host=f"{name}.example.test",
                )
            )
            session.add(
                ApprovalRequest(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    run_id=None,
                    principal_sub="someone-else",
                    op_id=f"{name}.op.create",
                    connector_id="vsphere-1.x",
                    target_id=None,
                    params_hash="0" * 64,
                    params={},
                    proposed_effect={"op_id": f"{name}.op.create"},
                    status=ApprovalRequestStatus.PENDING.value,
                    created_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )

    _run(_do())


def test_pages_show_only_the_current_tenant(keypair: Any) -> None:
    _seed_tenants((_TENANT_A, "team-a"), (_TENANT_B, "team-b"))
    _seed_target_and_approval(_TENANT_A, "target-in-a")
    _seed_target_and_approval(_TENANT_B, "target-in-b")
    session_id = _seed_session(_two_tenant_token(keypair), tenant_id=_TENANT_A)
    client = _client(session_id)

    def _seen() -> tuple[str, str, str]:
        targets = client.get("/ui/connectors").text
        approvals = client.get("/ui/approvals", headers={"HX-Request": "true"}).text
        dashboard = client.get("/ui/").text
        return targets, approvals, dashboard

    targets, approvals, dashboard = _seen()
    assert "target-in-a" in targets and "target-in-b" not in targets
    assert "target-in-a.op.create" in approvals and "target-in-b.op.create" not in approvals
    assert f'data-meho-tenant="{_TENANT_A}"' in dashboard
    assert 'data-testid="tenant-switch"' in dashboard
    assert "Tenant team-a" in dashboard

    assert _switch(client, session_id, str(_TENANT_B)).status_code == 303

    targets, approvals, dashboard = _seen()
    assert "target-in-b" in targets and "target-in-a" not in targets
    assert "target-in-b.op.create" in approvals and "target-in-a.op.create" not in approvals
    assert f'data-meho-tenant="{_TENANT_B}"' in dashboard
    assert "Tenant team-b" in dashboard


# ---------------------------------------------------------------------------
# Old browser tab
# ---------------------------------------------------------------------------


def test_form_from_a_page_of_the_old_tenant_is_refused(keypair: Any) -> None:
    _seed_tenants((_TENANT_A, "team-a"), (_TENANT_B, "team-b"))
    session_id = _seed_session(_two_tenant_token(keypair), tenant_id=_TENANT_A)
    client = _client(session_id)
    csrf = mint_csrf_token(str(session_id))
    assert _switch(client, session_id, str(_TENANT_B)).status_code == 303

    # htmx request from a page made for tenant A.
    htmx = client.post(
        "/ui/account/sessions/revoke-others",
        headers={"X-CSRF-Token": csrf, "HX-Request": "true", "Meho-Tenant": str(_TENANT_A)},
    )
    assert htmx.status_code == 409
    assert htmx.json()["detail"] == "The tenant changed. Reload the page."
    assert htmx.headers["x-meho-tenant-changed"] == "1"

    # Plain form from a page made for tenant A: the hidden field.
    form = client.post(
        f"/ui/account/sessions/{session_id}/revoke",
        data={"csrf_token": csrf, "meho_tenant": str(_TENANT_A)},
    )
    assert form.status_code == 409
    # Nothing ran: the session was not revoked.
    assert _session_row(session_id).revoked_at is None

    # A page load of an old tab's fragment is refused too.
    fragment = client.get("/ui/connectors", headers={"Meho-Tenant": str(_TENANT_A)})
    assert fragment.status_code == 409

    # The same request from a page made for tenant B runs.
    ok = client.get("/ui/connectors", headers={"Meho-Tenant": str(_TENANT_B)})
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
# Removed from a tenant
# ---------------------------------------------------------------------------


def test_removal_takes_the_person_to_the_only_tenant_left(keypair: Any) -> None:
    _seed_tenants((_TENANT_A, "team-a"), (_TENANT_B, "team-b"))
    session_id = _seed_session(_two_tenant_token(keypair), tenant_id=_TENANT_B)
    client = _client(session_id)
    # The next token refresh brings a list without tenant B.
    _replace_token(
        session_id,
        mint_token(keypair, sub=_PERSON, extra_claims={"meho_tenants": [_ENTRY_A_READERS]}),
    )

    response = client.get("/ui/account", headers={"Accept": "text/html"})

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/tenant?notice=removed"
    row = _session_row(session_id)
    assert (row.tenant_id, row.tenant_chosen, row.can_switch_tenant) == (_TENANT_A, True, False)
    picker = client.get(response.headers["location"])
    assert "You are no longer in the tenant you worked in" in picker.text
    assert "Tenant team-a" in picker.text


def test_removal_on_a_page_load_goes_back_to_the_picker(
    monkeypatch: pytest.MonkeyPatch, keypair: Any
) -> None:
    monkeypatch.setenv("UI_SESSION_READ_REVALIDATION_SECONDS", "0")
    get_settings.cache_clear()
    _seed_tenants((_TENANT_A, "team-a"), (_TENANT_B, "team-b"), (_TENANT_C, "team-c"))
    session_id = _seed_session(_two_tenant_token(keypair), tenant_id=_TENANT_B)
    client = _client(session_id)
    entry_c = f"{_TENANT_C}:team-c:Readers"
    _replace_token(
        session_id,
        mint_token(
            keypair, sub=_PERSON, extra_claims={"meho_tenants": [_ENTRY_A_READERS, entry_c]}
        ),
    )

    response = client.get("/ui/connectors")

    assert response.status_code == 302
    assert response.headers["location"] == "/ui/tenant?notice=removed"
    row = _session_row(session_id)
    assert row.tenant_chosen is False
    # Until a tenant is picked, only the picker opens.
    assert client.get("/ui/connectors").headers["location"] == "/ui/tenant"
    picker = client.get("/ui/tenant?notice=removed")
    assert "Choose a tenant to go on" in picker.text


# ---------------------------------------------------------------------------
# Account page
# ---------------------------------------------------------------------------


def test_account_page_lists_the_tenants_with_the_role_in_each(keypair: Any) -> None:
    _seed_tenants((_TENANT_A, "team-a"), (_TENANT_B, "team-b"))
    session_id = _seed_session(_two_tenant_token(keypair), tenant_id=_TENANT_B)

    page = _client(session_id).get("/ui/account")

    assert page.status_code == 200, page.text
    assert 'data-field="tenants"' in page.text
    assert "Tenant team-a" in page.text
    assert "Tenant team-b" in page.text
    assert "Readers" in page.text
    assert "Operators" in page.text
    # The role shown is the role in the current tenant (B: Operators).
    assert 'data-field="role">operator<' in page.text


# ---------------------------------------------------------------------------
# Page marker script
# ---------------------------------------------------------------------------


def test_every_page_loads_the_page_tenant_script() -> None:
    """``page-tenant.js`` is served and loads after the banner helper it reuses."""
    head = (static_root_dir().parent / "templates" / "_head_assets.html").read_text("utf-8")
    assert '<script src="/ui/static/src/app/page-tenant.js" defer></script>' in head
    assert head.index("session-expiry.js") < head.index("page-tenant.js")

    response = TestClient(_build_app()).get("/ui/static/src/app/page-tenant.js")
    assert response.status_code == 200
    for needle in ("Meho-Tenant", "meho_tenant", "x-meho-tenant-changed", "dataset.mehoTenant"):
        assert needle in response.text
