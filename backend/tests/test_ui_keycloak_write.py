# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Keycloak console authoring (write) UI surface.

Initiative #1943 (G10.x Keycloak console), Task #1961 (T3). Covers the two
approval-gated authoring writes built on T1's (#1959) read scaffold:

* ``POST /ui/keycloak/client-scopes/create`` -> ``keycloak.client_scope.create``
* ``POST /ui/keycloak/clients/{uuid}/protocol-mappers/create``
  -> ``keycloak.protocol_mapper.create``

Acceptance criteria asserted here (issue #1961):

* A confirmed scope-create POST whose dispatch returns
  ``status="awaiting_approval"`` renders the approval banner with
  ``extras["approval_request_id"]`` AND a link targeting ``/ui/approvals``.
* The protocol-mapper POST dispatches with ``params`` carrying
  ``{"representation": {...}, "id": <uuid>}`` -- the client keyed off the
  route path, not a free-form field (asserted against captured
  ``call_operation`` arguments).
* Confirm + CSRF: each create modal renders an unmissable confirm surfacing
  caution / requires-approval; a POST without the CSRF token -> 403, with it
  -> awaiting_approval.
* RBAC: a plain operator sees the relation/read surface but NOT the create
  buttons (soft-hide); a forged POST as a non-admin -> 403; a tenant_admin
  sees the buttons + the POST dispatches.

Suite shape mirrors :mod:`backend.tests.test_ui_keycloak`: a minimal FastAPI
app with the UI session + CSRF middlewares, a ``web_session`` row carrying a
real Keycloak-minted access token (so the operator lift + admin gate
re-verify the token and pick up the right :class:`TenantRole`), a seeded
keycloak ``Target`` row, and the WRITE module's ``call_operation`` patched to
a recording double.
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
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.keycloak.routes import KEYCLOAK_CONNECTOR_ID
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import AUDIENCE as _DEFAULT_AUDIENCE
from tests._oidc_jwt_helpers import ISSUER as _DEFAULT_ISSUER
from tests._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from tests._oidc_jwt_helpers import mint_token as _mint_token
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from tests._oidc_jwt_helpers import public_jwks as _public_jwks

_BACKPLANE_URL = "https://meho.test"
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OP_OPERATOR = "op-operator"
_TARGET_NAME = "rdc-keycloak"
_CLIENT_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_APPROVAL_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


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


def _seed_keycloak_target(*, tenant_id: uuid.UUID, name: str = _TARGET_NAME) -> None:
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                TargetORM(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    name=name,
                    product="keycloak",
                    host="keycloak.test",
                ),
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


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair(f"ui-keycloak-write-kid-{uuid.uuid4().hex[:8]}")
    return keypair, _public_jwks(keypair)


def _client_with_role(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter, uuid.UUID]:
    """Return a TestClient + UN-started respx mock + the session id.

    The session id is returned so a test can mint a matching CSRF token (the
    confirm POST's double-submit pair binds the token to the session id).
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


def _patch_write_call_operation(
    monkeypatch: pytest.MonkeyPatch, envelopes: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Patch the WRITE module's ``call_operation`` to a recording double."""
    received: list[dict[str, Any]] = []

    async def _fake_call_operation(operator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        received.append(arguments)
        op_id = arguments.get("op_id", "")
        return envelopes.get(
            op_id,
            {"status": "ok", "op_id": op_id, "result": {}, "error": None, "extras": {}},
        )

    monkeypatch.setattr(
        "meho_backplane.ui.routes.keycloak.write.call_operation",
        _fake_call_operation,
    )
    return received


def _awaiting_approval(op_id: str) -> dict[str, Any]:
    """Build an ``awaiting_approval`` envelope carrying an approval id."""
    return {
        "status": "awaiting_approval",
        "op_id": op_id,
        "result": None,
        "error": None,
        "duration_ms": 1.0,
        "handle": None,
        "extras": {"approval_request_id": _APPROVAL_ID},
    }


def _csrf_cookie_and_header(
    client: TestClient, session_id: uuid.UUID
) -> tuple[str, dict[str, str]]:
    """Mint a token, set the matching ``meho_csrf`` cookie, return the header."""
    token = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, token)
    return token, {"X-CSRF-Token": token}


# ---------------------------------------------------------------------------
# client_scope.create -- awaiting_approval banner + /ui/approvals deep-link
# ---------------------------------------------------------------------------


def test_keycloak_ui_client_scope_create_awaiting_approval_deep_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed scope-create POST renders the awaiting_approval banner.

    The banner surfaces ``extras["approval_request_id"]`` and a link whose
    href targets ``/ui/approvals`` (the deep-link handoff).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    received = _patch_write_call_operation(
        monkeypatch,
        {"keycloak.client_scope.create": _awaiting_approval("keycloak.client_scope.create")},
    )
    client, mock, session_id = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    _, headers = _csrf_cookie_and_header(client, session_id)
    with mock:
        response = client.post(
            "/ui/keycloak/client-scopes/create",
            data={"target": _TARGET_NAME, "name": "meho-tenant", "protocol": "openid-connect"},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-write-status="awaiting_approval"' in body
    assert _APPROVAL_ID in body
    assert "/ui/approvals" in body
    # The dispatch pinned the connector id + carried the representation.
    assert len(received) == 1
    args = received[0]
    assert args["connector_id"] == KEYCLOAK_CONNECTOR_ID
    assert args["op_id"] == "keycloak.client_scope.create"
    assert args["target"] == _TARGET_NAME
    assert args["params"]["representation"]["name"] == "meho-tenant"


# ---------------------------------------------------------------------------
# protocol_mapper.create -- client keyed off the route path, not a form field
# ---------------------------------------------------------------------------


def test_keycloak_ui_protocol_mapper_create_keys_client_off_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mapper POST dispatches with ``{"representation": {...}, "id": uuid}``.

    The client is keyed off the route's ``{client_uuid}`` -- not a free-form
    field -- so it cannot be re-pointed by a forged form value.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    received = _patch_write_call_operation(
        monkeypatch,
        {"keycloak.protocol_mapper.create": _awaiting_approval("keycloak.protocol_mapper.create")},
    )
    client, mock, session_id = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    _, headers = _csrf_cookie_and_header(client, session_id)
    with mock:
        response = client.post(
            f"/ui/keycloak/clients/{_CLIENT_UUID}/protocol-mappers/create",
            data={
                "target": _TARGET_NAME,
                "name": "tenant-id-mapper",
                "protocol": "openid-connect",
                "protocol_mapper": "oidc-usermodel-attribute-mapper",
                # A forged ``id`` form field MUST be ignored -- the route path
                # is the authority for which client is targeted.
                "id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            },
            headers=headers,
        )
    assert response.status_code == 200, response.text
    assert len(received) == 1
    args = received[0]
    assert args["op_id"] == "keycloak.protocol_mapper.create"
    assert args["params"]["id"] == _CLIENT_UUID
    assert args["params"]["representation"]["name"] == "tenant-id-mapper"
    assert args["params"]["representation"]["protocolMapper"] == "oidc-usermodel-attribute-mapper"


# ---------------------------------------------------------------------------
# Confirm + CSRF: modal renders the confirm; POST without token -> 403
# ---------------------------------------------------------------------------


def test_keycloak_ui_scope_create_modal_renders_unmissable_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The create-scope modal surfaces caution + requires-approval."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    _patch_write_call_operation(monkeypatch, {})
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        response = client.get(f"/ui/keycloak/client-scopes/create?target={_TARGET_NAME}")
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-write-gate="confirm"' in body
    assert "data-write-requires-approval" in body
    assert "requires approval" in body


def test_keycloak_ui_mapper_create_modal_renders_unmissable_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The create-mapper modal surfaces caution + requires-approval."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    _patch_write_call_operation(monkeypatch, {})
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        response = client.get(
            f"/ui/keycloak/clients/{_CLIENT_UUID}/protocol-mappers/create?target={_TARGET_NAME}"
        )
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-write-gate="confirm"' in body
    assert "requires approval" in body


def test_keycloak_ui_scope_create_without_csrf_is_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scope-create POST with no CSRF token is rejected 403 (no dispatch)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    received = _patch_write_call_operation(monkeypatch, {})
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        response = client.post(
            "/ui/keycloak/client-scopes/create",
            data={"target": _TARGET_NAME, "name": "meho-tenant"},
        )
    assert response.status_code == 403, response.text
    assert received == []


def test_keycloak_ui_mapper_create_without_csrf_is_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mapper-create POST with no CSRF token is rejected 403 (no dispatch)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    received = _patch_write_call_operation(monkeypatch, {})
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        response = client.post(
            f"/ui/keycloak/clients/{_CLIENT_UUID}/protocol-mappers/create",
            data={
                "target": _TARGET_NAME,
                "name": "m",
                "protocol_mapper": "oidc-usermodel-attribute-mapper",
            },
        )
    assert response.status_code == 403, response.text
    assert received == []


# ---------------------------------------------------------------------------
# RBAC: soft-hide affordances from operator; hard-403 the POST for non-admin
# ---------------------------------------------------------------------------


def test_keycloak_ui_operator_does_not_see_create_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain operator renders the scope list but NOT the create button.

    The relation/read surface stays operator-tier (the list renders); the
    create affordance is soft-hidden via ``resolve_role_probe``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    # The read index dispatches through the READ module's call_operation; patch
    # both so neither hits a live connector.
    monkeypatch.setattr(
        "meho_backplane.ui.routes.keycloak.routes.call_operation",
        _make_ok_double(),
    )
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get("/ui/keycloak")
    assert response.status_code == 200, response.text
    body = response.text
    # The read surface renders for an operator.
    assert "data-keycloak-scopes" in body
    # The create affordance is hidden.
    assert 'data-action="open-scope-create"' not in body


def test_keycloak_ui_tenant_admin_sees_create_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant_admin renders the scope list WITH the create button."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    monkeypatch.setattr(
        "meho_backplane.ui.routes.keycloak.routes.call_operation",
        _make_ok_double(),
    )
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        response = client.get("/ui/keycloak")
    assert response.status_code == 200, response.text
    assert 'data-action="open-scope-create"' in response.text


def test_keycloak_ui_non_admin_scope_create_post_is_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged scope-create POST from a plain operator returns 403.

    The CSRF token is present + valid (a real operator session can mint one);
    the 403 comes from the server-side ``tenant_admin`` gate, not CSRF.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    received = _patch_write_call_operation(monkeypatch, {})
    client, mock, session_id = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    _, headers = _csrf_cookie_and_header(client, session_id)
    with mock:
        response = client.post(
            "/ui/keycloak/client-scopes/create",
            data={"target": _TARGET_NAME, "name": "meho-tenant"},
            headers=headers,
        )
    assert response.status_code == 403, response.text
    assert received == []


def test_keycloak_ui_non_admin_mapper_create_post_is_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged mapper-create POST from a plain operator returns 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    received = _patch_write_call_operation(monkeypatch, {})
    client, mock, session_id = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    _, headers = _csrf_cookie_and_header(client, session_id)
    with mock:
        response = client.post(
            f"/ui/keycloak/clients/{_CLIENT_UUID}/protocol-mappers/create",
            data={
                "target": _TARGET_NAME,
                "name": "m",
                "protocol_mapper": "oidc-usermodel-attribute-mapper",
            },
            headers=headers,
        )
    assert response.status_code == 403, response.text
    assert received == []


def test_keycloak_ui_non_admin_scope_create_modal_get_is_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The modal-render GET is tenant_admin-gated too (URL not reachable)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    _patch_write_call_operation(monkeypatch, {})
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(f"/ui/keycloak/client-scopes/create?target={_TARGET_NAME}")
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# Scope <-> client relation view: operator-tier, deep-links to the scope list
# ---------------------------------------------------------------------------


def test_keycloak_ui_client_detail_shows_scope_relation_for_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain operator sees the client's referenced scopes + the deep-link.

    The scope <-> client relation view is read-only assembly over T1's
    ``keycloak.client.get`` (it reads ``defaultClientScopes`` /
    ``optionalClientScopes``); it stays operator-tier and deep-links back to
    the realm browser's client-scope list. The mapper-create affordance,
    however, is hidden from the operator.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    detail_env = {
        "status": "ok",
        "op_id": "keycloak.client.get",
        "result": {
            "client": {
                "id": _CLIENT_UUID,
                "clientId": "meho-web",
                "enabled": True,
                "defaultClientScopes": ["profile", "meho-tenant"],
                "optionalClientScopes": ["address"],
            }
        },
        "error": None,
        "duration_ms": 1.0,
        "handle": None,
        "extras": {},
    }

    async def _fake(operator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        return detail_env

    monkeypatch.setattr("meho_backplane.ui.routes.keycloak.routes.call_operation", _fake)
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(f"/ui/keycloak/clients/{_CLIENT_UUID}?target={_TARGET_NAME}")
    assert response.status_code == 200, response.text
    body = response.text
    # The relation view renders the referenced scopes + the deep-link.
    assert "data-client-scopes" in body
    assert "meho-tenant" in body
    assert "address" in body
    assert "data-scope-list-link" in body
    # The mapper-create affordance is hidden from a plain operator.
    assert 'data-action="open-mapper-create"' not in body


def test_keycloak_ui_client_detail_shows_mapper_create_for_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant_admin sees the Add-protocol-mapper affordance on the detail."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_keycloak_target(tenant_id=_TENANT_A)
    detail_env = {
        "status": "ok",
        "op_id": "keycloak.client.get",
        "result": {"client": {"id": _CLIENT_UUID, "clientId": "meho-web", "enabled": True}},
        "error": None,
        "duration_ms": 1.0,
        "handle": None,
        "extras": {},
    }

    async def _fake(operator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        return detail_env

    monkeypatch.setattr("meho_backplane.ui.routes.keycloak.routes.call_operation", _fake)
    client, mock, _ = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        response = client.get(f"/ui/keycloak/clients/{_CLIENT_UUID}?target={_TARGET_NAME}")
    assert response.status_code == 200, response.text
    assert 'data-action="open-mapper-create"' in response.text


def _make_ok_double() -> Any:
    """Return an ``ok``-everything ``call_operation`` double for read paths."""

    async def _fake(operator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        op_id = arguments.get("op_id", "")
        # Shape the realm/list reads so the index renders without error.
        result: dict[str, Any] = {}
        if op_id in ("keycloak.client.list", "keycloak.client_scope.list"):
            result = {"rows": [], "total": 0}
        elif op_id == "keycloak.realm.get":
            result = {"realm": {"realm": "evba", "enabled": True}}
        return {
            "status": "ok",
            "op_id": op_id,
            "result": result,
            "error": None,
            "duration_ms": 1.0,
            "handle": None,
            "extras": {},
        }

    return _fake
