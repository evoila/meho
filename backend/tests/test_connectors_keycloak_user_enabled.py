# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Turn a Keycloak user off and on: ``keycloak.user.disable`` / ``keycloak.user.enable`` (#4940).

Drives the ops through the real dispatch stack against a respx-mocked
Keycloak Admin REST API. No running Keycloak and no Vault.

What is proven:

* both ops register on the ``dangerous`` risk level, need approval and sit in
  the ``user_write`` group;
* the user is read, ``enabled`` is changed, and every other field is written
  back as it was;
* ``username`` is looked up to the UUID; ``id`` is used as is;
* a second run is a success too;
* a user that does not exist is an error and nothing is written;
* the approval gate holds: nothing is written before a second person approves.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

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

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-enable-e2e"
_KC_HOST = "keycloak-enable-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "meho"
_ADMIN_REALM = "master"
_USER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_USER_PATH = f"/admin/realms/{_REALM}/users/{_USER_UUID}"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000004940")
_DISABLE = "keycloak.user.disable"
_ENABLE = "keycloak.user.enable"


def _user(enabled: bool) -> dict[str, Any]:
    """A made-up user as Keycloak returns it."""
    return {
        "id": _USER_UUID,
        "username": "event-guest",
        "email": "guest@example.invalid",
        "firstName": "Event",
        "enabled": enabled,
        "attributes": {"seat": ["12"]},
        "requiredActions": ["VERIFY_EMAIL"],
    }


def _operator(sub: str = "kc-enable-requester", role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Enable Test Operator",
        email=None,
        raw_jwt="<kc-enable-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=role,
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
                notes="seeded by test_connectors_keycloak_user_enabled",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


async def _run(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run the op as an already-approved call (the handler path)."""
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
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


@pytest.mark.parametrize("op_id", [_DISABLE, _ENABLE])
async def test_ops_register_dangerous_and_approval_gated(kc: KeycloakConnector, op_id: str) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
    assert row.safety_level == "dangerous"
    assert row.requires_approval is True
    (op,) = [op for op in WRITE_OPS if op.op_id == op_id]
    assert op.group_key == "user_write"


def test_disable_blurb_names_the_logout_order() -> None:
    (op,) = [op for op in WRITE_OPS if op.op_id == _DISABLE]
    when_to_use = op.llm_instructions["when_to_use"]
    assert "keycloak.user.logout" in when_to_use
    assert "does NOT end sessions" in when_to_use


@pytest.mark.parametrize("op_id", [_DISABLE, _ENABLE])
def test_ops_classify_as_plain_write(op_id: str) -> None:
    assert classify_op(op_id) == "write"


@pytest.mark.parametrize("op_id", [_DISABLE, _ENABLE])
async def test_without_id_or_username_is_invalid_params(kc: KeycloakConnector, op_id: str) -> None:
    result = await _run(op_id, {})
    assert result["status"] != "ok"
    assert result["extras"]["error_code"] == "invalid_params"


# ---------------------------------------------------------------------------
# The handlers
# ---------------------------------------------------------------------------


async def test_disable_by_username_keeps_every_other_field(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        lookup = mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[_user(True)])
        reads = mock.get(_USER_PATH).mock(
            side_effect=[
                httpx.Response(200, json=_user(True)),
                httpx.Response(200, json=_user(False)),
            ]
        )
        put = mock.put(_USER_PATH).respond(204)
        result = await _run(_DISABLE, {"username": "event-guest"})
    assert result["status"] == "ok", result
    assert result["result"] == {
        "id": _USER_UUID,
        "username": "event-guest",
        "enabled": False,
        "unrequested_changes": [],
        "read_back_checked": True,
    }
    assert lookup.calls.last.request.url.params["exact"] == "true"
    assert reads.call_count == 2
    assert put.call_count == 1
    assert json.loads(put.calls.last.request.content) == _user(False)


async def test_enable_by_id_skips_lookup_and_keeps_other_fields(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        lookup = mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[])
        mock.get(_USER_PATH).mock(
            side_effect=[
                httpx.Response(200, json=_user(False)),
                httpx.Response(200, json=_user(True)),
            ]
        )
        put = mock.put(_USER_PATH).respond(204)
        result = await _run(_ENABLE, {"id": _USER_UUID})
    assert result["status"] == "ok", result
    assert result["result"]["enabled"] is True
    assert result["result"]["username"] is None
    assert not lookup.called
    assert json.loads(put.calls.last.request.content) == _user(True)


async def test_disable_twice_is_a_success_both_times(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).respond(200, json=_user(False))
        put = mock.put(_USER_PATH).respond(204)
        first = await _run(_DISABLE, {"id": _USER_UUID})
        second = await _run(_DISABLE, {"id": _USER_UUID})
    for result in (first, second):
        assert result["status"] == "ok", result
        assert result["result"]["enabled"] is False
    assert put.call_count == 2


async def test_unrequested_change_is_named_not_valued(kc: KeycloakConnector) -> None:
    after = {**_user(False), "email": "other@example.invalid"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).mock(
            side_effect=[httpx.Response(200, json=_user(True)), httpx.Response(200, json=after)]
        )
        mock.put(_USER_PATH).respond(204)
        result = await _run(_DISABLE, {"id": _USER_UUID})
    assert result["result"]["unrequested_changes"] == ["email"]
    assert "other@example.invalid" not in str(result)


@pytest.mark.parametrize("op_id", [_DISABLE, _ENABLE])
async def test_unknown_username_is_not_found_and_nothing_is_written(
    kc: KeycloakConnector, op_id: str
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[])
        put = mock.put(url__regex=r".*/users/.*").respond(204)
        result = await _run(op_id, {"username": "nobody"})
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" in str(result)
    assert not put.called


@pytest.mark.parametrize("op_id", [_DISABLE, _ENABLE])
async def test_unknown_id_is_not_found_and_nothing_is_written(
    kc: KeycloakConnector, op_id: str
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).respond(404, json={"error": "User not found"})
        put = mock.put(_USER_PATH).respond(204)
        result = await _run(op_id, {"id": _USER_UUID})
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" in str(result)
    assert not put.called


async def test_a_vendor_server_error_is_an_error_not_success(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).respond(200, json=_user(True))
        mock.put(_USER_PATH).respond(500, json={"error": "boom"})
        result = await _run(_DISABLE, {"id": _USER_UUID})
    assert result["status"] != "ok"


# ---------------------------------------------------------------------------
# The approval gate
# ---------------------------------------------------------------------------


async def test_disable_waits_for_approval_then_runs(kc: KeycloakConnector) -> None:
    requester = _operator("kc-enable-requester")
    approver = _operator("kc-enable-approver", TenantRole.TENANT_ADMIN)
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _DISABLE,
        "target": _TARGET_NAME,
        "params": {"id": _USER_UUID},
    }
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).respond(200, json=_user(True))
        put = mock.put(_USER_PATH).respond(204)

        call = await call_operation(requester, args)
        assert call["status"] == "awaiting_approval", call
        assert not put.called  # nothing is written before approval
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
    assert put.call_count == 1
    assert json.loads(put.calls.last.request.content)["enabled"] is False
