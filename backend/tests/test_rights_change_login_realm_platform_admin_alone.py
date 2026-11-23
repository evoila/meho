# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""In MEHO's own login realm a platform admin makes a change of rights alone.

For each kind of Keycloak write that is a change of rights there (a
protocol mapper, a client update, a realm update, a user enable, the
``authflow.ensure_*`` writes, and a ``user.update`` of harmless keys), with
the approval card MEHO builds for it:

* a platform admin in the web console with a fresh sign-in runs it at
  once; with an old sign-in it waits;
* a tenant admin who is not a platform admin waits, and cannot approve it;
* a call with a token waits;
* a platform admin who asked in the web console approves their own
  request there; a request asked with a token is never approved by the
  login that asked, and another platform admin approves it.

A realm update reads, writes or makes a credential, so it always waits
first. Fixture values are made up.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._validate import compute_params_hash, policy_gate
from meho_backplane.operations.approval_queue import (
    ApprovalRoleFloorError,
    approve_request,
    create_pending_request,
)
from meho_backplane.operations.approval_rights import approve_right
from meho_backplane.operations.grant_approval import ASKED_IN_WEB_CONSOLE
from tests.test_keycloak_login_realm_every_write import _effect, _FakeKeycloak, _Write
from tests.test_rights_change_console_run import (
    _CONNECTOR_ID,
    _OLD_SIGN_IN_AGE,
    _TENANT_ID,
    _required_settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
    keycloak,  # noqa: F401 -- fixture: registers the Keycloak operations
)

_USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_LIVE_CARD = "meho_backplane.operations.approval_object_check.live_card"

_RUN_ALONE: list[_Write] = [
    _Write(
        "keycloak.protocol_mapper.create",
        {
            "client_id": "meho-console",
            "representation": {
                "name": "a-claim",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-hardcoded-claim-mapper",
                "config": {"claim.name": "team", "claim.value": "x"},
            },
        },
    ),
    _Write(
        "keycloak.client.update",
        {"client_id": "meho-console", "representation": {"redirectUris": ["https://x.example/*"]}},
    ),
    _Write("keycloak.user.enable", {"id": _USER_ID}),
    _Write("keycloak.authflow.ensure_approval_flow", {"client_id": "meho-console"}),
    _Write("keycloak.authflow.ensure_approval_touch", {"client_id": "meho-console"}),
    _Write("keycloak.authflow.ensure_passkey_code_skip", {}),
    _Write("keycloak.user.update", {"id": _USER_ID, "changes": {"firstName": "A"}}),
]
_REALM_UPDATE = _Write(
    "keycloak.realm.update",
    {"realm": "{realm}", "representation": {"smtpServer": {"host": "mail.example"}}},
)
_ALL = [*_RUN_ALONE, _REALM_UPDATE]
_REALM = "meho"


def _ids(writes: list[_Write]) -> list[str]:
    return [write.op_id for write in writes]


def _login(
    sub: str = "platform-admin-a",
    *,
    platform_admin: bool = True,
    web: bool = True,
    sign_in_age: int = 5,
) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="<login-realm-alone-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=platform_admin,
        web_session=web,
        acr="1",
        auth_time=int(time.time()) - sign_in_age,
    )


async def _card(write: _Write) -> tuple[dict[str, Any], dict[str, Any]]:
    """The approval card MEHO builds for *write* in the login realm, and its params."""
    effect, params = await _effect(_FakeKeycloak(), write, _REALM)
    assert effect["preview"]["grant"]["login_realm"] is True
    return effect, params


async def _gate(
    monkeypatch: pytest.MonkeyPatch, write: _Write, operator: Operator
) -> tuple[str, dict[str, Any]]:
    """The policy gate's verdict for *operator*'s call of *write*, with its real card."""
    effect, params = await _card(write)
    monkeypatch.setattr(_LIVE_CARD, AsyncMock(return_value=effect))
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT_ID,
        product="keycloak",
        version="26.x",
        impl_id="keycloak-admin",
        op_id=write.op_id,
    )
    assert descriptor is not None
    verdict, _reason = await policy_gate(
        operator=operator,
        descriptor=descriptor,
        target=None,
        connector_id=_CONNECTOR_ID,
        params=params,
    )
    return verdict.value, params


