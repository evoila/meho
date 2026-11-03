# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved run acts only on the object the approver saw.

A destructive request waits for approval with a card
(``proposed_effect.blast_radius.object``) that names the object: its id or
key, or, for an object that has none, its main fields. Many deletes find
their object by name when they run, and an approved run can happen days
after the request. So right before every approved run, MEHO builds the
card again against the live system and compares the object with the card:

* The card shows an id or key: the live object must have the same one.
* The card shows no id or key (a pfSense alias, a static route, a DNS
  record): the live object must have the same fields. Counts and yes/no
  flags do not count; they change on their own and do not say which
  object it is.
* The live object is gone, or the card does not build now (a builder
  declines when the name matches no object): the run goes ahead, and the
  handler answers as it does for an absent object ("already absent" /
  "not found"), or reports its own error.

Like the preview check in
:mod:`~meho_backplane.operations.approval_resume_checks`, the check runs
for a request that stored a preview hash (every destructive request has
one). A request whose card has no object block is not checked.

Every way an approved request runs calls
:func:`object_changed_since_request`: the approve surfaces and the agent's
own resume through
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

from meho_backplane.operations._preview import BLAST_RADIUS_KEY

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.db.models import ApprovalRequest

__all__ = [
    "IDENTITY_FIELDS",
    "REASON_CHANGED",
    "card_object",
    "live_card",
    "object_changed_since_request",
    "object_identity",
]

_log = structlog.get_logger(__name__)

#: The card fields that name one object on its own. The first one the
#: card has is the identity; its value must be the same at run time.
IDENTITY_FIELDS: Final[tuple[str, ...]] = ("id", "key")

#: Why a run was refused: the live object is not the one on the card.
REASON_CHANGED: Final[str] = (
    "The object this call would change is not the one on the approval card: "
    "it changed after the request was made. The operation did not run. "
    "Send a new request."
)


def card_object(proposed_effect: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The ``blast_radius.object`` block of a card, or ``None`` when it has none.

    The dispatcher stores the block at the top level of the card and inside
    its ``preview`` body; a card built by the preview hook alone has only
    the second. Both places are read.
    """
    if not isinstance(proposed_effect, Mapping):
        return None
    block = proposed_effect.get(BLAST_RADIUS_KEY)
    if not isinstance(block, Mapping):
        preview = proposed_effect.get("preview")
        block = preview.get(BLAST_RADIUS_KEY) if isinstance(preview, Mapping) else None
    if not isinstance(block, Mapping):
        return None
    obj = block.get("object")
    return obj if isinstance(obj, Mapping) else None


def object_identity(obj: Mapping[str, Any]) -> dict[str, Any] | None:
    """What names the one object *obj* describes; ``None`` when the card says it is absent.

    The id or key when the card has one (:data:`IDENTITY_FIELDS`). A card
    that has the field with no value, or ``present: false``, describes an
    object that is not there. A card without such a field names its object
    by its other fields, without counts and yes/no flags.
    """
    if obj.get("present") is False:
        return None
    for field in IDENTITY_FIELDS:
        if field in obj:
            value = obj[field]
            return None if value is None or value == "" else _as_stored({field: value})
    return _as_stored(
        {
            field: value
            for field, value in obj.items()
            if not isinstance(value, bool | int | float) and field != "present"
        }
    )


def _as_stored(identity: dict[str, Any]) -> dict[str, Any]:
    """*identity* as it reads back from the stored card.

    The stored card is JSON, the live card is fresh Python values. One
    round trip through JSON gives both sides the same shape (a tuple reads
    back as a list), so a same object never compares as different.
    """
    normalised: dict[str, Any] = json.loads(json.dumps(identity, default=str))
    return normalised


async def live_card(
    operator: Operator,
    *,
    connector_id: str,
    op_id: str,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the approval card again against the live system; ``None`` when it does not build.

    Resolves the descriptor and the connector the same way the park did,
    so the card is the one the park would store now. A builder that
    declines or fails leaves the card without an object block.
    """
    # The two helpers of the park path, so the live card has the exact
    # shape of the stored one. Imported late: both modules import from the
    # approval queue at load time.
    from meho_backplane.operations._request_preview import _resolve_descriptor
    from meho_backplane.operations.dispatcher import _build_proposed_effect

    resolved = await _resolve_descriptor(
        operator=operator, connector_id=connector_id, op_id=op_id, target=target
    )
    if isinstance(resolved, dict):
        return None
    descriptor, resolved_connector_id = resolved
    return await _build_proposed_effect(
        op_id=op_id,
        connector_id=resolved_connector_id,
        descriptor=descriptor,
        operator=operator,
        target=target,
        params=params,
    )


async def object_changed_since_request(
    operator: Operator,
    request: ApprovalRequest,
    target: Any,
    params: dict[str, Any],
) -> str | None:
    """Why the run must be refused, or ``None`` when the object is the approved one.

    Returns :data:`REASON_CHANGED` when the live object has another
    identity than the card. Returns ``None`` when the request stored no
    preview hash or no card object, when the card already said the object
    is absent, when the live object is gone or the card does not build
    now, and when the identities match.
    """
    if request.preview_hash is None:
        return None
    approved = card_object(request.proposed_effect)
    if approved is None:
        return None
    approved_identity = object_identity(approved)
    if approved_identity is None:
        return None
    live = card_object(
        await live_card(
            operator,
            connector_id=request.connector_id,
            op_id=request.op_id,
            target=target,
            params=params,
        )
    )
    if live is None:
        # The builder declined or failed: the object is gone, or cannot be
        # read now. The handler finds that out itself and answers.
        _log.info(
            "approval_resume_object_card_not_built",
            approval_request_id=str(request.id),
            op_id=request.op_id,
            connector_id=request.connector_id,
        )
        return None
    live_identity = object_identity(live)
    if live_identity is None or live_identity == approved_identity:
        return None
    _log.warning(
        "approval_resume_object_changed",
        approval_request_id=str(request.id),
        op_id=request.op_id,
        connector_id=request.connector_id,
        identity_fields=sorted(approved_identity),
    )
    return REASON_CHANGED
