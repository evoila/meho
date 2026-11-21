# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak writes keep MEHO's own sign-in clients and its login realm working.

Drives ``keycloak.client.update``, ``keycloak.client.delete``,
``keycloak.realm.update``, ``keycloak.protocol_mapper.create`` and
``keycloak.client_scope.protocol_mapper.update`` through the real dispatch stack against a
respx-mocked Keycloak Admin REST API (made-up names and ids). The target
manages MEHO's own login realm. What is proven:

* each write that can break MEHO's sign-in is refused before approval and
  when it runs, for a person, an agent login and a service login, with and
  without a standing grant; a standing grant never runs these writes
  without approval;
* a client is MEHO's own by MEHO's settings, also when the call names it by
  its internal id or the stored client id has other letter case;
* a client that becomes MEHO's own while the request waits is refused when
  the approved call runs;
* a realm named in another spelling that Keycloak answers as the login
  realm counts as the login realm, before approval and at the run; when
  MEHO cannot read the realm, or the realm comes back without its name,
  the run is refused;
* an approval client keeps its own login flow;
* a new mapper on each of MEHO's own sign-in clients, and a mapper change on
  a client scope such a client uses, are refused like the client change;
* each allowed change waits for approval and runs;
* the same writes on another client, or in another realm, are not refused;
* the refusal text names no client the caller did not name, gives no
  advice to change rights, and says where a realm administrator makes the
  change.

