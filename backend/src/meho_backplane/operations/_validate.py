# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Param validation + canonical hashing + policy gate for the G0.6 dispatcher.

Three small responsibilities the dispatcher (T5, #396) consults per
call:

* :func:`compute_params_hash` -- stable SHA-256 over canonicalised
  params for the audit row's ``params_hash`` field. Two dispatches
  with identical args produce identical hashes -- correlates retries,
  composite sub-calls, and reruns without persisting the params
  themselves to the audit row.
* :func:`validate_params` -- jsonschema 2020-12 (OpenAPI 3.1
  compatible) validation. Returns a list of structured error dicts;
  empty list = valid.
* :func:`policy_gate` -- G11.2-T3 per-(principal, op, target) verdict
  resolution: effective = user-role-allows ∩ agent-permission ∩
  op-requirement. Returns the three-state
  :class:`~meho_backplane.db.models.PermissionVerdict` so the
  dispatcher can handle ``auto-execute``, ``needs-approval``, and
  ``deny`` paths distinctly. G7 / G10 will extend the gate further
  without re-touching every dispatch call site.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

import structlog
from jsonschema import Draft202012Validator
from referencing.exceptions import Unresolvable

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.auth.permissions import _more_restrictive, resolve_verdict
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict

# Read-class HTTP verbs — the same set the ingest layer pins as
# ``READ_HTTP_METHODS`` (``operations/ingest/_internals.py``). Duplicated as
# a two-element literal here so the gate carries no import into the ingest
# package (which would risk an import cycle on the hot dispatch path).
_READ_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})

_log = structlog.get_logger(__name__)

__all__ = [
    "InvalidOpSchemaError",
    "compute_params_hash",
    "policy_gate",
    "validate_params",
]


class InvalidOpSchemaError(Exception):
    """The stored ``parameter_schema`` itself is broken — not the params.

    Raised by :func:`validate_params` when jsonschema's reference
    machinery cannot resolve a ``$ref`` inside the descriptor's stored
    schema (``referencing.exceptions.Unresolvable`` —
    ``PointerToNowhere`` / ``NoSuchAnchor``). The classic producer is a
    descriptor ingested before #3095's component bundling: its body
    schema carries ``$ref: "#/components/schemas/X"`` but ``X`` was
    never materialized into the stored document, so *no* input could
    ever validate. Callers map this to a structured
    ``invalid_op_schema`` error (the descriptor is at fault; the
    caller's params were never judged) instead of letting the
    referencing error escape as an HTTP 500.

    ``missing_ref`` carries the offending ``$ref`` in the spelling a
    spec author would recognise (``#/...``).
    """

    def __init__(self, missing_ref: str) -> None:
        self.missing_ref = missing_ref
        super().__init__(f"stored parameter_schema contains an unresolvable $ref: {missing_ref}")


