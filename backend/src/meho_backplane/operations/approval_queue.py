# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pre-existing >600-line durable approval
# queue module; #3290 is a surgical guard fix, not a split.

"""Durable approval queue for ``requires_approval`` dispatches.

Initiative #803 (G11.2 Agent permission model), Task #817 (T4). When the
G11.2-T3 policy gate resolves
:attr:`~meho_backplane.db.models.PermissionVerdict.NEEDS_APPROVAL` for a
dispatch (an agent principal on a ``requires_approval`` / caution /
dangerous op), the dispatcher creates an
:class:`~meho_backplane.db.models.ApprovalRequest` row (via
:func:`create_pending_request`) instead of executing the op. The row
parks the call durably so a process restart cannot lose the pending
request. Two REST endpoints (``/api/v1/approvals/{id}/approve`` and
``…/reject``) let authorized reviewers decide; approval re-dispatches the
original call with the original params
(``dispatch(..., _approved=True)``). The approval is not asked for again;
the run checks again what can change while the call waits
(:mod:`~meho_backplane.operations.approved_run_rights`).

Audit invariant
---------------

Every mutation writes a synchronous audit row in the **same transaction**:

* :func:`create_pending_request` writes a ``"request"`` audit row
  (``method='APPROVAL'``, ``path='approval.request'``) alongside the
  pending :class:`~meho_backplane.db.models.ApprovalRequest` insert.
* :func:`approve_request` / :func:`reject_request` /
  :func:`expire_stale_requests` write a ``"decision"`` audit row
  (``path='approval.decision'``) inside the same transaction as the
  status update. The decision row does **not** land until the status
  commit succeeds — mirroring the dispatcher's synchronous-audit
  invariant. An approval isn't "granted" until its decision row
  commits.

Session-replay lineage (#2086)
------------------------------

Every lifecycle audit row is linked into the G8.2 session-replay graph.
The parked row persists ``agent_session_id`` (resolved at creation on
the requester's task) and ``request_audit_id`` (the "request" audit
row's pre-generated id); :func:`_write_audit_row` stamps both onto the
rows it writes, and :func:`resume_dispatch_after_approval` re-binds them
(plus ``work_ref``) around the re-dispatch. The result is one replay
subtree — the ``approval.request`` row anchored on the originating
session, with the decision row and the executed dispatch's row as its
children — where previously every row in the chain was invisible to
``GET /api/v1/audit/sessions/{id}/replay``.

Transaction discipline
----------------------

Every mutating function takes an open
:class:`~sqlalchemy.ext.asyncio.AsyncSession`, flushes its changes, and
returns — the **caller** owns the commit. This lets the dispatcher
compose the approval-row insert with the audit row in one transaction.
:func:`expire_stale_requests` follows the same contract; the CLI / task
scheduler calls it inside its own transaction.

Resume path
-----------

:func:`approve_request` re-dispatches the original call by calling
:func:`~meho_backplane.operations.dispatcher.dispatch` with the stored
``connector_id`` / ``op_id`` / ``target_id`` / ``params``. The caller
must pass the **original params** (not a re-hash); the service re-hashes
them and rejects if the hash does not match the stored ``params_hash``
(:class:`ParamsMismatchError`). This guards against a substitution attack
where a malicious approver swaps the params between the "request" and the
"approve" call.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Literal, NamedTuple, cast

import structlog
from sqlalchemy import CursorResult, and_, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.approval_sign_in import ApprovalAuth, approval_sign_in_max_age
from meho_backplane.auth.client_identity import (
    CLIENT_PAYLOAD_KEY,
    apply_client_block,
    build_client_block,
    channel_of,
    clean_client_text,
    client_identity_enabled,
)
from meho_backplane.auth.current_tenant import tenant_role_source_payload
from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.display_name import principal_display_name
from meho_backplane.auth.operator import Operator, TenantRole, is_human_principal
from meho_backplane.auth.principal_block import checking_logins_for_steps
from meho_backplane.auth.secret_read_as import current_secret_read_as
from meho_backplane.auth.sign_in_use import SIGN_IN_LOCK_SCOPE, earlier_sign_in_uses
from meho_backplane.auth.tenant_admins import only_admin_note
from meho_backplane.broadcast.events import (
    HIDDEN_PARAM_MARKER,
    classify_op,
    holds_hidden_param_values,
    scrub_secret_named_values,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestKey,
    ApprovalRequestStatus,
    AuditEventClass,
    AuditLog,
)
from meho_backplane.op_classes import (
    SECRET_REQUEST_OP_CLASSES,
    hidden_stored_map_values,
    hidden_stored_param,
)
from meho_backplane.operations._audit import (
    SESSION_FROM_CLIENT_KEY,
    agent_session_id_var,
    client_session_id_var,
    parent_audit_id_var,
    policy_decision_var,
    policy_rule_columns,
    policy_rule_payload,
    policy_rule_var,
    resolve_audit_session_id,
    run_id_var,
    session_is_from_client,
    step_id_var,
    work_ref_var,
)
from meho_backplane.operations._errors import (
    result_already_resumed,
    result_object_changed_since_approval,
    result_preview_changed_since_approval,
    result_release_changed_while_waiting,
    result_resume_params_unavailable,
    result_target_changed_since_approval,
    result_target_gone_since_approval,
)
from meho_backplane.operations._principal_block_gate import refuse_if_principal_blocked
from meho_backplane.operations._secret_values import split_reveal_secret, with_reveal_secret
from meho_backplane.operations._target_connector_limit import (
    restored_connector_limit,
    stored_connector_limit,
)
from meho_backplane.operations._validate import (
    TOKEN_CALL_WAIT,
    compute_audit_params_hash,
    params_hash_candidates,
    params_hash_matches,
)
from meho_backplane.operations.approval_handoff import (
    ApprovalHandoffError,
)
from meho_backplane.operations.approval_handoff import (
    consume as consume_handoff,
)
from meho_backplane.operations.approval_handoff import (
    discard as discard_handoff,
)
from meho_backplane.operations.approval_handoff import (
    store as store_handoff,
)
from meho_backplane.operations.approval_object_check import object_changed_since_request
from meho_backplane.operations.approval_outcome import with_approval_guidance
from meho_backplane.operations.approval_resume_checks import (
    park_time_target_hash,
    preview_changed_since_request,
    target_changed_since_request,
)
from meho_backplane.operations.approval_state_pin import state_changed_since_request
from meho_backplane.operations.approved_reveal import (
    is_secret_reveal,
    result_waiting_for_requester,
    without_secret_values,
)
from meho_backplane.operations.grant_approval import (
    ASKED_IN_WEB_CONSOLE,
    RIGHTS_CHANGE_REFUSAL,
    check_grant_approval,
    check_rights_change_approval,
    grant_audit_fields,
    grant_changed_since_request,
    grant_facts,
    is_rights_change,
)
from meho_backplane.operations.people_behind import (
    behind_subs,
    find_people_behind,
    is_person_behind,
    people_behind_unknown,
    whys_of,
)
from meho_backplane.operations.reducer import handle_owner_sub_var
from meho_backplane.operations.role_floors import role_floor_denial
from meho_backplane.operations.shutdown_drain import is_draining, shutting_down_result
from meho_backplane.operations.uncertain_outcome import envelope_is_uncertain
from meho_backplane.policy.routing import (
    ROUTING_INVALID_RULE,
    group_names,
    narrow,
    routing_enabled,
    routing_refusal,
    stamp_approver_set,
    stored_kinds,
    today_kinds,
)
from meho_backplane.policy.routing_floor import load_install_floor
from meho_backplane.version import running_release_id
from meho_backplane.work_ref import work_ref_filter_values

__all__ = [
    "EXPIRED_REASON_MESSAGES",
    "EXPIRY_OPERATOR_SUB",
    "MAX_REASON_LENGTH",
    "ApprovalError",
    "ApprovalExpiredReason",
    "ApprovalGroupForbiddenError",
    "ApprovalListOrder",
    "ApprovalNotFoundError",
    "ApprovalRequestAlreadyDecidedError",
    "ApprovalRequestExpiredError",
    "ApprovalResultState",
    "ApprovalRoleFloorError",
    "ApprovalRoutingError",
    "ApprovalRule",
    "ApprovalSignInReusedError",
    "ApprovalSignInTooOldError",
    "ChainPosition",
    "DelegatedDecisionForbiddenError",
    "DelegatedReasonInvalidError",
    "Delegation",
    "IdempotencyKeyConflictError",
    "NonHumanApprovalError",
    "ParamsMismatchError",
    "PreviewBindingMissingError",
    "ResultAccessForbiddenError",
    "RightsChangeApprovalError",
    "SelfApprovalForbiddenError",
    "UnauthorizedApprovalError",
    "UnknownRequesterApprovalError",
    "WithdrawForbiddenError",
    "approval_expired_reason",
    "approval_release_changed",
    "approval_result_state",
    "approval_risk_level",
    "approve_request",
    "claim_resume",
    "composite_resume_binding",
    "consume_remote_write_approval",
    "create_pending_request",
    "expire_older_release_requests",
    "expire_stale_requests",
    "expiry_operator",
    "find_remote_write_approval",
    "get_decision_delegation",
    "get_request",
    "is_admin_approver",
    "is_high_risk_level",
    "is_self_approval",
    "list_pending",
    "may_approve_high_risk",
    "parse_delegation",
    "pending_chain_positions",
    "read_approval_result",
    "record_approval_step_event",
    "refuse_resume_if_asker_blocked",
    "refuse_resume_if_preview_changed",
    "refuse_resume_if_release_changed",
    "refuse_resume_if_target_changed",
    "rehydrate_resume_target",
    "reject_request",
    "request_waits_only_for_token_call",
    "result_state_of_envelope",
    "resume_dispatch_after_approval",
    "rights_change_note",
    "take_approval_lock",
    "withdraw_request",
]


def _log() -> Any:
    # Resolved per call, not held as a module-level proxy. Production sets
    # ``cache_logger_on_first_use=True`` in ``configure_logging``, and a
    # cached logger keeps the processor list it was built with. A later
    # ``structlog.configure(...)`` replaces that list, so test capture such
    # as ``structlog.testing.capture_logs`` cannot reach a cached logger.
    # Same precedent as ``meho_backplane.auth.jwt``.
    return structlog.get_logger(__name__)


#: Synthetic status code used for approval audit rows. Mirrors the
#: ``status_code_for_result`` convention: ``202`` means "accepted / pending".
_APPROVAL_STATUS_CODE: int = 202
_DECISION_STATUS_CODE_APPROVED: int = 200
_DECISION_STATUS_CODE_REJECTED: int = 403
_DECISION_STATUS_CODE_EXPIRED: int = 410
#: Audit ``status_code`` for an approved run MEHO refused before it ran:
#: MEHO was updated while the request waited (#5030), or the target changed
#: or no longer resolves (:func:`refuse_resume_if_target_changed`).
_RESUME_REFUSED_STATUS_CODE: int = 409
#: The audit status of a park refused because the stored routing is not valid.
_ROUTING_REFUSED_STATUS_CODE: int = 409

#: Who may approve another person's ``dangerous`` or ``destructive`` change,
#: in words for the refusal texts. The rule itself is in
#: :func:`_check_approver_group`; the groups come from the decision record
#: ``docs/decisions/approval-groups-and-self-approval.md`` (#3325).
_HIGH_RISK_APPROVERS_TEXT: Final[str] = "a MEHO-Admin"


class ApprovalRule(StrEnum):
    """The rule that allowed an approval (#3328).

    Recorded as ``approval_rule`` on the ``approval.decision`` audit row
    and on the ``approval.approved`` step event, next to ``self_approved``.

    * ``sa_operator_self_approval`` — an Admin (MEHO-Admins) approved
      their own change. The stored value keeps its name.
    * ``break_glass_self_approval`` — the requester approved their own
      ``safe`` or ``caution`` change under ``APPROVAL_ALLOW_SELF_APPROVAL``.
    * ``sa_operator_or_admin`` — another person's ``dangerous`` or
      ``destructive`` change, approved by a person in a group that may
      approve it.
    * ``reviewer`` — another person's lower-risk change, approved under the
      reviewer rule of :func:`_check_reviewer_role`.
    * ``token_call_self_approval`` — a person approved, with a click in the
      web interface, their own ``caution`` change that waited only because
      it came with a token (#3980).
    """

    SA_OPERATOR_SELF_APPROVAL = "sa_operator_self_approval"
    BREAK_GLASS_SELF_APPROVAL = "break_glass_self_approval"
    SA_OPERATOR_OR_ADMIN = "sa_operator_or_admin"
    REVIEWER = "reviewer"
    TOKEN_CALL_SELF_APPROVAL = "token_call_self_approval"


class ApprovalError(Exception):
    """Base class for approval queue failures."""


class ApprovalNotFoundError(ApprovalError):
    """No :class:`ApprovalRequest` row exists for the requested id.

    Raised by :func:`approve_request` / :func:`reject_request` when the id
    does not resolve or belongs to a different tenant. The two cases are
    indistinguishable to callers (tenant isolation); the route layer maps
    this to 404.
    """

    def __init__(self, request_id: uuid.UUID) -> None:
        self.request_id = request_id
        super().__init__(f"no approval_request row for id {request_id}")


class ApprovalRequestAlreadyDecidedError(ApprovalError):
    """The approval request is already in a terminal state.

    Raised when :func:`approve_request` / :func:`reject_request` is
    called on a row that is not ``pending``. The route layer maps this to
    409 (conflict).
    """

    def __init__(self, request_id: uuid.UUID, status: str) -> None:
        self.request_id = request_id
        self.status = status
        super().__init__(f"approval_request {request_id} is already in terminal state {status!r}")


class ApprovalRequestExpiredError(ApprovalError):
    """The pending request's deadline has passed; it cannot be approved.

    Raised by :func:`approve_request` when a still-``pending`` row is past
    its ``expires_at`` deadline at decision time (security F12 / #274). The
    deadline is re-checked on the approve path rather than trusting the
    background expiry sweep (:mod:`~meho_backplane.operations.approval_expiry`),
    whose default cadence is 300s and whose per-tick failures are
    swallowed to keep the loop alive: an overdue intent must be
    un-approvable *immediately*, not only after the next successful sweep.
    The row is left ``pending`` for the sweeper to transition to
    ``expired`` with its decision audit row — this exception only refuses
    the approval. The route layer maps it to 409 (conflict), the same
    class as an already-decided row.
    """

    def __init__(self, request_id: uuid.UUID) -> None:
        self.request_id = request_id
        super().__init__(f"approval_request {request_id} deadline has passed")


class ParamsMismatchError(ApprovalError):
    """The params hash does not match the stored hash on the request.

    Raised by :func:`approve_request` when the caller-supplied params
    hash against a value that differs from the stored
    :attr:`~meho_backplane.db.models.ApprovalRequest.params_hash`. This
    guards against param-substitution between queue and approve time.
    The route layer maps this to 422.
    """

    def __init__(self, request_id: uuid.UUID) -> None:
        self.request_id = request_id
        super().__init__(
            f"params hash mismatch on approval_request {request_id}; "
            "original params must be supplied unchanged"
        )


class IdempotencyKeyConflictError(ApprovalError):
    """An ``idempotency_key`` is already used by a different call (#3486).

    Raised by :func:`create_pending_request` when the same principal sends a
    key that a live (``pending`` or ``approved``) request already holds, but
    the new call is not the same call: a different ``op_id``,
    ``connector_id``, target, params, ``preview_hash`` or ``work_ref``.
    Returning the old request would tell the caller that the wrong call is
    waiting for approval, so the new call is refused and nothing is parked.
    """

    def __init__(self, request_id: uuid.UUID) -> None:
        self.request_id = request_id
        super().__init__(
            f"idempotency_key is already used by approval_request {request_id} "
            "for a different call; send the identical call to get that request "
            "back, or use a new key"
        )


class PreviewBindingMissingError(ApprovalError):
    """A ``destructive`` request is missing its preview-result-hash binding.

    Raised by :func:`approve_request` (via :func:`_load_pending_for_approval`)
    when a ``destructive``-tier request — identified by
    ``proposed_effect.safety_level == "destructive"`` — carries no
    ``preview_hash``. The dispatcher refuses to *park* a destructive op
    without a verified binding (#3197, decision requirement 2), so a
    well-formed pending row always has one; this is the fail-closed
    re-verification at *approve* time, extending the existing
    :class:`ParamsMismatchError` swap-defence to the preview binding. The
    route layer maps it to 422. It fires only if a destructive row reached
    the queue through some path that bypassed the dispatcher's park gate —
    the approval is refused rather than clearing an unbound destructive op.
    """

    def __init__(self, request_id: uuid.UUID) -> None:
        self.request_id = request_id
        super().__init__(
            f"approval_request {request_id} is destructive-tier but carries no "
            "preview_hash binding; approval refused (the preview-result-hash "
            "binding is mandatory for the destructive tier, #3197)"
        )


class ApprovalSignInReusedError(ApprovalError):
    """This sign-in cannot give this approval: it is used up (#3979, #5515).

    Raised by :func:`approve_request` in two cases. Both are read from the
    person's earlier audit rows with the same ``sign_in_key``: the
    ``approval.decision`` rows, and the ``principal.unblock`` row of an
    own-login unblock:

    * the request is ``destructive`` and the sign-in already approved
      something or removed the person's own block: a destructive approve
      needs a sign-in of its own;
    * the sign-in already approved a ``destructive`` request: that sign-in
      approves nothing more.

    The REST layer answers ``401`` with the same fresh sign-in challenge as
    a too-old sign-in, so the command line asks for a new sign-in.
    """

    def __init__(self, request_id: uuid.UUID) -> None:
        self.request_id = request_id
        super().__init__(
            f"approval_request {request_id}: this sign-in was already used; "
            "a destructive operation needs a sign-in of its own"
        )


class ApprovalSignInTooOldError(ApprovalError):
    """The sign-in is too old for this request (#5515).

    Each risk level has its own limit
    (:func:`~meho_backplane.auth.approval_sign_in.approval_sign_in_max_age`):
    the approval window ``FRESH_SIGN_IN_MAX_AGE_SECONDS``, or for a
    ``destructive`` request ``PER_ACTION_SIGN_IN_MAX_AGE_SECONDS``.
    ``max_age_seconds`` is the limit that applied. The REST layer answers
    ``401`` with the fresh sign-in challenge and that ``max_age``.
    """

    def __init__(self, request_id: uuid.UUID, *, max_age_seconds: int) -> None:
        self.request_id = request_id
        self.max_age_seconds = max_age_seconds
        super().__init__(
            f"approval_request {request_id}: the sign-in is older than "
            f"{max_age_seconds} seconds; approving needs a newer one"
        )


class SelfApprovalForbiddenError(ApprovalError):
    """The approver is the same principal that requested the approval.

    Raised by :func:`approve_request` when
    ``operator.sub == request.principal_sub``, the person is not in the
    MEHO-Admins group
    (:attr:`~meho_backplane.auth.operator.Operator.may_self_approve`), and
    the audited single-operator break-glass mode
    (``Settings.approval_allow_self_approval``) does not cover the change.
    Break-glass never covers a ``dangerous`` or ``destructive`` change
    (*high_risk*). The route layer maps this to 403.

    The message says who can approve instead and where to ask: the groups
    in *approver_kinds* (:func:`_approver_kinds`), on the Approvals page.
    The text names no setting and no other way to approve the change.

    Reject is deliberately *not* guarded — an operator withdrawing their
    own pending request is not a privilege escalation.
    """

    def __init__(
        self,
        request_id: uuid.UUID,
        *,
        principal_sub: str,
        approver_kinds: Sequence[str],
        high_risk: bool = False,
        web_click_allowed: bool = False,
        behind: bool = False,
    ) -> None:
        self.request_id = request_id
        self.principal_sub = principal_sub
        self.high_risk = high_risk
        self.behind = behind
        if web_click_allowed:
            # #3980: the change waits only because it came with a token.
            who_can = (
                "This change waits only because it came with a token, so you may "
                "approve it yourself with a click in the web interface."
            )
        elif approver_kinds:
            who_can = (
                f"Ask another person in {group_names(approver_kinds)} to approve it. "
                "They find it on the Approvals page of the web interface."
            )
        else:
            who_can = "No group may approve this change, so it waits until it expires."
        #: The plain refusal text, without ids, for the web interface.
        if behind:
            self.reason_text = (
                "You cannot approve this change. You set up, changed or started the "
                "agent, add-on or runner that asked for it, so it counts as your own "
                f"change. {who_can}"
            )
        else:
            self.reason_text = (
                "You cannot approve your own change. Only members of the "
                f"MEHO-Admins group may approve their own changes. {who_can}"
            )
        super().__init__(
            f"operator {principal_sub!r} may not approve approval_request "
            f"{request_id}: {self.reason_text}"
        )


class UnauthorizedApprovalError(ApprovalError):
    """The operator lacks the access required to approve / reject a request.

    Raised when the operator's ``tenant_role`` is ``read_only`` **and** it
    does not carry the orthogonal ``approver`` capability (#3243). An
    ``operator``-or-higher principal, or a ``read_only`` principal with
    ``approver=true``, passes. The route layer maps this to 403.
    """

    def __init__(self, *, operator_sub: str, role: str, message: str | None = None) -> None:
        self.operator_sub = operator_sub
        self.role = role
        super().__init__(
            message
            if message is not None
            else (
                f"operator {operator_sub!r} with role {role!r} may not "
                "approve/reject an approval request (requires at least 'operator')"
            )
        )


class ApprovalGroupForbiddenError(UnauthorizedApprovalError):
    """The person may not approve another person's high-risk change (#3328).

    Raised by :func:`approve_request` when the request is another person's
    ``dangerous`` or ``destructive`` change and the approver is not in a
    group that may approve it. The groups are those of the decision record
    ``docs/decisions/approval-groups-and-self-approval.md``. Subclasses
    :class:`UnauthorizedApprovalError`, so every handler of that error still
    answers 403; the approve routes catch this one first to show its text.
    *reason* replaces that text (:class:`ApprovalRoutingError` passes its own).
    """

    def __init__(
        self,
        request_id: uuid.UUID,
        *,
        operator_sub: str,
        role: str,
        safety_level: str,
        reason: str | None = None,
    ) -> None:
        self.request_id = request_id
        self.safety_level = safety_level
        #: The plain refusal text, without ids, for the web interface.
        self.reason_text = reason or (
            f"You cannot approve this {safety_level} change of another person. "
            f"Only {_HIGH_RISK_APPROVERS_TEXT} may approve dangerous and "
            "destructive changes."
        )
        super().__init__(
            operator_sub=operator_sub,
            role=role,
            message=(
                f"operator {operator_sub!r} may not approve approval_request "
                f"{request_id}: {self.reason_text}"
            ),
        )


class ApprovalRoutingError(ApprovalGroupForbiddenError):
    """The person is not in a login group kind the request is routed to.

    Raised by :func:`approve_request` when the ``POLICY_APPROVER_ROUTING``
    switch is on, the request stored the group kinds that may approve it
    (:mod:`meho_backplane.policy.routing`), and the person's kind is not one
    of them. Subclasses :class:`ApprovalGroupForbiddenError`, so every
    approve route answers 403 with :attr:`reason_text`: one sentence that
    names who may approve.
    """

    def __init__(self, request_id: uuid.UUID, *, operator_sub: str, role: str, reason: str) -> None:
        super().__init__(
            request_id, operator_sub=operator_sub, role=role, safety_level="", reason=reason
        )


class UnknownRequesterApprovalError(ApprovalGroupForbiddenError):
    """MEHO knows nobody behind the login that asked; only an admin may approve.

    Raised by :func:`approve_request` when an agent, service or runner login
    made the request, MEHO found no person behind it at park time
    (:func:`~meho_backplane.operations.people_behind.people_behind_unknown`),
    and the approver is not an admin. Subclasses
    :class:`ApprovalGroupForbiddenError`, so every approve route answers 403
    with :attr:`reason_text`.
    """

    def __init__(self, request_id: uuid.UUID, *, operator_sub: str, role: str) -> None:
        super().__init__(
            request_id,
            operator_sub=operator_sub,
            role=role,
            safety_level="",
            reason=(
                "You cannot approve this change. MEHO does not know who set up the "
                "login that asked for it, so only an Admin may approve it. "
                "Ask an Admin to approve it on the Approvals page of the web interface."
            ),
        )


class NonHumanApprovalError(UnauthorizedApprovalError):
    """A machine (non-human) principal tried to decide an approval request.

    Approving / rejecting / deciding a parked op is a **human** decision
    (v0.1-spec §7). Raised by :func:`_check_reviewer_role`
    when ``operator.principal_kind`` is a machine kind (agent / service /
    runner), independent of tenant role or the ``approver`` capability — an
    agent principal is minted ``tenant_admin``, so a role/approver check
    alone would admit it.

    Subclasses :class:`UnauthorizedApprovalError` so the existing
    ``except UnauthorizedApprovalError`` handlers in the REST and console
    routes still map it to HTTP 403. This service-layer refusal is the
    transport-independent backstop behind the REST human-principal
    dependency (:func:`~meho_backplane.auth.rbac.require_human_principal`):
    a caller reaching the service directly (agent runtime, tests) sees the
    same human-only policy.
    """

    def __init__(self, *, operator_sub: str, principal_kind: str) -> None:
        self.operator_sub = operator_sub
        self.principal_kind = principal_kind
        # ``role`` is populated for base-class compatibility (nothing reads
        # it apart from the message); the refusal is kind-based, not role-
        # based, so mirror the kind into it rather than a tenant role.
        self.role = principal_kind
        # Skip ``UnauthorizedApprovalError.__init__`` (its message talks about
        # roles); build the kind-aware message off the shared base instead.
        ApprovalError.__init__(
            self,
            f"principal {operator_sub!r} of kind {principal_kind!r} may not "
            "decide an approval request — approval is a human decision "
            "(v0.1-spec §7)",
        )


class ApprovalRoleFloorError(UnauthorizedApprovalError):
    """The approver's role is below the role floor of the parked operation.

    Raised by :func:`approve_request` when the operation needs a role from
    the person who runs it (:mod:`meho_backplane.operations.role_floors`)
    and the approver's ``tenant_role`` is below it. The approver is read as
    recorded at the decision, on whichever resume path wins, so the person
    who approves must hold the role the operation needs.
    Checked before any state change: the request stays ``pending`` and no
    decision audit row is written. Subclasses
    :class:`UnauthorizedApprovalError`, so every handler of that error still
    answers 403; the approve routes catch this one first to show its text.
    """

    def __init__(self, request_id: uuid.UUID, *, operator_sub: str, role: str, denial: str) -> None:
        self.request_id = request_id
        #: The plain refusal text, without ids, for the web interface.
        self.reason_text = f"You cannot approve this change: {denial}."
        super().__init__(
            operator_sub=operator_sub,
            role=role,
            message=(
                f"operator {operator_sub!r} may not approve approval_request {request_id}: {denial}"
            ),
        )


class RightsChangeApprovalError(ApprovalRoleFloorError):
    """The person may not approve this change of rights.

    Raised by :func:`approve_request` when the request carries the
    rights-change mark and the person is not an admin
    (:meth:`~meho_backplane.auth.operator.Operator.may_approve_rights_change`),
    or approves on the command line or over REST instead of with a click in
    the web console. *reason* is :data:`RIGHTS_CHANGE_REFUSAL` or
    :data:`~meho_backplane.operations.grant_approval.RIGHTS_CHANGE_CONSOLE_ONLY`.
    Subclasses :class:`ApprovalRoleFloorError`, so every approve route
    answers 403 with its text.
    """

    def __init__(
        self,
        request_id: uuid.UUID,
        *,
        operator_sub: str,
        role: str,
        reason: str = RIGHTS_CHANGE_REFUSAL,
    ) -> None:
        super().__init__(request_id, operator_sub=operator_sub, role=role, denial=reason)
        self.reason_text = reason


class ResultAccessForbiddenError(ApprovalError):
    """A principal other than the request owner tried to read its result.

    Raised by :func:`read_approval_result` when
    ``operator.sub != request.principal_sub``. The approved re-dispatch's
    result is surfaced only to the **originating** principal — the consumer
    that parked the op (#3209) — so it can resume a non-idempotent parked op
    (VCF ``bringup.start``) by poll instead of a second submit. Any other
    principal, including an operator who could approve the request, is refused;
    the route layer maps this to 403. Tenant isolation is handled upstream by
    :func:`get_request` (a cross-tenant id is a 404, never reaching this guard).
    """

    def __init__(self, *, request_id: uuid.UUID, principal_sub: str) -> None:
        self.request_id = request_id
        self.principal_sub = principal_sub
        super().__init__(
            f"principal {principal_sub!r} is not the owner of approval_request "
            f"{request_id}; its result is readable only by the originating principal"
        )


class WithdrawForbiddenError(ApprovalError):
    """A principal other than the sender tried to withdraw a request (#4987).

    Raised by :func:`withdraw_request` when
    ``operator.sub != request.principal_sub``. Only the principal that sent
    the request may take it back; an approver may not. The route layer maps
    this to 403.
    """

    def __init__(self, *, request_id: uuid.UUID, principal_sub: str) -> None:
        self.request_id = request_id
        self.principal_sub = principal_sub
        super().__init__(
            f"principal {principal_sub!r} did not send approval_request {request_id}; "
            "only the sender can withdraw it"
        )


class DelegatedReasonInvalidError(ApprovalError):
    """A decision reason names an instructing human but is incomplete.

    Raised by :func:`parse_delegation` (called from :func:`approve_request`
    and :func:`reject_request`) when the reason carries an
    ``Instructed-by:`` line but the trailer block at its end is missing
    ``Instruction:`` or ``Refs:``, has an empty value, repeats a key, or is
    not one contiguous block of ``Key: value`` lines (#3692). Fail-closed:
    a delegated decision that cannot say who instructed, what they wrote,
    and where it is tracked is refused, not recorded half-way. The route
    layer maps this to 422.
    """

    def __init__(self, problem: str) -> None:
        self.problem = problem
        super().__init__(
            f"delegated decision reason is invalid: {problem}. End the reason "
            "with a block of three lines: 'Instructed-by: <who>', "
            "'Instruction: \"<their words>\"', 'Refs: <issue or change ref>'"
        )


class DelegatedDecisionForbiddenError(ApprovalError):
    """A delegated decision was made on a deployment that is not lab-class.

    Raised by :func:`approve_request` and :func:`reject_request` when the
    decision ``reason`` carries a valid delegation block (#3692) and the
    deployment has not set ``Settings.approval_allow_delegated_decision``
    (#3693). On production an approve or reject must be the approver's own
    call, not one made on another person's instruction. The switch is read
    from the environment at boot, so no request field can change the
    outcome. The route layer maps this to 403.
    """

    def __init__(self, request_id: uuid.UUID, *, operator_sub: str, instructed_by: str) -> None:
        self.request_id = request_id
        self.operator_sub = operator_sub
        self.instructed_by = instructed_by
        super().__init__(
            f"operator {operator_sub!r} may not decide approval_request "
            f"{request_id} on the instruction of {instructed_by!r}: delegated "
            "decisions are allowed only on lab-class deployments "
            "(APPROVAL_ALLOW_DELEGATED_DECISION=true). Decide on your own "
            "judgement, without the Instructed-by: block"
        )


#: Upper bound on a decision ``reason``, shared by the REST bodies and the
#: console form. The audit row stores the reason verbatim; the bound keeps a
#: paste accident from landing a huge free-text blob on the ledger.
MAX_REASON_LENGTH: Final[int] = 2000


@dataclass(frozen=True, slots=True)
class Delegation:
    """Who told the approver to decide, in their own words (#3692).

    Parsed from the git-trailer-style block at the end of a decision
    ``reason`` by :func:`parse_delegation`. The approver still decides under
    their **own** identity (``reviewed_by``); this records the other human
    whose written instruction they acted on. Not related to the RFC 8693
    ``principal_act`` pair, which describes the *requester's* delegation.
    """

    instructed_by: str
    instruction: str
    refs: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON shape stored on the decision audit row."""
        return {
            "instructed_by": self.instructed_by,
            "instruction": self.instruction,
            "refs": list(self.refs),
        }


#: A reason is delegated iff any line starts with ``Instructed-by:``.
_INSTRUCTED_BY_LINE = re.compile(r"^[ \t]*instructed-by[ \t]*:", re.IGNORECASE | re.MULTILINE)
#: One ``Key: value`` trailer line (git trailer shape).
_TRAILER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)[ \t]*:[ \t]*(.*)$")
#: Quote pairs stripped from the ``Instruction:`` value.
_QUOTE_PAIRS: Final[tuple[tuple[str, str], ...]] = (('"', '"'), ("\u201c", "\u201d"))


def _trailer_block(reason: str) -> dict[str, str] | None:
    """Parse the last paragraph of *reason* as ``Key: value`` trailers.

    Returns a lower-cased-key dict, or ``None`` when any line of the last
    paragraph is not a trailer line. Raises on a repeated key.
    """
    block: list[str] = []
    for line in reversed(reason.strip().splitlines()):
        if not line.strip():
            break
        block.append(line.strip())
    trailers: dict[str, str] = {}
    for line in reversed(block):
        match = _TRAILER_LINE.match(line)
        if match is None:
            return None
        key = match.group(1).lower()
        if key in trailers:
            raise DelegatedReasonInvalidError(f"the {match.group(1)}: line appears more than once")
        trailers[key] = match.group(2).strip()
    return trailers


def _unquote(value: str) -> str:
    for opening, closing in _QUOTE_PAIRS:
        if len(value) >= 2 and value.startswith(opening) and value.endswith(closing):
            return value[1:-1].strip()
    return value


def parse_delegation(reason: str) -> Delegation | None:
    """Parse the delegated-decision trailers out of a decision *reason*.

    Pure and deterministic (#3692). Returns ``None`` when no line starts
    with ``Instructed-by:`` (keys are case-insensitive) -- an ordinary
    decision. Otherwise the last paragraph of the reason must be a block of
    ``Key: value`` lines carrying non-empty ``Instructed-by:``,
    ``Instruction:`` and ``Refs:`` (comma-separated). Other trailer keys in
    the block are ignored. Surrounding double quotes on the instruction are
    removed; the words inside are kept verbatim.

    Raises:
        DelegatedReasonInvalidError: The marker is present but the block is
            missing, malformed, or incomplete.
    """
    if not reason or not _INSTRUCTED_BY_LINE.search(reason):
        return None
    trailers = _trailer_block(reason)
    if trailers is None or "instructed-by" not in trailers:
        raise DelegatedReasonInvalidError(
            "the Instructed-by:, Instruction: and Refs: lines must be one block "
            "of 'Key: value' lines at the end of the reason, after a blank line"
        )
    instructed_by = trailers["instructed-by"]
    instruction = _unquote(trailers.get("instruction", ""))
    refs = tuple(ref.strip() for ref in trailers.get("refs", "").split(",") if ref.strip())
    missing = [
        name
        for name, value in (
            ("Instructed-by", instructed_by),
            ("Instruction", instruction),
            ("Refs", refs),
        )
        if not value
    ]
    if missing:
        raise DelegatedReasonInvalidError(f"missing or empty: {', '.join(missing)}")
    return Delegation(instructed_by=instructed_by, instruction=instruction, refs=refs)


def _record_delegation(
    extra: dict[str, Any],
    delegation: Delegation | None,
    *,
    request: ApprovalRequest,
    operator: Operator,
    decision: str,
) -> None:
    """Add the structured delegation block to *extra* and log it (#3692).

    No-op for an ordinary decision. The block rides the decision audit
    row's payload (no column, no migration -- the #2087 break-glass
    precedent), and the WARNING event makes a delegated decision visible
    in the log stream the way ``approval_self_approval_break_glass`` does.
    """
    if delegation is None:
        return
    extra["delegation"] = delegation.to_payload()
    _log().warning(
        "approval_delegated_decision",
        approval_request_id=str(request.id),
        op_id=request.op_id,
        decision=decision,
        reviewed_by=operator.sub,
        instructed_by=delegation.instructed_by,
        tenant_id=str(operator.tenant_id),
    )


def _check_delegated_allowed(
    delegation: Delegation | None,
    *,
    request: ApprovalRequest,
    operator: Operator,
    decision: str,
) -> None:
    """Refuse a delegated decision unless the deployment is lab-class (#3693).

    An ordinary decision (*delegation* is ``None``) always passes. A
    delegated one passes only when ``Settings.approval_allow_delegated_decision``
    is set. Settings are imported lazily, like :func:`_check_self_approval`.

    Raises:
        DelegatedDecisionForbiddenError: Delegated decision on a deployment
            without the lab-class switch.
    """
    if delegation is None:
        return

    from meho_backplane.settings import get_settings

    if get_settings().approval_allow_delegated_decision:
        return
    _log().warning(
        "approval_delegated_decision_refused",
        approval_request_id=str(request.id),
        op_id=request.op_id,
        decision=decision,
        operator_sub=operator.sub,
        instructed_by=delegation.instructed_by,
        tenant_id=str(operator.tenant_id),
    )
    raise DelegatedDecisionForbiddenError(
        request.id, operator_sub=operator.sub, instructed_by=delegation.instructed_by
    )


class ApprovalResultState(StrEnum):
    """Whether an approved request has run yet (#3486).

    Read off the row, never stored. ``status`` keeps its closed four-state
    contract; this field adds the run phase next to it, so a client that
    lists or shows requests can tell "approved, not run yet" from
    "approved and run" without polling the owner-only ``/result`` read.

    * :attr:`NOT_STARTED` -- approved, and no resumer has claimed the run
      yet (``resumed_at IS NULL``).
    * :attr:`STARTED` -- a resumer claimed the run (``resumed_at`` is set),
      but no result is stored. Either the op is still running, or the run
      ended without a stored result: the result write is best-effort, and a
      re-dispatch that raised instead of returning an envelope stores
      nothing. Both resume paths (operator surfaces and the in-process
      agent waiter) store the envelope they get back.
    * :attr:`EXECUTED` -- the stored result has ``status == "ok"``.
    * :attr:`FAILED` -- a result is stored and its ``status`` is not ``ok``.
    * :attr:`UNCERTAIN` -- a result is stored, its ``status`` is not ``ok``,
      and it says MEHO does not know if the change happened
      (``extras.execution_outcome == "uncertain"``). This covers a lost
      vendor answer after the request was sent, a failed audit write, and an
      approved run that MEHO lost in a restart. MEHO never runs it again, and
      every view says: do not run it again, check the target first.
    """

    NOT_STARTED = "not_started"
    STARTED = "started"
    EXECUTED = "executed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


def result_state_of_envelope(envelope: Mapping[str, Any]) -> ApprovalResultState:
    """The run state a stored result envelope stands for (#5025).

    The one rule every approval view uses: ``ok`` is ``executed``, an error
    that says the outcome is unknown is ``uncertain``, any other result is
    ``failed``.
    """
    if envelope.get("status") == "ok":
        return ApprovalResultState.EXECUTED
    if envelope_is_uncertain(envelope):
        return ApprovalResultState.UNCERTAIN
    return ApprovalResultState.FAILED


def approval_result_state(request: ApprovalRequest) -> ApprovalResultState | None:
    """Return the run phase of *request*, or ``None`` when it is not approved (#3486)."""
    if request.status != ApprovalRequestStatus.APPROVED.value:
        return None
    if request.resume_result is not None:
        return result_state_of_envelope(request.resume_result)
    if request.resumed_at is not None:
        return ApprovalResultState.STARTED
    return ApprovalResultState.NOT_STARTED


class ApprovalExpiredReason(StrEnum):
    """Why an ``expired`` request expired (#5030).

    Read off the row, never stored, like :class:`ApprovalResultState`.

    * :attr:`DEADLINE_PASSED` -- the request waited past ``expires_at``
      without a decision.
    * :attr:`RELEASE_CHANGED` -- MEHO started with a different release
      while the request waited (:func:`expire_older_release_requests`).
      Only this path expires a request before its deadline, so an
      ``expired`` row with ``decided_at < expires_at`` is this case.
    """

    DEADLINE_PASSED = "deadline_passed"
    RELEASE_CHANGED = "release_changed"


#: What the requester reads for each expiry reason (#5030), on every surface.
EXPIRED_REASON_MESSAGES: Final[dict[ApprovalExpiredReason, str]] = {
    ApprovalExpiredReason.DEADLINE_PASSED: (
        "The time for this request ran out before the operation ran. "
        "Ask again if the change is still needed."
    ),
    ApprovalExpiredReason.RELEASE_CHANGED: (
        "MEHO was updated while the request waited, and an approval covers "
        "one exact release. Ask again."
    ),
}


def approval_expired_reason(request: ApprovalRequest) -> ApprovalExpiredReason | None:
    """Return why *request* expired, or ``None`` when it is not ``expired`` (#5030)."""
    if request.status != ApprovalRequestStatus.EXPIRED.value:
        return None
    if (
        request.decided_at is not None
        and request.expires_at is not None
        and _as_utc(request.decided_at) < _as_utc(request.expires_at)
    ):
        return ApprovalExpiredReason.RELEASE_CHANGED
    return ApprovalExpiredReason.DEADLINE_PASSED


def approval_release_changed(request: ApprovalRequest) -> bool:
    """True when *request* was made under another release than the running one (#5030).

    A request saved before MEHO recorded the release (``release_id IS
    NULL``) counts as an older release. When the running build has no
    release identity (a local development run), nothing is bound and this
    is always ``False``.
    """
    running = running_release_id()
    return running is not None and request.release_id != running


#: Statuses in which a request still holds its ``idempotency_key`` (#3486).
#: Must match the partial unique index predicate of migration ``0105``.
_IDEMPOTENT_LIVE_STATUSES: Final[tuple[str, ...]] = (
    ApprovalRequestStatus.PENDING.value,
    ApprovalRequestStatus.APPROVED.value,
)


@dataclass(frozen=True)
class _Asker:
    """What the approve checks read about who asked, as one call sends it.

    *kind* is the kind of login, *people* the casefolded subs of the people
    behind it (:func:`behind_subs`, the compare of the self-approval rule),
    *reveal_secret* the choice to reveal secret values. A waiting request
    is given back to a call only when all three are the same.
    """

    kind: str | None
    people: frozenset[str]
    reveal_secret: bool

    def asked(self, request: ApprovalRequest) -> bool:
        """True when *request* was asked by the same kind of login, people and choice."""
        return (
            request.principal_kind == self.kind
            and bool(request.reveal_secret) == self.reveal_secret
            and behind_subs(request.people_behind) == self.people
        )


async def _key_holders(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    idempotency_key: str,
) -> list[ApprovalRequest]:
    """Return every live request of this login that *idempotency_key* gets back.

    Scoped to the tenant **and** the principal, so a key never matches
    another tenant's or another requester's request. The request that holds
    the key in its own column comes first, then the requests the key is
    linked to (:class:`ApprovalRequestKey`), newest first.

    ``populate_existing`` refreshes a row the session already holds. The
    retry loop in :func:`_resolve_idempotent_request` calls this again after
    its locked re-read missed; by then the row may have been approved on
    another connection. Without the refresh the session would keep the
    cached ``pending`` state, treat the row as overdue again, and loop.

    A key is linked to a request by a same-call reuse, or by a call that
    got its own request for a key another request holds.
    """
    live_for_caller = (
        ApprovalRequest.tenant_id == tenant_id,
        ApprovalRequest.principal_sub == principal_sub,
        ApprovalRequest.status.in_(_IDEMPOTENT_LIVE_STATUSES),
    )
    held = (
        select(ApprovalRequest)
        .where(ApprovalRequest.idempotency_key == idempotency_key, *live_for_caller)
        .execution_options(populate_existing=True)
    )
    linked = (
        select(ApprovalRequest)
        .join(ApprovalRequestKey, ApprovalRequestKey.approval_request_id == ApprovalRequest.id)
        .where(ApprovalRequestKey.idempotency_key == idempotency_key, *live_for_caller)
        .order_by(ApprovalRequest.created_at.desc())
        .execution_options(populate_existing=True)
    )
    found: list[ApprovalRequest] = []
    for stmt in (held, linked):
        found.extend((await session.execute(stmt)).scalars())
    return found


async def _find_idempotent_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    idempotency_key: str,
    asker: _Asker,
) -> ApprovalRequest | None:
    """Return the live request that holds *idempotency_key* for this caller.

    A request of :func:`_key_holders` counts only when *asker* asked it: the
    same kind of login, the same people behind the login and the same
    choice to reveal secret values. A request asked otherwise is not
    returned, so the call gets its own request.
    """
    holders = await _key_holders(
        session,
        tenant_id=tenant_id,
        principal_sub=principal_sub,
        idempotency_key=idempotency_key,
    )
    return next((found for found in holders if asker.asked(found)), None)


async def _refuse_key_held_for_another_call(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    idempotency_key: str,
    is_this_call: Callable[[ApprovalRequest], bool],
) -> None:
    """Refuse the call when *idempotency_key* gets back a request of another call.

    Runs before the call gets a request back for the key or stores its own.
    Every live request the key gets back is checked: the one that holds the
    key in its own column and every linked one, asked the same way or
    another way (:class:`_Asker`). One that holds the key for another call
    (*is_this_call* is false) refuses the call.

    A ``pending`` request past its deadline does not count, but only after
    a locked re-read (:func:`_holds_key_after_lock`): an approve that
    started before the deadline holds the row lock until it commits, and
    the approved request keeps the key.

    Raises:
        IdempotencyKeyConflictError: a live request of another call holds
            the key.
    """
    holders = await _key_holders(
        session,
        tenant_id=tenant_id,
        principal_sub=principal_sub,
        idempotency_key=idempotency_key,
    )
    for holder in holders:
        if is_this_call(holder):
            continue
        if _is_overdue_pending(holder) and not await _holds_key_after_lock(session, holder):
            continue
        raise IdempotencyKeyConflictError(holder.id)


async def _holds_key_after_lock(session: AsyncSession, request: ApprovalRequest) -> bool:
    """Lock *request*, read it again, and say whether it still holds its key.

    ``SELECT ... FOR UPDATE`` waits for a decision that holds the row lock
    (:func:`_load_for_tenant` with ``for_update``) to commit. The request holds
    its key when it is ``approved``, or ``pending`` within its deadline.
    """
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.id == request.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked = (await session.execute(stmt)).scalars().first()
    return (
        locked is not None
        and locked.status in _IDEMPOTENT_LIVE_STATUSES
        and not _is_overdue_pending(locked)
    )


async def _link_reuse_key(
    session: AsyncSession, request: ApprovalRequest, idempotency_key: str | None
) -> None:
    """Link *idempotency_key* to *request*, so a later retry finds it.

    *request* is a reused request, or a request that got its own row while
    a request of another asker holds the key in its own column.

    Nothing to do without a key, or when the row already holds this key.
    The insert runs in a savepoint: when a call that arrives at the same
    moment already linked the same key, the duplicate is dropped and the
    outer transaction stays usable.
    """
    if idempotency_key is None or request.idempotency_key == idempotency_key:
        return
    try:
        async with session.begin_nested():
            session.add(
                ApprovalRequestKey(
                    approval_request_id=request.id,
                    idempotency_key=idempotency_key,
                    created_at=_now(),
                )
            )
            await session.flush()
    except IntegrityError:
        _log().info(
            "approval_request_reuse_key_already_linked",
            approval_request_id=str(request.id),
        )


#: Status code of the ``approval.reuse`` row of a retry that got a request a
#: person already approved. Only a waiting request has 202.
_REUSE_APPROVED_STATUS_CODE: int = 200


def _found_request_state(request: ApprovalRequest) -> str:
    """The state of *request* as the ``approval.reuse`` row says it.

    ``waiting``, or ``approved_`` plus the run phase
    (:class:`ApprovalResultState`): ``approved_not_started``,
    ``approved_started``, ``approved_executed``, ``approved_failed`` or
    ``approved_uncertain``.
    """
    phase = approval_result_state(request)
    return "waiting" if phase is None else f"approved_{phase.value}"


async def _write_reuse_audit_row(
    session: AsyncSession,
    operator: Operator,
    request: ApprovalRequest,
    params: dict[str, Any],
    *,
    say_state: bool = False,
) -> None:
    """Write the ``approval.reuse`` audit row of a call that got *request*.

    Same transaction as the call. The row names the reused request and the
    caller's own session. Its payload has the ``params_hash`` of the call's
    own *params* (:func:`compute_audit_params_hash`, so secret values are
    masked before the hash). Its id is kept in ``request._reuse_audit_id``,
    so the answer's ``audit_id`` is this row and not the first call's
    ``approval.request`` row.

    With *say_state* (a retry with the same key), the payload also has
    ``request_state`` (see :func:`_found_request_state`). The status code is
    202 for a waiting request and 200 for an approved one.
    """
    audit_id = uuid.uuid4()
    payload: dict[str, Any] = {"params_hash": compute_audit_params_hash(params)}
    status_code = _APPROVAL_STATUS_CODE
    if say_state:
        state = _found_request_state(request)
        payload["request_state"] = state
        if state != "waiting":
            status_code = _REUSE_APPROVED_STATUS_CODE
    await _write_audit_row(
        session,
        audit_id=audit_id,
        event_class=AuditEventClass.POLICY_DECISION,
        operator=operator,
        request=request,
        path=_REUSE_PATH,
        status_code=status_code,
        duration_ms=0.0,
        extra_payload=payload,
        caller_session=True,
    )
    request._reuse_audit_id = audit_id  # type: ignore[attr-defined]


async def _reuse_for_key_retry(
    session: AsyncSession,
    operator: Operator,
    reused: ApprovalRequest,
    params: dict[str, Any],
) -> ApprovalRequest:
    """Give a retry with the same key its own audit row when the switch is on.

    Needs ``APPROVAL_RETRY_OWN_AUDIT_ROW``. The row is the same
    ``approval.reuse`` row that the same-call path writes. With the switch
    off, the retry writes no row and the answer is the first call's.
    """
    from meho_backplane.settings import get_settings

    if get_settings().approval_retry_own_audit_row:
        await _write_reuse_audit_row(session, operator, reused, params, say_state=True)
    return reused


def _is_composite_step() -> bool:
    """True when this call is a step a multi-step operation sent through ``dispatch_child``."""
    from meho_backplane.operations.composite import COMPOSITE_DEPTH_TOP_LEVEL, composite_depth_var

    return composite_depth_var.get() != COMPOSITE_DEPTH_TOP_LEVEL


def _same_call_reuse_applies(
    run_id: uuid.UUID | None,
    resume_parent: dict[str, Any] | None,
    requester: ResumeRequester | None,
    allow_same_call_reuse: bool = True,
) -> bool:
    """True when a direct call may get the waiting request of the same call.

    Needs the switch ``APPROVAL_SAME_CALL_REUSE``. A call inside an agent
    run, a step of a multi-step operation (one sent through
    ``dispatch_child``, or one saved to restart after approval), a step
    checked by ``enforce_subop_policy`` (*allow_same_call_reuse* false), or a
    re-run for an earlier requester always gets its own request.
    """
    from meho_backplane.settings import get_settings

    if not allow_same_call_reuse:
        return False
    if run_id is not None or resume_parent is not None or requester is not None:
        return False
    if _is_composite_step():
        return False
    return get_settings().approval_same_call_reuse


async def _own_people_behind(
    session: AsyncSession, operator: Operator
) -> list[dict[str, Any]] | None:
    """The people behind the caller's own login (``None`` for a person)."""
    return await find_people_behind(
        session,
        tenant_id=operator.tenant_id,
        sub=operator.sub,
        client_id=operator.client_id,
        kind=operator.principal_kind.value,
        runner_id=operator.runner_id,
    )


async def _find_same_call_pending_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    op_id: str,
    connector_id: str,
    target_id: uuid.UUID | None,
    params: dict[str, Any],
    preview_hash: str | None,
    asker: _Asker,
    principal_act: str | None,
) -> ApprovalRequest | None:
    """Return the waiting request of this login for the same call, or ``None``.

    Used only with ``APPROVAL_SAME_CALL_REUSE`` on. Scoped to the tenant and
    the login, so a caller never learns about the request of another person.
    Only a ``pending`` request inside its deadline counts: an approved one
    may have run, and an overdue one can no longer be approved. Nothing is
    expired here. A request of an agent run or of a step of a multi-step
    operation (``resume_parent`` set, or ``composite_step`` not false) never
    counts: approving it runs that run or that step, not this direct call. A
    request made before migration 0183 has ``composite_step`` NULL, so it
    never counts either. The match
    is the same as for a retry key: same operation, connector, target, params,
    ``preview_hash`` and ``work_ref``, plus the same *asker*: the kind of
    login, the people behind it and the ``reveal_secret`` choice
    (:class:`_Asker`). The approval checks read only the kind and the list
    stored on the request. The acting agent (*principal_act*, ``None`` for a
    direct call) must be the same too.
    """
    stmt = (
        select(ApprovalRequest)
        .where(
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.principal_sub == principal_sub,
            ApprovalRequest.principal_act == principal_act
            if principal_act is not None
            else ApprovalRequest.principal_act.is_(None),
            ApprovalRequest.op_id == op_id,
            ApprovalRequest.connector_id == connector_id,
            ApprovalRequest.target_id == target_id
            if target_id is not None
            else ApprovalRequest.target_id.is_(None),
            ApprovalRequest.status == ApprovalRequestStatus.PENDING.value,
            ApprovalRequest.run_id.is_(None),
        )
        .order_by(ApprovalRequest.created_at)
        .execution_options(populate_existing=True)
    )
    for existing in (await session.execute(stmt)).scalars():
        # Checked here, not in SQL: a JSON column can hold JSON ``null``
        # instead of SQL NULL for a direct call.
        if (
            existing.resume_parent is None
            # NULL is a request made before migration 0183: it may be a step.
            and existing.composite_step is False
            and params_hash_matches(params, existing.params_hash)
            and existing.preview_hash == preview_hash
            and existing.work_ref == work_ref_var.get()
            and asker.asked(existing)
            and not _is_overdue_pending(existing)
        ):
            return existing
    return None


def _reuse_idempotent_request(
    existing: ApprovalRequest,
    *,
    op_id: str,
    connector_id: str,
    target_id: uuid.UUID | None,
    params: dict[str, Any],
    preview_hash: str | None,
) -> ApprovalRequest:
    """Return *existing* for a retry, or refuse when the call differs (#3486).

    The retry must be the identical call: its *params* must match the
    stored ``params_hash`` (:func:`params_hash_matches`). ``work_ref`` is
    compared too, so two runs under different change tickets never share
    one request.
    """
    if not _is_same_keyed_call(
        existing,
        op_id=op_id,
        connector_id=connector_id,
        target_id=target_id,
        params=params,
        preview_hash=preview_hash,
    ):
        raise IdempotencyKeyConflictError(existing.id)
    _log().info(
        "approval_request_reused",
        approval_request_id=str(existing.id),
        op_id=op_id,
        connector_id=connector_id,
        principal_sub=existing.principal_sub,
        tenant_id=str(existing.tenant_id),
    )
    existing._reused = True  # type: ignore[attr-defined]
    return existing


def _is_same_keyed_call(
    existing: ApprovalRequest,
    *,
    op_id: str,
    connector_id: str,
    target_id: uuid.UUID | None,
    params: dict[str, Any],
    preview_hash: str | None,
) -> bool:
    """True when *existing* was asked for this call: what a retry key compares.

    The same operation, connector, target, params (:func:`params_hash_matches`),
    ``preview_hash`` and ``work_ref``.
    """
    return (
        existing.op_id == op_id
        and existing.connector_id == connector_id
        and existing.target_id == target_id
        and params_hash_matches(params, existing.params_hash)
        and existing.preview_hash == preview_hash
        and existing.work_ref == work_ref_var.get()
    )


async def _insert_idempotent_request(
    session: AsyncSession, request: ApprovalRequest, *, idempotency_key: str, asker: _Asker
) -> ApprovalRequest | None:
    """Insert *request* in a savepoint; return the winner if a racer got there first.

    The partial unique index (migration ``0105``) makes the second of two
    racing inserts fail. That insert's savepoint rolls back, the outer
    transaction stays usable, and the row the other request committed is
    returned. Returns ``None`` when *request* itself was inserted.

    The index also refuses the insert when a request of another *asker*
    holds the key (:func:`_find_idempotent_request` does not return it).
    Then *request* keeps the key in :class:`ApprovalRequestKey` instead of
    its own column. The caller holds the key lock
    (:func:`_take_idempotency_key_lock`), so a second call with this key
    looks it up only after this one commits, and finds this request.
    """
    try:
        async with session.begin_nested():
            session.add(request)
            await session.flush()
    except IntegrityError:
        existing = await _find_idempotent_request(
            session,
            tenant_id=request.tenant_id,
            principal_sub=request.principal_sub,
            idempotency_key=idempotency_key,
            asker=asker,
        )
        if existing is not None:
            return existing
        request.idempotency_key = None
        session.add(request)
        await session.flush()
        await _link_reuse_key(session, request, idempotency_key)
    return None


async def _take_idempotency_key_lock(
    session: AsyncSession, *, tenant_id: uuid.UUID, principal_sub: str, idempotency_key: str
) -> None:
    """Take the lock of one retry key of one login, until the transaction ends.

    Every call with a retry key takes it before it looks the key up. So two
    calls with the same key never look it up and store a request at the same
    moment: the second one looks the key up only after the first one
    committed, and finds the request the first one stored or linked. That
    holds also when the request that held the key is decided in between.
    """
    await take_approval_lock(
        session, "idempotency-key", f"{tenant_id}\0{principal_sub}\0{idempotency_key}"
    )


#: The acting identity for every expiry: the sweeper and the late-retry path
#: of :func:`create_pending_request`. A stable synthetic sub makes expiries
#: filterable in audit queries and distinct from operator-driven decisions,
#: the same pattern the topology scheduler (``system:topology-scheduler``)
#: and connector system contexts use.
EXPIRY_OPERATOR_SUB: Final = "system:approval-expiry"


def expiry_operator(tenant_id: uuid.UUID) -> Operator:
    """Build the synthetic per-tenant operator that expires requests.

    ``raw_jwt`` is the empty string: an expiry forwards no token
    downstream. ``OPERATOR`` role satisfies the ``>= operator`` floor
    :func:`expire_stale_requests` enforces on the acting identity.
    """
    return Operator(
        sub=EXPIRY_OPERATOR_SUB,
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        in_code_kind="system",
    )


def _is_overdue_pending(existing: ApprovalRequest) -> bool:
    """True when *existing* is ``pending`` and past its deadline (#3486)."""
    if existing.status != ApprovalRequestStatus.PENDING.value:
        return False
    return _deadline_has_passed(existing, _now(), _resolve_default_ttl())


async def _lock_and_expire_pending(
    session: AsyncSession, existing: ApprovalRequest
) -> ApprovalRequest | None:
    """Lock the overdue *existing* row and expire it; ``None`` if it changed (#3486).

    The sweeper may not have run yet (or may be off). An overdue ``pending``
    row can no longer be approved, so a retry must not get it back. It is
    locked and expired here, in the caller's transaction, the same way
    :func:`expire_stale_requests` does it and under the same
    :func:`expiry_operator` identity, so the decision audit row names the
    system, not the requester. That frees the key for a new request.

    Returns ``None`` when the locked re-read finds no ``pending`` row with
    this id: a concurrent sweep, reject, approve, or another retry changed
    it between the caller's first read and this lock. The caller must then
    look the key up again instead of using the stale row.
    """
    cutoff = _now()
    default_ttl = _resolve_default_ttl()
    stmt = (
        select(ApprovalRequest)
        .where(
            ApprovalRequest.id == existing.id,
            ApprovalRequest.status == ApprovalRequestStatus.PENDING.value,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked = (await session.execute(stmt)).scalars().first()
    if locked is None:
        return None
    await _expire_request(
        session,
        locked,
        operator=expiry_operator(locked.tenant_id),
        cutoff=cutoff,
        default_ttl=default_ttl,
    )
    return locked


async def _resolve_idempotent_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    idempotency_key: str,
    asker: _Asker,
) -> tuple[ApprovalRequest | None, ApprovalRequest | None]:
    """Find what *idempotency_key* holds right now: ``(live, expired)`` (#3486).

    * ``(row, None)``: a usable request holds the key (``approved``, or
      ``pending`` within its deadline). The caller returns it for the retry.
    * ``(None, row)``: the key was held by an overdue ``pending`` request.
      This call expired it; the caller parks a new request with the key.
    * ``(None, None)``: the key is free.

    When the locked re-read in :func:`_lock_and_expire_pending` misses, the
    row changed under us: a sweep or reject freed the key, an approve kept
    it live, or another retry already expired the row and parked its
    replacement. The key is looked up again so the caller gets that current
    state (the replacement, for example), never the stale first read. Each
    repeat follows a committed change to the row it saw, so the loop ends.
    """
    while True:
        existing = await _find_idempotent_request(
            session,
            tenant_id=tenant_id,
            principal_sub=principal_sub,
            idempotency_key=idempotency_key,
            asker=asker,
        )
        if existing is None:
            return None, None
        if not _is_overdue_pending(existing):
            return existing, None
        expired = await _lock_and_expire_pending(session, existing)
        if expired is not None:
            return None, expired
        _log().info(
            "approval_request_idempotency_key_rechecked",
            approval_request_id=str(existing.id),
            principal_sub=principal_sub,
            tenant_id=str(tenant_id),
        )


def _now() -> datetime:
    """Return the current UTC datetime. Isolated for testing."""
    return datetime.now(UTC)


def _resolve_default_ttl() -> timedelta:
    """The configured default approval TTL as a :class:`~datetime.timedelta`.

    Read lazily off :func:`~meho_backplane.settings.get_settings` (mirrors
    the local settings import in :func:`_check_self_approval`) so the queue
    module stays decoupled from settings at import time.
    """
    from meho_backplane.settings import get_settings

    return timedelta(seconds=get_settings().approval_default_ttl_seconds)


def _bounded_expires_at(
    created_at: datetime,
    requested: datetime | None,
    default_ttl: timedelta,
) -> datetime:
    """Resolve the deadline a parked approval is stamped with (#2322).

    The TTL lifecycle is inert unless every parked row carries a concrete
    ``expires_at`` for the sweep to act on, so a ``None`` request defaults
    to ``created_at + default_ttl``. An explicit caller deadline is
    honoured but **capped** at that same ceiling — the single configured
    TTL doubles as the upper bound, so no surface can park an
    unbounded-lived approval, while a shorter (e.g. preflight-probe) or
    already-past deadline the caller asked for is respected as-is. There
    is deliberately no lower bound / per-op policy (substrate minimalism,
    #1177).
    """
    ceiling = created_at + default_ttl
    if requested is None:
        return ceiling
    return min(requested, ceiling)


def _policy_rule_fields() -> dict[str, Any]:
    """``policy_rule`` and ``policy_version`` of a new request, from ``policy_rule_var``."""
    stamp = policy_rule_var.get()
    if stamp is None:
        return {}
    return {"policy_rule": stamp.as_json(), "policy_version": stamp.policy_version}


#: Audit path of a call that gets an existing request. The call writes this
#: row, not a second ``approval.request`` row. Two calls write it: a call that
#: gets the waiting request of the same call (``APPROVAL_SAME_CALL_REUSE``),
#: and a retry with the same key (``APPROVAL_RETRY_OWN_AUDIT_ROW``).
_REUSE_PATH = "approval.reuse"

#: Audit paths written on the task of the call that waits.
_CALLER_PATHS = frozenset({"approval.request", _REUSE_PATH})

#: Audit paths that carry the runbook run and step of the call.
_RUNBOOK_STEP_PATHS = frozenset({*_CALLER_PATHS, "approval.result"})


def _request_row_columns() -> dict[str, Any]:
    """The columns the rows of ``_CALLER_PATHS`` take from the waiting call's task.

    The matched policy rule, and the login a secret store accepted
    for the parking call so far (``secret_read_as``), for example the read of
    the sink secret for the approval card of ``secret.move``.
    """
    return {**policy_rule_columns(), "secret_read_as": current_secret_read_as()}


async def _write_audit_row(
    session: AsyncSession,
    *,
    audit_id: uuid.UUID,
    event_class: AuditEventClass,
    operator: Operator,
    request: ApprovalRequest,
    path: str,
    status_code: int,
    duration_ms: float,
    extra_payload: dict[str, Any] | None = None,
    caller_session: bool = False,
) -> None:
    """Insert one ``audit_log`` row for an approval queue event.

    *event_class* (#3733) is declared by each caller:
    ``policy_decision`` for the ``approval.request`` row (the gate decided
    the call needs approval), ``approval_decision`` for an approve, reject
    or expiry ``approval.decision`` row and for the ``approval.withdraw``
    row, and ``legacy_request`` for the
    ``approval.result`` read row.

    Uses ``method='APPROVAL'`` and the provided *path* (``'approval.request'``
    or ``'approval.decision'``). The payload carries the
    ``approval_request_id``, ``op_id``, and ``connector_id`` so audit
    queries can surface the request context without joining the
    ``approval_request`` table.

    Session-replay lineage (#2086): ``agent_session_id`` and
    ``parent_audit_id`` are read off *request* (the durable copies
    captured at park time), not off this task's contextvars — the
    "request" row and the "decision" rows are written on different
    tasks (requester vs approver / expiry sweep). The "request" row
    itself is the one stored as ``request.request_audit_id``, so it
    parent-links to the contextvar value instead of self-parenting;
    decision rows parent-link to the stored id.

    *caller_session* is ``True`` for the ``approval.reuse`` row: it is
    written on the task of the call that got the waiting request, so it
    names that call's own session, not the session stored on *request*.
    """
    payload: dict[str, Any] = {
        "approval_request_id": str(request.id),
        "op_id": request.op_id,
        "connector_id": request.connector_id,
        # Two pairs, each id next to the name of the same person:
        # ``principal_sub`` / ``principal_name`` is the person who acted on
        # this row (the row's ``operator_sub``), ``requester_sub`` /
        # ``requester_name`` is the person who asked for the operation.
        "principal_sub": operator.sub,
        "requester_sub": request.principal_sub,
        "result_status": path.split(".")[-1],  # "request" | "decision"
    }
    if request.principal_name:
        payload["requester_name"] = request.principal_name
    # The class of the operation that waits, as the approval request stores
    # it (the stored class of an ingested row counts). Only on the "request" row.
    op_class = (request.proposed_effect or {}).get("op_class")
    if path == "approval.request" and isinstance(op_class, str):
        payload["op_class"] = op_class
    # Hoist the acting operator's display name so the name-aware audit views
    # resolve the approval row's ``operator_sub`` to a human name (#3300) --
    # the same ``payload['principal_name']`` shape the MCP wrapper rows (#1212)
    # and the DISPATCH run rows (#4029) carry.
    # ``operator`` is the requester on a "request" row / the reviewer on a
    # "decision" row, so the name always pairs with ``operator_sub``. Omitted
    # when there is no name (resolver fails open to the raw sub); display
    # name only, email is never widened onto this surface. An agent token
    # has no ``name`` claim: its registered name or client id is used (#3885).
    principal_name = await principal_display_name(session, operator)
    if principal_name:
        payload["principal_name"] = principal_name
    # #5239: the role of a platform admin without a group in this tenant.
    payload.update(tenant_role_source_payload(operator))
    # The group kind the policy file's bindings gave the person who acted on
    # this row (POLICY_APPROVER_ROUTING), so a decision row says where the
    # approver's right came from.
    if operator.bound_kind is not None:
        payload["bound_kind"] = operator.bound_kind.value
    # The login client of the person who acted on this row (the requester on
    # a "request" row, the reviewer on a "decision" row), when
    # AUDIT_CLIENT_IDENTITY is on.
    apply_client_block(payload, operator)
    # Approver routing: the group kinds that may approve the request and
    # the routing entry that set them. Only on a request routed while the
    # POLICY_APPROVER_ROUTING switch was on.
    if request.approver_rule is not None:
        payload["approver_rule"] = request.approver_rule
        payload["approver_roles"] = request.approver_roles
    # The runbook run and step of a check that waits or reads its result
    # (the "request" and "result" rows). Both rows are written on the task
    # of the runbook step, so the vars are bound. A "decision" row is written
    # on the approver's task and gets neither.
    run_id: uuid.UUID | None = None
    step_id: str | None = None
    if path in _RUNBOOK_STEP_PATHS:
        run_id = run_id_var.get()
        step_id = step_id_var.get()
        if run_id is not None:
            payload["run_id"] = str(run_id)
        if step_id is not None:
            payload["step_id"] = step_id
    if extra_payload:
        payload.update(extra_payload)
    # A request parked from the CLI stores the CLI login session as its
    # session id. The decision rows are written later, on the approver's or
    # the expiry sweep's task, so the listener that marks CLI rows does not
    # see them: mark them here from the stored flag.
    agent_session_id = request.agent_session_id
    from_client = bool(request.session_from_client)
    if caller_session:
        agent_session_id = resolve_audit_session_id()
        from_client = session_is_from_client()
    if from_client:
        payload[SESSION_FROM_CLIENT_KEY] = True
    # The reviewer's name next to ``reviewed_by`` on an approve or reject row.
    if principal_name and payload.get("reviewed_by") == operator.sub:
        payload["reviewed_by_name"] = principal_name
    # policy-gate verdict (#130). The parked "request" row is written on the
    # requester's task, where the dispatcher (or, for a step inside a
    # multi-step operation, the step gate ``composite.enforce_subop_policy``,
    # #4646) bound ``policy_decision_var`` to ``needs-approval`` before
    # parking, so the var carries the verdict here.
    # A "decision" row is written on the *approver's* task (var unset) and on
    # a path where no gate ran, so it correctly stays NULL — the operator's
    # approve/reject is recorded via ``result_status``, not a gate verdict.
    # ``hasattr`` guard mirrors ``write_audit_row``: the column lands in ``0051``.
    policy_kwargs: dict[str, Any] = {}
    if hasattr(AuditLog, "policy_decision"):
        policy_kwargs["policy_decision"] = policy_decision_var.get()
    # The matched policy rule (#6044), only on the "request" row. A
    # "decision" row can be written on the task of another call: a retry
    # that expires an old overdue request writes that request's decision
    # row while the retry's own rule is bound.
    if path in _CALLER_PATHS:
        policy_kwargs.update(_request_row_columns())
        payload.update(policy_rule_payload())
    # Session-replay lineage (#2086), off the request row — the durable
    # source of truth, same discipline as ``work_ref`` below. The
    # "request" row and the "decision" rows are written on *different*
    # tasks (requester vs approver / expiry sweep), so reading the
    # contextvars here would drop the originating session on every
    # decision row; the row keeps them aligned with the values the
    # request was parked under.
    #
    # * ``agent_session_id`` — the parking session, so every lifecycle
    #   row anchors in the originating session's replay tree.
    # * ``parent_audit_id`` — the ``approval.request`` audit row's id
    #   (``request.request_audit_id``) for decision rows, which is what
    #   links park → decide into one subtree. The "request" row itself
    #   is the one being written under that id (``audit_id`` equals it),
    #   so it must not self-parent; it takes ``parent_audit_id_var``
    #   instead (NULL for a top-level dispatch; a composite parent's id
    #   when a composite child parked).
    stored_parent = request.request_audit_id
    parent_audit_id: uuid.UUID | None
    if stored_parent is not None and stored_parent != audit_id:
        parent_audit_id = stored_parent
    else:
        parent_audit_id = parent_audit_id_var.get()
    row = AuditLog(
        id=audit_id,
        occurred_at=_now(),
        operator_sub=operator.sub,
        tenant_id=operator.tenant_id,
        target_id=request.target_id,
        parent_audit_id=parent_audit_id,
        agent_session_id=agent_session_id,
        event_class=event_class,
        method="APPROVAL",
        path=path,
        status_code=status_code,
        request_id=None,
        duration_ms=Decimal(str(round(duration_ms, 2))),
        payload=payload,
        # work_ref I2-T1 (#1659): stamp the authorising change-ticket ref
        # straight off the request row rather than re-reading work_ref_var.
        # The approval-request and decision audit rows are written from the
        # request's own lifecycle (create, then a later approve/reject on a
        # different operator's task whose var is unset), so the row is the
        # durable source of truth -- it keeps both audit rows aligned with
        # the value the request was parked under.
        work_ref=request.work_ref,
        run_id=run_id,
        step_id=step_id,
        **policy_kwargs,
    )
    session.add(row)
    await session.flush()


class ResumeRequester(NamedTuple):
    """The requester of an approval request, as saved on its row (#4217).

    Bound in :data:`resume_requester_var` while an approved composite step is
    re-run, so a later step that waits again is saved for this requester.
    """

    sub: str
    act: str | None
    name: str | None
    #: The requester's ``principal_kind`` (#3885). ``None`` on a row parked
    #: before migration 0149.
    kind: str | None = None
    #: The requester's ``client`` block from the stored effect
    #: (AUDIT_CLIENT_IDENTITY). ``None`` when the request has none.
    client: dict[str, Any] | None = None
    #: The requester record on the card (``permission_preflight["requester"]``),
    #: so a re-park keeps it (#6314). ``None`` when the card has none.
    subject: dict[str, Any] | None = None
    #: The people behind the requester (:mod:`~meho_backplane.operations.people_behind`),
    #: so a re-park keeps the list of the first request. ``None`` for a person.
    people_behind: list[dict[str, Any]] | None = None


#: Where the card keeps the requester record (#3898, #6314).
_PERMISSION_PREFLIGHT_KEY = "permission_preflight"
_REQUESTER_KEY = "requester"

#: The requester of the approval request whose call is being re-run
#: (#4217). Bound by :func:`composite_resume_binding` on both resume paths
#: (the reviewer's and the agent's). A step of the step gate or a child call
#: (``dispatch_child``) that waits during the re-run is saved for this
#: requester, so the new request belongs to whoever started the operation,
#: whoever runs the re-run. ``None`` outside such a re-run.
resume_requester_var: ContextVar[ResumeRequester | None] = ContextVar(
    "resume_requester",
    default=None,
)


# code-quality-allow: already over the 100-line limit on main before #1481
# (105 lines); the principal_act source fix adds a handful of lines, not the
# bulk — splitting the row-build + audit-write is out of scope for this fix.
async def create_pending_request(
    session: AsyncSession,
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    target: Any,
    params: dict[str, Any],
    params_hash: str,
    run_id: uuid.UUID | None = None,
    proposed_effect: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
    preview_hash: str | None = None,
    resume_parent: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    requester: ResumeRequester | None = None,
    reveal_secret: bool = False,
    rights_change: bool = False,
    category: str | None = None,
    wait_reason: str | None = None,
    install_category: str | None = None,
    allow_same_call_reuse: bool = True,
) -> ApprovalRequest:
    """Insert a pending :class:`ApprovalRequest` row + one audit row.

    Both the request row and the audit row are inserted in the same
    session / transaction — the caller owns the commit. Until the commit
    lands, no external observer sees the pending request, preserving the
    synchronous-audit invariant.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        operator: The authenticated operator whose dispatch triggered
            the approval gate.
        connector_id: The full ``<impl_id>-<version>`` string passed to
            the dispatcher.
        op_id: The operation id.
        target: The dispatch target (or ``None`` for tenant-wide ops).
            ``target.id`` is extracted when present.
        params: The original dispatch params. Stored verbatim on the row
            (#1503) so any approval surface (REST ``/decide``, MCP by-id
            approve) can re-dispatch a parked direct operator op with the
            stored params, and also used to verify hash consistency.
        params_hash: Pre-computed
            :func:`~meho_backplane.operations._validate.compute_params_hash`
            of *params* (the full approval hash, not the audit hash).
            Stored for resume-path verification.
        run_id: The ``agent_run.id`` this request belongs to; ``None``
            for non-agent-run dispatches.
        proposed_effect: Human-readable summary for the reviewer.
            Defaults to ``{"op_id": op_id, "connector_id": connector_id}``.
        expires_at: Optional caller deadline (#2322). ``None`` defaults to
            ``created_at + APPROVAL_DEFAULT_TTL``; an explicit value is
            honoured but capped at that ceiling. The row is never parked
            with a null deadline — the TTL sweep depends on it.
        preview_hash: The preview-result-hash binding for the
            ``destructive`` tier (#3197). Stored verbatim on the row and
            re-verified at approve time. ``None`` for every non-destructive
            request (the binding is a destructive-tier requirement only);
            the dispatcher supplies the value it already verified against
            the recomputed preview.
        resume_parent: The parent composite to re-enter on resume when this
            park is a composite **sub-op** (#3351):
            ``{"op_id": <composite op_id>, "params": <composite params>}``.
            The sub-op's own ``op_id`` + identity-only gate params are not a
            dispatchable descriptor call, so
            :func:`resume_dispatch_after_approval` re-dispatches this parent
            composite ``_approved=True`` with the approved sub-op pre-cleared.
            Optional keys (#4217): ``"state"``, the JSON the composite saved
            for that re-entry, and ``"after_main_effect": true`` for a step
            that runs after the composite's main change exists.
            ``None`` for a direct-op park (the generic re-dispatch of
            ``op_id`` is used).
        idempotency_key: Optional client-chosen retry key (#3486). When the
            same principal in the same tenant already has a ``pending`` or
            ``approved`` request with this key, that request is returned
            instead of a new one, with no new request row. When
            ``APPROVAL_RETRY_OWN_AUDIT_ROW`` is off, the retry writes no
            audit row. When it is on, the retry writes one
            ``approval.reuse`` audit row and the request carries its id as
            ``_reuse_audit_id`` (see ``_reuse_for_key_retry``). The
            returned row carries the transient attr ``_reused = True`` (and
            no ``_audit_id``), so the caller does not publish a second
            ``approval.pending`` event. A ``pending`` request already past
            its deadline is not returned: it is expired in this
            transaction and a new request is parked. The returned row
            then carries the expired one as ``_expired_request`` (else
            ``None``), so the caller can publish ``approval.expired``
            after commit. That holds also when the returned row is
            another waiting request that the call got (``_reused``).
            ``None`` keeps the old behaviour.
        requester: The requester to save on the row instead of *operator*
            (#4217). Set for a composite step that waits again while an
            approved request is re-run: the new request belongs to the
            requester of the request being re-run, not to the reviewer who
            runs it. ``None`` saves *operator* (and the bound actor) as the
            requester.
        reveal_secret: The caller asked for the hidden values of the answer
            (``reveal_secret: true``; the dispatcher refused an agent
            already). The choice is saved in its own column on the request,
            not with the params and not in the encrypted payload of a
            credential write, so every approved run returns the values,
            also the re-run of a failed credential write after the first
            run deleted the payload. It is not part of *params_hash*, so
            the retry check and the approval surfaces compare the call
            without it.
        category: The category the classification gave the operation
            (the looked-up row's ``policy_category``). Approver routing
            matches a tenant's ``category`` entry on it
            (:func:`~meho_backplane.policy.routing.stamp_approver_set`).
        wait_reason: Why the policy gate made the call wait. The
            ``approval.request`` audit row keeps it as ``wait_reason``.
        install_category: The category the install's policy alone gives
            the operation (the looked-up row's
            ``policy_install_category``). The install's routing entries
            match on it, so a tenant that moves the operation to another
            category cannot take it away from the install's routing.

        allow_same_call_reuse: ``False`` for the check of a step of a
            multi-step operation (``enforce_subop_policy``): the step then
            always gets its own request, whatever ``APPROVAL_SAME_CALL_REUSE``
            says.

    Returns:
        The flushed :class:`ApprovalRequest` row (new, or reused).

    Raises:
        IdempotencyKeyConflictError: *idempotency_key* is held by a live
            request for a different call.
    """
    target_id: uuid.UUID | None = None
    raw_tid = getattr(target, "id", None) if target is not None else None
    if isinstance(raw_tid, uuid.UUID):
        target_id = raw_tid

    expired_request: ApprovalRequest | None = None

    def _reuse(existing: ApprovalRequest) -> ApprovalRequest:
        reused = _reuse_idempotent_request(
            existing,
            op_id=op_id,
            connector_id=connector_id,
            target_id=target_id,
            params=params,
            preview_hash=preview_hash,
        )
        # A late retry can expire its old request and still get another
        # waiting request: the caller then sends the old one's expiry event.
        reused._expired_request = expired_request  # type: ignore[attr-defined]
        return reused

    # What the approve checks will read about who asked. A waiting request
    # is given back to this call only when it was asked the same way.
    if requester is not None:
        principal_kind = requester.kind
        people_behind = requester.people_behind
    else:
        principal_kind = operator.principal_kind.value
        people_behind = await _own_people_behind(session, operator)
    asker = _Asker(principal_kind, behind_subs(people_behind), reveal_secret)

    if idempotency_key is not None:
        await _take_idempotency_key_lock(
            session,
            tenant_id=operator.tenant_id,
            principal_sub=operator.sub,
            idempotency_key=idempotency_key,
        )
        live, expired_request = await _resolve_idempotent_request(
            session,
            tenant_id=operator.tenant_id,
            principal_sub=operator.sub,
            idempotency_key=idempotency_key,
            asker=asker,
        )
        await _refuse_key_held_for_another_call(
            session,
            tenant_id=operator.tenant_id,
            principal_sub=operator.sub,
            idempotency_key=idempotency_key,
            is_this_call=lambda held: _is_same_keyed_call(
                held,
                op_id=op_id,
                connector_id=connector_id,
                target_id=target_id,
                params=params,
                preview_hash=preview_hash,
            ),
        )
        if live is not None:
            return await _reuse_for_key_retry(session, operator, _reuse(live), params)

    if _same_call_reuse_applies(run_id, resume_parent, requester, allow_same_call_reuse):
        same_call = await _find_same_call_pending_request(
            session,
            tenant_id=operator.tenant_id,
            principal_sub=operator.sub,
            op_id=op_id,
            connector_id=connector_id,
            target_id=target_id,
            params=params,
            preview_hash=preview_hash,
            asker=asker,
            principal_act=resolve_actor_sub(),
        )
        if same_call is not None:
            reused = _reuse(same_call)
            await _link_reuse_key(session, reused, idempotency_key)
            await _write_reuse_audit_row(session, operator, reused, params)
            return reused

    if proposed_effect is None:
        proposed_effect = {
            "op_id": op_id,
            "connector_id": connector_id,
        }
        if target_id is not None:
            proposed_effect["target_id"] = str(target_id)
    # The requester's login client and channel (AUDIT_CLIENT_IDENTITY), so
    # every approval surface can say through which tool the change was asked
    # for. A re-park on behalf of an earlier requester keeps that requester's
    # block: *operator* is then the reviewer, not the requester.
    if client_identity_enabled():
        client = build_client_block(operator) if requester is None else requester.client
        if client is not None:
            proposed_effect = {**proposed_effect, CLIENT_PAYLOAD_KEY: client}
    proposed_effect = _with_requester_record(proposed_effect, operator, requester)

    # RFC 8693 ``act`` (#1481): the agent principal acting on the human
    # subject's behalf on a delegated run, read from the same
    # ``actor_delegation`` contextvar the synchronous audit log resolves
    # (``resolve_actor_sub``). Keeps the row's ``principal_sub`` +
    # ``principal_act`` lineage in lock-step with the audit log; a direct
    # human / autonomous-agent call (no delegation bound) resolves to
    # ``None``. The prior ``getattr(operator, "identity_act", None)`` was
    # dead code — ``Operator`` has no such field, so it was always ``None``.
    principal_act: str | None = resolve_actor_sub()
    if requester is not None:
        principal_sub = requester.sub
        principal_act = requester.act
        principal_name = requester.name
    else:
        principal_sub = operator.sub
        principal_name = await principal_display_name(session, operator)

    # External change-ticket ref (work_ref I2-T1 #1659): read from the
    # same request-time ContextVar the audit writers read (bound at the
    # transport/agent boundary, #1657), so the parked request, its
    # decision audit row, and the re-dispatched op all carry one ref.
    # NULL when nothing bound it.
    #
    # Session-replay lineage (#2086): the "request" audit row's id is
    # generated *before* the row insert so it can be persisted as
    # ``request_audit_id`` — the anchor every later lifecycle audit row
    # (decision, resumed dispatch) parent-links to. ``agent_session_id``
    # is resolved here, on the requester's task, where the session
    # context (agent run var, or the MCP transport's session binding)
    # is still live; the approve / resume surfaces run on a different
    # operator's task and re-hydrate it from the row.
    # Stamp the approval-TTL deadline (#2322): default to
    # ``created_at + APPROVAL_DEFAULT_TTL`` and cap any caller override at
    # that ceiling, so every parked row — REST, MCP, and run-bound — leaves
    # the queue with a concrete ``expires_at`` for the sweeper to act on.
    request_audit_id = uuid.uuid4()
    created_at = _now()
    bounded_expires_at = _bounded_expires_at(created_at, expires_at, _resolve_default_ttl())
    handoff_payload: dict[str, Any] | None = None
    stored_params: dict[str, Any] | None = params
    stored_resume_parent = resume_parent
    # A credential write or use, a step of a multi-step operation that is a
    # credential write or use, and a call whose param a connector hides (the
    # manifest of a k8s.apply of a Secret), keep their params and the
    # parent's params only in the encrypted one-time handoff: the row never
    # holds them, also not when the request is approved and never runs.
    parent_op_id = resume_parent.get("op_id") if resume_parent is not None else None
    if (
        classify_op(op_id) in SECRET_REQUEST_OP_CLASSES
        or (
            isinstance(parent_op_id, str) and classify_op(parent_op_id) in SECRET_REQUEST_OP_CLASSES
        )
        or (params is not None and hidden_stored_param(op_id, params) is not None)
    ):
        handoff_payload = {"params": params, "resume_parent": resume_parent}
        stored_params = None
        if resume_parent is not None:
            stored_resume_parent = {
                key: resume_parent[key] for key in ("op_id", "request_sent") if key in resume_parent
            }
    request = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=operator.tenant_id,
        run_id=run_id,
        principal_sub=principal_sub,
        principal_act=principal_act,
        # Requester display name hoisted at park time (#3300) so every
        # approvals surface renders a name alongside ``principal_sub``: the
        # JWT ``name`` claim, else for a machine token the registered agent
        # name or the client id (#3885). ``None`` when there is none --
        # fail-open to the raw sub. Same pattern as audit_log.principal_name.
        principal_name=principal_name,
        principal_kind=principal_kind,
        people_behind=people_behind,
        op_id=op_id,
        connector_id=connector_id,
        target_id=target_id,
        params_hash=params_hash,
        preview_hash=preview_hash,
        params=stored_params,
        proposed_effect=proposed_effect,
        status=ApprovalRequestStatus.PENDING.value,
        created_at=created_at,
        expires_at=bounded_expires_at,
        work_ref=work_ref_var.get(),
        agent_session_id=resolve_audit_session_id(),
        session_from_client=session_is_from_client(),
        request_audit_id=request_audit_id,
        resume_parent=stored_resume_parent,
        idempotency_key=idempotency_key,
        release_id=running_release_id(),
        target_hash=await park_time_target_hash(
            session, tenant_id=operator.tenant_id, target_id=target_id
        ),
        reveal_secret=reveal_secret,
        composite_step=_is_composite_step(),
        allowed_connectors=stored_connector_limit(),
        **_policy_rule_fields(),
    )
    if rights_change or is_rights_change(request):
        request.rights_change = True
        # Who asked where: in MEHO's own login realm only a request asked
        # with a click in the web console is approved by the login that asked.
        asked_in_console = (
            requester is None and operator.web_session and is_human_principal(operator)
        )
        request.proposed_effect = {
            **(request.proposed_effect or {}),
            ASKED_IN_WEB_CONSOLE: asked_in_console,
        }
    try:
        await stamp_approver_set(
            session, request, category=category, install_category=install_category
        )
    except ValueError as exc:
        await _audit_routing_refusal(operator, request, request_audit_id, str(exc))
        raise
    if idempotency_key is None:
        session.add(request)
        await session.flush()
    else:
        winner = await _insert_idempotent_request(
            session, request, idempotency_key=idempotency_key, asker=asker
        )
        if winner is not None:
            return await _reuse_for_key_retry(session, operator, _reuse(winner), params)
    request._expired_request = expired_request  # type: ignore[attr-defined]
    if handoff_payload is not None:
        request.execution_handle = store_handoff(
            session, request.id, request.tenant_id, handoff_payload
        )
        await session.flush()

    # Synchronous "request" audit row -- same transaction.
    await _write_audit_row(
        session,
        audit_id=request_audit_id,
        event_class=AuditEventClass.POLICY_DECISION,
        operator=operator,
        request=request,
        path="approval.request",
        status_code=_APPROVAL_STATUS_CODE,
        duration_ms=0.0,
        extra_payload={"wait_reason": wait_reason} if wait_reason else None,
    )

    _log().info(
        "approval_request_created",
        approval_request_id=str(request.id),
        op_id=op_id,
        connector_id=connector_id,
        principal_sub=principal_sub,
        tenant_id=str(operator.tenant_id),
        run_id=str(run_id) if run_id else None,
    )
    # Expose the audit_id as a transient attr so callers can record the
    # ``approval.pending`` step event AFTER they commit the session.
    # A publish-before-commit would surface a phantom event if the commit
    # fails. The transient attr does not persist to the DB row.
    request._audit_id = request_audit_id  # type: ignore[attr-defined]
    return request


async def rights_change_note(request: ApprovalRequest) -> str | None:
    """The note for a waiting change of rights that no other admin could approve.

    * A sure count of zero admins gives the "no admin" note for every
      waiting change of rights (:func:`is_rights_change`), also
      ``policy.apply``, ``policy.classify`` and a change in any other realm.
    * The note that the person who asked is the only admin shows only where
      a platform admin must approve: the card's ``login_realm`` fact is true
      (:func:`~meho_backplane.operations.grant_approval.grant_facts`; a
      missing realm fact counts as true). There the person who asked
      approves it only as a platform admin who asked in the web console.

    ``None`` for every other request, when another admin is found, when the
    person who asked is not an admin, and when MEHO cannot count the admins
    (:func:`~meho_backplane.auth.tenant_admins.only_admin_note`). The request
    is stored all the same, so an admin added later can still approve it.

    The note is extra help only. When reading it fails, the answer is
    ``None`` and the request still waits for approval as stored.
    """
    if not is_rights_change(request):
        return None
    facts = grant_facts(request)
    try:
        return await only_admin_note(
            request.tenant_id,
            request.principal_sub,
            login_realm=facts is not None and facts.login_realm,
        )
    except Exception:
        _log().warning("rights_change_note_failed", request_id=str(request.id), exc_info=True)
        return None


async def _audit_routing_refusal(
    operator: Operator, request: ApprovalRequest, audit_id: uuid.UUID, routing_error: str
) -> None:
    """Write the audit row of a park refused because the stored routing is not valid.

    The caller's transaction rolls back with the refusal, so the row is
    written in its own session. Its id *audit_id* is the request's
    ``request_audit_id``, the id the "request" row would have had. A failed
    write is logged; the park is refused either way.
    """
    from meho_backplane.db.engine import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            await _write_audit_row(
                session,
                audit_id=audit_id,
                event_class=AuditEventClass.POLICY_DECISION,
                operator=operator,
                request=request,
                path="approval.request",
                status_code=_ROUTING_REFUSED_STATUS_CODE,
                duration_ms=0.0,
                extra_payload={
                    "result_status": "routing_invalid",
                    "approver_rule": ROUTING_INVALID_RULE,
                    "routing_error": routing_error,
                },
            )
            await session.commit()
    except Exception:
        _log().exception(
            "approval_routing_refusal_audit_failed", approval_request_id=str(request.id)
        )


def _approval_lock_key(scope: str, key: str) -> int:
    """Map *key* under *scope* to a stable, non-negative 63-bit advisory-lock key.

    A blake2b digest under its own prefix, masked to 63 bits so it fits the
    ``bigint`` that ``pg_advisory_xact_lock`` takes. The same shape as the
    other advisory-lock keys in the code base.
    """
    label = f"meho:{scope}\0{key}"
    digest = hashlib.blake2b(label.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


async def take_approval_lock(session: AsyncSession, scope: str, key: str) -> None:
    """Take the transaction-level advisory lock of an approval step.

    Two approves that use the same proof (one sign-in, or one passkey touch)
    can run at the same moment, on two replicas. Each takes this lock first,
    so the second waits until the first commits or rolls back. PostgreSQL
    releases the lock with the transaction. On other databases (the
    single-writer SQLite test path) there is no lock.
    """
    conn = await session.connection()
    if conn.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": _approval_lock_key(scope, key)}
        )


def _is_destructive(request: ApprovalRequest) -> bool:
    """Whether the parked operation counts as ``destructive`` (:func:`approval_risk_level`)."""
    return approval_risk_level(request) == "destructive"


async def _check_sign_in_use(
    session: AsyncSession,
    request: ApprovalRequest,
    operator: Operator,
    approval_auth: ApprovalAuth,
) -> bool:
    """Check the sign-in's age and use for a token approve; return whether it is single-use.

    The sign-in must be young enough for the request's risk level
    (:func:`~meho_backplane.auth.approval_sign_in.approval_sign_in_max_age`,
    :class:`ApprovalSignInTooOldError` otherwise): the approval window, or
    for a ``destructive`` request its own limit. Inside the window one
    sign-in may approve several requests (#5515). A ``destructive`` request
    is the exception: its sign-in must not have approved anything else or
    removed the person's own block, and after it that sign-in approves
    nothing more (:class:`ApprovalSignInReusedError` in both cases). The
    check is the same for an approve on REST and in the web interface.

    The earlier uses of the sign-in are the person's audit rows with the
    same ``sign_in_key``, written since the sign-in: the
    ``approval.decision`` rows, and the ``principal.unblock`` row of an
    own-login unblock (:func:`~meho_backplane.auth.sign_in_use.earlier_sign_in_uses`).
    The scan is short: the window is minutes.

    Two approves with the same sign-in can run at the same moment, on two
    replicas. So the check first takes a transaction-level PostgreSQL
    advisory lock on the sign-in key. The second approve waits until the
    first commits or rolls back, and then its scan sees the first audit
    row. PostgreSQL releases the lock with the transaction. On other
    databases (the single-writer SQLite test path) there is no lock.

    The scan starts :data:`~meho_backplane.auth.fresh_sign_in.CLOCK_SKEW_SECONDS`
    before ``auth_time``: the fresh sign-in check accepts an ``auth_time``
    that far in the future, so an earlier approve of the same sign-in can
    have an audit time before ``auth_time``.
    """
    key = approval_auth.sign_in_key(operator.sub)
    destructive = _is_destructive(request)
    from meho_backplane.settings import get_settings

    max_age = approval_sign_in_max_age(get_settings(), destructive=destructive)
    if _now().timestamp() - approval_auth.auth_time > max_age:
        _log().warning(
            "approval_sign_in_too_old",
            approval_request_id=str(request.id),
            operator_sub=operator.sub,
            destructive=destructive,
            max_age_seconds=max_age,
        )
        raise ApprovalSignInTooOldError(request.id, max_age_seconds=max_age)
    await take_approval_lock(session, SIGN_IN_LOCK_SCOPE, key)
    uses = await earlier_sign_in_uses(
        session, sub=operator.sub, sign_in_key=key, auth_time=approval_auth.auth_time
    )
    for use in uses:
        # A destructive approve needs a sign-in no other step used (an approve
        # or the removal of the person's own block). Any other approve is
        # refused only after a sign-in that a destructive approve used up.
        if destructive or use.single_use:
            _log().warning(
                "approval_sign_in_reused",
                approval_request_id=str(request.id),
                operator_sub=operator.sub,
                destructive=destructive,
            )
            raise ApprovalSignInReusedError(request.id)
    return destructive


#: The longest ``User-Agent`` value stored on a decision row.
_USER_AGENT_MAX_LENGTH: Final[int] = 256


def _approver_client_fields(operator: Operator, user_agent: str | None) -> dict[str, Any]:
    """Name the approver's login client on the decision row (AUDIT_CLIENT_IDENTITY).

    ``approver_azp`` and ``approver_channel`` come from the checked login.
    ``approver_user_agent`` is the ``User-Agent`` header of a REST approve,
    cleaned like every client text and cut to 256 characters. The tool sends
    it about itself and MEHO cannot check it, so it is marked
    ``"self_declared": true``. Empty when the switch is off.
    """
    if not client_identity_enabled():
        return {}
    fields: dict[str, Any] = {
        "approver_azp": operator.azp,
        "approver_channel": channel_of(operator),
    }
    cleaned = clean_client_text(user_agent, _USER_AGENT_MAX_LENGTH)
    if cleaned:
        fields["approver_user_agent"] = {"value": cleaned, "self_declared": True}
    return fields


# code-quality-allow: 101 lines but ~70 are the Args/Raises docstring documenting
# the four preconditions + the params/reason contract; the executable body is ~15
# lines. Splitting it would scatter one linear approve flow for a line-count win.
async def approve_request(
    session: AsyncSession,
    request_id: uuid.UUID,
    *,
    operator: Operator,
    params: dict[str, Any] | None = None,
    reason: str = "",
    approval_auth: ApprovalAuth | None = None,
    user_agent: str | None = None,
) -> ApprovalRequest:
    """Approve a pending request.

    Loads the :class:`ApprovalRequest` row, verifies:

    1. The row exists and belongs to ``operator.tenant_id`` (else 404).
    2. The operator may decide — ``operator``-or-higher, or a
       ``read_only`` principal carrying the ``approver`` capability
       (#3243); else 403.
    3. The row is still ``pending`` (else 409).
    4. Who may approve (#1401, #3328, :func:`_check_approver_group`):
       the approver's own change only for an Admin, or for a
       ``safe`` / ``caution`` change under the audited break-glass mode
       (``Settings.approval_allow_self_approval``); else 403,
       :class:`SelfApprovalForbiddenError`. Another person's
       ``dangerous`` or ``destructive`` change only for the groups of the
       decision record; else 403, :class:`ApprovalGroupForbiddenError`.
       Checked before the hash check so the approver learns *why* they
       are refused rather than being told their params mismatch.
       When the operation carries a role floor
       (:mod:`meho_backplane.operations.role_floors`), the approver's role
       must meet it; else 403, :class:`ApprovalRoleFloorError`. The
       approver is read as recorded at the decision, so the person who
       approves must hold the role the operation needs.
    5. If *params* is supplied, it matches the stored ``params_hash``
       (:func:`params_hash_matches`; else 422, :class:`ParamsMismatchError`).
       The hash check is **skipped** when *params* is ``None`` — the
       operator-decision path (G11.2-T5 MCP/CLI surface) approves by
       request id alone and does not have the original params. The
       agent's REST path still supplies them so the swap defence applies
       on that branch.
    6. If the row is ``destructive``-tier, it carries a ``preview_hash``
       binding (else 422, :class:`PreviewBindingMissingError`). The
       preview-result-hash re-verification (#3197, decision requirement 2)
       — checked on every approve surface, independent of *params*.

    Then:

    * Flips the row to ``approved``, stamps ``reviewed_by`` + ``decided_at``.
    * Writes a synchronous "decision" audit row in the same transaction.
      Its payload carries ``reviewed_by``, ``self_approved`` and
      ``approval_rule`` (the :class:`ApprovalRule` that allowed the
      approval). The ``approval.approved`` step event carries the same
      three fields.

    The **re-dispatch** (executing the approved op) happens *after* the
    caller commits this transaction. The ``POST /api/v1/approvals/{id}/
    approve`` REST route calls :func:`~meho_backplane.operations.dispatcher.dispatch`
    with ``_approved=True``: the approval is not asked for again, and the
    run checks again what can change while the call waits
    (:mod:`~meho_backplane.operations.approved_run_rights`). The MCP/CLI
    operator-decision path commits the decision without re-dispatching —
    the agent picks up execution separately. Separating the decision commit from the re-dispatch
    means the approval is durable even if the re-dispatch fails.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        request_id: The pending row's id.
        operator: The authenticated reviewer.
        params: The **original** dispatch params (un-modified). Required
            on the REST path so the hash check applies; ``None`` on the
            MCP/CLI operator-decision path.
        reason: Optional human-readable approval reason (recorded in the
            audit payload). Mirrors :func:`reject_request`.
        approval_auth: How the person proved they were present (#3979): a
            fresh sign-in or a one-time code, on REST or in the web
            interface. Recorded in the audit payload as ``approval_auth``.
            Inside the approval window one sign-in may approve several
            requests; a ``destructive`` request needs a sign-in of its own
            (#5515). Every production approve path passes it.
        user_agent: The ``User-Agent`` header of a REST approve. With
            ``AUDIT_CLIENT_IDENTITY`` on it is stored on the decision row,
            marked as what the tool says about itself. ``None`` elsewhere.

    Returns:
        The updated, flushed :class:`ApprovalRequest`.

    Raises:
        ApprovalNotFoundError: No row for *request_id* in this tenant.
        UnauthorizedApprovalError: Operator lacks approvals access
            (``read_only`` without the ``approver`` capability, #3243).
        ApprovalRequestAlreadyDecidedError: Row is not ``pending``.
        SelfApprovalForbiddenError: Approver is the requester, is not a
            Admin, and break-glass does not cover the change
            (G11.7-T1 #1401, #3328).
        ApprovalGroupForbiddenError: Another person's ``dangerous`` or
            ``destructive`` change, and the approver is not in a group that
            may approve it (#3328).
        ApprovalRoutingError: With approver routing on, the person is not
            in a login group kind the request stored as the ones that may
            approve it (:mod:`meho_backplane.policy.routing`).
        ApprovalRoleFloorError: The operation needs a role from the person
            who runs it, and the approver's role is below it.
        ParamsMismatchError: Hash of *params* != stored ``params_hash``
            (only raised when *params* is supplied).
        PreviewBindingMissingError: The row is ``destructive``-tier but
            carries no ``preview_hash`` binding (#3197).
        DelegatedReasonInvalidError: *reason* names an instructing human
            (``Instructed-by:``) but its trailer block is incomplete (#3692).
        DelegatedDecisionForbiddenError: *reason* is a valid delegated
            decision but the deployment is not lab-class (#3693).
        ApprovalSignInReusedError: The request is ``destructive`` and
            *approval_auth* names a sign-in that already approved something,
            or the sign-in already approved a ``destructive`` request (#5515).
        ApprovalSignInTooOldError: The sign-in is older than the limit of
            the request's risk level: the approval window, or for a
            ``destructive`` request ``PER_ACTION_SIGN_IN_MAX_AGE_SECONDS``.
    """
    request, rule = await _load_pending_for_approval(
        session, request_id, operator=operator, params=params
    )
    # Validate before any state change so a bad or forbidden delegated
    # reason leaves the row pending (#3692, #3693).
    delegation = parse_delegation(reason)
    _check_delegated_allowed(delegation, request=request, operator=operator, decision="approved")
    single_use = False
    if approval_auth is not None:
        single_use = await _check_sign_in_use(session, request, operator, approval_auth)

    now = _now()
    request.status = ApprovalRequestStatus.APPROVED.value
    request.reviewed_by = operator.sub
    # Reviewer display name hoisted from the deciding operator's JWT ``name``
    # claim (#3300), the peer of ``principal_name`` for ``reviewed_by``.
    # ``None`` when the token carried no name -- fail-open to the raw sub.
    request.reviewed_by_name = operator.name
    request.decided_at = now
    await session.flush()

    self_approved = is_self_approval(operator.sub, request)
    extra: dict[str, Any] = {
        "decision": "approved",
        "reviewed_by": operator.sub,
        "self_approved": self_approved,
        "approval_rule": rule.value,
        # Read when the risk level of the call goes up before it runs again
        # (:mod:`~meho_backplane.operations.approved_run_rights`).
        "approver_may_approve_high_risk": may_approve_high_risk(operator),
        **grant_audit_fields(request, operator),
        **_behind_audit_fields(operator.sub, request),
    }
    if reason:
        extra["reason"] = reason
    # The install's routing entry that counted at this click (the floor).
    floor = getattr(request, "_approver_floor", None)
    if floor is not None:
        extra["approver_floor"] = floor
    if approval_auth is not None:
        extra["approval_auth"] = approval_auth.audit_payload(operator.sub, single_use=single_use)
    extra.update(_approver_client_fields(operator, user_agent))
    _record_delegation(extra, delegation, request=request, operator=operator, decision="approved")

    approve_audit_id = uuid.uuid4()
    await _write_audit_row(
        session,
        audit_id=approve_audit_id,
        event_class=AuditEventClass.APPROVAL_DECISION,
        operator=operator,
        request=request,
        path="approval.decision",
        status_code=_DECISION_STATUS_CODE_APPROVED,
        duration_ms=0.0,
        extra_payload=extra,
    )

    _log().info(
        "approval_request_approved",
        approval_request_id=str(request_id),
        op_id=request.op_id,
        reviewed_by=operator.sub,
        self_approved=self_approved,
        approval_rule=rule.value,
        tenant_id=str(operator.tenant_id),
    )
    request._audit_id = approve_audit_id  # type: ignore[attr-defined]
    # Read by record_approval_step_event for the ``approval.approved`` event.
    request._approval_rule = rule  # type: ignore[attr-defined]
    return request


async def reject_request(
    session: AsyncSession,
    request_id: uuid.UUID,
    *,
    operator: Operator,
    reason: str = "",
) -> ApprovalRequest:
    """Reject a pending request — the original dispatch is not executed.

    Same preconditions as :func:`approve_request` (tenant isolation,
    role check, ``pending`` guard) minus the hash check (no params
    needed to reject). Flips the row to ``rejected``, stamps
    ``reviewed_by`` + ``decided_at``, writes the decision audit row.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        request_id: The pending row's id.
        operator: The authenticated reviewer.
        reason: Optional human-readable rejection reason (recorded in
            the audit payload).

    Returns:
        The updated, flushed :class:`ApprovalRequest`.

    Raises:
        ApprovalNotFoundError: No row for *request_id* in this tenant.
        UnauthorizedApprovalError: Operator lacks approvals access
            (``read_only`` without the ``approver`` capability, #3243).
        ApprovalRequestAlreadyDecidedError: Row is not ``pending``.
        DelegatedReasonInvalidError: *reason* names an instructing human
            (``Instructed-by:``) but its trailer block is incomplete (#3692).
        DelegatedDecisionForbiddenError: *reason* is a valid delegated
            decision but the deployment is not lab-class (#3693).
    """
    _check_reviewer_role(operator)

    # Row lock (security F12 / #274): a competing approve / reject / expiry
    # on this row serialises on the lock, so the loser re-reads the
    # committed terminal state and refuses rather than overwriting it.
    request = await _load_for_tenant(session, request_id, operator.tenant_id, for_update=True)

    if request.status != ApprovalRequestStatus.PENDING.value:
        raise ApprovalRequestAlreadyDecidedError(request_id, request.status)
    delegation = parse_delegation(reason)
    _check_delegated_allowed(delegation, request=request, operator=operator, decision="rejected")

    now = _now()
    request.status = ApprovalRequestStatus.REJECTED.value
    request.reviewed_by = operator.sub
    # Reviewer display name hoisted from the deciding operator's JWT ``name``
    # claim (#3300); ``None`` fail-opens to the raw sub. See approve_request.
    request.reviewed_by_name = operator.name
    request.decided_at = now
    await end_run_input(session, request)
    await session.flush()

    extra: dict[str, Any] = {"decision": "rejected", "reviewed_by": operator.sub}
    if reason:
        extra["reason"] = reason
    _record_delegation(extra, delegation, request=request, operator=operator, decision="rejected")

    reject_audit_id = uuid.uuid4()
    await _write_audit_row(
        session,
        audit_id=reject_audit_id,
        event_class=AuditEventClass.APPROVAL_DECISION,
        operator=operator,
        request=request,
        path="approval.decision",
        status_code=_DECISION_STATUS_CODE_REJECTED,
        duration_ms=0.0,
        extra_payload=extra,
    )

    _log().info(
        "approval_request_rejected",
        approval_request_id=str(request_id),
        op_id=request.op_id,
        reviewed_by=operator.sub,
        tenant_id=str(operator.tenant_id),
        reason=reason or None,
    )
    request._audit_id = reject_audit_id  # type: ignore[attr-defined]
    return request


async def withdraw_request(
    session: AsyncSession,
    request_id: uuid.UUID,
    *,
    operator: Operator,
    reason: str = "",
) -> ApprovalRequest:
    """The sender takes back its own pending request (#4987).

    A withdrawal is not a decision. Any kind of principal (person, agent,
    service) may withdraw, but only the one that sent the request
    (``operator.sub == request.principal_sub``). The row moves to
    ``withdrawn`` with ``withdrawn_at`` and ``withdraw_reason`` set;
    ``reviewed_by`` and ``decided_at`` stay NULL. One ``approval.withdraw``
    audit row of class ``approval_decision`` records it; the audit query
    shows its result as ``withdrawn``. The original call never runs: approve, reject
    and decide then see a closed row and refuse (409).

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        request_id: The pending row's id.
        operator: The authenticated sender.
        reason: Optional free-text reason, stored on the row and the
            audit row.

    Returns:
        The updated, flushed :class:`ApprovalRequest`.

    Raises:
        ApprovalNotFoundError: No row for *request_id* in this tenant.
        WithdrawForbiddenError: The caller did not send the request.
        ApprovalRequestAlreadyDecidedError: Row is not ``pending``.
    """
    # Same row lock as approve / reject: a racing decision or expiry
    # waits, then sees the closed row and refuses.
    request = await _load_for_tenant(session, request_id, operator.tenant_id, for_update=True)
    # Owner check first, so a non-sender learns nothing about the state.
    if operator.sub != request.principal_sub:
        raise WithdrawForbiddenError(request_id=request_id, principal_sub=operator.sub)
    if request.status != ApprovalRequestStatus.PENDING.value:
        raise ApprovalRequestAlreadyDecidedError(request_id, request.status)

    request.status = ApprovalRequestStatus.WITHDRAWN.value
    request.withdrawn_at = _now()
    request.withdraw_reason = reason or None
    await end_run_input(session, request)
    await session.flush()

    extra: dict[str, Any] = {"withdrawn_by": operator.sub}
    if reason:
        extra["reason"] = reason
    withdraw_audit_id = uuid.uuid4()
    await _write_audit_row(
        session,
        audit_id=withdraw_audit_id,
        # Not OPERATION_EXECUTION: the operation never ran. The row closes
        # the approval request, like a reject or an expiry row (#5717).
        event_class=AuditEventClass.APPROVAL_DECISION,
        operator=operator,
        request=request,
        path="approval.withdraw",
        status_code=_DECISION_STATUS_CODE_APPROVED,
        duration_ms=0.0,
        extra_payload=extra,
    )

    _log().info(
        "approval_request_withdrawn",
        approval_request_id=str(request_id),
        op_id=request.op_id,
        principal_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
    )
    request._audit_id = withdraw_audit_id  # type: ignore[attr-defined]
    return request


def params_with_secret_fields_hidden(
    params: dict[str, Any] | None, *, op_id: str
) -> dict[str, Any] | None:
    """The stored params with each secret-named value replaced, or ``None``.

    ``None`` when *params* is ``None`` or holds no secret-named value
    (``password``, ``licenseKey``, ``client_secret``, ...; the name rule of
    :func:`~meho_backplane.broadcast.events.scrub_secret_named_values`), so
    a row without such a field is never rewritten. A number under such a
    name (a PIN, a password sent as a number) is hidden too. The param that
    *op_id* declared with
    :func:`~meho_backplane.op_classes.declare_hidden_stored_param` is
    replaced too, when its check flags the params (the manifest of a
    ``k8s.apply`` that holds a Secret). Each value of the map param that
    *op_id* named with
    :func:`~meho_backplane.op_classes.declare_hidden_stored_map_values` is
    replaced too; its key names stay (the ``extras_changes`` of
    ``targets.update``). The other fields keep their value, as in the audit
    row's raw copy.
    """
    if params is None:
        return None
    hidden, found = scrub_secret_named_values(params, hide_numbers=True)
    name = hidden_stored_param(op_id, params)
    if name is not None:
        hidden = {**hidden, name: HIDDEN_PARAM_MARKER}
        found = True
    map_name = hidden_stored_map_values(op_id)
    values = hidden.get(map_name) if map_name is not None else None
    if map_name is not None and isinstance(values, dict) and values:
        hidden = {
            **hidden,
            map_name: {
                key: None if value is None else HIDDEN_PARAM_MARKER for key, value in values.items()
            },
        }
        found = found or any(value is not None for value in values.values())
    if not found:
        return None
    return cast(dict[str, Any], hidden)


def resume_parent_without_inputs(
    resume_parent: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The parent of a step request that never runs, kept as its ``op_id`` only.

    A request that ends without a run never goes back into its multi-step
    operation, so the row needs no inputs of that operation (its params,
    saved state and finished steps). The approval pages and the runbook
    check read only ``op_id``. ``None`` when *resume_parent* holds nothing
    else, so such a row is not written.
    """
    if resume_parent is None or set(resume_parent) <= {"op_id"}:
        return None
    return {"op_id": resume_parent.get("op_id")}


async def end_run_input(session: AsyncSession, request: ApprovalRequest) -> None:
    """Drop what MEHO kept only to run *request*: the row is ending without a run.

    Deletes the encrypted custody row of a credential write
    (:func:`~meho_backplane.operations.approval_handoff.discard`) and
    replaces each secret-named value in the stored ``params`` (a password or
    a licence key inside a deployment spec) with a marker. The parent of a
    step of a multi-step operation keeps only its ``op_id``
    (:func:`resume_parent_without_inputs`). The caller holds
    the row lock, flushes and commits. Rejected, withdrawn and expired
    requests end here; a request that runs has its values hidden when the
    run claim is won (:func:`claim_resume`).
    """
    await discard_handoff(session, request.id, request.tenant_id)
    hidden = params_with_secret_fields_hidden(request.params, op_id=request.op_id)
    if hidden is not None:
        request.params = hidden
    parent = resume_parent_without_inputs(request.resume_parent)
    if parent is not None:
        request.resume_parent = parent


async def claim_resume(
    request_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> bool:
    """Win the exactly-one-resumer claim for *request_id* (#2293, G0.30).

    A single conditional ``UPDATE approval_request SET resumed_at = :now
    WHERE id = :request_id AND resumed_at IS NULL AND status = 'approved'``
    -- the atomic claim every resumer of an approved op must win before it
    re-dispatches ``dispatch(..., _approved=True)``: the in-process agent
    waiter (:mod:`meho_backplane.agent.approval_wait`), the shared
    :func:`resume_dispatch_after_approval` operator path (REST ``/approve``
    + ``/decide``, MCP by-id approve, UI approve), and any future resumer.
    Returns ``True`` when this caller set ``resumed_at`` (one row touched)
    and therefore owns the single execution; ``False`` when another
    resumer already claimed it, or the row is not ``approved`` (zero rows
    touched) and this caller must no-op cleanly.

    The ``status = 'approved'`` predicate (security F12 / #274) makes the
    claim require the approved state *in the same statement* as the
    single-resumer latch, so no execution can follow a losing/rejected/
    expired transition even under a race: a request that a competing
    reject or expiry won cannot be claimed for execution. Every resume
    surface already only reaches here after the decision committed
    ``approved``, so this tightens the invariant without changing the
    live control flow -- it fails closed if a resume is ever attempted
    against a non-approved row.

    Why a conditional ``UPDATE`` rather than a Python ``if`` + edit (the
    same reasoning as :func:`~meho_backplane.operations.agent_run.extend_lease`'s
    lease claim): a read-then-write would race a concurrent resumer -- the
    agent waiter that read the approved row vs. the operator surface
    that approved it -- with both reading
    ``resumed_at IS NULL`` and both dispatching the write. The predicate
    and the write commit together in one statement, so at most one resumer
    wins even across processes / pods: a concurrent claim blocks on the
    row lock, then re-evaluates the predicate after commit and loses. No
    advisory locks.

    Runs in its **own committed transaction** so the latch is durable the
    instant it is won -- the decision commit and the re-dispatch happen in
    separate sessions, and the claim must be visible to a racing resumer
    independently of either. The column is a one-way latch (never cleared),
    so a dispatch that fails after a won claim is not silently retried into
    a possible double write; the residual expiry follow-up (out of scope
    here) makes such a void approval visibly "expired".

    The winner already read the stored params into memory for its run. So
    the same transaction replaces each secret-named value in the row's
    ``params`` (a password or a licence key inside a deployment spec) with
    a marker: the row keeps them only until the run starts. A later re-run
    of a failed run needs the params sent again
    (:func:`~meho_backplane.operations.approval_rerun.rerun_failed_request`).
    """
    from meho_backplane.db.engine import get_sessionmaker

    stamp = now or _now()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # ``synchronize_session=False`` -- no ORM identity map to keep in
        # step, this is a bare claim probe. ``id`` is the primary key, so
        # the claim returns one row when won and none when lost.
        claimed = await session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == request_id)
            .where(ApprovalRequest.resumed_at.is_(None))
            .where(ApprovalRequest.status == ApprovalRequestStatus.APPROVED.value)
            .values(resumed_at=stamp)
            .returning(ApprovalRequest.params, ApprovalRequest.op_id)
            .execution_options(synchronize_session=False)
        )
        row = claimed.first()
        won = row is not None
        hidden = params_with_secret_fields_hidden(row[0], op_id=row[1]) if row is not None else None
        if hidden is not None:
            await session.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == request_id)
                .values(params=hidden)
                .execution_options(synchronize_session=False)
            )
        await session.commit()
    return won


async def find_remote_write_approval(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    approval_request_id: uuid.UUID,
    asker_sub: str,
    connector_id: str,
    op_id: str,
    target_id: uuid.UUID | None,
    params: dict[str, Any],
    now: datetime | None = None,
) -> ApprovalRequest | None:
    """Find the committed approval that authorises a remote-write mint (read-only).

    The approval is the request named by *approval_request_id*. Only the
    login that asked (*asker_sub*) can use it, and it must match the
    connector. An approval past its time limit is refused.

    Mechanism 1 of the satellite write-path composed gate (#3189, design §3):
    the ``remote-write`` (caution) tier mints a capability **only** against a
    committed :class:`ApprovalRequest` for the *identical*
    ``(tenant, op, target, params)`` -- the human approval decision is the
    mint's authorization, as :func:`resume_dispatch_after_approval` runs an
    approved op without asking for the approval again
    (``dispatch(..., _approved=True)``). The ``params_hash`` predicate (any of
    :func:`params_hash_candidates` for *params*) is the swap defence
    (#1503 / #3197): a mint whose params differ from the approved ones matches
    no row and is refused.

    Read-only: it never mutates a row, so the mint can refuse fail-closed (no
    approval, or -- later -- the signing key is unavailable) **without**
    consuming an approval. The single-use consumption is a separate atomic
    step, :func:`consume_remote_write_approval`, run only once the mint is
    certain to proceed. Returns the matching un-consumed
    (``resumed_at IS NULL``) approval, or ``None``.
    """
    target_clause = (
        ApprovalRequest.target_id == target_id
        if target_id is not None
        else ApprovalRequest.target_id.is_(None)
    )
    stmt = select(ApprovalRequest).where(
        ApprovalRequest.id == approval_request_id,
        ApprovalRequest.tenant_id == tenant_id,
        ApprovalRequest.principal_sub == asker_sub,
        ApprovalRequest.connector_id == connector_id,
        ApprovalRequest.op_id == op_id,
        ApprovalRequest.params_hash.in_(params_hash_candidates(params)),
        ApprovalRequest.status == ApprovalRequestStatus.APPROVED.value,
        ApprovalRequest.resumed_at.is_(None),
        target_clause,
    )
    result = await session.execute(stmt)
    approval = result.scalars().first()
    if approval is None:
        return None
    if _deadline_has_passed(approval, now or _now(), _resolve_default_ttl()):
        return None
    return approval


async def consume_remote_write_approval(
    session: AsyncSession,
    request: ApprovalRequest,
    *,
    now: datetime | None = None,
) -> bool:
    """Atomically claim a remote-write approval as single-use, in the mint session.

    Wins the same one-way ``resumed_at`` conditional-``UPDATE`` latch the
    re-dispatch path uses (:func:`claim_resume`) and the command consume path
    mirrors (``consume_command``), but run **in the caller's mint session**
    (flushed, not committed on its own) so the approval is consumed **iff** the
    mint commits: a rolled-back mint frees the approval, and two concurrent
    mints cannot both bind the same approval (the loser touches zero rows and
    gets ``False``). A caution op destined for a runner is executed by the
    mint, so the mint is the sole consumer of ``resumed_at`` -- there is no
    competing direct re-dispatch for a runner-targeted op.

    Returns ``True`` when this caller set ``resumed_at`` (owns the single mint),
    ``False`` when a racing mint already claimed it or the request is not
    ``approved`` any more (it expired after the read).
    """
    stamp = now or _now()
    claimed = await session.execute(
        update(ApprovalRequest)
        .where(
            ApprovalRequest.id == request.id,
            ApprovalRequest.resumed_at.is_(None),
            ApprovalRequest.status == ApprovalRequestStatus.APPROVED.value,
        )
        .values(resumed_at=stamp)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    if cast(CursorResult[Any], claimed).rowcount != 1:
        return False
    request.resumed_at = stamp
    _log().info(
        "remote_write_approval_bound",
        approval_request_id=str(request.id),
        op_id=request.op_id,
        tenant_id=str(request.tenant_id),
    )
    return True


async def resume_dispatch_after_approval(
    *,
    operator: Operator,
    request: ApprovalRequest,
    params: dict[str, Any] | None = None,
    requester_fetch: bool = False,
) -> Any:
    """Re-hydrate the stored target and re-dispatch an approved op (#1503, G11.7-T1).

    The single execute-after-approve entry point shared by every operator
    approval surface — REST ``/approve`` and ``/decide``, and the MCP
    by-id approve tool. It re-runs the original dispatch under the
    committed approval decision (``dispatch(..., _approved=True)``), so a
    parked **direct** operator op is executed exactly once regardless of
    which surface granted it. The in-process agent-run resume path does
    not call this — it keeps the params in memory and re-dispatches from
    there (see :mod:`meho_backplane.agent.approval_wait`). Both paths re-enter
    a parked composite sub-op through the same
    :func:`composite_resume_binding` helper (#3421).

    *params* source:

    * REST ``/approve`` passes the caller-supplied params (already
      hash-verified against ``params_hash`` by :func:`approve_request`).
    * ``/decide`` and the MCP by-id approve hold only the request id, so
      they pass ``params=None`` and this helper falls back to the params
      stored on the row at park time (``request.params``, #1503). When
      the row holds no params MEHO can run with (``_stored_call``), there
      is nothing to re-dispatch: the run is refused like the release and
      target refusals (:func:`_refuse_resume`, error code
      ``approval_resume_params_unavailable``), so the request ends as a
      failed run with a reason, and the requester can re-run it with the
      params (#6064).

    Target re-hydration (G11.7-T1 #1401): the row persists only
    ``target_id``, not the full target. A write op whose handler reads
    ``target.host`` / ``target.name`` would mis-resolve (or crash) if
    re-dispatched with ``target=None``, so a concrete ``target_id`` is
    re-loaded by id (tenant-scoped, ``deleted_at IS NULL``). A target
    soft-deleted (or revoked) between request and approval resolves to
    ``None``; the re-dispatch then **fails closed** (structured ``denied``,
    never calls :func:`dispatch`) rather than executing the approved
    privileged write outside the original target scope. Tenant-wide ops
    (no original target) keep ``target_id IS NULL`` → ``None`` and
    dispatch normally.

    The re-dispatch does not ask for the approval again (``_approved=True``):
    without that, it would wait again (a human/service principal routes
    ``requires_approval`` to ``needs-approval`` per G11.7-T1; an agent
    gets ``needs-approval`` again), so the approved op would never execute.
    The rights of who asked are still checked as for a new call made now
    (:mod:`~meho_backplane.operations.approved_run_rights`).

    Exactly-one-resumer claim (#2293): every operator surface routes
    through here, and for a run-bound request the in-process agent waiter
    (:mod:`meho_backplane.agent.approval_wait`) also resumes off the
    approved row it reads. Both must win :func:`claim_resume`
    before dispatching, so the approved op executes exactly once: this
    path wins the claim right before :func:`dispatch` (after the
    fail-closed checks, so a refused resume never burns the claim) and, if
    a racing resumer already claimed it, returns a benign
    ``already_resumed`` result instead of re-dispatching the write. This
    is what lets ``/decide`` + MCP fall back to a server-side re-dispatch
    when the claim is free (covering waiter-gone -- timeout / restart /
    cancel) while the claim blocks the ``/approve`` / UI double-dispatch
    when the waiter is alive.

    Before the run, the target must still resolve and still have the
    connection settings it had when the request was made
    (:func:`refuse_resume_if_target_changed`), and a request with a preview
    hash must still build the same preview
    (:func:`refuse_resume_if_preview_changed`). Either change refuses the
    run, and the requester asks again.

    A secret reveal (``reveal_secret`` on the row) does not run here on an
    approve: the answer is :func:`result_waiting_for_requester`, and nothing
    is claimed. Only the fetch of the person who asked runs it
    (*requester_fetch*,
    :func:`~meho_backplane.operations.approved_reveal.fetch_approved_reveal`).
    """
    # #5024: a stopping pod starts no new run. The claim stays free, so
    # nothing is used up and nothing was sent.
    if is_draining():
        return shutting_down_result(request.op_id)
    if is_secret_reveal(request) and not requester_fetch:
        return result_waiting_for_requester(request)
    # The requester's reveal choice is on the request row, outside the
    # params and the hash (``create_pending_request``). It goes on the
    # params of the run only.
    effective_params, reveal_secret = _stored_call(request, params)
    if effective_params is None and request.execution_handle is None:
        return await _refuse_resume_params_unavailable(operator, request)

    release_refusal = await refuse_resume_if_release_changed(operator, request)
    if release_refusal is not None:
        return release_refusal

    blocked_refusal = await refuse_resume_if_asker_blocked(operator, request)
    if blocked_refusal is not None:
        return blocked_refusal

    resolved_target, target_refusal = await refuse_resume_if_target_changed(operator, request)
    if target_refusal is not None:
        return target_refusal

    # Exactly-one-resumer claim (#2293): win the atomic conditional UPDATE
    # right before executing. A refusal above (no params to run with,
    # release or target) takes the claim itself and stores the refusal as
    # the run result: such a request never runs later. A lost claim means another
    # resumer already executed the approved op (the in-process agent waiter
    # that read the same approved row, or a racing operator
    # surface), so no-op with a benign already_resumed result rather than
    # double-dispatching the approved write.
    #
    # #5024, checked again: the stop may have come during the lookups
    # above. The claim is the last step before the run, so this is the
    # last point at which nothing is used up yet.
    if is_draining():
        return shutting_down_result(request.op_id)
    if not await claim_resume(request.id):
        _log().info(
            "approval_resume_already_claimed",
            approval_request_id=str(request.id),
            op_id=request.op_id,
            connector_id=request.connector_id,
            operator_sub=operator.sub,
        )
        return result_already_resumed(request.op_id, request.id, 0.0)

    # Only the durable claim winner may touch the one-time ciphertext.  A
    # losing concurrent resume returns above without consuming its payload.
    resume_parent_override: dict[str, Any] | None = None
    if request.execution_handle is not None:
        from meho_backplane.db.engine import get_sessionmaker

        async with get_sessionmaker()() as handoff_session:
            handoff = await consume_handoff(
                handoff_session, request.execution_handle, request.id, request.tenant_id
            )
            await handoff_session.commit()
        effective_params = cast(dict[str, Any], handoff["params"])
        if not params_hash_matches(effective_params, request.params_hash):
            raise ApprovalHandoffError(
                "approval execution payload does not match the approved request"
            )
        if handoff.get("resume_parent") is not None:
            # Never assign decrypted composite inputs to the ORM row: an
            # attached request could autoflush them back into approval_request.
            resume_parent_override = cast(dict[str, Any], handoff["resume_parent"])

    preview_refusal = await refuse_resume_if_preview_changed(
        operator, request, resolved_target, cast(dict[str, Any], effective_params)
    )
    if preview_refusal is not None:
        return preview_refusal

    return await _dispatch_resume_with_bound_context(
        operator=operator,
        request=request,
        resolved_target=resolved_target,
        effective_params=cast(dict[str, Any], effective_params),
        resume_parent_override=resume_parent_override,
        reveal_secret=reveal_secret,
    )


def _stored_call(
    request: ApprovalRequest, params: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, bool]:
    """Return the params an approved run uses, and the requester's reveal choice.

    *params* (the caller's own copy of the call) win over the stored
    ``request.params``. A ``reveal_secret`` flag sent with them comes off
    before the hash and preview checks and does not count: the choice is the
    one of the original call, stored on the request row
    (``request.reveal_secret``). So the choice also survives for a
    credential write, whose params are not on the row and come from the
    caller on a re-run. The params are ``None`` when the row stores none and
    none were sent, or when the stored params can no longer run the call
    (:func:`_stored_params_can_run`).
    """
    if params is not None:
        params, _ = split_reveal_secret(params)
    effective: dict[str, Any] | None = params
    if effective is None and _stored_params_can_run(request):
        effective = cast(dict[str, Any] | None, request.params)
    return effective, bool(request.reveal_secret)


def _stored_params_can_run(request: ApprovalRequest) -> bool:
    """True when the params stored on *request* are still the ones the requester sent.

    Stored params without a marker (``[REDACTED:param_name]``) can run.
    With a marker, there are two cases (#6064):

    * MEHO wrote it (:func:`claim_resume` or :func:`end_run_input`): the
      real secret value is gone, so the params no longer match the
      request's ``params_hash``. They cannot run the call.
    * The requester sent it as a value, for example an object read from
      MEHO and passed back unchanged. The hash covers it, so the params
      still match ``params_hash``. They run as sent.
    """
    stored = cast(dict[str, Any] | None, request.params)
    if stored is None:
        return False
    if not holds_hidden_param_values(stored):
        return True
    return params_hash_matches(stored, request.params_hash)


async def refuse_resume_if_release_changed(
    operator: Operator,
    request: ApprovalRequest,
) -> OperationResult | None:
    """Refuse an approved run when MEHO was updated while it waited (#5030).

    A person approves one exact release of MEHO. Returns ``None`` when
    *request* was made under the running release (or the build has no
    release identity), so the caller runs it as before. Otherwise the
    operation never runs and the vendor gets no call:

    * The one-resumer claim (:func:`claim_resume`) is taken first, so the
      request can never run later, on this release or another. A lost
      claim means another resumer already took the request; the usual
      ``already_resumed`` result is returned.
    * The stored run input (the credential-write custody row) is deleted,
      the same as on every other way a request ends.
    * One audit row records the refusal (``result_status``
      ``release_changed_while_waiting``, both releases). A failed audit
      write is logged; the run is refused either way.
    * The refusal is stored as the request's run result, so every read
      surface shows the run as failed with that error code.

    Shared by both resume paths: the operator surfaces
    (:func:`resume_dispatch_after_approval`) and the in-process agent
    waiter (:mod:`meho_backplane.agent.approval_wait`).
    """
    if not approval_release_changed(request):
        return None
    running = cast(str, running_release_id())
    _log().warning(
        "approval_resume_release_changed",
        approval_request_id=str(request.id),
        op_id=request.op_id,
        request_release=request.release_id,
        running_release=running,
        operator_sub=operator.sub,
    )
    refusal = result_release_changed_while_waiting(
        request.op_id,
        approval_request_id=request.id,
        request_release=request.release_id,
        running_release=running,
    )
    return await _refuse_resume(
        operator,
        request,
        refusal,
        audit_payload={"request_release": request.release_id, "running_release": running},
    )


async def refuse_resume_if_asker_blocked(
    operator: Operator,
    request: ApprovalRequest,
) -> OperationResult | None:
    """Refuse an approved run when the login that asked is blocked.

    Returns ``None`` when the asker (``request.principal_sub``) is not
    blocked, so the caller runs the request. Otherwise the operation never
    runs and the vendor gets no call. The refusal is recorded like the
    release refusal (:func:`refuse_resume_if_release_changed`): the claim
    is taken, so the request never runs later, the stored run input is
    deleted, one audit row records it, and the refusal is the stored run
    result. A block-list read that fails refuses the run the same way.

    Shared by both resume paths: the operator surfaces
    (:func:`resume_dispatch_after_approval`) and the in-process agent
    waiter (:mod:`meho_backplane.agent.approval_wait`).
    """
    refusal = await refuse_if_principal_blocked(
        request.op_id,
        request.principal_sub,
        request.tenant_id,
        # The multi-step operation sent a step before it waited.
        earlier_sent=(request.resume_parent or {}).get("request_sent") is True,
    )
    if refusal is None:
        return None
    return await _refuse_resume(
        operator,
        request,
        refusal,
        audit_payload={"asker_sub": request.principal_sub},
    )


async def refuse_resume_if_target_changed(
    operator: Operator,
    request: ApprovalRequest,
) -> tuple[Any, OperationResult | None]:
    """Load the saved target for an approved run, or refuse the run.

    Returns ``(resolved_target, None)`` when the target the request pinned
    still resolves and still has the connection settings it had when the
    request was made (``None`` for a tenant-wide request). Else the
    operation never runs and the vendor gets no call: the refusal of
    :func:`rehydrate_resume_target` (``target_changed_since_approval`` or
    ``target_gone_since_approval``) is recorded the same way as the release
    refusal (:func:`_refuse_resume`): the one-resumer claim is taken, the
    stored run input is deleted, one audit row records the refusal, and the
    refusal is stored as the request's run result. A lost claim returns the
    usual ``already_resumed`` result.

    Shared by both resume paths: the operator surfaces
    (:func:`resume_dispatch_after_approval`) and the in-process agent
    waiter (:mod:`meho_backplane.agent.approval_wait`). The re-run after a
    failed run (:mod:`meho_backplane.operations.approval_rerun`) calls
    :func:`rehydrate_resume_target` directly and records its own refusal.
    """
    resolved_target, denied = await rehydrate_resume_target(operator, request)
    if denied is None:
        return resolved_target, None
    refusal = await _refuse_resume(
        operator,
        request,
        denied,
        audit_payload={"target_id": str(request.target_id)},
    )
    return None, refusal


async def _refuse_resume(
    operator: Operator,
    request: ApprovalRequest,
    refusal: OperationResult,
    *,
    audit_payload: dict[str, Any],
) -> OperationResult:
    """Record that an approved run was refused before it ran, and return the refusal.

    The same four steps for every refusal that happens before the
    one-resumer claim:

    * The claim (:func:`claim_resume`) is taken, so the request can never
      run later. A lost claim means another resumer already took the
      request; the usual ``already_resumed`` result is returned and nothing
      is recorded here. The claim itself hides the secret-named values of
      the stored params.
    * The stored run input (the credential-write custody row) is deleted,
      the same as on every other way a request ends.
    * One audit row records the refusal (``path`` ``approval.resume``,
      ``result_status`` = the refusal's error code, plus *audit_payload*).
      A failed audit write is logged; the run is refused either way.
    * The refusal is stored as the request's run result, so every read
      surface shows the run as refused with that error code.
    """
    if not await claim_resume(request.id):
        return result_already_resumed(request.op_id, request.id, 0.0)
    await _discard_refused_handoff(request)
    error_code = str((refusal.extras or {}).get("error_code"))
    audit_id = await _audit_resume_refusal(
        operator, request, extra_payload={"result_status": error_code, **audit_payload}
    )
    if audit_id is not None:
        refusal = refusal.model_copy(update={"audit_id": audit_id})
    await persist_resume_result(request.id, refusal, parked_op_id=request.op_id)
    return refusal


async def refuse_resume_if_preview_changed(
    operator: Operator,
    request: ApprovalRequest,
    target: Any,
    params: dict[str, Any],
) -> OperationResult | None:
    """Refuse an approved run whose preview, or whose object, is not the approved one.

    Two checks, for a request that stored a preview hash. The preview built
    now for *target* and *params* must have the stored hash. Then the
    object the approval card named must still be the object the live
    system has under the call's name
    (:func:`~meho_backplane.operations.approval_object_check.object_changed_since_request`).
    A third check runs for every change of rights, with or without a
    preview hash: the login and the group it is for must still have the
    ids the approver saw
    (:func:`~meho_backplane.operations.grant_approval.grant_changed_since_request`).
    A fourth check runs for an operation whose card shows the object as it is
    now: that state must still be the one the approver saw
    (:func:`~meho_backplane.operations.approval_state_pin.state_changed_since_request`).
    Returns ``None`` when all hold. Else the operation does not run: the
    refusal is stored as the request's run result, so every read surface
    shows the run as failed with the ``preview_changed_since_approval`` or
    ``object_changed_since_approval`` error code. The caller has already
    won the one-resumer claim, so the request cannot run later.

    Shared by both resume paths: the operator surfaces
    (:func:`resume_dispatch_after_approval`) and the in-process agent
    waiter (:mod:`meho_backplane.agent.approval_wait`).
    """
    refusal: OperationResult
    if await preview_changed_since_request(operator, request, target, params):
        refusal = result_preview_changed_since_approval(
            request.op_id, approval_request_id=request.id
        )
        _log().warning(
            "approval_resume_preview_changed",
            approval_request_id=str(request.id),
            op_id=request.op_id,
            connector_id=request.connector_id,
            operator_sub=operator.sub,
        )
    else:
        reason = await object_changed_since_request(operator, request, target, params)
        if reason is None:
            reason = await grant_changed_since_request(operator, request, target, params)
        if reason is None:
            reason = await state_changed_since_request(operator, request, target, params)
        if reason is None:
            return None
        refusal = result_object_changed_since_approval(
            request.op_id, approval_request_id=request.id, reason=reason
        )
    await persist_resume_result(request.id, refusal, parked_op_id=request.op_id)
    return refusal


async def _discard_refused_handoff(request: ApprovalRequest) -> None:
    """Delete the stored run input of a refused request (#5030)."""
    from meho_backplane.db.engine import get_sessionmaker

    async with get_sessionmaker()() as session:
        await discard_handoff(session, request.id, request.tenant_id)
        await session.commit()


async def _audit_resume_refusal(
    operator: Operator,
    request: ApprovalRequest,
    *,
    extra_payload: dict[str, Any],
) -> uuid.UUID | None:
    """Write the audit row for a refused run; ``None`` if the write failed."""
    from meho_backplane.db.engine import get_sessionmaker

    audit_id = uuid.uuid4()
    try:
        async with get_sessionmaker()() as session:
            await _write_audit_row(
                session,
                audit_id=audit_id,
                event_class=AuditEventClass.POLICY_DECISION,
                operator=operator,
                request=request,
                path="approval.resume",
                status_code=_RESUME_REFUSED_STATUS_CODE,
                duration_ms=0.0,
                extra_payload=extra_payload,
            )
            await session.commit()
    except Exception:
        _log().exception(
            "approval_resume_refusal_audit_failed",
            approval_request_id=str(request.id),
            result_status=extra_payload.get("result_status"),
        )
        return None
    return audit_id


def _dump_result_envelope(result: Any) -> dict[str, Any] | None:
    """Project a re-dispatch result into a JSON dict for durable storage (#3209).

    The operator resume path returns an
    :class:`~meho_backplane.connectors.schemas.OperationResult` (a Pydantic
    model). ``model_dump(mode="json")`` yields the reduced / handle-shaped
    envelope — the same one the approver sees inline, so a set-shaped response
    is already a JSONFlux ``handle`` (v0.1-spec §4), never a raw body. The
    in-process agent resume already holds that dump as a plain dict (#3486);
    it is stored as is. Returns ``None`` for anything else (defence-in-depth;
    an unknown result shape is simply not captured rather than raising).
    """
    if isinstance(result, dict):
        return result
    dump = getattr(result, "model_dump", None)
    if dump is None:
        return None
    projection = dump(mode="json")
    return projection if isinstance(projection, dict) else None


async def persist_resume_result(request_id: uuid.UUID, result: Any, *, parked_op_id: str) -> None:
    """Store the approved re-dispatch's reduced result envelope on the row (#3209).

    Called on the exactly-one-resumer winning path only, so it runs at most
    once per approved request: by :func:`_dispatch_resume_with_bound_context`
    for the operator surfaces, and by the in-process agent waiter
    (:mod:`meho_backplane.agent.approval_wait`) after its re-dispatch (#3486),
    so a request the agent resumed also moves off ``started`` on the list and
    show views. The stored value is the reduced / handle-shaped
    ``OperationResult`` envelope; the originating consumer reads it back —
    principal-scoped — via ``GET /api/v1/approvals/{id}/result`` to resume a
    non-idempotent parked op (VCF ``bringup.start``) by poll.

    Before the write, the next step a failed multi-step operation names is
    stamped into ``extras["approval_guidance"]`` (#5377,
    :func:`~meho_backplane.operations.approval_outcome.with_approval_guidance`).
    The run summary on the list and show views reads only that stamp, so a
    row this code did not write shows no guidance. *parked_op_id* is the op
    id of the request; a secret-class op gets no stamp.

    Fail-soft: the dispatch's own synchronous audit row already committed
    inside :func:`~meho_backplane.operations.dispatcher.dispatch` (the
    v0.1-spec §6 record of what executed), so this convenience-durability write
    must never break the approve path. A failure is logged and swallowed; the
    approver still receives the result inline and the consumer can re-poll. Runs
    in its own committed transaction — the decision and the dispatch already
    committed in separate sessions.
    """
    dumped = _dump_result_envelope(result)
    if dumped is None:
        return
    dumped = with_approval_guidance(dumped, parked_op_id=parked_op_id)
    from meho_backplane.db.engine import get_sessionmaker

    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            await session.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == request_id)
                .values(resume_result=dumped)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
    except Exception:
        # Fail-soft convenience write (see docstring): the dispatch's own
        # synchronous audit row already committed, so a capture failure must
        # not break the approve path.
        _log().warning(
            "approval_resume_result_persist_failed",
            approval_request_id=str(request_id),
            exc_info=True,
        )


async def _refuse_resume_params_unavailable(
    operator: Operator, request: ApprovalRequest
) -> OperationResult:
    """Refuse an approved run that has no params MEHO can run with (#6064).

    The surface sent no params, and :func:`_stored_call` found none on the
    row. The run is recorded like the other refusals (:func:`_refuse_resume`):
    the claim is taken, one audit row is written, and the refusal is stored
    as the run result. So the request ends as a failed run with a reason on
    every surface, instead of staying approved and not run, and the
    requester can re-run it with the params.
    """
    _log().warning(
        "approval_resume_params_unavailable",
        approval_request_id=str(request.id),
        op_id=request.op_id,
        connector_id=request.connector_id,
        operator_sub=operator.sub,
    )
    refusal = result_resume_params_unavailable(request.op_id, approval_request_id=request.id)
    return await _refuse_resume(operator, request, refusal, audit_payload={})


def requester_of(request: ApprovalRequest) -> ResumeRequester:
    """The requester saved on *request*, with its ``client`` block and record.

    The record is the card's requester record, else the record a person's
    or a service login's request keeps under
    :data:`~meho_backplane.operations._preview.REQUESTER_RECORD_KEY`.
    """
    from meho_backplane.operations._preview import REQUESTER_RECORD_KEY, stored_requester_record

    effect = request.proposed_effect or {}
    client = effect.get(CLIENT_PAYLOAD_KEY)
    subject = stored_requester_record(effect)
    if not isinstance(subject, dict):
        subject = effect.get(REQUESTER_RECORD_KEY)
    return ResumeRequester(
        request.principal_sub,
        request.principal_act,
        request.principal_name,
        request.principal_kind,
        client if isinstance(client, dict) else None,
        subject if isinstance(subject, dict) else None,
        request.people_behind,
    )


def _with_requester_record(
    proposed_effect: dict[str, Any], operator: Operator, requester: ResumeRequester | None
) -> dict[str, Any]:
    """Put the requester's record on the card.

    An approvals surface runs the approved call as the person who approved
    it. It rebuilds the agent from this record to check the agent's grants
    for the steps again
    (:func:`~meho_backplane.operations.approved_step_check.approved_step_refusal`).
    A re-park on behalf of an earlier requester keeps that requester's
    record. A record the operation's own preflight stored stays.

    The record is kept under an internal key
    (:data:`~meho_backplane.operations._preview.AGENT_REQUESTER_RECORD_KEY`).
    The REST and MCP approval tools leave it out.
    While ``AGENT_GRANT_REQUIRED`` is not ``off``, the card also shows it
    under ``permission_preflight["requester"]``.

    A person's or a service login's own request keeps its record only under
    :data:`~meho_backplane.operations._preview.REQUESTER_RECORD_KEY`, which
    only a re-run of the approved call reads (:func:`requester_of`). So a call
    that waits during the re-run is checked as the caller who started the run.
    """
    from meho_backplane.operations._preview import (
        AGENT_REQUESTER_RECORD_KEY,
        REQUESTER_RECORD_KEY,
    )
    from meho_backplane.operations.approved_step_check import requester_record
    from meho_backplane.operations.secondary_target_policy import requester_subject
    from meho_backplane.settings import get_settings

    record = requester_record(operator) if requester is None else requester.subject
    if record is None:
        if requester is None:
            return {REQUESTER_RECORD_KEY: requester_subject(operator), **proposed_effect}
        return proposed_effect
    effect = {AGENT_REQUESTER_RECORD_KEY: record, **proposed_effect}
    if get_settings().agent_grant_required == "off":
        return effect
    preflight = effect.get(_PERMISSION_PREFLIGHT_KEY)
    preflight = dict(preflight) if isinstance(preflight, dict) else {}
    preflight.setdefault(_REQUESTER_KEY, record)
    return {**effect, _PERMISSION_PREFLIGHT_KEY: preflight}


@contextmanager
def composite_resume_binding(
    *,
    subop_op_id: str,
    subop_params_hash: str,
    resume_parent: dict[str, Any] | None,
    requester: ResumeRequester | None = None,
    proposed_effect: dict[str, Any] | None = None,
) -> Iterator[tuple[str, dict[str, Any]] | None]:
    """Set up the re-entry of a parent composite for an approved sub-op park.

    The one composite re-entry path both resume surfaces share (#3351, #3421):
    the operator surface (:func:`_dispatch_resume_with_bound_context`) and the
    in-process agent waiter
    (:func:`~meho_backplane.agent.approval_wait.resume_or_surface_awaiting_approval`).
    Sharing it keeps the two surfaces from drifting apart.

    * ``resume_parent`` set (a composite sub-op park) — yields the parent
      composite's ``(op_id, params)`` to re-dispatch ``_approved=True``. For the
      block it sets
      :data:`~meho_backplane.operations.composite.composite_resume_var` to the
      approved sub-op's ``(op_id, params_hash)``, so that one sub-op clears its
      gate instead of parking again. It also sets
      :data:`~meho_backplane.operations.composite.composite_resume_scope_var`,
      which is never consumed, so the steps of the re-entry know they run
      after an approval. It hands the steps the earlier run stored
      (``resume_parent["steps"]``) to the composite, so a step that already
      ran is not sent again
      (:mod:`~meho_backplane.operations.composite_steps`).
    * ``resume_parent`` is ``None`` (a direct park) — yields ``None`` and sets
      no re-entry vars; the caller keeps its generic re-dispatch.

    When the park saved a ``state`` (#4217), the block also binds
    :data:`~meho_backplane.operations.composite.composite_resume_state_var`
    with it and the park's ``after_main_effect`` mark, so the re-entered
    composite can continue at the step that waited instead of running from
    the start.

    For every park, direct or sub-op, the block binds
    :data:`resume_requester_var` with *requester*, the requester of the
    request being re-run. So a step of the step gate or a child call
    (``dispatch_child``) that waits during the run is saved for the caller
    who started the run, on both resume paths, not for the reviewer
    who runs it.

    For every park, direct or sub-op, the block also binds
    :data:`~meho_backplane.operations._preview.approved_preview_var` with
    the approved request's op id and the computed preview its park stored
    (the ``preview`` body of *proposed_effect*, or ``{}``). A handler reads
    it with :func:`~meho_backplane.operations._preview.approved_preview`,
    so a name it resolved at park time is not looked up again after the
    approval; the re-run acts on the object the approver saw (#5202).
    """
    from meho_backplane.operations._preview import approved_preview_var

    preview_token = approved_preview_var.set((subop_op_id, _approved_preview_body(proposed_effect)))
    requester_token = resume_requester_var.set(requester)
    try:
        if resume_parent is None:
            yield None
        else:
            with _composite_parent_binding(
                subop_op_id=subop_op_id,
                subop_params_hash=subop_params_hash,
                resume_parent=resume_parent,
            ) as parent_call:
                yield parent_call
    finally:
        resume_requester_var.reset(requester_token)
        approved_preview_var.reset(preview_token)


def _approved_preview_body(proposed_effect: dict[str, Any] | None) -> dict[str, Any]:
    """The computed preview a park stored, or ``{}`` when it stored none."""
    body = (proposed_effect or {}).get("preview")
    return body if isinstance(body, dict) else {}


@contextmanager
def _composite_parent_binding(
    *,
    subop_op_id: str,
    subop_params_hash: str,
    resume_parent: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Bind the composite re-entry vars of :func:`composite_resume_binding`."""
    from meho_backplane.operations.composite import (
        CompositeResumeState,
        composite_resume_scope_var,
        composite_resume_state_var,
        composite_resume_var,
    )
    from meho_backplane.operations.composite_steps import composite_resume_steps

    parent_op_id = str(resume_parent["op_id"])
    saved_state = resume_parent.get("state")
    approved_subop = (subop_op_id, subop_params_hash)
    resume_token = composite_resume_var.set(approved_subop)
    scope_token = composite_resume_scope_var.set(approved_subop)
    state_token = composite_resume_state_var.set(
        CompositeResumeState(
            op_id=parent_op_id,
            state=cast(dict[str, Any], saved_state),
            after_main_effect=bool(resume_parent.get("after_main_effect")),
        )
        if isinstance(saved_state, dict)
        else None
    )
    try:
        with composite_resume_steps(
            parent_op_id,
            resume_parent.get("steps"),
            earlier_sent=resume_parent.get("request_sent") is True,
        ):
            yield parent_op_id, cast(dict[str, Any], resume_parent["params"])
    finally:
        composite_resume_state_var.reset(state_token)
        composite_resume_scope_var.reset(scope_token)
        composite_resume_var.reset(resume_token)


def _run_audit_context(
    request: ApprovalRequest, rerun_attempt: int | None = None
) -> dict[str, Any]:
    """The ``audit_*`` log context to bind while an approved op runs (#3864).

    The REST approve routes bind ``audit_op_id="approval.decide"`` (and its
    siblings) for their own request row. The dispatcher copies every bound
    ``audit_*`` key into the run's audit payload, so those keys relabelled the
    run as ``approval.decide``. A search for the real op then found no run at
    all. Every route key is set to ``None`` here (the payload builder skips
    ``None``), and only ``approval_request_id`` is kept, so the run row names
    its own op and the request it ran for. ``bound_contextvars`` puts the
    route keys back when the run ends.

    A re-run of a failed request (#5417) passes *rerun_attempt*, so the
    run row also says which attempt it is.
    """
    context: dict[str, Any] = {
        key: None for key in structlog.contextvars.get_contextvars() if key.startswith("audit_")
    }
    context["audit_approval_request_id"] = str(request.id)
    if rerun_attempt is not None:
        context["audit_approval_rerun_attempt"] = rerun_attempt
    return context


async def _dispatch_resume_with_bound_context(
    *,
    operator: Operator,
    request: ApprovalRequest,
    resolved_target: Any,
    effective_params: dict[str, Any],
    resume_parent_override: dict[str, Any] | None = None,
    rerun_attempt: int | None = None,
    reveal_secret: bool = False,
) -> Any:
    """Re-bind the parked row's stored context, then dispatch ``_approved=True``.

    The decision surfaces run on a fresh task whose vars are unset, so the
    executed op's audit row would otherwise lose everything the parked
    request carried. Three row-sourced, token-reset bindings: ``work_ref``
    (#1659, the authorising ticket), ``agent_session_id`` (#2086, anchors
    the row in the requester's replay tree), and ``parent_audit_id`` (#2086,
    back-links it to the parking row). The in-process agent-run resume keeps
    its own bound vars and does not call this helper; pre-0053 NULLs bind
    ``None``, the vars' defaults. Only reached after the exactly-one-resumer
    claim is won (#2293), so it dispatches exactly once.

    Composite sub-op resume (#3351): when the row carries ``resume_parent``,
    the parked op is a composite's governed sub-op whose stored ``op_id`` +
    gate params are not a dispatchable descriptor call. Re-dispatch the
    **parent composite** verbatim (``_approved=True`` skips its top-level
    gate) with :data:`~meho_backplane.operations.composite.composite_resume_var`
    set to the approved sub-op's ``(op_id, params_hash)`` so that one sub-op
    clears its gate and the whole governed step reproduces, instead of the
    old generic re-dispatch of the un-executable sub-op key. A direct-op park
    (``resume_parent IS NULL``) keeps the generic re-dispatch of ``op_id``.
    The var handling lives in :func:`composite_resume_binding`, which the
    in-process agent waiter uses too (#3421).

    The resume re-enters the composite under the approving reviewer, who
    auto-executes every governed sub-op (all ``dangerous`` +
    ``requires_approval=False``), so a shipped composite completes in this one
    pass. ``composite_resume_scope_var`` is bound too — unconsumed for the whole
    re-entry. A later governed sub-op that needs its own approval waits for
    it again; the steps this run already did are stored with that request
    (:mod:`~meho_backplane.operations.composite_steps`), so its resume does
    not send them again. That new request is saved for the requester of
    *request*, not for the reviewer who runs this re-run.

    #3898: the requester (from the row) is bound for the run
    (:func:`~meho_backplane.operations.secondary_target_policy.bind_approved_requester`),
    so an op that writes to a second, caller-named target checks the
    requester's policy there too, not only the reviewer's.

    A hosted agent's connector list, stored on the row, is bound again
    (:func:`~meho_backplane.operations._target_connector_limit.restored_connector_limit`),
    so the run and each step of a multi-step operation stay on targets of
    the agent's connectors, also after the agent stopped waiting.

    *reveal_secret* is the requester's ``reveal_secret: true``, stored on
    the request row. It goes on the params of the run, so the answer holds
    the values the requester asked for; the dispatcher stamps the choice on
    the audit row of the run.
    """
    from meho_backplane.operations.approved_run_rights import bind_approved_run
    from meho_backplane.operations.dispatcher import dispatch
    from meho_backplane.operations.secondary_target_policy import bind_approved_requester

    resume_parent = (
        resume_parent_override if resume_parent_override is not None else request.resume_parent
    )
    work_ref_token = work_ref_var.set(request.work_ref)
    # A CLI login session goes back on client_session_id_var, so the run's
    # audit rows carry it but nothing treats the run as an agent session.
    # The approver's own CLI session never labels the requester's run.
    from_client = bool(request.session_from_client)
    session_token = agent_session_id_var.set(None if from_client else request.agent_session_id)
    client_token = client_session_id_var.set(request.agent_session_id if from_client else None)
    parent_token = parent_audit_id_var.set(request.request_audit_id)
    # #3548: the approver runs the op, but the parker asked for the data and is
    # the only one GET /approvals/{id}/result serves, so the parker owns any
    # result handle this run spills.
    owner_token = handle_owner_sub_var.set(request.principal_sub)
    try:
        with (
            structlog.contextvars.bound_contextvars(**_run_audit_context(request, rerun_attempt)),
            bind_approved_requester(request),
            bind_approved_run(request),
            restored_connector_limit(request.allowed_connectors),
            # Each step of the run also checks the person who asked.
            checking_logins_for_steps(request.principal_sub, request.tenant_id),
            composite_resume_binding(
                subop_op_id=request.op_id,
                subop_params_hash=request.params_hash,
                resume_parent=resume_parent,
                requester=requester_of(request),
                proposed_effect=request.proposed_effect,
            ) as parent_call,
        ):
            dispatch_op_id, dispatch_params = (
                parent_call if parent_call is not None else (request.op_id, effective_params)
            )
            if reveal_secret:
                dispatch_params = with_reveal_secret(dispatch_params)
            result = await dispatch(
                operator=operator,
                connector_id=request.connector_id,
                op_id=dispatch_op_id,
                target=resolved_target,
                params=dispatch_params,
                _approved=True,
            )
    finally:
        handle_owner_sub_var.reset(owner_token)
        parent_audit_id_var.reset(parent_token)
        client_session_id_var.reset(client_token)
        agent_session_id_var.reset(session_token)
        work_ref_var.reset(work_ref_token)
    # Persist the reduced result envelope so the originating consumer can read it
    # back (principal-scoped) and resume a non-idempotent parked op by poll
    # instead of a second submit (#3209). Reached only by the exactly-one-resumer
    # winner, so the capture runs at most once; fail-soft, never breaks the
    # approve path.
    # A reveal keeps its values only in the answer to the person who asked.
    stored = without_secret_values(result) if reveal_secret else result
    await persist_resume_result(request.id, stored, parked_op_id=request.op_id)
    return result


async def rehydrate_resume_target(
    operator: Operator,
    request: ApprovalRequest,
) -> tuple[Any, Any]:
    """Re-load the stored target by id for a resume re-dispatch (G11.7-T1 #1401).

    Returns ``(resolved_target, None)`` on success — ``resolved_target``
    is ``None`` for a tenant-wide op (no ``target_id`` pinned) and the
    live :class:`Target` otherwise. Returns ``(None, denied_result)`` when
    a concrete ``target_id`` was pinned at request time but no live target
    resolves it now (soft-deleted / revoked between request and approval,
    or cross-tenant): the caller must **fail closed** and not dispatch,
    since dispatching with ``target=None`` would let a typed handler that
    derives its connection from ``connector_id`` / ``params`` execute the
    approved privileged write outside the original target scope.

    Shared, through :func:`refuse_resume_if_target_changed`, by the operator
    surface (:func:`resume_dispatch_after_approval`) and the in-process
    agent waiter (:mod:`meho_backplane.agent.approval_wait`, #3421), and
    called directly by the re-run after a failed run
    (:mod:`meho_backplane.operations.approval_rerun`). Loading by the saved
    ``target_id`` instead of the name the caller used at park time means a
    target renamed, or deleted and re-created under the same name, while the
    request waited can never receive the approved write.

    A target whose connection settings changed since the request was made
    (``request.target_hash``, see
    :mod:`meho_backplane.operations.approval_resume_checks`) is refused the
    same way, with the ``target_changed_since_approval`` error code: the
    approval covers the target as the person saw it, not a new host or new
    credentials. A target that no longer resolves gives the
    ``target_gone_since_approval`` error code. This function only checks;
    it records nothing.
    """
    if request.target_id is None:
        return None, None

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.targets.resolver import resolve_target_by_id

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        resolved_target = await resolve_target_by_id(session, operator.tenant_id, request.target_id)
    if resolved_target is not None and target_changed_since_request(request, resolved_target):
        _log().warning(
            "approval_resume_target_changed",
            approval_request_id=str(request.id),
            op_id=request.op_id,
            connector_id=request.connector_id,
            target_id=str(request.target_id),
            operator_sub=operator.sub,
        )
        return None, result_target_changed_since_approval(
            request.op_id, approval_request_id=request.id, target_id=request.target_id
        )
    if resolved_target is not None:
        return resolved_target, None

    _log().warning(
        "approval_resume_target_unresolvable",
        approval_request_id=str(request.id),
        op_id=request.op_id,
        connector_id=request.connector_id,
        target_id=str(request.target_id),
        operator_sub=operator.sub,
    )
    return None, result_target_gone_since_approval(
        request.op_id, approval_request_id=request.id, target_id=request.target_id
    )


def _deadline_passed_clause(cutoff: datetime, default_ttl: timedelta | None) -> Any:
    """Build the "expiry deadline has passed" WHERE clause for the sweep (#2322).

    A concrete ``expires_at`` compares directly. When *default_ttl* is
    supplied, legacy null-expiry rows (parked before #2322) are coalesced
    against ``created_at + default_ttl`` — expressed as
    ``created_at <= cutoff - default_ttl`` rather than SQL
    column+interval arithmetic so the predicate binds two plain Python
    datetimes and stays dialect-portable (PG ``timestamptz`` + the SQLite
    test path both compare cleanly), the same bound-parameter discipline
    the grant-expiry sweeper uses. With no *default_ttl* only rows with a
    concrete deadline are considered (the historical contract).
    """
    concrete = and_(
        ApprovalRequest.expires_at.is_not(None),
        ApprovalRequest.expires_at <= cutoff,
    )
    if default_ttl is None:
        return concrete
    legacy_cutoff = cutoff - default_ttl
    return or_(
        concrete,
        and_(
            ApprovalRequest.expires_at.is_(None),
            ApprovalRequest.created_at <= legacy_cutoff,
        ),
    )


def _as_utc(value: datetime) -> datetime:
    """Coerce a possibly-naive datetime to timezone-aware UTC.

    Postgres ``timestamptz`` columns round-trip as aware datetimes, but the
    SQLite unit-test driver returns naive ones; comparing a naive row value
    against the aware :func:`_now` cutoff raises ``TypeError``. A naive
    value is assumed to be UTC (the only zone the queue ever stores), the
    same normalisation the console / checks surfaces apply on read.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _deadline_has_passed(
    request: ApprovalRequest,
    cutoff: datetime,
    default_ttl: timedelta | None,
) -> bool:
    """Row-level mirror of :func:`_deadline_passed_clause` (security F12 / #274).

    The decision-time deadline gate the approve path evaluates in Python
    on the single locked row, using the same coalesce the background sweep
    applies in SQL so the two agree on when a row is overdue: a concrete
    ``expires_at`` compares directly; a legacy null-``expires_at`` row
    (parked before #2322) is treated as if its deadline were
    ``created_at + default_ttl`` when a *default_ttl* is supplied, and is
    never overdue otherwise (the historical contract). Keeping this in
    lock-step with :func:`_deadline_passed_clause` means an overdue request
    the approve path refuses is exactly one the sweep would expire.
    """
    cutoff = _as_utc(cutoff)
    if request.expires_at is not None:
        return _as_utc(request.expires_at) <= cutoff
    if default_ttl is None:
        return False
    return _as_utc(request.created_at) <= cutoff - default_ttl


async def expire_stale_requests(
    session: AsyncSession,
    *,
    operator: Operator,
    now: datetime | None = None,
    default_ttl: timedelta | None = None,
) -> list[ApprovalRequest]:
    """Transition all ``pending`` rows past their ``expires_at`` to ``expired``.

    Writes one "decision" audit row per expired request inside the same
    session (caller commits). Intended to be called from a background
    task / CLI sweep on a periodic interval.

    Rows with ``expires_at IS NOT NULL AND expires_at <= now`` and
    ``status = 'pending'`` are always affected. When *default_ttl* is
    supplied (the periodic sweeper passes ``APPROVAL_DEFAULT_TTL``),
    legacy rows parked before #2322 with ``expires_at IS NULL`` are also
    aged out via a sweep-time coalesce: a null-expiry row is treated as
    if its deadline were ``created_at + default_ttl`` and, when that
    coalesced deadline has passed, the row is expired and its
    ``expires_at`` is backfilled to that value so the decision row and
    the persisted deadline agree. No schema change is needed — the
    coalesce lives entirely in this predicate.

    Args:
        session: Open :class:`AsyncSession``; flushed, not committed.
        operator: The identity to record on the audit rows (typically a
            system service-account operator). Must hold at least the
            ``operator`` role.
        now: Override for "current time" (used in tests). Defaults to
            :func:`datetime.now(UTC)`.
        default_ttl: When set, null-``expires_at`` legacy rows are
            coalesced against ``created_at + default_ttl`` (#2322). When
            ``None`` (the historical contract), only rows with a concrete
            ``expires_at`` are considered.

    Returns:
        List of the expired :class:`ApprovalRequest` rows (may be empty).
        Each row carries the transient ``_audit_id`` attribute (the
        decision row's primary key) so the caller can record a
        fail-open ``approval.expired`` step event **after commit**
        via :func:`record_approval_step_event`. The attribute is set on
        every returned row — see :func:`approve_request` /
        :func:`reject_request` for the same pattern.
    """
    # Enforce the operator-role floor the docstring promises, mirroring
    # approve_request / reject_request (CodeRabbit #1086).
    _check_reviewer_role(operator)
    cutoff = now or _now()

    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.status == ApprovalRequestStatus.PENDING.value)
        .where(_deadline_passed_clause(cutoff, default_ttl))
        .where(ApprovalRequest.tenant_id == operator.tenant_id)
        # Lock each swept row FOR UPDATE and skip any a concurrent approve /
        # reject already holds (security F12 / #274): the sweep never blocks
        # on an in-flight decision, and it never competes with one for the
        # same row — the decision path wins that row and the sweep expires
        # only the rows no operator is deciding, so every transition has
        # exactly one winner. ``SKIP LOCKED`` is a no-op on SQLite; the
        # guarantee is proven against Postgres in the integration suite.
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())

    for request in rows:
        await _expire_request(
            session, request, operator=operator, cutoff=cutoff, default_ttl=default_ttl
        )

    return rows


async def expire_older_release_requests(
    session: AsyncSession,
    *,
    operator: Operator,
    release_id: str,
    now: datetime | None = None,
) -> list[ApprovalRequest]:
    """Expire every ``pending`` request made under another release (#5030).

    A person approves one exact release of MEHO. Run once at start-up with
    the running *release_id*: every ``pending`` request of the operator's
    tenant whose ``release_id`` differs, or is NULL (saved before MEHO
    recorded the release), moves to ``expired`` through the same path as
    :func:`expire_stale_requests`, with one decision audit row each. The
    audit row carries ``reason="release_changed"`` and both releases. A
    request already past its deadline expires with no extra reason, as the
    TTL sweep would expire it.

    Same contract as :func:`expire_stale_requests`: the caller commits,
    rows a concurrent decision holds are skipped, and each returned row
    carries ``_audit_id`` for the after-commit step event.
    """
    _check_reviewer_role(operator)
    cutoff = now or _now()
    default_ttl = _resolve_default_ttl()
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.status == ApprovalRequestStatus.PENDING.value)
        .where(ApprovalRequest.tenant_id == operator.tenant_id)
        .where(
            or_(
                ApprovalRequest.release_id.is_(None),
                ApprovalRequest.release_id != release_id,
            )
        )
        .with_for_update(skip_locked=True)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for request in rows:
        extra_payload: dict[str, Any] | None = None
        if not _deadline_has_passed(request, cutoff, default_ttl):
            extra_payload = {
                "reason": ApprovalExpiredReason.RELEASE_CHANGED.value,
                "request_release": request.release_id,
                "running_release": release_id,
            }
        await _expire_request(
            session,
            request,
            operator=operator,
            cutoff=cutoff,
            default_ttl=default_ttl,
            extra_payload=extra_payload,
        )
    return rows


async def _expire_request(
    session: AsyncSession,
    request: ApprovalRequest,
    *,
    operator: Operator,
    cutoff: datetime,
    default_ttl: timedelta | None,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    """Move one locked ``pending`` row to ``expired`` + audit it.

    Shared by :func:`expire_stale_requests`, the idempotent retry path
    of :func:`create_pending_request` (#3486) and
    :func:`expire_older_release_requests` (#5030), so all write the same
    decision audit row. *extra_payload* adds keys to that row (the
    release-change reason). The caller holds the row lock and owns the
    commit.
    """
    # Backfill a legacy null deadline to the coalesced value so the
    # decision audit row and the persisted row agree on when it expired.
    if request.expires_at is None and default_ttl is not None:
        request.expires_at = request.created_at + default_ttl
    request.status = ApprovalRequestStatus.EXPIRED.value
    request.decided_at = cutoff
    await end_run_input(session, request)
    await session.flush()

    expire_audit_id = uuid.uuid4()
    await _write_audit_row(
        session,
        audit_id=expire_audit_id,
        event_class=AuditEventClass.APPROVAL_DECISION,
        operator=operator,
        request=request,
        path="approval.decision",
        status_code=_DECISION_STATUS_CODE_EXPIRED,
        duration_ms=0.0,
        extra_payload={
            "decision": "expired",
            "expires_at": str(request.expires_at),
            **(extra_payload or {}),
        },
    )

    _log().info(
        "approval_request_expired",
        approval_request_id=str(request.id),
        op_id=request.op_id,
        tenant_id=str(operator.tenant_id),
    )
    # Expose the audit_id as a transient attr so the caller can
    # record the ``approval.expired`` step event AFTER commit.
    # See create_pending_request / approve_request / reject_request
    # for the same publish-after-commit invariant: a publish-before-
    # commit would surface a phantom event if the commit fails.
    request._audit_id = expire_audit_id  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_reviewer_role(operator: Operator) -> None:
    """Raise :class:`UnauthorizedApprovalError` unless the operator may decide.

    The service-layer half of the two-layer approvals gate (the REST
    dependency :func:`~meho_backplane.auth.rbac.require_approvals_access` is
    the other). A principal may decide an approval when it is
    ``operator``-or-higher **or** carries the orthogonal ``approver``
    capability (#3243); only a ``read_only`` principal *without* the
    ``approver`` flag is refused. This mirrors the route gate so a
    caller reaching the service directly (agent runtime, tests) sees the
    same decoupled-from-dispatch access model, and keeps ``read_only``
    (no flag) failing closed exactly as before.
    """
    from meho_backplane.auth.operator import TenantRole, is_human_principal

    # Human-only governance (v0.1-spec §7): a machine
    # principal (agent / service / runner) can never decide an approval,
    # regardless of tenant role or the ``approver`` capability. Checked
    # first so a machine token minted ``tenant_admin`` is refused here too.
    # This mirrors the REST require_human_principal dependency as the
    # transport-independent backstop for direct service callers.
    if not is_human_principal(operator):
        raise NonHumanApprovalError(
            operator_sub=operator.sub,
            principal_kind=operator.principal_kind.value,
        )

    if operator.tenant_role == TenantRole.READ_ONLY and not operator.approver:
        raise UnauthorizedApprovalError(
            operator_sub=operator.sub,
            role=operator.tenant_role.value,
        )


#: The risk levels with the group rule (#3328). Break-glass
#: (``APPROVAL_ALLOW_SELF_APPROVAL``) never allows a self-approval of these
#: (#3198, #3290), and another person's change at these levels may be
#: approved only by the groups :func:`_check_approver_group` names.
#: ``dangerous`` is here because a delete-class op can register on it.
_HIGH_RISK_TIERS = frozenset({"dangerous", "destructive"})
#: The four level names. Any other stored value counts as ``destructive``.
_RISK_LEVELS = frozenset({"safe", "caution", "dangerous", "destructive"})


def is_high_risk_level(level: str | None) -> bool:
    """True when *level* is ``dangerous`` or ``destructive`` (the group rule applies)."""
    return level in _HIGH_RISK_TIERS


def may_approve_high_risk(operator: Operator) -> bool:
    """True when *operator* may approve another principal's high-risk change (#3328).

    The groups of ``docs/decisions/approval-groups-and-self-approval.md``:
    a tenant admin. Run approvals use the same rule
    (:meth:`~meho_backplane.operations.run_approvals.RunApprovalService.approve`).
    """
    return operator.may_self_approve or operator.tenant_role == TenantRole.TENANT_ADMIN


def _request_safety_level(request: ApprovalRequest) -> str | None:
    """Return the risk level the dispatcher stamped on *request*, or ``None``."""
    level = (request.proposed_effect or {}).get("safety_level")
    return level if isinstance(level, str) else None


def approval_risk_level(request: ApprovalRequest) -> str:
    """The risk level that decides who may approve *request*.

    The stamped level, or ``destructive`` when the request has none or a
    value that is not one of the four level names: such a request counts
    as the strictest level.
    """
    level = _request_safety_level(request)
    return level if level in _RISK_LEVELS else "destructive"


def request_waits_only_for_token_call(request: ApprovalRequest) -> bool:
    """True when *request* is a ``caution`` change that waits only because it came with a token.

    The park stamps the reason on ``proposed_effect`` (#3980). A request
    marked ``requires_approval`` never carries it.
    """
    effect = request.proposed_effect or {}
    return (
        effect.get("waits_because") == TOKEN_CALL_WAIT
        and _request_safety_level(request) == "caution"
    )


def _check_self_approval(
    operator: Operator, request: ApprovalRequest, *, log_break_glass: bool = True
) -> ApprovalRule | None:
    """Check a self-approval; return its rule, or ``None`` when it is not one.

    A self-approval is ``operator.sub == request.principal_sub``, or
    *operator* is one of the people behind the agent, service or runner
    login that asked (``request.people_behind``,
    :mod:`~meho_backplane.operations.people_behind`). The comparison is on
    the stable ``sub`` claim, so a renamed display name can never turn into
    a self-approval. Then, in order:

    1. A MEHO-Admin (:attr:`Operator.may_self_approve`)
       may approve their own change at every risk level. On the
       command line and REST the approve routes have already required a
       fresh sign-in (#3979).
    2. A person may approve their own ``caution`` change that waited only
       because it came with a token (``proposed_effect["waits_because"]``,
       #3980), but only with a click in the web interface
       (:attr:`Operator.web_session`). The command line and REST are
       refused below, unless break-glass covers the change.
    3. Anyone else may approve their own ``safe`` or ``caution`` change
       only when the deployment enabled the audited single-operator
       break-glass mode (``Settings.approval_allow_self_approval``). The
       switch never covers ``dangerous`` or ``destructive`` changes
       (#3198, #3290).
    4. Otherwise :class:`SelfApprovalForbiddenError` (403).

    The level is read off the ``proposed_effect`` envelope the dispatcher
    stamped at park time. Settings are imported lazily to keep the queue
    module decoupled from settings at import time.

    *log_break_glass* is ``False`` when MEHO only shows whether a person
    may approve (:mod:`~meho_backplane.operations.approval_rights`): that
    read approves nothing, so it logs no break-glass warning.
    """
    behind = operator.sub != request.principal_sub
    if behind and not is_person_behind(operator.sub, request.people_behind):
        return None
    if operator.may_self_approve:
        return ApprovalRule.SA_OPERATOR_SELF_APPROVAL
    token_wait = request_waits_only_for_token_call(request)
    if token_wait and operator.web_session:
        return ApprovalRule.TOKEN_CALL_SELF_APPROVAL

    high_risk = approval_risk_level(request) in _HIGH_RISK_TIERS

    from meho_backplane.settings import get_settings

    if not high_risk and get_settings().approval_allow_self_approval:
        if not log_break_glass:
            return ApprovalRule.BREAK_GLASS_SELF_APPROVAL
        _log().warning(
            "approval_self_approval_break_glass",
            approval_request_id=str(request.id),
            op_id=request.op_id,
            operator_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
        )
        return ApprovalRule.BREAK_GLASS_SELF_APPROVAL

    raise SelfApprovalForbiddenError(
        request.id,
        principal_sub=operator.sub,
        approver_kinds=_approver_kinds(request),
        high_risk=high_risk,
        web_click_allowed=token_wait,
        behind=behind,
    )


def _approver_kinds(request: ApprovalRequest) -> tuple[str, ...]:
    """The login group kinds that may approve *request* for another person.

    The same rules the approve checks: today's group rule
    (:func:`~meho_backplane.policy.routing.today_kinds`), and with approver
    routing on, only the kinds the request stored as well. Used for the
    "who may approve" part of :class:`SelfApprovalForbiddenError`.
    """
    kinds = today_kinds(approval_risk_level(request), request.op_id)
    if not routing_enabled():
        return kinds
    stored = stored_kinds(request.approver_roles)
    if stored is None:
        return kinds
    return narrow(kinds, stored)


def is_self_approval(sub: str, request: ApprovalRequest) -> bool:
    """True when *sub* approving *request* counts as an own change.

    *sub* is the requester, or one of the people behind the agent, service
    or runner login that asked (``request.people_behind``).
    """
    return sub == request.principal_sub or is_person_behind(sub, request.people_behind)


def _behind_audit_fields(sub: str, request: ApprovalRequest) -> dict[str, Any]:
    """``approver_behind_requester`` and its reasons, when *sub* is behind the requester."""
    whys = whys_of(sub, request.people_behind)
    if not whys:
        return {}
    return {"approver_behind_requester": True, "approver_behind_why": whys}


def is_admin_approver(operator: Operator) -> bool:
    """True when *operator* is a person who is an admin in the chosen tenant."""
    is_admin = operator.tenant_role == TenantRole.TENANT_ADMIN or operator.platform_admin
    return is_admin and operator.tenant_chosen and is_human_principal(operator)


def _check_requester_known(operator: Operator, request: ApprovalRequest) -> None:
    """Raise :class:`UnknownRequesterApprovalError` when only an admin may approve *request*.

    That is the case when an agent, service or runner login asked and MEHO
    found no person behind it. An unknown origin never gets less control
    than a known one.
    """
    if not people_behind_unknown(request.principal_kind, request.people_behind):
        return
    if is_admin_approver(operator):
        return
    raise UnknownRequesterApprovalError(
        request.id, operator_sub=operator.sub, role=operator.tenant_role.value
    )


def _check_approver_group(
    operator: Operator, request: ApprovalRequest, *, log_break_glass: bool = True
) -> ApprovalRule:
    """Return the rule that lets *operator* approve *request*, or raise (#3328).

    Runs after :func:`_check_reviewer_role`, so a machine principal is
    already refused. The decision depends only on the person's group and
    the risk level of the change; there are no per-system, per-connector
    or time-limited exceptions.

    * A change of rights: only an admin, with a click in the web console
      (:func:`~meho_backplane.operations.grant_approval.check_rights_change_approval`).
      The admin may be the person who asked, or one of the people behind
      the login that asked: that is recorded as
      ``sa_operator_self_approval``. No other rule below can allow it.
    * A change of a login's or group's rights:
      :func:`~meho_backplane.operations.grant_approval.check_grant_approval`.
    * A request of a machine login with nobody known behind it: only an
      admin (:func:`_check_requester_known`).
    * Own change, also as one of the people behind the login that asked:
      :func:`_check_self_approval`.
    * Another person's ``dangerous`` or ``destructive`` change: only a
      tenant admin may approve it, as the decision
      record ``docs/decisions/approval-groups-and-self-approval.md`` says.
      Everyone else gets :class:`ApprovalGroupForbiddenError` (403).
    * Another person's lower-risk change (also one that waits only because
      it is marked ``requires_approval``): the reviewer rule of
      :func:`_check_reviewer_role` is enough.

    A request without a stamped level counts as ``destructive`` here and
    in the break-glass check (:func:`approval_risk_level`).
    """
    check_rights_change_approval(operator, request)
    check_grant_approval(operator, request)
    _check_requester_known(operator, request)
    if is_rights_change(request) and is_self_approval(operator.sub, request):
        # The checks above decided: an admin, in the web console.
        return ApprovalRule.SA_OPERATOR_SELF_APPROVAL
    self_rule = _check_self_approval(operator, request, log_break_glass=log_break_glass)
    if self_rule is not None:
        return self_rule
    level = approval_risk_level(request)
    if not is_high_risk_level(level):
        return ApprovalRule.REVIEWER
    if may_approve_high_risk(operator):
        return ApprovalRule.SA_OPERATOR_OR_ADMIN
    raise ApprovalGroupForbiddenError(
        request.id,
        operator_sub=operator.sub,
        role=operator.tenant_role.value,
        safety_level=level,
    )


def _check_approver_role_floor(operator: Operator, request: ApprovalRequest) -> None:
    """Raise :class:`ApprovalRoleFloorError` when the approver is below the op's floor.

    A parked ``targets.register`` or ``topology.*`` write needs the
    ``tenant_admin`` role from the person who runs it
    (:mod:`meho_backplane.operations.role_floors`). Once approved, the
    approver is read as recorded at the decision: the agent's own resume
    and the hosted-run resume do not look at who approved again. So the
    role is checked here, at the decision, before any state change. An operation
    without a floor is not affected.
    """
    denial = role_floor_denial(request.op_id, operator.tenant_role)
    if denial is not None:
        raise ApprovalRoleFloorError(
            request.id,
            operator_sub=operator.sub,
            role=operator.tenant_role.value,
            denial=denial,
        )


def _check_routed_approver(operator: Operator, request: ApprovalRequest) -> None:
    """Raise :class:`ApprovalRoutingError` when *operator* is not in the stored group kinds.

    Runs after today's checks. Does nothing while the
    ``POLICY_APPROVER_ROUTING`` switch is off, or when the request stored no
    group kinds (it keeps today's rule).
    """
    if not routing_enabled():
        return
    refusal = routing_refusal(operator, request.approver_roles, request.approver_rule)
    if refusal is not None:
        raise ApprovalRoutingError(
            request.id,
            operator_sub=operator.sub,
            role=operator.tenant_role.value,
            reason=refusal,
        )


async def _check_install_floor(
    session: AsyncSession, operator: Operator, request: ApprovalRequest
) -> None:
    """Raise :class:`ApprovalRoutingError` when the install's current routing refuses *operator*.

    The install's routing is a floor: it is checked first, with the
    install's file as it is now, then the stored group kinds
    (:func:`_check_routed_approver`). Does nothing while the
    ``POLICY_APPROVER_ROUTING`` switch is off
    (:class:`~meho_backplane.policy.routing_floor.InstallFloor`). When an
    install entry counted, its place is kept on *request* for the approve
    audit row (``approver_floor``).
    """
    floor = await load_install_floor(session)
    if floor is None:
        return
    refusal = floor.refusal(operator, request)
    if refusal is not None:
        raise ApprovalRoutingError(
            request.id,
            operator_sub=operator.sub,
            role=operator.tenant_role.value,
            reason=refusal,
        )
    found = floor.check(request)
    if found is not None and found.floor is not None:
        request._approver_floor = found.floor  # type: ignore[attr-defined]


def _shadow_approval(operator: Operator, request: ApprovalRequest, *, today: bool) -> None:
    """Compare the policy evaluator with today's answer, when ``POLICY_ENGINE_SHADOW`` is on."""
    from meho_backplane.policy.shadow import shadow_approval

    shadow_approval(operator, request, today=today)


async def _load_pending_for_approval(
    session: AsyncSession,
    request_id: uuid.UUID,
    *,
    operator: Operator,
    params: dict[str, Any] | None,
) -> tuple[ApprovalRequest, ApprovalRule]:
    """Load + validate a row for approval, raising on any precondition failure.

    Runs the full approve precondition ladder in order so callers learn
    the most specific reason first: role gate → tenant-scoped load (under a
    row lock) → pending guard → deadline guard (security F12 / #274) →
    self-approval and group rule (#1401, #3328) → the op's role floor
    (:func:`_check_approver_role_floor`) → the install's current approver
    routing (:func:`_check_install_floor`) → the stored approver group kinds
    (:func:`_check_routed_approver`) → params-hash check (only when
    *params* is supplied) → destructive preview-binding re-verification
    (#3197). Returns the validated pending row and the
    rule that allowed the approval; the caller flips status + writes the
    decision audit row.

    The row is loaded ``FOR UPDATE`` so a competing approve / reject /
    expiry serialises on the lock and the loser re-reads the committed
    terminal state (the pending guard then refuses it) rather than
    overwriting a decision. The deadline guard re-checks ``expires_at`` at
    decision time — an overdue pending request is refused with
    :class:`ApprovalRequestExpiredError` even while the background sweep is
    delayed or failing, so it can never be approved into execution.
    """
    _check_reviewer_role(operator)
    request = await _load_for_tenant(session, request_id, operator.tenant_id, for_update=True)
    if request.status != ApprovalRequestStatus.PENDING.value:
        raise ApprovalRequestAlreadyDecidedError(request_id, request.status)
    if _deadline_has_passed(request, _now(), _resolve_default_ttl()):
        raise ApprovalRequestExpiredError(request_id)
    try:
        rule = _check_approver_group(operator, request)
        _check_approver_role_floor(operator, request)
    except (UnauthorizedApprovalError, SelfApprovalForbiddenError):
        _shadow_approval(operator, request, today=False)
        raise
    _shadow_approval(operator, request, today=True)
    await _check_install_floor(session, operator, request)
    _check_routed_approver(operator, request)
    if params is not None and not params_hash_matches(params, request.params_hash):
        raise ParamsMismatchError(request_id)
    _check_destructive_preview_binding(request)
    return request, rule


def _check_destructive_preview_binding(request: ApprovalRequest) -> None:
    """Raise :class:`PreviewBindingMissingError` on an unbound destructive request.

    The approve-time half of the preview-result-hash binding (#3197,
    decision requirement 2). Extends the params-hash swap-defence: a
    ``destructive``-tier request (``proposed_effect.safety_level ==
    "destructive"``) must carry a ``preview_hash``. The dispatcher already
    refuses to park one without a *verified* binding, so this is the
    fail-closed re-verification at the decision point — an unbound
    destructive row can never be approved into execution. Non-destructive
    requests are unaffected (they legitimately carry no binding).
    """
    effect = request.proposed_effect or {}
    if effect.get("safety_level") == "destructive" and not request.preview_hash:
        raise PreviewBindingMissingError(request.id)


async def _load_for_tenant(
    session: AsyncSession,
    request_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> ApprovalRequest:
    """Load an :class:`ApprovalRequest` by id, enforcing tenant isolation.

    Returns the row if found and owned by *tenant_id*; raises
    :class:`ApprovalNotFoundError` for missing rows or cross-tenant
    access (the two cases are indistinguishable to callers).

    When *for_update* is set the row is loaded ``SELECT ... FOR UPDATE``
    (security F12 / #274): the decision paths (approve / reject) take the
    row lock so a competing approve / reject / expiry serialises on it and
    the loser re-reads the committed terminal state rather than
    overwriting it, yielding exactly one winning transition. The read
    surfaces (``get_request`` / ``read_approval_result`` / ``list_pending``)
    leave it unset — they must not lock. On SQLite (the unit-suite driver)
    ``FOR UPDATE`` is a silent no-op; the concurrency guarantee is proven
    against Postgres in the integration suite.
    """
    row = await session.get(ApprovalRequest, request_id, with_for_update=for_update)
    if row is None or row.tenant_id != tenant_id:
        raise ApprovalNotFoundError(request_id)
    return row


# ---------------------------------------------------------------------------
# G11.2-T5 (#818) read helpers for the operator surfaces (REST GET /{id},
# MCP `meho_approvals_list` / `.get`, CLI `meho approvals list / show`).
# ---------------------------------------------------------------------------


#: Sort order of an approval list (#3596): ``"newest"`` is ``created_at``
#: descending (the long-standing default), ``"oldest"`` is ascending.
ApprovalListOrder = Literal["oldest", "newest"]


async def list_pending(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: str | None = "pending",
    work_ref: str | None = None,
    limit: int = 50,
    offset: int = 0,
    order: ApprovalListOrder = "newest",
    after: ApprovalRequest | None = None,
) -> list[ApprovalRequest]:
    """Page through approval requests in *tenant_id*.

    G11.2-T5 (#818) read substrate. ``status=None`` returns every state;
    ``status="pending"`` (the default for the operator UX) returns only
    requests awaiting a decision. ``work_ref`` (work_ref I2-T1 #1659),
    when supplied, narrows to requests authorised by that exact external
    change ticket (``"gh:evoila/meho#1"``); ``None`` applies no work_ref
    filter. Tenant-isolated by the WHERE clause — cross-tenant ids are
    invisible.

    ``order`` (#3596) picks the sort: ``"newest"`` (default) lists the
    newest request first; ``"oldest"`` lists the oldest first, which is the
    order a chain of requests under one change ticket was sent in. The
    oldest-first order breaks ties on ``id`` so it matches the step numbers
    :func:`pending_chain_positions` gives. Both orders break ties on ``id``,
    so pages read one after another (``offset``) never skip or repeat a row.

    ``after`` starts the list right after that row in *order*, instead of
    counting rows with ``offset``. A reader that walks the whole list in
    batches passes the last row of the batch before. Then a request that is
    decided between two batches does not move the rest of the list, so no
    row is skipped.
    """
    stmt = select(ApprovalRequest).where(ApprovalRequest.tenant_id == tenant_id)
    if status is not None:
        stmt = stmt.where(ApprovalRequest.status == status)
    if work_ref is not None:
        stmt = stmt.where(ApprovalRequest.work_ref.in_(work_ref_filter_values(work_ref)))
    if after is not None:
        if order == "oldest":
            stmt = stmt.where(
                or_(
                    ApprovalRequest.created_at > after.created_at,
                    and_(
                        ApprovalRequest.created_at == after.created_at,
                        ApprovalRequest.id > after.id,
                    ),
                )
            )
        else:
            stmt = stmt.where(
                or_(
                    ApprovalRequest.created_at < after.created_at,
                    and_(
                        ApprovalRequest.created_at == after.created_at,
                        ApprovalRequest.id < after.id,
                    ),
                )
            )
    if order == "oldest":
        stmt = stmt.order_by(ApprovalRequest.created_at.asc(), ApprovalRequest.id.asc())
    else:
        stmt = stmt.order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


@dataclass(frozen=True)
class ChainPosition:
    """Where one pending request sits in its change-ticket group (#3596).

    ``step`` is 1 for the oldest pending request of the group; ``total`` is
    how many requests of the group still wait for a decision.
    """

    work_ref: str
    step: int
    total: int


async def pending_chain_positions(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    work_ref: str | None = None,
) -> dict[uuid.UUID, ChainPosition]:
    """Number the pending requests of each multi-request change ticket (#3596).

    Requests sent under one ``work_ref`` are often steps of one chain: a
    later step needs an earlier one to have run. This reads every pending
    request of *tenant_id* that has a ``work_ref`` (only that ``work_ref``
    when one is given), groups them by ``work_ref``, and numbers each group
    oldest first. Only groups with two or more pending requests are
    returned; a lone request has no order to show. The numbers count
    pending requests only, so step 1 is always the oldest request that
    still waits. Tenant-isolated by the WHERE clause.

    With *work_ref*, every row the filter matches is one group, keyed by
    *work_ref*. The filter matches the cleaned id and the text as typed
    (:func:`work_ref_filter_values`), so rows of one ticket stored in two
    forms get one numbering, the same rows the filtered list shows.
    """
    stmt = (
        select(ApprovalRequest.id, ApprovalRequest.work_ref)
        .where(
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.status == ApprovalRequestStatus.PENDING.value,
            ApprovalRequest.work_ref.is_not(None),
        )
        .order_by(ApprovalRequest.created_at.asc(), ApprovalRequest.id.asc())
    )
    if work_ref is not None:
        stmt = stmt.where(ApprovalRequest.work_ref.in_(work_ref_filter_values(work_ref)))
    groups: dict[str, list[uuid.UUID]] = {}
    for request_id, ref in (await session.execute(stmt)).all():
        groups.setdefault(ref if work_ref is None else work_ref, []).append(request_id)
    positions: dict[uuid.UUID, ChainPosition] = {}
    for ref, ids in groups.items():
        if len(ids) < 2:
            continue
        for step, request_id in enumerate(ids, start=1):
            positions[request_id] = ChainPosition(work_ref=ref, step=step, total=len(ids))
    return positions


async def get_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
) -> ApprovalRequest:
    """Fetch one approval request by id, tenant-isolated.

    G11.2-T5 (#818) — drives ``GET /api/v1/approvals/{id}``,
    ``meho_approvals_get``, and ``meho approvals show``. Raises
    :class:`ApprovalNotFoundError` for missing rows or cross-tenant
    access (indistinguishable to the caller).
    """
    return await _load_for_tenant(session, request_id, tenant_id)


async def get_decision_delegation(
    session: AsyncSession, request: ApprovalRequest
) -> Delegation | None:
    """Read the delegation block off *request*'s decision audit row (#3692).

    The block lives only on the ``approval.decision`` audit row's payload
    (no column), so the single-request read views load it from there.
    Returns ``None`` for an undecided or expired request, for an ordinary
    decision, or when the stored block is not the expected shape
    (fail-open: the view just omits it).
    """
    if request.status not in (
        ApprovalRequestStatus.APPROVED.value,
        ApprovalRequestStatus.REJECTED.value,
    ):
        return None
    stmt = select(AuditLog.payload).where(
        AuditLog.tenant_id == request.tenant_id,
        AuditLog.path == "approval.decision",
        AuditLog.payload["approval_request_id"].as_string() == str(request.id),
    )
    if request.request_audit_id is not None:
        # Indexed narrowing: decision rows parent-link to the request row.
        stmt = stmt.where(AuditLog.parent_audit_id == request.request_audit_id)
    for payload in (await session.execute(stmt)).scalars():
        block = payload.get("delegation") if isinstance(payload, dict) else None
        if not isinstance(block, dict):
            continue
        refs = block.get("refs")
        return Delegation(
            instructed_by=str(block.get("instructed_by", "")),
            instruction=str(block.get("instruction", "")),
            refs=tuple(str(ref) for ref in refs) if isinstance(refs, list) else (),
        )
    return None


async def read_approval_result(
    session: AsyncSession,
    *,
    operator: Operator,
    request_id: uuid.UUID,
) -> ApprovalRequest:
    """Load a request for its owner + write the ``approval.result`` audit row (#3209).

    The principal-scoped result-retrieval surface behind
    ``GET /api/v1/approvals/{id}/result``. Two gates, in order:

    1. **Tenant isolation** via :func:`get_request` — a cross-tenant or
       missing id both raise :class:`ApprovalNotFoundError` (→ 404), so the
       existence of another tenant's request never leaks.
    2. **Principal scope** — only the request's owner
       (``operator.sub == request.principal_sub``, the party that parked the
       op) may read the re-dispatch result. Any other principal, operator role
       or not, gets :class:`ResultAccessForbiddenError` (→ 403). The result is
       otherwise visible only in the approver's inline response, so this is the
       surface that lets the *originating* consumer retrieve it.

    Writes one synchronous ``approval.result`` audit row (``method='APPROVAL'``,
    ``status_code=200``) in the caller's transaction before returning — the
    result surfacing is an auditable data egress (v0.1-spec §6), so the read
    leaves a ledger row like every other governed access, anchored in the
    request's replay subtree (#2086). The row records the request ``status``
    and whether a result was present (``resume_result_present``) so a
    poll-until-ready read pattern is visible on the ledger without the payload
    ever landing on it. The caller owns the commit.
    """
    request = await get_request(session, tenant_id=operator.tenant_id, request_id=request_id)
    if operator.sub != request.principal_sub:
        raise ResultAccessForbiddenError(request_id=request_id, principal_sub=operator.sub)
    await _write_audit_row(
        session,
        audit_id=uuid.uuid4(),
        event_class=AuditEventClass.LEGACY_REQUEST,
        operator=operator,
        request=request,
        path="approval.result",
        status_code=_DECISION_STATUS_CODE_APPROVED,
        duration_ms=0.0,
        extra_payload={
            "status": request.status,
            "resume_result_present": request.resume_result is not None,
        },
    )
    return request


# ---------------------------------------------------------------------------
# #3027 add-on step events for approval lifecycle steps.
# ---------------------------------------------------------------------------


async def record_approval_step_event(
    *,
    tenant_id: uuid.UUID,
    request: ApprovalRequest,
    decision: str,
    audit_id: uuid.UUID,
) -> None:
    """Record an approval lifecycle step for the paired add-on that asked for it.

    *decision* is one of ``"pending"`` (creation), ``"approved"``,
    ``"rejected"``, ``"expired"`` (sweeper-driven), or ``"withdrawn"``
    (the sender took it back, #4987). The event kind is
    ``approval.<decision>``.

    #3027 step-event push: the outcome is durably recorded for the paired
    add-on that **requested** the approval (matched by
    ``request.principal_sub`` — the requester's ``sub``, constant across
    the request's lifecycle, so every decision routes to the same add-on
    regardless of who decided it). The record is a self-contained
    fail-open commit; a no-op when the requester is not a paired add-on.

    No Broadcast event is published (#3758). The stored approval row is
    what waiters, the approvals bell and the console read.

    It also starts the push messages to the browsers of the people who may
    approve the request (:func:`~meho_backplane.web_push.send.notify_approval_step`).
    Every caller runs this after its commit, so a message never goes out
    for a request whose audit row did not commit.

    An ``approved`` event also carries ``reviewed_by`` (who approved),
    ``self_approved`` and, when :func:`approve_request` set it,
    ``approval_rule`` (#3328) -- the same fields as the
    ``approval.decision`` audit row.

    A ``pending`` event of a request that shares its ``work_ref`` with
    other pending requests also carries ``chain_step`` / ``chain_total``
    (#3596): "this is step k of N waiting under this change ticket".
    """
    from meho_backplane.operations.addon_step_events import AddonStepEventService
    from meho_backplane.web_push.send import notify_approval_step

    # Push messages to the people who may approve (web_push/send.py). It
    # only starts a background task, so it never slows the caller, and
    # never raises.
    notify_approval_step(tenant_id=tenant_id, request=request, decision=decision)
    payload: dict[str, object] = {
        "approval_request_id": str(request.id),
        "decision": decision,
        "connector_id": request.connector_id,
        "approval_op_id": request.op_id,
        "work_ref": request.work_ref,
    }
    if decision == "approved":
        payload["reviewed_by"] = request.reviewed_by
        reviewer = request.reviewed_by
        payload["self_approved"] = reviewer is not None and is_self_approval(reviewer, request)
        rule = getattr(request, "_approval_rule", None)
        if rule is not None:
            payload["approval_rule"] = str(rule)
    if decision == "pending" and request.work_ref is not None:
        position = await _chain_position_fail_open(tenant_id, request)
        if position is not None:
            payload["chain_step"] = position.step
            payload["chain_total"] = position.total
    await AddonStepEventService().record_if_owned_committed(
        tenant_id=tenant_id,
        owner_principal_sub=request.principal_sub,
        event_kind=f"approval.{decision}",
        work_ref=request.work_ref,
        audit_id=audit_id,
        payload=payload,
    )


async def _chain_position_fail_open(
    tenant_id: uuid.UUID, request: ApprovalRequest
) -> ChainPosition | None:
    """Return *request*'s step in its change-ticket group, or ``None`` (#3596).

    Fail-open like the step event itself: a read error only drops the
    step number from the event.
    """
    from meho_backplane.db.engine import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            positions = await pending_chain_positions(
                session, tenant_id=tenant_id, work_ref=request.work_ref
            )
    except Exception:
        _log().exception("approval_chain_position_failed", approval_request_id=str(request.id))
        return None
    return positions.get(request.id)
