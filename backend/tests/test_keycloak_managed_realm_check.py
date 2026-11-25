# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak must answer for exactly the target's managed realm before MEHO uses it.

A Keycloak database can match a realm name without letter case or accents.
Then a request for the managed realm reaches another realm. MEHO reads the
realm first and compares the answered name:

* a read returns nothing, and asks for nothing inside the realm;
* a write sends nothing, also the approval sign-in flow that writes the
  realm's settings;
* the login check sends no password;
* a request of the secret broker sends nothing;
* each run checks anew, and one run does not check before each request;
* a passed check never counts for a later call: two calls of the secret
  broker, of an approval card or of the check before an approved run each
  read the realm first, also within :data:`CHECK_SECONDS`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.managed_realm_check import ManagedRealmChecks
from meho_backplane.connectors.keycloak.ops_authflow import ENSURE_APPROVAL_FLOW_OP_ID
from meho_backplane.connectors.keycloak.secret_endpoint import build_keycloak_secret_endpoint
from meho_backplane.connectors.secret.endpoints import SecretMaterial
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.operations.approval_object_check import live_card
from tests.test_keycloak_realm_fence import (
    _CHECK_LOGIN,
    _CONNECTOR_ID,
    _ID,
    _KC_BASE_URL,
    _OTHER_ANSWERS,
    _OTHER_IDS,
    _READ_PARAMS,
    _READS,
    _REALM,
    _REALM_PATH,
    _TARGET_NAME,
    _dispatch,
    _mount_token,
    _required_settings_env,  # noqa: F401 -- autouse fixture
    _reset_module_state,  # noqa: F401 -- autouse fixture
    _target,
    operator,  # noqa: F401 -- the seeded operator and target
)

pytestmark = pytest.mark.usefixtures("real_managed_realm_check")

_USERS = f"{_REALM_PATH}/users"
_OTHER_USERS = [{"id": "u-1", "username": "user-of-another-realm"}]

#: Calls that change something inside the realm, or the realm settings.
_WRITES: dict[str, dict[str, Any]] = {
    ENSURE_APPROVAL_FLOW_OP_ID: {},
    "keycloak.authflow.ensure_passkey_code_skip": {},
    "keycloak.group.create": {"name": "example-group"},
    "keycloak.user.disable": {"id": _ID},
    "keycloak.client.delete": {"client_id": "example-client"},
}


def _mount(mock: respx.MockRouter, answer: Any) -> tuple[Any, Any]:
    """The realm answers *answer*; everything else answers like the other realm."""
    _mount_token(mock)
    realm = mock.get(_REALM_PATH).respond(200, json=answer)
    rest = mock.route(url__regex=r".*").mock(return_value=httpx.Response(200, json=_OTHER_USERS))
    return realm, rest


@pytest.mark.parametrize("answer", _OTHER_ANSWERS, ids=_OTHER_IDS)
async def test_a_mismatch_returns_no_users(operator: Operator, answer: Any) -> None:  # noqa: F811
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        realm, rest = _mount(mock, answer)
        result = await _dispatch(operator, "keycloak.user.list", {}, approved=False)

    assert result.status == "error"
    assert result.extras["exception_class"] == "ValueError"
    assert "user-of-another-realm" not in result.model_dump_json()
    assert realm.called
    assert not rest.called


