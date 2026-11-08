# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Delete old search and call records (#4750).

The daily request usage retention job
(:func:`meho_backplane.request_usage.retention.prune_request_usage_once`)
calls :func:`delete_pick_records_older_than` with its own cutoff. So the
records are kept exactly as long as request usage events, and a stored
query text goes with its record.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationCallRecord, OperationSearchRecord

__all__ = ["delete_pick_records_older_than"]


async def delete_pick_records_older_than(cutoff: datetime) -> int:
    """Delete search and call records older than *cutoff*; return how many went."""
    dropped = 0
    async with get_sessionmaker()() as session:
        for model in (OperationCallRecord, OperationSearchRecord):
            result = await session.execute(delete(model).where(model.occurred_at < cutoff))
            dropped += result.rowcount or 0  # type: ignore[attr-defined]
        await session.commit()
    return dropped
