# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may approve a waiting change of rights, and when the note shows: every mix.

A waiting change of rights is stored with its approval card. Then the
approve and the "may I approve?" sign run for each approver. The cases are
every mix of 12 request kinds and 9 approvers:

* the request kinds: a member add in another realm (asked by a tenant Admin
  by token and in the web console, by an Operator, by an agent with an Admin
  behind it and by an agent with nobody behind it); a member add in MEHO's
  own login realm (asked by a tenant Admin in the web console, and by a
  platform admin by token and in the web console); a change of the sign-in
  flow of the approval client; ``policy.apply``; a password reset of the
  asker's own login in another realm; a ``secret.move`` into the asker's own
  password in MEHO's own login realm;
* the approvers: the asker in the web console, by token, and in the web
  console with the break-glass switch on; another tenant Admin and another
  platform admin, each in the web console and by token; another platform
  admin who also has an Operator group in the tenant; an Operator.

For each request kind the note of the waiting request is checked with one
admin (the asker), two admins, and "not known". It shows only for a change
in MEHO's own login realm whose asker is the only admin. Fixture values are
made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import select

import meho_backplane.connectors.policy
import meho_backplane.connectors.secret.move_preview  # noqa: F401 -- registers secret.move
from meho_backplane.auth import tenant_admins
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector  # noqa: F401 -- registers writes
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalError,
    approve_request,
    create_pending_request,
    rights_change_note,
)
from meho_backplane.settings import get_settings
from tests.test_people_behind_approve import (
    _AGENT_CLIENT,
    _AGENT_SUB,
    _SETTER_UP,
    _TENANT,
    _load,
    _register_agent,
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
    _surfaces_agree,
)

_ASKER = "11111111-1111-1111-1111-11111111aaaa"
_UNLINKED_ADMIN = "11111111-1111-1111-1111-11111111bbbb"
_OTHER_TENANT_ADMIN = "11111111-1111-1111-1111-11111111cccc"
_OTHER_PLATFORM_ADMIN = "11111111-1111-1111-1111-11111111dddd"
_GROUP_USER = "22222222-2222-2222-2222-222222222222"
_GROUP = "33333333-3333-3333-3333-333333333333"
_VAULT = "vault:secret/meho/example#password"
_MEMBER_ADD = "keycloak.group.member.add"

_GUIDE = (
    "https://evoila.github.io/meho/latest/guides/approvals-and-break-glass/"
    "#changes-of-rights-need-another-admin"
)
_NOTE = (
    "This change is in MEHO's own login realm, so a platform admin must approve it. "
    "You can approve it yourself only if you are a platform admin and asked for it in the "
    f"web console. MEHO found no other admin in this tenant. See {_GUIDE}"
)

#: The refusals, word for word or by the part that names the rule.
_CONSOLE_ONLY = "Approve this change of rights in the web console."
_ONLY_AN_ADMIN = "Only an admin may approve it."
_PLATFORM_ADMIN = "A second person who is a platform admin must approve it"
_WITH_A_TOKEN = "you asked for it with a token"
_SIGN_IN = "it can change the sign-in that MEHO asks for before an approval"
_OWN_PASSWORD = "this change sets the password of your own login"
_GROUP_RULE = "You cannot approve this dangerous change of another person."


def _person(
    sub: str,
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    platform_admin: bool = False,
    web: bool = True,
    kind: PrincipalKind = PrincipalKind.USER,
    client_id: str | None = None,
) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="<every-approver-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=kind,
        client_id=client_id,
        platform_admin=platform_admin,
        web_session=web,
    )


def _grant(*, login_realm: bool, user_id: str | None, **more: Any) -> dict[str, Any]:
    return {
        "login_realm": login_realm,
        "platform_admin": login_realm,
        "harmless_keys_only": False,
        "user_id": user_id,
        **more,
    }


