# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An admin's change of rights in the web console runs at once.

``keycloak.group.member.add`` goes through the full dispatch stack against
a respx-mocked Keycloak Admin REST API:

* an admin (tenant admin or platform admin) in the web console: it runs at
  once, no request waits, and the audit row says it ran at once from the
  web console;
* the same admin with a token, an operator in the console, an agent, and a
  hosted agent run: the call waits;
* a tenant admin who is not a platform admin adds a person to the platform
  admin group: refused with the same text as the approve, nothing written;
* a group MEHO cannot read counts as the strict case: refused for a tenant
  admin.

The gate keeps the wait for a change of rights that reads, writes or makes
a credential, for one whose params carry a credential (a client secret, a
user's credentials, a password), for a ``destructive`` one, for one with the "always wait"
mark, and for a ``secret.move`` with a
Keycloak side. The policy evaluator gives the same answers.

Fixture values are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.operations.rights_change_console import (
    CONSOLE_RUN_AUDIT_VALUE,
    runs_at_once_in_console,
)
from meho_backplane.policy.evaluate import Asked, Asker, Lookups, Verdict, evaluate
from meho_backplane.policy.shadow import asked_of
from meho_backplane.targets.resolver import resolve_target

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "keycloak-console-rights"
_KC_HOST = "keycloak-console-rights.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "meho"
_ADD = "keycloak.group.member.add"
_GROUP_UUID = "77777777-7777-7777-7777-777777777777"
_USER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c7")
_LOGIN_REALMS = "meho_backplane.connectors.keycloak.ops_write_grant._backplane_login_realms"
_PARAMS: dict[str, Any] = {"group_id": _GROUP_UUID, "user_id": _USER_UUID}


def _person(
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    web: bool = True,
    platform_admin: bool = False,
    kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
    return Operator(
        sub="console-rights-admin",
        raw_jwt="<console-rights-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=role,
        principal_kind=kind,
        platform_admin=platform_admin,
        web_session=web,
    )


def _stub_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


async def _seed_keycloak_target() -> None:
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
                secret_ref="example-site/keycloak/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _REALM},
                fingerprint={"version": "26.0.5"},
                notes="seeded by test_rights_change_console_run",
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    get_settings.cache_clear()


@pytest.fixture
async def keycloak() -> AsyncIterator[KeycloakConnector]:
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    await _seed_keycloak_target()
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    instance = KeycloakConnector(credentials_loader=_stub_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = instance
    yield instance
    await instance.aclose()


def _mount(mock: respx.MockRouter, *, group: dict[str, Any] | None) -> respx.Route:
    """Keycloak answers; *group* is the group read, ``None`` for a failed read. Returns the PUT."""
    mock.post("/realms/master/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token-console", "expires_in": 300}
    )
    mock.get(f"/admin/realms/{_REALM}").respond(200, json={"realm": _REALM})
    group_path = f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}"
    if group is None:
        mock.get(group_path).respond(500, json={"error": "unknown_error"})
    else:
        mock.get(group_path).respond(200, json={"id": _GROUP_UUID, **group})
    mock.get(f"{group_path}/members").respond(200, json=[])
    mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(
        200, json={"id": _USER_UUID, "username": "operator-a"}
    )
    mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}/groups").respond(200, json=[])
    return mock.put(f"/admin/realms/{_REALM}/users/{_USER_UUID}/groups/{_GROUP_UUID}").respond(204)


_PLAIN_GROUP = {"name": "role-tenant", "path": "/role-tenant", "attributes": {}}
_PLATFORM_GROUP = {"name": "MEHO-Platform-Admins", "path": "/MEHO-Platform-Admins"}


async def _add(operator: Operator) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=operator, connector_id=_CONNECTOR_ID, op_id=_ADD, target=target, params=_PARAMS
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


async def _run_rows() -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog.payload))).scalars().all()
    return [dict(row) for row in rows if row.get("rights_change_run") is not None]


