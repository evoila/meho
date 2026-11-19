# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Keycloak target of a ``keycloak:`` side of ``secret.move``, as the approval card shows it.

When a ``secret.move`` waits for approval, the card names, for each
``keycloak:`` side (a user password sink or a client secret source), the
target the name points to (``target_id``, ``target_name``), its ``host``
and the reference of its admin login (``secret_ref``)
(:func:`keycloak_side_card`). The approved run finds the target again and
compares the id, the host and the secret reference with the card
(:func:`check_approved_keycloak_target`). Another target id, another host,
another secret reference, or a card without one of them refuses the run
with :class:`KeycloakTargetChangedError`. The check runs before any
Keycloak login. So the approved run works only on the Keycloak target the
approver saw, even when a name or alias points to another target by then,
or the same target got a new host or admin login.

An ordinary run (no approval card) checks nothing here.

This module imports nothing of the secret broker package, so the broker
and the Keycloak store can both import it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.base import ConnectorOwnMessage
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations._preview import approved_preview
from meho_backplane.targets.resolver import (
    AmbiguousTargetError,
    TargetNotFoundError,
    resolve_target,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.db.models import Target as TargetORM

__all__ = [
    "KeycloakTargetChangedError",
    "check_approved_keycloak_target",
    "keycloak_side_card",
    "keycloak_target_card",
]

_MOVE_OP_ID: Final = "secret.move"
_AGAIN: Final = "Nothing was read or written. Send a new request"


class KeycloakTargetChangedError(ValueError, ConnectorOwnMessage):
    """The approved run finds another Keycloak target, host or admin login than the card showed.

    Or the card does not show one of them, so MEHO cannot tell what was
    approved. Raised before any Keycloak login. MEHO writes the message
    (the target name, id and host; never a value), so the caller sees it.
    """


def keycloak_target_card(row: TargetORM) -> dict[str, str]:
    """What the approval card shows of the Keycloak target *row*: id, name, host and secret ref.

    A target with no secret reference shows an empty ``secret_ref``.
    """
    return {
        "target_id": str(row.id),
        "target_name": row.name,
        "host": row.host,
        "secret_ref": row.secret_ref or "",
    }


async def keycloak_side_card(target_name: str, operator: Operator) -> dict[str, str]:
    """The Keycloak target the name *target_name* points to now, as the card shows it.

    Empty when the name finds no target (or more than one). The approved
    run then finds no target id on the card and refuses.
    """
    try:
        async with get_sessionmaker()() as session:
            row = await resolve_target(session, operator.tenant_id, target_name)
    except (TargetNotFoundError, AmbiguousTargetError):
        return {}
    return keycloak_target_card(row)


def check_approved_keycloak_target(side_name: str, target_name: str, row: TargetORM) -> None:
    """Refuse an approved ``secret.move`` whose Keycloak target is not the one on its card.

    *side_name* is ``"source"`` or ``"sink"``; *target_name* is the name as
    the ref names it; *row* is the target it finds now. No card (an
    ordinary run) checks nothing.
    """
    card = approved_preview(_MOVE_OP_ID)
    if card is None:
        return
    found = card.get(side_name)
    side: dict[str, Any] = found if isinstance(found, dict) else {}
    now = keycloak_target_card(row)
    approved_id = side.get("target_id")
    if not isinstance(approved_id, str) or not approved_id:
        raise KeycloakTargetChangedError(
            f"the approval does not show which Keycloak target the {side_name} name "
            f"{target_name!r} pointed to, so MEHO cannot tell what was approved. {_AGAIN}"
        )
    for key, label in (("host", "host"), ("secret_ref", "admin login reference")):
        if not isinstance(side.get(key), str):
            raise KeycloakTargetChangedError(
                f"the approval does not show the Keycloak {label} of the {side_name} name "
                f"{target_name!r}, so MEHO cannot tell what was approved. {_AGAIN}"
            )
    if approved_id != now["target_id"]:
        raise KeycloakTargetChangedError(
            f"the {side_name} name {target_name!r} now points to Keycloak target "
            f"{row.name!r} (id {row.id}), not to the target the approval showed "
            f"(id {approved_id}). {_AGAIN}"
        )
    if side["host"] != now["host"]:
        raise KeycloakTargetChangedError(
            f"the host of the {side_name} target {row.name!r} changed since the approval "
            f"(the approval showed {side['host']!r}, now it is {now['host']!r}). {_AGAIN}"
        )
    if side["secret_ref"] != now["secret_ref"]:
        raise KeycloakTargetChangedError(
            f"the admin login reference of the {side_name} target {row.name!r} changed "
            f"since the approval. {_AGAIN}"
        )
