# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak writes keep MEHO's own sign-in clients and its login realm working.

Drives ``keycloak.client.update``, ``keycloak.client.delete`` and
``keycloak.realm.update`` through the real dispatch stack against a
respx-mocked Keycloak Admin REST API (made-up names and ids). The target
manages MEHO's own login realm. What is proven:

* each write that can break MEHO's sign-in is refused before approval and
  when it runs, for a person, an agent login and a service login, with and
  without a standing grant;
* a client is MEHO's own by MEHO's settings, also when the call names it by
  its internal id or the stored client id has other letter case;
* a client that becomes MEHO's own while the request waits is refused when
  the approved call runs;
* each allowed change waits for approval and runs;
* the same writes on another client, or in another realm, are not refused;
* the refusal text names no client the caller did not name and gives no
  advice to change rights.

Nothing is written to Keycloak when MEHO refuses.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_meho_own_login_guard import (
    LOGIN_REALM_CHANGES,
    OWN_CLIENT_CHANGES,
    blocked_changes,
    meho_own_client_ids,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, ServicePrincipalGrant
from meho_backplane.operations import dispatch
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests.test_connectors_keycloak_meho_access_guard import _approve_and_run
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

_ADMIN_CLIENT = "meho-backplane-admin"
_OWN_UUID = "c1111111-1111-1111-1111-111111111111"
_OTHER_UUID = "c2222222-2222-2222-2222-222222222222"
_OTHER_CLIENT = "event-portal"
_REDIRECT = "https://console.example.invalid/*"
_NEW_REDIRECT = "https://console-new.example.invalid/*"

_CLIENT_UPDATE = "keycloak.client.update"
_CLIENT_DELETE = "keycloak.client.delete"
_REALM_UPDATE = "keycloak.realm.update"

#: Words of advice to change rights or to go around the check; no refusal text has them.
_ADVICE = ("give a second person", "ask another", "admin console", "first give", "then send")


@pytest.fixture(autouse=True)
def _admin_client_env(
    _required_settings_env: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", _ADMIN_CLIENT)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client(client_uuid: str, client_id: str) -> dict[str, Any]:
    return {
        "id": client_uuid,
        "clientId": client_id,
        "name": "",
        "description": "",
        "enabled": True,
        "publicClient": False,
        "redirectUris": [_REDIRECT],
        "webOrigins": ["+"],
        "attributes": {"post.logout.redirect.uris": "+"},
        "authenticationFlowBindingOverrides": {"browser": "made-up-flow-id"},
    }


def _realm_rep(realm: str) -> dict[str, Any]:
    return {
        "id": f"id-of-{realm}",
        "realm": realm,
        "enabled": True,
        "displayName": "Sign in",
        "sslRequired": "external",
        "eventsEnabled": False,
        "attributes": {"frontendUrl": ""},
        "browserFlow": "passkey-browser",
    }


class _Keycloak:
    """The mocked Admin API of one realm: the realm and its clients."""

    def __init__(self, mock: respx.MockRouter, realm: str) -> None:
        self.realm = _realm_rep(realm)
        self.clients = {
            _OWN_UUID: _client(_OWN_UUID, _ADMIN_CLIENT),
            _OTHER_UUID: _client(_OTHER_UUID, _OTHER_CLIENT),
        }
        base = f"/admin/realms/{realm}"
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        mock.route(url__regex=rf".*{base}/clients/[^/?]+(\?.*)?$").mock(side_effect=self._one)
        mock.get(url__regex=rf".*{base}/clients(\?.*)?$").mock(side_effect=self._find)
        mock.route(url__regex=rf".*{base}(\?.*)?$").mock(side_effect=self._realm)
        mock.get(url__regex=rf".*{base}/.+").respond(200, json=[])

    def _find(self, request: httpx.Request) -> httpx.Response:
        wanted = request.url.params.get("clientId")
        rows = [c for c in self.clients.values() if wanted is None or c["clientId"] == wanted]
        return httpx.Response(200, json=rows)

    def _one(self, request: httpx.Request) -> httpx.Response:
        client_uuid = request.url.path.rsplit("/", 1)[1]
        client = self.clients.get(client_uuid)
        if client is None:
            return httpx.Response(404)
        if request.method == "PUT":
            self.clients[client_uuid] = json.loads(request.content)
            return httpx.Response(204)
        if request.method == "DELETE":
            del self.clients[client_uuid]
            return httpx.Response(204)
        return httpx.Response(200, json=client)

    def _realm(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            self.realm = json.loads(request.content)
            return httpx.Response(204)
        return httpx.Response(200, json=self.realm)


def _writes(mock: respx.MockRouter) -> list[str]:
    return [
        f"{call.request.method} {call.request.url.path}"
        for call in mock.calls
        if call.request.method in {"PUT", "POST", "DELETE"} and "/admin/" in call.request.url.path
    ]


def _person(sub: str = "kc-own-login-person") -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Own Login Test Person",
        email=None,
        raw_jwt="<kc-own-login-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


def _machine(kind: PrincipalKind) -> Operator:
    return Operator(
        sub=f"kc-own-login-{kind.value}",
        name=f"own login {kind.value}",
        email=None,
        raw_jwt="<kc-own-login-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
        client_id=f"own-login-{kind.value}",
    )


_CALLERS = {
    "person": _person,
    "agent": lambda: _machine(PrincipalKind.AGENT),
    "service": lambda: _machine(PrincipalKind.SERVICE),
}


async def _grant(caller: Operator, op_id: str) -> None:
    """A standing grant that lets *caller* run *op_id* on the login realm target."""
    target_id = (await _target(_LOGIN_REALM_TARGET)).id
    async with get_sessionmaker()() as session:
        if caller.principal_kind is PrincipalKind.AGENT:
            session.add(
                AgentPermission(
                    tenant_id=_TENANT_ID,
                    principal_sub=caller.sub,
                    op_pattern=op_id,
                    verdict="auto-execute",
                    created_by_sub="kc-own-login-admin",
                )
            )
        else:
            session.add(
                ServicePrincipalGrant(
                    id=uuid.uuid4(),
                    tenant_id=_TENANT_ID,
                    principal_sub=caller.sub,
                    op_id=op_id,
                    connector_id=_CONNECTOR_ID,
                    target_id=target_id,
                    reason="unattended upkeep",
                    created_by_sub="kc-own-login-admin",
                )
            )
        await session.commit()


async def _call(
    op_id: str,
    params: dict[str, Any],
    caller: Operator | None = None,
    target: str = _LOGIN_REALM_TARGET,
) -> dict[str, Any]:
    """Call the op as an agent does; a delete first asks for its preview, as MEHO requires."""
    caller = caller or _person()
    args: dict[str, Any] = {
        "connector_id": _CONNECTOR_ID,
        "op_id": op_id,
        "target": target,
        "params": params,
    }
    if op_id == _CLIENT_DELETE:
        preview = await preview_operation(caller, args)
        if preview.get("preview_hash"):
            args["preview_hash"] = preview["preview_hash"]
    return await call_operation(caller, args)


async def _run(
    op_id: str,
    params: dict[str, Any],
    caller: Operator | None = None,
    target: str = _LOGIN_REALM_TARGET,
) -> dict[str, Any]:
    result = await dispatch(
        operator=caller or _person(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=await _target(target),
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def _no_advice(text: str) -> None:
    assert "this write was refused:" in text, text
    assert not [w for w in _ADVICE if w in text.casefold()], text


def _refused_before_approval(call: dict[str, Any], words: str) -> None:
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval", call
    assert words in call["error"], call["error"]
    _no_advice(call["error"])


def _refused_on_run(result: dict[str, Any], code: str) -> None:
    assert result["status"] != "ok", result
    assert result["extras"]["exception_class"] == "MehoAccessRefusedError", result
    message = result["extras"]["exception_message"]
    # keycloak.realm.update can send a secret, so MEHO withholds its error text.
    if "withheld" not in message:
        assert code in message, result
        _no_advice(message)


_OWN_CLIENT_WORDS = "this client is one that MEHO itself signs in with"
_LOGIN_REALM_WORDS = "this realm is the realm MEHO itself signs in with"

#: Each refused write: (op_id, params, the words of the refusal, the code).
_REFUSED: list[tuple[str, dict[str, Any], str, str]] = [
    (
        _CLIENT_UPDATE,
        {"id": _OWN_UUID, "representation": {"enabled": False}},
        _OWN_CLIENT_WORDS,
        "meho_own_client",
    ),
    (_CLIENT_DELETE, {"id": _OWN_UUID}, _OWN_CLIENT_WORDS, "meho_own_client"),
    (
        _REALM_UPDATE,
        {"representation": {"enabled": False}},
        _LOGIN_REALM_WORDS,
        "meho_login_realm",
    ),
]
_REFUSED_IDS = ["client-update", "client-delete", "realm-update"]


# ---------------------------------------------------------------------------
# Refused before approval and when it runs, for every kind of caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("granted", [False, True], ids=["no-grant", "standing-grant"])
@pytest.mark.parametrize("caller_kind", list(_CALLERS))
@pytest.mark.parametrize(("op_id", "params", "words", "code"), _REFUSED, ids=_REFUSED_IDS)
async def test_the_write_is_refused_before_approval(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
    words: str,
    code: str,
    caller_kind: str,
    granted: bool,
) -> None:
    caller = _CALLERS[caller_kind]()
    if granted:
        await _grant(caller, op_id)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _LOGIN_REALM)
        call = await _call(op_id, params, caller)
        written = _writes(mock)
    _refused_before_approval(call, words)
    assert await _stored_requests() == []
    assert written == []


@pytest.mark.parametrize("granted", [False, True], ids=["no-grant", "standing-grant"])
@pytest.mark.parametrize("caller_kind", list(_CALLERS))
@pytest.mark.parametrize(("op_id", "params", "words", "code"), _REFUSED, ids=_REFUSED_IDS)
async def test_the_write_is_refused_when_it_runs(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
    words: str,
    code: str,
    caller_kind: str,
    granted: bool,
) -> None:
    caller = _CALLERS[caller_kind]()
    if granted:
        await _grant(caller, op_id)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _LOGIN_REALM)
        result = await _run(op_id, params, caller)
        written = _writes(mock)
    _refused_on_run(result, code)
    assert written == []


async def test_a_person_call_is_refused_before_it_waits(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _LOGIN_REALM)
        call = await _call(_CLIENT_UPDATE, {"id": _OWN_UUID, "representation": {"enabled": False}})
    _refused_before_approval(call, _OWN_CLIENT_WORDS)
    assert "No approval and no grant allows this write." in call["error"]


# ---------------------------------------------------------------------------
# Which client is MEHO's own
# ---------------------------------------------------------------------------


def test_meho_own_clients_come_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECK_RUNNER_CLIENT_ID", "check-runner")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "web-console")
    monkeypatch.setenv("KEYCLOAK_CLI_CLIENT_ID", "cli-login")
    get_settings.cache_clear()
    own = meho_own_client_ids()
    for name in (
        _ADMIN_CLIENT,
        "check-runner",
        "web-console",
        "cli-login",
        "meho-backplane",
        "meho-cli-approve",
        "meho-web-approve",
        "meho-mcp",
        "meho-mcp-code",
        "meho-mcp-desktop",
    ):
        assert name in own, name
    assert _OTHER_CLIENT not in own


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        (_CLIENT_UPDATE, {"client_id": _ADMIN_CLIENT, "representation": {"enabled": False}}),
        (_CLIENT_DELETE, {"client_id": _ADMIN_CLIENT}),
    ],
    ids=["update", "delete"],
)
async def test_a_client_named_by_its_client_id_is_refused(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _LOGIN_REALM)
        call = await _call(op_id, params)
        result = await _run(op_id, params)
        written = _writes(mock)
    _refused_before_approval(call, _OWN_CLIENT_WORDS)
    _refused_on_run(result, "meho_own_client")
    assert written == []


@pytest.mark.parametrize("op_id", [_CLIENT_UPDATE, _CLIENT_DELETE])
async def test_a_client_in_other_letter_case_is_the_same_client(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
) -> None:
    spelled = "MEHO-Backplane-Admin"
    params: dict[str, Any] = {"client_id": spelled}
    if op_id == _CLIENT_UPDATE:
        params["representation"] = {"enabled": False}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _LOGIN_REALM)
        api.clients[_OWN_UUID]["clientId"] = spelled
        call = await _call(op_id, params)
        result = await _run(op_id, params)
        written = _writes(mock)
    _refused_before_approval(call, _OWN_CLIENT_WORDS)
    _refused_on_run(result, "meho_own_client")
    assert written == []


