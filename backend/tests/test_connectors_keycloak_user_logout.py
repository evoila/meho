# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed Keycloak user logout: ``keycloak.user.logout`` (#3851).

Drives the op through the real dispatch stack against a respx-mocked
Keycloak Admin REST API. No running Keycloak and no Vault: the
admin-credential loader is a stub and the connector instance is preseeded
into the dispatcher's cache.

What is proven:

* the op registers on the ``caution`` risk level, needs approval, sits in the
  ``user_write`` group, and shows as a plain ``write`` on the broadcast feed;
* ``username`` is looked up to the user's UUID; ``id`` is used as is;
* a second run is a success too (Keycloak answers 204 either way);
* a user that does not exist is an error and nothing is posted;
* the result is ``{id, username, logged_out: true}`` and carries no
  credential;
* the approval gate: the call waits, a second person approves, and only then
  the logout POST runs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx
from sqlalchemy import select

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write import WRITE_OPS
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_OP_ID = "keycloak.user.logout"
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-logout-e2e"
_KC_HOST = "keycloak-logout-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "meho"
_ADMIN_REALM = "master"
_USER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_LOGOUT_PATH = f"/admin/realms/{_REALM}/users/{_USER_UUID}/logout"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000003851")


def _operator(sub: str = "kc-logout-requester") -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Logout Test Operator",
        email=None,
        raw_jwt="<kc-logout-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _creds_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
async def kc() -> AsyncIterator[KeycloakConnector]:
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="keycloak",
                host=_KC_HOST,
                port=443,
                fqdn=None,
                secret_ref="keycloak/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _REALM, "admin_realm": _ADMIN_REALM},
                fingerprint={"version": "26.3.3"},
                notes="seeded by test_connectors_keycloak_user_logout",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


async def _run(params: dict[str, Any]) -> dict[str, Any]:
    """Run the op as an already-approved call (the handler path)."""
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=target,
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def _mount_token(mock: respx.MockRouter) -> None:
    mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token", "expires_in": 300}
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_logout_registers_caution_and_approval_gated(kc: KeycloakConnector) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP_ID)
            )
        ).scalar_one()
    assert row.safety_level == "caution"
    assert row.requires_approval is True
    (op,) = [op for op in WRITE_OPS if op.op_id == _OP_ID]
    assert op.group_key == "user_write"
    when_to_use = op.llm_instructions["when_to_use"]
    assert "keycloak.user.reset_password" in when_to_use
    assert "never the whole realm" in when_to_use


def test_logout_classifies_as_plain_write() -> None:
    """No secret rides in the params, so the feed shows it as a normal write."""
    assert classify_op(_OP_ID) == "write"


async def test_logout_without_id_or_username_is_invalid_params(kc: KeycloakConnector) -> None:
    result = await _run({})
    assert result["status"] != "ok"
    assert result["extras"]["error_code"] == "invalid_params"


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


async def test_logout_by_username_resolves_uuid(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        lookup = mock.get(f"/admin/realms/{_REALM}/users").respond(
            200, json=[{"id": _USER_UUID, "username": "event-guest", "enabled": True}]
        )
        logout = mock.post(_LOGOUT_PATH).respond(204)
        result = await _run({"username": "event-guest"})
    assert result["status"] == "ok", result
    assert result["result"] == {"id": _USER_UUID, "username": "event-guest", "logged_out": True}
    assert lookup.calls.last.request.url.params["username"] == "event-guest"
    assert lookup.calls.last.request.url.params["exact"] == "true"
    assert logout.call_count == 1
    assert logout.calls.last.request.content == b""


async def test_logout_by_id_skips_lookup_and_is_safe_to_run_twice(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        lookup = mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[])
        logout = mock.post(_LOGOUT_PATH).respond(204)
        first = await _run({"id": _USER_UUID})
        second = await _run({"id": _USER_UUID})
    for result in (first, second):
        assert result["status"] == "ok", result
        assert result["result"] == {"id": _USER_UUID, "username": None, "logged_out": True}
    assert not lookup.called
    assert logout.call_count == 2


async def test_logout_unknown_username_is_not_found(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[])
        logout = mock.post(url__regex=r".*/logout$").respond(204)
        result = await _run({"username": "nobody"})
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" in str(result)
    assert not logout.called


async def test_logout_unknown_id_is_not_found(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.post(_LOGOUT_PATH).respond(404, json={"error": "User not found"})
        result = await _run({"id": _USER_UUID})
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" in str(result)


# ---------------------------------------------------------------------------
# The approval gate
# ---------------------------------------------------------------------------


async def test_logout_waits_for_approval_then_runs(kc: KeycloakConnector) -> None:
    """The call waits; a second person approves; only then the POST runs once."""
    requester = _operator("kc-logout-requester")
    approver = _operator("kc-logout-approver")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": _TARGET_NAME,
        "params": {"username": "event-guest"},
    }
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(
            200, json=[{"id": _USER_UUID, "username": "event-guest", "enabled": True}]
        )
        logout = mock.post(_LOGOUT_PATH).respond(204)

        call = await call_operation(requester, args)
        assert call["status"] == "awaiting_approval", call
        assert not logout.called  # nothing runs before approval
        request_id = uuid.UUID(call["extras"]["approval_request_id"])

        async with get_sessionmaker()() as session:
            approved = await approve_request(session, request_id, operator=approver, params=None)
            await session.commit()
        assert approved.status == "approved"

        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            resumed = await resume_dispatch_after_approval(
                operator=approver, request=row, params=None
            )
    assert resumed.status == "ok", resumed
    assert resumed.result == {"id": _USER_UUID, "username": "event-guest", "logged_out": True}
    assert logout.call_count == 1
