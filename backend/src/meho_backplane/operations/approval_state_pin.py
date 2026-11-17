# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved run goes ahead only when the object is still as the approver saw it.

Some approval cards show the object as it is now, next to what the call
will change (for example the roles a user has now, next to the roles the
call gives). A connector registers such an operation with
:func:`register_state_pin`, naming the card key that holds the state now.
The card says ``<key>_available: true`` when MEHO could read it, and holds
the state under ``<key>``.

Right before every approved run, :func:`state_changed_since_request`
builds the card again against the live system
(:func:`~meho_backplane.operations.approval_object_check.live_card`) and
compares the state under the key with the approved card:

* The same state: the run goes ahead.
* Another state: the run is refused with :data:`REASON_STATE_CHANGED`.
* The approved card did not show the state (MEHO could not read it, or the
  request was made before the card had it), or MEHO cannot read it now:
  the run is refused with :data:`REASON_STATE_UNREADABLE`. MEHO cannot tell
  that nothing changed, so it never treats an unknown state as the same.

Every way an approved request runs calls the check: the approve surfaces
and the agent's own resume through
:func:`~meho_backplane.operations.approval_queue.refuse_resume_if_preview_changed`,
and the re-run after a failure through
:mod:`~meho_backplane.operations.approval_rerun`. The refusal result is
:func:`~meho_backplane.operations._errors.result_object_changed_since_approval`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.db.models import ApprovalRequest

__all__ = [
    "REASON_STATE_CHANGED",
    "REASON_STATE_UNREADABLE",
    "register_state_pin",
    "state_changed_since_request",
    "state_pin_key",
]

_log = structlog.get_logger(__name__)

#: Why a run was refused: the object is not as the approval card showed it.
REASON_STATE_CHANGED: Final[str] = (
    "The object changed since the approval; send the request again. The operation did not run."
)

#: Why a run was refused: MEHO cannot compare the object with the card.
REASON_STATE_UNREADABLE: Final[str] = (
    "MEHO could not read the object as it is now, or the approval request did not "
    "show it, so MEHO cannot check that it did not change since the approval; send "
    "the request again. The operation did not run."
)

#: op_id -> the card key that holds the object as it is now.
_PINNED: dict[str, str] = {}


def register_state_pin(op_id: str, card_key: str) -> None:
    """Refuse an approved run of *op_id* when the card's *card_key* state changed."""
    _PINNED[op_id] = card_key


def state_pin_key(op_id: str) -> str | None:
    """The card key registered for *op_id*, or ``None`` when it has none."""
    return _PINNED.get(op_id)


def _card_state(preview: Any, key: str) -> tuple[bool, Any]:
    """``(True, state)`` when the card shows the state under *key*, else ``(False, None)``.

    The state goes through JSON once, so the stored card and the fresh one
    have the same shape (a tuple reads back as a list).
    """
    if not isinstance(preview, Mapping) or preview.get(f"{key}_available") is not True:
        return False, None
    if key not in preview:
        return False, None
    return True, json.loads(json.dumps(preview[key], default=str))


def _preview(effect: Any) -> Any:
    return effect.get("preview") if isinstance(effect, Mapping) else None


async def state_changed_since_request(
    operator: Operator,
    request: ApprovalRequest,
    target: Any,
    params: dict[str, Any],
) -> str | None:
    """Why the run must be refused, or ``None`` when the object is as the approver saw it.

    Returns ``None`` at once for an operation with no registered key.
    """
    key = _PINNED.get(request.op_id)
    if key is None:
        return None
    log_fields = {
        "approval_request_id": str(request.id),
        "op_id": request.op_id,
        "connector_id": request.connector_id,
        "card_key": key,
    }
    shown, approved = _card_state(_preview(request.proposed_effect), key)
    if not shown:
        _log.warning("approval_resume_state_not_on_card", **log_fields)
        return REASON_STATE_UNREADABLE
    # Imported late, and read off the module, so the live card is the one
    # the object check uses (both modules import from the approval queue).
    from meho_backplane.operations import approval_object_check

    card = await approval_object_check.live_card(
        operator,
        connector_id=request.connector_id,
        op_id=request.op_id,
        target=target,
        params=params,
    )
    readable, live = _card_state(_preview(card), key)
    if not readable:
        _log.warning("approval_resume_state_unreadable", **log_fields)
        return REASON_STATE_UNREADABLE
    if live != approved:
        _log.warning("approval_resume_state_changed", **log_fields)
        return REASON_STATE_CHANGED
    return None
