# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two more parts of the login realm rule for Keycloak writes.

* A Keycloak write without a per-call mark (for example
  ``keycloak.group.create``) is never cleared by a standing grant or a run
  approval when it changes MEHO's own login realm. It waits for a person,
  so its approval card asks for a platform admin. In another realm a grant
  clears it.
* A ``secret.move`` that sets a user's password in the login realm names
  that login on its card. The person whose password it sets never approves
  it, also when someone else asked. When MEHO cannot read the login, the
  request cannot be approved; when the name names another login when the
  run starts, the run is refused.

Fixture values are made up.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.rights_changes import (
    NOT_RIGHTS_CHANGE_OPS,
    RIGHTS_CHANGE_OPS,
)
from meho_backplane.db.models import ApprovalRequestStatus, PermissionVerdict
from meho_backplane.op_classes import needs_per_call_approval
from meho_backplane.operations._preview import build_proposed_effect
from meho_backplane.operations._validate import _service_grant_verdict
from meho_backplane.operations.approval_queue import ApprovalRoleFloorError
from meho_backplane.operations.grant_approval import (
    FOR_LOGIN,
    GRANT_REASON_CHANGED,
    grant_changed_since_request,
)
from meho_backplane.operations.grant_cover_check import GRANT_COVER_REFUSAL
from tests.test_api_v1_approvals_fresh_sign_in import (
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_grant_to_self import (
    _LOGIN_REALMS,
    _OTHER_USER,
    _REALM,
    _REQUESTER,
    _approve,
    _ctx,
    _park,
    _person,
    _row,
    _status,
)
from tests.test_keycloak_login_realm_every_write import (
    _OTHER_REALM,
    _WRITES,
    _FakeKeycloak,
    _fill,
    fake,  # noqa: F401 -- fixture: the fake Keycloak
)

_APPROVER = "55555555-5555-5555-5555-555555555555"
_GRANT_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
_SINK = {"from": "vault:secret/meho/example#password", "to": f"keycloak:kc/{_REALM}/alice#password"}

#: The Keycloak writes that a person does not approve call by call everywhere.
_NO_MARK = sorted(
    op_id
    for op_id in (*RIGHTS_CHANGE_OPS, *NOT_RIGHTS_CHANGE_OPS)
    if not needs_per_call_approval(op_id)
)
_PARAMS = {write.op_id: write for write in _WRITES if write.op_id in _NO_MARK}


def test_the_writes_without_a_per_call_mark_are_known() -> None:
    assert set(_PARAMS) == set(_NO_MARK)
    assert "keycloak.group.create" in _NO_MARK


async def _verdict(
    monkeypatch: pytest.MonkeyPatch,
    fake_keycloak: _FakeKeycloak,
    op_id: str,
    realm: str,
    *,
    path: str,
) -> tuple[PermissionVerdict | None, str | None, list[str]]:
    """The verdict of a service login that holds a standing grant or a run approval."""
    used: list[str] = []

    async def _grant(**_kwargs: Any) -> uuid.UUID | None:
        used.append("standing grant")
        return _GRANT_ID if path == "standing grant" else None

    async def _run_approval(**_kwargs: Any) -> uuid.UUID | None:
        used.append("run approval")
        return _GRANT_ID if path == "run approval" else None

    async def _resolve(_descriptor: Any, _target: Any) -> tuple[Any, None, None]:
        return fake_keycloak.connector(), None, None

    monkeypatch.setattr("meho_backplane.operations.service_grants.consult_and_record_grant", _grant)
    monkeypatch.setattr(
        "meho_backplane.operations.run_approval_gate.consult_and_record_run_approval",
        _run_approval,
    )
    monkeypatch.setattr(
        "meho_backplane.operations.dispatcher._resolve_connector_instance", _resolve
    )
    write = _PARAMS[op_id]
    fake_keycloak.realm = write.target_realm or realm
    params = _fill(copy.deepcopy(write.params), realm)
    descriptor = SimpleNamespace(
        op_id=op_id,
        parameter_schema={},
        op_class=None,
        safety_level="caution",
        policy_always_wait=False,
    )
    verdict, reason = await _service_grant_verdict(
        operator=_person("service-login"),
        descriptor=descriptor,  # type: ignore[arg-type]
        target=_ctx(op_id, {}, realm=fake_keycloak.realm).target,
        connector_id="keycloak-1.x",
        gate_reason="safety_level=caution",
        target_id_str=None,
        params=params,
        decide_only=True,
    )
    return verdict, reason, used


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["standing grant", "run approval"])
@pytest.mark.parametrize("op_id", _NO_MARK)
async def test_in_the_login_realm_no_grant_clears_the_write(
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    path: str,
) -> None:
    verdict, reason, used = await _verdict(monkeypatch, fake, op_id, _REALM, path=path)

    assert verdict is None
    assert reason is not None and GRANT_COVER_REFUSAL in reason
    assert used == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["standing grant", "run approval"])
