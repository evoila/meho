# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared seeding helpers for request usage event tests (#3734)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RequestUsageEvent


async def seed_usage_event(
    *,
    tenant_id: uuid.UUID,
    route: str,
    occurred_at: datetime,
    principal_sub: str = "op-1",
    outcome: str = "ok",
    transport: str = "mcp",
    session_id: uuid.UUID | None = None,
) -> None:
    """Insert one ``request_finished`` row directly (reader-side tests)."""
    async with get_sessionmaker()() as session:
        session.add(
            RequestUsageEvent(
                id=uuid.uuid4(),
                kind="request_finished",
                occurred_at=occurred_at,
                tenant_id=tenant_id,
                principal_sub=principal_sub,
                session_id=session_id,
                transport=transport,
                route=route,
                outcome=outcome,
                duration_ms=Decimal("1.00"),
            ),
        )
        await session.commit()


async def read_usage_events() -> list[RequestUsageEvent]:
    """Return every stored usage event, oldest first."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(RequestUsageEvent).order_by(RequestUsageEvent.occurred_at),
        )
        return list(result.scalars().all())
