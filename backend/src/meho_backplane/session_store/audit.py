# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Audit rows of the session store.

Every upload, every read (list, show, download) and every delete writes
one ``audit_log`` row in the caller's name. A change writes its row in the
same transaction as the index change, so the change does not commit
without its record. A read commits its row before the answer is sent.

``payload["op_id"]`` names what happened (``sessions.upload``,
``sessions.list``, ``sessions.read``, ``sessions.download``,
``sessions.delete``, ``sessions.retention.set``, ``sessions.cleanup``).
The payload never holds file content; it holds the session id, counts and
hashes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog

__all__ = ["add_audit_row", "write_audit_row"]


def add_audit_row(
    session: AsyncSession,
    operator: Operator,
    *,
    op_id: str,
    method: str,
    path: str,
    status_code: int = 200,
    event_class: AuditEventClass = AuditEventClass.OPERATION_EXECUTION,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Add one audit row to *session*; the caller commits it."""
    audit_id = uuid.uuid4()
    session.add(
        AuditLog(
            id=audit_id,
            event_class=event_class,
            occurred_at=datetime.now(UTC),
            operator_sub=operator.sub,
            actor_sub=resolve_actor_sub(),
            tenant_id=operator.tenant_id,
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=Decimal("0"),
            payload={"op_id": op_id, **(payload or {})},
        )
    )
    return audit_id


async def write_audit_row(
    operator: Operator,
    *,
    op_id: str,
    method: str,
    path: str,
    status_code: int = 200,
    event_class: AuditEventClass = AuditEventClass.OPERATION_EXECUTION,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Commit one audit row on its own. Raises when the commit fails."""
    async with get_sessionmaker()() as session:
        audit_id = add_audit_row(
            session,
            operator,
            op_id=op_id,
            method=method,
            path=path,
            status_code=status_code,
            event_class=event_class,
            payload=payload,
        )
        await session.commit()
    return audit_id
