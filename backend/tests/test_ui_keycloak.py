# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the read-only Keycloak console UI surface.

Initiative #1943 (G10.x Keycloak console), Task #1959 (T1). The acceptance
criteria on issue #1959 are:

* ``GET /ui/keycloak`` renders the realm-config card + client list +
  client-scope list; the dispatched ``connector_id`` is
  ``keycloak-admin-26.x`` and the bare slug ``keycloak`` is NOT used (the
  pinned id is asserted on the captured ``call_operation`` arguments,
  mirroring the ``/ui/operations`` picker test that rejects the bare
  product slug).
* ``GET /ui/keycloak/clients/{uuid}`` renders projected client fields; a
  planted live ``secret`` does NOT appear in the response body and no
  verbatim ``OperationResult`` envelope blob is dumped into the page.
* A plain **operator** session (not tenant_admin) successfully renders
  ``/ui/keycloak`` and the client detail (reads are operator-tier).
* Route order: the literal ``/ui/keycloak/clients/{client_uuid}`` resolves
  to the client-detail handler, and ``build_keycloak_router`` is included
  before ``build_stubs_router()``.

Suite shape mirrors :mod:`backend.tests.test_ui_operations`: a minimal
FastAPI app with the UI session + CSRF middlewares, a ``web_session`` row
carrying a real Keycloak-minted access token (so the operator lift + role
probe re-verify the token and pick up the right :class:`TenantRole`), a
seeded keycloak ``Target`` row, and the route module's ``call_operation``
patched to a recording double (the dispatch contract itself is covered by
``test_operations_meta_tools`` / the keycloak connector suites; these BFF
tests verify the route wiring + the render branches).
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
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.db.models import Tenant
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
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    mint_csrf_token,
)
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_keycloak_router
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.keycloak.routes import KEYCLOAK_CONNECTOR_ID
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import AUDIENCE as _DEFAULT_AUDIENCE
from tests._oidc_jwt_helpers import ISSUER as _DEFAULT_ISSUER
from tests._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from tests._oidc_jwt_helpers import mint_token as _mint_token
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from tests._oidc_jwt_helpers import public_jwks as _public_jwks
from tests._route_tree_helpers import iter_routes

_BACKPLANE_URL = "https://meho.test"
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OP_OPERATOR = "op-operator"
_TARGET_NAME = "rdc-keycloak"

#: A live client secret planted in the dispatch double's envelope. The
#: client-detail render must NEVER echo this (the connector redacts secrets
#: upstream; the UI must not re-surface a raw blob that would carry it).
_PLANTED_SECRET = "PLANTED-LIVE-CLIENT-SECRET-do-not-leak"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the UI suites)."""
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
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    reset_dispatcher_caches()


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the keycloak UI tests."""
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


def _seed_keycloak_target(
    *,
    tenant_id: uuid.UUID,
    name: str = _TARGET_NAME,
    product: str = "keycloak",
) -> None:
    """Seed one keycloak ``Target`` row scoped to *tenant_id*."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                TargetORM(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    name=name,
                    product=product,
                    host="keycloak.test",
                ),
            )

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str,
    operator_sub: str,
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
    # Unique kid per call so a JWKS cached in a prior test cannot shadow
    # this token's signing key (the ``jws_signature_mismatch`` failure mode
    # when every test reuses one kid).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair(f"ui-keycloak-test-kid-{uuid.uuid4().hex[:8]}")
    return keypair, _public_jwks(keypair)


def _client_with_role(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter]:
    """Return a TestClient + UN-started respx mock for the keycloak routes.

    The operator lift + role probe re-validate the BFF session's access
    token through the JWT chain; the chain needs the JWKS endpoint mocked.
    The caller enters ``mock`` as a context manager (start AND stop exactly
    once) -- starting it here too would leave the httpx patch active after
    the ``with`` block, so the next test's JWKS fetch would hit stale routes.
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
    return client, mock


