# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who runs a member add at once, who waits, and when the note shows: every caller.

``keycloak.group.member.add`` goes through the full call path (``dispatch``,
and the ``call_operation`` tool for the MCP callers) against a respx-mocked
Keycloak. The cases are every mix of:

* 12 callers: a tenant Admin and a platform admin in the web console (fresh
  and old sign-in), by token and by MCP; an Operator in the web console; an
  agent; a hosted agent run of an Admin; a service login with and without a
  stored grant row;
* 2 realms: MEHO's own login realm, and another realm;
* 3 admin counts: one admin, two admins, and "not known".

Two more cases run the member add on a target whose managed realm is
``master``, and on one whose managed realm is its ``admin_realm``. There too
the note shows, and another tenant Admin may not approve.

With one admin, the one admin is the person who asked. For the Operator,
agent and service callers it is another person.

For each case the test checks whether the call runs at once or waits, that
Keycloak got the write only when it ran, the number of waiting requests,
and whether the note shows, with its full text. The note shows only for a
change of rights in MEHO's own login realm, asked by the only admin of the
tenant. Fixture values are made up.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import respx
from sqlalchemy import update

from meho_backplane.auth import tenant_admins
from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ServicePrincipalGrant, Target
from meho_backplane.operations.approval_queue import ApprovalError, approve_request
from meho_backplane.operations.meta_tools import call_operation
from tests.test_rights_change_approve_every_approver import _PLATFORM_ADMIN
from tests.test_rights_change_console_run import (
    _ADD,
    _CONNECTOR_ID,
    _GROUP_UUID,
    _KC_BASE_URL,
    _LOGIN_REALMS,
    _OLD_SIGN_IN_AGE,
    _PARAMS,
    _PLAIN_GROUP,
    _REALM,
    _TARGET_NAME,
    _TENANT_ID,
    _USER_UUID,
    _add,
    _mount,
    _requests,
    _required_settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
    keycloak,  # noqa: F401 -- fixture: registers the Keycloak operations
)

_ASKER = "console-rights-admin"
_OTHER_ADMIN = "another-admin"
_GUIDE = (
    "https://evoila.github.io/meho/latest/guides/approvals-and-break-glass/"
    "#changes-of-rights-need-another-admin"
)
#: The whole note, word for word.
_NOTE = (
    "A platform admin must approve this change. It is in MEHO's own login realm, in "
    "`master` or in the target's admin realm, or MEHO could not read its realm. "
    "You can approve it yourself only if you are a platform admin and asked for it in the "
    f"web console. MEHO found no other admin in this tenant. See {_GUIDE}"
)
_WAITING = f"awaiting_approval: {_ADD!r} requires approval before execution"


@dataclass(frozen=True)
class _Caller:
    name: str
    kind: PrincipalKind = PrincipalKind.USER
    role: TenantRole = TenantRole.TENANT_ADMIN
    platform_admin: bool = False
    web: bool = True
    old_sign_in: bool = False
    mcp: bool = False
    hosted_run: bool = False
    grant_row: bool = False
    #: The caller is an admin, so the one admin of the tenant is the caller.
    admin: bool = True
    #: Runs at once outside MEHO's own login realm, and in it.
    at_once_outside: bool = False
    at_once_inside: bool = False

    def operator(self) -> Operator:
        age = _OLD_SIGN_IN_AGE if self.old_sign_in else 5
        return Operator(
            sub=_ASKER,
            raw_jwt="<every-caller-raw-jwt>",
            tenant_id=_TENANT_ID,
            tenant_role=self.role,
            principal_kind=self.kind,
            platform_admin=self.platform_admin,
            web_session=self.web,
            acr="1",
            auth_time=int(time.time()) - age,
        )


_SERVICE = PrincipalKind.SERVICE
_CALLERS = [
    _Caller("tenant-admin-web-fresh", at_once_outside=True),
    _Caller("tenant-admin-web-old-sign-in", old_sign_in=True),
    _Caller(
        "platform-admin-web-fresh", platform_admin=True, at_once_outside=True, at_once_inside=True
    ),
    _Caller("tenant-admin-token", web=False),
    _Caller("platform-admin-token", platform_admin=True, web=False),
    _Caller("tenant-admin-mcp", web=False, mcp=True),
    _Caller("platform-admin-mcp", platform_admin=True, web=False, mcp=True),
    _Caller("operator-web", role=TenantRole.OPERATOR, admin=False),
    _Caller("agent", kind=PrincipalKind.AGENT, role=TenantRole.OPERATOR, web=False, admin=False),
    _Caller("hosted-agent-run-of-an-admin", hosted_run=True),
    _Caller("service-login", kind=_SERVICE, role=TenantRole.OPERATOR, web=False, admin=False),
    _Caller(
        "service-login-with-a-grant-row",
        kind=_SERVICE,
        role=TenantRole.OPERATOR,
        web=False,
        admin=False,
        grant_row=True,
    ),
]

#: The admins of the tenant for each count. ``None``: MEHO cannot count them.
_ONE, _TWO, _UNKNOWN = "one-admin", "two-admins", "not-known"


def _admins(count: str, caller: _Caller) -> frozenset[str] | None:
    one = _ASKER if caller.admin else _OTHER_ADMIN
    if count == _ONE:
        return frozenset({one})
    if count == _TWO:
        return frozenset({one, "a-third-admin"})
    return None


