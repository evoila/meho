# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which Keycloak writes in MEHO's own login realm reach platform admin rights.

MEHO gives a person platform admin rights from their login token: the
``platform_admin`` claim (or the claim in ``JWT_PLATFORM_ADMIN_CLAIM_NAME``)
or the realm role in ``JWT_PLATFORM_ADMIN_ROLE_NAME``. In MEHO's own login
realm, a write that can put either into a token, or that acts on the login
of a person who has these rights, needs a platform admin as approver. This
module decides it for each such write and puts the answer on the approval
card, in the ``grant`` block that
:mod:`meho_backplane.operations.grant_approval` reads:

* ``keycloak.user.create``: the new user's attributes set the platform
  admin flag, one of its ``groups`` gives platform admin rights
  (:func:`~.ops_write_grant_platform.group_gives_platform_admin`), or it
  names realm or client roles. MEHO does not read what a named role gives,
  so named roles count as the strict case.
* ``keycloak.user.reset_password``, ``keycloak.user.update`` and
  ``keycloak.user.credential.delete``: the login has platform admin rights
  (:func:`login_has_platform_admin`): the flag on the user, a group that
  gives the rights, or the platform admin realm role among all its realm
  roles. The first three are in ``GRANT_OPS`` of :mod:`.ops_write_grant`.
* ``keycloak.protocol_mapper.create``,
  ``keycloak.client_scope.protocol_mapper.update``,
  ``keycloak.client.create``, ``keycloak.client.update`` and
  ``keycloak.client_scope.create``: a protocol mapper that can put the
  platform admin claim or role into a token (:func:`mapper_sets_platform_admin`).
* ``keycloak.realm.update``: new default groups that give platform admin
  rights, a new default role, or realm parts (users, groups, roles,
  clients, client scopes) that MEHO does not read here.

Each answer is ``True`` (it does), ``False`` (it does not) or ``None``
(MEHO could not read what it needs). The approval treats ``None`` as the
strict case: a platform admin must approve. The card also says why, in
``platform_admin_reason``. The same builder runs again right before the
approved change runs; when the change now needs a stricter approval, the
run is refused (:func:`~meho_backplane.operations.grant_approval.grant_changed_since_request`).

A ``secret.move`` whose source or sink is in the login realm always needs
a platform admin (:func:`secret_move_grant`).

On top of these facts, :mod:`.ops_write_login_realm_rule` asks for a
platform admin for every Keycloak write in the login realm that sets more
than the harmless keys.

Outside the login realm none of this applies, and MEHO reads nothing for it.