def _client_with_role_and_session(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter, uuid.UUID]:
    """Like :func:`_client_with_role` but also returns the seeded session id.

    The write tests need the session id to mint the matching double-submit
    CSRF token (the token is derived from the session id, so a forged POST
    without it -- or with a token minted from a different session -- fails
    the ``ui/csrf.py`` gate).
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
    return client, mock, session_id


def _csrf_kwargs(session_id: uuid.UUID) -> dict[str, Any]:
    """Header + cookie kwargs that satisfy the double-submit CSRF check."""
    token = mint_csrf_token(str(session_id))
    return {
        "headers": {CSRF_HEADER_NAME: token},
        "cookies": {CSRF_COOKIE_NAME: token},
    }


def _patch_call_operation(
    monkeypatch: pytest.MonkeyPatch, envelopes: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Patch the route module's ``call_operation`` to a recording double.

    *envelopes* maps an ``op_id`` to the structured envelope the double
    returns for it. Each call's ``arguments`` dict is recorded so a test can
    assert the BFF pinned ``connector_id`` + threaded the op id / target /
    params through unchanged.
    """
    received: list[dict[str, Any]] = []

    async def _fake_call_operation(operator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        received.append(arguments)
        op_id = arguments.get("op_id", "")
        return envelopes.get(
            op_id,
            {"status": "ok", "op_id": op_id, "result": {}, "error": None, "extras": {}},
        )

    monkeypatch.setattr(
        "meho_backplane.ui.routes.keycloak.routes.call_operation",
        _fake_call_operation,
    )
    return received


def _ok(op_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Build an ``ok`` envelope shaped like ``OperationResult.model_dump``."""
    return {
        "status": "ok",
        "op_id": op_id,
        "result": result,
        "error": None,
        "duration_ms": 1.0,
        "handle": None,
        "extras": {},
    }


def _index_envelopes() -> dict[str, dict[str, Any]]:
    """Envelopes for the three index dispatches (realm / clients / scopes)."""
    return {
        "keycloak.realm.get": _ok(
            "keycloak.realm.get",
            {
                "realm": {
                    "realm": "evba",
                    "enabled": True,
                    "displayName": "EVBA Realm",
                    "accessTokenLifespan": 300,
                }
            },
        ),
        "keycloak.client.list": _ok(
            "keycloak.client.list",
            {
                "rows": [
                    {
                        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "clientId": "meho-web",
                        "enabled": True,
                        "publicClient": False,
                        "protocol": "openid-connect",
                    }
                ],
                "total": 1,
            },
        ),
        "keycloak.client_scope.list": _ok(
            "keycloak.client_scope.list",
            {
                "rows": [
                    {
                        "name": "profile",
                        "protocol": "openid-connect",
                        "description": "User profile scope",
                    }
                ],
                "total": 1,
            },
        ),
    }


# ---------------------------------------------------------------------------
# Index: renders the three surfaces + pins connector_id (rejects bare slug)
# ---------------------------------------------------------------------------


def test_keycloak_ui_index_renders_three_surfaces_with_pinned_connector_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /ui/keycloak`` renders realm card + client list + scope list.

    The dispatched ``connector_id`` is ``keycloak-admin-26.x`` on EVERY
    dispatch; the bare slug ``keycloak`` is never used.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    received = _patch_call_operation(monkeypatch, _index_envelopes())
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get("/ui/keycloak")
    assert response.status_code == 200, response.text
    body = response.text
    # The three domain surfaces render.
    assert "data-keycloak-realm" in body
    assert "data-keycloak-clients" in body
    assert "data-keycloak-scopes" in body
    # Projected fields from each surface.
    assert "evba" in body  # realm name
    assert "meho-web" in body  # client clientId
    assert "profile" in body  # client scope name
    # The pinned connector id was used on every dispatch; the bare slug was
    # NEVER used.
    assert len(received) == 3
    op_ids = {args["op_id"] for args in received}
    assert op_ids == {
        "keycloak.realm.get",
        "keycloak.client.list",
        "keycloak.client_scope.list",
    }
    for args in received:
        assert args["connector_id"] == KEYCLOAK_CONNECTOR_ID
        assert args["connector_id"] != "keycloak"
        assert args["target"] == _TARGET_NAME


def test_keycloak_ui_index_no_target_prompts_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no keycloak target, the index prompts instead of dispatching."""
    _seed_tenant(_TENANT_A, "tenant-a")
    received = _patch_call_operation(monkeypatch, {})
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get("/ui/keycloak")
    assert response.status_code == 200, response.text
    assert "No Keycloak targets registered" in response.text
    # No dispatch happens without a resolvable target.
    assert received == []


def test_keycloak_ui_index_ignores_cross_tenant_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``?target=`` naming another tenant's target drives no dispatch.

    The no-tenant-override posture: a cross-tenant slug is absent from the
    operator's tenant-scoped target list, so it never selects an active
    target and never dispatches (no cross-tenant read).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_keycloak_target(tenant_id=_TENANT_B, name="other-keycloak")
    received = _patch_call_operation(monkeypatch, _index_envelopes())
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get("/ui/keycloak?target=other-keycloak")
    assert response.status_code == 200, response.text
    assert received == []


# ---------------------------------------------------------------------------
# Client detail: projected fields, secret-redaction trap, no raw envelope
# ---------------------------------------------------------------------------


def test_keycloak_ui_client_detail_redacts_secret_and_no_raw_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /ui/keycloak/clients/{uuid}`` renders projected fields only.

    A planted live ``secret`` in the envelope's client dict does NOT appear
    in the body, and no verbatim ``OperationResult`` envelope blob (the
    ``status``/``duration_ms``/``handle`` machinery) is dumped into the page.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    client_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    # The connector redacts secrets upstream; we plant one here to prove the
    # UI does not re-surface a raw blob carrying it even if it slipped through.
    detail_env = _ok(
        "keycloak.client.get",
        {
            "client": {
                "id": client_uuid,
                "clientId": "meho-web",
                "enabled": True,
                "publicClient": False,
                "protocol": "openid-connect",
                "secret": _PLANTED_SECRET,
                "redirectUris": ["https://meho.test/ui/auth/callback"],
                "protocolMappers": [
                    {"name": "audience-mapper", "protocolMapper": "oidc-audience-mapper"}
                ],
            }
        },
    )
    received = _patch_call_operation(monkeypatch, {"keycloak.client.get": detail_env})
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(f"/ui/keycloak/clients/{client_uuid}?target={_TARGET_NAME}")
    assert response.status_code == 200, response.text
    body = response.text
    # Projected fields render.
    assert "meho-web" in body
    assert "https://meho.test/ui/auth/callback" in body
    assert "audience-mapper" in body
    # The planted secret never reaches the body.
    assert _PLANTED_SECRET not in body
    # No verbatim envelope blob -- the render projects named fields, never
    # the raw OperationResult machinery.
    assert "duration_ms" not in body
    assert '"handle"' not in body
    # The dispatch pinned the connector id + keyed on the internal UUID.
    assert len(received) == 1
    args = received[0]
    assert args["connector_id"] == KEYCLOAK_CONNECTOR_ID
    assert args["op_id"] == "keycloak.client.get"
    assert args["params"] == {"id": client_uuid}
    assert args["target"] == _TARGET_NAME


def test_keycloak_ui_client_detail_cross_links_to_principals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client-detail view links to the agent-principals kill switch."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    client_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    detail_env = _ok(
        "keycloak.client.get",
        {"client": {"id": client_uuid, "clientId": "meho-web", "enabled": True}},
    )
    _patch_call_operation(monkeypatch, {"keycloak.client.get": detail_env})
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(f"/ui/keycloak/clients/{client_uuid}?target={_TARGET_NAME}")
    assert response.status_code == 200, response.text
    assert "/ui/agents/principals" in response.text


# ---------------------------------------------------------------------------
# RBAC: reads are operator-tier (a non-admin renders both surfaces)
# ---------------------------------------------------------------------------


def test_keycloak_ui_operator_role_can_render_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain operator (not tenant_admin) renders ``/ui/keycloak``."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    _patch_call_operation(monkeypatch, _index_envelopes())
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get("/ui/keycloak")
    assert response.status_code == 200, response.text
    assert "data-keycloak-clients" in response.text


def test_keycloak_ui_operator_role_can_render_client_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain operator (not tenant_admin) renders the client detail."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    client_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    detail_env = _ok(
        "keycloak.client.get",
        {"client": {"id": client_uuid, "clientId": "meho-web", "enabled": True}},
    )
    _patch_call_operation(monkeypatch, {"keycloak.client.get": detail_env})
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(f"/ui/keycloak/clients/{client_uuid}?target={_TARGET_NAME}")
    assert response.status_code == 200, response.text
    assert "meho-web" in response.text


