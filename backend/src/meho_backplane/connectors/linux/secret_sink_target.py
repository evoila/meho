# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Linux target of a ``linux:`` side of ``secret.move``, as the approval card shows it.

When a ``secret.move`` waits for approval, the card names, for a ``linux:``
side, the target the name points to (``target_id``, ``target_name``), its
``host`` and its SSH ``port`` (:func:`sink_target_card`). The approved run
finds the target again and compares all four with the card
(:func:`check_approved_sink_target`). Another target id, another host,
another port, or a card without one of them refuses the run with
:class:`LinuxTargetChangedError`. The move runs this check before it looks
up the sudo password, before it reads the source and before any SSH call.
So the approved run writes only to the host the approver saw, even when a
name or alias points to another target by then, or the same target got a
new host or port.

An ordinary run (no approval card) checks nothing here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.base import ConnectorOwnMessage
from meho_backplane.operations._preview import approved_preview

if TYPE_CHECKING:
    from meho_backplane.db.models import Target as TargetORM

__all__ = [
    "LinuxTargetChangedError",
    "check_approved_sink_target",
    "sink_target_card",
    "ssh_port",
]

_MOVE_OP_ID: Final = "secret.move"
#: The SSH port MEHO dials when the target names none (the SSH adapter's default).
_DEFAULT_SSH_PORT: Final = 22
_AGAIN: Final = "Nothing was read or written. Send a new request"


class LinuxTargetChangedError(ValueError, ConnectorOwnMessage):
    """The approved run finds another Linux target, host or port than the approval card showed.

    Or the card does not show one of them, so MEHO cannot tell what was
    approved. Raised before any SSH call. MEHO writes the message (the
    target name, id, host and port; never a value), so the caller sees it.
    """


def ssh_port(row: TargetORM) -> int:
    """The SSH port MEHO dials for *row*: its own port, or 22 when it names none."""
    return row.port or _DEFAULT_SSH_PORT


def sink_target_card(row: TargetORM) -> dict[str, str]:
    """What the approval card shows of the Linux target *row*: id, name, host and port."""
    return {
        "target_id": str(row.id),
        "target_name": row.name,
        "host": row.host,
        "port": str(ssh_port(row)),
    }


def check_approved_sink_target(target_name: str, row: TargetORM) -> None:
    """Refuse an approved ``secret.move`` whose Linux target is not the one on its card.

    *target_name* is the name as the ref names it; *row* is the target it
    finds now. No card (an ordinary run) checks nothing.
    """
    card = approved_preview(_MOVE_OP_ID)
    if card is None:
        return
    found = card.get("sink")
    side: dict[str, Any] = found if isinstance(found, dict) else {}
    now = sink_target_card(row)
    for key, label in (("target_id", "target"), ("host", "host"), ("port", "port")):
        approved = side.get(key)
        if not isinstance(approved, str) or not approved:
            raise LinuxTargetChangedError(
                f"the approval does not show the Linux {label} of the sink name "
                f"{target_name!r}, so MEHO cannot tell what was approved. {_AGAIN}"
            )
    if side["target_id"] != now["target_id"]:
        raise LinuxTargetChangedError(
            f"the sink name {target_name!r} now points to Linux target {row.name!r} "
            f"(id {row.id}), not to the target the approval showed "
            f"(id {side['target_id']}). {_AGAIN}"
        )
    for key in ("host", "port"):
        if side[key] != now[key]:
            raise LinuxTargetChangedError(
                f"the {key} of the sink target {row.name!r} changed since the approval "
                f"(the approval showed {side[key]!r}, now it is {now[key]!r}). {_AGAIN}"
            )
