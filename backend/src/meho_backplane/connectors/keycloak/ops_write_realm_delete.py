# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed delete of a whole Keycloak realm (#3886).

``keycloak.realm.delete`` sends ``DELETE /admin/realms/{realm}``. It is on
the ``destructive`` risk level, so a second person must approve every call.
Deleting a realm removes every user, client, group and session in it at
once, so this op follows the other governed deletes
(:mod:`~meho_backplane.connectors.keycloak.ops_write_delete`):

1. **Check the guards.** The caller must name the realm; the op never falls
   back to the target's managed realm. Some realms are never deleted through
   MEHO (refused with the ``protected_realm`` code):

   * ``master``, Keycloak's own admin realm, and the target's
     ``admin_realm`` when it is set to another realm: the target logs in
     there;
   * the target's ``managed_realm``: the realm this target exists to manage;
   * the realm behind this backplane's own login (``KEYCLOAK_ISSUER_URL``,
     and ``KEYCLOAK_ADMIN_URL`` when set). Deleting it locks every operator
     out of MEHO. This realm name is refused on every Keycloak target,
     because MEHO cannot tell for sure that a target is a different
     Keycloak.

   Realm names are compared without case, so a guard cannot be skipped by
   changing the case of a letter.
2. **Read the realm.** A realm that does not exist is a success with
   ``already_absent: true``, and no DELETE is sent. A teardown can re-run.
3. **Delete, then read again.** ``deleted: true`` is reported only after
   ``GET /admin/realms/{realm}`` answers 404. A read that answers with
   something other than a realm object (a list, a string, ``null``) is an
   error, before and after the delete; it is never read as "absent".

The preview builder in
:mod:`~meho_backplane.connectors.keycloak.ops_write_preview` runs steps 1 and
2 before the request waits for approval, so a protected realm never reaches
an approver. The approval card shows whether the realm is enabled and how
many users, clients and groups go with it. The handler runs the guards again
when the approved call executes.

References
----------

* Task: https://github.com/evoila/meho/issues/3886
* Keycloak 26.3 Admin REST API:
  https://www.keycloak.org/docs-api/26.3.3/rest-api/index.html
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final
from urllib.parse import unquote, urlsplit

from meho_backplane.connectors.keycloak._paths import (
    _ADMIN_REALM_PATH,
    _CLIENTS_PATH,
    _GROUPS_COUNT_PATH,
    _USERS_COUNT_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write_delete import (
    ALREADY_ABSENT,
    IRREVERSIBLE,
    _delete_and_verify,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakDeleteRefusedError,
    KeycloakUnexpectedResponseError,
    get_or_none,
)
from meho_backplane.connectors.keycloak.session import (
    DEFAULT_ADMIN_REALM,
    quote_segment,
    resolve_realm_config,
)
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "REALM_DELETE_OP",
    "check_realm_guards",
    "keycloak_realm_delete",
    "realm_blast_radius",
    "realm_name_param",
]

REALM_DELETE_OP: Final = "keycloak.realm.delete"


def realm_name_param(params: dict[str, Any]) -> str:
    """Return the required ``realm`` param, trimmed. Blank is an error."""
    value = params.get("realm")
    realm = value.strip() if isinstance(value, str) else ""
    if not realm:
        raise ValueError(f"{REALM_DELETE_OP} requires 'realm' (the name of the realm to delete)")
    return realm


def _realm_from_url(url: str) -> str | None:
    """Return the realm name in a Keycloak issuer or admin URL.

    The realm is the path segment after the last ``realms`` segment, so
    ``https://kc/realms/meho`` and ``https://kc/admin/realms/meho`` both give
    ``meho``. A URL with no realm gives ``None``.
    """
    segments = [unquote(s) for s in urlsplit(url).path.split("/") if s]
    realm = None
    for index in range(len(segments) - 1):
        if segments[index] == "realms":
            realm = segments[index + 1]
    return realm


