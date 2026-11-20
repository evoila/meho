# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak client and realm writes keep MEHO's own login working.

Drives ``keycloak.client.update``, ``keycloak.client.delete`` and
``keycloak.realm.update`` through the real dispatch stack against a
respx-mocked Keycloak Admin REST API (made-up names and ids). The target
manages MEHO's own login realm. What is proven:

* a write that changes a client MEHO itself uses, or switches the login
  realm off, is refused before approval and when the call runs, for a
  person, an agent login and a service login, with and without a standing
  grant;
* MEHO finds its own clients in its settings: a client named by its
  internal id, or stored with another spelling of the id, is the same
  client;
* each allowed change waits for approval and then runs;
* the same writes in another realm are not refused;
* the refusal text names no client the caller did not name and gives no
  advice to change rights.

Nothing is written to Keycloak when MEHO refuses.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_client_realm_meho_access_guard import (
    CLIENT_ALLOWED,
    REALM_ALLOWED,
    meho_own_client_ids,
    refuse_own_client_delete,
)
from meho_backplane.connectors.keycloak.ops_user_meho_access_guard import MehoAccessRefusedError
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, ApprovalRequest, ServicePrincipalGrant
from meho_backplane.operations import dispatch
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests.test_connectors_keycloak_user_login_guard import (  # noqa: F401  (fixtures)
    _ADMIN_REALM,
    _CONNECTOR_ID,
    _KC_BASE_URL,
    _LOGIN_REALM,
    _LOGIN_REALM_TARGET,
    _OTHER_REALM,
    _OTHER_REALM_TARGET,
    _TENANT_ID,
    _required_settings_env,
    _reset_module_state,
    _stored_requests,
    _target,
    kc,
)

_WEB_CLIENT = "meho-web"
_WEB_UUID = "c1c1c1c1-0000-0000-0000-000000000001"
_ADMIN_CLIENT = "meho-backplane-admin"
_ADMIN_CLIENT_UUID = "c1c1c1c1-0000-0000-0000-000000000002"
_OTHER_CLIENT = "event-app"
_OTHER_UUID = "c1c1c1c1-0000-0000-0000-000000000003"
_CALLBACK = "https://console.example.test/ui/auth/callback"
_NEW_CALLBACK = "https://console-new.example.test/ui/auth/callback"

_UPDATE = "keycloak.client.update"
_DELETE = "keycloak.client.delete"
_REALM_UPDATE = "keycloak.realm.update"

#: Each refused write and its params.
_REFUSED: list[tuple[str, dict[str, Any]]] = [
    (_UPDATE, {"id": _WEB_UUID, "representation": {"publicClient": True}}),
    (_DELETE, {"id": _WEB_UUID}),
    (_REALM_UPDATE, {"representation": {"enabled": False}}),
]
_REFUSED_IDS = ["client-update", "client-delete", "realm-update"]

_WORDS = {
    _UPDATE: "this client is one that MEHO itself uses",
    _DELETE: "this client is one that MEHO itself uses",
    _REALM_UPDATE: "this realm is the one MEHO's own login uses",
}
_CODES = {_UPDATE: "meho_own_client", _DELETE: "meho_own_client", _REALM_UPDATE: "meho_login_realm"}

#: Words of advice to change rights or to go around the check; no refusal text has them.
_ADVICE = ("ask another", "admin console", "first give", "then send", "change the setting")