@dataclass(frozen=True)
class _Kind:
    name: str
    op_id: str
    asker: Operator
    #: The person the approvers "the asker" stand for (the Admin behind an agent).
    approver_as_asker: Operator
    params: dict[str, Any]
    preview: dict[str, Any] = field(default_factory=dict)
    agent_with_admin_behind: bool = False
    #: The one admin of the tenant is the asker; else another person.
    asker_is_the_admin: bool = True
    #: The note shows with one admin.
    note: bool = False


_MEMBER = {"group_id": _GROUP, "user_id": _GROUP_USER}


def _member_add(*, login_realm: bool) -> dict[str, Any]:
    return {"grant": _grant(login_realm=login_realm, user_id=_GROUP_USER, group_id=_GROUP)}


_TENANT_ADMIN_TOKEN = _person(_ASKER, web=False)
_TENANT_ADMIN_WEB = _person(_ASKER)
_OPERATOR_TOKEN = _person(_ASKER, role=TenantRole.OPERATOR, web=False)
_PLATFORM_TOKEN = _person(_ASKER, platform_admin=True, web=False)
_PLATFORM_WEB = _person(_ASKER, platform_admin=True)
_AGENT = _person(
    _AGENT_SUB,
    role=TenantRole.OPERATOR,
    web=False,
    kind=PrincipalKind.AGENT,
    client_id=_AGENT_CLIENT,
)

_KINDS = [
    _Kind("member-add-other-realm-tenant-admin-token", _MEMBER_ADD, _TENANT_ADMIN_TOKEN,
          _TENANT_ADMIN_WEB, _MEMBER, _member_add(login_realm=False)),
    _Kind("member-add-other-realm-tenant-admin-web", _MEMBER_ADD, _TENANT_ADMIN_WEB,
          _TENANT_ADMIN_WEB, _MEMBER, _member_add(login_realm=False)),
    _Kind("member-add-other-realm-operator-token", _MEMBER_ADD, _OPERATOR_TOKEN,
          _person(_ASKER, role=TenantRole.OPERATOR), _MEMBER, _member_add(login_realm=False),
          asker_is_the_admin=False),
    _Kind("member-add-other-realm-agent-with-admin-behind", _MEMBER_ADD, _AGENT,
          _person(_SETTER_UP), _MEMBER, _member_add(login_realm=False),
          agent_with_admin_behind=True, asker_is_the_admin=False),
    _Kind("member-add-other-realm-agent-with-nobody-behind", _MEMBER_ADD, _AGENT,
          _person(_UNLINKED_ADMIN), _MEMBER, _member_add(login_realm=False),
          asker_is_the_admin=False),
    _Kind("member-add-login-realm-tenant-admin-web", _MEMBER_ADD, _TENANT_ADMIN_WEB,
          _TENANT_ADMIN_WEB, _MEMBER, _member_add(login_realm=True), note=True),
    _Kind("member-add-login-realm-platform-admin-token", _MEMBER_ADD, _PLATFORM_TOKEN,
          _PLATFORM_WEB, _MEMBER, _member_add(login_realm=True), note=True),
    _Kind("member-add-login-realm-platform-admin-web", _MEMBER_ADD, _PLATFORM_WEB,
          _PLATFORM_WEB, _MEMBER, _member_add(login_realm=True), note=True),
    _Kind("approval-sign-in-flow-change-platform-admin-web", "keycloak.client.update",
          _PLATFORM_WEB, _PLATFORM_WEB, {"client_id": "meho-web-approve"},
          {"grant": _grant(login_realm=True, user_id=None), "client_id": "meho-web-approve",
           "new_values": {"authenticationFlowBindingOverrides": {"browser": ""}}}, note=True),
    _Kind("policy-apply-tenant-admin-token", "policy.apply", _TENANT_ADMIN_TOKEN,
          _TENANT_ADMIN_WEB, {"scope": "tenant"}),
    _Kind("own-password-reset-other-realm-tenant-admin-token", "keycloak.user.reset_password",
          _TENANT_ADMIN_TOKEN, _TENANT_ADMIN_WEB, {"id": _ASKER, "password_secret_ref": _VAULT},
          {"grant": _grant(login_realm=False, user_id=_ASKER)}),
    _Kind("secret-move-into-own-password-login-realm-platform-admin-web", "secret.move",
          _PLATFORM_WEB, _PLATFORM_WEB, {"from": _VAULT, "to": "keycloak:kc/meho/alice#password"},
          {"grant": _grant(login_realm=True, user_id=_ASKER, for_login=True)}, note=True),
]  # fmt: skip