def _backplane_login_realms() -> set[str]:
    """Return the realms this backplane's own login uses.

    They are refused on every Keycloak target, not only on the Keycloak
    behind those URLs: MEHO cannot tell for sure that a target is a
    different Keycloak.
    """
    settings = get_settings()
    realms: set[str] = set()
    for url in (str(settings.keycloak_issuer_url), settings.keycloak_admin_url):
        if not url:
            continue
        realm = _realm_from_url(url)
        if realm is not None:
            realms.add(realm)
    return realms


def check_realm_guards(target: KeycloakTargetLike, realm: str) -> None:
    """Refuse the realms this target or this backplane cannot live without."""
    realms = resolve_realm_config(target)
    wanted = realm.casefold()
    reasons = (
        (DEFAULT_ADMIN_REALM, "Keycloak's own admin realm"),
        (realms.admin_realm, "the target's admin realm; the target logs in there"),
        (realms.managed_realm, "the realm this target manages"),
        *((name, "the realm MEHO's own login uses") for name in _backplane_login_realms()),
    )
    for protected, why in reasons:
        if wanted == protected.casefold():
            raise KeycloakDeleteRefusedError(
                REALM_DELETE_OP,
                "protected_realm",
                f"realm {realm!r} is {why}. MEHO never deletes it.",
            )


def _realm_path(realm: str) -> str:
    return fill_path(_ADMIN_REALM_PATH, {"realm": quote_segment(realm)})


async def _count(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
) -> dict[str, int | None]:
    """Count the realm's users, clients and groups.

    A user or group count is ``None`` when Keycloak answers with an
    unexpected shape. The client list must be a JSON array: any other body
    raises :exc:`KeycloakUnexpectedResponseError`, so the card never shows
    ``clients: 0`` for a realm whose clients could not be read. An HTTP
    error is raised, not turned into ``None``.
    """
    values = {"realm": quote_segment(realm)}
    users: Any = await connector._get_admin_json(
        target, fill_path(_USERS_COUNT_PATH, values), operator=operator
    )
    groups: Any = await connector._get_admin_json(
        target, fill_path(_GROUPS_COUNT_PATH, values), operator=operator
    )
    clients_path = fill_path(_CLIENTS_PATH, values)
    clients: Any = await connector._get_admin_json(target, clients_path, operator=operator)
    if not isinstance(clients, list):
        raise KeycloakUnexpectedResponseError(
            f"GET {clients_path} answered with {type(clients).__name__} instead of a "
            "JSON array; the client count is unknown"
        )
    group_count = groups.get("count") if isinstance(groups, dict) else None
    return {
        "users": users if isinstance(users, int) and not isinstance(users, bool) else None,
        "clients": len(clients),
        "groups": group_count if isinstance(group_count, int) else None,
    }


async def realm_blast_radius(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
) -> dict[str, Any]:
    """Approval card for a realm delete: enabled, and the user / client / group counts.

    Only names and counts; the realm representation (which can carry the
    SMTP password) is never echoed.
    """
    rep = await get_or_none(connector, target, _realm_path(realm), operator)
    obj: dict[str, Any] = {
        "kind": "keycloak_realm",
        "resource": "realm",
        "realm": realm,
        "present": rep is not None,
    }
    children: list[dict[str, Any]] = []
    if rep is not None:
        counts = await _count(connector, operator, target, realm)
        obj["enabled"] = rep.get("enabled") is True
        obj.update(counts)
        children = [{"kind": kind, "count": count} for kind, count in counts.items()]
    return {
        "blast_radius": {
            "object": obj,
            "children": children,
            "irreversibility": IRREVERSIBLE if rep is not None else ALREADY_ABSENT,
        }
    }


async def keycloak_realm_delete(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Delete one realm (``DELETE /admin/realms/{realm}``). Op-id: ``keycloak.realm.delete``."""
    realm = realm_name_param(params)
    check_realm_guards(target, realm)
    result: dict[str, Any] = {"realm": realm}
    path = _realm_path(realm)
    if await get_or_none(self, target, path, operator) is None:
        return {**result, "deleted": False, "already_absent": True}
    return await _delete_and_verify(
        self, operator, target, op_id=REALM_DELETE_OP, path=path, result=result
    )
