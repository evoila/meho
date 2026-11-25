# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed delete of a whole Keycloak realm (#3886).

``keycloak.realm.delete`` sends ``DELETE /admin/realms/{realm}``. It is on
the ``destructive`` risk level: approval cannot be turned off for it.
Deleting a realm removes every user, client, group and session in it at
once, so this op follows the other governed deletes
(:mod:`~meho_backplane.connectors.keycloak.ops_write_delete`):

1. **Check the guards.** The caller must name the realm; the op never falls
   back to the target's managed realm. A target works on one realm only, so
   the realm must be the target's managed realm
   (:func:`~meho_backplane.connectors.keycloak.realm_params.realm_param`):
   another realm is refused, and nothing is sent to Keycloak. Some realms
   are never deleted through MEHO (refused with the ``protected_realm``
   code):

   * ``master``, Keycloak's own admin realm, and the target's
     ``admin_realm`` when it is set to another realm: the target logs in
     there;
   * the realm behind this backplane's own login (``KEYCLOAK_ISSUER_URL``,
     and ``KEYCLOAK_ADMIN_URL`` when set). Deleting it locks every operator
     out of MEHO. This realm name is refused on every Keycloak target,
     because MEHO cannot tell for sure that a target is a different
     Keycloak.

   Realm names are compared without case, so a guard cannot be skipped by
   changing the case of a letter.
2. **Read the realm.** A realm that does not exist is a success with
   ``already_absent: true``, and no DELETE is sent. A teardown can re-run.
   The name in Keycloak's answer must be exactly the managed realm
   (:func:`read_managed_realm`). An answer with another name, or with no
   name, is refused with the ``realm_name_mismatch`` code, and no DELETE is
   sent. The answer must also have the realm's ``id``, else the delete is
   refused with the ``realm_id_missing`` code.
3. **Delete, then read again.** ``deleted: true`` is reported only after
   ``GET /admin/realms/{realm}`` answers 404. A read that answers with
   something other than a realm object (a list, a string, ``null``) is an
   error, before and after the delete; it is never read as "absent".

The preview builder in
:mod:`~meho_backplane.connectors.keycloak.ops_write_preview` runs steps 1 and
2 before the request waits for approval, so a protected realm, a realm whose
answered name is not the managed realm, or a realm with no id never reaches
an approver. The
approval card shows Keycloak's id of the realm, whether the realm is enabled
and how many users, clients and groups go with it. The handler runs both
steps again when the approved call executes. Before an approved call runs,
MEHO builds the card again and compares the realm id
(:mod:`~meho_backplane.operations.approval_object_check`): a realm that was
deleted and made again under the same name has a new id, so the run is
refused and nothing is deleted. The handler's own read, right before the
DELETE, compares the realm id once more with the id on the approval card.
A call that a delete grant clears (sandbox mode) has no approval card: there
the handler compares with the fresh preview MEHO built for that call. When
the ids differ, or the card has no id, the call is refused with the
``realm_id_changed`` code and no DELETE is sent.

References
----------

* Task: https://github.com/evoila/meho/issues/3886
* Keycloak 26.3 Admin REST API:
  https://www.keycloak.org/docs-api/26.3.3/rest-api/index.html
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

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
from meho_backplane.connectors.keycloak.realm_params import realm_param
from meho_backplane.connectors.keycloak.session import (
    DEFAULT_ADMIN_REALM,
    resolve_realm_config,
)
from meho_backplane.operations._preview import approved_preview, grant_preview
from meho_backplane.targets.keycloak_realm_warnings import (
    backplane_login_realms as _backplane_login_realms,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "REALM_DELETE_OP",
    "check_realm_guards",
    "keycloak_realm_delete",
    "read_managed_realm",
    "realm_blast_radius",
    "realm_name_param",
]

REALM_DELETE_OP: Final = "keycloak.realm.delete"


def realm_name_param(params: dict[str, Any], target: KeycloakTargetLike) -> str:
    """Return the required ``realm`` param: a plain name, and the target's managed realm."""
    realm = realm_param(params, "realm", REALM_DELETE_OP, target)
    if realm is None:
        raise ValueError(f"{REALM_DELETE_OP} requires 'realm' (the name of the realm to delete)")
    return realm