@pytest.mark.parametrize("op_id", [_CLIENT_UPDATE, _CLIENT_DELETE])
async def test_a_client_named_by_its_internal_id_is_not_named_in_the_refusal(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
) -> None:
    params: dict[str, Any] = {"id": _OWN_UUID}
    if op_id == _CLIENT_UPDATE:
        params["representation"] = {"redirectUris": []}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _LOGIN_REALM)
        call = await _call(op_id, params)
        result = await _run(op_id, params)
    _refused_before_approval(call, _OWN_CLIENT_WORDS)
    assert _ADMIN_CLIENT not in call["error"]
    _refused_on_run(result, "meho_own_client")
    assert _ADMIN_CLIENT not in result["extras"]["exception_message"]
    assert "withheld" not in result["extras"]["exception_message"]


async def test_a_client_that_becomes_meho_own_while_the_request_waits_is_refused_on_run(
    kc: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "another-client")
    get_settings.cache_clear()
    params = {"id": _OWN_UUID, "representation": {"enabled": False}}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _LOGIN_REALM)
        call = await _call(_CLIENT_UPDATE, params)
        assert call["status"] == "awaiting_approval", call
        # While the request waits, MEHO is set up to sign in with this client.
        monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", _ADMIN_CLIENT)
        get_settings.cache_clear()
        resumed = await _approve_and_run(call)
        written = _writes(mock)
    _refused_on_run(resumed.model_dump(mode="json"), "meho_own_client")
    assert written == []


