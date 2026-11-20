# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time facts for the Keycloak writes that change who has rights.

``keycloak.group.member.add``, ``keycloak.role_mapping.assign``,
``keycloak.role_mapping.client.assign`` and
``keycloak.group.update_attributes`` change what a login may do in MEHO.
``keycloak.user.update`` (a new email or user name),
``keycloak.user.credential.delete`` (a passkey or one-time code removed)
and ``keycloak.user.reset_password`` (a new password) change how a login
signs in, so they count as a change of rights too. In the login realm they
count as "gives platform admin" when the login has platform admin rights
(:func:`~.ops_write_platform_reach.login_has_platform_admin`).
When one of them waits for approval, :func:`with_grant_facts` adds a
``grant`` block to its preview:

* ``user_id``: the login the change is for. A user name is resolved to its
  id here, so the approval compares ids. ``None`` when it cannot be read.
* ``group_id``: the group the change is for, resolved the way the handler
  resolves it: an id in the params wins, else the name is looked up on
  the same level (``parent_id``). ``None`` when it cannot be read.
* ``login_realm``: the target manages a realm that MEHO's own login uses,
  ``master``, or the target's ``admin_realm``, by the realm name Keycloak
  answers with (:func:`is_login_realm_name`). ``True`` when it cannot be
  read.
* ``platform_admin``: the change can give platform admin rights, or gives a
  role of the ``realm-management`` client (it decides who may manage the
  realm). For a group, MEHO reads what the group gives
  (:func:`~.ops_write_grant_platform.group_gives_platform_admin`): the
  ``platform_admin`` attribute on the group or a parent group, a place under
  the platform admin group, or the platform admin realm role. A group
  attribute change that sets ``platform_admin`` counts too. ``None`` when
  MEHO could not read the group, which the approval treats as the strict
  case.
* ``members``: for a group attribute change in the login realm, the ids
  of the logins in the group (up to :data:`MEMBER_CAP`). ``None`` when
  they could not be read, or when there are more than the cap. The
  approval then refuses: it cannot tell whether the approver is in the
  group.
* ``agent_view``: only in ``master`` or the target's ``admin_realm`` when
  that realm is not one of MEHO's own login. It holds the facts a caller
  reads while ``KEYCLOAK_RIGHTS_CHANGE_CARDS`` is off: the facts of a write
  outside the login realm (:func:`_grant_as_agent_reads`).

The same builder runs again right before the approved change runs, so
the approval check can compare the ids with the live system. For
``keycloak.user.credential.delete`` it also compares the credentials the
card lists under ``will_remove``: when they differ, the run is refused
with :data:`CREDENTIALS_CHANGED`. The approval
rules that use these facts are in
:mod:`meho_backplane.operations.grant_approval`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, cast