@pytest.fixture
def admin_count(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Set the admins MEHO reads from Keycloak; a fresh cache for every case."""
    monkeypatch.setattr(tenant_admins, "_kept", {})

    def _set(ids: frozenset[str] | None) -> None:
        async def _read(*_args: object) -> frozenset[str] | None:
            return ids

        monkeypatch.setattr(tenant_admins, "_read", _read)

    yield _set


async def _grant_row() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                tenant_id=_TENANT_ID,
                principal_sub=_ASKER,
                op_id=_ADD,
                connector_id=_CONNECTOR_ID,
                reason="nightly group sync",
                created_by_sub="grant-giver",
            )
        )
        await session.commit()


async def _call(caller: _Caller) -> dict[str, Any]:
    operator = caller.operator()
    if caller.mcp:
        return await call_operation(
            operator,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": _ADD,
                "target": _TARGET_NAME,
                "params": _PARAMS,
            },
        )
    if caller.hosted_run:
        with actor_delegation("agent:helper"):
            return await _add(operator)
    return await _add(operator)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [_ONE, _TWO, _UNKNOWN])
@pytest.mark.parametrize("login_realm", [True, False], ids=["login-realm", "another-realm"])
@pytest.mark.parametrize("caller", _CALLERS, ids=[c.name for c in _CALLERS])
async def test_who_runs_at_once_who_waits_and_when_the_note_shows(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    admin_count: Any,
    caller: _Caller,
    login_realm: bool,
    count: str,
) -> None:
    if not login_realm:
        monkeypatch.setattr(_LOGIN_REALMS, lambda: {"another-login-realm"})
    admin_count(_admins(count, caller))
    if caller.grant_row:
        await _grant_row()

    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount(mock, group=_PLAIN_GROUP)
        result = await _call(caller)

    at_once = caller.at_once_inside if login_realm else caller.at_once_outside
    if at_once:
        assert result["status"] == "ok", result
        assert put.called
        assert await _requests() == []
        return

    assert result["status"] == "awaiting_approval", result
    assert not put.called
    assert len(await _requests()) == 1
    note_shows = login_realm and count == _ONE and caller.admin
    if note_shows:
        assert result["extras"]["approval_note"] == _NOTE
        assert result["error"] == f"{_WAITING}. {_NOTE}"
    else:
        assert "approval_note" not in result["extras"]
        assert result["error"] == _WAITING


async def _manage(realm: str, *, admin_realm: str | None) -> None:
    """Point the seeded target at *realm*, with *admin_realm* when set."""
    extras: dict[str, str] = {"managed_realm": realm}
    if admin_realm is not None:
        extras["admin_realm"] = admin_realm
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Target).where(Target.name == _TARGET_NAME).values(extras=extras)
        )
        await session.commit()


def _mount_realm(mock: respx.MockRouter, realm: str) -> respx.Route:
    """Keycloak answers for a member add in *realm*. Returns the PUT."""
    for token_realm in {"master", realm}:
        mock.post(f"/realms/{token_realm}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token-realm", "expires_in": 300}
        )
    base = f"/admin/realms/{realm}"
    mock.get(base).respond(200, json={"realm": realm})
    group_path = f"{base}/groups/{_GROUP_UUID}"
    mock.get(group_path).respond(200, json={"id": _GROUP_UUID, **_PLAIN_GROUP})
    mock.get(f"{group_path}/members").respond(200, json=[])
    mock.get(f"{base}/users/{_USER_UUID}").respond(
        200, json={"id": _USER_UUID, "username": "operator-a"}
    )
    mock.get(f"{base}/users/{_USER_UUID}/groups").respond(200, json=[])
    return mock.put(f"{base}/users/{_USER_UUID}/groups/{_GROUP_UUID}").respond(204)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("realm", "admin_realm"),
    [("master", None), ("site-admins", "site-admins")],
    ids=["master", "the-target-s-admin-realm"],
)
async def test_the_note_shows_in_master_and_in_the_target_s_admin_realm(
    keycloak: KeycloakConnector,  # noqa: F811
    admin_count: Any,
    realm: str,
    admin_realm: str | None,
) -> None:
    caller = _CALLERS[0]
    await _manage(realm, admin_realm=admin_realm)
    admin_count(frozenset({_ASKER}))

    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount_realm(mock, realm)
        result = await _add(caller.operator())

    assert result["status"] == "awaiting_approval", result
    assert not put.called
    assert result["extras"]["approval_note"] == _NOTE
    assert result["error"] == f"{_WAITING}. {_NOTE}"
    (request,) = await _requests()
    another_admin = caller.operator().model_copy(update={"sub": _OTHER_ADMIN})
    with pytest.raises(ApprovalError) as refused:
        async with get_sessionmaker()() as session:
            await approve_request(session, request.id, operator=another_admin, params=_PARAMS)
    refusal = getattr(refused.value, "reason_text", None) or str(refused.value)
    assert _PLATFORM_ADMIN in refusal, refusal
    assert [r.status for r in await _requests()] == ["pending"]


def test_the_cases_are_every_mix() -> None:
    assert len(_CALLERS) * 2 * 3 == 72
    assert _REALM == "meho"
