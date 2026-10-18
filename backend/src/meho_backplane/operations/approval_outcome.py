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

Only these four fields leave the envelope. The result body and the free-text
``error`` stay on the owner-only ``GET /api/v1/approvals/{id}/result`` read.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus

__all__ = ["ResumeOutcome", "resume_outcome", "resume_outcome_from_envelope"]


@dataclass(frozen=True)
class ResumeOutcome:
    """Summary of the run of an approved request. Every field may be ``None``."""

    resumed_at: str | None
    result_status: str | None
    error_code: str | None
    audit_id: uuid.UUID | None


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


def resume_outcome_from_envelope(
    envelope: Mapping[str, Any] | None, *, resumed_at: str | None = None
) -> ResumeOutcome:
    """Read the outcome summary off a stored result envelope.

    The envelope is ``OperationResult.model_dump(mode="json")``. A missing
    or odd-shaped field reads as ``None`` instead of raising, because the
    column is plain JSON and older rows may differ.
    """
    data: Mapping[str, Any] = envelope or {}
    extras = data.get("extras")
    error_code = _str_or_none(extras.get("error_code")) if isinstance(extras, Mapping) else None
    return ResumeOutcome(
        resumed_at=resumed_at,
        result_status=_str_or_none(data.get("status")),
        error_code=error_code,
        audit_id=_uuid_or_none(data.get("audit_id")),
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
