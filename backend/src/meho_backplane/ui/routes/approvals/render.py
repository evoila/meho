# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render helpers for the approvals UI surface.

Initiative #1775 (G10.7 Operator-console hardening), Task #1778. Extended
by Task #1827 (G10.8) when the surface grew from a pending-only modal to a
full-page console with status-filterable decision history. Pulled out of
:mod:`~meho_backplane.ui.routes.approvals.routes` so the projection +
cookie helpers are unit-testable on their own and the route module stays
under the chassis ~600-line cap.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from fastapi.responses import HTMLResponse

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations.approval_outcome import (
    ResumeOutcome,
    resume_outcome,
    resume_outcome_from_envelope,
)
from meho_backplane.operations.approval_queue import (
    EXPIRED_REASON_MESSAGES,
    ApprovalNotFoundError,
    ChainPosition,
    Delegation,
    approval_expired_reason,
    approval_result_state,
    get_request,
    pending_chain_positions,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME
from meho_backplane.ui.references import subject_ref

__all__ = [
    "RESULT_STATE_PILL",
    "STATUS_PILL_CLASS",
    "parked_request_chain",
    "project_outcome",
    "project_request_to_view",
    "project_run_result",
    "set_csrf_cookie",
]

#: DaisyUI badge modifier per request status, for the history view's pill.
#: Pending is the actionable state (warning); approved/rejected are the two
#: terminal decisions (success / error); expired is a quiet timeout
#: (neutral); withdrawn means the sender took it back (#4987), also quiet
#: (neutral). An unknown status falls back to a plain ghost badge rather
#: than raising, so a future status value degrades to a visible pill
#: instead of a ``StrictUndefined`` template error.
STATUS_PILL_CLASS: Final[dict[str, str]] = {
    "pending": "badge-warning",
    "approved": "badge-success",
    "rejected": "badge-error",
    "expired": "badge-neutral",
    "withdrawn": "badge-neutral",
}


#: Label + DaisyUI badge modifier per run state of an approved request
#: (#3543). Shown next to the ``approved`` pill so "approved, and the run
#: failed" reads differently from "approved, and the change landed".
RESULT_STATE_PILL: Final[dict[str, tuple[str, str]]] = {
    "not_started": ("not run yet", "badge-ghost"),
    "started": ("running", "badge-info"),
    "executed": ("ran OK", "badge-success"),
    "failed": ("run failed", "badge-error"),
}


def project_outcome(outcome: ResumeOutcome | None) -> dict[str, Any] | None:
    """Project a run summary (#3543) into a template dict, with an audit link."""
    if outcome is None:
        return None
    audit_id = str(outcome.audit_id) if outcome.audit_id else None
    return {
        "resumed_at": outcome.resumed_at,
        "result_status": outcome.result_status,
        "error_code": outcome.error_code,
        "audit_id": audit_id,
        "audit_href": f"/ui/audit?audit_id={audit_id}" if audit_id else None,
    }


def project_run_result(result: Any) -> dict[str, Any] | None:
    """Project the result of the run that an approve just started (#3543).

    Used by the decision-outcome fragment, which renders right after the
    run returns. The request row in hand was read before the run, so the
    outcome is read off the returned result, not off the row.

    Returns ``None`` when another resumer (usually the waiting requester)
    already ran the operation (``already_resumed``). That is not a failure,
    so the fragment shows the normal "Request approved" card.
    """
    if getattr(result, "status", None) == "already_resumed":
        return None
    outcome = resume_outcome_from_envelope(
        {
            "status": getattr(result, "status", None),
            "extras": getattr(result, "extras", None),
            "audit_id": getattr(result, "audit_id", None),
        }
    )
    return {
        "result_state": "executed" if outcome.result_status == "ok" else "failed",
        "outcome": project_outcome(outcome),
    }


def project_request_to_view(
    row: ApprovalRequest,
    delegation: Delegation | None = None,
    chain: ChainPosition | None = None,
) -> dict[str, Any]:
    """Project an :class:`ApprovalRequest` ORM row into a template dict.

    The templates render only the operator-relevant subset: the pending
    detail fields (op_id, connector_id, ``proposed_effect``, requester
    ``principal_sub``, created_at) plus the decided-row shape the history
    view + decided-modal banner need (``status``, ``reviewed_by``,
    ``decided_at``, ``expires_at``, ``work_ref``, ``principal_act``) and the
    id (for the form action URLs). Datetimes are pre-formatted to ISO
    strings here so the templates never touch the ORM row (which carries
    lazy-load + secret-bearing fields).

    The ``requester`` / ``reviewer`` keys carry the display-name resolution
    (#3300): a :class:`~meho_backplane.ui.references.SubjectRef` pairing the
    ``sub`` with the name captured on the row at write time, so the
    ``_subject`` macro renders the human name **alongside** the GUID
    (fail-open to the GUID when no name was recorded). ``reviewer`` is
    ``None`` for an undecided request. The raw ``principal_sub`` /
    ``reviewed_by`` keys stay projected so the ``sub`` remains directly
    reachable and pre-existing consumers are unaffected.

    The internal ``params`` / ``params_hash`` columns are **never**
    projected: they are the swap-defence + re-dispatch input the approvals
    contract keeps off every read view and broadcast frame
    (``docs/codebase/approvals.md``). The status pill class is resolved by
    the template from :data:`STATUS_PILL_CLASS`, not baked in here, so the
    projection stays presentation-free.

    ``delegation`` (#3692) is the delegated-decision block -- who instructed
    the approver, their words, and the refs -- or ``None``. It is read off
    the decision audit row (no column), so only the callers that loaded it
    pass it; the key is always present so the templates stay
    ``StrictUndefined``-safe.

    ``withdrawn_at`` / ``withdraw_reason`` (#4987) say when and why the
    sender withdrew the request; both are ``None`` on any other row.

    ``result_state`` and ``outcome`` (#3543) say whether an approved request
    ran and how it ended. Both are ``None`` for a request that is not
    approved; ``outcome`` is also ``None`` before the run starts.

    ``chain`` (#3596) is ``{"work_ref", "step", "total"}`` when the request
    is step ``step`` of ``total`` pending requests under one change ticket,
    else ``None``. Only the callers that computed it pass it.

    ``expired_reason`` / ``expired_message`` (#5030) say why an ``expired``
    request expired (its deadline passed, or MEHO was updated while it
    waited) and tell the requester to ask again; ``release_id`` is the
    release the request was made under. The reason keys are ``None`` for a
    request that is not expired.
    """
    result_state = approval_result_state(row)
    expired_reason = approval_expired_reason(row)
    return {
        "id": str(row.id),
        "op_id": row.op_id,
        "connector_id": row.connector_id,
        "principal_sub": row.principal_sub,
        "principal_act": row.principal_act,
        "requester": subject_ref(row.principal_sub, row.principal_name),
        "reviewer": subject_ref(row.reviewed_by, row.reviewed_by_name),
        "proposed_effect": row.proposed_effect,
        "status": row.status,
        "reviewed_by": row.reviewed_by,
        "work_ref": row.work_ref,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "withdrawn_at": row.withdrawn_at.isoformat() if row.withdrawn_at else None,
        "withdraw_reason": row.withdraw_reason,
        "delegation": delegation.to_payload() if delegation is not None else None,
        "result_state": result_state.value if result_state is not None else None,
        "outcome": project_outcome(resume_outcome(row)),
        "chain": _project_chain(chain),
        "expired_reason": expired_reason.value if expired_reason else None,
        "expired_message": EXPIRED_REASON_MESSAGES[expired_reason] if expired_reason else None,
        "release_id": row.release_id,
    }


def _project_chain(chain: ChainPosition | None) -> dict[str, Any] | None:
    """Project a :class:`ChainPosition` into the ``chain`` template dict (#3596)."""
    if chain is None:
        return None
    return {"work_ref": chain.work_ref, "step": chain.step, "total": chain.total}


async def parked_request_chain(
    tenant_id: uuid.UUID, envelope: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Find the step of a just-parked request in its change ticket (#3596).

    A "waiting for approval" result fragment (operations run, vault write)
    calls this with the ``call_operation`` envelope. For an
    ``awaiting_approval`` envelope it loads the parked request named by
    ``extras.approval_request_id`` and numbers it among the pending
    requests of its ``work_ref``. Returns ``{"work_ref", "step", "total"}``
    when that change ticket has two or more pending requests, so the
    fragment can say "Step k of N" and link to the ticket's oldest-first
    view. Returns ``None`` for any other envelope, for a request with no
    ``work_ref`` or no pending siblings, and for an id that is missing or
    outside *tenant_id* (fail-open: the fragment keeps its per-request
    link).
    """
    if not envelope or envelope.get("status") != "awaiting_approval":
        return None
    raw_id = (envelope.get("extras") or {}).get("approval_request_id")
    if not raw_id:
        return None
    try:
        request_id = uuid.UUID(str(raw_id))
    except ValueError:
        return None
    async with get_sessionmaker()() as db_session:
        try:
            row = await get_request(db_session, tenant_id=tenant_id, request_id=request_id)
        except ApprovalNotFoundError:
            return None
        if row.work_ref is None:
            return None
        positions = await pending_chain_positions(
            db_session, tenant_id=tenant_id, work_ref=row.work_ref
        )
    return _project_chain(positions.get(row.id))


def set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the ``meho_csrf`` double-submit cookie on *response*.

    Mirrors the SameSite=Strict + Secure + non-HttpOnly posture every UI
    surface's CSRF cookie carries (HTMX must read it to populate
    ``X-CSRF-Token``; the HMAC binding to the session id defeats the
    cookie-injection vector JS-read would otherwise open). The cookie value
    MUST equal the token the rendered modal echoes via ``hx-headers`` -- the
    CSRF middleware rejects a mismatch.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
