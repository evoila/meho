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

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.auth.permissions import _more_restrictive, resolve_verdict
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict

_log = structlog.get_logger(__name__)

__all__ = [
    "compute_params_hash",
    "policy_gate",
    "validate_params",
]


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
    """
    if not parameter_schema:
        return []
    validator = Draft202012Validator(parameter_schema)
    out: list[dict[str, Any]] = []
    for err in validator.iter_errors(params):
        out.append(
            {
                "path": err.json_path,
                "message": err.message,
                "validator": err.validator,
            }
        )
    return out


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
      (G11.2-T4, #817). Only agent principals reach this verdict.
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
    principals** (``principal_kind == agent``). Human operators and
    service accounts keep the v0.2 contract — default-allow except an op
    flagged ``requires_approval`` — so a caution/dangerous op a human
    has always been able to run does not silently start pending/denying.
    This is exactly G11.2-T4 (#817)'s stated split: replace the
    ``requires_approval`` hard-deny with the pending path *for agents*;
    humans keep current behaviour. The agent path additionally folds
    ``requires_approval`` into the verdict as a ``needs-approval`` floor,
    so an op the connector author marked as requiring approval is never
    auto-executed by an agent regardless of its ``safety_level``.

    The function is **async** — it opens its own DB session to load the
    caller's :class:`~meho_backplane.db.models.AgentPermission` rows,
    mirroring the same pattern :func:`audit_and_broadcast_safe` uses.
    The dispatcher's call site changes only in adding ``await``; the
    signature (operator / descriptor / target) stays identical to v0.2.
    """
    # --- Human / service principals: preserve the v0.2 contract --------
    if operator.principal_kind is not PrincipalKind.AGENT:
        if descriptor.requires_approval:
            # v0.2 hard-deny is retained for non-agent principals; the
            # approval queue (G11.2-T4) routes only agent runs to the
            # pending path.
            return (
                PermissionVerdict.DENY,
                "requires_approval is True; only agent principals are routed "
                "to the approval queue (G11.2-T4)",
            )
        _log.info(
            "policy_gate_default_allow",
            operator_sub=operator.sub,
            principal_kind=operator.principal_kind.value,
            tenant_id=str(operator.tenant_id),
            op_id=descriptor.op_id,
            safety_level=descriptor.safety_level,
            target_id=str(getattr(target, "id", None)) if target is not None else None,
        )
        return PermissionVerdict.AUTO_EXECUTE, None

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