async def _park(write: _Write, requester: Operator) -> uuid.UUID:
    effect, params = await _card(write)
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=requester,
            connector_id=_CONNECTOR_ID,
            op_id=write.op_id,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            proposed_effect=effect,
        )
        await session.commit()
        return request.id


async def _row(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row


async def _approve(request_id: uuid.UUID, approver: Operator) -> None:
    row = await _row(request_id)
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=row.params)
        await session.commit()


# -- a direct run in the web console --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _RUN_ALONE, ids=_ids(_RUN_ALONE))
async def test_a_platform_admin_with_a_fresh_sign_in_runs_it_alone(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    write: _Write,
) -> None:
    verdict, _params = await _gate(monkeypatch, write, _login())

    assert verdict == "auto-execute"


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _ALL, ids=_ids(_ALL))
@pytest.mark.parametrize(
    "operator",
    [
        _login(sign_in_age=_OLD_SIGN_IN_AGE),
        _login(platform_admin=False),
        _login(web=False),
    ],
    ids=["old-sign-in", "tenant-admin", "token"],
)
async def test_every_other_call_waits(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    write: _Write,
    operator: Operator,
) -> None:
    verdict, _params = await _gate(monkeypatch, write, operator)

    assert verdict == "needs-approval"


@pytest.mark.asyncio
async def test_a_realm_update_waits_also_for_a_platform_admin(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict, _params = await _gate(monkeypatch, _REALM_UPDATE, _login())

    assert verdict == "needs-approval"


# -- the approve of a waiting request -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _ALL, ids=_ids(_ALL))
async def test_a_platform_admin_approves_their_own_request_from_the_console(
    keycloak: KeycloakConnector,  # noqa: F811
    write: _Write,
) -> None:
    request_id = await _park(write, _login())
    row = await _row(request_id)
    assert (row.proposed_effect or {})[ASKED_IN_WEB_CONSOLE] is True
    assert approve_right(_login(), row).may_approve is True

    await _approve(request_id, _login())

    assert (await _row(request_id)).status == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _ALL, ids=_ids(_ALL))
async def test_a_tenant_admin_never_approves_it(
    keycloak: KeycloakConnector,  # noqa: F811
    write: _Write,
) -> None:
    tenant_admin = _login("tenant-admin-b", platform_admin=False)
    request_id = await _park(write, tenant_admin)

    for approver in (tenant_admin, _login("tenant-admin-c", platform_admin=False)):
        with pytest.raises(ApprovalRoleFloorError) as exc:
            await _approve(request_id, approver)
        assert "platform admin must approve it" in exc.value.reason_text

    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _ALL, ids=_ids(_ALL))
async def test_a_request_asked_with_a_token_is_never_approved_by_the_login_that_asked(
    keycloak: KeycloakConnector,  # noqa: F811
    write: _Write,
) -> None:
    request_id = await _park(write, _login(web=False))
    assert ((await _row(request_id)).proposed_effect or {})[ASKED_IN_WEB_CONSOLE] is False

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _login())
    assert "with a token" in exc.value.reason_text
    assert approve_right(_login(), await _row(request_id)).may_approve is False
    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value

    await _approve(request_id, _login("platform-admin-b"))
    assert (await _row(request_id)).status == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
async def test_a_request_asked_in_the_console_is_not_approved_with_a_token(
    keycloak: KeycloakConnector,  # noqa: F811
) -> None:
    request_id = await _park(_RUN_ALONE[0], _login())

    with pytest.raises(Exception, match="web console"):
        await _approve(request_id, _login(web=False))

    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value
