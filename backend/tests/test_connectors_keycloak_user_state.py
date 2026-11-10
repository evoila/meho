# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak user reads: ``keycloak.user.get`` and ``keycloak.user.sessions``.

Drives both ops through the real dispatch stack against a respx-mocked
Keycloak Admin REST API (recorded Keycloak 26 answers, made-up names). No
running Keycloak and no Vault.
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
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_GET_OP = "keycloak.user.get"
_SESSIONS_OP = "keycloak.user.sessions"
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-state-e2e"
_KC_HOST = "keycloak-state-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "meho"
_ADMIN_REALM = "master"
_USER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_USER_PATH = f"/admin/realms/{_REALM}/users/{_USER_UUID}"
_CREDENTIALS_PATH = f"{_USER_PATH}/credentials"
_SESSIONS_PATH = f"{_USER_PATH}/sessions"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000005076")


def _operator(sub: str = "kc-state-requester") -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak State Test Operator",
        email=None,
        raw_jwt="<kc-state-raw-jwt>",
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
                notes="seeded by test_connectors_keycloak_user_state",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


async def _run(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run one read op through the dispatcher."""
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def _mount_token(mock: respx.MockRouter) -> None:
    mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token", "expires_in": 300}
    )


_ZERO_UUID = "00000000-0000-0000-0000-000000000000"
_MS = 1_760_000_000_000  # 2025-10-09T08:53:20Z
_ISO_MS = "2025-10-09T08:53:20Z"

# Recorded shape of a Keycloak 26 user. The made-up credential values must never show.
_USER = {
    "id": _USER_UUID,
    "username": "event-guest",
    "enabled": True,
    "emailVerified": False,
    "createdTimestamp": _MS,
    "notBefore": 1_760_000_100,
    "requiredActions": ["UPDATE_PASSWORD", "CONFIGURE_TOTP"],
    "totp": False,
    "disableableCredentialTypes": [],
    "access": {"manage": True},
}
_CREDENTIALS = [
    {
        "id": "cred-id-1",
        "type": "password",
        "userLabel": "My password",
        "createdDate": _MS,
        "secretData": '{"value":"MADE-UP-HASH","salt":"MADE-UP-SALT"}',
        "credentialData": '{"hashIterations":5,"algorithm":"argon2"}',
        "value": "made-up-plain-value",
        "priority": 10,
    },
    {"id": "cred-id-2", "type": "otp", "createdDate": _MS + 1000, "secretData": "{}"},
]
_SESSIONS = [
    {
        "id": "made-up-session-id-1",
        "username": "event-guest",
        "userId": _USER_UUID,
        "ipAddress": "192.0.2.10",
        "start": _MS,
        "lastAccess": _MS + 60_000,
        "rememberMe": False,
        "clients": {"11111111-1111-1111-1111-111111111111": "meho-console", "x": "account"},
        "refresh_token": "made-up-token",
    }
]
_LEAK_WORDS = (
    "MADE-UP-HASH",
    "MADE-UP-SALT",
    "made-up-plain-value",
    "secretData",
    "credentialData",
    "made-up-session-id-1",
    "made-up-token",
    "cred-id-1",
)


def _assert_no_leak(result: dict[str, Any]) -> None:
    text = str(result)
    for word in _LEAK_WORDS:
        assert word not in text, word


@pytest.mark.parametrize("op_id", [_GET_OP, _SESSIONS_OP])
async def test_both_ops_are_safe_reads(kc: KeycloakConnector, op_id: str) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
    assert row.safety_level == "safe"
    assert row.requires_approval is False


async def test_get_by_id_returns_state_and_credential_type_and_date(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).respond(200, json=_USER)
        mock.get(_CREDENTIALS_PATH).respond(200, json=_CREDENTIALS)
        result = await _run(_GET_OP, {"id": _USER_UUID})
    assert result["status"] == "ok", result
    assert result["result"] == {
        "id": _USER_UUID,
        "username": "event-guest",
        "enabled": True,
        "emailVerified": False,
        "requiredActions": ["UPDATE_PASSWORD", "CONFIGURE_TOTP"],
        "notBefore": "2025-10-09T08:55:00Z",
        "createdTimestamp": _ISO_MS,
        "credentialDates": [
            {"type": "password", "createdDate": _ISO_MS},
            {"type": "otp", "createdDate": "2025-10-09T08:53:21Z"},
        ],
    }
    _assert_no_leak(result)


async def test_get_by_exact_username_ignores_a_longer_name(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        lookup = mock.get(f"/admin/realms/{_REALM}/users").respond(
            200,
            json=[
                {"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "username": "event-guest-two"},
                {"id": _USER_UUID, "username": "Event-Guest"},
            ],
        )
        mock.get(_USER_PATH).respond(200, json=_USER)
        mock.get(_CREDENTIALS_PATH).respond(200, json=[])
        result = await _run(_GET_OP, {"username": "event-guest"})
    assert result["status"] == "ok", result
    assert result["result"]["id"] == _USER_UUID
    assert result["result"]["credentialDates"] == []
    assert lookup.calls.last.request.url.params["exact"] == "true"


async def test_get_edge_inputs_no_times_and_no_actions(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).respond(200, json={"id": _USER_UUID, "notBefore": 0})
        mock.get(_CREDENTIALS_PATH).respond(200, json=[{"type": "password"}])
        result = await _run(_GET_OP, {"id": _USER_UUID})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["notBefore"] is None
    assert out["createdTimestamp"] is None
    assert out["requiredActions"] == []
    assert out["credentialDates"] == [{"type": "password", "createdDate": None}]


async def test_get_unknown_user_is_not_found(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[])
        by_name = await _run(_GET_OP, {"username": "nobody"})
        mock.get(_USER_PATH).respond(404, json={"error": "User not found"})
        by_id = await _run(_GET_OP, {"id": _USER_UUID})
    for result in (by_name, by_id):
        assert result["status"] != "ok"
        assert "KeycloakUserNotFoundError" in str(result)
        assert "user not found" in str(result)


async def test_get_without_id_or_username_is_invalid_params(kc: KeycloakConnector) -> None:
    result = await _run(_GET_OP, {})
    assert result["status"] != "ok"


async def test_get_vendor_failures_are_errors_not_empty_answers(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).respond(200, json=_USER)
        creds = mock.get(_CREDENTIALS_PATH).respond(500, json={"error": "boom"})
        server_error = await _run(_GET_OP, {"id": _USER_UUID})
        creds.respond(200, json={"not": "a list"})
        wrong_shape = await _run(_GET_OP, {"id": _USER_UUID})
        mock.get(_USER_PATH).respond(200, json=[])
        wrong_user_shape = await _run(_GET_OP, {"id": _USER_UUID})
    for result in (server_error, wrong_shape, wrong_user_shape):
        assert result["status"] != "ok", result


async def test_sessions_return_times_ip_and_client_names_only(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_SESSIONS_PATH).respond(200, json=_SESSIONS)
        result = await _run(_SESSIONS_OP, {"id": _USER_UUID})
    assert result["status"] == "ok", result
    assert result["result"] == {
        "userId": _USER_UUID,
        "count": 1,
        "sessions": [
            {
                "started": _ISO_MS,
                "lastAccess": "2025-10-09T08:54:20Z",
                "ipAddress": "192.0.2.10",
                "clients": ["account", "meho-console"],
            }
        ],
    }
    _assert_no_leak(result)


async def test_sessions_by_username_and_none_is_count_zero(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(
            200, json=[{"id": _USER_UUID, "username": "event-guest"}]
        )
        mock.get(_SESSIONS_PATH).respond(200, json=[])
        result = await _run(_SESSIONS_OP, {"username": "event-guest"})
    assert result["status"] == "ok", result
    assert result["result"] == {"userId": _USER_UUID, "count": 0, "sessions": []}


async def test_sessions_unknown_user_is_not_found(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[])
        by_name = await _run(_SESSIONS_OP, {"username": "nobody"})
        mock.get(_SESSIONS_PATH).respond(404, json={"error": "User not found"})
        by_id = await _run(_SESSIONS_OP, {"id": _USER_UUID})
    for result in (by_name, by_id):
        assert result["status"] != "ok"
        assert "user not found" in str(result)


async def test_sessions_vendor_failures_are_errors(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        route = mock.get(_SESSIONS_PATH).respond(503, json={"error": "down"})
        server_error = await _run(_SESSIONS_OP, {"id": _USER_UUID})
        route.respond(200, json={"sessions": []})
        wrong_shape = await _run(_SESSIONS_OP, {"id": _USER_UUID})
        route.respond(200, json=["not-an-object"])
        wrong_row = await _run(_SESSIONS_OP, {"id": _USER_UUID})
    for result in (server_error, wrong_shape, wrong_row):
        assert result["status"] != "ok", result


async def test_traversal_in_id_is_refused(kc: KeycloakConnector) -> None:
    result = await _run(_GET_OP, {"id": "../../users"})
    assert result["status"] != "ok"