async def test_when_meho_cannot_read_its_settings_the_run_is_refused(
    kc: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.connectors.keycloak import ops_meho_own_login_guard

    def _broken() -> frozenset[str]:
        raise RuntimeError("settings cannot be read")

    monkeypatch.setattr(ops_meho_own_login_guard, "meho_own_client_ids", _broken)
    params = {"id": _OTHER_UUID, "representation": {"enabled": False}}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _LOGIN_REALM)
        result = await _run(_CLIENT_UPDATE, params)
        written = _writes(mock)
    _refused_on_run(result, "meho_access_not_checked")
    assert written == []


# ---------------------------------------------------------------------------
# Other changes that can lock MEHO out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "representation",
    [
        {"redirectUris": [_NEW_REDIRECT]},
        {"webOrigins": []},
        {"publicClient": True},
        {"attributes": {"post.logout.redirect.uris": "-"}},
        {"protocolMappers": []},
        {"authenticationFlowBindingOverrides": {"browser": "another-flow-id"}},
        {"description": "new text", "enabled": False},
    ],
    ids=[
        "redirect-replaced",
        "web-origins-emptied",
        "public",
        "attribute",
        "mappers",
        "new-flow",
        "mixed",
    ],
)
async def test_other_changes_of_meho_own_client_are_refused(
    kc: KeycloakConnector,  # noqa: F811
    representation: dict[str, Any],
) -> None:
    params = {"id": _OWN_UUID, "representation": representation}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _LOGIN_REALM)
        call = await _call(_CLIENT_UPDATE, params)
        result = await _run(_CLIENT_UPDATE, params)
        written = _writes(mock)
    _refused_before_approval(call, _OWN_CLIENT_WORDS)
    _refused_on_run(result, "meho_own_client")
    assert written == []