@pytest.mark.parametrize("op_id", _READS)
async def test_a_mismatch_returns_no_data_of_any_read(
    operator: Operator,  # noqa: F811
    op_id: str,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm, rest = _mount(mock, {**_OTHER_ANSWERS[0], "bruteForceProtected": True})
        result = await _dispatch(operator, op_id, _READ_PARAMS.get(op_id, {}), approved=False)

    assert result.status == "error", result
    assert result.extras["exception_class"] == "ValueError"
    assert not rest.called


@pytest.mark.parametrize("answer", _OTHER_ANSWERS, ids=_OTHER_IDS)
@pytest.mark.parametrize("op_id", _WRITES)
async def test_a_mismatch_sends_no_write(
    operator: Operator,  # noqa: F811
    op_id: str,
    answer: Any,
) -> None:
    """Also no write of the realm's sign-in settings (the approval sign-in flow)."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        realm, rest = _mount(mock, answer)
        result = await _dispatch(operator, op_id, _WRITES[op_id], approved=True)

    assert result.status == "error", result
    assert realm.called
    assert not rest.called


async def test_the_level_map_of_another_realm_is_not_written_back(
    operator: Operator,  # noqa: F811
) -> None:
    """The realm settings are read again right before the write: that answer is checked too."""
    from meho_backplane.connectors.keycloak import ops_authflow

    connector = _CONNECTOR_INSTANCE_CACHE[KeycloakConnector]
    target = await _target(operator)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm, rest = _mount(mock, {**_OTHER_ANSWERS[0], "attributes": {}})
        with pytest.raises(ValueError, match="did not answer with the realm"):
            await ops_authflow._ensure_level_name(connector, operator, target, _REALM)
    assert not rest.called


async def test_a_mismatch_sends_no_password(
    operator: Operator,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.connectors.keycloak import ops_login_check

    async def _password(*_args: Any, **_kwargs: Any) -> str:
        return "example-password"

    monkeypatch.setattr(ops_login_check, "_read_password_from_vault", _password)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        realm, rest = _mount(mock, _OTHER_ANSWERS[0])
        result = await _dispatch(
            operator,
            _CHECK_LOGIN,
            {"username": "someone", "password_secret_ref": "example/ref"},
            approved=True,
        )
    assert result.status == "error", result
    assert realm.called
    assert not rest.called


async def test_a_mismatch_sends_no_request_of_the_secret_broker(
    operator: Operator,  # noqa: F811
) -> None:
    """The sink sets a password, the source reads a client secret: both use these helpers."""
    connector = _CONNECTOR_INSTANCE_CACHE[KeycloakConnector]
    target = await _target(operator)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm, rest = _mount(mock, _OTHER_ANSWERS[0])
        with pytest.raises(ValueError, match="did not answer with the realm"):
            await connector._write_admin(
                target,
                "PUT",
                f"{_USERS}/{_ID}/reset-password",
                operator=operator,
                json={"value": "example"},
            )
        with pytest.raises(ValueError, match="did not answer with the realm"):
            await connector._get_admin_json(
                target, f"{_REALM_PATH}/clients/{_ID}/client-secret", operator=operator
            )
    assert not rest.called


async def test_each_run_checks_anew(operator: Operator) -> None:  # noqa: F811
    answers = [{"realm": _REALM}, _OTHER_ANSWERS[0]]
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).mock(
            side_effect=[httpx.Response(200, json=answer) for answer in answers]
        )
        users = mock.get(_USERS).respond(200, json=[{"id": "u-1"}])
        first = await _dispatch(operator, "keycloak.user.list", {}, approved=False)
        second = await _dispatch(operator, "keycloak.user.list", {}, approved=False)

    assert first.status == "ok", first.error
    assert second.status == "error"
    assert users.call_count == 1


async def test_a_passed_check_counts_for_the_requests_that_follow(
    operator: Operator,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = {"t": 100.0}
    monkeypatch.setattr(
        "meho_backplane.connectors.keycloak.managed_realm_check.time.monotonic", lambda: now["t"]
    )
    connector = _CONNECTOR_INSTANCE_CACHE[KeycloakConnector]
    target = await _target(operator)
    checks = ManagedRealmChecks()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        realm = mock.get(_REALM_PATH).respond(200, json={"realm": _REALM})
        with checks.run():
            for _ in range(3):
                await checks.check(connector, target, operator)
            assert realm.call_count == 1
            now["t"] += 5.1
            await checks.check(connector, target, operator)
            assert realm.call_count == 2
            checks.forget(target)
            await checks.check(connector, target, operator)
            assert realm.call_count == 3
        # Outside a run, no passed check counts: every request reads the realm.
        for count in (4, 5):
            await checks.check(connector, target, operator)
            assert realm.call_count == count


_SOMEONE = [{"id": _ID, "username": "someone"}]


async def _password_move(operator: Operator) -> None:  # noqa: F811
    sink = build_keycloak_secret_endpoint(f"{_TARGET_NAME}/{_REALM}/someone#password")
    await sink.write_secret(operator, SecretMaterial(b"example-password"))


async def _client_secret_read(operator: Operator) -> None:  # noqa: F811
    source = build_keycloak_secret_endpoint(f"{_TARGET_NAME}/{_REALM}/clients/example-app#secret")
    await source.read_secret(operator)


async def _delete_card(operator: Operator) -> None:  # noqa: F811
    await build_proposed_effect(
        PreviewContext(
            descriptor=SimpleNamespace(op_id="keycloak.user.delete"),  # type: ignore[arg-type]
            connector_instance=_CONNECTOR_INSTANCE_CACHE[KeycloakConnector],
            operator=operator,
            target=await _target(operator),
            params={"username": "someone"},
            connector_id=_CONNECTOR_ID,
        )
    )


async def _check_before_the_run(operator: Operator) -> None:  # noqa: F811
    await live_card(
        operator,
        connector_id=_CONNECTOR_ID,
        op_id="keycloak.user.delete",
        target=await _target(operator),
        params={"username": "someone"},
    )


_SECOND_CALLS = {
    "secret-broker-password": _password_move,
    "secret-broker-client-secret": _client_secret_read,
    "approval-card": _delete_card,
    "check-before-the-run": _check_before_the_run,
}


@pytest.mark.parametrize("call", list(_SECOND_CALLS))
async def test_a_passed_check_does_not_count_for_the_next_call(
    operator: Operator,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    call: str,
) -> None:
    """Two calls within :data:`CHECK_SECONDS`; Keycloak answers another realm for the second.

    The second call reads the realm and sends nothing else: no password set,
    no client secret read, no user read for a card.
    """
    monkeypatch.setattr(
        "meho_backplane.connectors.keycloak.managed_realm_check.time.monotonic", lambda: 100.0
    )
    run = _SECOND_CALLS[call]
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        answer: dict[str, Any] = {"now": {"realm": _REALM, "id": "realm-id"}}
        realm = mock.get(_REALM_PATH).mock(
            side_effect=lambda _request: httpx.Response(200, json=answer["now"])
        )
        secret = mock.get(path__regex=r".*/client-secret$").respond(
            200, json={"type": "secret", "value": "example-secret"}
        )
        clients = mock.get(path__regex=r".*/clients$").respond(
            200, json=[{"id": _ID, "clientId": "example-app"}]
        )
        rest = mock.route(url__regex=r".*").mock(return_value=httpx.Response(200, json=_SOMEONE))
        await run(operator)
        reads = realm.call_count
        sent = rest.call_count + secret.call_count + clients.call_count
        assert reads > 0
        assert sent > 0

        answer["now"] = _OTHER_ANSWERS[0]
        try:
            await run(operator)
        except ValueError as exc:
            assert "did not answer with the realm" in str(exc)
        assert realm.call_count == reads + 1
        assert rest.call_count + secret.call_count + clients.call_count == sent
