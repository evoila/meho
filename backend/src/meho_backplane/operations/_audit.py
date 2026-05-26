# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Audit row writing + broadcast publishing for the G0.6 dispatcher.

The dispatcher (T5, #396) writes one ``audit_log`` row per dispatch
synchronously and publishes one :class:`BroadcastEvent` (fail-open per
the :func:`publish_event` contract). This module owns:

* :func:`write_audit_row` -- the row insert. Composes the payload from
  the descriptor + parent audit linkage (composite recursion contextvar)
  + result_status.
* :func:`publish_broadcast` -- the broadcast emit with redacted payload.
* :func:`audit_and_broadcast_safe` -- the dispatcher's "write + publish,
  swallow internal failures" wrapper. The :class:`OperationResult` has
  already been decided by the time this runs; audit/broadcast failures
  log loudly but don't flip the outcome.

The audit-row contract uses ``method='DISPATCH'`` and
``path=descriptor.op_id`` so the chassis :class:`AuditLog` table shape
(HTTP-shaped columns) doesn't need a migration. The richer dispatcher-
specific fields land in ``payload``.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast import (
    BroadcastEvent,
    classify_op,
    publish_event,
    redact_payload,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations._errors import status_code_for_result

__all__ = [
    "audit_and_broadcast_safe",
    "parent_audit_id_var",
    "publish_broadcast",
    "write_audit_row",
]

_log = structlog.get_logger(__name__)


#: ContextVar carrying the parent audit row id when a composite handler
#: dispatches a sub-op. T7 (#398) builds the ``dispatch_child`` callable
#: that binds + resets this contextvar around every recursive dispatch
#: from inside a composite handler; the AuditMiddleware (this module's
#: :func:`write_audit_row`) reads it and writes it into the real
#: ``audit_log.parent_audit_id`` column added by migration ``0006``.
#: Defaulting to ``None`` is correct -- a top-level dispatch has no
#: parent.
parent_audit_id_var: ContextVar[uuid.UUID | None] = ContextVar(
    "parent_audit_id",
    default=None,
)


#: Prefix matching the FastAPI audit middleware's ``audit_*`` contextvar
#: convention (:data:`meho_backplane.audit._AUDIT_PAYLOAD_PREFIX`).
#: Connector handlers that need to enrich the dispatcher's audit row
#: bind e.g. ``structlog.contextvars.bind_contextvars(
#: audit_state_before=..., audit_state_after=...)`` before returning;
#: :func:`_build_audit_payload` strips the prefix and merges the
#: result into the payload, mirroring the chassis HTTP behaviour so
#: the typed-op dispatch path and the FastAPI route path produce the
#: same shape of audit row.
_AUDIT_PAYLOAD_PREFIX: str = "audit_"


def _resolve_audit_extras_from_contextvars() -> dict[str, Any]:
    """Build the ``audit_*``-contextvar extras dict for the audit payload.

    Reads every key in the current structlog contextvar context whose
    name starts with :data:`_AUDIT_PAYLOAD_PREFIX`, strips the prefix,
    and returns the result as a fresh dict. ``None`` values are
    dropped so a handler can ``bind_contextvars(audit_kind=None)`` to
    intentionally omit a key without writing ``"kind": null``.

    Mirrors :func:`meho_backplane.audit._resolve_audit_payload` (the
    FastAPI middleware's collector). Duplicated rather than imported
    because the dispatcher path and the chassis HTTP middleware path
    must remain decoupled in their imports — but the on-the-wire
    payload shape stays identical so audit consumers (G8.1 audit
    query, G8.2 audit replay) can treat both the same.
    """
    out: dict[str, Any] = {}
    for key, value in structlog.contextvars.get_contextvars().items():
        if not key.startswith(_AUDIT_PAYLOAD_PREFIX):
            continue
        if value is None:
            continue
        stripped = key[len(_AUDIT_PAYLOAD_PREFIX) :]
        if stripped:
            out[stripped] = value
    return out


def _build_audit_payload(
    descriptor: EndpointDescriptor,
    params_hash: str,
    result_status: str,
    *,
    redaction_policy_id: str | None = None,
) -> dict[str, Any]:
    """Compose the ``audit_log.payload`` dict for the dispatcher row.

    Default fields (descriptor, params_hash, result_status,
    parent_audit_id-mirror) are merged with any ``audit_*``
    contextvars bound by the connector handler — see
    :func:`_resolve_audit_extras_from_contextvars`. The
    parent_audit_id mirror in the payload remains for the v0.2
    broadcast-event surface (the broadcast schema is JSON-shaped and
    consumers parse the payload, not the audit row); the canonical
    linkage lives in the real ``audit_log.parent_audit_id`` column
    added by migration ``0006`` and written by
    :func:`write_audit_row`.

    *redaction_policy_id* is the connector-boundary redaction
    middleware's resolved policy id (G11.4-T2 #1071); the manifest
    itself lands in the dedicated ``redaction_manifest`` column
    (migration ``0030``), but mirroring the policy id into the
    JSON payload keeps the broadcast-event surface (which serialises
    ``payload``, not the dedicated columns) attribution-complete.
    """
    payload: dict[str, Any] = {
        "op_id": descriptor.op_id,
        "params_hash": params_hash,
        "source_kind": descriptor.source_kind,
        "connector_product": descriptor.product,
        "connector_version": descriptor.version,
        "connector_impl_id": descriptor.impl_id,
        "result_status": result_status,
    }
    parent_audit_id = parent_audit_id_var.get()
    if parent_audit_id is not None:
        payload["parent_audit_id"] = str(parent_audit_id)
    if redaction_policy_id is not None:
        payload["redaction_policy_id"] = redaction_policy_id
    # Handler-bound extras last so a handler can intentionally override
    # a default (e.g. a future per-op result_status override); the
    # default keys are documented + load-bearing for audit consumers,
    # so this layering is the explicit knob, not an accidental coupling.
    payload.update(_resolve_audit_extras_from_contextvars())
    return payload


def _resolve_target_id(target: Any) -> uuid.UUID | None:
    """Extract ``target.id`` when it's a real :class:`UUID`; else ``None``.

    The audit row column is nullable -- tenant-wide ops (no target)
    leave it NULL, and a duck-typed test target without a UUID-shaped
    id also lands as NULL rather than failing the insert.
    """
    raw = getattr(target, "id", None) if target is not None else None
    return raw if isinstance(raw, uuid.UUID) else None


async def write_audit_row(
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    params_hash: str,
    result_status: str,
    duration_ms: float,
    raw_payload: Any | None = None,
    redaction_manifest: list[dict[str, Any]] | None = None,
    redaction_policy_id: str | None = None,
) -> None:
    """Insert one ``audit_log`` row for this dispatch.

    Helper-owned session: opens, inserts, commits. Per the parent issue
    body, audit writes are synchronous from the dispatcher's perspective
    -- the :class:`OperationResult` returned by :func:`dispatch` is
    consistent with the row that landed. Audit failures bubble up to
    the caller (the dispatcher's surrounding ``try`` converts them into
    structured ``connector_error`` rather than crashing the request).

    The payload shape is documented in :func:`_build_audit_payload`.
    *raw_payload* / *redaction_manifest* / *redaction_policy_id* are
    the connector-boundary redaction middleware's three artefacts
    (G11.4-T2 #1071): the raw connector response, the engine's
    manifest, and the resolved policy id. Error-path rows (handler
    raised before producing a response) leave them ``None``; the
    columns are nullable per migration ``0030``.
    """
    sessionmaker = get_sessionmaker()
    payload = _build_audit_payload(
        descriptor,
        params_hash,
        result_status,
        redaction_policy_id=redaction_policy_id,
    )
    target_id = _resolve_target_id(target)
    parent_audit_id = parent_audit_id_var.get()
    async with sessionmaker() as session:
        row = AuditLog(
            id=audit_id,
            occurred_at=datetime.now(UTC),
            operator_sub=operator.sub,
            actor_sub=resolve_actor_sub(),
            tenant_id=operator.tenant_id,
            target_id=target_id,
            parent_audit_id=parent_audit_id,
            method="DISPATCH",
            path=descriptor.op_id,
            status_code=status_code_for_result(result_status),
            request_id=None,
            duration_ms=Decimal(str(round(duration_ms, 2))),
            payload=payload,
            raw_payload=raw_payload,
            redaction_manifest=redaction_manifest,
        )
        session.add(row)
        await session.commit()


async def publish_broadcast(
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    params: dict[str, Any],
    result_status: str,
) -> None:
    """Emit one :class:`BroadcastEvent` for the dispatch.

    Fail-open per the :func:`publish_event` contract -- a publish
    failure logs + bumps the error counter; the dispatcher's
    :class:`OperationResult` is independent.
    """
    op_class = classify_op(descriptor.op_id)
    payload = redact_payload(op_class, {"params": params}, result_status)
    raw_target_name = getattr(target, "name", None) if target is not None else None
    target_name = raw_target_name if isinstance(raw_target_name, str) else None
    event = BroadcastEvent(
        event_id=uuid.uuid4(),
        ts=datetime.now(UTC),
        tenant_id=operator.tenant_id,
        principal_sub=operator.sub,
        principal_name=operator.name,
        target_name=target_name,
        op_id=descriptor.op_id,
        op_class=op_class,
        result_status=result_status,
        audit_id=audit_id,
        payload=payload,
    )
    await publish_event(event)


async def audit_and_broadcast_safe(
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    params: dict[str, Any],
    params_hash: str,
    result_status: str,
    duration_ms: float,
    raw_payload: Any | None = None,
    redaction_manifest: list[dict[str, Any]] | None = None,
    redaction_policy_id: str | None = None,
) -> None:
    """Write the audit row + publish broadcast; swallow internal failures.

    Audit/broadcast failures are recorded at error level but do **not**
    flip the :class:`OperationResult` status -- the caller has already
    decided the outcome. Two reasons:

    * Audit-insert failures are rare and operationally distinct from
      "the operation succeeded but we couldn't record it". The on-call
      receives a ``dispatch_audit_failed`` log line; the operator sees
      the original outcome.
    * Broadcast failures are already fail-open by
      :func:`publish_event`'s contract.

    A future tightening of the audit-failure handling (e.g. failing the
    operation when audit cannot land) is a v0.2.next consideration and
    would land in this helper.

    *raw_payload* / *redaction_manifest* / *redaction_policy_id* are
    the connector-boundary redaction artefacts (G11.4-T2 #1071);
    forwarded to :func:`write_audit_row`. The broadcast event still
    consumes *params* (request-side) rather than the response-side
    raw payload: per :func:`publish_broadcast`'s
    :func:`~meho_backplane.broadcast.events.redact_payload` step, the
    broadcast surface ships params only -- the response goes nowhere
    near the broadcast subscribers, who already see redacted outcomes
    via the broadcast detail policy (G6.3).
    """
    try:
        await write_audit_row(
            audit_id=audit_id,
            operator=operator,
            descriptor=descriptor,
            target=target,
            params_hash=params_hash,
            result_status=result_status,
            duration_ms=duration_ms,
            raw_payload=raw_payload,
            redaction_manifest=redaction_manifest,
            redaction_policy_id=redaction_policy_id,
        )
    except Exception:
        _log.exception(
            "dispatch_audit_failed",
            op_id=descriptor.op_id,
            result_status=result_status,
            operator_sub=operator.sub,
        )
        # Skip the broadcast when audit failed -- the broadcast event
        # references the audit_id by FK contract, so a phantom event
        # would mislead subscribers about a row that doesn't exist.
        return
    try:
        await publish_broadcast(
            audit_id=audit_id,
            operator=operator,
            descriptor=descriptor,
            target=target,
            params=params,
            result_status=result_status,
        )
    except Exception:
        _log.exception(
            "dispatch_broadcast_failed",
            op_id=descriptor.op_id,
            result_status=result_status,
            operator_sub=operator.sub,
        )
