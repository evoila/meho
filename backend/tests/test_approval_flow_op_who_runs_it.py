# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who runs ``keycloak.authflow.ensure_approval_flow`` in MEHO's own login realm.

The ``meho doctor`` fix of the approval sign-in line names this operation.
Its text says: a platform admin runs it in the web console; asked with a
token, a second platform admin approves it. The call goes through the full
call path (``dispatch``) against a fake Keycloak:

* a platform admin in the web console with a fresh sign-in runs it at once;
* a platform admin with a token waits. The login that asked never approves
  it, also in the web console. Another platform admin approves it in the
  web console. A tenant Admin who is not a platform admin never approves it.

Fixture values are made up.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.operations import dispatch
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE, _HANDLER_CACHE
from meho_backplane.operations.approval_queue import ApprovalRoleFloorError, approve_request
from meho_backplane.operations.approval_rights import approve_right
from meho_backplane.targets.resolver import resolve_target
from tests.test_keycloak_login_realm_every_write import _FakeKeycloak
from tests.test_rights_change_console_run import (
    _CONNECTOR_ID,
    _TARGET_NAME,
    _TENANT_ID,
    _requests,
    _required_settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
    keycloak,  # noqa: F401 -- fixture: registers the Keycloak operations
)

_OP = "keycloak.authflow.ensure_approval_flow"
_PARAMS: dict[str, Any] = {"client_id": "meho-cli-approve"}
_RESULT: dict[str, Any] = {
    "realm": "meho",
    "flow_alias": "meho approval sign-in level",
    "flow_id": "flow-1",
    "flow_created": False,
    "flow_steps": [],
    "level_setting_added": False,
    "level_name": "meho-approve",
    "level_changed": False,
    "client_id": "meho-cli-approve",
    "client_uuid": "client-1",
    "browser_flow_id": "flow-1",
    "previous_browser_flow_id": "flow-1",
    "binding_changed": False,
}


def _admin(sub: str, *, platform_admin: bool = True, web: bool = True) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="<approval-flow-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=platform_admin,
        web_session=web,
        acr="1",
        auth_time=int(time.time()) - 5,
    )


@pytest.fixture
def runs(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[dict[str, Any]]]:
    """A fake Keycloak behind the connector; the list of the runs of the operation."""
    calls: list[dict[str, Any]] = []
    fake = _FakeKeycloak().connector()
    for name in (
        "_get_admin_json",
        "_get_admin_list",
        "_find_group",
        "_find_user_uuid",
        "_find_client_uuid",
        "_find_realm_role",
    ):
        setattr(keycloak, name, getattr(fake, name))

    async def authflow_ensure_approval_flow(
        self: Any, operator: Any, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        calls.append(params)
        return _RESULT

    monkeypatch.setattr(
        KeycloakConnector, "authflow_ensure_approval_flow", authflow_ensure_approval_flow
    )
    _HANDLER_CACHE.clear()
    assert _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] is keycloak
    yield calls
    _HANDLER_CACHE.clear()


async def _call(operator: Operator) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=operator, connector_id=_CONNECTOR_ID, op_id=_OP, target=target, params=_PARAMS
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


async def _approve(request_id: Any, approver: Operator) -> None:
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=_PARAMS)
        await session.commit()


@pytest.mark.asyncio
async def test_a_platform_admin_in_the_web_console_runs_it_at_once(
    runs: list[dict[str, Any]],
) -> None:
    result = await _call(_admin("platform-admin-a"))

    assert result["status"] == "ok", result
    assert runs == [_PARAMS]
    assert await _requests() == []


@pytest.mark.asyncio
async def test_asked_with_a_token_a_second_platform_admin_approves_it(
    runs: list[dict[str, Any]],
) -> None:
    asker = _admin("platform-admin-a", web=False)

    result = await _call(asker)

    assert result["status"] == "awaiting_approval", result
    assert runs == []
    (request,) = await _requests()
    asker_in_console = _admin("platform-admin-a")
    tenant_admin = _admin("tenant-admin-b", platform_admin=False)
    for refused in (asker_in_console, tenant_admin):
        assert approve_right(refused, request).may_approve is False
        with pytest.raises(ApprovalRoleFloorError):
            await _approve(request.id, refused)
    (request,) = await _requests()
    assert request.status == ApprovalRequestStatus.PENDING.value

    second = _admin("platform-admin-c")
    assert approve_right(second, request).may_approve is True
    await _approve(request.id, second)
    (request,) = await _requests()
    assert request.status == ApprovalRequestStatus.APPROVED.value