Nothing is written to Keycloak when MEHO refuses.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

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
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests.test_connectors_keycloak_meho_access_guard import _approve_and_run
from tests.test_connectors_keycloak_user_login_guard import (  # noqa: F401  (fixtures)
    _ADMIN_REALM,
    _CONNECTOR_ID,
    _KC_BASE_URL,
    _KC_HOST,
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

#: A realm name in another spelling that Keycloak answers as the login realm.
_SPELLED_REALM = "meh\u00f6"
_SPELLED_TARGET = "kc-guard-login-realm-spelled"

_CLIENT_UPDATE = "keycloak.client.update"
_CLIENT_DELETE = "keycloak.client.delete"
_REALM_UPDATE = "keycloak.realm.update"

#: Words of advice to change rights or to go around the check; no refusal text has them.
_ADVICE = ("give a second person", "ask another", "first give", "then send")
#: The words of each refusal that say where the change is made.
_WHERE = "Use the Keycloak admin console as a realm admin."

_MAPPER_CREATE = "keycloak.protocol_mapper.create"
_SCOPE_MAPPER_UPDATE = "keycloak.client_scope.protocol_mapper.update"
_OWN_SCOPE_UUID = "51111111-1111-1111-1111-111111111111"
_OTHER_SCOPE_UUID = "52222222-2222-2222-2222-222222222222"
_OWN_SCOPE = "tenant-claims"
_OTHER_SCOPE = "event-claims"
_MAPPER_UUID = "a1111111-1111-1111-1111-111111111111"
_NEW_MAPPER = {
    "name": "extra-claim",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-hardcoded-claim-mapper",
    "config": {"claim.name": "extra", "claim.value": "x"},
}
_MAPPER_CHANGE = {"config": {"claim.value": "y"}}


@pytest.fixture(autouse=True)
def _admin_client_env(
    _required_settings_env: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", _ADMIN_CLIENT)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def spelled_target(kc: KeycloakConnector) -> str:  # noqa: F811
    """A target whose managed realm is the login realm in another spelling."""
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_SPELLED_TARGET,
                aliases=[],
                product="keycloak",
                host=_KC_HOST,
                port=443,
                fqdn=None,
                secret_ref="keycloak/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _SPELLED_REALM, "admin_realm": _ADMIN_REALM},
                fingerprint={"version": "26.3.3"},
                notes="seeded by test_connectors_keycloak_meho_own_login_guard",
            )
        )
        await session.commit()
    return _SPELLED_TARGET


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
        "defaultClientScopes": ["basic", _OWN_SCOPE if client_uuid == _OWN_UUID else _OTHER_SCOPE],
        "optionalClientScopes": [],
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
        self.realm_read_fails = False
        self.clients = {
            _OWN_UUID: _client(_OWN_UUID, _ADMIN_CLIENT),
            _OTHER_UUID: _client(_OTHER_UUID, _OTHER_CLIENT),
        }
        self.scopes = {
            _OWN_SCOPE_UUID: {"id": _OWN_SCOPE_UUID, "name": _OWN_SCOPE},
            _OTHER_SCOPE_UUID: {"id": _OTHER_SCOPE_UUID, "name": _OTHER_SCOPE},
        }
        base = re.escape(f"/admin/realms/{quote(realm, safe='')}")
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        mock.post(url__regex=rf".*{base}/clients/[^/]+/protocol-mappers/models$").respond(201)
        mapper = rf".*{base}/client-scopes/[^/]+/protocol-mappers/models"
        mock.route(url__regex=rf"{mapper}/[^/?]+$").mock(side_effect=self._mapper)
        mock.get(url__regex=rf"{mapper}$").mock(side_effect=self._mappers)
        mock.get(url__regex=rf".*{base}/client-scopes/[^/?]+$").mock(side_effect=self._scope)
        mock.get(url__regex=rf".*{base}/client-scopes$").mock(
            side_effect=lambda _r: httpx.Response(200, json=list(self.scopes.values()))
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

    def _scope(self, request: httpx.Request) -> httpx.Response:
        scope = self.scopes.get(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(404) if scope is None else httpx.Response(200, json=scope)

    def _mappers(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[self._mapper_rep()])

    def _mapper(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(204)
        return httpx.Response(200, json=self._mapper_rep())

    @staticmethod
    def _mapper_rep() -> dict[str, Any]:
        return {**_NEW_MAPPER, "id": _MAPPER_UUID}

    def _realm(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            self.realm = json.loads(request.content)
            return httpx.Response(204)
        if self.realm_read_fails:
            return httpx.Response(500)
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


async def _grant(caller: Operator, op_id: str, target: str = _LOGIN_REALM_TARGET) -> None:
    """A standing grant that lets *caller* run *op_id* on *target*."""
    target_id = (await _target(target)).id
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


@pytest.mark.parametrize("granted", [False, True], ids=["no-grant", "approved-and-granted"])
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


#: Writes the check lets through: (op_id, params).
_NOT_REFUSED: list[tuple[str, dict[str, Any]]] = [
    (_CLIENT_UPDATE, {"id": _OTHER_UUID, "representation": {"enabled": False}}),
    (_CLIENT_DELETE, {"id": _OTHER_UUID}),
    (_REALM_UPDATE, {"representation": {"displayName": "MEHO sign-in"}}),
]


@pytest.mark.parametrize("caller_kind", ["agent", "service"])
@pytest.mark.parametrize(("op_id", "params"), _NOT_REFUSED, ids=_REFUSED_IDS)
async def test_a_standing_grant_does_not_run_the_write_without_approval(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
    caller_kind: str,
) -> None:
    caller = _CALLERS[caller_kind]()
    await _grant(caller, op_id)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _LOGIN_REALM)
        call = await _call(op_id, params, caller)
        written = _writes(mock)
    assert call["status"] == "awaiting_approval", call
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
# The login realm in another spelling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("op_id", "params", "words", "code"), _REFUSED, ids=_REFUSED_IDS)
async def test_a_target_realm_in_another_spelling_is_the_login_realm(
    spelled_target: str,
    op_id: str,
    params: dict[str, Any],
    words: str,
    code: str,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _SPELLED_REALM)
        api.realm["realm"] = _LOGIN_REALM
        call = await _call(op_id, params, target=spelled_target)
        result = await _run(op_id, params, target=spelled_target)
        written = _writes(mock)
    _refused_before_approval(call, words)
    _refused_on_run(result, code)
    assert written == []


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        (_CLIENT_UPDATE, {"id": _OWN_UUID, "representation": {"enabled": False}}),
        (_CLIENT_DELETE, {"id": _OWN_UUID}),
    ],
    ids=["update", "delete"],
)
async def test_when_meho_cannot_read_the_realm_the_client_write_is_refused_on_run(
    spelled_target: str,
    op_id: str,
    params: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _SPELLED_REALM)
        api.realm_read_fails = True
        call = await _call(op_id, params, target=spelled_target)
        result = await _run(op_id, params, target=spelled_target)
        written = _writes(mock)
    # Before approval a realm MEHO cannot read is left to the run.
    assert call["status"] == "awaiting_approval", call
    _refused_on_run(result, "meho_access_not_checked")
    assert written == []


async def test_a_realm_answer_without_its_name_refuses_the_realm_update_on_run(
    spelled_target: str,
) -> None:
    params = {"representation": {"enabled": False}}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _SPELLED_REALM)
        del api.realm["realm"]
        call = await _call(_REALM_UPDATE, params, target=spelled_target)
        result = await _run(_REALM_UPDATE, params, target=spelled_target)
        written = _writes(mock)
    assert call["status"] in {"denied", "awaiting_approval"}, call
    _refused_on_run(result, "meho_access_not_checked")
    assert written == []


