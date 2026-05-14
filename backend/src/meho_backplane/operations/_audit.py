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
#: dispatches a sub-op. T5 binds this before each composite handler call
#: (so a nested dispatch reads it from the contextvar); T7 (#398) will
#: ship the full audit-tree column on ``audit_log`` and the
#: bounded-recursion guard. Defaulting to ``None`` is correct -- a
#: top-level dispatch has no parent.
parent_audit_id_var: ContextVar[uuid.UUID | None] = ContextVar(
    "parent_audit_id",
    default=None,
)


def _build_audit_payload(
    descriptor: EndpointDescriptor,
    params_hash: str,
    result_status: str,
) -> dict[str, Any]:
    """Compose the ``audit_log.payload`` dict for the dispatcher row.

    Includes the optional ``parent_audit_id`` (from the contextvar)
    when a composite handler is mid-recursion -- T7 will promote it to
    a real column, but the payload-linkage shape lets composite
    sub-call linkage already work today.
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
) -> None:
    """Insert one ``audit_log`` row for this dispatch.

    Helper-owned session: opens, inserts, commits. Per the parent issue
    body, audit writes are synchronous from the dispatcher's perspective
    -- the :class:`OperationResult` returned by :func:`dispatch` is
    consistent with the row that landed. Audit failures bubble up to
    the caller (the dispatcher's surrounding ``try`` converts them into
    structured ``connector_error`` rather than crashing the request).

    The payload shape is documented in :func:`_build_audit_payload`.
    """
    sessionmaker = get_sessionmaker()
    payload = _build_audit_payload(descriptor, params_hash, result_status)
    target_id = _resolve_target_id(target)
    async with sessionmaker() as session:
        row = AuditLog(
            id=audit_id,
            occurred_at=datetime.now(UTC),
            operator_sub=operator.sub,
            tenant_id=operator.tenant_id,
            target_id=target_id,
            method="DISPATCH",
            path=descriptor.op_id,
            status_code=status_code_for_result(result_status),
            request_id=None,
            duration_ms=Decimal(str(round(duration_ms, 2))),
            payload=payload,
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
