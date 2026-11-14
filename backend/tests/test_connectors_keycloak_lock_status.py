# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak lock reads: ``keycloak.user.lock_status``, ``keycloak.realm.lock_settings``.

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

_STATUS_OP = "keycloak.user.lock_status"
_SETTINGS_OP = "keycloak.realm.lock_settings"
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-lock-e2e"
_KC_HOST = "keycloak-lock-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "meho"
_ADMIN_REALM = "master"
_USER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_LOCK_PATH = f"/admin/realms/{_REALM}/attack-detection/brute-force/users/{_USER_UUID}"
_USER_PATH = f"/admin/realms/{_REALM}/users/{_USER_UUID}"
_REALM_PATH = f"/admin/realms/{_REALM}"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000005371")


def _operator(sub: str = "kc-lock-requester") -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Lock Test Operator",
        email=None,
        raw_jwt="<kc-lock-raw-jwt>",
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
                notes="seeded by test_connectors_keycloak_lock_status",
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


_MS = 1_760_000_000_000  # 2025-10-09T08:53:20Z
_ISO_MS = "2025-10-09T08:53:20Z"

# Recorded shape of a Keycloak 26 answer. The IP address must never show.
_LOCKED = {
    "numFailures": 5,
    "numTemporaryLockouts": 2,
    "disabled": True,
    "lastIPFailure": "192.0.2.77",
    "lastFailure": _MS,
}
_CLEAN = {"numFailures": 0, "disabled": False, "lastIPFailure": "n/a", "lastFailure": 0}
# What Keycloak 26.3 answers for an id it does not know, and when the realm's lock is off.
_DEFAULTS = {
    "disabled": False,
    "numFailures": 0,
    "numTemporaryLockouts": 0,
    "lastFailure": 0,
    "lastIPFailure": "n/a",
    "failedLoginNotBefore": 0,
}
_USER_ROW = {"id": _USER_UUID, "username": "event-guest", "enabled": True}
_REALM_ROW = {
    "realm": _REALM,
    "bruteForceProtected": True,
    "permanentLockout": False,
    "failureFactor": 5,
    "waitIncrementSeconds": 60,
    "maxFailureWaitSeconds": 900,
    "maxDeltaTimeSeconds": 43200,
    "minimumQuickLoginWaitSeconds": 60,
    "quickLoginCheckMilliSeconds": 1000,
    "maxTemporaryLockouts": 0,
    "smtpServer": {"password": "made-up-smtp-secret"},
    "browserFlow": "browser",
}


@pytest.mark.parametrize("op_id", [_STATUS_OP, _SETTINGS_OP])
async def test_both_ops_are_safe_reads(kc: KeycloakConnector, op_id: str) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
    assert row.safety_level == "safe"
    assert row.requires_approval is False


