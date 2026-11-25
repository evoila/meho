# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A realm that a param names is judged by the name Keycloak answers with.

A Keycloak database can match a realm name without letter case or accents.
So for each write whose ``realm`` param names the realm it changes
(``keycloak.realm.create``, ``keycloak.realm.update``,
``keycloak.realm.delete``, ``keycloak.user.check_login``), MEHO reads the
realm from Keycloak and judges the name in the answer:

* a name that Keycloak answers as the login realm or ``master`` needs a
  platform admin: a tenant admin's approval is refused, a platform admin's
  goes through;
* when the read fails, a tenant admin's approval is refused;
* when the answer becomes a protected realm while the request waits, the
  run is refused.

A new realm (``keycloak.realm.create``) that Keycloak does not know is
judged by its name as typed, and that name must be plain ASCII. A side of a
``secret.move`` is judged without a Keycloak call: a name that is not plain
ASCII counts as a protected realm, and so does every side when the name of
a protected realm is not plain ASCII. There, when the person who asked is
the only admin of the tenant, the waiting move shows the full one-admin
note.

Fixture values are made up.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth import tenant_admins
from meho_backplane.connectors.keycloak.realm_params import (
    check_realm_params,
    is_plain_realm_name,
)
from meho_backplane.connectors.keycloak.secret_endpoint import (
    KeycloakClientSecretSourceEndpoint,
    KeycloakCredentialSecretEndpoint,
)
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.connectors.secret.vault_endpoint import VaultKvSecretEndpoint
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations import dispatch
from meho_backplane.operations.grant_approval import GRANT_REASON_STRICTER
from tests._vault_fakes import install_fake_client
from tests.test_api_v1_approvals_fresh_sign_in import (
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_grant_to_self import (
    _REALM,
    _REQUESTER,
    _approve,
    _FakeTarget,
    _park,
    _person,
    _status,
)
from tests.test_keycloak_login_realm_every_write import (
    _OTHER_REALM,
    _REALM_DELETE,
    _VAULT,
    _effect,
    _FakeKeycloak,
    _fill,
    _login_realms,
    _run_check,
    _tenant_admin_refused,
    _Write,
    fake,  # noqa: F401 -- the fake Keycloak fixture
)

#: A realm name as typed, with a letter with an accent.
_TYPED = "mého"
_HOME = "kc-home"
_CREATE = "keycloak.realm.create"

#: The writes whose ``realm`` param names a realm that exists.
_NAMED: list[_Write] = [
    _Write(
        "keycloak.realm.update",
        {"realm": "{realm}", "representation": {"smtpServer": {"host": "mail.example"}}},
        target_realm=_HOME,
    ),
    _Write(_REALM_DELETE, {"realm": "{realm}"}, target_realm=_HOME),
    _Write(
        "keycloak.user.check_login",
        {"realm": "{realm}", "username": "alice", "password_secret_ref": _VAULT},
        target_realm=_HOME,
    ),
]
_IDS = [write.op_id for write in _NAMED]


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [_REALM, "master"])
@pytest.mark.parametrize("write", _NAMED, ids=_IDS)
async def test_a_realm_keycloak_answers_as_a_protected_realm_needs_a_platform_admin(
    write: _Write,
    answered: str,
    fake: _FakeKeycloak,  # noqa: F811
) -> None:
    fake.realm_answers[_TYPED] = answered
    effect, params = await _effect(fake, write, _TYPED)
    assert effect["preview"]["grant"]["login_realm"] is True
    request_id = await _park(params, effect, op_id=write.op_id)

    reason = await _tenant_admin_refused(request_id, params)
    assert "platform admin must approve" in reason

    await _approve(request_id, _person("person-platform", platform_admin=True), params)
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _NAMED, ids=_IDS)
async def test_when_meho_cannot_read_the_named_realm_a_tenant_admin_is_refused(
    write: _Write,
    fake: _FakeKeycloak,  # noqa: F811
) -> None:
    fake.realm_reads_fail.add(_OTHER_REALM)
    effect, params = await _effect(fake, write, _OTHER_REALM)
    if write.op_id != _REALM_DELETE:  # the delete card reads the realm itself and has no preview
        assert effect["preview"]["grant"]["login_realm"] is True
    request_id = await _park(params, effect, op_id=write.op_id)

    await _tenant_admin_refused(request_id, params)


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _NAMED, ids=_IDS)
async def test_a_named_realm_that_keycloak_answers_as_the_login_realm_at_run_start_stops_the_run(
    write: _Write,
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect, params = await _effect(fake, write, _TYPED)
    assert effect["preview"]["grant"]["login_realm"] is False
    request_id = await _park(params, effect, op_id=write.op_id)
    await _approve(request_id, _person("person-admin"), params)
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value

    fake.realm_answers[_TYPED] = _REALM

    assert await _run_check(monkeypatch, fake, request_id, write, _TYPED) is not None


# -- a new realm -------------------------------------------------------------


_NEW = _Write(_CREATE, {"realm": "{realm}", "representation": {"realm": "{realm}"}})


@pytest.mark.asyncio
async def test_a_new_realm_with_a_plain_name_needs_only_a_tenant_admin(
    fake: _FakeKeycloak,  # noqa: F811
) -> None:
    fake.missing_realms.add("team-new")
    effect, params = await _effect(fake, _NEW, "team-new")
    assert effect["preview"]["grant"]["login_realm"] is False
    request_id = await _park(params, effect, op_id=_CREATE)

    await _approve(request_id, _person("person-admin"), params)

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [_REALM, "master"])
async def test_a_new_realm_that_keycloak_answers_as_a_protected_realm_needs_a_platform_admin(
    answered: str,
    fake: _FakeKeycloak,  # noqa: F811
) -> None:
    fake.realm_answers["Meho"] = answered
    effect, params = await _effect(fake, _NEW, "Meho")
    assert effect["preview"]["grant"]["login_realm"] is True
    request_id = await _park(params, effect, op_id=_CREATE)

    assert "platform admin must approve" in await _tenant_admin_refused(request_id, params)


