# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``keycloak.auth_flow.step.list``, ``.required_action.list`` and ``.events.config.get``.

Run through the real dispatch stack against a fake Keycloak (respx), with
recorded Keycloak 26 answer shapes and made-up names. The proof that matters:
a step setting, an action config value and a made-up secret never reach the
result.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
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

_EXEC_OP = "keycloak.auth_flow.step.list"
_ACTIONS_OP = "keycloak.required_action.list"
_EVENTS_OP = "keycloak.events.config.get"
_ALL_OPS = [_EXEC_OP, _ACTIONS_OP, _EVENTS_OP]
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-flow-reads-test"
_KC_HOST = "keycloak-flow-reads.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "demo"
_ADMIN = f"/admin/realms/{_REALM}"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000005109")
_MADE_UP_SECRET = "made-up-step-secret-5109"


def _operator() -> Operator:
    return Operator(
        sub="kc-flow-reads-requester",
        name="Keycloak Flow Reads Operator",
        email=None,
        raw_jwt="<kc-flow-reads-raw-jwt>",
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
                extras={"managed_realm": _REALM, "admin_realm": "master"},
                fingerprint={"version": "26.3.3"},
                notes="seeded by test_connectors_keycloak_flow_reads",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


async def _run(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
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


def _kc_mock() -> respx.MockRouter:
    """A fake Keycloak that also hands out the admin token."""
    router = respx.mock(base_url=_KC_BASE_URL, assert_all_called=False)
    router.post("/realms/master/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token", "expires_in": 300}
    )
    return router


def _flow_rows() -> list[dict[str, Any]]:
    """The browser flow of a Keycloak 26 realm, cut down, with step settings."""
    return [
        {
            "id": "exec-0",
            "requirement": "ALTERNATIVE",
            "displayName": "Cookie",
            "requirementChoices": ["ALTERNATIVE", "DISABLED"],
            "configurable": False,
            "providerId": "auth-cookie",
            "level": 0,
            "index": 0,
        },
        {
            "id": "exec-1",
            "requirement": "ALTERNATIVE",
            "displayName": "demo forms",
            "requirementChoices": ["REQUIRED", "ALTERNATIVE", "DISABLED", "CONDITIONAL"],
            "configurable": False,
            "authenticationFlow": True,
            "flowId": "flow-id-1",
            "level": 0,
            "index": 1,
        },
        {
            "id": "exec-2",
            "requirement": "CONDITIONAL",
            "displayName": "demo Browser - Conditional OTP",
            "authenticationFlow": True,
            "level": 1,
            "index": 0,
        },
        {
            "id": "exec-3",
            "requirement": "REQUIRED",
            "displayName": "OTP Form",
            "providerId": "auth-otp-form",
            "configurable": True,
            "authenticationConfig": "cfg-1",
            "authenticationConfigData": {"secretKey": _MADE_UP_SECRET, "algorithm": "HmacSHA1"},
            "config": {"password": _MADE_UP_SECRET},
            "level": 2,
            "index": 0,
        },
    ]


def _actions() -> list[dict[str, Any]]:
    return [
        {
            "alias": "UPDATE_PASSWORD",
            "name": "Update Password",
            "providerId": "UPDATE_PASSWORD",
            "enabled": True,
            "defaultAction": False,
            "priority": 30,
            "config": {"secretValue": _MADE_UP_SECRET},
        },
        {
            "alias": "CONFIGURE_TOTP",
            "name": "Configure OTP",
            "providerId": "CONFIGURE_TOTP",
            "enabled": True,
            "defaultAction": False,
            "priority": 10,
            "config": {"secretValue": _MADE_UP_SECRET},
        },
        {
            "alias": "no-priority",
            "name": "Odd one",
            "enabled": False,
            "defaultAction": False,
        },
    ]


def _mount_realm(mock: respx.MockRouter, browser_flow: str = "browser") -> None:
    mock.get(_ADMIN).respond(200, json={"realm": _REALM, "browserFlow": browser_flow})


async def test_ops_are_safe_reads(kc: KeycloakConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_ALL_OPS))
                )
            )
            .scalars()
            .all()
        )
    assert sorted(row.op_id for row in rows) == sorted(_ALL_OPS)
    for row in rows:
        assert row.safety_level == "safe"
        assert row.requires_approval is False
        assert classify_op(row.op_id) == "read"


async def test_executions_default_to_browser_flow_and_hide_step_settings(
    kc: KeycloakConnector,
) -> None:
    with _kc_mock() as mock:
        _mount_realm(mock, browser_flow="my browser")
        route = mock.get(f"{_ADMIN}/authentication/flows/my%20browser/executions").respond(
            200, json=_flow_rows()
        )
        result = await _run(_EXEC_OP, {})

    assert result["status"] == "ok", result
    assert route.call_count == 1
    out = result["result"]
    assert out["flow"] == "my browser"
    assert out["total"] == 4
    assert out["rows"][3] == {
        "level": 2,
        "index": 0,
        "displayName": "OTP Form",
        "requirement": "REQUIRED",
        "providerId": "auth-otp-form",
        "authenticationFlow": None,
    }
    assert out["rows"][2]["requirement"] == "CONDITIONAL"
    assert out["rows"][2]["authenticationFlow"] is True
    dumped = json.dumps(result)
    assert _MADE_UP_SECRET not in dumped
    for left_out in ("authenticationConfig", "config", "flowId", "secretKey"):
        assert left_out not in dumped