async def test_locked_user_by_id_shows_counts_and_no_ip(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        user_read = mock.get(_USER_PATH).respond(200, json=_USER_ROW)
        mock.get(_LOCK_PATH).respond(200, json=_LOCKED)
        result = await _run(_STATUS_OP, {"id": _USER_UUID})
    assert user_read.called
    assert result["status"] == "ok", result
    assert result["result"] == {
        "userId": _USER_UUID,
        "locked": True,
        "numFailures": 5,
        "numTemporaryLockouts": 2,
        "lastFailure": _ISO_MS,
    }
    assert "192.0.2.77" not in str(result)
    assert "lastIPFailure" not in str(result)


async def test_user_with_no_failures_is_not_locked(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        lookup = mock.get(f"/admin/realms/{_REALM}/users").respond(
            200,
            json=[
                {"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "username": "event-guest-two"},
                {"id": _USER_UUID, "username": "Event-Guest"},
            ],
        )
        user_read = mock.get(_USER_PATH).respond(200, json=_USER_ROW)
        mock.get(_LOCK_PATH).respond(200, json=_CLEAN)
        result = await _run(_STATUS_OP, {"username": "event-guest"})
    assert not user_read.called
    assert result["status"] == "ok", result
    assert result["result"] == {
        "userId": _USER_UUID,
        "locked": False,
        "numFailures": 0,
        "numTemporaryLockouts": None,
        "lastFailure": None,
    }
    assert lookup.calls.last.request.url.params["exact"] == "true"


async def test_unknown_user_is_not_found(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[])
        by_name = await _run(_STATUS_OP, {"username": "nobody"})
        mock.get(_USER_PATH).respond(404, json={"error": "User not found"})
        lock_read = mock.get(_LOCK_PATH).respond(200, json=_DEFAULTS)
        by_id = await _run(_STATUS_OP, {"id": _USER_UUID})
    for result in (by_name, by_id):
        assert result["status"] != "ok"
        assert "user not found" in str(result)
    assert not lock_read.called


async def test_unknown_id_with_default_lock_answer_is_not_found(kc: KeycloakConnector) -> None:
    """Keycloak answers 200 with "not locked" for an unknown id; the user read decides."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).respond(404, json={"error": "User not found"})
        mock.get(_LOCK_PATH).respond(200, json=_DEFAULTS)
        result = await _run(_STATUS_OP, {"id": _USER_UUID})
    assert result["status"] != "ok", result
    assert "user not found" in str(result)
    assert "locked" not in str(result.get("result"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lastFailure", "yesterday"),
        ("lastFailure", True),
        ("numFailures", "5"),
        ("numFailures", True),
        ("numTemporaryLockouts", 1.5),
    ],
)
async def test_status_field_of_wrong_type_is_an_error(
    kc: KeycloakConnector, field: str, value: Any
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).respond(200, json=_USER_ROW)
        mock.get(_LOCK_PATH).respond(200, json={**_LOCKED, field: value})
        result = await _run(_STATUS_OP, {"id": _USER_UUID})
    assert result["status"] != "ok", result
    assert field in str(result)


async def test_absent_or_null_last_failure_is_null(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).respond(200, json=_USER_ROW)
        route = mock.get(_LOCK_PATH).respond(200, json={"disabled": False})
        absent = await _run(_STATUS_OP, {"id": _USER_UUID})
        route.respond(200, json={"disabled": False, "lastFailure": None})
        null = await _run(_STATUS_OP, {"id": _USER_UUID})
    for result in (absent, null):
        assert result["status"] == "ok", result
        assert result["result"]["lastFailure"] is None


async def test_status_output_shape_says_a_switched_off_lock_also_reads_not_locked(
    kc: KeycloakConnector,
) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == _STATUS_OP)
            )
        ).scalar_one()
    assert "lock is off" in row.llm_instructions["output_shape"]
    assert "lock is off" in row.response_schema["properties"]["locked"]["description"]


async def test_without_id_or_username_and_traversal_are_refused(kc: KeycloakConnector) -> None:
    assert (await _run(_STATUS_OP, {}))["status"] != "ok"
    assert (await _run(_STATUS_OP, {"id": "../../users"}))["status"] != "ok"


async def test_status_vendor_failures_are_errors_not_unlocked(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).respond(200, json=_USER_ROW)
        route = mock.get(_LOCK_PATH).respond(500, json={"error": "boom"})
        server_error = await _run(_STATUS_OP, {"id": _USER_UUID})
        route.respond(200, json=[])
        wrong_shape = await _run(_STATUS_OP, {"id": _USER_UUID})
        route.respond(200, json={"numFailures": 3})
        no_flag = await _run(_STATUS_OP, {"id": _USER_UUID})
    for result in (server_error, wrong_shape, no_flag):
        assert result["status"] != "ok", result


async def test_realm_lock_settings_copy_only_the_lock_fields(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=_REALM_ROW)
        result = await _run(_SETTINGS_OP, {})
    assert result["status"] == "ok", result
    assert result["result"] == {
        "realm": _REALM,
        "bruteForceProtected": True,
        "permanentLockout": False,
        "failureFactor": 5,
        "waitIncrementSeconds": 60,
        "maxFailureWaitSeconds": 900,
        "maxDeltaTimeSeconds": 43200,
        "minimumQuickLoginWaitSeconds": 60,
        "quickLoginCheckMilliSeconds": 1000,
        "maxTemporaryLockouts": 0,
    }
    assert "made-up-smtp-secret" not in str(result)


async def test_realm_lock_settings_failures_are_errors(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        route = mock.get(_REALM_PATH).respond(503, json={"error": "down"})
        server_error = await _run(_SETTINGS_OP, {})
        route.respond(200, json={"realm": _REALM})
        no_field = await _run(_SETTINGS_OP, {})
        route.respond(200, json=[])
        wrong_shape = await _run(_SETTINGS_OP, {})
    for result in (server_error, no_field, wrong_shape):
        assert result["status"] != "ok", result


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bruteForceProtected", "true"),
        ("permanentLockout", 1),
        ("failureFactor", "5"),
        ("failureFactor", True),
        ("maxFailureWaitSeconds", 900.5),
        ("maxTemporaryLockouts", [0]),
    ],
)
async def test_realm_lock_setting_of_wrong_type_is_an_error(
    kc: KeycloakConnector, field: str, value: Any
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json={**_REALM_ROW, field: value})
        result = await _run(_SETTINGS_OP, {})
    assert result["status"] != "ok", result
    assert field in str(result)


async def test_realm_lock_setting_absent_is_null(kc: KeycloakConnector) -> None:
    row = {k: v for k, v in _REALM_ROW.items() if k != "maxTemporaryLockouts"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=row)
        result = await _run(_SETTINGS_OP, {})
    assert result["status"] == "ok", result
    assert result["result"]["maxTemporaryLockouts"] is None
