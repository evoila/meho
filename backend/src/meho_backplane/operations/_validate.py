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
* :func:`policy_gate` -- v0.2 default-allow with the
  ``requires_approval`` honor; G7 / G10 swap in the real engine here
  without re-touching every dispatch call site.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "compute_params_hash",
    "policy_gate",
    "validate_params",
]

_log = structlog.get_logger(__name__)


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


def policy_gate(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
) -> tuple[bool, str | None]:
    """v0.2 default-allow policy gate.

    Returns ``(allowed, reason_or_None)``. G7 / G10 hooks into this
    function -- when those Goals land, the body grows the real policy
    decision and the dispatcher's call site stays unchanged. The
    structured-log line ``policy_gate_default_allow`` is the operator's
    signal that no real policy is in effect (a future audit query can
    count these events to verify the upgrade landed everywhere).

    The function is intentionally synchronous -- v0.2's only decision
    is "did the connector author flag this op as requiring approval?".
    Async I/O against a remote policy service is a G7+ concern; the
    call site already awaits the surrounding context so promoting this
    to async later is a non-breaking change.
    """
    if descriptor.requires_approval:
        # v0.2 doesn't ship the approval workflow -- record the decision
        # and deny rather than silently allowing. G10's approval queue
        # will replace the deny with a "pending" path.
        return False, "requires_approval is True; v0.2 has no approval workflow"
    _log.info(
        "policy_gate_default_allow",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        op_id=descriptor.op_id,
        safety_level=descriptor.safety_level,
        target_id=str(getattr(target, "id", None)) if target is not None else None,
    )
    return True, None
