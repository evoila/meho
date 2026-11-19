# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A ``secret.move`` with a ``keycloak:`` side is a change of rights, bound to its Keycloak target.

* A move to a ``keycloak:`` user password, or from a ``keycloak:`` client
  secret, is a change of rights: the requester never approves it, a
  reviewer who is not an admin cannot approve it, and another tenant admin
  can. A move between two Vault places is not a change of rights.
* The card a person reads names, for each ``keycloak:`` side, the Keycloak
  target it points to (id and name), its host and its secret reference.
  The agent's views leave these fields out.
* The approved run compares the target id, host and secret reference with
  the target it finds. Another target, host or secret reference, or a card
  without one of them, refuses the run before any Keycloak login, and
  nothing is written. With no change, the approved run works.

The Vault side is faked at the ``_build_client`` seam and Keycloak with
respx; the targets and the approval rows are real rows.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from sqlalchemy import select, update

import meho_backplane.connectors.secret  # noqa: F401 -- registers the secret kinds
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import proposed_effect_for_agent
from meho_backplane.operations.approval_queue import (
    RightsChangeApprovalError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.grant_approval import is_rights_change
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# Made-up values for the tests only.
_SECRET_VALUE = "made-up-moved-value-4711"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c7")
_REALM = "example-realm"
_USERNAME = "operator-a"
_USER_UUID = "44444444-4444-4444-4444-444444444444"
_CLIENT_ID = "launcher-app"
_CLIENT_UUID = "55555555-5555-5555-5555-555555555555"
_HOST_A = "kc-a.test.invalid"
_HOST_B = "kc-b.test.invalid"
_HOST_C = "kc-c.test.invalid"
#: The alias the moves name. It points to ``kc-a`` first.
_ALIAS = "idp"

_VAULT_PLACE = f"vault:tenants/{_TENANT_ID}/app#password"
_KC_PASSWORD = f"keycloak:{_ALIAS}/{_REALM}/{_USERNAME}#password"
_KC_CLIENT = f"keycloak:{_ALIAS}/{_REALM}/clients/{_CLIENT_ID}#secret"
_TO_KEYCLOAK = {"from": _VAULT_PLACE, "to": _KC_PASSWORD}
_FROM_KEYCLOAK = {"from": _KC_CLIENT, "to": _VAULT_PLACE}


def _person(
    sub: str,
    *,
    role: TenantRole = TenantRole.OPERATOR,
    sa_operator: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT_ID,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
        self_approver=sa_operator,
    )


#: The requester is a tenant admin, so only the rights-change rule stops them.
_REQUESTER = _person("kc-requester", role=TenantRole.TENANT_ADMIN)
#: A reviewer who approves a dangerous change of another person, but is not an admin.
_PLAIN_REVIEWER = _person("kc-reviewer", sa_operator=True)
_OTHER_ADMIN = _person("kc-other-admin", role=TenantRole.TENANT_ADMIN)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "operator-role")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


def _stub_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="made-up-admin")

    return _load()


async def _seed_keycloak(name: str, host: str, aliases: list[str]) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=name,
                aliases=aliases,
                product="keycloak",
                host=host,
                port=443,
                fqdn=None,
                secret_ref=f"example/{name}/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _REALM},
                fingerprint={"version": "26.0.5"},
                notes="seeded by test_secret_move_keycloak_side",
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def _env(_settings_env: None) -> AsyncIterator[None]:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    set_default_reducer(PassThroughReducer())
    await register_secret_broker_operations(embedding_service=embedding)
    await _seed_keycloak("kc-a", _HOST_A, [_ALIAS])
    await _seed_keycloak("kc-b", _HOST_B, [])
    connector = KeycloakConnector(credentials_loader=_stub_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield
    await connector.aclose()
    _CONNECTOR_INSTANCE_CACHE.pop(KeycloakConnector, None)


class _Keycloak:
    """Fake Keycloak Admin REST on every seeded host, so a call to any of them is seen."""

    def __init__(self, mock: respx.MockRouter) -> None:
        self.mock = mock
        self.puts: list[str] = []
        for host in (_HOST_A, _HOST_B, _HOST_C):
            base = f"https://{host}"
            mock.post(f"{base}/realms/master/protocol/openid-connect/token").respond(
                200, json={"access_token": "made-up-admin-token", "expires_in": 300}
            )
            mock.get(f"{base}/admin/realms/{_REALM}/users").respond(
                200, json=[{"id": _USER_UUID, "username": _USERNAME, "enabled": True}]
            )
            mock.put(
                f"{base}/admin/realms/{_REALM}/users/{_USER_UUID}/reset-password",
                name=f"put-{host}",
            ).respond(204)
            mock.get(f"{base}/admin/realms/{_REALM}/clients").respond(
                200, json=[{"id": _CLIENT_UUID, "clientId": _CLIENT_ID, "enabled": True}]
            )
            mock.get(f"{base}/admin/realms/{_REALM}/clients/{_CLIENT_UUID}/client-secret").respond(
                200, json={"type": "secret", "value": _SECRET_VALUE}
            )

    def hosts_called(self) -> set[str]:
        return {call.request.url.host for call in self.mock.calls}


@pytest.fixture
def keycloak() -> Iterator[_Keycloak]:
    with respx.mock(assert_all_called=False) as mock:
        yield _Keycloak(mock)


async def _ask(params: dict[str, Any]) -> ApprovalRequest:
    """Send the move without approval; return the approval row it waits on."""
    result = await dispatch(
        operator=_REQUESTER,
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params=params,
    )
    assert result.status == "awaiting_approval", result
    async with get_sessionmaker()() as session:
        (row,) = (await session.execute(select(ApprovalRequest))).scalars().all()
    return row


async def _approve(request_id: uuid.UUID, operator: Operator) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=operator)
        await session.commit()
        return request


