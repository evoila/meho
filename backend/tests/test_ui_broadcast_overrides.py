# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Broadcast Overrides tab UI surface.

Initiative #1842 (G10.x operator console), Task #1891. The Overrides tab
on ``/ui/broadcast`` lets a tenant admin list / create / delete
:class:`BroadcastOverride` rules in-console, through the in-process REST
impl functions (never the Bearer API, never the DB directly).

Acceptance criteria asserted here:

1. ``GET /ui/broadcast/overrides`` as tenant_admin → 200 + the rules
   table; as a non-admin operator → 403 gated empty state, never the
   table (``test_overrides_tab_tenant_admin_gated``).
2. ``POST`` with a regex-bearing ``op_id_pattern`` surfaces the 422
   glob-not-regex message inline; a duplicate surfaces the 409
   ``broadcast_override_already_exists`` inline
   (``test_overrides_create_surfaces_422_and_409``).
3. The delete-confirm modal body carries the re-exposure warning; a
   cross-tenant override id DELETE returns 404, not 403
   (``test_overrides_delete_cross_tenant_404``).
4. A mutation missing the CSRF token gets ``csrf_token_invalid`` 403
   (``test_overrides_mutation_requires_csrf``).
5. ``GET /ui/broadcast/overrides`` resolves to the overrides handler,
   not the event handler -- the overrides router is included before the
   event router (``test_overrides_route_resolves_before_event``).

The role-gated routes lift the operator via the BFF session's access
token through the JWT chain, so the RBAC tests mint a real RSA-signed
JWT carrying the role + mock the JWKS endpoint, mirroring
:mod:`backend.tests.test_ui_connectors_view`.
"""

from __future__ import annotations

import asyncio
import uuid
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
from meho_backplane.db.models import BroadcastOverride, Tenant
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
_OP_ADMIN = "op-admin"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars + reset global state per test.

    Mirrors :func:`backend.tests.test_ui_connectors_view._bff_env`: the
    JWT chain that ``resolve_operator_or_403`` runs needs the issuer /
    audience to match the OIDC helper's minted token.
    """
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


def _iter_routes(routes):  # type: ignore[no-untyped-def]
    """Recursively yield leaf routes from the FastAPI 0.137+ route tree."""
    for route in routes:
        if hasattr(route, "routes"):
            yield from _iter_routes(route.routes)
        else:
            yield route


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the broadcast UI tests."""
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
    """Insert one ``tenant`` row so the BroadcastOverride FK resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str,
    operator_sub: str = _OP_ADMIN,
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


def _seed_override(
    *,
    tenant_id: uuid.UUID,
    op_id_pattern: str = "vault.kv.*",
    scope_field: str | None = None,
    scope_value: str | None = None,
    detail: str = "aggregate",
    created_by_sub: str = _OP_ADMIN,
) -> uuid.UUID:
    """Insert one ``broadcast_override`` row and return its id."""
    override_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                BroadcastOverride(
                    id=override_id,
                    tenant_id=tenant_id,
                    op_id_pattern=op_id_pattern,
                    scope_field=scope_field,
                    scope_value=scope_value,
                    detail=detail,
                    created_by_sub=created_by_sub,
                ),
            )

    asyncio.run(_do())
    return override_id


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    """Mint a stable RSA-2048 keypair + the matching JWKS document."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-overrides-test-kid")
    return keypair, _public_jwks(keypair)


def _authenticated_client_with_role(
    *,
    tenant_id: uuid.UUID,
    role: TenantRole,
    operator_sub: str = _OP_ADMIN,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + entered respx mock + csrf token for the role-gated routes.

    The role lift re-validates the BFF session's access token through the
    JWT chain, so the JWKS endpoint must be mocked. The caller is
    responsible for stopping ``mock``.
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
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _csrf_headers(token: str) -> dict[str, str]:
    """Headers for an HTMX state-changing request -- CSRF + HX-Request."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


# ---------------------------------------------------------------------------
# AC1 -- tenant_admin gate on the list fragment
# ---------------------------------------------------------------------------