@pytest.mark.parametrize("op_id", _NO_MARK)
async def test_in_another_realm_a_grant_clears_the_write(
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    path: str,
) -> None:
    verdict, reason, _used = await _verdict(monkeypatch, fake, op_id, _OTHER_REALM, path=path)

    assert verdict is PermissionVerdict.AUTO_EXECUTE
    assert reason is not None and str(_GRANT_ID) in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["standing grant", "run approval"])
async def test_when_meho_cannot_read_its_login_realms_no_grant_clears_the_write(
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    def _broken() -> set[str]:
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(_LOGIN_REALMS, _broken)

    verdict, reason, used = await _verdict(
        monkeypatch, fake, "keycloak.group.create", _OTHER_REALM, path=path
    )

    assert verdict is None
    assert reason is not None and GRANT_COVER_REFUSAL in reason
    assert used == []


# ---------------------------------------------------------------------------
# secret.move that sets a password in the login realm
# ---------------------------------------------------------------------------


def _sink_login(
    monkeypatch: pytest.MonkeyPatch, fake_keycloak: _FakeKeycloak, user_id: str | None
) -> None:
    """The login that the sink ``alice`` names is *user_id*; ``None``: the read fails."""
    connector = fake_keycloak.connector()

    async def _find_user(_t: Any, _realm: str, _username: str, *, operator: Any) -> str:
        if user_id is None:
            raise RuntimeError("keycloak unreachable")
        return user_id

    connector._find_user_uuid = _find_user  # type: ignore[method-assign]
    monkeypatch.setattr(
        "meho_backplane.operations._handler_resolve.get_or_create_connector_instance",
        lambda _cls: connector if _cls is KeycloakConnector else None,
    )


async def _sink_effect(fake_keycloak: _FakeKeycloak) -> dict[str, Any]:
    ctx = _ctx("secret.move", dict(_SINK), realm=_REALM, connector=fake_keycloak.connector())
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    return effect


@pytest.mark.asyncio
async def test_the_card_names_the_login_whose_password_the_move_sets(
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sink_login(monkeypatch, fake, _APPROVER)

    grant = (await _sink_effect(fake))["preview"]["grant"]

    assert grant["user_id"] == _APPROVER
    assert grant[FOR_LOGIN] is True
    assert grant["platform_admin"] is True


@pytest.mark.asyncio
async def test_a_platform_admin_never_approves_a_move_that_sets_their_own_password(
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sink_login(monkeypatch, fake, _APPROVER)
    request_id = await _park(dict(_SINK), await _sink_effect(fake), op_id="secret.move")

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _person(_APPROVER, platform_admin=True), dict(_SINK))

    assert "your own login" in exc.value.reason_text
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_another_platform_admin_approves_the_move_and_it_runs(
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sink_login(monkeypatch, fake, _APPROVER)
    effect = await _sink_effect(fake)
    request_id = await _park(dict(_SINK), effect, op_id="secret.move")

    await _approve(request_id, _person("person-platform", platform_admin=True), dict(_SINK))

    async def _live_card(_operator: Any, **_kwargs: Any) -> dict[str, Any]:
        return await _sink_effect(fake)

    monkeypatch.setattr("meho_backplane.operations.approval_object_check.live_card", _live_card)
    row = await _row(request_id)
    assert await grant_changed_since_request(_person(_REQUESTER), row, None, dict(_SINK)) is None

    _sink_login(monkeypatch, fake, _OTHER_USER)
    assert (
        await grant_changed_since_request(_person(_REQUESTER), row, None, dict(_SINK))
        == GRANT_REASON_CHANGED
    )


@pytest.mark.asyncio
async def test_a_move_whose_login_meho_cannot_read_cannot_be_approved(
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sink_login(monkeypatch, fake, None)
    effect = await _sink_effect(fake)
    assert effect["preview"]["grant"]["user_id"] is None
    request_id = await _park(dict(_SINK), effect, op_id="secret.move")

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _person("person-platform", platform_admin=True), dict(_SINK))

    assert "could not establish which login" in exc.value.reason_text


@pytest.mark.asyncio
async def test_a_move_in_another_realm_names_no_login(
    fake: _FakeKeycloak,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sink_login(monkeypatch, fake, _APPROVER)
    params = {**_SINK, "to": f"keycloak:kc/{_OTHER_REALM}/alice#password"}
    ctx = _ctx("secret.move", params, realm=_OTHER_REALM, connector=fake.connector())

    effect = await build_proposed_effect(ctx)

    assert effect is not None
    grant = effect["preview"]["grant"]
    assert grant["login_realm"] is False
    assert grant["user_id"] is None
    assert FOR_LOGIN not in grant