from meho_backplane.connectors.keycloak._paths import (
    _ADMIN_REALM_PATH,
    _GROUP_MEMBERS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write_account_common import is_realm_management
from meho_backplane.connectors.keycloak.ops_write_effective_roles import (
    ROLE_PARTS_CHANGED,
    ROLE_PARTS_NOT_READ,
    effective_roles,
)
from meho_backplane.connectors.keycloak.ops_write_grant_platform import (
    attributes_set_platform_admin,
    group_gives_platform_admin,
)
from meho_backplane.connectors.keycloak.ops_write_platform_reach import (
    REASON_KEYS,
    REASON_LOGIN,
    REASON_LOGIN_UNREAD,
    REASON_UNREAD,
    agent_cards_on,
    login_has_platform_admin,
    platform_fields,
)
from meho_backplane.connectors.keycloak.realm_params import is_plain_realm_name
from meho_backplane.connectors.keycloak.session import DEFAULT_ADMIN_REALM, resolve_realm_config
from meho_backplane.core.keycloak import ADMIN_GROUPS
from meho_backplane.operations._preview import (
    PreviewBuilder,
    PreviewContext,
    register_agent_preview_filter,
)
from meho_backplane.operations.grant_approval import HARMLESS_KEYS_ONLY, register_grant_op
from meho_backplane.settings import get_settings
from meho_backplane.targets.keycloak_realm_warnings import (
    backplane_login_realms as _backplane_login_realms,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "CREDENTIALS_CHANGED",
    "GRANT_OPS",
    "MEMBER_CAP",
    "is_login_realm",
    "is_login_realm_name",
    "named_realm_is_protected",
    "protected_realm_names",
    "register_grant_ops",
    "with_grant_facts",
]

PLATFORM_ADMIN_GROUP = ADMIN_GROUPS[1]

#: The most members MEHO reads for a group attribute change. A group with
#: more counts as "members unknown".
MEMBER_CAP: Final[int] = 1000

_MEMBER_PAGE: Final[int] = 100

#: op_id -> (params with the user id, param with the group id, param with the group name)
GRANT_OPS: dict[str, tuple[tuple[str, ...], str | None, str | None]] = {
    "keycloak.group.member.add": (("user_id",), "group_id", "group_name"),
    "keycloak.role_mapping.assign": (("id",), None, None),
    "keycloak.role_mapping.client.assign": (("id",), None, None),
    "keycloak.user.update": (("id",), None, None),
    "keycloak.user.credential.delete": (("id",), None, None),
    "keycloak.user.reset_password": (("id",), None, None),
    "keycloak.group.update_attributes": ((), "id", "name"),
}

#: Why an approved credential delete did not run.
CREDENTIALS_CHANGED: Final[str] = (
    "The credentials changed since the request; send a new request. The operation did not run."
)

#: op_id -> (the card key that lists what the run acts on, the refusal when it changed)
_BOUND_IDS: dict[str, tuple[str, str]] = {
    "keycloak.user.credential.delete": ("will_remove", CREDENTIALS_CHANGED),
    "keycloak.role_mapping.assign": ("effective_roles", ROLE_PARTS_CHANGED),
    "keycloak.role_mapping.client.assign": ("effective_roles", ROLE_PARTS_CHANGED),
}


#: The ops that give roles: their card lists the roles the request really gives.
_CLIENT_ASSIGN: Final[str] = "keycloak.role_mapping.client.assign"
_ROLE_OPS: Final[frozenset[str]] = frozenset({"keycloak.role_mapping.assign", _CLIENT_ASSIGN})
_ATTRS: Final[str] = "keycloak.group.update_attributes"

#: The ops that change how one login signs in: they need a platform admin when
#: the login has platform admin rights.
_LOGIN_OPS: Final[frozenset[str]] = frozenset(
    {"keycloak.user.update", "keycloak.user.credential.delete", "keycloak.user.reset_password"}
)

#: The ops whose preview a caller reads without a ``grant`` block while
#: ``KEYCLOAK_RIGHTS_CHANGE_CARDS`` is off.
_AGENT_NO_GRANT: Final[frozenset[str]] = frozenset({"keycloak.user.reset_password"})

#: The key of the ``grant`` block with the facts a caller reads while
#: ``KEYCLOAK_RIGHTS_CHANGE_CARDS`` is off, for a write in ``master`` or in the
#: target's admin realm that is not one of MEHO's own login.
_AGENT_VIEW: Final[str] = "agent_view"

#: Why a group or role change needs a platform admin.
REASON_GROUP_OR_ROLE: Final[str] = (
    "The group or role gives platform admin rights, or lets a login manage the realm."
)


def register_grant_ops() -> None:
    """Tell the approval check which Keycloak ops change who has rights."""
    for op_id, (user_params, group_id_param, _group_name) in GRANT_OPS.items():
        register_grant_op(
            op_id,
            user_id_params=user_params,
            group_id_params=(group_id_param,) if group_id_param else (),
            bound_ids=_BOUND_IDS.get(op_id),
            bound_ids_missing=ROLE_PARTS_NOT_READ if op_id in _ROLE_OPS else None,
        )
        register_agent_preview_filter(op_id, _grant_as_agent_reads(op_id))


def _agent_grant(grant: Any) -> Any:
    """The ``grant`` block as a caller reads it while ``KEYCLOAK_RIGHTS_CHANGE_CARDS`` is off.

    No reason, no harmless keys fact, and the ``platform_admin`` fact of
    the call only: not the one MEHO read from the login of a sign-in
    change, and not the one of the harmless keys rule.
    """
    if not isinstance(grant, dict):
        return grant
    shown = dict(grant)
    shown.pop(HARMLESS_KEYS_ONLY, None)
    if shown.pop("platform_admin_reason", None) in {REASON_LOGIN, REASON_LOGIN_UNREAD, REASON_KEYS}:
        shown["platform_admin"] = False
    return shown


def _outside_own_realm(preview: dict[str, Any], view: dict[str, Any]) -> None:
    """Show *preview* as it is for a write outside the login realm (:data:`_AGENT_VIEW`)."""
    grant = preview["grant"]
    if isinstance(grant, dict):
        grant.update(view, login_realm=False)
        if "members" in grant:
            grant["members"] = None
    if "effective_roles" in preview:
        preview["effective_roles"] = []


def _grant_as_agent_reads(op_id: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """The stored preview of *op_id* as a caller reads it (:func:`_agent_grant`).

    While ``KEYCLOAK_RIGHTS_CHANGE_CARDS`` is off, a write in ``master`` or in
    the target's admin realm reads as a write outside the login realm
    (:data:`_AGENT_VIEW`).
    """

    def _filter(preview: dict[str, Any]) -> dict[str, Any]:
        if "grant" not in preview:
            return preview
        shown = dict(preview)
        grant = shown["grant"]
        view = grant.get(_AGENT_VIEW) if isinstance(grant, dict) else None
        if isinstance(grant, dict) and _AGENT_VIEW in grant:
            shown["grant"] = {key: value for key, value in grant.items() if key != _AGENT_VIEW}
        if agent_cards_on():
            return shown
        if op_id in _AGENT_NO_GRANT:
            del shown["grant"]
            return shown
        shown["grant"] = _agent_grant(shown["grant"])
        if isinstance(view, dict):
            _outside_own_realm(shown, view)
        return shown

    return _filter


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _live_connector(ctx: PreviewContext) -> KeycloakConnector | None:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    connector = ctx.connector_instance
    if ctx.target is None or not isinstance(connector, KeycloakConnector):
        return None
    return connector


def is_login_realm_name(realm: str, target: object = None, *, own_only: bool = False) -> bool:
    """True when *realm* is a realm the login realm rule protects (letter case and spaces ignored).

    These are the realms of MEHO's own login, ``master`` (Keycloak's admin
    realm), and the ``admin_realm`` of *target* when it names one. A
    Keycloak admin in ``master`` can change every realm, also the one of
    MEHO's own login. The target's admin realm holds the login MEHO uses
    for that target. With *own_only*, only the realms of MEHO's own login
    count.
    """
    names = protected_realm_names(target, own_only=own_only)
    return realm.strip().casefold() in {name.strip().casefold() for name in names}


def protected_realm_names(target: object = None, *, own_only: bool = False) -> set[str]:
    """The realm names :func:`is_login_realm_name` protects, as configured."""
    names = set(_backplane_login_realms())
    if not own_only:
        names.add(DEFAULT_ADMIN_REALM)
        extras = getattr(target, "extras", None) or {}
        admin = extras.get("admin_realm") if isinstance(extras, Mapping) else None
        if isinstance(admin, str) and admin.strip():
            names.add(admin)
    return names


async def login_realm_facts(ctx: PreviewContext) -> tuple[bool, bool]:
    """Whether the target manages a protected realm, and whether it is one of MEHO's own login.

    The first value is :func:`is_login_realm_name`, the second the same with
    ``own_only``. The name is the one Keycloak answers with for the target's
    realm (``GET /admin/realms/{realm}``), not the text stored on the
    target: a Keycloak database can match a realm name without letter case
    or with a space at the end. When the read fails, both are ``True``.
    """
    connector = _live_connector(ctx)
    if connector is None:
        return True, True
    try:
        stored = resolve_realm_config(ctx.target).managed_realm.strip()
        answer = await connector._get_admin_json(
            ctx.target,
            fill_path(_ADMIN_REALM_PATH, {"realm": stored}),
            operator=ctx.operator,
        )
        name = _text(answer.get("realm")) if isinstance(answer, dict) else None
        if name is None:
            return True, True
        return (
            is_login_realm_name(name, ctx.target) or is_login_realm_name(stored, ctx.target),
            is_login_realm_name(name, own_only=True) or is_login_realm_name(stored, own_only=True),
        )
    except Exception:
        return True, True


async def named_realm_is_protected(
    ctx: PreviewContext,
    realm: str,
    *,
    target: object = None,
    connector: KeycloakConnector | None = None,
    new: bool = False,
) -> bool:
    """Whether the realm a param names is a protected realm (:func:`is_login_realm_name`).

    MEHO reads the realm from Keycloak (``GET /admin/realms/{realm}``) and
    judges the name in the answer: a Keycloak database can match a realm
    name without letter case, accents or a space at the end. *target* is the
    target the call goes to (default: ``ctx.target``), *connector* the
    Keycloak connector that reads it (default: the one of *ctx*).

    ``True`` when the read fails or its answer has no name. With *new* (a
    realm that ``keycloak.realm.create`` makes), a realm that Keycloak does
    not know (HTTP 404) is judged by the name as typed, and only a plain
    name (:func:`is_plain_realm_name`) counts as unprotected.
    """
    import httpx

    target = ctx.target if target is None else target
    if is_login_realm_name(realm, target):
        return True
    connector = connector if connector is not None else _live_connector(ctx)
    if connector is None or target is None:
        return True
    try:
        answer = await connector._get_admin_json(
            cast("KeycloakTargetLike", target),
            fill_path(_ADMIN_REALM_PATH, {"realm": realm}),
            operator=ctx.operator,
        )
    except httpx.HTTPStatusError as exc:
        return not (new and exc.response.status_code == 404 and is_plain_realm_name(realm))
    except Exception:
        return True
    name = _text(answer.get("realm")) if isinstance(answer, dict) else None
    return name is None or is_login_realm_name(name, target)


async def is_login_realm(ctx: PreviewContext) -> bool:
    """True when the target manages a protected realm (:func:`login_realm_facts`)."""
    protected, _own = await login_realm_facts(ctx)
    return protected


async def _user_id(ctx: PreviewContext, user_params: tuple[str, ...]) -> str | None:
    for key in user_params:
        if (value := _text(ctx.params.get(key))) is not None:
            return value
    username = _text(ctx.params.get("username"))
    connector = _live_connector(ctx)
    if username is None or connector is None:
        return None
    try:
        realm = resolve_realm_config(ctx.target).managed_realm
        return await connector._find_user_uuid(ctx.target, realm, username, operator=ctx.operator)
    except Exception:
        return None


async def _group(ctx: PreviewContext, group_id_param: str, group_name_param: str) -> str | None:
    """The id of the group the handler will change; ``None`` when unknown.

    The same order as the handler: an id in the params wins. Without an id,
    the name is looked up on the level ``parent_id`` names, which gives the id.
    """
    group_id = _text(ctx.params.get(group_id_param))
    if group_id is not None:
        return group_id
    name = _text(ctx.params.get(group_name_param))
    connector = _live_connector(ctx)
    if name is None or connector is None:
        return None
    try:
        realm = resolve_realm_config(ctx.target).managed_realm
        row = await connector._find_group(
            ctx.target, realm, name, _text(ctx.params.get("parent_id")), operator=ctx.operator
        )
    except Exception:
        return None
    found_id = row.get("id") if isinstance(row, dict) else None
    return found_id if isinstance(found_id, str) and found_id else None


async def _group_platform(op_id: str, ctx: PreviewContext, group_id: str | None) -> bool | None:
    """Whether the group change can give platform admin rights; ``None`` when unknown."""
    if op_id == _ATTRS and attributes_set_platform_admin(ctx.params.get("attributes")):
        return True
    connector = _live_connector(ctx)
    if group_id is None or connector is None:
        return None
    return await group_gives_platform_admin(connector, ctx, group_id)


async def _members(ctx: PreviewContext, group_id: str) -> list[str] | None:
    """The ids of the logins in the group; ``None`` when they cannot be read or exceed the cap."""
    connector = _live_connector(ctx)
    if connector is None:
        return None
    members: list[str] = []
    try:
        realm = resolve_realm_config(ctx.target).managed_realm
        path = fill_path(_GROUP_MEMBERS_PATH, {"realm": realm, "group-id": group_id})
        while len(members) <= MEMBER_CAP:
            rows = await connector._get_admin_list(
                ctx.target,
                path,
                operator=ctx.operator,
                params={
                    "first": str(len(members)),
                    "max": str(_MEMBER_PAGE),
                    "briefRepresentation": "true",
                },
                strict=True,
            )
            ids = [row["id"] for row in rows if isinstance(row, dict) and _text(row.get("id"))]
            if len(ids) != len(rows):
                return None
            members.extend(ids)
            if len(rows) < _MEMBER_PAGE:
                break
    except Exception:
        return None
    return None if len(members) > MEMBER_CAP else members


def _roles_include_platform_admin(ctx: PreviewContext) -> bool | None:
    """Whether the roles include the platform admin role; ``None`` when that cannot be read."""
    raw = ctx.params.get("roles")
    names = {str(r).strip().casefold() for r in raw} if isinstance(raw, list) else set()
    if names & {PLATFORM_ADMIN_GROUP.casefold(), "platform_admin"}:
        return True
    try:
        configured = get_settings().jwt_platform_admin_role_name
    except Exception:
        return None
    if not configured:
        return False
    return configured.casefold() in names


async def _with_effective_roles(
    op_id: str,
    ctx: PreviewContext,
    preview: dict[str, Any],
    platform: bool | None,
    *,
    login_realm: bool,
) -> bool | None:
    """Put the effective roles on the card; return the platform admin fact they decide.

    In MEHO's own login realm a list MEHO could read decides alone: the name of
    the asked-for role does not count. A list MEHO cannot read makes the fact
    strict (``None``) unless it was already ``True``. Outside the login realm
    the list is empty and the fact is unchanged.
    """
    connector = _live_connector(ctx)
    roles: list[dict[str, Any]] | None = []
    if login_realm:
        roles = None
        if connector is not None:
            client_id = _text(ctx.params.get("client_id")) if op_id == _CLIENT_ASSIGN else None
            roles = await effective_roles(
                connector,
                ctx,
                resolve_realm_config(ctx.target).managed_realm,
                client_id=client_id,
            )
        preview["effective_roles"] = roles
        if roles is None:
            return True if platform else None
        return any(r["realm_management"] or r["login_role"] for r in roles)
    else:
        preview["effective_roles"] = roles
    return platform


async def _login_platform(ctx: PreviewContext, user_id: str | None) -> bool | None:
    """Whether the login of a sign-in change has platform admin rights; ``None`` when unknown."""
    connector = _live_connector(ctx)
    if user_id is None or connector is None:
        return None
    try:
        realm = resolve_realm_config(ctx.target).managed_realm
    except Exception:
        return None
    return await login_has_platform_admin(connector, ctx, realm, user_id)


async def _platform_by_call(op_id: str, ctx: PreviewContext) -> tuple[str | None, bool | None]:
    """The group id and the ``platform_admin`` fact of the call, as outside the login realm."""
    _user_params, group_id_param, group_name_param = GRANT_OPS[op_id]
    if op_id == _CLIENT_ASSIGN and is_realm_management(ctx.params):
        return None, True
    if op_id in _LOGIN_OPS or group_id_param is None or group_name_param is None:
        return None, _roles_include_platform_admin(ctx)
    group_id = await _group(ctx, group_id_param, group_name_param)
    return group_id, await _group_platform(op_id, ctx, group_id)


def with_grant_facts(op_id: str, builder: PreviewBuilder) -> PreviewBuilder:
    """Wrap *builder* so its preview carries the ``grant`` block."""
    user_params = GRANT_OPS[op_id][0]

    async def _wrapped(ctx: PreviewContext) -> dict[str, Any] | None:
        preview = await builder(ctx)
        if preview is None:
            return None
        agent_off = ctx.agent_preview and not agent_cards_on()
        if agent_off and op_id in _AGENT_NO_GRANT:
            return preview
        protected, own = await login_realm_facts(ctx)
        # The agent's own preview with the switch off: only MEHO's own login realm counts.
        login_realm = own if agent_off else protected
        user_id = await _user_id(ctx, user_params)
        reason, unread = REASON_GROUP_OR_ROLE, REASON_UNREAD
        group_id, platform = await _platform_by_call(op_id, ctx)
        outside = platform
        if op_id in _LOGIN_OPS and platform is False and login_realm and not agent_off:
            platform = await _login_platform(ctx, user_id)
            reason, unread = REASON_LOGIN, REASON_LOGIN_UNREAD
        if op_id in _ROLE_OPS:
            platform = await _with_effective_roles(
                op_id, ctx, preview, platform, login_realm=login_realm
            )
        grant: dict[str, Any] = {
            "user_id": user_id,
            "group_id": group_id,
            "login_realm": login_realm,
            **(
                {"platform_admin": platform}
                if agent_off
                else platform_fields(platform, reason, unread)
            ),
        }
        if not user_params:
            grant["members"] = (
                await _members(ctx, group_id) if login_realm and group_id is not None else None
            )
        if login_realm and not own and not ctx.agent_preview:
            grant[_AGENT_VIEW] = {"platform_admin": outside}
        preview["grant"] = grant
        return preview

    return _wrapped
