# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``keycloak.events.list`` and ``keycloak.admin_events.list``.

Run through the real dispatch stack against a fake Keycloak (respx), with
recorded Keycloak 26 answer shapes and made-up names. The proof that matters:
the admin event ``representation`` never reaches the result or the stored
trace, and a realm that does not save events says so.
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
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

from ._result_store_helpers import MemoryResultStore

_EVENTS_OP = "keycloak.events.list"
_ADMIN_OP = "keycloak.admin_events.list"
_ALL_OPS = [_EVENTS_OP, _ADMIN_OP]
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-events-test"
_KC_HOST = "keycloak-events.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "demo"
_ADMIN = f"/admin/realms/{_REALM}"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000004990")
_MADE_UP_SECRET = "made-up-representation-secret-4990"


def _operator() -> Operator:
    return Operator(
        sub="kc-events-requester",
        name="Keycloak Events Operator",
        email=None,
        raw_jwt="<kc-events-raw-jwt>",
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
                notes="seeded by test_connectors_keycloak_events",
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


def _mount_realm(
    mock: respx.MockRouter, *, events: bool = True, admin_events: bool = True
) -> respx.Route:
    return mock.get(_ADMIN).respond(
        200,
        json={"realm": _REALM, "eventsEnabled": events, "adminEventsEnabled": admin_events},
    )


def _login_event(n: int = 0) -> dict[str, Any]:
    """One login event of a Keycloak 26 realm, with a details map and a session id."""
    return {
        "time": 1767225600000 + n,
        "type": "LOGIN_ERROR",
        "realmId": "realm-id-1",
        "clientId": "example-client",
        "userId": "00000000-0000-0000-0000-000000000001",
        "sessionId": "session-id-secret",
        "ipAddress": "192.0.2.10",
        "error": "invalid_user_credentials",
        "details": {"username": "example-user", "auth_method": "openid-connect"},
    }


def _admin_event(n: int = 0) -> dict[str, Any]:
    return {
        "time": 1767225600000 + n,
        "realmId": "realm-id-1",
        "authDetails": {
            "realmId": "master",
            "clientId": "admin-cli",
            "userId": "00000000-0000-0000-0000-000000000002",
            "ipAddress": "192.0.2.20",
        },
        "operationType": "UPDATE",
        "resourceType": "USER",
        "resourcePath": "users/00000000-0000-0000-0000-000000000003",
        "representation": json.dumps({"credentials": [{"value": _MADE_UP_SECRET}]}),
        "details": {"note": "example-note"},
    }


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


async def test_login_events_return_only_the_allowed_fields(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        _mount_realm(mock)
        route = mock.get(f"{_ADMIN}/events").respond(200, json=[_login_event()])
        result = await _run(_EVENTS_OP, {})
    assert result["status"] == "ok", result
    assert route.call_count == 1
    out = result["result"]
    assert out["enabled"] is True
    assert out["has_more"] is False
    assert out["total"] == 1
    assert out["rows"] == [
        {
            "time": "2026-01-01T00:00:00Z",
            "type": "LOGIN_ERROR",
            "clientId": "example-client",
            "userId": "00000000-0000-0000-0000-000000000001",
            "ipAddress": "192.0.2.10",
            "error": "invalid_user_credentials",
        }
    ]
    dumped = json.dumps(result)
    assert "session-id-secret" not in dumped
    assert "example-user" not in dumped


async def test_login_event_filters_and_paging_reach_keycloak(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        _mount_realm(mock)
        route = mock.get(f"{_ADMIN}/events").respond(200, json=[])
        result = await _run(
            _EVENTS_OP,
            {
                "type": ["LOGIN", "LOGIN_ERROR"],
                "client": "example-client",
                "user": "00000000-0000-0000-0000-000000000001",
                "ipAddress": "192.0.2.10",
                "dateFrom": "2026-01-01",
                "dateTo": "2026-01-31",
                "first": 20,
                "max": 10,
            },
        )
    assert result["status"] == "ok", result
    assert result["result"]["rows"] == []
    assert result["result"]["enabled"] is True
    query = route.calls.last.request.url.params
    assert query.get_list("type") == ["LOGIN", "LOGIN_ERROR"]
    assert query["client"] == "example-client"
    assert query["user"] == "00000000-0000-0000-0000-000000000001"
    assert query["ipAddress"] == "192.0.2.10"
    assert query["dateFrom"] == "2026-01-01"
    assert query["dateTo"] == "2026-01-31"
    assert query["first"] == "20"
    assert query["max"] == "11"


async def test_a_full_page_says_there_is_more(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        _mount_realm(mock)
        mock.get(f"{_ADMIN}/events").respond(200, json=[_login_event(n) for n in range(3)])
        result = await _run(_EVENTS_OP, {"max": 2})
    out = result["result"]
    assert out["total"] == 2
    assert len(out["rows"]) == 2
    assert out["has_more"] is True


async def test_a_page_that_ends_the_list_says_there_is_no_more(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        _mount_realm(mock)
        mock.get(f"{_ADMIN}/events").respond(200, json=[_login_event(n) for n in range(2)])
        result = await _run(_EVENTS_OP, {"max": 2})
    assert result["result"]["total"] == 2
    assert result["result"]["has_more"] is False


async def test_a_long_list_comes_back_behind_a_result_handle(kc: KeycloakConnector) -> None:
    """The ``rows`` list of a long answer is what the JSONFlux reducer turns into a handle."""
    with _kc_mock() as mock:
        _mount_realm(mock)
        mock.get(f"{_ADMIN}/events").respond(200, json=[_login_event(n) for n in range(120)])
        result = await _run(_EVENTS_OP, {"max": 200})
    assert result["status"] == "ok", result
    payload = result["result"]
    assert payload["total"] == 120
    _, handle = await JsonFluxReducer(store=MemoryResultStore()).reduce(
        payload,
        None,
        context={"tenant_id": str(_TENANT_ID), "operator_sub": "kc-events-requester"},
    )
    assert handle is not None


async def test_login_events_switched_off_say_so(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        _mount_realm(mock, events=False, admin_events=True)
        route = mock.get(f"{_ADMIN}/events").respond(200, json=[])
        result = await _run(_EVENTS_OP, {})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["enabled"] is False
    assert out["rows"] == []
    assert "does not save login events" in out["message"]
    assert "not an empty history" in out["message"]
    assert route.call_count == 0


async def test_admin_events_return_no_representation(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        _mount_realm(mock)
        mock.get(f"{_ADMIN}/admin-events").respond(200, json=[_admin_event()])
        result = await _run(_ADMIN_OP, {})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["enabled"] is True
    assert out["rows"] == [
        {
            "time": "2026-01-01T00:00:00Z",
            "operationType": "UPDATE",
            "resourceType": "USER",
            "resourcePath": "users/00000000-0000-0000-0000-000000000003",
            "authRealmId": "master",
            "authClientId": "admin-cli",
            "authUserId": "00000000-0000-0000-0000-000000000002",
            "authIpAddress": "192.0.2.20",
            "error": None,
        }
    ]
    dumped = json.dumps(result)
    assert _MADE_UP_SECRET not in dumped
    assert "representation" not in dumped


async def test_admin_event_filters_reach_keycloak(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        _mount_realm(mock)
        route = mock.get(f"{_ADMIN}/admin-events").respond(200, json=[])
        result = await _run(
            _ADMIN_OP,
            {
                "operationTypes": ["CREATE", "DELETE"],
                "resourceTypes": ["USER", "CLIENT"],
                "resourcePath": "users/*",
                "authUser": "00000000-0000-0000-0000-000000000002",
                "authIpAddress": "192.0.2.20",
                "dateFrom": "2026-01-01",
                "dateTo": "2026-01-31",
            },
        )
    assert result["status"] == "ok", result
    query = route.calls.last.request.url.params
    assert query.get_list("operationTypes") == ["CREATE", "DELETE"]
    assert query.get_list("resourceTypes") == ["USER", "CLIENT"]
    assert query["resourcePath"] == "users/*"
    assert query["authUser"] == "00000000-0000-0000-0000-000000000002"
    assert query["authIpAddress"] == "192.0.2.20"
    assert query["first"] == "0"
    assert query["max"] == "101"


async def test_admin_event_without_auth_details_has_empty_who(kc: KeycloakConnector) -> None:
    event = _admin_event()
    del event["authDetails"]
    with _kc_mock() as mock:
        _mount_realm(mock)
        mock.get(f"{_ADMIN}/admin-events").respond(200, json=[event])
        result = await _run(_ADMIN_OP, {})
    row = result["result"]["rows"][0]
    assert row["authUserId"] is None
    assert row["authIpAddress"] is None


async def test_admin_events_switched_off_say_so(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        _mount_realm(mock, events=True, admin_events=False)
        route = mock.get(f"{_ADMIN}/admin-events").respond(200, json=[])
        result = await _run(_ADMIN_OP, {})
    out = result["result"]
    assert result["status"] == "ok", result
    assert out["enabled"] is False
    assert out["rows"] == []
    assert "does not save admin events" in out["message"]
    assert route.call_count == 0


async def test_each_op_checks_its_own_flag(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        _mount_realm(mock, events=False, admin_events=True)
        mock.get(f"{_ADMIN}/admin-events").respond(200, json=[_admin_event()])
        admin = await _run(_ADMIN_OP, {})
    assert admin["result"]["enabled"] is True
    assert admin["result"]["total"] == 1


async def test_failures_are_errors_not_empty_lists(kc: KeycloakConnector) -> None:
    for op in (_EVENTS_OP, _ADMIN_OP):
        path = "events" if op == _EVENTS_OP else "admin-events"
        with _kc_mock() as mock:
            _mount_realm(mock)
            mock.get(f"{_ADMIN}/{path}").respond(500, json={})
            server_error = await _run(op, {})
        with _kc_mock() as mock:
            _mount_realm(mock)
            mock.get(f"{_ADMIN}/{path}").respond(403, json={})
            denied = await _run(op, {})
        with _kc_mock() as mock:
            _mount_realm(mock)
            mock.get(f"{_ADMIN}/{path}").respond(200, json={"rows": []})
            not_a_list = await _run(op, {})
        with _kc_mock() as mock:
            _mount_realm(mock)
            mock.get(f"{_ADMIN}/{path}").respond(200, json=["x"])
            odd_row = await _run(op, {})
        for result in (server_error, denied, not_a_list, odd_row):
            assert result["status"] != "ok", (op, result)
            assert result.get("result") in (None, {})


async def test_realm_read_failures_are_errors(kc: KeycloakConnector) -> None:
    with _kc_mock() as mock:
        mock.get(_ADMIN).respond(500, json={})
        failed = await _run(_EVENTS_OP, {})
    with _kc_mock() as mock:
        mock.get(_ADMIN).respond(200, json={"realm": _REALM})
        no_flag = await _run(_ADMIN_OP, {})
    assert failed["status"] != "ok"
    assert no_flag["status"] != "ok"


async def test_invalid_params_are_refused(kc: KeycloakConnector) -> None:
    for op, params in (
        (_EVENTS_OP, {"realm": "master"}),
        (_EVENTS_OP, {"dateFrom": "yesterday"}),
        (_EVENTS_OP, {"max": 0}),
        (_ADMIN_OP, {"operationTypes": "DELETE"}),
        (_ADMIN_OP, {"first": -1}),
    ):
        result = await _run(op, params)
        assert result["extras"]["error_code"] == "invalid_params", (op, params)


def test_the_flight_recorder_trace_masks_the_admin_event_representation() -> None:
    """The trace stores the vendor answer before the op filters it."""
    from meho_backplane.connectors.keycloak.redaction import REDACTED

    recorded = KeycloakConnector.trace_response_body(
        None,  # type: ignore[arg-type]  # the hook does not use self
        httpx.Response(200, json=[_admin_event()]),
    )
    assert recorded[0]["representation"] == REDACTED
    assert _MADE_UP_SECRET not in json.dumps(recorded)
