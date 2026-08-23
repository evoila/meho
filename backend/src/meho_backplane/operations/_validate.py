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
from typing import Any

import structlog
from jsonschema import Draft202012Validator
from referencing.exceptions import Unresolvable

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.auth.permissions import _more_restrictive, resolve_verdict
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict

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


def _non_agent_verdict(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
) -> tuple[PermissionVerdict, str | None]:
    """Resolve the verdict for a human / service (non-agent) principal.

    Preserves the v0.2 default-allow contract for ordinary ops, but
    routes a ``requires_approval`` op to ``needs-approval`` (the approval
    queue) rather than hard-denying it (G11.7-T1 #1401). Self-approval is
    blocked at decide time by ``approve_request``'s requester != approver
    guard — the gate's job here is only to park the call.
    """
    target_id_str = str(getattr(target, "id", None)) if target is not None else None
    if descriptor.requires_approval:
        _log.info(
            "policy_gate_needs_approval",
            operator_sub=operator.sub,
            principal_kind=operator.principal_kind.value,
            tenant_id=str(operator.tenant_id),
            op_id=descriptor.op_id,
            safety_level=descriptor.safety_level,
            target_id=target_id_str,
        )
        return (
            PermissionVerdict.NEEDS_APPROVAL,
            "requires_approval is True; routed to the approval queue (G11.7-T1)",
        )
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


async def policy_gate(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
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
    operators and service accounts keep the v0.2 default-allow contract
    for ordinary ops, but a ``requires_approval`` op now routes them to
    the **approval queue** (``needs-approval``) rather than hard-denying.

    G11.7-T1 (#1401) closes the Phase-C gap: ops-team operators
    authenticate as ``USER`` principals
    (:func:`meho_backplane.auth.operator`), so the old hard-deny meant
    every governed connector write returned ``denied`` to the very
    people meant to run it. Routing them to ``needs-approval`` reuses
    the already-built queue/approve/resume substrate
    (:func:`~meho_backplane.operations.approval_queue.create_pending_request`
    accepts any operator; the REST/MCP/CLI approve surfaces and the
    ``_approved=True`` resume path are unchanged) — it is a one-branch
    policy change, not new infrastructure. A non-``requires_approval``
    op a human has always been able to run still auto-executes, so no
    existing human-runnable op regresses.

    The agent path additionally folds ``requires_approval`` into the
    verdict as a ``needs-approval`` floor, so an op the connector author
    marked as requiring approval is never auto-executed by an agent
    regardless of its ``safety_level``.

    The function is **async** — it opens its own DB session to load the
    caller's :class:`~meho_backplane.db.models.AgentPermission` rows,
    mirroring the same pattern :func:`audit_and_broadcast_safe` uses.
    The dispatcher's call site changes only in adding ``await``; the
    signature (operator / descriptor / target) stays identical to v0.2.
    """
    # --- Human / service principals: default-allow, queue on approval --
    if operator.principal_kind is not PrincipalKind.AGENT:
        return _non_agent_verdict(operator=operator, descriptor=descriptor, target=target)

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