# -- runs at once ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operator",
    [_person(), _person(platform_admin=True)],
    ids=["tenant-admin", "platform-admin"],
)
async def test_an_admin_in_the_console_adds_a_member_at_once(
    keycloak: KeycloakConnector, operator: Operator
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount(mock, group=_PLAIN_GROUP)
        result = await _add(operator)

    assert result["status"] == "ok", result
    assert put.called
    assert await _requests() == []
    (row,) = await _run_rows()
    assert row["rights_change_run"] == CONSOLE_RUN_AUDIT_VALUE == "admin_in_web_console"


@pytest.mark.asyncio
async def test_a_platform_admin_adds_a_member_to_the_platform_admin_group_at_once(
    keycloak: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount(mock, group=_PLATFORM_GROUP)
        result = await _add(_person(platform_admin=True))

    assert result["status"] == "ok", result
    assert put.called
    assert await _requests() == []


# -- waits ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operator",
    [
        _person(web=False),
        _person(role=TenantRole.OPERATOR),
        _person(kind=PrincipalKind.AGENT, web=False),
    ],
    ids=["admin-with-a-token", "operator-in-the-console", "agent"],
)
async def test_every_other_call_waits(keycloak: KeycloakConnector, operator: Operator) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount(mock, group=_PLAIN_GROUP)
        result = await _add(operator)

    assert result["status"] == "awaiting_approval", result
    assert not put.called
    (request,) = await _requests()
    assert request.rights_change is True
    assert await _run_rows() == []


@pytest.mark.asyncio
async def test_a_hosted_agent_run_of_an_admin_waits(keycloak: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount(mock, group=_PLAIN_GROUP)
        with actor_delegation("agent:helper"):
            result = await _add(_person())

    assert result["status"] in {"awaiting_approval", "denied"}, result
    assert not put.called
    assert await _run_rows() == []


# -- refused ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tenant_admin_cannot_add_a_member_to_the_platform_admin_group(
    keycloak: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount(mock, group=_PLATFORM_GROUP)
        result = await _add(_person())

    assert result["status"] == "denied", result
    assert "Only a platform admin may approve it" in result["error"]
    assert not put.called
    assert await _requests() == []
    assert await _run_rows() == []


@pytest.mark.asyncio
async def test_a_group_that_cannot_be_read_counts_as_the_strict_case(
    keycloak: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount(mock, group=None)
        result = await _add(_person())

    assert result["status"] == "denied", result
    assert "platform admin" in result["error"]
    assert not put.called


# -- the gate keeps the wait ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id", ["keycloak.user.reset_password", "keycloak.user.create", "keycloak.realm.update"]
)
async def test_a_change_of_rights_that_writes_a_credential_waits(
    keycloak: KeycloakConnector, op_id: str
) -> None:
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT_ID,
        product="keycloak",
        version="26.x",
        impl_id="keycloak-admin",
        op_id=op_id,
    )
    assert descriptor is not None

    verdict, _reason = await policy_gate(
        operator=_person(), descriptor=descriptor, target=None, connector_id=_CONNECTOR_ID
    )

    assert verdict.value == "needs-approval"
    assert runs_at_once_in_console(_person(), descriptor, params={}) is False


@pytest.mark.asyncio
async def test_an_always_wait_change_of_rights_waits_for_an_admin_in_the_console(
    keycloak: KeycloakConnector,
) -> None:
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT_ID,
        product="keycloak",
        version="26.x",
        impl_id="keycloak-admin",
        op_id=_ADD,
    )
    assert descriptor is not None
    descriptor.policy_always_wait = True

    verdict, _reason = await policy_gate(
        operator=_person(), descriptor=descriptor, target=None, connector_id=_CONNECTOR_ID
    )

    assert verdict.value == "needs-approval"
    assert runs_at_once_in_console(_person(), descriptor, params={}) is False


def test_a_destructive_or_always_wait_change_of_rights_and_a_secret_move_wait() -> None:
    destructive = EndpointDescriptor(op_id=_ADD, safety_level="destructive", parameter_schema={})
    move = EndpointDescriptor(op_id="secret.move", safety_level="dangerous", parameter_schema={})
    plain = EndpointDescriptor(op_id=_ADD, safety_level="dangerous", parameter_schema={})
    marked = EndpointDescriptor(op_id=_ADD, safety_level="dangerous", parameter_schema={})
    marked.policy_always_wait = True

    assert runs_at_once_in_console(_person(), destructive, params=_PARAMS) is False
    assert runs_at_once_in_console(_person(), marked, params=_PARAMS) is False
    assert runs_at_once_in_console(_person(), move, params=_PARAMS) is False
    assert runs_at_once_in_console(_person(), plain, params=_PARAMS) is True
    assert runs_at_once_in_console(_person(web=False), plain, params=_PARAMS) is False


# -- the policy evaluator gives the same answers ---------------------------------


def _asker(*, via: str = "web", kind: str = "person", roles: set[str] | None = None) -> Asker:
    return Asker(
        kind=kind,  # type: ignore[arg-type]
        sub="console-rights-admin",
        tenant_id=_TENANT_ID,
        roles=frozenset(roles if roles is not None else {"tenant_admin"}),
        via=via,  # type: ignore[arg-type]
    )