async def test_executions_of_a_named_flow_skip_the_realm_read(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        realm_route = mock.get(_ADMIN).respond(200, json={"browserFlow": "browser"})
        mock.get(f"{_ADMIN}/authentication/flows/direct%20grant/executions").respond(200, json=[])
        result = await _run(_EXEC_OP, {"flow": "direct grant"})
    assert result["status"] == "ok", result
    assert result["result"]["rows"] == []
    assert result["result"]["total"] == 0
    assert realm_route.call_count == 0


async def test_unknown_flow_is_a_plain_flow_not_found_error(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        mock.get(f"{_ADMIN}/authentication/flows/nope/executions").respond(
            404, json={"error": "Flow not found"}
        )
        result = await _run(_EXEC_OP, {"flow": "nope"})
    assert result["status"] != "ok"
    assert "flow not found" in json.dumps(result)


async def test_flow_alias_cannot_change_the_path(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        route = mock.get(f"{_ADMIN}/authentication/flows/..%2F..%2Fusers/executions").respond(
            200, json=[]
        )
        result = await _run(_EXEC_OP, {"flow": "../../users"})
    assert result["status"] == "ok", result
    assert route.call_count == 1


async def test_executions_server_error_is_an_error(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        mock.get(f"{_ADMIN}/authentication/flows/browser/executions").respond(500, json={})
        result = await _run(_EXEC_OP, {"flow": "browser"})
    assert result["status"] != "ok"
    assert result.get("result") in (None, {})


async def test_executions_that_are_not_a_list_or_have_odd_rows_are_errors(
    kc: KeycloakConnector,
) -> None:
    with _kc_mock() as mock:
        mock.get(f"{_ADMIN}/authentication/flows/browser/executions").respond(
            200, json={"rows": []}
        )
        not_list = await _run(_EXEC_OP, {"flow": "browser"})
    with _kc_mock() as mock:
        mock.get(f"{_ADMIN}/authentication/flows/browser/executions").respond(200, json=["x"])
        odd_row = await _run(_EXEC_OP, {"flow": "browser"})
    assert not_list["status"] != "ok"
    assert odd_row["status"] != "ok"


async def test_realm_without_browser_flow_is_an_error(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        mock.get(_ADMIN).respond(200, json={"realm": _REALM})
        result = await _run(_EXEC_OP, {})
    assert result["status"] != "ok"


async def test_unknown_param_is_invalid(kc: KeycloakConnector) -> None:
    result = await _run(_EXEC_OP, {"realm": "master"})
    assert result["extras"]["error_code"] == "invalid_params"


async def test_required_actions_sorted_by_priority_and_no_config(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        mock.get(f"{_ADMIN}/authentication/required-actions").respond(200, json=_actions())
        result = await _run(_ACTIONS_OP, {})
    assert result["status"] == "ok", result
    out = result["result"]
    assert [row["alias"] for row in out["rows"]] == [
        "CONFIGURE_TOTP",
        "UPDATE_PASSWORD",
        "no-priority",
    ]
    assert out["rows"][0] == {
        "alias": "CONFIGURE_TOTP",
        "name": "Configure OTP",
        "enabled": True,
        "defaultAction": False,
        "priority": 10,
    }
    assert out["rows"][2]["priority"] is None
    assert out["total"] == 3
    dumped = json.dumps(result)
    assert _MADE_UP_SECRET not in dumped
    assert "config" not in dumped


async def test_required_actions_empty_list_and_failures(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        mock.get(f"{_ADMIN}/authentication/required-actions").respond(200, json=[])
        empty = await _run(_ACTIONS_OP, {})
    with _kc_mock() as mock:
        mock.get(f"{_ADMIN}/authentication/required-actions").respond(403, json={})
        denied = await _run(_ACTIONS_OP, {})
    with _kc_mock() as mock:
        mock.get(f"{_ADMIN}/authentication/required-actions").respond(200, json={"a": 1})
        wrong = await _run(_ACTIONS_OP, {})
    assert empty["result"]["rows"] == [] and empty["result"]["total"] == 0
    assert denied["status"] != "ok"
    assert wrong["status"] != "ok"


_DEFAULT_TYPES = ["LOGIN", "LOGIN_ERROR", "LOGOUT", "UPDATE_PASSWORD", "UPDATE_TOTP"]


async def test_events_config_returns_the_six_fields_and_default_list(
    kc: KeycloakConnector,
) -> None:
    body = {
        "eventsEnabled": False,
        "eventsExpiration": 0,
        "eventsListeners": ["jboss-logging"],
        "enabledEventTypes": _DEFAULT_TYPES,
        "adminEventsEnabled": False,
        "adminEventsDetailsEnabled": False,
        "someOtherField": _MADE_UP_SECRET,
    }
    with _kc_mock() as mock:
        mock.get(f"{_ADMIN}/events/config").respond(200, json=body)
        result = await _run(_EVENTS_OP, {})
    assert result["status"] == "ok", result
    assert result["result"] == {
        "realm": _REALM,
        "eventsEnabled": False,
        "eventsExpiration": 0,
        "eventsListeners": ["jboss-logging"],
        "enabledEventTypes": _DEFAULT_TYPES,
        "adminEventsEnabled": False,
        "adminEventsDetailsEnabled": False,
    }


async def test_events_config_failures_are_errors(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        mock.get(f"{_ADMIN}/events/config").respond(
            200, json={"eventsEnabled": True, "enabledEventTypes": None}
        )
        no_list = await _run(_EVENTS_OP, {})
    with _kc_mock() as mock:
        mock.get(f"{_ADMIN}/events/config").respond(500, json={})
        server = await _run(_EVENTS_OP, {})
    assert no_list["status"] != "ok"
    assert server["status"] != "ok"