@pytest.mark.asyncio
async def test_when_meho_cannot_read_a_new_realm_a_tenant_admin_is_refused(
    fake: _FakeKeycloak,  # noqa: F811
) -> None:
    fake.realm_reads_fail.add("team-new")
    effect, params = await _effect(fake, _NEW, "team-new")
    assert effect["preview"]["grant"]["login_realm"] is True
    request_id = await _park(params, effect, op_id=_CREATE)

    assert "platform admin must approve" in await _tenant_admin_refused(request_id, params)


@pytest.mark.asyncio
async def test_a_new_realm_that_keycloak_knows_at_run_start_as_the_login_realm_stops_the_run(
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.missing_realms.add("team-new")
    effect, params = await _effect(fake, _NEW, "team-new")
    request_id = await _park(params, effect, op_id=_CREATE)
    await _approve(request_id, _person("person-admin"), params)

    fake.missing_realms.clear()
    fake.realm_answers["team-new"] = _REALM

    assert await _run_check(monkeypatch, fake, request_id, _NEW, "team-new") is not None


@pytest.mark.parametrize("path", ["realm", "representation.realm"])
def test_the_name_of_a_new_realm_must_be_plain_ascii(path: str) -> None:
    params = _fill(copy.deepcopy(_NEW.params), "team-new")
    assert check_realm_params(_CREATE)(params) is None

    params = _fill(copy.deepcopy(_NEW.params), "team-new")
    if path == "realm":
        params["realm"] = _TYPED
    else:
        params["representation"]["realm"] = _TYPED
    problem = check_realm_params(_CREATE)(params)

    assert problem is not None
    assert "plain ASCII" in str(problem)


def test_an_existing_realm_may_have_a_name_with_an_accent() -> None:
    check = check_realm_params("keycloak.realm.update")
    assert check({"realm": _TYPED, "representation": {"displayName": "M"}}) is None


def test_plain_names_are_ascii_from_space_to_tilde() -> None:
    assert is_plain_realm_name("Team new_1.x-y~")
    assert not is_plain_realm_name(_TYPED)
    assert not is_plain_realm_name("team\u00a0new")


# -- a side of a secret.move ---------------------------------------------------


_SIDES = [
    _Write("secret.move", {"from": _VAULT, "to": "keycloak:kc/{realm}/alice#password"}),
    _Write("secret.move", {"from": "keycloak:kc/{realm}/clients/app#secret", "to": _VAULT}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _SIDES, ids=["sink", "source"])
async def test_a_side_whose_realm_is_not_plain_ascii_needs_a_platform_admin(
    write: _Write,
    fake: _FakeKeycloak,  # noqa: F811
) -> None:
    effect, params = await _effect(fake, write, _TYPED)
    assert effect["preview"]["grant"]["login_realm"] is True
    request_id = await _park(params, effect, op_id=write.op_id)

    assert "platform admin must approve" in await _tenant_admin_refused(request_id, params)


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _SIDES, ids=["sink", "source"])
async def test_a_side_in_a_plain_other_realm_is_judged_without_a_keycloak_read(
    write: _Write,
    fake: _FakeKeycloak,  # noqa: F811
) -> None:
    fake.realm_reads_fail.add(_OTHER_REALM)
    effect, _params = await _effect(fake, write, _OTHER_REALM)
    assert effect["preview"]["grant"]["login_realm"] is False


#: A protected realm name with a letter with an accent; a side names it as
#: the plain :data:`_OTHER_REALM`.
_ACCENTED = "t\u00ebam-realm"


def _protect_accented(monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    """Make :data:`_ACCENTED` a protected realm: MEHO's login realm or the target's admin realm."""
    if where == "login-realm":
        _login_realms(monkeypatch, {_REALM, _ACCENTED})
        return

    async def _resolve_target(_session: Any, _tenant: Any, _name: str) -> Any:
        return _FakeTarget(extras={"managed_realm": _REALM, "admin_realm": _ACCENTED})

    monkeypatch.setattr("meho_backplane.targets.resolver.resolve_target", _resolve_target)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["login-realm", "admin-realm"])
@pytest.mark.parametrize("write", _SIDES, ids=["sink", "source"])
async def test_a_plain_side_needs_a_platform_admin_when_a_protected_name_has_an_accent(
    write: _Write,
    where: str,
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _protect_accented(monkeypatch, where)
    effect, params = await _effect(fake, write, _OTHER_REALM)
    assert effect["preview"]["grant"]["login_realm"] is True
    request_id = await _park(params, effect, op_id=write.op_id)

    assert "platform admin must approve" in await _tenant_admin_refused(request_id, params)


#: The one-admin note on a waiting request, word for word.
_ONE_ADMIN_NOTE = (
    "A platform admin must approve this change. It is in MEHO's own login realm, in "
    "`master` or in the target's admin realm, or MEHO could not make sure that it is not. "
    "You can approve it yourself only if you are a platform admin and asked for it in the "
    "web console. MEHO found no other admin in this tenant. See "
    "https://evoila.github.io/meho/latest/guides/approvals-and-break-glass/"
    "#changes-of-rights-need-another-admin"
)


@pytest.fixture
async def _secret_move_registered() -> None:
    """Register the ``secret.move`` row so the test can run it through ``dispatch``."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_secret_broker_operations(embedding_service=service)


def _count_side_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every read and write of a Vault or Keycloak side of a ``secret.move``."""
    calls: list[str] = []
    for cls in (
        VaultKvSecretEndpoint,
        KeycloakCredentialSecretEndpoint,
        KeycloakClientSecretSourceEndpoint,
    ):
        for name in ("read_secret", "write_secret"):

            async def _record(*_args: object, _name: str = f"{cls.__name__}.{name}") -> None:
                calls.append(_name)
                raise AssertionError(f"{_name} ran before approval")

            monkeypatch.setattr(cls, name, _record)
    return calls


@pytest.mark.asyncio
@pytest.mark.usefixtures("_secret_move_registered")
@pytest.mark.parametrize("write", _SIDES, ids=["sink", "source"])
async def test_a_plain_side_with_an_accented_admin_realm_waits_with_the_one_admin_note(
    write: _Write,
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _protect_accented(monkeypatch, "admin-realm")
    monkeypatch.setattr(tenant_admins, "_kept", {})

    async def _only_the_asker(*_args: object) -> frozenset[str]:
        return frozenset({_REQUESTER})

    monkeypatch.setattr(tenant_admins, "_read", _only_the_asker)
    vault = install_fake_client(monkeypatch)
    side_calls = _count_side_calls(monkeypatch)
    fake.realm = _OTHER_REALM
    params = {**_fill(copy.deepcopy(write.params), _OTHER_REALM), "reason": "rotate"}

    result = await dispatch(
        operator=_person(_REQUESTER),
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params=params,
    )

    assert result.status == "awaiting_approval"
    assert result.extras["approval_note"] == _ONE_ADMIN_NOTE
    assert result.error is not None
    assert result.error.endswith(_ONE_ADMIN_NOTE)
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(ApprovalRequest))).scalars().all()
    assert [(row.op_id, row.status) for row in rows] == [
        ("secret.move", ApprovalRequestStatus.PENDING.value)
    ]
    assert rows[0].proposed_effect["preview"]["grant"]["login_realm"] is True
    assert vault.secrets.kv.v2.put_calls == []
    assert side_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["login-realm", "admin-realm"])
@pytest.mark.parametrize("write", _SIDES, ids=["sink", "source"])
async def test_a_protected_name_with_an_accent_at_run_start_stops_a_plain_side(
    write: _Write,
    where: str,
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect, params = await _effect(fake, write, _OTHER_REALM)
    assert effect["preview"]["grant"]["login_realm"] is False
    request_id = await _park(params, effect, op_id=write.op_id)
    await _approve(request_id, _person("person-admin"), params)
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    assert await _run_check(monkeypatch, fake, request_id, write, _OTHER_REALM) is None

    _protect_accented(monkeypatch, where)

    reason = await _run_check(monkeypatch, fake, request_id, write, _OTHER_REALM)
    assert reason == GRANT_REASON_STRICTER