def check_realm_guards(target: KeycloakTargetLike, realm: str) -> None:
    """Refuse the realms this target or this backplane cannot live without."""
    realms = resolve_realm_config(target)
    wanted = realm.casefold()
    reasons = (
        (DEFAULT_ADMIN_REALM, "Keycloak's own admin realm"),
        (realms.admin_realm, "the target's admin realm; the target logs in there"),
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
    return fill_path(_ADMIN_REALM_PATH, {"realm": realm})


async def read_managed_realm(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
) -> dict[str, Any] | None:
    """Read *realm*; ``None`` when Keycloak answers 404.

    The answer's ``realm`` must be exactly the target's managed realm, with
    the same letters, case and accents. Any other name, or no name, raises
    :exc:`KeycloakDeleteRefusedError` (``realm_name_mismatch``). The message
    names the managed realm only.

    The answer's ``id`` must be a string that is not empty. Without it MEHO
    cannot compare the realm with the one on the approval card, so the
    delete is refused (``realm_id_missing``).
    """
    rep = await get_or_none(connector, target, _realm_path(realm), operator)
    if rep is None:
        return None
    managed = resolve_realm_config(target).managed_realm
    if rep.get("realm") != managed:
        raise KeycloakDeleteRefusedError(
            REALM_DELETE_OP,
            "realm_name_mismatch",
            f"Keycloak did not answer with the realm {managed!r}. Nothing was deleted. "
            "Check the managed realm of the target.",
        )
    realm_id = rep.get("id")
    if not isinstance(realm_id, str) or not realm_id.strip():
        raise KeycloakDeleteRefusedError(
            REALM_DELETE_OP,
            "realm_id_missing",
            f"Keycloak answered for the realm {managed!r} without an id, so MEHO cannot "
            "check that it is the realm on the approval card. Nothing was deleted. "
            "The target's login needs the right to view the realm (view-realm).",
        )
    return rep


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
    values = {"realm": realm}
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
    """Approval card for a realm delete: the realm id, enabled, and the counts.

    The id is Keycloak's id of the realm. The check before an approved run
    compares it, so a realm made again under the same name is not deleted.

    Only the id, names and counts; the realm representation (which can carry the
    SMTP password) is never echoed.
    """
    rep = await read_managed_realm(connector, operator, target, realm)
    obj: dict[str, Any] = {
        "kind": "keycloak_realm",
        "resource": "realm",
        "realm": realm,
        "present": rep is not None,
    }
    children: list[dict[str, Any]] = []
    if rep is not None:
        counts = await _count(connector, operator, target, realm)
        obj["id"] = rep["id"]
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


def _check_approved_id(rep: dict[str, Any]) -> None:
    """Refuse a realm whose id is not the id MEHO showed for this call.

    An approved run compares with the approval card. A run under a delete
    grant compares with the fresh preview MEHO built for that call.
    """
    card = approved_preview(REALM_DELETE_OP)
    shown_on = "the approval card"
    if card is None:
        card = grant_preview(REALM_DELETE_OP)
        shown_on = "the preview MEHO built for this call"
    if card is None:
        return
    block = card.get("blast_radius")
    shown = block.get("object") if isinstance(block, dict) else None
    approved_id = shown.get("id") if isinstance(shown, dict) else None
    if not isinstance(approved_id, str) or not approved_id or approved_id != rep["id"]:
        raise KeycloakDeleteRefusedError(
            REALM_DELETE_OP,
            "realm_id_changed",
            f"Keycloak answered with another realm id than the one on {shown_on}. "
            "Nothing was deleted. Send a new request.",
        )


async def keycloak_realm_delete(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Delete one realm (``DELETE /admin/realms/{realm}``). Op-id: ``keycloak.realm.delete``."""
    realm = realm_name_param(params, target)
    check_realm_guards(target, realm)
    result: dict[str, Any] = {"realm": realm}
    rep = await read_managed_realm(self, operator, target, realm)
    if rep is None:
        return {**result, "deleted": False, "already_absent": True}
    _check_approved_id(rep)
    return await _delete_and_verify(
        self, operator, target, op_id=REALM_DELETE_OP, path=_realm_path(realm), result=result
    )
