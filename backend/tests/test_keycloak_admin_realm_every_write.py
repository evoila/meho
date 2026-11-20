# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""In ``master`` and in the target's admin realm, a platform admin approves every Keycloak write.

The rule and the harmless keys are the same as in MEHO's own login realm.
For each Keycloak write, and for ``secret.move`` with a Keycloak source or
sink, in ``master`` and in a target's ``admin_realm``:

* a tenant admin's approval is refused (MEHO never deletes these realms, so
  ``keycloak.realm.delete`` is refused before it waits);
* a platform admin's approval goes through, and the run check passes;
* when MEHO cannot read the realms it protects, a tenant admin's approval
  is refused;
* a write in another realm needs only a tenant admin; when the target's
  admin realm becomes that realm while the request waits, the run is
  refused.

Fixture values are made up.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write_platform_reach import REASON_SECRET_MOVE
from meho_backplane.connectors.keycloak.session import KeycloakClientCredentials
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.operations.grant_approval import (
    GRANT_REASON_STRICTER,
    HARMLESS_KEYS_ONLY,
    grant_changed_since_request,
)
from meho_backplane.settings import get_settings
from tests.test_api_v1_approvals_fresh_sign_in import (
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_grant_to_self import (
    _LOGIN_REALMS,
    _REALM,
    _REQUESTER,
    _approve,
    _park,
    _person,
    _row,
    _status,
)
from tests.test_keycloak_login_realm_every_write import (
    _HARMLESS,
    _IDS,
    _OTHER_REALM,
    _REALM_DELETE,
    _RUNS,
    _WRITES,
    _FakeKeycloak,
    _fill,
    _id,
    _tenant_admin_refused,
    _Write,
)

_MASTER = "master"
_ADMIN_REALM = "kc-admin"
#: The protected realms of these tests: ``master``, and a target's own admin realm.
_PROTECTED = [_MASTER, _ADMIN_REALM]
#: MEHO never deletes ``master`` or the admin realm: that write is refused before it waits.
_PARKS = [write for write in _WRITES if write.op_id != _REALM_DELETE]


@dataclass
class _Target:
    extras: dict[str, Any]


@dataclass
class _Desc:
    op_id: str


class _Keycloak(_FakeKeycloak):
    """The fake Keycloak, with the admin realm its targets log in to."""

    def __init__(self) -> None:
        super().__init__()
        self.admin_realm = _ADMIN_REALM

    def target(self) -> _Target:
        return _Target(extras={"managed_realm": self.realm, "admin_realm": self.admin_realm})

    def connector(self) -> KeycloakConnector:
        connector = super().connector()

        async def _creds(_target: Any, _operator: Any) -> KeycloakClientCredentials:
            return KeycloakClientCredentials(client_id="meho-admin", client_secret="made-up")

        connector._credentials_loader = _creds
        return connector


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _Keycloak:
    """The fake Keycloak, also for ``secret.move``, which finds its target and connector itself."""
    keycloak = _Keycloak()
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    get_settings.cache_clear()

    async def _resolve_target(_session: Any, _tenant: Any, _name: str) -> Any:
        return keycloak.target()

    monkeypatch.setattr("meho_backplane.targets.resolver.resolve_target", _resolve_target)
    monkeypatch.setattr(
        "meho_backplane.operations._handler_resolve.get_or_create_connector_instance",
        lambda _cls: keycloak.connector(),
    )
    return keycloak


async def _effect(fake: _Keycloak, write: _Write, realm: str) -> tuple[dict[str, Any], Any]:
    fake.realm = write.target_realm or realm
    params = _fill(copy.deepcopy(write.params), realm)
    ctx = PreviewContext(
        descriptor=_Desc(op_id=write.op_id),  # type: ignore[arg-type]
        connector_instance=fake.connector(),
        operator=_person("requester", role=TenantRole.OPERATOR),
        target=fake.target(),
        params=params,
        connector_id="keycloak-1.x",
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    return effect, params


async def _run_check(
    monkeypatch: pytest.MonkeyPatch, fake: _Keycloak, request_id: Any, write: _Write, realm: str
) -> str | None:
    async def _live_card(operator: Any, **kwargs: Any) -> dict[str, Any]:
        effect, _params = await _effect(fake, write, realm)
        return effect

    monkeypatch.setattr("meho_backplane.operations.approval_object_check.live_card", _live_card)
    params = _fill(copy.deepcopy(write.params), realm)
    return await grant_changed_since_request(
        _person(_REQUESTER), await _row(request_id), None, params
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", _PROTECTED)
@pytest.mark.parametrize("write", _PARKS, ids=[_id(write) for write in _PARKS])
async def test_in_master_and_the_admin_realm_a_tenant_admin_is_refused(
    write: _Write, realm: str, fake: _Keycloak
) -> None:
    effect, params = await _effect(fake, write, realm)
    assert effect["preview"]["grant"]["login_realm"] is True
    request_id = await _park(params, effect, op_id=write.op_id)

    reason = await _tenant_admin_refused(request_id, params)

    assert "platform admin must approve" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", _PROTECTED)
@pytest.mark.parametrize("write", _RUNS, ids=[_id(write) for write in _RUNS])
async def test_in_master_and_the_admin_realm_a_platform_admin_approves_and_it_runs(
    write: _Write, realm: str, fake: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    effect, params = await _effect(fake, write, realm)
    grant = effect["preview"]["grant"]
    assert grant[HARMLESS_KEYS_ONLY] is False
    assert grant["platform_admin"] is not False
    request_id = await _park(params, effect, op_id=write.op_id)

    await _approve(request_id, _person("person-platform", platform_admin=True), params)

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    assert await _run_check(monkeypatch, fake, request_id, write, realm) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", _PROTECTED)
@pytest.mark.parametrize("write", _PARKS, ids=[_id(write) for write in _PARKS])
async def test_in_master_and_the_admin_realm_an_unread_realm_list_refuses_a_tenant_admin(
    write: _Write, realm: str, fake: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken() -> set[str]:
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(_LOGIN_REALMS, _broken)
    effect, params = await _effect(fake, write, realm)
    request_id = await _park(params, effect, op_id=write.op_id)

    reason = await _tenant_admin_refused(request_id, params)

    assert "platform admin must approve" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _WRITES, ids=_IDS)
async def test_a_new_admin_realm_while_the_request_waits_stops_the_run(
    write: _Write, fake: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    effect, params = await _effect(fake, write, _OTHER_REALM)
    grant = effect["preview"]["grant"]
    assert grant["login_realm"] is False
    assert grant["platform_admin"] is False
    request_id = await _park(params, effect, op_id=write.op_id)
    await _approve(request_id, _person("person-admin"), params)
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    assert await _run_check(monkeypatch, fake, request_id, write, _OTHER_REALM) is None

    fake.admin_realm = _OTHER_REALM

    reason = await _run_check(monkeypatch, fake, request_id, write, _OTHER_REALM)
    assert reason is not None
    if write.op_id != _REALM_DELETE:
        assert reason == GRANT_REASON_STRICTER


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", _PROTECTED)
@pytest.mark.parametrize("write", _HARMLESS, ids=[write.op_id for write in _HARMLESS])
async def test_in_master_and_the_admin_realm_harmless_keys_need_a_tenant_admin_only(
    write: _Write, realm: str, fake: _Keycloak
) -> None:
    effect, _params = await _effect(fake, write, realm)
    grant = effect["preview"]["grant"]
    assert grant[HARMLESS_KEYS_ONLY] is True
    assert grant["platform_admin"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", ["MASTER", " Master ", "KC-Admin"])
async def test_letter_case_and_spaces_do_not_hide_master_or_the_admin_realm(
    realm: str, fake: _Keycloak
) -> None:
    write = _Write("keycloak.realm.update", {"realm": "{realm}", "representation": {"x": 1}})

    effect, _params = await _effect(fake, write, realm)

    assert effect["preview"]["grant"]["platform_admin"] is not False


@pytest.mark.asyncio
async def test_a_secret_move_whose_target_cannot_be_read_needs_a_platform_admin(
    fake: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _unreadable(_session: Any, _tenant: Any, _name: str) -> Any:
        raise RuntimeError("target unreadable")

    monkeypatch.setattr("meho_backplane.targets.resolver.resolve_target", _unreadable)
    write = next(write for write in _WRITES if _id(write) == "secret.move-source")

    effect, _params = await _effect(fake, write, _OTHER_REALM)

    grant = effect["preview"]["grant"]
    assert grant["platform_admin"] is True
    assert grant["platform_admin_reason"] == REASON_SECRET_MOVE