def _asked(*marks: str, risk: str = "dangerous") -> Asked:
    return Asked(
        connector_id=_CONNECTOR_ID,
        op_id=_ADD,
        risk=risk,
        always_wait=True,
        mutating=True,
        op_class=frozenset({"per_call_approval", *marks}),
    )


@pytest.mark.parametrize(
    ("asker", "asked", "found", "verdict"),
    [
        (_asker(), _asked("rights_change"), Lookups(), Verdict.RUN),
        (
            _asker(),
            _asked("rights_change"),
            Lookups(rights_change_refusal="Only a platform admin may approve it"),
            Verdict.DENY,
        ),
        (_asker(via="token"), _asked("rights_change"), Lookups(), Verdict.WAIT),
        (_asker(roles={"operator"}), _asked("rights_change"), Lookups(), Verdict.WAIT),
        (
            _asker(roles={"operator", "platform_admin"}),
            _asked("rights_change"),
            Lookups(),
            Verdict.RUN,
        ),
        (_asker(kind="agent", via="token"), _asked("rights_change"), Lookups(), Verdict.WAIT),
        (_asker(), _asked("rights_change", "credential_waits"), Lookups(), Verdict.WAIT),
        (_asker(), _asked("rights_change", risk="destructive"), Lookups(), Verdict.WAIT),
        (_asker(), _asked("rights_change", "classified_always_wait"), Lookups(), Verdict.WAIT),
        (_asker(), _asked(), Lookups(), Verdict.WAIT),
    ],
    ids=[
        "admin-web",
        "admin-web-refused",
        "admin-token",
        "operator-web",
        "platform-admin-web",
        "agent",
        "credential",
        "destructive",
        "always-wait",
        "not-a-rights-change",
    ],
)
def test_the_evaluator_gives_the_same_answer(
    asker: Asker, asked: Asked, found: Lookups, verdict: Verdict
) -> None:
    assert evaluate(asker, asked, found).verdict is verdict


_CLIENT_SECRET = {"representation": {"clientId": "console-client", "secret": "<made-up>"}}
_REALM_WITH_SECRETS = {
    "representation": {
        "realm": "console-new",
        "clients": [{"clientId": "console-client", "secret": "<made-up>"}],
    }
}
_USER_WITH_CREDENTIALS = {"representation": {"credentials": [{"type": "password"}]}}
_IDP_SECRET = {"representation": {"identityProviders": [{"config": {"clientSecret": "<made-up>"}}]}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("keycloak.client.update", _CLIENT_SECRET),
        ("keycloak.client.create", _CLIENT_SECRET),
        ("keycloak.realm.create", _REALM_WITH_SECRETS),
        ("keycloak.realm.create", _IDP_SECRET),
        ("keycloak.user.update", _USER_WITH_CREDENTIALS),
    ],
    ids=["client-update", "client-create", "realm-client", "realm-idp", "user"],
)
async def test_a_change_of_rights_whose_params_carry_a_credential_waits(
    keycloak: KeycloakConnector, op_id: str, params: dict[str, Any]
) -> None:
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT_ID,
        product="keycloak",
        version="26.x",
        impl_id="keycloak-admin",
        op_id=op_id,
    )
    assert descriptor is not None

    verdict, _reason = await policy_gate(
        operator=_person(),
        descriptor=descriptor,
        target=None,
        connector_id=_CONNECTOR_ID,
        params=params,
    )

    assert verdict.value == "needs-approval"
    assert runs_at_once_in_console(_person(), descriptor, params=params) is False
    asked = asked_of(descriptor, connector_id=_CONNECTOR_ID, target=None, params=params)
    assert "credential_waits" in asked.op_class
    assert evaluate(_asker(), asked, Lookups()).verdict is Verdict.WAIT


@pytest.mark.asyncio
async def test_a_client_update_without_a_credential_runs_at_once_in_the_console(
    keycloak: KeycloakConnector,
) -> None:
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT_ID,
        product="keycloak",
        version="26.x",
        impl_id="keycloak-admin",
        op_id="keycloak.client.update",
    )
    assert descriptor is not None
    params = {"client_id": "console-client", "representation": {"redirectUris": []}}

    assert runs_at_once_in_console(_person(), descriptor, params=params) is True
    asked = asked_of(descriptor, connector_id=_CONNECTOR_ID, target=None, params=params)
    assert "credential_waits" not in asked.op_class
    assert evaluate(_asker(), asked, Lookups()).verdict is Verdict.RUN
