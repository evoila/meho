# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What happened when an approved request ran (#3543).

After a human approves a request, MEHO runs the operation and stores the
result envelope on ``approval_request.resume_result`` (#3209). The status of
the request stays ``approved`` either way. This module reads a small,
safe summary off that envelope so the approval surfaces (REST list and
show, ``meho approvals``, the ``/ui/approvals`` console) can show whether
the approved change actually landed:

* ``resumed_at`` -- when MEHO started the run (the ``resumed_at`` claim).
* ``result_status`` -- the envelope's ``status`` (``ok`` / ``error`` / ...).
* ``error_code`` -- ``extras["error_code"]`` of a failed run.
* ``audit_id`` -- the audit row the dispatcher wrote for the run.
* ``guidance`` -- the next step a failed multi-step operation names (#5377),
  for example "The VM waits on question _vmx1: ...".

Only these five fields leave the envelope. The result body and the free-text
``error`` stay on the owner-only ``GET /api/v1/approvals/{id}/result`` read.

``guidance`` is MEHO's own words, never vendor text. The approval store
stamps it into ``extras["approval_guidance"]`` when it stores the result
(:func:`with_approval_guidance`), and the summary reads only that stamp. The
stamp is taken from the error answer a multi-step operation wrote itself
(#3809): the result body's ``status`` is the run's ``error_code``. It is
never taken from a vendor answer, never for a run that ended ``ok``, and
never for an operation in a secret class (``credential_read``,
``credential_mint``, ``credential_write``), so no param and no vendor text of
such an operation reaches it (the rule of #4854). A multi-step operation
keeps a vendor error in its ``issues``, not in its ``guidance``. Like every
error result, the text has already passed the dispatcher's secret cleanup
(:func:`~._vendor_text.redact_error_result`).

A result stored before the stamp existed has no ``approval_guidance``, so
the summary shows no guidance for it. Its body may carry a ``guidance``
with vendor text from an older connector, and that text stays on the
owner-only result read.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from meho_backplane.broadcast.events import classify_op
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.redaction.flight_recorder.families import SECRET_OP_CLASSES

__all__ = [
    "ResumeOutcome",
    "resume_outcome",
    "resume_outcome_from_envelope",
    "with_approval_guidance",
]


@dataclass(frozen=True)
class ResumeOutcome:
    """Summary of the run of an approved request. Every field may be ``None``."""

    resumed_at: str | None
    result_status: str | None
    error_code: str | None
    audit_id: uuid.UUID | None
    guidance: str | None = None


#: Longest ``guidance`` kept in the summary, so a list view stays small.
_GUIDANCE_CAP = 500

#: The ``extras`` key the approval store stamps the summary's guidance into.
_GUIDANCE_KEY = "approval_guidance"


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _uuid_or_none(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _extras(data: Mapping[str, Any]) -> Mapping[str, Any]:
    extras = data.get("extras")
    return extras if isinstance(extras, Mapping) else {}


def _sends_or_reads_secret(op_id: object) -> bool:
    """Whether *op_id* is in a secret class. An op id that is not a string counts as yes."""
    return not isinstance(op_id, str) or classify_op(op_id) in SECRET_OP_CLASSES


def _run_guidance(data: Mapping[str, Any], parked_op_id: str) -> str | None:
    """The ``guidance`` of a failed multi-step operation's own error answer, else ``None``."""
    body = data.get("result")
    error_code = _str_or_none(_extras(data).get("error_code"))
    if data.get("status") == "ok" or error_code is None or not isinstance(body, Mapping):
        return None
    if body.get("status") != error_code:
        return None
    if _sends_or_reads_secret(data.get("op_id")) or _sends_or_reads_secret(parked_op_id):
        return None
    text = _str_or_none(body.get("guidance"))
    if text is not None and len(text) > _GUIDANCE_CAP:
        text = text[:_GUIDANCE_CAP] + " [truncated]"
    return text


def with_approval_guidance(envelope: Mapping[str, Any], *, parked_op_id: str) -> dict[str, Any]:
    """Return a copy of *envelope* with the summary's guidance stamped into ``extras``.

    *envelope* is the result the running code just produced
    (``OperationResult.model_dump(mode="json")``). The approval store calls
    this before it stores the result, so the summary can trust the stamp:
    only text that passed the rules in the module docstring gets in, and a
    result stored by older code has no stamp at all.

    *parked_op_id* is the op id of the approval request. It can differ from
    the envelope's ``op_id``: an approved step of a multi-step operation runs
    the whole operation again. When either one is in a secret class, there is
    no stamp.
    """
    extras = {k: v for k, v in _extras(envelope).items() if k != _GUIDANCE_KEY}
    text = _run_guidance(envelope, parked_op_id)
    if text is not None:
        extras[_GUIDANCE_KEY] = text
    return {**envelope, "extras": extras}


def resume_outcome_from_envelope(
    envelope: Mapping[str, Any] | None,
    *,
    resumed_at: str | None = None,
) -> ResumeOutcome:
    """Read the outcome summary off a stored result envelope.

    The envelope is ``OperationResult.model_dump(mode="json")`` as the
    approval store wrote it (see :func:`with_approval_guidance`). A missing
    or odd-shaped field reads as ``None`` instead of raising, because the
    column is plain JSON and older rows may differ. ``guidance`` comes only
    from the stamp, never from the result body.
    """
    data: Mapping[str, Any] = envelope or {}
    extras = _extras(data)
    return ResumeOutcome(
        resumed_at=resumed_at,
        result_status=_str_or_none(data.get("status")),
        error_code=_str_or_none(extras.get("error_code")),
        audit_id=_uuid_or_none(data.get("audit_id")),
        guidance=_str_or_none(extras.get(_GUIDANCE_KEY)),
    )


def resume_outcome(request: ApprovalRequest) -> ResumeOutcome | None:
    """Return the run summary of *request*, or ``None`` when it has not run.

    ``None`` when the request is not ``approved`` or no run has started yet
    (``resumed_at`` unset and no stored result).
    """
    if request.status != ApprovalRequestStatus.APPROVED.value:
        return None
    if request.resume_result is None and request.resumed_at is None:
        return None
    return resume_outcome_from_envelope(
        request.resume_result,
        resumed_at=request.resumed_at.isoformat() if request.resumed_at else None,
    )