@pytest.mark.parametrize("named", [_LOGIN_REALM, _LOGIN_REALM.upper()], ids=["same", "upper-case"])
async def test_the_login_realm_named_by_the_realm_param_is_refused(
    kc: KeycloakConnector,  # noqa: F811
    named: str,
) -> None:
    params = {"realm": named, "representation": {"enabled": False}}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, named)
        api.realm["realm"] = _LOGIN_REALM
        call = await _call(_REALM_UPDATE, params, target=_OTHER_REALM_TARGET)
        result = await _run(_REALM_UPDATE, params, target=_OTHER_REALM_TARGET)
        written = _writes(mock)
    _refused_before_approval(call, _LOGIN_REALM_WORDS)
    _refused_on_run(result, "meho_login_realm")
    assert written == []


@pytest.mark.parametrize(
    "representation",
    [
        {"realm": "renamed"},
        {"sslRequired": "all"},
        {"attributes": {"frontendUrl": "https://elsewhere.invalid"}},
        {"browserFlow": "made-up-flow"},
        {"defaultSignatureAlgorithm": "HS256"},
        {"displayName": "New name", "enabled": False},
    ],
    ids=["rename", "ssl", "frontend-url", "browser-flow", "signature", "mixed"],
)
async def test_changes_of_the_target_login_realm_are_refused(
    kc: KeycloakConnector,  # noqa: F811
    representation: dict[str, Any],
) -> None:
    params = {"representation": representation}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _LOGIN_REALM)
        call = await _call(_REALM_UPDATE, params)
        result = await _run(_REALM_UPDATE, params)
        written = _writes(mock)
    _refused_before_approval(call, _LOGIN_REALM_WORDS)
    _refused_on_run(result, "meho_login_realm")
    assert written == []