async def _status(request_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row.status


async def _approve_and_run(request_id: uuid.UUID) -> OperationResult:
    request = await _approve(request_id, _OTHER_ADMIN)
    result: OperationResult = await resume_dispatch_after_approval(
        operator=_OTHER_ADMIN, request=request, params=None
    )
    return result


async def _update(name: str, **values: Any) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(update(TargetORM).where(TargetORM.name == name).values(**values))
        await session.commit()


async def _target_id(name: str) -> str:
    async with get_sessionmaker()() as session:
        found = await session.execute(select(TargetORM.id).where(TargetORM.name == name))
        return str(found.scalar_one())


def _card(row: ApprovalRequest) -> dict[str, Any]:
    return dict(dict(row.proposed_effect)["preview"])


def _side_of(params: dict[str, Any]) -> str:
    return "sink" if params is _TO_KEYCLOAK else "source"


def _message(result: OperationResult) -> str:
    return str((result.extras or {}).get("exception_message"))


_MOVES = pytest.mark.parametrize(
    "params", [_TO_KEYCLOAK, _FROM_KEYCLOAK], ids=["to-keycloak-password", "from-client-secret"]
)


# -- a change of rights --------------------------------------------------


@_MOVES
async def test_a_move_with_a_keycloak_side_is_a_change_of_rights(
    params: dict[str, Any], monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask(params)
    assert row.rights_change is True
    assert is_rights_change(row)

    with pytest.raises(RightsChangeApprovalError):
        await _approve(row.id, _REQUESTER)
    with pytest.raises(RightsChangeApprovalError):
        await _approve(row.id, _PLAIN_REVIEWER)
    assert await _status(row.id) == ApprovalRequestStatus.PENDING.value

    approved = await _approve(row.id, _OTHER_ADMIN)
    assert approved.status == ApprovalRequestStatus.APPROVED.value
    assert keycloak.hosts_called() == set()  # parking and approving call no Keycloak


async def test_a_move_between_two_vault_places_is_not_a_change_of_rights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask({"from": _VAULT_PLACE, "to": f"vault:tenants/{_TENANT_ID}/other#password"})

    assert not row.rights_change
    assert not is_rights_change(row)
    approved = await _approve(row.id, _PLAIN_REVIEWER)
    assert approved.status == ApprovalRequestStatus.APPROVED.value


# -- what the card shows -------------------------------------------------


@_MOVES
async def test_the_card_names_the_keycloak_target_its_host_and_its_secret_ref(
    params: dict[str, Any], monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    row = await _ask(params)
    side = _card(row)[_side_of(params)]

    assert side["kind"] == "keycloak"
    assert side["target_id"] == await _target_id("kc-a")
    assert side["target_name"] == "kc-a"
    assert side["host"] == _HOST_A
    assert side["port"] == "443"
    assert side["scheme"] == "https"
    assert side["tls"] == "checked"
    assert side["secret_ref"] == "example/kc-a/admin"
    assert _SECRET_VALUE not in json.dumps(row.proposed_effect)
    # The agent's view of the stored request leaves the target fields out.
    shown = proposed_effect_for_agent(
        "secret.move",
        dict(row.proposed_effect),
        params=dict(row.params or {}),
        connector_id="secret-broker-1.x",
        tenant=str(_TENANT_ID),
    )
    assert shown is not None
    agent_side = shown["preview"][_side_of(params)]
    card_keys = {"target_id", "target_name", "host", "port", "scheme", "tls", "secret_ref"}
    assert card_keys.isdisjoint(agent_side)
    assert agent_side["kind"] == "keycloak"


async def test_a_name_that_finds_no_target_gets_no_target_on_the_card(
    monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    row = await _ask(
        {"from": _VAULT_PLACE, "to": f"keycloak:nowhere/{_REALM}/{_USERNAME}#password"}
    )

    assert "target_id" not in _card(row)["sink"]


# -- the approved run checks the target ----------------------------------


@_MOVES
async def test_with_no_change_the_approved_move_runs(
    params: dict[str, Any], monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask(params)

    result = await _approve_and_run(row.id)

    assert result.status == "ok", result
    assert keycloak.hosts_called() == {_HOST_A}
    if params is _TO_KEYCLOAK:
        assert keycloak.mock.routes[f"put-{_HOST_A}"].called
    else:
        assert fake.secrets.kv.v2.put_calls  # the client secret reached Vault


def _assert_refused(result: OperationResult, says: str, fake: Any, keycloak: _Keycloak) -> None:
    assert result.status == "error", result
    assert result.error == "connector_error: KeycloakTargetChangedError"
    message = _message(result)
    assert says in message
    assert "Nothing was read or written. Send a new request" in message
    assert _SECRET_VALUE not in message
    assert keycloak.hosts_called() == set()  # no Keycloak login, no Keycloak call
    assert fake.secrets.kv.v2.put_calls == []  # nothing written to Vault


@_MOVES
async def test_a_target_moved_to_another_host_refuses_the_run(
    params: dict[str, Any], monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask(params)
    await _update("kc-a", host=_HOST_C)

    result = await _approve_and_run(row.id)

    _assert_refused(result, "the host of the", fake, keycloak)
    assert _HOST_C in _message(result)


@_MOVES
async def test_a_new_port_on_the_same_target_refuses_the_run(
    params: dict[str, Any], monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask(params)
    await _update("kc-a", port=8080)

    result = await _approve_and_run(row.id)

    _assert_refused(result, "the port of the", fake, keycloak)
    assert "'8080'" in _message(result)


@_MOVES
async def test_plain_http_on_the_same_target_refuses_the_run(
    params: dict[str, Any], monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask(params)
    # The stored port stays 443, so only the scheme changes.
    await _update("kc-a", extras={"managed_realm": _REALM, "scheme": "http"})

    result = await _approve_and_run(row.id)

    _assert_refused(result, "the scheme (http or https) of the", fake, keycloak)
    assert "'http'" in _message(result)


@_MOVES
async def test_an_unknown_scheme_on_the_same_target_refuses_the_run(
    params: dict[str, Any], monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask(params)
    await _update("kc-a", extras={"managed_realm": _REALM, "scheme": "ftp"})

    result = await _approve_and_run(row.id)

    _assert_refused(result, "is not http or https", fake, keycloak)


@_MOVES
@pytest.mark.parametrize(
    ("values", "now"),
    [
        ({"verify_tls": False}, "not checked"),
        ({"tls_server_name": "other.test.invalid"}, "server name other.test.invalid"),
        ({"tls_ca_pin": "-----BEGIN CERTIFICATE-----\nmade-up\n"}, "pinned CA"),
    ],
)
async def test_a_new_tls_setting_on_the_same_target_refuses_the_run(
    params: dict[str, Any],
    values: dict[str, Any],
    now: str,
    monkeypatch: pytest.MonkeyPatch,
    keycloak: _Keycloak,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask(params)
    await _update("kc-a", **values)

    result = await _approve_and_run(row.id)

    _assert_refused(result, "the TLS check of the", fake, keycloak)
    assert now in _message(result)


@_MOVES
async def test_a_new_tls_server_name_refuses_the_run_also_without_a_certificate_check(
    params: dict[str, Any], monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    await _update("kc-a", verify_tls=False)
    row = await _ask(params)
    await _update("kc-a", tls_server_name="other.test.invalid")

    result = await _approve_and_run(row.id)

    _assert_refused(result, "the TLS check of the", fake, keycloak)
    assert "not checked, server name other.test.invalid" in _message(result)


@_MOVES
async def test_an_alias_moved_to_another_keycloak_target_refuses_the_run(
    params: dict[str, Any], monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask(params)
    await _update("kc-a", aliases=[])
    await _update("kc-b", aliases=[_ALIAS])

    result = await _approve_and_run(row.id)

    _assert_refused(result, "now points to Keycloak target 'kc-b'", fake, keycloak)


@_MOVES
async def test_a_new_secret_ref_on_the_same_target_refuses_the_run(
    params: dict[str, Any], monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask(params)
    await _update("kc-a", secret_ref="example/other/admin")

    result = await _approve_and_run(row.id)

    _assert_refused(result, "the admin login reference of the", fake, keycloak)
    assert "example/other/admin" not in _message(result)


@pytest.mark.parametrize("key", ["target_id", "host", "port", "scheme", "tls", "secret_ref"])
async def test_a_card_without_the_target_a_connection_field_or_secret_ref_refuses_the_run(
    key: str, monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask(_TO_KEYCLOAK)
    effect = dict(row.proposed_effect)
    card = _card(row)
    card["sink"] = {k: v for k, v in card["sink"].items() if k != key}
    async with get_sessionmaker()() as session:
        await session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == row.id)
            .values(proposed_effect={**effect, "preview": card})
        )
        await session.commit()

    result = await _approve_and_run(row.id)

    _assert_refused(result, "cannot tell what was approved", fake, keycloak)
    assert fake.secrets.kv.v2.read_calls == []  # the sink check runs before the source read


async def test_a_name_with_no_target_at_park_refuses_the_run_once_it_finds_one(
    monkeypatch: pytest.MonkeyPatch, keycloak: _Keycloak
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    row = await _ask(
        {"from": _VAULT_PLACE, "to": f"keycloak:new-idp/{_REALM}/{_USERNAME}#password"}
    )
    await _update("kc-b", aliases=["new-idp"])

    result = await _approve_and_run(row.id)

    _assert_refused(result, "cannot tell what was approved", fake, keycloak)