The approval request a person reads always holds the ``grant`` block. A
caller (``preview_operation``, ``meho_approvals_get`` and
``meho_approvals_list``) reads it only while ``KEYCLOAK_RIGHTS_CHANGE_CARDS``
(``Settings.keycloak_rights_change_cards``) is on
(:func:`hide_grant_from_agent`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final, cast

from meho_backplane.connectors.keycloak._paths import (
    _USER_GROUPS_PATH,
    _USER_PATH,
    _USER_REALM_COMPOSITE_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import get_or_none
from meho_backplane.connectors.keycloak.ops_write_effective_roles import platform_role_names
from meho_backplane.connectors.keycloak.ops_write_grant_platform import (
    attributes_set_platform_admin,
    group_gives_platform_admin,
    has_platform_flag,
    platform_claim_names,
    under_platform_group,
)
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.operations._preview import register_person_only_preview_keys
from meho_backplane.operations.grant_approval import register_grant_op
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike
    from meho_backplane.operations._preview import PreviewBuilder, PreviewContext

__all__ = [
    "GROUP_CAP",
    "PLATFORM_OPS",
    "REASON_KEYS",
    "REASON_LOGIN",
    "REASON_LOGIN_UNREAD",
    "REASON_MAPPER",
    "REASON_NEW_USER",
    "REASON_REALM_DEFAULTS",
    "REASON_SECRET_MOVE",
    "REASON_UNREAD",
    "SCOPE_MAPPER_UPDATE",
    "agent_cards_on",
    "group_paths_give_platform_admin",
    "hide_grant_from_agent",
    "login_has_platform_admin",
    "mapper_sets_platform_admin",
    "platform_fields",
    "secret_move_grant",
    "with_platform_facts",
]

#: The most groups MEHO reads for one login. More counts as unknown.
GROUP_CAP: Final[int] = 100

REASON_NEW_USER: Final[str] = (
    "The new login gets an attribute, a group or a role that gives platform admin rights."
)
REASON_LOGIN: Final[str] = (
    "This login has platform admin rights. The change sets how it signs in, so it "
    "can hand that login, and its rights, to someone else."
)
REASON_LOGIN_UNREAD: Final[str] = (
    "MEHO could not read whether this login has platform admin rights, so it counts "
    "as a login that has them."
)
REASON_MAPPER: Final[str] = (
    "A protocol mapper in this change can put the platform admin claim or role into login tokens."
)
REASON_REALM_DEFAULTS: Final[str] = (
    "The change sets default groups or roles of the realm, or other realm parts, that can "
    "give platform admin rights to logins."
)
REASON_UNREAD: Final[str] = (
    "MEHO could not read whether this change gives platform admin rights, so it counts "
    "as a change that does."
)

REASON_KEYS: Final[str] = (
    "In MEHO's own login realm, in master and in the target's admin realm, a platform "
    "admin approves every Keycloak write that sets more than a display name or a description."
)
REASON_SECRET_MOVE: Final[str] = (
    "This secret move reads from or writes to MEHO's own login realm, master or the "
    "target's admin realm. There, a platform admin approves every secret move."
)

#: The scope mapper write, whose ``grant`` block :mod:`.ops_write_scope_mapper` builds.
SCOPE_MAPPER_UPDATE: Final[str] = "keycloak.client_scope.protocol_mapper.update"

#: Whether a write can give platform admin rights, from its call and the realm it changes.
PlatformFact = Callable[["PreviewContext", str], Awaitable[bool | None]]

#: Mapper types that run a script, beside the ones that start with ``script-``.
_SCRIPT_MAPPERS: Final[frozenset[str]] = frozenset({"oidc-script-based-protocol-mapper"})

#: The realm parts of a realm update that MEHO does not read here.
_REALM_PARTS: Final[tuple[str, ...]] = ("users", "groups", "roles", "clients", "clientScopes")

_REALM_UPDATE: Final[str] = "keycloak.realm.update"


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _any(facts: list[bool | None]) -> bool | None:
    """``True`` when one fact is ``True``; else ``None`` when one is unknown; else ``False``."""
    if any(fact is True for fact in facts):
        return True
    return None if any(fact is None for fact in facts) else False


def platform_fields(
    platform: bool | None, reason: str, unread: str = REASON_UNREAD
) -> dict[str, Any]:
    """The ``platform_admin`` fact for the ``grant`` block, with a reason unless it is ``False``.

    *unread* is the reason when the fact is ``None``.
    """
    fields: dict[str, Any] = {"platform_admin": platform}
    if platform is not False:
        fields["platform_admin_reason"] = reason if platform else unread
    return fields


def agent_cards_on() -> bool:
    """Whether a caller reads the ``grant`` block (``KEYCLOAK_RIGHTS_CHANGE_CARDS``)."""
    return get_settings().keycloak_rights_change_cards


def hide_grant_from_agent(op_id: str) -> None:
    """A caller reads the stored request of *op_id* without ``grant`` while the switch is off."""
    register_person_only_preview_keys(op_id, agent_cards_on, frozenset({"grant"}))


# ---------------------------------------------------------------------------
# What one object gives
# ---------------------------------------------------------------------------


def mapper_sets_platform_admin(mapper: Any) -> bool | None:
    """Whether the protocol *mapper* can put the platform admin claim or role into a token.

    True for a mapper whose claim name is the platform admin claim, a
    script mapper (its script can set any claim: a type that starts with
    ``script-``, or ``oidc-script-based-protocol-mapper``), and, when
    ``JWT_PLATFORM_ADMIN_ROLE_NAME`` is set, a mapper that writes the realm
    roles claim (``realm_access``) or names that role (``role`` or
    ``new.role.name``). A claim name with escaped dots (``a\\.b``) counts
    as the same name with plain dots (``a.b``). ``None`` when the settings
    cannot be read.
    """
    if not isinstance(mapper, dict):
        return False
    roles = platform_role_names()
    if roles is None:
        return None
    kind = (_text(mapper.get("protocolMapper")) or "").casefold()
    if kind.startswith("script-") or kind in _SCRIPT_MAPPERS:
        return True
    raw = mapper.get("config")
    config: dict[str, Any] = raw if isinstance(raw, dict) else {}
    claim = (_text(config.get("claim.name")) or "").replace("\\.", ".").casefold()
    if claim in platform_claim_names():
        return True
    if not roles:
        return False
    named = {_text(config.get(key)) for key in ("role", "new.role.name")}
    return claim.startswith("realm_access") or bool(named & roles)


def _mappers_set_platform_admin(mappers: Any) -> bool | None:
    if mappers is None:
        return False
    if not isinstance(mappers, list):
        return None
    return _any([mapper_sets_platform_admin(mapper) for mapper in mappers])


async def _group_id_by_path(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str, path: str
) -> str | None:
    """The id of the group at *path* (``/top/child``), level by level; ``None`` when not found."""
    names = [name for name in path.split("/") if name]
    parent: str | None = None
    for name in names:
        row = await connector._find_group(ctx.target, realm, name, parent, operator=ctx.operator)
        parent = _text(row.get("id")) if isinstance(row, dict) else None
        if parent is None:
            return None
    return parent


async def _path_gives_platform_admin(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str, raw: Any
) -> bool | None:
    path = _text(raw)
    if path is None:
        return None
    if under_platform_group({"path": "/" + path.lstrip("/")}):
        return True
    try:
        group_id = await _group_id_by_path(connector, ctx, realm, path)
    except Exception:
        return None
    if group_id is None:
        return None
    return await group_gives_platform_admin(connector, ctx, group_id, realm=realm)


async def group_paths_give_platform_admin(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str, paths: Any
) -> bool | None:
    """Whether one of the group *paths* gives platform admin rights; ``None`` when unknown."""
    if paths is None:
        return False
    if not isinstance(paths, list) or len(paths) > GROUP_CAP:
        return None
    facts: list[bool | None] = []
    for raw in paths:
        fact = await _path_gives_platform_admin(connector, ctx, realm, raw)
        if fact:
            return True
        facts.append(fact)
    return _any(facts)


async def _groups_give_platform_admin(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str, rows: list[Any]
) -> bool | None:
    facts: list[bool | None] = []
    for row in rows:
        group_id = _text(row.get("id")) if isinstance(row, dict) else None
        if group_id is None:
            return None
        if under_platform_group(row):
            return True
        fact = await group_gives_platform_admin(connector, ctx, group_id, realm=realm)
        if fact:
            return True
        facts.append(fact)
    return _any(facts)


async def _has_platform_role(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str, user_id: str, roles: set[str]
) -> bool | None:
    rows = await connector._get_admin_list(
        ctx.target,
        fill_path(_USER_REALM_COMPOSITE_PATH, {"realm": realm, "user-id": user_id}),
        operator=ctx.operator,
        strict=True,
    )
    names = {_text(row.get("name")) if isinstance(row, dict) else None for row in rows}
    if None in names:
        return None
    return bool(names & roles)


async def login_has_platform_admin(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str, user_id: str
) -> bool | None:
    """Whether the login *user_id* has platform admin rights; ``None`` when unknown.

    Reads the user (the platform admin attribute), its groups (each with
    :func:`~.ops_write_grant_platform.group_gives_platform_admin`, up to
    :data:`GROUP_CAP`) and, when ``JWT_PLATFORM_ADMIN_ROLE_NAME`` is set,
    every realm role it has (``role-mappings/realm/composite``: direct, from
    groups and the parts of composites).
    """
    try:
        roles = platform_role_names()
        if roles is None:
            return None
        user = await get_or_none(
            connector,
            ctx.target,
            fill_path(_USER_PATH, {"realm": realm, "user-id": user_id}),
            ctx.operator,
        )
        if user is None:
            return None
        if has_platform_flag(user):
            return True
        groups = await connector._get_admin_list(
            ctx.target,
            fill_path(_USER_GROUPS_PATH, {"realm": realm, "user-id": user_id}),
            operator=ctx.operator,
            params={"first": "0", "max": str(GROUP_CAP + 1), "briefRepresentation": "true"},
            strict=True,
        )
        if len(groups) > GROUP_CAP:
            return None
        in_groups = await _groups_give_platform_admin(connector, ctx, realm, groups)
        if in_groups is not False or not roles:
            return in_groups
        return await _has_platform_role(connector, ctx, realm, user_id, roles)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The facts of each write
# ---------------------------------------------------------------------------


def _representation(ctx: PreviewContext) -> dict[str, Any]:
    representation = ctx.params.get("representation")
    return representation if isinstance(representation, dict) else {}


def _connector(ctx: PreviewContext) -> KeycloakConnector | None:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    connector = ctx.connector_instance
    if ctx.target is None or not isinstance(connector, KeycloakConnector):
        return None
    return connector


async def _new_user(ctx: PreviewContext, realm: str) -> bool | None:
    representation = _representation(ctx)
    if attributes_set_platform_admin(representation.get("attributes")):
        return True
    if representation.get("realmRoles") or representation.get("clientRoles"):
        return None
    if not representation.get("groups"):
        return False
    connector = _connector(ctx)
    if connector is None:
        return None
    return await group_paths_give_platform_admin(
        connector, ctx, realm, representation.get("groups")
    )


async def _new_mapper(ctx: PreviewContext, realm: str) -> bool | None:
    return mapper_sets_platform_admin(_representation(ctx))


async def _client_mappers(ctx: PreviewContext, realm: str) -> bool | None:
    return _mappers_set_platform_admin(_representation(ctx).get("protocolMappers"))


async def _realm_defaults(ctx: PreviewContext, realm: str) -> bool | None:
    representation = _representation(ctx)
    if any(key in representation for key in ("defaultRole", "defaultRoles", *_REALM_PARTS)):
        return True
    if representation.get("defaultGroups") is None:
        return False
    connector = _connector(ctx)
    if connector is None:
        return None
    return await group_paths_give_platform_admin(
        connector, ctx, realm, representation.get("defaultGroups")
    )


#: op_id -> (what decides whether the write can give platform admin rights, the reason).
PLATFORM_OPS: Final[dict[str, tuple[PlatformFact, str]]] = {
    "keycloak.user.create": (_new_user, REASON_NEW_USER),
    "keycloak.protocol_mapper.create": (_new_mapper, REASON_MAPPER),
    "keycloak.client.create": (_client_mappers, REASON_MAPPER),
    "keycloak.client.update": (_client_mappers, REASON_MAPPER),
    "keycloak.client_scope.create": (_client_mappers, REASON_MAPPER),
    _REALM_UPDATE: (_realm_defaults, REASON_REALM_DEFAULTS),
}


async def _realm_of(op_id: str, ctx: PreviewContext) -> tuple[bool, str | None]:
    """Whether the write changes a realm of MEHO's own login, and that realm's name.

    The realm is the target's managed realm; for ``keycloak.realm.update``
    the ``realm`` param names it when it is given, and MEHO judges the name
    Keycloak answers with for it. The name is ``None`` when it cannot be
    read; the realm then counts as MEHO's own.
    """
    from meho_backplane.connectors.keycloak.ops_write_grant import (
        is_login_realm,
        named_realm_is_protected,
    )

    explicit = _text(ctx.params.get("realm")) if op_id == _REALM_UPDATE else None
    if explicit is not None:
        return await named_realm_is_protected(ctx, explicit), explicit
    try:
        realm: str | None = resolve_realm_config(ctx.target).managed_realm
    except Exception:
        realm = None
    return await is_login_realm(ctx), realm


def with_platform_facts(
    op_id: str,
    builder: PreviewBuilder,
    fact: tuple[PlatformFact, str] | None = None,
    *,
    agent_switch: Callable[[], bool] | None = None,
) -> PreviewBuilder:
    """Wrap *builder* so the approval card carries the ``grant`` block of *op_id*.

    *fact* is what decides whether the write can give platform admin rights,
    with the reason the card shows; it defaults to the one in
    :data:`PLATFORM_OPS`. It runs only in MEHO's own login realm. The
    agent's own ``preview_operation`` gets the block only while
    *agent_switch* is on; without a switch it never does, so it makes no
    extra Keycloak read.
    """
    decide, reason = PLATFORM_OPS[op_id] if fact is None else fact

    async def _wrapped(ctx: PreviewContext) -> dict[str, Any] | None:
        preview = await builder(ctx)
        if preview is None or (ctx.agent_preview and not (agent_switch and agent_switch())):
            return preview
        login_realm, realm = await _realm_of(op_id, ctx)
        platform: bool | None = False
        if login_realm:
            try:
                platform = None if realm is None else await decide(ctx, realm)
            except Exception:
                platform = None
        preview["grant"] = {
            "user_id": None,
            "group_id": None,
            "login_realm": login_realm,
            **platform_fields(platform, reason),
        }
        return preview

    return _wrapped


# ---------------------------------------------------------------------------
# secret.move with a Keycloak side
# ---------------------------------------------------------------------------


def _keycloak_sides(
    params: dict[str, Any],
) -> tuple[list[tuple[str, str] | None], tuple[str, str, str] | None, bool]:
    """The Keycloak sides of a ``secret.move``.

    Returns the ``(target, realm)`` of each ``keycloak:`` side (``None`` for
    one MEHO cannot read), the user-password sink ``(target, realm, username)``, and
    whether the sink is a user password that MEHO cannot read. A ref is a
    client source or a user-password sink by the same address shape as the
    endpoint factory (:func:`~.secret_endpoint.build_keycloak_secret_endpoint`),
    so a realm named ``clients`` still holds a sink.
    """
    from meho_backplane.connectors.keycloak.secret_endpoint import (
        _looks_like_client_secret_ref,
        _parse_keycloak_client_ref,
        _parse_keycloak_ref,
    )
    from meho_backplane.connectors.secret.endpoints import parse_secret_ref

    realms: list[tuple[str, str] | None] = []
    sink: tuple[str, str, str] | None = None
    sink_unread = False
    for key in ("from", "to"):
        raw = params.get(key)
        try:
            spec = parse_secret_ref(raw) if isinstance(raw, str) else None
        except ValueError:
            continue
        if spec is None or spec.kind != "keycloak":
            continue
        client = False
        try:
            client = _looks_like_client_secret_ref(spec.ref)
            if client:
                target, realm, _client = _parse_keycloak_client_ref(spec.ref)
                realms.append((target, realm))
            else:
                parsed = _parse_keycloak_ref(spec.ref)
                realms.append((parsed[0], parsed[1]))
                if key == "to":
                    sink = parsed
        except ValueError:
            realms.append(None)
            sink_unread = sink_unread or (key == "to" and not client)
    return realms, sink, sink_unread


async def _side_target(ctx: PreviewContext, target_name: str) -> KeycloakTargetLike:
    """The target a ``keycloak:`` side names, read from the tenant's targets."""
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.targets.resolver import resolve_target

    async with get_sessionmaker()() as session:
        return cast(
            "KeycloakTargetLike",
            await resolve_target(session, ctx.operator.tenant_id, target_name),
        )


async def _is_login_realm(ctx: PreviewContext, side: tuple[str, str] | None) -> bool:
    """Whether a side is in a realm of :func:`~.ops_write_grant.is_login_realm_name`.

    MEHO calls no Keycloak for a ``secret.move`` before its run checks the
    target (:mod:`.secret_move_target`), so it judges the realm name as
    typed. A Keycloak database can match a letter with an accent with the
    plain one. So the side counts as protected when its realm name is not
    plain ASCII (:func:`~.realm_params.is_plain_realm_name`), and also when
    a protected realm name (:func:`~.ops_write_grant.protected_realm_names`)
    is not plain ASCII. ``True`` also when MEHO cannot read the side, or the
    target it names (then MEHO cannot read the target's ``admin_realm``).
    """
    from meho_backplane.connectors.keycloak.ops_write_grant import (
        is_login_realm_name,
        protected_realm_names,
    )
    from meho_backplane.connectors.keycloak.realm_params import is_plain_realm_name

    if side is None:
        return True
    target_name, realm = side
    try:
        if is_login_realm_name(realm) or not is_plain_realm_name(realm):
            return True
        target = await _side_target(ctx, target_name)
        names = protected_realm_names(target)
        if not all(is_plain_realm_name(name) for name in names):
            return True
        return is_login_realm_name(realm, target)
    except Exception:
        return True


async def _sink_user_id(ctx: PreviewContext, sink: tuple[str, str, str]) -> str | None:
    """The id of the login whose password the sink sets; ``None`` when MEHO cannot read it."""
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.operations._handler_resolve import get_or_create_connector_instance

    target_name, realm, username = sink
    try:
        connector = get_or_create_connector_instance(KeycloakConnector)
        if not isinstance(connector, KeycloakConnector):
            return None
        row = await _side_target(ctx, target_name)
        return await connector._find_user_uuid(row, realm, username, operator=ctx.operator)
    except Exception:
        return None


async def secret_move_grant(ctx: PreviewContext) -> dict[str, Any] | None:
    """The ``grant`` block of a ``secret.move`` with a Keycloak side; ``None`` without one.

    When the source or the sink is in MEHO's own login realm, ``master`` or
    the ``admin_realm`` of the target it names, or MEHO cannot read the
    realm of a side, a platform admin must approve. A
    user-password sink there names the login whose password changes
    (``for_login`` and ``user_id``), so that login never approves the move.
    When MEHO cannot read that login, ``user_id`` is ``None`` and the
    request cannot be approved.
    """
    from meho_backplane.operations.grant_approval import FOR_LOGIN, HARMLESS_KEYS_ONLY

    realms, sink, sink_unread = _keycloak_sides(ctx.params)
    if not realms:
        return None
    protected = [await _is_login_realm(ctx, side) for side in realms]
    login_realm = any(protected)
    sink_in_login_realm = sink_unread or (
        sink is not None and await _is_login_realm(ctx, (sink[0], sink[1]))
    )
    grant: dict[str, Any] = {
        "user_id": None,
        "group_id": None,
        "login_realm": login_realm,
        "platform_admin": login_realm,
        HARMLESS_KEYS_ONLY: False,
    }
    if login_realm:
        grant["platform_admin_reason"] = REASON_SECRET_MOVE
    if sink_in_login_realm:
        grant[FOR_LOGIN] = True
        if sink is not None:
            grant["user_id"] = await _sink_user_id(ctx, sink)
    return grant


def _register() -> None:
    for op_id in (*PLATFORM_OPS, SCOPE_MAPPER_UPDATE):
        register_grant_op(op_id)


_register()