async def _park(kind: _Kind) -> uuid.UUID:
    if kind.agent_with_admin_behind:
        await _register_agent()
    effect = {"op_id": kind.op_id, "safety_level": "dangerous", "preview": kind.preview}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=kind.asker,
            connector_id="keycloak-admin-26.x",
            op_id=kind.op_id,
            target=None,
            params=kind.params,
            params_hash=compute_params_hash(kind.params),
            proposed_effect=effect,
            rights_change=True,
        )
        await session.commit()
        return request.id


# --- the approvers ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Approver:
    name: str
    #: The approver for a request kind.
    who: Any
    break_glass: bool = False


def _as_asker(*, web: bool) -> Any:
    return lambda kind: kind.approver_as_asker.model_copy(update={"web_session": web})


_APPROVERS = [
    _Approver("asker-web", _as_asker(web=True)),
    _Approver("asker-token", _as_asker(web=False)),
    _Approver("asker-web-break-glass", _as_asker(web=True), break_glass=True),
    _Approver("tenant-admin-web", lambda _k: _person(_OTHER_TENANT_ADMIN)),
    _Approver("tenant-admin-token", lambda _k: _person(_OTHER_TENANT_ADMIN, web=False)),
    _Approver("platform-admin-web", lambda _k: _person(_OTHER_PLATFORM_ADMIN, platform_admin=True)),
    _Approver(
        "platform-admin-token",
        lambda _k: _person(_OTHER_PLATFORM_ADMIN, platform_admin=True, web=False),
    ),
    _Approver(
        "platform-admin-with-an-operator-group-web",
        lambda _k: _person(_OTHER_PLATFORM_ADMIN, role=TenantRole.OPERATOR, platform_admin=True),
    ),
    _Approver("operator-web", lambda _k: _person(_OTHER_TENANT_ADMIN, role=TenantRole.OPERATOR)),
]


