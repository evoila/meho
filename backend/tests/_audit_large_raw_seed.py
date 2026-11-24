# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Audit rows with a large stored raw answer, for the list-read tests (#6818).

:func:`seed` writes one target and three operation rows on it: the newest
row holds a raw answer of about 5 MB, one row a small raw answer, and one
row none. :func:`full_rows` loads the same rows with every column, the way
the list reads loaded them before they stopped loading ``raw_payload``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog, Target, Tenant

#: About 5 MB of raw answer: 5,000 items of about 1 KB each.
LARGE_RAW: dict[str, Any] = {
    "items": [{"name": f"item-{i}", "blob": "x" * 1000} for i in range(5000)],
}

TARGET_NAME = "big-answer-target"
TENANT_NAME = "Big Answer Tenant"
OPERATOR_SUB = "big-answer-operator"


@dataclass(frozen=True)
class Seeded:
    tenant_id: uuid.UUID
    target_id: uuid.UUID
    #: Oldest first. The last row holds :data:`LARGE_RAW`.
    ids: list[uuid.UUID]


async def seed(tenant_id: uuid.UUID, *, add_tenant: bool = True) -> Seeded:
    """Write the target and the three rows in *tenant_id*."""
    target_id = uuid.uuid4()
    base = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
    raws: list[Any] = [None, {"ok": True}, LARGE_RAW]
    ids: list[uuid.UUID] = []
    async with get_sessionmaker()() as s:
        if add_tenant:
            s.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name=TENANT_NAME))
        s.add(
            Target(
                id=target_id,
                tenant_id=tenant_id,
                name=TARGET_NAME,
                aliases=[],
                product="vsphere",
                host=f"{TARGET_NAME}.example.com",
                auth_model="shared_service_account",
                extras={},
            ),
        )
        for i, raw in enumerate(raws):
            row_id = uuid.uuid4()
            ids.append(row_id)
            s.add(
                AuditLog(
                    id=row_id,
                    occurred_at=base + timedelta(seconds=i),
                    operator_sub=OPERATOR_SUB,
                    tenant_id=tenant_id,
                    method="POST",
                    path=f"vsphere.vm.list.{i}",
                    status_code=200 if i else 502,
                    duration_ms=Decimal("1.5"),
                    payload={"op_id": f"vsphere.vm.list.{i}", "op_class": "read"},
                    raw_payload=raw,
                    target_id=target_id,
                    event_class=AuditEventClass.OPERATION_EXECUTION.value,
                ),
            )
        await s.commit()
    return Seeded(tenant_id, target_id, ids)


async def full_rows(ids: list[uuid.UUID]) -> dict[uuid.UUID, AuditLog]:
    """The rows *ids* with every column loaded, ``raw_payload`` too."""
    async with get_sessionmaker()() as s:
        rows = (await s.scalars(sa.select(AuditLog).where(AuditLog.id.in_(ids)))).all()
        s.expunge_all()
    return {row.id: row for row in rows}
