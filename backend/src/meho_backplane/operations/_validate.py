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

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.auth.permissions import _more_restrictive, resolve_verdict
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.operations._param_errors import describe_violation
from meho_backplane.sandbox_mode import sandbox_mode_active

# Read-class HTTP verbs — the same set the ingest layer pins as
# ``READ_HTTP_METHODS`` (``operations/ingest/_internals.py``). Duplicated as
# a two-element literal here so the gate carries no import into the ingest
# package (which would risk an import cycle on the hot dispatch path).
_READ_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})

_log = structlog.get_logger(__name__)

__all__ = [
    "InvalidOpSchemaError",
    "compute_params_hash",
    "ingested_schema_for_validation",
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

    Returns a list of validation-error dicts (``[]`` on success), one
    per violation. Each entry carries ``path`` (JSON path of the bad
    spot), ``validator`` (the failed schema keyword), ``expected`` (what
    the schema wants there) and ``remediation`` (a short fixed hint),
    built by :func:`~meho_backplane.operations._param_errors.describe_violation`
    (#3669). No entry carries the rejected value: jsonschema's own
    ``message`` quotes it, so it is not used. The list is not capped
    here; :func:`~meho_backplane.operations._errors.result_invalid_params`
    caps what reaches the envelope.

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
        out.extend(describe_violation(err) for err in validator.iter_errors(params))
    except Unresolvable as exc:
        pointer = str(exc.ref)
        missing_ref = f"#{pointer}" if pointer.startswith("/") else pointer
        raise InvalidOpSchemaError(missing_ref) from exc
    return out


def ingested_schema_for_validation(parameter_schema: dict[str, Any]) -> dict[str, Any]:
    """Return *parameter_schema* strengthened to reject undeclared params.

    Fail-closed dispatch backstop (#293 / security review S05) for
    ``source_kind='ingested'`` descriptors. The OpenAPI ingester now emits
    ``additionalProperties: false`` on every built schema
    (:func:`~meho_backplane.operations.ingest.openapi._build_parameter_schema`),
    but a descriptor persisted before that fix carries no such clause, so
    :func:`validate_params` -- JSON Schema 2020-12, which defaults
    ``additionalProperties`` to ``true`` -- would accept a param the op
    never declared and let the dispatcher forward it verbatim onto the
    vendor query string. Applying the clause at the dispatch seam closes
    that window for already-ingested connectors without a re-ingest, and
    produces the *same* ``invalid_params`` error shape (``validator ==
    "additionalProperties"``) a freshly-ingested strict descriptor does.

    A no-op when the schema already declares ``additionalProperties`` (a
    post-fix descriptor, or one an operator deliberately widened) or is
    not an object schema with declared ``properties`` (an empty ``{}``
    descriptor stays permissive, matching :func:`validate_params`). The
    returned mapping is a shallow copy -- the stored descriptor's JSON
    column is never mutated, and the nested ``properties`` /
    ``components`` are shared by reference (``additionalProperties: false``
    constrains only the top-level instance keys, so nested body/object
    fields stay unconstrained).
    """
    if not isinstance(parameter_schema, dict):
        return parameter_schema
    if "properties" not in parameter_schema:
        return parameter_schema
    if "additionalProperties" in parameter_schema:
        return parameter_schema
    return {**parameter_schema, "additionalProperties": False}


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


def _sandbox_delete_grant_note() -> str:
    """The sentence a park reason adds in sandbox mode (#4622)."""
    return (
        "; this instance is in sandbox mode, so a delete grant made in the web console may cover it"
    )


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
        if sandbox_mode_active():
            return (
                "safety_level=destructive; service-principal deletes park "
                "(routed to the approval queue) unless a delete grant made in the "
                "web console covers them — this instance is in sandbox mode (#4622)"
            )
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


async def _null_target_park_hint(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str,
) -> str | None:
    """Return the #3349 null-target loud-hint for a parking dispatch, or None.

    Fail-open — a diagnostic must never block a dispatch, so any lookup
    error is swallowed after logging (mirrors
    :func:`_warn_if_grant_holding_non_service`).
    """
    from meho_backplane.operations.service_grants import null_target_only_park_hint

    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            return await null_target_only_park_hint(
                session,
                tenant_id=operator.tenant_id,
                principal_sub=operator.sub,
                op_id=descriptor.op_id,
                connector_id=connector_id,
                target_id=getattr(target, "id", None),
            )
    except Exception:
        _log.exception("policy_gate_null_target_hint_failed", operator_sub=operator.sub)
        return None


async def _service_grant_verdict(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str,
    gate_reason: str | None,
    target_id_str: str | None,
    preview_hash: str | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[PermissionVerdict | None, str | None]:
    """Consult a standing grant for a parking service-principal op (#3151 / #3349).

    Returns ``(AUTO_EXECUTE, reason)`` when a live grant (concrete-target or
    selector, #3349) clears the gate, else ``(None, gate_reason)`` — with the
    #3349 null-target hint appended to *gate_reason* when the dispatch is
    target-scoped and the only live candidate for this
    ``(principal, op, connector)`` is a null-target grant (the operator
    likely created it believing null = "any target").
    """
    from meho_backplane.operations.service_grants import consult_and_record_grant

    grant_id = await consult_and_record_grant(
        operator=operator,
        descriptor=descriptor,
        target=target,
        connector_id=connector_id,
        preview_hash=preview_hash,
        params=params,
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

    hint = await _null_target_park_hint(
        operator=operator,
        descriptor=descriptor,
        target=target,
        connector_id=connector_id,
    )
    if hint is not None:
        gate_reason = f"{gate_reason}; {hint}" if gate_reason else hint
    return None, gate_reason


async def _non_agent_verdict(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str | None,
    preview_hash: str | None = None,
    params: dict[str, Any] | None = None,
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
        if is_service and sandbox_mode_active():
            gate_reason += _sandbox_delete_grant_note()
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
        cleared, gate_reason = await _service_grant_verdict(
            operator=operator,
            descriptor=descriptor,
            target=target,
            connector_id=connector_id,
            gate_reason=gate_reason,
            target_id_str=target_id_str,
            preview_hash=preview_hash,
            params=params,
        )
        if cleared is not None:
            return cleared, gate_reason

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
    preview_hash: str | None = None,
    params: dict[str, Any] | None = None,
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

    **Direct hosted-agent runs** (#4237): when an actor is bound, the call
    gets the stricter of the caller's and the hosted agent's verdict
    (:func:`_direct_run_verdict`). This applies to every caller kind: an
    agent-kind caller keeps its own grants and the hosted agent's grants
    limit the run on top of them.

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
    mirroring the same pattern :func:`audit_safe` uses.

    ``preview_hash`` and ``params`` (#4622) are read only by the
    service-principal grant consult on an instance in sandbox mode: a
    ``destructive`` delete under a delete grant must present the hash of
    its own preview and a valid blast radius before the grant use is
    recorded. Every other path ignores them.
    """
    actor_sub = resolve_actor_sub()
    if actor_sub is not None:
        return await _direct_run_verdict(
            operator=operator,
            actor_sub=actor_sub,
            descriptor=descriptor,
            target=target,
            connector_id=connector_id,
            preview_hash=preview_hash,
            params=params,
        )
    return await _caller_verdict(
        operator=operator,
        descriptor=descriptor,
        target=target,
        connector_id=connector_id,
        preview_hash=preview_hash,
        params=params,
    )


async def _caller_verdict(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str | None,
    preview_hash: str | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[PermissionVerdict, str | None]:
    """Verdict for the caller alone, by its principal kind."""
    if operator.principal_kind is PrincipalKind.AGENT:
        return await _agent_verdict(operator=operator, descriptor=descriptor, target=target)
    return await _non_agent_verdict(
        operator=operator,
        descriptor=descriptor,
        target=target,
        connector_id=connector_id,
        preview_hash=preview_hash,
        params=params,
    )


async def _direct_run_verdict(
    *,
    operator: Operator,
    actor_sub: str,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str | None,
    preview_hash: str | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[PermissionVerdict, str | None]:
    """Verdict for a call made by a direct hosted-agent run (#4237).

    The caller who started the run is the subject; the run's agent is the
    actor. The hosted agent's verdict uses its ``agent:<name>`` grants, the
    agent safety defaults and the caller's role ceiling. The caller's own
    verdict uses the caller's principal kind (:func:`_caller_verdict`), so
    an agent-kind caller keeps its own grants. The stricter of the two
    verdicts wins.

    The caller's verdict is skipped when it cannot change the outcome. A
    hosted-agent deny is already the strictest verdict. When the hosted
    agent needs approval, only an agent-kind caller can still make it
    stricter (a deny grant); a human or service caller never gets deny
    (:func:`_non_agent_verdict`), and working out its verdict could record
    a standing grant as used for a call that then waits for approval.
    """
    agent = operator.model_copy(
        update={"sub": actor_sub, "principal_kind": PrincipalKind.AGENT, "client_id": actor_sub}
    )
    agent_verdict, agent_reason = await _agent_verdict(
        operator=agent, descriptor=descriptor, target=target
    )
    agent_result = (agent_verdict, f"agent {actor_sub} acting for {operator.sub}: {agent_reason}")
    if agent_verdict is PermissionVerdict.DENY:
        return agent_result
    if (
        agent_verdict is PermissionVerdict.NEEDS_APPROVAL
        and operator.principal_kind is not PrincipalKind.AGENT
    ):
        return agent_result
    verdict, reason = await _caller_verdict(
        operator=operator,
        descriptor=descriptor,
        target=target,
        connector_id=connector_id,
        preview_hash=preview_hash,
        params=params,
    )
    if _more_restrictive(verdict, agent_verdict) is verdict:
        return verdict, reason
    return agent_result


async def _agent_verdict(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
) -> tuple[PermissionVerdict, str | None]:
    """Resolve the per-(principal, op, target) verdict for an agent principal."""
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