# ---------------------------------------------------------------------------
# Route order: literal clients/{uuid} resolves to the detail handler
# ---------------------------------------------------------------------------


def test_keycloak_ui_router_included_before_stubs() -> None:
    """``build_keycloak_router`` is included before ``build_stubs_router()``."""
    import inspect

    from meho_backplane.ui import routes as routes_module

    source = inspect.getsource(routes_module.build_router)
    kc_pos = source.index("build_keycloak_router()")
    stubs_pos = source.index("build_stubs_router()")
    assert kc_pos < stubs_pos


def test_keycloak_ui_client_detail_route_resolves_to_detail_handler() -> None:
    """The literal ``/ui/keycloak/clients/{client_uuid}`` resolves to detail.

    A future literal ``/ui/keycloak/users`` (T2) registered before any
    ``{param}`` route would resolve before the params route (first-match-
    wins): the only ``{param}`` route here sits under the distinct
    ``/ui/keycloak/clients/`` prefix.
    """
    router = build_keycloak_router()
    # 0.137+ nests included routers; iter_routes flattens the tree in
    # registration order so the first-match-wins ordering check still holds.
    paths = [getattr(route, "path", None) for route in iter_routes(router.routes)]
    assert "/ui/keycloak/clients/{client_uuid}" in paths
    assert "/ui/keycloak" in paths
    # The client-detail (parametrised) route is registered before the bare
    # index so the first-match-wins lookup is unambiguous.
    detail_idx = paths.index("/ui/keycloak/clients/{client_uuid}")
    index_idx = paths.index("/ui/keycloak")
    assert detail_idx < index_idx