def test_overrides_tab_tenant_admin_gated() -> None:
    """tenant_admin sees the rules table; a non-admin operator does not.

    AC1: the GET fragment returns 200 + the rules table for a
    tenant_admin and the 403-driven gated empty state (never the table)
    for a plain operator, using the ``resolve_operator_or_403`` lift.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_override(tenant_id=_TENANT_A, op_id_pattern="vault.kv.read")

    # tenant_admin: 200 + table.
    client, mock, _ = _authenticated_client_with_role(
        tenant_id=_TENANT_A,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        admin_resp = client.get("/ui/broadcast/overrides")
    finally:
        mock.stop()
    assert admin_resp.status_code == 200, admin_resp.text
    assert "vault.kv.read" in admin_resp.text
    assert 'aria-label="Broadcast suppression rules"' in admin_resp.text
    assert "Overrides require tenant_admin" not in admin_resp.text

    # Clear the cached JWKS so the operator client's distinct keypair is
    # re-fetched -- the two clients mint under the same kid, and a stale
    # cached JWKS from the admin keypair would fail the operator token's
    # signature check (jws_signature_mismatch) for the wrong reason.
    clear_jwks_cache()
    clear_discovery_cache()

    # plain operator: 403 gated state, never the rules.
    op_client, op_mock, _ = _authenticated_client_with_role(
        tenant_id=_TENANT_A,
        role=TenantRole.OPERATOR,
        operator_sub="op-plain",
    )
    try:
        op_resp = op_client.get("/ui/broadcast/overrides")
    finally:
        op_mock.stop()
    assert op_resp.status_code == 403, op_resp.text
    assert "Overrides require tenant_admin" in op_resp.text
    assert "vault.kv.read" not in op_resp.text
    assert 'aria-label="Broadcast suppression rules"' not in op_resp.text


# ---------------------------------------------------------------------------
# AC2 -- 422 glob-not-regex + 409 already-exists surfaced inline
# ---------------------------------------------------------------------------


def test_overrides_create_surfaces_422_and_409() -> None:
    """A regex pattern surfaces the 422; a duplicate surfaces the 409 -- inline.

    AC2: a regex-bearing ``op_id_pattern`` surfaces the backend's
    glob-not-regex message (not a 500, not a blank success); a duplicate
    ``(tenant_id, op_id_pattern, scope_field, scope_value)`` surfaces the
    409 ``broadcast_override_already_exists`` message.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _authenticated_client_with_role(
        tenant_id=_TENANT_A,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        # Regex pattern -> 422 glob-not-regex, echoed inline.
        regex_resp = client.post(
            "/ui/broadcast/overrides",
            data={
                "op_id_pattern": "k8s.(get|list)",
                "detail": "aggregate",
                "scope_field": "",
                "scope_value": "",
            },
            headers=_csrf_headers(csrf),
        )
        assert regex_resp.status_code == 422, regex_resp.text
        assert "glob, not regex" in regex_resp.text

        # Valid create -> happy path (so the duplicate below collides).
        # A SCOPED rule: the composite-unique index treats NULL scope
        # values as distinct (SQL standard), so two op-wide rules with
        # the same pattern would NOT collide -- the natural-key
        # uniqueness only bites on a non-NULL scope pair (mirrors the
        # REST 409 test).
        ok_resp = client.post(
            "/ui/broadcast/overrides",
            data={
                "op_id_pattern": "vault.kv.read",
                "detail": "aggregate",
                "scope_field": "namespace",
                "scope_value": "kube-system",
            },
            headers=_csrf_headers(csrf),
        )
        assert ok_resp.status_code == 200, ok_resp.text
        assert "vault.kv.read" in ok_resp.text

        # Duplicate -> 409 already-exists, echoed inline.
        dup_resp = client.post(
            "/ui/broadcast/overrides",
            data={
                "op_id_pattern": "vault.kv.read",
                "detail": "aggregate",
                "scope_field": "namespace",
                "scope_value": "kube-system",
            },
            headers=_csrf_headers(csrf),
        )
        assert dup_resp.status_code == 409, dup_resp.text
        assert "already exists" in dup_resp.text.lower()
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# AC3 -- delete-confirm re-exposure warning + cross-tenant 404
# ---------------------------------------------------------------------------


def test_overrides_delete_confirm_warns_reexposure() -> None:
    """The rendered fragment carries the literal re-exposure warning.

    AC3 (first half): the delete-confirm modal body spells out that
    deleting re-exposes suppressed detail on the feed + Slack mirror.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_override(tenant_id=_TENANT_A, op_id_pattern="vault.kv.read")
    client, mock, _ = _authenticated_client_with_role(
        tenant_id=_TENANT_A,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        resp = client.get("/ui/broadcast/overrides")
    finally:
        mock.stop()
    assert resp.status_code == 200, resp.text
    html = resp.text.lower()
    assert "re-expose" in html
    assert "slack mirror" in html


def test_overrides_delete_cross_tenant_404() -> None:
    """A DELETE on another tenant's override id returns 404, not 403.

    AC3 (second half): existence is not leaked across tenants -- a
    cross-tenant id matches zero rows under the tenant filter, so the
    backend raises 404 ``broadcast_override_not_found``, never 403.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    # The override belongs to tenant B; the caller is a tenant-A admin.
    other_tenant_override = _seed_override(
        tenant_id=_TENANT_B,
        op_id_pattern="vault.kv.read",
    )
    client, mock, csrf = _authenticated_client_with_role(
        tenant_id=_TENANT_A,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        resp = client.delete(
            f"/ui/broadcast/overrides/{other_tenant_override}",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert resp.status_code == 404, resp.text
    assert resp.status_code != 403
    assert "already removed" in resp.text.lower()


# ---------------------------------------------------------------------------
# AC4 -- CSRF double-submit required on mutations
# ---------------------------------------------------------------------------


def test_overrides_mutation_requires_csrf() -> None:
    """A create POST missing the CSRF token gets the csrf_token_invalid 403.

    AC4: every UI mutation carries the double-submit token; a request
    missing it is rejected by the chassis CSRF middleware before the
    handler runs.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _ = _authenticated_client_with_role(
        tenant_id=_TENANT_A,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        # No X-CSRF-Token header and no csrf_token form field.
        resp = client.post(
            "/ui/broadcast/overrides",
            data={
                "op_id_pattern": "vault.kv.read",
                "detail": "aggregate",
            },
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "csrf_token_invalid"


# ---------------------------------------------------------------------------
# AC5 -- overrides route resolves before the event route
# ---------------------------------------------------------------------------


def test_overrides_route_resolves_before_event() -> None:
    """``GET /ui/broadcast/overrides`` binds the overrides handler.

    AC5: the overrides router is included before the event router, so the
    literal ``/ui/broadcast/overrides`` is matched ahead of the
    parametrised ``/ui/broadcast/event/{audit_id}`` -- the GET resolves to
    the overrides handler (renders the rules pane), not the event drawer.
    """
    app = _build_app()
    # GET and POST share the ``/ui/broadcast/overrides`` path, so key on
    # (path, method) to disambiguate. The GET must bind the overrides
    # list handler (registered before the event router), never the event
    # drawer's ``/ui/broadcast/event/{audit_id}``.
    # Use _iter_routes to walk the 0.137+ route tree (include_router now
    # produces nested _IncludedRouter objects, not a flat list).
    routes = {
        (getattr(route, "path", ""), method): getattr(route, "name", None)
        for route in _iter_routes(app.routes)
        for method in (getattr(route, "methods", None) or set())
        if getattr(route, "path", "").startswith("/ui/broadcast/overrides")
    }
    assert routes.get(("/ui/broadcast/overrides", "GET")) == "ui_broadcast_overrides"
    assert routes.get(("/ui/broadcast/overrides", "POST")) == "ui_broadcast_overrides_create"
    assert (
        routes.get(("/ui/broadcast/overrides/{override_id}", "DELETE"))
        == "ui_broadcast_overrides_delete"
    )

    # The overrides router is included before the event router in
    # build_router(), so a literal /ui/broadcast/overrides resolves to the
    # overrides list handler, not the event handler.
    overrides_idx = next(
        i
        for i, route in enumerate(app.routes)
        if getattr(route, "path", "") == "/ui/broadcast/overrides"
    )
    event_idx = next(
        i
        for i, route in enumerate(app.routes)
        if getattr(route, "path", "") == "/ui/broadcast/event/{audit_id}"
    )
    assert overrides_idx < event_idx

    # And it actually renders the overrides pane (not the event drawer).
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _ = _authenticated_client_with_role(
        tenant_id=_TENANT_A,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        resp = client.get("/ui/broadcast/overrides")
    finally:
        mock.stop()
    assert resp.status_code == 200, resp.text
    assert 'id="broadcast-overrides-pane"' in resp.text
    assert 'id="event-drawer"' not in resp.text