def compute_params_hash(params: dict[str, Any]) -> str:
    """Return a stable SHA-256 hex hash over the canonicalised *params*.

    Used by the dispatcher's audit row so two dispatches with the same
    args land identical ``params_hash`` values -- correlates retries,
    composite sub-calls, and reruns without leaking the params
    themselves into the row (the full params live in the broadcast
    payload for non-sensitive op classes and never appear on the audit
    row in v0.2 -- see :class:`AuditLog`).

    Canonicalisation: ``json.dumps(..., sort_keys=True, default=str,
    separators=(",", ":"))``. ``default=str`` covers non-JSON natives
    (e.g. :class:`datetime` or :class:`uuid.UUID`) the caller may slip in
    without forcing every call site to pre-stringify them.
    """
    canonical = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_params(
    parameter_schema: dict[str, Any],
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate *params* against *parameter_schema* via JSON Schema 2020-12.

    Returns a list of validation-error dicts (``[]`` on success). Each
    entry carries ``path`` (JSON Pointer-ish dotted shape), ``message``,
    and ``validator`` so the dispatcher's ``invalid_params`` error
    payload is operator-actionable without leaking the JSON Schema's
    internals.

    Empty / missing schemas validate everything as ok -- typed ops
    registered without a parameter_schema (or with ``{}``) accept any
    params; the dispatcher is permissive at the schema layer when the
    descriptor itself is.

    Raises:
        InvalidOpSchemaError: The stored schema carries a ``$ref`` the
            validator cannot resolve within the schema document
            (#3095). The fault is the descriptor's, not the caller's,
            so it is a distinct typed exception rather than an entry in
            the returned (caller-attributed) error list. Surfaces
            lazily — jsonschema resolves refs while validating — which
            is why the ``iter_errors`` loop sits inside the guard.
    """
    if not parameter_schema:
        return []
    out: list[dict[str, Any]] = []
    try:
        validator = Draft202012Validator(parameter_schema)
        for err in validator.iter_errors(params):
            out.append(
                {
                    "path": err.json_path,
                    "message": err.message,
                    "validator": err.validator,
                }
            )
    except Unresolvable as exc:
        pointer = str(exc.ref)
        missing_ref = f"#{pointer}" if pointer.startswith("/") else pointer
        raise InvalidOpSchemaError(missing_ref) from exc
    return out


def _is_mutating(descriptor: EndpointDescriptor) -> bool:
    """Whether *descriptor* names a mutating (non-read) operation.

    Grounded in the fields the repo already carries — no invented column:

    * an ingested op declares an HTTP ``method``; it is read-class iff the
      verb is in :data:`_READ_METHODS` (``GET`` / ``HEAD``), mutating
      otherwise (``POST`` / ``PUT`` / ``PATCH`` / ``DELETE``);
    * a typed / composite op carries no ``method``, so its curated
      ``safety_level`` stands in — a ``safe`` typed op is a read, a
      ``caution`` / ``dangerous`` one is a write by convention.
    """
    method = descriptor.method
    if method:
        return method.upper() not in _READ_METHODS
    return descriptor.safety_level != "safe"


def _service_safety_gate_reason(descriptor: EndpointDescriptor) -> str | None:
    """Extra gating a **service** principal gets from ``safety_level`` (#3152).

    Resolution of #3152 as its option 1: the non-agent gate now consults
    ``safety_level`` for a service principal on a ``requires_approval=False``
    op. A ``destructive`` op parks always and is never grant-satisfiable
    (#3183); a ``dangerous`` op parks always; a mutating ``caution`` op
    parks; ``safe`` (and any non-mutating) op is unchanged (auto-executes).
    A standing grant is the sanctioned path to run a ``dangerous`` /
    ``caution`` op unattended — but never a ``destructive`` one (the grant
    lookup in :func:`~meho_backplane.operations.service_grants.consult_and_record_grant`
    refuses the tier before matching a row).

    Returns the park reason, or ``None`` when the op stays auto-execute.
    """
    if descriptor.safety_level == "destructive":
        return (
            "safety_level=destructive; service-principal deletes park always "
            "(routed to the approval queue) and are never satisfiable by a "
            "standing grant — mandatory human approval (#3183)"
        )
    if descriptor.safety_level == "dangerous":
        return (
            "safety_level=dangerous; service-principal mutations park "
            "(routed to the approval queue) unless a standing grant covers them (#3152)"
        )
    if descriptor.safety_level == "caution" and _is_mutating(descriptor):
        return (
            "safety_level=caution mutating op; service-principal mutations park "
            "unless a standing grant covers them (#3152)"
        )
    return None


async def _warn_if_grant_holding_non_service(operator: Operator) -> None:
    """Emit a WARN when a **non-service** principal that is about to park
    holds a live standing grant (#3178).

    That combination is almost certainly the #3178 misclassification bug: a
    service account whose client-credentials token missed the
    service-account marker classified as ``user`` (or, rarely, a genuine
    human who was mistakenly issued a grant). Either way the grant is
    silently inert and the op is parking — exactly the pattern #3151/#3152
    exists to eliminate — so it warrants an operator-visible signal.

    Scoped to the **park** path only (already the slow path): the extra
    count query never touches the auto-execute hot path. Fail-open — a
    diagnostic must never block a dispatch, so any lookup error is
    swallowed after logging.
    """
    from meho_backplane.operations.service_grants import count_live_grants_for_principal

    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            live_grants = await count_live_grants_for_principal(
                session,
                tenant_id=operator.tenant_id,
                principal_sub=operator.sub,
            )
        if live_grants > 0:
            _log.warning(
                "policy_gate_grant_holder_classified_non_service",
                operator_sub=operator.sub,
                principal_kind=operator.principal_kind.value,
                tenant_id=str(operator.tenant_id),
                live_grant_count=live_grants,
                hint=(
                    "principal holds >=1 active standing grant but is not "
                    "classified 'service'; the grants gate cannot evaluate it "
                    "(likely a client-credentials token missing the "
                    "service-account marker -- see #3178)"
                ),
            )
    except Exception:
        _log.exception(
            "policy_gate_grant_holder_check_failed",
            operator_sub=operator.sub,
        )


async def _non_agent_verdict(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str | None,
) -> tuple[PermissionVerdict, str | None]:
    """Resolve the verdict for a human / service (non-agent) principal.

    Preserves the v0.2 default-allow contract for a human ``USER`` operator
    — an ordinary op auto-executes and a ``requires_approval`` op routes to
    the approval queue (G11.7-T1 #1401); they are their own approver.

    For a **service** principal (client-credentials, ``principal_kind =
    service``) the gate additionally consults ``safety_level`` (#3152) so a
    mutating ``caution`` / ``dangerous`` op parks even without
    ``requires_approval``, and — for any parked service-principal op — a
    live matching **standing grant** (#3151) auto-approves it: the grant
    use is recorded on the approvals audit ledger and the gate clears to
    ``AUTO_EXECUTE``. Absent a grant the op parks for a human decision.
    """
    target_id_str = str(getattr(target, "id", None)) if target is not None else None
    is_service = operator.principal_kind is PrincipalKind.SERVICE

    gate_reason: str | None = None
    if descriptor.safety_level == "destructive":
        # #3198: the destructive tier is mandatory-human-approval for EVERY
        # non-agent principal — it parks regardless of ``requires_approval``
        # and regardless of principal kind. Without this branch a USER
        # dispatching a destructive op declared ``requires_approval=False``
        # would auto-execute below (the safety_level→park mapping lived only
        # on the ``elif is_service`` branch), bypassing the preview-hash +
        # blast-radius gate entirely. No standing grant can pre-clear it
        # either: ``consult_and_record_grant`` refuses the destructive tier
        # (decision ``governed-delete-operations.md`` requirement 1).
        gate_reason = (
            "safety_level=destructive; mandatory human approval — every "
            "non-agent principal parks (routed to the approval queue), never "
            "auto-executes, and no standing grant can pre-clear it (#3183)"
        )
    elif descriptor.requires_approval:
        gate_reason = "requires_approval is True; routed to the approval queue (G11.7-T1)"
    elif is_service:
        gate_reason = _service_safety_gate_reason(descriptor)

    if gate_reason is None:
        _log.info(
            "policy_gate_default_allow",
            operator_sub=operator.sub,
            principal_kind=operator.principal_kind.value,
            tenant_id=str(operator.tenant_id),
            op_id=descriptor.op_id,
            safety_level=descriptor.safety_level,
            target_id=target_id_str,
        )
        return PermissionVerdict.AUTO_EXECUTE, None

    # The op would park. A service principal can clear the gate with a live
    # standing grant (recorded as an auto-approval on the audit ledger).
    if is_service and connector_id is not None:
        from meho_backplane.operations.service_grants import consult_and_record_grant

        grant_id = await consult_and_record_grant(
            operator=operator,
            descriptor=descriptor,
            target=target,
            connector_id=connector_id,
        )
        if grant_id is not None:
            _log.info(
                "policy_gate_standing_grant_auto_approved",
                operator_sub=operator.sub,
                principal_kind=operator.principal_kind.value,
                tenant_id=str(operator.tenant_id),
                op_id=descriptor.op_id,
                safety_level=descriptor.safety_level,
                target_id=target_id_str,
                grant_id=str(grant_id),
            )
            return PermissionVerdict.AUTO_EXECUTE, f"auto-granted by standing grant {grant_id}"

    if not is_service:
        # The op is parking for a non-service principal. If that principal
        # holds a live standing grant, surface the #3178 misclassification.
        await _warn_if_grant_holding_non_service(operator)

    _log.info(
        "policy_gate_needs_approval",
        operator_sub=operator.sub,
        principal_kind=operator.principal_kind.value,
        tenant_id=str(operator.tenant_id),
        op_id=descriptor.op_id,
        safety_level=descriptor.safety_level,
        target_id=target_id_str,
    )
    return PermissionVerdict.NEEDS_APPROVAL, gate_reason


async def policy_gate(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str | None = None,
) -> tuple[PermissionVerdict, str | None]:
    """G11.2-T3 per-(principal, op, target) policy gate.

    Returns ``(verdict, reason_or_None)`` where *verdict* is one of
    :attr:`~meho_backplane.db.models.PermissionVerdict.AUTO_EXECUTE`,
    :attr:`~meho_backplane.db.models.PermissionVerdict.NEEDS_APPROVAL`,
    or :attr:`~meho_backplane.db.models.PermissionVerdict.DENY`.

    The dispatcher branches on *verdict*:

    * ``auto-execute`` — proceed to connector resolution + execution.
    * ``needs-approval`` — write a durable
      :class:`~meho_backplane.db.models.ApprovalRequest` row and return an
      ``awaiting_approval`` result, via
      :func:`~meho_backplane.operations.approval_queue.create_pending_request`
      (G11.2-T4, #817). Reached by both agent principals (verdict floor)
      and, since G11.7-T1 (#1401), human/service principals on a
      ``requires_approval`` op.
    * ``deny`` — write an audit row in ``denied`` status, return
      :func:`~meho_backplane.operations._errors.result_denied` with the
      *reason* string so the agent can reason about the refusal.

    Effective verdict = user-role-allows ∩ agent-permission ∩
    op-requirement, resolved by
    :func:`~meho_backplane.auth.permissions.resolve_verdict`. See that
    module for the full resolution algorithm.

    Principal-kind branch
    ---------------------

    The per-(principal, op, target) agent-permission model gates **agent
    principals** (``principal_kind == agent``) through
    :func:`~meho_backplane.auth.permissions.resolve_verdict`. Human
    operators keep the v0.2 default-allow contract for ordinary ops, but a
    ``requires_approval`` op now routes them to the **approval queue**
    (``needs-approval``) rather than hard-denying.

    **Service principals** (client-credentials, ``principal_kind ==
    service``) additionally consult ``safety_level`` (#3152): a mutating
    ``caution`` / ``dangerous`` op parks even without ``requires_approval``.
    Any parked service-principal op is auto-approved when a live matching
    **standing grant** exists (#3151), which requires *connector_id* to be
    supplied so the grant's connector scope can be matched; the grant use
    is recorded on the approvals audit ledger. See :func:`_non_agent_verdict`.

    G11.7-T1 (#1401) routes ``USER`` operators to ``needs-approval`` on a
    ``requires_approval`` op (reusing the queue/approve/resume substrate)
    rather than hard-denying — so no existing human-runnable op regresses.
    The agent path additionally folds ``requires_approval`` into the
    verdict as a ``needs-approval`` floor, so an op the connector author
    marked as requiring approval is never auto-executed by an agent
    regardless of its ``safety_level``.

    The function is **async** — it opens its own DB session to load the
    caller's :class:`~meho_backplane.db.models.AgentPermission` rows,
    mirroring the same pattern :func:`audit_and_broadcast_safe` uses.
    """
    # --- Human / service principals: default-allow, queue on approval --
    # (a service principal additionally consults safety_level + standing
    # grants — see :func:`_non_agent_verdict`).
    if operator.principal_kind is not PrincipalKind.AGENT:
        return await _non_agent_verdict(
            operator=operator,
            descriptor=descriptor,
            target=target,
            connector_id=connector_id,
        )

    # --- Agent principals: per-(principal, op, target) verdict ----------
    target_id = getattr(target, "id", target) if target is not None else None
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        verdict, reason = await resolve_verdict(
            session=session,
            operator=operator,
            op_id=descriptor.op_id,
            safety_level=descriptor.safety_level,
            target_id=target_id,
        )
    if descriptor.requires_approval:
        floored = _more_restrictive(verdict, PermissionVerdict.NEEDS_APPROVAL)
        if floored is not verdict:
            reason = f"{reason}; floored to needs-approval (descriptor.requires_approval)"
            verdict = floored
    return verdict, reason