@pytest.fixture
def break_glass(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def _on() -> None:
        monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
        get_settings.cache_clear()

    yield _on


async def _outcome(kind: _Kind, request_id: uuid.UUID, approver: Operator) -> str:
    """``approved``, or the class and text of the refusal."""
    try:
        async with get_sessionmaker()() as session:
            await approve_request(session, request_id, operator=approver, params=kind.params)
            await session.commit()
    except ApprovalError as exc:
        return f"{type(exc).__name__}: {getattr(exc, 'reason_text', None) or exc}"
    return _APPROVED


async def _self_approved() -> bool:
    """Whether the one decision row of the test records a self-approval."""
    async with get_sessionmaker()() as session:
        (row,) = (
            await session.scalars(select(AuditLog).where(AuditLog.path == "approval.decision"))
        ).all()
    return bool(row.payload["self_approved"])


_APPROVED = "approved"

#: What the asker gets in the web console (also with the break-glass switch on).
_ASKER_GETS: dict[str, str] = {
    "member-add-other-realm-tenant-admin-token": _APPROVED,
    "member-add-other-realm-tenant-admin-web": _APPROVED,
    "member-add-other-realm-operator-token": _ONLY_AN_ADMIN,
    "member-add-other-realm-agent-with-admin-behind": _APPROVED,
    "member-add-other-realm-agent-with-nobody-behind": _APPROVED,
    "member-add-login-realm-tenant-admin-web": _PLATFORM_ADMIN,
    "member-add-login-realm-platform-admin-token": _WITH_A_TOKEN,
    "member-add-login-realm-platform-admin-web": _APPROVED,
    "approval-sign-in-flow-change-platform-admin-web": _SIGN_IN,
    "policy-apply-tenant-admin-token": _APPROVED,
    "own-password-reset-other-realm-tenant-admin-token": _APPROVED,
    "secret-move-into-own-password-login-realm-platform-admin-web": _OWN_PASSWORD,
}

#: The kinds where another tenant Admin is refused: a platform admin must approve.
_LOGIN_REALM_KINDS = {k.name for k in _KINDS if k.preview.get("grant", {}).get("login_realm")}


def _expected(kind: _Kind, approver: _Approver) -> str:
    """``approved``, or a part of the refusal text."""
    if approver.name in {"asker-web", "asker-web-break-glass"}:
        return _ASKER_GETS[kind.name]
    if approver.name == "asker-token":
        return _ONLY_AN_ADMIN if _ASKER_GETS[kind.name] == _ONLY_AN_ADMIN else _CONSOLE_ONLY
    if approver.name == "tenant-admin-web":
        return _PLATFORM_ADMIN if kind.name in _LOGIN_REALM_KINDS else _APPROVED
    return {
        "tenant-admin-token": _CONSOLE_ONLY,
        "platform-admin-web": _APPROVED,
        "platform-admin-token": _CONSOLE_ONLY,
        "platform-admin-with-an-operator-group-web": _GROUP_RULE,
        "operator-web": _ONLY_AN_ADMIN,
    }[approver.name]


@pytest.mark.asyncio
@pytest.mark.parametrize("approver", _APPROVERS, ids=[a.name for a in _APPROVERS])
@pytest.mark.parametrize("kind", _KINDS, ids=[k.name for k in _KINDS])
async def test_who_may_approve(kind: _Kind, approver: _Approver, break_glass: Any) -> None:
    request_id = await _park(kind)
    if approver.break_glass:
        break_glass()
    operator = approver.who(kind)
    expected = _expected(kind, approver)

    may = await _surfaces_agree(request_id, operator)
    outcome = await _outcome(kind, request_id, operator)

    assert may is (expected == _APPROVED), outcome
    if expected == _APPROVED:
        assert outcome == _APPROVED
        asker = approver.name.startswith("asker") and kind.asker_is_the_admin
        behind = approver.name.startswith("asker") and kind.agent_with_admin_behind
        assert await _self_approved() is (asker or behind)
    else:
        assert expected in outcome, outcome
        assert (await _load(request_id)).status == "pending"


# --- the note of the waiting request ------------------------------------------------------


@pytest.fixture
def admins(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Set the admins MEHO reads from Keycloak; a fresh cache for every case."""
    monkeypatch.setattr(tenant_admins, "_kept", {})

    def _set(ids: frozenset[str] | None) -> None:
        async def _read(*_args: object) -> frozenset[str] | None:
            return ids

        monkeypatch.setattr(tenant_admins, "_read", _read)

    yield _set


@pytest.mark.asyncio
@pytest.mark.parametrize("count", ["one-admin", "two-admins", "not-known"])
@pytest.mark.parametrize("kind", _KINDS, ids=[k.name for k in _KINDS])
async def test_the_note_of_the_waiting_request(kind: _Kind, count: str, admins: Any) -> None:
    one = kind.asker.sub if kind.asker_is_the_admin else _UNLINKED_ADMIN
    if kind.agent_with_admin_behind:
        one = _SETTER_UP
    admins(
        {
            "one-admin": frozenset({one}),
            "two-admins": frozenset({one, _OTHER_PLATFORM_ADMIN}),
            "not-known": None,
        }[count]
    )
    request_id = await _park(kind)

    note = await rights_change_note(await _load(request_id))

    assert note == (_NOTE if kind.note and count == "one-admin" else None)


def test_the_note_shows_for_five_kinds() -> None:
    assert [k.name for k in _KINDS if k.note] == [
        "member-add-login-realm-tenant-admin-web",
        "member-add-login-realm-platform-admin-token",
        "member-add-login-realm-platform-admin-web",
        "approval-sign-in-flow-change-platform-admin-web",
        "secret-move-into-own-password-login-realm-platform-admin-web",
    ]
    assert len(_KINDS) * len(_APPROVERS) == 108