# ---------------------------------------------------------------------------
# Allowed changes still run
# ---------------------------------------------------------------------------

_ALLOWED_CLIENT: list[dict[str, Any]] = [
    {"name": "MEHO admin login"},
    {"description": "The client MEHO signs in with"},
    {"redirectUris": [_REDIRECT, _NEW_REDIRECT]},
    {"webOrigins": ["+", "https://console-new.example.invalid"]},
    {"enabled": True},
    {"publicClient": False, "redirectUris": [_REDIRECT]},
    {"authenticationFlowBindingOverrides": {"browser": ""}},
]
_ALLOWED_CLIENT_IDS = [
    "name",
    "description",
    "new-redirect",
    "new-web-origin",
    "on",
    "same-values",
    "realm-flow-again",
]


@pytest.mark.parametrize("representation", _ALLOWED_CLIENT, ids=_ALLOWED_CLIENT_IDS)
async def test_an_allowed_change_of_meho_own_client_runs(
    kc: KeycloakConnector,  # noqa: F811
    representation: dict[str, Any],
) -> None:
    params = {"id": _OWN_UUID, "representation": representation}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _LOGIN_REALM)
        call = await _call(_CLIENT_UPDATE, params)
        assert call["status"] in {"awaiting_approval", "ok"}, call
        if call["status"] == "awaiting_approval":
            result = (await _approve_and_run(call)).model_dump(mode="json")
        else:
            result = call
        written = _writes(mock)
    assert result["status"] == "ok", result
    assert written == [f"PUT /admin/realms/{_LOGIN_REALM}/clients/{_OWN_UUID}"]
    for key, value in representation.items():
        assert api.clients[_OWN_UUID][key] == value


_ALLOWED_REALM: list[dict[str, Any]] = [
    {"displayName": "MEHO sign-in"},
    {"displayNameHtml": "<b>MEHO</b>"},
    {"smtpServer": {"host": "mail.example.invalid", "port": "587", "from": "no-reply@x.invalid"}},
    {"eventsEnabled": True, "eventsExpiration": 3600, "enabledEventTypes": ["LOGIN_ERROR"]},
    {"eventsListeners": ["jboss-logging"]},
    {"adminEventsEnabled": True, "adminEventsDetailsEnabled": False},
    {"internationalizationEnabled": True, "supportedLocales": ["en", "de"], "defaultLocale": "en"},
    {"enabled": True},
    {"sslRequired": "external", "displayName": "MEHO"},
    {"browserFlow": "browser"},
]
_ALLOWED_REALM_IDS = [
    "display-name",
    "display-name-html",
    "mail-server",
    "events",
    "event-listeners",
    "admin-events",
    "languages",
    "on",
    "same-values",
    "builtin-browser-flow",
]