async def test_a_realm_answer_that_is_a_list_refuses_the_client_update_on_run(
    spelled_target: str,
) -> None:
    params = {"id": _OWN_UUID, "representation": {"enabled": False}}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _SPELLED_REALM)
        api.realm = [_realm_rep(_LOGIN_REALM)]  # type: ignore[assignment]
        call = await _call(_CLIENT_UPDATE, params, target=spelled_target)
        result = await _run(_CLIENT_UPDATE, params, target=spelled_target)
        written = _writes(mock)
    assert call["status"] in {"denied", "awaiting_approval"}, call
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


@pytest.mark.parametrize("approval_client", ["meho-cli-approve", "meho-web-approve"])
async def test_an_approval_client_keeps_its_own_login_flow(
    kc: KeycloakConnector,  # noqa: F811
    approval_client: str,
) -> None:
    params = {
        "id": _OWN_UUID,
        "representation": {"authenticationFlowBindingOverrides": {"browser": ""}},
    }
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _LOGIN_REALM)
        api.clients[_OWN_UUID]["clientId"] = approval_client
        call = await _call(_CLIENT_UPDATE, params)
        result = await _run(_CLIENT_UPDATE, params)
        written = _writes(mock)
    _refused_before_approval(call, _OWN_CLIENT_WORDS)
    assert "authenticationFlowBindingOverrides" in call["error"]
    _refused_on_run(result, "meho_own_client")
    assert written == []


@pytest.mark.parametrize(
    "named",
    [_LOGIN_REALM, _LOGIN_REALM.upper(), _SPELLED_REALM],
    ids=["same", "upper-case", "another-spelling"],
)
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


# ---------------------------------------------------------------------------
# Mapper writes follow the rule of the client change
# ---------------------------------------------------------------------------

#: Each sign-in client MEHO uses, with the settings of the fixture below.
_OWN_CLIENTS = [
    _ADMIN_CLIENT,
    "check-runner",
    "meho-backplane",
    "meho-cli",
    "meho-cli-approve",
    "meho-web",
    "meho-web-approve",
    "meho-mcp",
    "meho-mcp-code",
    "meho-mcp-desktop",
]


@pytest.fixture
def _every_own_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECK_RUNNER_CLIENT_ID", "check-runner")
    monkeypatch.setenv("KEYCLOAK_CLI_CLIENT_ID", "meho-cli")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    get_settings.cache_clear()


def _mapper_create(client_uuid: str = _OWN_UUID) -> dict[str, Any]:
    return {"id": client_uuid, "representation": dict(_NEW_MAPPER)}


def _scope_mapper_update(scope_uuid: str = _OWN_SCOPE_UUID) -> dict[str, Any]:
    return {
        "scope_id": scope_uuid,
        "mapper_id": _MAPPER_UUID,
        "representation": copy.deepcopy(_MAPPER_CHANGE),
    }


_SCOPE_WORDS = "a client MEHO signs in with uses this scope"


@pytest.mark.usefixtures("_every_own_client")
@pytest.mark.parametrize("own_client", _OWN_CLIENTS)
async def test_a_new_mapper_on_meho_own_client_is_refused(
    kc: KeycloakConnector,  # noqa: F811
    own_client: str,
) -> None:
    assert own_client.casefold() in meho_own_client_ids()
    params = _mapper_create()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _LOGIN_REALM)
        api.clients[_OWN_UUID]["clientId"] = own_client
        call = await _call(_MAPPER_CREATE, params)
        result = await _run(_MAPPER_CREATE, params)
        written = _writes(mock)
    _refused_before_approval(call, _OWN_CLIENT_WORDS)
    assert "adding a protocol mapper" in call["error"]
    _refused_on_run(result, "meho_own_client")
    assert written == []