@pytest.fixture(autouse=True)
def _client_env(
    _required_settings_env: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", _ADMIN_CLIENT)
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", _WEB_CLIENT)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client(uuid_value: str, client_id: str) -> dict[str, Any]:
    return {
        "id": uuid_value,
        "clientId": client_id,
        "name": "Console",
        "enabled": True,
        "publicClient": False,
        "redirectUris": [_CALLBACK],
        "webOrigins": ["+"],
    }


class _Keycloak:
    """The mocked Admin API of one realm: its clients, the realm, and the writes."""

    def __init__(self, mock: respx.MockRouter, realm: str) -> None:
        self.realm = realm
        self.clients: dict[str, dict[str, Any]] = {}
        self.realm_rep: dict[str, Any] = {"id": realm, "realm": realm, "enabled": True}
        self.writes: list[str] = []
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        mock.get(f"/admin/realms/{realm}/clients").mock(side_effect=self._find)
        mock.route(url__regex=rf".*/admin/realms/{realm}/clients/[^/]+$").mock(
            side_effect=self._client_route
        )
        mock.route(url__regex=rf".*/admin/realms/{realm}$").mock(side_effect=self._realm_route)
        for path in ("users/count", "groups/count"):
            mock.get(f"/admin/realms/{realm}/{path}").respond(200, json=0)

    def add(self, client: dict[str, Any]) -> None:
        self.clients[client["id"]] = client

    def _find(self, request: httpx.Request) -> httpx.Response:
        wanted = request.url.params.get("clientId")
        rows = [c for c in self.clients.values() if wanted is None or c["clientId"] == wanted]
        return httpx.Response(200, json=rows)

    def _client_route(self, request: httpx.Request) -> httpx.Response:
        client_id = request.url.path.rsplit("/", 1)[1]
        if request.method != "GET":
            self.writes.append(f"{request.method} {request.url.path}")
        if request.method == "PUT":
            self.clients[client_id] = json.loads(request.content)
            return httpx.Response(204)
        if request.method == "DELETE":
            self.clients.pop(client_id, None)
            return httpx.Response(204)
        client = self.clients.get(client_id)
        return httpx.Response(200, json=client) if client else httpx.Response(404)

    def _realm_route(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            self.writes.append(f"PUT {request.url.path}")
            self.realm_rep = json.loads(request.content)
            return httpx.Response(204)
        return httpx.Response(200, json=self.realm_rep)


def _keycloak(mock: respx.MockRouter, realm: str = _LOGIN_REALM) -> _Keycloak:
    api = _Keycloak(mock, realm)
    api.add(_client(_WEB_UUID, _WEB_CLIENT))
    api.add(_client(_ADMIN_CLIENT_UUID, _ADMIN_CLIENT))
    api.add(_client(_OTHER_UUID, _OTHER_CLIENT))
    return api


def _person() -> Operator:
    return Operator(
        sub="kc-client-person",
        name="Client Guard Test Person",
        email=None,
        raw_jwt="<kc-client-person-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


def _machine(kind: PrincipalKind) -> Operator:
    name = f"client-guard-{kind.value}"
    return Operator(
        sub=f"svc:{name}",
        name=name,
        email=None,
        raw_jwt=f"<{name}-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
        client_id=name,
    )


async def _grant(operator: Operator, op_id: str) -> None:
    """A standing grant that would run *op_id* for *operator* without a wait."""
    async with get_sessionmaker()() as session:
        if operator.principal_kind == PrincipalKind.AGENT:
            session.add(
                AgentPermission(
                    tenant_id=_TENANT_ID,
                    principal_sub=operator.sub,
                    op_pattern="keycloak.*",
                    verdict="auto-execute",
                    created_by_sub="kc-client-admin",
                )
            )
        else:
            session.add(
                ServicePrincipalGrant(
                    id=uuid.uuid4(),
                    tenant_id=_TENANT_ID,
                    principal_sub=operator.sub,
                    op_id=op_id,
                    connector_id=_CONNECTOR_ID,
                    target_id=(await _target(_LOGIN_REALM_TARGET)).id,
                    reason="unattended clean-up",
                    created_by_sub="kc-client-admin",
                )
            )
        await session.commit()


#: The callers: a person, an agent login and a service login, with and without a grant.
_CALLERS = [
    ("person", PrincipalKind.USER, False),
    ("agent", PrincipalKind.AGENT, False),
    ("agent-with-grant", PrincipalKind.AGENT, True),
    ("service", PrincipalKind.SERVICE, False),
    ("service-with-grant", PrincipalKind.SERVICE, True),
]
_CALLER_IDS = [name for name, _, _ in _CALLERS]


async def _caller(kind: PrincipalKind, granted: bool, op_id: str) -> Operator:
    operator = _person() if kind == PrincipalKind.USER else _machine(kind)
    if granted:
        await _grant(operator, op_id)
    return operator


async def _call(
    op_id: str,
    params: dict[str, Any],
    operator: Operator | None = None,
    target: str = _LOGIN_REALM_TARGET,
) -> dict[str, Any]:
    """Call the op as an agent does; a delete first asks for its preview, as MEHO requires."""
    caller = operator or _person()
    args: dict[str, Any] = {
        "connector_id": _CONNECTOR_ID,
        "op_id": op_id,
        "target": target,
        "params": params,
    }
    if op_id == _DELETE:
        preview = await preview_operation(caller, args)
        if preview.get("preview_hash"):
            args["preview_hash"] = preview["preview_hash"]
    return await call_operation(caller, args)


async def _run(
    op_id: str,
    params: dict[str, Any],
    operator: Operator | None = None,
    target: str = _LOGIN_REALM_TARGET,
) -> dict[str, Any]:
    result = await dispatch(
        operator=operator or _person(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=await _target(target),
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


async def _approve_and_run(call: dict[str, Any]) -> Any:
    approver = Operator(
        sub="kc-client-approver",
        name="Client Guard Approver",
        email=None,
        raw_jwt="<kc-client-approver-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )
    request_id = uuid.UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=None)
        await session.commit()
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


def _refused_before_approval(op_id: str, call: dict[str, Any]) -> None:
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval", call
    assert _WORDS[op_id] in call["error"], call["error"]
    assert "No approval and no grant allows this write." in call["error"]
    assert not [w for w in _ADVICE if w in call["error"].casefold()], call["error"]


def _refused_on_run(op_id: str, result: dict[str, Any]) -> None:
    assert result["status"] != "ok", result
    assert result["extras"]["exception_class"] == "MehoAccessRefusedError", result
    message = result["extras"]["exception_message"]
    # realm.update can send a secret, so MEHO withholds its error text.
    if "withheld" not in message:
        assert _CODES[op_id] in message, message


def _refused_after_wait(op_id: str, result: dict[str, Any]) -> None:
    """The approved run is refused; a delete builds its preview again first, which refuses it."""
    if op_id == _DELETE:
        assert result["status"] == "denied", result
        assert result["extras"]["error_code"] == "preview_changed_since_approval", result
        return
    _refused_on_run(op_id, result)


def test_meho_finds_its_own_clients_in_its_settings() -> None:
    own = meho_own_client_ids()
    assert own is not None
    assert {_WEB_CLIENT, _ADMIN_CLIENT, "meho-backplane", "meho-cli-approve"} <= own
    assert _OTHER_CLIENT not in own


def test_a_client_without_a_stored_client_id_counts_as_meho_own() -> None:
    with pytest.raises(MehoAccessRefusedError):
        refuse_own_client_delete({"id": _OTHER_UUID})


# ---------------------------------------------------------------------------
# Refused before approval and when the call runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "kind", "granted"), _CALLERS, ids=_CALLER_IDS)
@pytest.mark.parametrize(("op_id", "params"), _REFUSED, ids=_REFUSED_IDS)
async def test_the_write_is_refused_before_approval(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
    name: str,
    kind: PrincipalKind,
    granted: bool,
) -> None:
    operator = await _caller(kind, granted, op_id)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        call = await _call(op_id, params, operator)
    _refused_before_approval(op_id, call)
    assert await _stored_requests() == []
    assert api.writes == []


@pytest.mark.parametrize(("name", "kind", "granted"), _CALLERS, ids=_CALLER_IDS)
@pytest.mark.parametrize(("op_id", "params"), _REFUSED, ids=_REFUSED_IDS)
async def test_the_write_is_refused_when_it_runs(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
    name: str,
    kind: PrincipalKind,
    granted: bool,
) -> None:
    operator = await _caller(kind, granted, op_id)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        result = await _run(op_id, params, operator)
    _refused_on_run(op_id, result)
    assert api.writes == []


@pytest.mark.parametrize(("op_id", "params"), _REFUSED[:2], ids=_REFUSED_IDS[:2])
async def test_a_client_meho_starts_to_use_while_the_request_waits_is_refused_on_run(
    kc: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
) -> None:
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "another-console")
    get_settings.cache_clear()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        call = await _call(op_id, params)
        assert call["status"] == "awaiting_approval", call
        # While the request waits, MEHO is set up to use this client.
        monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", _WEB_CLIENT)
        get_settings.cache_clear()
        resumed = await _approve_and_run(call)
    _refused_after_wait(op_id, resumed.model_dump(mode="json"))
    assert api.writes == []


@pytest.mark.parametrize("op_id", [_UPDATE, _DELETE])
async def test_another_spelling_of_the_client_id_is_the_same_client(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
) -> None:
    stored = "MEHO-Web"
    extra = {"representation": {"publicClient": True}} if op_id == _UPDATE else {}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        api.add(_client(_WEB_UUID, stored))
        call = await _call(op_id, {"client_id": stored, **extra})
        result = await _run(op_id, {"client_id": stored, **extra})
    _refused_before_approval(op_id, call)
    _refused_on_run(op_id, result)
    assert api.writes == []


@pytest.mark.parametrize("op_id", [_UPDATE, _DELETE])
async def test_a_client_named_by_its_internal_id_is_refused_and_not_named(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
) -> None:
    extra = {"representation": {"secret": "made-up-value"}} if op_id == _UPDATE else {}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        call = await _call(op_id, {"id": _ADMIN_CLIENT_UUID, **extra})
    _refused_before_approval(op_id, call)
    assert _ADMIN_CLIENT not in call["error"]
    assert "made-up-value" not in call["error"]
    assert api.writes == []


@pytest.mark.parametrize(
    "representation",
    [
        {"redirectUris": [_NEW_CALLBACK]},
        {"webOrigins": []},
        {"enabled": False},
        {"clientId": "renamed-console"},
        {"standardFlowEnabled": False},
        {"attributes": {"pkce.code.challenge.method": "plain"}},
    ],
    ids=["drop-redirect", "drop-web-origin", "disable", "rename", "flow-off", "attribute"],
)
async def test_other_changes_of_a_meho_client_are_refused(
    kc: KeycloakConnector,  # noqa: F811
    representation: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        result = await _run(_UPDATE, {"id": _WEB_UUID, "representation": representation})
    _refused_on_run(_UPDATE, result)
    assert api.writes == []


@pytest.mark.parametrize(
    "representation",
    [
        {"enabled": False},
        {"realm": "renamed-login"},
        {"sslRequired": "all"},
        {"enabled": True, "verifyEmail": True},
    ],
    ids=["switch-off", "rename", "ssl", "allowed-with-other"],
)
async def test_other_changes_of_the_login_realm_are_refused(
    kc: KeycloakConnector,  # noqa: F811
    representation: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        call = await _call(_REALM_UPDATE, {"representation": representation})
        result = await _run(_REALM_UPDATE, {"representation": representation})
    _refused_before_approval(_REALM_UPDATE, call)
    _refused_on_run(_REALM_UPDATE, result)
    assert api.writes == []


async def test_the_login_realm_named_with_other_letter_case_is_refused(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    other_case = _LOGIN_REALM.upper()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock, other_case)
        result = await _run(
            _REALM_UPDATE, {"realm": other_case, "representation": {"enabled": False}}
        )
    _refused_on_run(_REALM_UPDATE, result)
    assert api.writes == []


async def test_a_failed_read_of_the_login_realm_is_refused(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        mock.get(f"/admin/realms/{_LOGIN_REALM}").respond(503)
        put = mock.put(f"/admin/realms/{_LOGIN_REALM}").respond(204)
        result = await _run(_REALM_UPDATE, {"representation": {"displayName": "MEHO"}})
    assert result["status"] != "ok", result
    assert not put.called


# ---------------------------------------------------------------------------
# Allowed changes still work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("representation", "field", "value"),
    [
        ({"redirectUris": [_CALLBACK, _NEW_CALLBACK]}, "redirectUris", [_CALLBACK, _NEW_CALLBACK]),
        ({"webOrigins": ["+", "https://console-new.example.test"]}, "webOrigins", None),
        ({"name": "MEHO console"}, "name", "MEHO console"),
        ({"description": "Web console login"}, "description", "Web console login"),
        ({"enabled": True}, "enabled", True),
        ({"publicClient": False, "name": "Console 2"}, "name", "Console 2"),
    ],
    ids=["new-redirect", "new-web-origin", "name", "description", "enable", "same-value"],
)
async def test_an_allowed_change_of_a_meho_client_waits_and_runs(
    kc: KeycloakConnector,  # noqa: F811
    representation: dict[str, Any],
    field: str,
    value: Any,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        call = await _call(_UPDATE, {"client_id": _WEB_CLIENT, "representation": representation})
        assert call["status"] == "awaiting_approval", call
        result = await _approve_and_run(call)
    assert result.status == "ok", result
    assert api.writes == [f"PUT /admin/realms/{_LOGIN_REALM}/clients/{_WEB_UUID}"]
    stored = api.clients[_WEB_UUID]
    assert stored[field] == (representation[field] if value is None else value)
    assert stored["clientId"] == _WEB_CLIENT
    assert stored["publicClient"] is False


@pytest.mark.parametrize(
    "representation",
    [
        {"displayName": "MEHO"},
        {"displayNameHtml": "<b>MEHO</b>"},
        {"smtpServer": {"host": "smtp.example.test", "from": "meho@example.test"}},
        {"internationalizationEnabled": True, "supportedLocales": ["en", "de"]},
        {"defaultLocale": "en"},
        {"eventsEnabled": True, "eventsExpiration": 86400, "enabledEventTypes": ["LOGIN"]},
        {"eventsListeners": ["jboss-logging"]},
        {"adminEventsEnabled": True, "adminEventsDetailsEnabled": False},
        {"enabled": True},
        {"realm": _LOGIN_REALM, "displayName": "MEHO login"},
    ],
    ids=[
        "display-name",
        "display-name-html",
        "smtp",
        "locales",
        "default-locale",
        "events",
        "event-listeners",
        "admin-events",
        "enable",
        "same-name",
    ],
)
async def test_an_allowed_change_of_the_login_realm_waits_and_runs(
    kc: KeycloakConnector,  # noqa: F811
    representation: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        call = await _call(_REALM_UPDATE, {"representation": representation})
        assert call["status"] == "awaiting_approval", call
        result = await _approve_and_run(call)
    assert result.status == "ok", result
    assert api.writes == [f"PUT /admin/realms/{_LOGIN_REALM}"]
    for field, value in representation.items():
        assert api.realm_rep[field] == value
    assert api.realm_rep["enabled"] is True


def test_the_allowed_fields() -> None:
    assert {"redirectUris", "webOrigins", "name", "description", "enabled"} == CLIENT_ALLOWED
    assert "enabled" in REALM_ALLOWED
    assert "realm" not in REALM_ALLOWED
    assert "sslRequired" not in REALM_ALLOWED


async def test_another_client_in_the_login_realm_is_not_refused(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        update = await _run(_UPDATE, {"id": _OTHER_UUID, "representation": {"enabled": False}})
        delete = await _run(_DELETE, {"id": _OTHER_UUID})
    assert update["status"] == "ok", update
    assert delete["status"] == "ok", delete
    assert api.writes == [
        f"PUT /admin/realms/{_LOGIN_REALM}/clients/{_OTHER_UUID}",
        f"DELETE /admin/realms/{_LOGIN_REALM}/clients/{_OTHER_UUID}",
    ]


@pytest.mark.parametrize(("op_id", "params"), _REFUSED, ids=_REFUSED_IDS)
async def test_the_same_writes_in_another_realm_are_not_refused(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock, _OTHER_REALM)
        result = await _run(op_id, params, target=_OTHER_REALM_TARGET)
    assert result["status"] == "ok", result
    assert len(api.writes) == 1, api.writes


async def test_a_client_that_is_not_found_waits_and_then_fails_as_not_found(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    missing = "c1c1c1c1-0000-0000-0000-0000000000ff"
    params = {"id": missing, "representation": {"enabled": False}}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        call = await _call(_UPDATE, params)
        result = await _run(_UPDATE, params)
    assert call["status"] == "awaiting_approval", call
    assert result["status"] != "ok", result
    assert api.writes == []
