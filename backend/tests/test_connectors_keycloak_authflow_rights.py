# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The login flow ops check the rights of the target's account before they wait.

``keycloak.authflow.ensure_approval_flow``,
``keycloak.authflow.ensure_passkey_code_skip`` and
``keycloak.authflow.ensure_approval_touch`` run through the real dispatch
stack against a fake Keycloak (respx). No running Keycloak and no Vault.

What is proven, for each op:

* an account without ``manage-realm`` is refused before any approval
  request exists; the text names the role and the account. A missing
  ``manage-clients`` does not refuse: a Keycloak permission on the one
  client can replace it, so the card says MEHO could not check it;
* when the roles cannot be read, the call waits for approval and the card
  says so in one line; a 403 at the run stops with a text that names the
  roles;
* an account with the roles waits for approval as before; the card lists
  the roles;
* the admin token never reaches the card or the result;
* the card and the check of one request share one read of the roles;
* after a refusal, MEHO drops the admin token it keeps;
* a preview that does not talk to Keycloak says the check runs at the call;
* a 403 at the client lookup names the roles that let an account find a client;
* after a refusal, the role is given and the next call waits for approval.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_authflow_rights import (
    NO_RIGHTS_CHECK,
    RIGHTS_CHECKED_AT_CALL,
    AccountRights,
    _admin_client,
    _claims,
    _client_roles,
    read_account_rights,
    rights_text,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_FLOW_OP = "keycloak.authflow.ensure_approval_flow"
_PASSKEY_OP = "keycloak.authflow.ensure_passkey_code_skip"
_TOUCH_OP = "keycloak.authflow.ensure_approval_touch"
_ROLES = {
    _FLOW_OP: ["manage-realm", "manage-clients"],
    _PASSKEY_OP: ["manage-realm"],
    _TOUCH_OP: ["manage-realm", "manage-clients"],
}
_OPS = [_FLOW_OP, _PASSKEY_OP, _TOUCH_OP]
#: The ops that find a client first and bind a flow to it.
_CLIENT_OPS = [_FLOW_OP, _TOUCH_OP]
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-rights-test"
_KC_HOST = "keycloak-rights.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "example-realm"
_ADMIN_REALM = "master"
_ADMIN_CLIENT = f"{_REALM}-realm"
_ACCOUNT = "service-account-example-admin"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000006337")


def _operator() -> Operator:
    return Operator(
        sub="kc-rights-requester",
        name="Keycloak Rights Test Operator",
        email=None,
        raw_jwt="<kc-rights-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _creds_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="example-admin", client_secret="stub-secret")

    return _load()


def _b64(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _token(claims: dict[str, Any]) -> str:
    """A made-up JWT. The signature is never checked by the rights check."""
    return f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(claims)}.made-up-signature"


def _token_with(roles: list[str]) -> str:
    return _token(
        {
            "preferred_username": _ACCOUNT,
            "azp": "example-admin",
            "resource_access": {
                _ADMIN_CLIENT: {"roles": roles},
                "account": {"roles": ["view-profile"]},
            },
        }
    )


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
                fingerprint={"version": "26.4.0"},
                notes="seeded by test_connectors_keycloak_authflow_rights",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


def _mount(mock: respx.MockRouter, token: str, *, admin_status: int = 403) -> respx.Route:
    """Mount the token endpoint; every Admin REST API call answers *admin_status*."""
    mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
        200, json={"access_token": token, "expires_in": 300}
    )
    return mock.route(path__startswith="/admin/").respond(
        admin_status, json={"error": "HTTP 403 Forbidden"}
    )


async def _dispatch(op_id: str, *, approved: bool = False) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params={},
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


async def _approval_count() -> int:
    async with get_sessionmaker()() as session:
        count = await session.scalar(select(func.count()).select_from(ApprovalRequest))
    return int(count or 0)


async def _effect(result: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, uuid.UUID(result["extras"]["approval_request_id"]))
    assert row is not None
    effect: dict[str, Any] = row.proposed_effect
    return effect


# ---------------------------------------------------------------------------
# A role is missing: refused before the wait
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", _OPS)
async def test_account_without_manage_realm_is_refused_before_any_approval_request(
    kc: KeycloakConnector, op_id: str
) -> None:
    token = _token_with(["view-realm", "manage-clients"])
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        admin = _mount(mock, token)
        result = await _dispatch(op_id)

    assert result["status"] == "denied", result
    extras = result["extras"]
    assert extras["error_code"] == "write_capability_denied"
    assert extras["required"] == _ROLES[op_id]
    assert "manage-realm" not in extras["granted"]
    assert _ACCOUNT in extras["reason"]
    assert "manage-realm" in extras["reason"]
    assert _ADMIN_CLIENT in extras["reason"]
    assert extras["doc_ref"].startswith("docs/codebase/connectors-keycloak.md")
    assert "manage-realm" in result["error"]
    assert _ACCOUNT in result["error"]
    assert "vault" not in result["error"].lower()
    assert await _approval_count() == 0
    assert admin.call_count == 0
    assert token not in json.dumps(result)
    assert "try again after that" in result["error"]
    # The kept admin token is dropped, so the next call signs in again.
    assert kc._admin_tokens == {}


@pytest.mark.parametrize("op_id", _CLIENT_OPS)
async def test_client_op_without_manage_clients_still_waits(
    kc: KeycloakConnector, op_id: str
) -> None:
    # A Keycloak permission to manage only the one client can replace
    # manage-clients, and the token does not show it. So no refusal.
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        admin = _mount(mock, _token_with(["manage-realm", "view-clients"]))
        result = await _dispatch(op_id)

    assert result["status"] == "awaiting_approval", result
    assert admin.call_count == 0
    effect = await _effect(result)
    preflight = effect["permission_preflight"]
    assert preflight["will_be_denied"] is False
    assert preflight["granted"] == ["manage-realm"]
    assert "reason" not in preflight
    check = effect["preview"]["rights_check"]
    assert check.startswith(
        f"MEHO checked the account {_ACCOUNT!r}: it has the role(s) manage-realm"
    )
    assert "lacks the role(s) manage-clients" in check
    assert "could not check that permission" in check


async def test_flow_op_without_both_roles_names_only_manage_realm(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount(mock, _token_with(["view-realm"]))
        result = await _dispatch(_FLOW_OP)
    assert result["status"] == "denied", result
    assert "lacks the role(s) manage-realm of" in result["extras"]["reason"]
    assert "manage-clients" not in result["extras"]["reason"]
    assert await _approval_count() == 0


# ---------------------------------------------------------------------------
# The roles cannot be read: the card says so, a 403 at the run is clear
# ---------------------------------------------------------------------------


_UNREADABLE = {
    "not a JWT": "kc-admin-token",
    "no resource_access claim": _token({"preferred_username": _ACCOUNT}),
    "payload is not JSON": "aGVhZGVy.bm90LWpzb24.sig",
}


@pytest.mark.parametrize("token", list(_UNREADABLE.values()), ids=list(_UNREADABLE))
@pytest.mark.parametrize("op_id", _OPS)
async def test_unreadable_roles_wait_for_approval_and_the_card_says_so(
    kc: KeycloakConnector, op_id: str, token: str
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        admin = _mount(mock, token)
        result = await _dispatch(op_id)

    assert result["status"] == "awaiting_approval", result
    assert admin.call_count == 0
    effect = await _effect(result)
    card = effect["preview"]
    assert card["rights_check"] == NO_RIGHTS_CHECK
    assert card["roles_needed"] == _ROLES[op_id]
    assert "permission_preflight" not in effect
    assert token not in json.dumps(effect)


@pytest.mark.parametrize("op_id", _OPS)
async def test_403_at_the_run_names_the_roles(kc: KeycloakConnector, op_id: str) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount(mock, "kc-admin-token")
        result = await _dispatch(op_id, approved=True)

    assert result["status"] == "error", result
    extras = result["extras"]
    assert extras["http_status"] == 403
    assert extras["exception_class"] == "KeycloakMissingRightsError"
    assert "HTTP 403" in extras["exception_message"]
    for role in _ROLES[op_id]:
        assert role in extras["remediation"]
    assert op_id in extras["remediation"]
    assert _ADMIN_CLIENT in extras["remediation"]
    assert "try again after that" in extras["remediation"]
    assert kc._admin_tokens == {}


async def test_403_at_the_run_names_the_missing_role_and_account(
    kc: KeycloakConnector,
) -> None:
    # Roles changed between the approval and the run.
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount(mock, _token_with(["view-realm"]))
        result = await _dispatch(_PASSKEY_OP, approved=True)
    remediation = result["extras"]["remediation"]
    assert f"on the account {_ACCOUNT!r}" in remediation
    assert "The account lacks: manage-realm." in remediation


@pytest.mark.parametrize("op_id", _OPS)
async def test_other_errors_at_the_run_are_unchanged(kc: KeycloakConnector, op_id: str) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount(mock, "kc-admin-token", admin_status=404)
        result = await _dispatch(op_id, approved=True)
    assert result["status"] == "error", result
    assert "remediation" not in result["extras"]
    assert result["extras"]["exception_class"] != "KeycloakMissingRightsError"


# ---------------------------------------------------------------------------
# The account has the roles: waits for approval as before
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", _OPS)
async def test_account_with_the_roles_waits_for_approval(kc: KeycloakConnector, op_id: str) -> None:
    token = _token_with(["view-realm", "manage-realm", "manage-clients"])
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        admin = _mount(mock, token)
        result = await _dispatch(op_id)

    assert result["status"] == "awaiting_approval", result
    assert admin.call_count == 0
    effect = await _effect(result)
    card = effect["preview"]
    assert card["roles_needed"] == _ROLES[op_id]
    assert card["rights_check"].startswith(f"MEHO checked the account {_ACCOUNT!r}")
    assert card["realm"] == _REALM
    preflight = effect["permission_preflight"]
    assert preflight["will_be_denied"] is False
    assert preflight["granted"] == sorted(_ROLES[op_id])
    assert preflight["account"] == _ACCOUNT
    assert token not in json.dumps(effect)


async def test_touch_op_card_names_the_client(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount(mock, _token_with(["manage-realm", "manage-clients"]))
        result = await _dispatch(_TOUCH_OP)
    card = (await _effect(result))["preview"]
    assert card["client_id"] == "meho-web"
    assert card["new_flow"] == "meho approval touch"
    assert card["client_lookup_needs_one_of"] == [
        "query-clients",
        "view-clients",
        "manage-clients",
    ]


async def test_flow_op_card_names_the_client(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount(mock, _token_with(["manage-realm", "manage-clients"]))
        result = await _dispatch(_FLOW_OP)
    card = (await _effect(result))["preview"]
    assert card["client_id"] == "meho-cli-approve"
    assert card["flow"] == "meho approval sign-in"
    assert card["client_lookup_needs_one_of"] == [
        "query-clients",
        "view-clients",
        "manage-clients",
    ]


async def test_wrong_password_makes_one_failed_sign_in(kc: KeycloakConnector) -> None:
    # The card and the check share one read, so one sign-in, not two.
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        sign_in = mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            401, json={"error": "invalid_client"}
        )
        result = await _dispatch(_FLOW_OP)
    assert result["status"] == "awaiting_approval", result
    assert sign_in.call_count == 1
    assert (await _effect(result))["preview"]["rights_check"] == NO_RIGHTS_CHECK


@pytest.mark.parametrize("op_id", _OPS)
async def test_preview_says_the_check_runs_at_the_call(kc: KeycloakConnector, op_id: str) -> None:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        sign_in = mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": _token_with([]), "expires_in": 300}
        )
        preview = await preview_dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=target,
            params={},
        )
    assert preview["status"] == "ok", preview
    card = preview["proposed_effect"]["preview"]
    assert card["rights_check"] == RIGHTS_CHECKED_AT_CALL
    assert card["roles_needed"] == _ROLES[op_id]
    assert sign_in.call_count == 0


@pytest.mark.parametrize("op_id", _CLIENT_OPS)
async def test_403_at_the_client_lookup_names_the_lookup_roles(
    kc: KeycloakConnector, op_id: str
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        admin = _mount(mock, _token_with(["manage-realm", "manage-clients"]))
        result = await _dispatch(op_id, approved=True)

    assert admin.call_count == 1
    assert admin.calls.last.request.url.path == f"/admin/realms/{_REALM}/clients"
    extras = result["extras"]
    assert extras["exception_class"] == "KeycloakMissingRightsError"
    assert "to find the client" in extras["exception_message"]
    assert "Nothing was changed." in extras["exception_message"]
    remediation = extras["remediation"]
    assert remediation.startswith("Keycloak refused (HTTP 403) to find the client")
    assert "one of the roles query-clients, view-clients, manage-clients" in remediation
    assert "Nothing was changed." in remediation
    assert "try again after that" in remediation
    assert kc._admin_tokens == {}


@pytest.mark.parametrize("op_id", _OPS)
async def test_refused_then_role_given_then_the_call_waits(
    kc: KeycloakConnector, op_id: str
) -> None:
    tokens = iter(
        [
            _token_with(["view-realm", "manage-clients"]),
            _token_with(["view-realm", "manage-realm", "manage-clients"]),
        ]
    )
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        sign_in = mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").mock(
            side_effect=lambda _: httpx.Response(
                200, json={"access_token": next(tokens), "expires_in": 300}
            )
        )
        admin = mock.route(path__startswith="/admin/").respond(403)
        refused = await _dispatch(op_id)
        # A person gives the account manage-realm in Keycloak; MEHO signs in again.
        waits = await _dispatch(op_id)

    assert refused["status"] == "denied", refused
    assert "manage-realm" in refused["extras"]["reason"]
    assert waits["status"] == "awaiting_approval", waits
    assert sign_in.call_count == 2
    assert admin.call_count == 0
    assert await _approval_count() == 1
    preflight = (await _effect(waits))["permission_preflight"]
    assert preflight["will_be_denied"] is False
    assert preflight["granted"] == sorted(_ROLES[op_id])


async def test_read_account_rights_never_raises(
    kc: KeycloakConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_target: Any) -> Any:
        raise RuntimeError("bad realm settings")

    monkeypatch.setattr(
        "meho_backplane.connectors.keycloak.ops_authflow_rights.resolve_realm_config", broken
    )
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    rights = await read_account_rights(kc, _operator(), target)
    assert rights == AccountRights(account=None, admin_realm=None, client=None, roles=None)
    text = rights_text(_PASSKEY_OP, ("manage-realm",), rights)
    assert "needs the role(s) manage-realm for this realm on the account of the target." in text


# ---------------------------------------------------------------------------
# Where the roles live
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("admin_realm", "managed_realm", "client"),
    [
        ("master", "example-realm", "example-realm-realm"),
        ("master", "master", "master-realm"),
        ("example-realm", "example-realm", "realm-management"),
        ("other-realm", "example-realm", None),
    ],
)
def test_admin_client(admin_realm: str, managed_realm: str, client: str | None) -> None:
    assert _admin_client(admin_realm, managed_realm) == client


def test_client_roles_edge_cases() -> None:
    assert _client_roles({}, "realm-management") is None
    # No entry for the admin client: not checked, not "no roles".
    assert _client_roles({"resource_access": {}}, "realm-management") is None
    assert _client_roles({"resource_access": {"account": {}}}, "realm-management") is None
    assert _client_roles({"resource_access": {"realm-management": {}}}, "realm-management") is None
    assert _client_roles(
        {"resource_access": {"realm-management": {"roles": ["manage-realm", 7]}}},
        "realm-management",
    ) == frozenset({"manage-realm"})


def test_claims_of_a_bad_token() -> None:
    assert _claims("") is None
    assert _claims("a.b") is None
    assert _claims("a.!!!.c") is None
    assert _claims(f"x.{base64.urlsafe_b64encode(b'[1]').decode()}.y") is None


def test_rights_text_for_a_third_realm_names_no_client() -> None:
    rights = AccountRights(account=None, admin_realm="other-realm", client=None, roles=None)
    text = rights_text(_PASSKEY_OP, ("manage-realm",), rights)
    assert "signs in to the realm 'other-realm'" in text
    assert "on the account of the target" in text