@pytest.mark.usefixtures("_every_own_client")
@pytest.mark.parametrize("own_client", _OWN_CLIENTS)
@pytest.mark.parametrize("kind", ["defaultClientScopes", "optionalClientScopes"])
async def test_a_mapper_change_on_a_scope_of_meho_own_client_is_refused(
    kc: KeycloakConnector,  # noqa: F811
    own_client: str,
    kind: str,
) -> None:
    params = _scope_mapper_update()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _LOGIN_REALM)
        own = api.clients[_OWN_UUID]
        own["clientId"] = own_client
        own["defaultClientScopes"], own["optionalClientScopes"] = ["basic"], []
        own[kind] = [_OWN_SCOPE]
        call = await _call(_SCOPE_MAPPER_UPDATE, params)
        result = await _run(_SCOPE_MAPPER_UPDATE, params)
        written = _writes(mock)
    _refused_before_approval(call, _SCOPE_WORDS)
    assert own_client not in call["error"]
    _refused_on_run(result, "meho_own_client")
    assert written == []


async def test_a_mapper_change_is_refused_on_run_when_meho_cannot_read_the_clients(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    params = _scope_mapper_update(_OTHER_SCOPE_UUID)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _LOGIN_REALM)
        api.clients[_OTHER_UUID]["defaultClientScopes"] = "not-a-list"
        api.clients[_OWN_UUID]["defaultClientScopes"] = "not-a-list"
        call = await _call(_SCOPE_MAPPER_UPDATE, params)
        result = await _run(_SCOPE_MAPPER_UPDATE, params)
        written = _writes(mock)
    # Before approval clients MEHO cannot read are left to the run.
    assert call["status"] == "awaiting_approval", call
    _refused_on_run(result, "meho_access_not_checked")
    assert written == []


@pytest.mark.parametrize(
    ("op_id", "params", "written"),
    [
        (
            _MAPPER_CREATE,
            _mapper_create(_OTHER_UUID),
            f"POST /admin/realms/{_LOGIN_REALM}/clients/{_OTHER_UUID}/protocol-mappers/models",
        ),
        (
            _SCOPE_MAPPER_UPDATE,
            _scope_mapper_update(_OTHER_SCOPE_UUID),
            f"PUT /admin/realms/{_LOGIN_REALM}/client-scopes/{_OTHER_SCOPE_UUID}"
            f"/protocol-mappers/models/{_MAPPER_UUID}",
        ),
    ],
    ids=["new-mapper", "scope-mapper"],
)
async def test_a_mapper_write_on_another_client_runs(
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
            _MAPPER_CREATE,
            _mapper_create(),
            f"POST /admin/realms/{_OTHER_REALM}/clients/{_OWN_UUID}/protocol-mappers/models",
        ),
        (
            _SCOPE_MAPPER_UPDATE,
            _scope_mapper_update(),
            f"PUT /admin/realms/{_OTHER_REALM}/client-scopes/{_OWN_SCOPE_UUID}"
            f"/protocol-mappers/models/{_MAPPER_UUID}",
        ),
    ],
    ids=["new-mapper", "scope-mapper"],
)
async def test_the_mapper_writes_in_another_realm_are_not_refused(
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
# The refusal says where the change is made
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("op_id", "params", "words", "code"), _REFUSED, ids=_REFUSED_IDS)
async def test_the_refusal_names_the_keycloak_admin_console(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
    words: str,
    code: str,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _Keycloak(mock, _LOGIN_REALM)
        call = await _call(op_id, params)
        result = await _run(op_id, params)
    assert _WHERE in call["error"], call["error"]
    message = result["extras"]["exception_message"]
    # keycloak.realm.update can send a secret, so MEHO withholds its error text.
    assert "withheld" in message or _WHERE in message, message


async def test_a_refusal_when_meho_cannot_check_names_the_keycloak_admin_console(
    spelled_target: str,
) -> None:
    params = {"id": _OWN_UUID, "representation": {"enabled": False}}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _SPELLED_REALM)
        api.realm_read_fails = True
        result = await _run(_CLIENT_UPDATE, params, target=spelled_target)
    _refused_on_run(result, "meho_access_not_checked")
    assert _WHERE in result["extras"]["exception_message"]
