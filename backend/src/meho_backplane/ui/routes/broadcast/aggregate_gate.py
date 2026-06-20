# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Aggregate-only payload gate shared by the broadcast + audit drawers.

Decision #3 (Initiative #338) withholds the raw request payload of a
sensitive operation from any console surface that resolves the canonical
``audit_log`` row. The broadcast event drawer
(:mod:`meho_backplane.ui.routes.broadcast.event`) established the rule;
the audit-query row drawer (:mod:`meho_backplane.ui.routes.audit`)
reuses it verbatim. To keep the sensitive op-class set single-sourced
(both surfaces must agree byte-for-byte on what is redacted), the gate
lives here and both routers import it -- there is no second copy of the
``{credential_read, credential_mint, audit_query}`` set.

The gate has three parts:

* :func:`fetch_audit_row` -- tenant-scoped resolution of one
  ``audit_log`` row. A cross-tenant id resolves to ``None`` identically
  to a non-existent id, so the tenant boundary is opaque (never leaked
  as a 403-vs-404 distinction).
* :func:`resolve_op_id` -- recover the op id the publisher classified
  on, falling back to the ``http.{method}:{path}`` heuristic the audit
  middleware uses, so :func:`~meho_backplane.broadcast.classify_op`
  yields the same class the broadcast publisher computed.
* :func:`is_aggregate_only` -- the verdict: honour the G6.3 resolver's
  recorded ``payload["broadcast_detail_effective"]`` when present,
  otherwise fall back to op-class membership in
  :data:`AGGREGATE_ONLY_OP_CLASSES`.

:data:`INTERNAL_PAYLOAD_KEYS` is the set of audit-only payload keys a
drawer strips before rendering the *request* payload (they are
classification hints / G6.3 forensic metadata, not request params).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import AuditLog

__all__ = [
    "AGGREGATE_ONLY_OP_CLASSES",
    "INTERNAL_PAYLOAD_KEYS",
    "fetch_audit_row",
    "is_aggregate_only",
    "resolve_op_id",
]

#: Op classes whose detail is withheld from any console drawer per
#: decision #3 -- the same classes :func:`redact_payload` strips at
#: publish time. A drawer never renders the audit row's raw payload for
#: these; it shows the 🔒 aggregate-only placeholder instead. Kept in
#: sync with the redaction contract in
#: :mod:`meho_backplane.broadcast.events`.
AGGREGATE_ONLY_OP_CLASSES: frozenset[str] = frozenset(
    {"credential_read", "credential_mint", "audit_query"}
)

#: Audit-only payload keys a drawer hides from the rendered request
#: payload. ``op_id`` / ``op_class`` are the route-bound classification
#: hints surfaced as first-class drawer fields, not request params;
#: ``broadcast_detail_origin`` / ``broadcast_detail_effective`` are the
#: G6.3 resolver's internal forensic metadata (``tenant_rule:<uuid>``
#: origins are deliberately never shown). The drawer renders the
#: *request* payload, so these are stripped before the ``| tojson`` dump.
INTERNAL_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"op_id", "op_class", "broadcast_detail_origin", "broadcast_detail_effective"}
)


async def fetch_audit_row(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    audit_id: uuid.UUID,
) -> AuditLog | None:
    """Resolve ``(tenant_id, audit_id)`` to an ``audit_log`` row.

    Returns ``None`` when no row matches. A cross-tenant id surfaces
    identically -- the tenant boundary is opaque, mirroring the
    topology drawer's ``_fetch_node`` contract.
    """
    stmt = select(AuditLog).where(
        AuditLog.tenant_id == tenant_id,
        AuditLog.id == audit_id,
    )
    result = await db_session.execute(stmt)
    return result.scalar_one_or_none()


def resolve_op_id(row: AuditLog) -> str:
    """Recover the op id for the row's sensitivity classification.

    The audit middleware stamps the canonical op id into
    ``payload["op_id"]`` for connector-style routes. When absent
    (chassis HTTP routes, non-op requests) we fall back to the
    publisher's own heuristic ``http.{method.lower()}:{path}`` -- the
    exact string
    :func:`meho_backplane.audit._resolve_op_id_and_class_override`
    builds -- so :func:`classify_op` here yields the same class the
    broadcast publisher computed. The ``:`` separator deliberately
    avoids a route ending in ``.list`` being misread as a ``read`` verb
    suffix (the publisher relies on the same guard).
    """
    op_id = row.payload.get("op_id")
    if isinstance(op_id, str) and op_id:
        return op_id
    return f"http.{row.method.lower()}:{row.path}"


def is_aggregate_only(row: AuditLog, op_class: str) -> bool:
    """Decide whether the drawer withholds the payload (decision #3).

    Honours the G6.3 resolver's recorded verdict
    (``payload["broadcast_detail_effective"]``) when present so the
    drawer matches the detail the feed actually showed -- including a
    per-tenant override that flipped a sensitive op to full detail.
    Falls back to op-class membership in
    :data:`AGGREGATE_ONLY_OP_CLASSES` for rows predating G6.3 (or rows
    written when ``tenant_id`` was unresolved, which carry no effective
    key).
    """
    effective = row.payload.get("broadcast_detail_effective")
    if isinstance(effective, str) and effective:
        return effective == "aggregate"
    return op_class in AGGREGATE_ONLY_OP_CLASSES