# ---------------------------------------------------------------------------
# User management (Task #1960): list + create / reset-password / role-assign
# ---------------------------------------------------------------------------


def _awaiting_approval(op_id: str, approval_request_id: str) -> dict[str, Any]:
    """Build an ``awaiting_approval`` envelope like the policy gate returns.

    Every keycloak write op is ``requires_approval=True``, so a confirmed
    write returns ``status="awaiting_approval"`` with the durable
    ApprovalRequest id under ``extras["approval_request_id"]``.
    """
    return {
        "status": "awaiting_approval",
        "op_id": op_id,
        "result": None,
        "error": None,
        "duration_ms": 1.0,
        "handle": None,
        "extras": {"approval_request_id": approval_request_id},
    }


def _seed_user_target_and_envs(
    *, users: list[dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    """Seed tenant A + a keycloak target; return the user.list envelope map."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    rows = (
        users
        if users is not None
        else [
            {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "username": "operator-a",
                "email": "operator-a@example.com",
                "enabled": True,
                "emailVerified": True,
            }
        ]
    )
    return {"keycloak.user.list": _ok("keycloak.user.list", {"rows": rows, "total": len(rows)})}


# ---- user list: read is operator-tier, writes soft-hidden ------------------


def test_keycloak_ui_user_list_operator_sees_no_write_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain operator renders the user list but sees NO write affordances.

    Reads are operator-tier (``keycloak.user.list`` is ``safe``); the
    create / reset / assign buttons are soft-hidden from a non-admin via the
    ``resolve_role_probe``-driven ``is_tenant_admin`` flag.
    """
    received = _patch_call_operation(monkeypatch, _seed_user_target_and_envs())
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get("/ui/keycloak/users")
    assert response.status_code == 200, response.text
    body = response.text
    # The list renders with the projected user row.
    assert "data-keycloak-users" in body
    assert "operator-a" in body
    # No write affordances for a plain operator.
    assert "data-keycloak-user-create" not in body
    assert "data-keycloak-reset-password" not in body
    assert "data-keycloak-role-assign" not in body
    # The read pinned the connector id (never the bare slug).
    assert len(received) == 1
    assert received[0]["connector_id"] == KEYCLOAK_CONNECTOR_ID
    assert received[0]["op_id"] == "keycloak.user.list"


def test_keycloak_ui_user_list_admin_sees_write_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant_admin sees the create / reset / assign affordances."""
    _patch_call_operation(monkeypatch, _seed_user_target_and_envs())
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        response = client.get("/ui/keycloak/users")
    assert response.status_code == 200, response.text
    body = response.text
    assert "data-keycloak-user-create" in body
    assert "data-keycloak-reset-password" in body
    assert "data-keycloak-role-assign" in body


# ---- create: awaiting_approval banner + deep-link --------------------------


def test_keycloak_ui_user_create_awaiting_approval_deep_links_to_approvals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed create whose dispatch returns ``awaiting_approval`` renders
    the approval banner with the request id AND a link to ``/ui/approvals``.

    The operator is never shown a silent / empty success: every keycloak
    write is ``requires_approval=True``, so the result MUST surface the
    parked ApprovalRequest with a deep-link into the approvals surface.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    approval_id = "33333333-3333-3333-3333-333333333333"
    received = _patch_call_operation(
        monkeypatch,
        {"keycloak.user.create": _awaiting_approval("keycloak.user.create", approval_id)},
    )
    client, mock, session_id = _client_with_role_and_session(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        response = client.post(
            "/ui/keycloak/users/create",
            data={
                "target": _TARGET_NAME,
                "representation": '{"username": "operator-a", "enabled": true}',
                "password_secret_ref": "rdc/keycloak/operator-a",
            },
            **_csrf_kwargs(session_id),
        )
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-write-status="awaiting_approval"' in body
    assert approval_id in body
    assert f'href="/ui/approvals/{approval_id}"' in body
    # The dispatch pinned the connector id + carried the representation.
    assert len(received) == 1
    assert received[0]["connector_id"] == KEYCLOAK_CONNECTOR_ID
    assert received[0]["op_id"] == "keycloak.user.create"


# ---- Vault-ref password, never inline --------------------------------------


def test_keycloak_ui_user_create_password_is_vault_ref_never_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create dispatches a ``password_secret_ref`` and NO plaintext password.

    The create form exposes a Vault KV path field and no plaintext password
    field; the dispatched params carry ``password_secret_ref`` and contain
    no key named ``password`` / ``value`` and no plaintext secret.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    received = _patch_call_operation(
        monkeypatch,
        {"keycloak.user.create": _awaiting_approval("keycloak.user.create", "id-1")},
    )
    client, mock, session_id = _client_with_role_and_session(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    # The modal GET exposes the Vault-ref field and no plaintext password.
    with mock:
        modal = client.get(f"/ui/keycloak/users/create?target={_TARGET_NAME}")
        assert modal.status_code == 200, modal.text
        assert 'name="password_secret_ref"' in modal.text
        assert 'name="password"' not in modal.text
        assert 'type="password"' not in modal.text
        # The dispatched params carry only the Vault ref, never a value.
        response = client.post(
            "/ui/keycloak/users/create",
            data={
                "target": _TARGET_NAME,
                "representation": '{"username": "operator-a"}',
                "password_secret_ref": "rdc/keycloak/operator-a",
            },
            **_csrf_kwargs(session_id),
        )
    assert response.status_code == 200, response.text
    assert len(received) == 1
    params = received[0]["params"]
    assert params["password_secret_ref"] == "rdc/keycloak/operator-a"
    assert "password" not in params
    assert "value" not in params
    # The representation carries no credentials block / plaintext secret.
    assert "password" not in params["representation"]


# ---- role-assign: privilege-grant confirm + CSRF ---------------------------


def test_keycloak_ui_role_assign_modal_names_privilege_grant_and_dangerous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The role-assign modal names it a privilege grant + surfaces dangerous."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    user_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    _patch_call_operation(
        monkeypatch,
        {
            "keycloak.role_mapping.get": _ok(
                "keycloak.role_mapping.get",
                {"role_mappings": {"realmMappings": [{"name": "viewer"}], "clientMappings": {}}},
            )
        },
    )
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        response = client.get(f"/ui/keycloak/users/{user_uuid}/roles/assign?target={_TARGET_NAME}")
    assert response.status_code == 200, response.text
    body = response.text
    assert "data-keycloak-privilege-grant" in body
    assert "Privilege grant" in body
    assert "data-keycloak-safety>safety: dangerous" in body
    # Current realm roles render for context.
    assert "viewer" in body


def test_keycloak_ui_role_assign_without_csrf_403_with_csrf_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST .../roles/assign`` without CSRF -> 403; with it -> dispatches.

    The dispatch carries ``{"roles": [...], "id": <uuid>}`` with roles as a
    string list, and the op id is ``keycloak.role_mapping.assign``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    user_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    received = _patch_call_operation(
        monkeypatch,
        {
            "keycloak.role_mapping.assign": _awaiting_approval(
                "keycloak.role_mapping.assign", "approval-rm-1"
            )
        },
    )
    client, mock, session_id = _client_with_role_and_session(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        # No CSRF token -> the double-submit middleware rejects with 403.
        no_csrf = client.post(
            f"/ui/keycloak/users/{user_uuid}/roles/assign",
            data={"target": _TARGET_NAME, "roles": ["operator", "auditor"]},
        )
        assert no_csrf.status_code == 403, no_csrf.text
        assert received == []
        # With the matching CSRF pair -> dispatches.
        ok = client.post(
            f"/ui/keycloak/users/{user_uuid}/roles/assign",
            data={"target": _TARGET_NAME, "roles": ["operator", "auditor"]},
            **_csrf_kwargs(session_id),
        )
    assert ok.status_code == 200, ok.text
    assert 'data-write-status="awaiting_approval"' in ok.text
    assert len(received) == 1
    args = received[0]
    assert args["op_id"] == "keycloak.role_mapping.assign"
    assert args["connector_id"] == KEYCLOAK_CONNECTOR_ID
    assert args["params"] == {"roles": ["operator", "auditor"], "id": user_uuid}


# ---- RBAC: non-admin write POST hard-403s ----------------------------------


def test_keycloak_ui_user_create_non_admin_post_is_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged create POST from a plain operator hard-403s (server-side gate).

    The soft-hide hides the button; ``_resolve_admin_or_403`` is the
    authority -- a non-admin POST never dispatches.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    received = _patch_call_operation(
        monkeypatch,
        {"keycloak.user.create": _awaiting_approval("keycloak.user.create", "id-x")},
    )
    client, mock, session_id = _client_with_role_and_session(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/keycloak/users/create",
            data={
                "target": _TARGET_NAME,
                "representation": '{"username": "operator-a"}',
                "password_secret_ref": "rdc/keycloak/operator-a",
            },
            **_csrf_kwargs(session_id),
        )
    assert response.status_code == 403, response.text
    # The dispatch never happened.
    assert received == []


def test_keycloak_ui_reset_password_non_admin_post_is_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged reset-password POST from a plain operator hard-403s."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    user_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    received = _patch_call_operation(monkeypatch, {})
    client, mock, session_id = _client_with_role_and_session(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            f"/ui/keycloak/users/{user_uuid}/reset-password",
            data={"target": _TARGET_NAME, "password_secret_ref": "rdc/keycloak/operator-a"},
            **_csrf_kwargs(session_id),
        )
    assert response.status_code == 403, response.text
    assert received == []


def test_keycloak_ui_role_assign_non_admin_post_is_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged role-assign POST from a plain operator hard-403s."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    user_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    received = _patch_call_operation(monkeypatch, {})
    client, mock, session_id = _client_with_role_and_session(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            f"/ui/keycloak/users/{user_uuid}/roles/assign",
            data={"target": _TARGET_NAME, "roles": ["operator"]},
            **_csrf_kwargs(session_id),
        )
    assert response.status_code == 403, response.text
    assert received == []


# ---- reset-password: Vault-ref, awaiting_approval --------------------------


def test_keycloak_ui_reset_password_vault_ref_awaiting_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reset-password collects a Vault ref (no plaintext) + deep-links approvals."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    user_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    received = _patch_call_operation(
        monkeypatch,
        {
            "keycloak.user.reset_password": _awaiting_approval(
                "keycloak.user.reset_password", "approval-rp-1"
            )
        },
    )
    client, mock, session_id = _client_with_role_and_session(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        modal = client.get(f"/ui/keycloak/users/{user_uuid}/reset-password?target={_TARGET_NAME}")
        assert modal.status_code == 200, modal.text
        assert 'name="password_secret_ref"' in modal.text
        assert 'type="password"' not in modal.text
        response = client.post(
            f"/ui/keycloak/users/{user_uuid}/reset-password",
            data={"target": _TARGET_NAME, "password_secret_ref": "rdc/keycloak/operator-a"},
            **_csrf_kwargs(session_id),
        )
    assert response.status_code == 200, response.text
    assert 'data-write-status="awaiting_approval"' in response.text
    assert len(received) == 1
    params = received[0]["params"]
    assert params["id"] == user_uuid
    assert params["password_secret_ref"] == "rdc/keycloak/operator-a"
    assert "password" not in params
    assert "value" not in params


# ---- route order: literal users/create before users/{uuid} -----------------


def test_keycloak_ui_user_routes_literal_before_param() -> None:
    """The literal ``users/create`` is registered before ``users/{user_uuid}``.

    First-match-wins: ``create`` must never be captured as a ``{user_uuid}``.
    """
    router = build_keycloak_router()
    # 0.137+ nests included routers; iter_routes flattens the tree in
    # registration order so the first-match-wins ordering check still holds.
    paths = [getattr(route, "path", None) for route in iter_routes(router.routes)]
    assert "/ui/keycloak/users" in paths
    assert "/ui/keycloak/users/create" in paths
    create_idx = paths.index("/ui/keycloak/users/create")
    # Every parametrised user route sits after the literal create route.
    param_paths = [p for p in paths if p and p.startswith("/ui/keycloak/users/{user_uuid}")]
    assert param_paths, "expected at least one /ui/keycloak/users/{user_uuid} route"
    for p in param_paths:
        assert create_idx < paths.index(p)
