# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep Keycloak group writes from taking the rights of the last platform admin.

In MEHO's own login realm, ``keycloak.group.delete`` and
``keycloak.group.update_attributes`` follow the rule of the user writes
(:mod:`~meho_backplane.connectors.keycloak.ops_user_meho_access_guard`).
MEHO refuses the write when both hold:

* the write takes the platform admin value ``true`` away from the group or
  from a group under it, or gives such a group the value ``false``, and a
  person in such a group can have platform admin rights now and is not sure
  to have them after the write;
* after the write, no other active person is sure to have platform admin
  rights (:mod:`~meho_backplane.connectors.keycloak.ops_meho_admin_count`).

The check runs before the request waits for approval
(:func:`register_group_meho_access_checks`) and again when the call runs
(:func:`refuse_group_meho_access_loss`), under the lock of the login realm.
No approval and no standing grant lets such a write through. On doubt MEHO
refuses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak.ops_meho_admin_count import Change, Counter, own_value
from meho_backplane.connectors.keycloak.ops_user_meho_access_guard import (
    NO_APPROVAL,
    MehoAccessRefusedError,
    RealmRoleError,
    has_platform_role,
    is_meho_login_realm,
    platform_roles,
    refuse_last_admin_loss,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import GROUP_DELETE_OP
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.operations._park_check import ParkCheck, register_park_check

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "GROUP_WRITES",
    "refuse_group_meho_access_loss",
    "register_group_meho_access_checks",
]

_UPDATE_OP: Final = "keycloak.group.update_attributes"

#: The guarded group writes, and the words that start the refusal.
GROUP_WRITES: Final[dict[str, str]] = {
    GROUP_DELETE_OP: "deleting this group",
    _UPDATE_OP: "changing the attributes of this group",
}


async def _change(counter: Counter, group_id: str, op_id: str, params: dict[str, Any]) -> Change:
    if op_id == GROUP_DELETE_OP:
        return Change(group_gone=group_id)
    # Imported late: ``ops_write_groups`` imports the user lookup, which imports the user guard.
    from meho_backplane.connectors.keycloak.ops_write_groups import _normalise_attributes

    current = await counter.read_group(group_id)
    attributes = _normalise_attributes(params.get("attributes"))
    base = current.get("attributes")
    base = base if isinstance(base, dict) else {}
    merged = dict(attributes) if params.get("replace", False) else {**base, **attributes}
    return Change(group_value=(group_id, own_value({"attributes": merged})))


def _can_take_rights(before: bool | None, after: bool | None) -> bool:
    """Whether a change of a group's value can take the rights of a member.

    A value ``true`` that goes away takes them. A new value ``false`` takes
    the sure rights of a member who has the value ``true`` from another group.
    """
    return (before is True and after is not True) or (after is False and before is not False)


async def _loses_last_platform_admin(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    group_id: str,
    *,
    op_id: str,
    params: dict[str, Any],
) -> bool:
    """Whether the group write takes the last active platform admin away; raises when not known."""
    roles = platform_roles()
    probe = Counter(connector, operator, target, realm)
    counter = Counter(
        connector, operator, target, realm, await _change(probe, group_id, op_id, params)
    )
    await counter.start()
    subtree = await counter.subtree(group_id)
    if roles and op_id == GROUP_DELETE_OP:
        for group in subtree:
            if await has_platform_role(
                connector, operator, target, realm, roles=roles, group_id=group
            ):
                raise RealmRoleError("the group gives a realm role")
    risky = [
        group
        for group in subtree
        if _can_take_rights(
            await counter.value(group, after=False), await counter.value(group, after=True)
        )
    ]
    if not risky:
        return False
    if roles:
        raise RealmRoleError("platform admin rights can come from a realm role")
    if await counter.sure_admin_after():
        return False
    return await counter.someone_loses(risky)


async def refuse_group_meho_access_loss(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    group_id: str,
    *,
    op_id: str,
    params: dict[str, Any],
    realm: str | None = None,
) -> None:
    """Raise :exc:`MehoAccessRefusedError` when the group write takes the last platform admin."""
    words = GROUP_WRITES.get(op_id)
    realm = realm or resolve_realm_config(target).managed_realm
    if words is None or not is_meho_login_realm(realm):
        return

    async def _count() -> bool:
        return await _loses_last_platform_admin(
            connector, operator, target, realm, group_id, op_id=op_id, params=params
        )

    await refuse_last_admin_loss(
        op_id,
        realm,
        f"this write was refused: {words} takes the platform admin rights from the last "
        f"active platform admin of MEHO. Nothing was changed. {NO_APPROVAL}",
        _count,
    )


async def _park_group_id(
    connector: KeycloakConnector, ctx: PreviewContext, op_id: str
) -> tuple[str, str] | None:
    """The realm and id of the group, or ``None`` when it is not found."""
    if op_id == GROUP_DELETE_OP:
        # Imported late: the delete lookup's callers import the user guard.
        from meho_backplane.connectors.keycloak.ops_write_delete_lookup import find_group

        found = await find_group(connector, ctx.operator, ctx.target, ctx.params)
        if not found.present or found.object_id is None:
            return None
        return found.realm, found.object_id
    from meho_backplane.connectors.keycloak.ops_write_groups import _resolve_group_id

    group_id = await _resolve_group_id(
        connector,
        ctx.target,
        {
            "_op_id": op_id,
            "group_id": ctx.params.get("id"),
            "group_name": ctx.params.get("name"),
            "parent_id": ctx.params.get("parent_id"),
        },
        ctx.operator,
    )
    return resolve_realm_config(ctx.target).managed_realm, group_id


def _park_check(op_id: str) -> ParkCheck:
    async def _check(ctx: PreviewContext) -> str | None:
        # Imported late: the preview module imports this module.
        from meho_backplane.connectors.keycloak.ops_write_preview import (
            _live_keycloak_connector,
        )

        connector = _live_keycloak_connector(ctx)
        if connector is None or ctx.target is None:
            return None
        if not is_meho_login_realm(resolve_realm_config(ctx.target).managed_realm):
            return None
        try:
            found = await _park_group_id(connector, ctx, op_id)
        except Exception:
            # A group MEHO cannot find is left to the run, which checks again.
            return None
        if found is None:
            return None
        realm, group_id = found
        try:
            await refuse_group_meho_access_loss(
                connector,
                ctx.operator,
                ctx.target,
                group_id,
                op_id=op_id,
                params=ctx.params,
                realm=realm,
            )
        except MehoAccessRefusedError as exc:
            return exc.reason
        return None

    return _check


def register_group_meho_access_checks() -> None:
    """Run the check before a guarded group write waits for approval."""
    for op_id in GROUP_WRITES:
        register_park_check(op_id, _park_check(op_id))
