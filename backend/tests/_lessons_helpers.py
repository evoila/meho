# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Seed helpers for the knowledge upkeep and learning-number tests."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, LessonRetrieval, Tenant


async def seed_tenant(tenant_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex}", name=f"T {tenant_id.hex[-4:]}"))
        await session.commit()


async def seed_dispatch(
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID | None,
    at: datetime,
    op_id: str = "k8s.pod.list",
    target_id: uuid.UUID | None = None,
    status_code: int = 200,
) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AuditLog(
                id=uuid.uuid4(),
                occurred_at=at,
                operator_sub="op-1",
                tenant_id=tenant_id,
                agent_session_id=session_id,
                target_id=target_id,
                method="DISPATCH",
                path=op_id,
                status_code=status_code,
                request_id=None,
                duration_ms=Decimal("1.00"),
                payload={},
            )
        )
        await session.commit()


async def seed_retrieval(
    *,
    tenant_id: uuid.UUID,
    entry_id: uuid.UUID,
    session_id: uuid.UUID,
    at: datetime,
    store: str = "kb",
    status: str | None = None,
    expired: bool = False,
    entry_tenant_id: uuid.UUID | None = None,
) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            LessonRetrieval(
                tenant_id=tenant_id,
                store=store,
                entry_id=entry_id,
                entry_tenant_id=entry_tenant_id or tenant_id,
                agent_session_id=session_id,
                retrieved_at=at,
                status=status,
                expired=expired,
            )
        )
        await session.commit()
