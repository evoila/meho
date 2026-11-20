# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who approves a waiting change of rights in MEHO's own login realm, and for an agent.

In MEHO's own login realm:

* a tenant admin who is not a platform admin never approves a change that
  can give platform admin rights, for a change of one login or group and
  for one that is not;
* the login that asked never approves the change, also a platform admin
  who asked with a token; another platform admin approves it.

For a request of an agent:

* an admin behind the agent approves its change of rights in the web
  console (``test_people_behind_approve.py``);
* in MEHO's own login realm only a platform admin behind the agent does;
* a plain operator behind the agent never approves a change of rights.

Every case is checked on the approve, on the "may I approve?" answer, on
web push and on the policy evaluator. Fixture values are made up.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import meho_backplane.connectors.keycloak  # noqa: F401 -- registers the Keycloak writes
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalRoleFloorError,
    ApprovalRule,
    RightsChangeApprovalError,
    create_pending_request,
)
from tests.test_people_behind_approve import (
    _PARAMS,
    _SETTER_UP,
    _agent,
    _approve,
    _login,
    _register_agent,
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
    _surfaces_agree,
)

pytestmark = pytest.mark.asyncio

#: A change of one login or group.
_MEMBER_ADD = "keycloak.group.member.add"
#: A change of rights that is not a change of one login or group.
_MAPPER_CREATE = "keycloak.protocol_mapper.create"


def _card(op_id: str) -> dict[str, Any]:
    """An approval card in the login realm, for a change that can give platform admin rights."""
    grant: dict[str, Any] = {
        "login_realm": True,
        "platform_admin": True,
        "harmless_keys_only": False,
    }
    if op_id == _MEMBER_ADD:
        grant.update(user_id="11111111-1111-1111-1111-111111111111")
        grant.update(group_id="22222222-2222-2222-2222-222222222222")
    return {"op_id": op_id, "safety_level": "dangerous", "preview": {"grant": grant}}


async def _park(requester: Operator, op_id: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=requester,
            connector_id="keycloak-admin-26.x",
            op_id=op_id,
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect=_card(op_id),
            rights_change=True,
        )
        await session.commit()
        return request.id


def _admin(sub: str, *, platform: bool, web: bool = True) -> Operator:
    return _login(sub, role=TenantRole.TENANT_ADMIN).model_copy(
        update={"platform_admin": platform, "web_session": web}
    )


@pytest.mark.parametrize("op_id", [_MEMBER_ADD, _MAPPER_CREATE])
async def test_a_tenant_admin_cannot_approve_a_change_that_can_give_platform_admin(
    op_id: str,
) -> None:
    request_id = await _park(_login("requester-person"), op_id)
    tenant_admin = _admin("tenant-admin", platform=False)

    assert not await _surfaces_agree(request_id, tenant_admin)
    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, tenant_admin)
    assert "platform admin" in exc.value.reason_text

    platform_admin = _admin("platform-admin", platform=True)
    assert await _surfaces_agree(request_id, platform_admin)
    assert (await _approve(request_id, platform_admin)).status == "approved"


@pytest.mark.parametrize("op_id", [_MEMBER_ADD, _MAPPER_CREATE])
async def test_a_tenant_admin_s_own_waiting_change_needs_a_platform_admin(op_id: str) -> None:
    tenant_admin = _admin("tenant-admin", platform=False)
    request_id = await _park(tenant_admin, op_id)

    assert not await _surfaces_agree(request_id, tenant_admin)
    with pytest.raises(ApprovalRoleFloorError):
        await _approve(request_id, tenant_admin)
    assert (await _approve(request_id, _admin("platform-admin", platform=True))).status == (
        "approved"
    )


@pytest.mark.parametrize("op_id", [_MEMBER_ADD, _MAPPER_CREATE])
async def test_a_platform_admin_never_approves_a_change_they_asked_with_a_token(
    op_id: str,
) -> None:
    requester = _admin("platform-requester", platform=True, web=False)
    request_id = await _park(requester, op_id)
    in_the_console = requester.model_copy(update={"web_session": True})

    assert not await _surfaces_agree(request_id, in_the_console)
    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, in_the_console)
    assert "you asked for it" in exc.value.reason_text

    other = _admin("another-platform-admin", platform=True)
    assert await _surfaces_agree(request_id, other)
    assert (await _approve(request_id, other)).status == "approved"


@pytest.mark.parametrize("op_id", [_MEMBER_ADD, _MAPPER_CREATE])
async def test_in_the_login_realm_only_a_platform_admin_behind_the_agent_approves(
    op_id: str,
) -> None:
    await _register_agent()
    request_id = await _park(_agent(), op_id)
    tenant_admin_behind = _admin(_SETTER_UP, platform=False)
    platform_admin_behind = _admin(_SETTER_UP, platform=True)

    assert not await _surfaces_agree(request_id, tenant_admin_behind)
    with pytest.raises(ApprovalRoleFloorError):
        await _approve(request_id, tenant_admin_behind)

    assert await _surfaces_agree(request_id, platform_admin_behind)
    decided = await _approve(request_id, platform_admin_behind)
    assert decided.status == "approved"
    assert decided._approval_rule == ApprovalRule.SA_OPERATOR_SELF_APPROVAL  # type: ignore[attr-defined]


@pytest.mark.parametrize("op_id", ["x.write", _MEMBER_ADD])
async def test_an_operator_behind_the_agent_never_approves_its_change_of_rights(
    op_id: str,
) -> None:
    await _register_agent()
    request_id = await _park(_agent(), op_id)
    operator_behind = _login(_SETTER_UP).model_copy(
        update={"web_session": True, "self_approver": True}
    )

    assert not await _surfaces_agree(request_id, operator_behind)
    with pytest.raises(RightsChangeApprovalError):
        await _approve(request_id, operator_behind)