@pytest.mark.parametrize("representation", _ALLOWED_REALM, ids=_ALLOWED_REALM_IDS)
async def test_an_allowed_change_of_the_login_realm_runs(
    kc: KeycloakConnector,  # noqa: F811
    representation: dict[str, Any],
) -> None:
    params = {"representation": representation}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _LOGIN_REALM)
        call = await _call(_REALM_UPDATE, params)
        assert call["status"] in {"awaiting_approval", "ok"}, call
        if call["status"] == "awaiting_approval":
            result = (await _approve_and_run(call)).model_dump(mode="json")
        else:
            result = call
        written = _writes(mock)
    assert result["status"] == "ok", result
    assert written == [f"PUT /admin/realms/{_LOGIN_REALM}"]
    for key, value in representation.items():
        assert api.realm[key] == value


@pytest.mark.parametrize(
    ("op_id", "params", "written"),
    [
        (
            _CLIENT_UPDATE,
            {"id": _OTHER_UUID, "representation": {"enabled": False}},
            f"PUT /admin/realms/{_LOGIN_REALM}/clients/{_OTHER_UUID}",
        ),
        (
            _CLIENT_DELETE,
            {"id": _OTHER_UUID},
            f"DELETE /admin/realms/{_LOGIN_REALM}/clients/{_OTHER_UUID}",
        ),
    ],
    ids=["update", "delete"],
)
async def test_another_client_in_the_login_realm_is_not_refused(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
    written: str,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _LOGIN_REALM)
        call = await _call(op_id, params)
        assert call["status"] == "awaiting_approval", call
        result = await _run(op_id, params)
        writes = _writes(mock)
    assert result["status"] == "ok", result
    assert writes == [written]


@pytest.mark.parametrize(
    ("op_id", "params", "written"),
    [
        (
            _CLIENT_UPDATE,
            {"id": _OWN_UUID, "representation": {"enabled": False}},
            f"PUT /admin/realms/{_OTHER_REALM}/clients/{_OWN_UUID}",
        ),
        (
            _CLIENT_DELETE,
            {"id": _OWN_UUID},
            f"DELETE /admin/realms/{_OTHER_REALM}/clients/{_OWN_UUID}",
        ),
        (
            _REALM_UPDATE,
            {"representation": {"enabled": False}},
            f"PUT /admin/realms/{_OTHER_REALM}",
        ),
    ],
    ids=["client-update", "client-delete", "realm-update"],
)
async def test_the_same_writes_in_another_realm_are_not_refused(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
    written: str,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _OTHER_REALM)
        result = await _run(op_id, params, target=_OTHER_REALM_TARGET)
        writes = _writes(mock)
    assert result["status"] == "ok", result
    assert writes == [written]


# ---------------------------------------------------------------------------
# The rule on its own
# ---------------------------------------------------------------------------


def test_a_key_sent_with_its_current_value_is_no_change() -> None:
    current = _client(_OWN_UUID, _ADMIN_CLIENT)
    assert blocked_changes(current, {"publicClient": False}, OWN_CLIENT_CHANGES) == []
    assert blocked_changes(current, {"publicClient": True}, OWN_CLIENT_CHANGES) == ["publicClient"]


def test_a_new_key_counts_as_a_change() -> None:
    current = _realm_rep(_LOGIN_REALM)
    assert blocked_changes(current, {"otpPolicyType": None}, LOGIN_REALM_CHANGES) == [
        "otpPolicyType"
    ]


def test_a_redirect_list_may_only_grow() -> None:
    current = _client(_OWN_UUID, _ADMIN_CLIENT)
    grown = {"redirectUris": [_NEW_REDIRECT, _REDIRECT]}
    assert blocked_changes(current, grown, OWN_CLIENT_CHANGES) == []
    shrunk = {"redirectUris": [_NEW_REDIRECT]}
    assert blocked_changes(current, shrunk, OWN_CLIENT_CHANGES) == ["redirectUris"]
    odd = {"redirectUris": "not-a-list"}
    assert blocked_changes(copy.deepcopy(current), odd, OWN_CLIENT_CHANGES) == ["redirectUris"]
