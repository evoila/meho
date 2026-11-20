# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Whether a Keycloak group gives platform admin rights in MEHO's login.

MEHO's login gives a person platform admin rights from what their groups
carry, not from the group name. So :func:`group_gives_platform_admin` reads
the group and counts it as "gives platform admin" when one of these holds:

* The group, or a parent group, has the attribute ``platform_admin`` (or the
  attribute named in ``JWT_PLATFORM_ADMIN_CLAIM_NAME``) with the value
  ``true``. MEHO's login reads it with a user attribute mapper, which looks
  at the person's groups and their parent groups.
* The group is the platform admin group, or its path is under it
  (``/MEHO-Platform-Admins/...``).
* ``JWT_PLATFORM_ADMIN_ROLE_NAME`` is set, and the group gives that realm
  role: mapped to the group or to a parent group, or as a part of a
  composite role. Keycloak's ``role-mappings/realm/composite`` answer for the
  group lists all of these. The role name must match exactly, with the same
  letter case, as in MEHO's login.

The answer is ``None`` when MEHO cannot read the group, one of its parent
groups or its roles. The approval treats ``None`` as the strict case: a
platform admin must approve.

:func:`attributes_set_platform_admin` covers the other half of a group
attribute change: the new attributes in the request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak._paths import (
    _GROUP_PATH,
    _GROUP_REALM_COMPOSITE_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import get_or_none
from meho_backplane.connectors.keycloak.ops_write_effective_roles import platform_role_names
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.core.keycloak import ADMIN_GROUPS
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "PARENT_CAP",
    "attributes_set_platform_admin",
    "group_gives_platform_admin",
    "platform_flag_names",
]

_PLATFORM_GROUP: Final[str] = ADMIN_GROUPS[1].casefold()
_PLATFORM_PATH: Final[str] = f"/{_PLATFORM_GROUP}"

#: The most parent groups MEHO reads above one group. More counts as unknown.
PARENT_CAP: Final[int] = 20


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def platform_flag_names() -> frozenset[str]:
    """The attribute names that carry the platform admin flag, in lower case."""
    return frozenset({"platform_admin", get_settings().jwt_platform_admin_claim_name.casefold()})


def _names_flag(key: Any, names: frozenset[str]) -> bool:
    return isinstance(key, str) and key.strip().casefold() in names


def _is_true(value: Any) -> bool:
    values = value if isinstance(value, list) else [value]
    return any(isinstance(v, str) and v.strip().casefold() == "true" for v in values)


def attributes_set_platform_admin(attributes: Any) -> bool:
    """True when the new *attributes* of a request set the platform admin flag, to any value."""
    if not isinstance(attributes, dict):
        return False
    names = platform_flag_names()
    return any(_names_flag(key, names) for key in attributes)


def _flag_is_true(rep: dict[str, Any], names: frozenset[str]) -> bool:
    attributes = rep.get("attributes")
    if not isinstance(attributes, dict):
        return False
    return any(_names_flag(k, names) and _is_true(v) for k, v in attributes.items())


def _in_platform_group(rep: dict[str, Any]) -> bool:
    name = _text(rep.get("name"))
    path = (_text(rep.get("path")) or "").casefold()
    return (
        (name is not None and name.casefold() == _PLATFORM_GROUP)
        or path == _PLATFORM_PATH
        or path.startswith(f"{_PLATFORM_PATH}/")
    )


async def _read_group(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str, group_id: str
) -> dict[str, Any] | None:
    return await get_or_none(
        connector,
        ctx.target,
        fill_path(_GROUP_PATH, {"realm": realm, "group-id": group_id}),
        ctx.operator,
    )


async def _maps_platform_role(
    connector: KeycloakConnector,
    ctx: PreviewContext,
    realm: str,
    group_id: str,
    roles: set[str],
) -> bool:
    rows = await connector._get_admin_list(
        ctx.target,
        fill_path(_GROUP_REALM_COMPOSITE_PATH, {"realm": realm, "group-id": group_id}),
        operator=ctx.operator,
        strict=True,
    )
    names = {_text(row.get("name")) if isinstance(row, dict) else None for row in rows}
    if None in names:
        raise ValueError("a role without a name")
    return bool(names & roles)


async def group_gives_platform_admin(
    connector: KeycloakConnector, ctx: PreviewContext, group_id: str
) -> bool | None:
    """Whether a login in the group gets platform admin rights; ``None`` when unknown."""
    try:
        realm = resolve_realm_config(ctx.target).managed_realm
        names = platform_flag_names()
        roles = platform_role_names()
        if roles is None:
            return None
        group = await _read_group(connector, ctx, realm, group_id)
        if group is None:
            return None
        if _in_platform_group(group):
            return True
        seen = {group_id}
        current: dict[str, Any] | None = group
        while current is not None:
            if _flag_is_true(current, names):
                return True
            parent_id = _text(current.get("parentId"))
            if parent_id is None:
                break
            if parent_id in seen or len(seen) > PARENT_CAP:
                return None
            seen.add(parent_id)
            current = await _read_group(connector, ctx, realm, parent_id)
            if current is None:
                return None
        return bool(roles) and await _maps_platform_role(connector, ctx, realm, group_id, roles)
    except Exception:
        return None
