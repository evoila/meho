# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The start warning for stored operations with a short Kubernetes path.

MEHO refuses an API file whose paths look like Kubernetes paths without the
usual start (:mod:`~meho_backplane.connectors.kubernetes.short_paths`).
Operations that an earlier ingest stored from such a file stay as they
are. Each of their calls gets the answer of the same call on the full path.
At start, MEHO writes one warning log line for each connector that has such
operations, so an operator can find them and their grants. The warning goes
only to the server log.
"""

from __future__ import annotations

from collections import Counter

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.connectors.kubernetes.short_paths import full_kubernetes_path
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor

__all__ = ["warn_stored_short_kubernetes_paths"]

_log = structlog.get_logger(__name__)


async def warn_stored_short_kubernetes_paths(
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """Log one warning per connector with stored short Kubernetes paths.

    Reads the op id of every stored ingested operation. Changes nothing.
    Returns how many warning lines it wrote. A failed read is logged as a
    warning and does not stop the start.
    """
    maker = sessionmaker if sessionmaker is not None else get_sessionmaker()
    stmt = select(
        EndpointDescriptor.tenant_id,
        EndpointDescriptor.impl_id,
        EndpointDescriptor.version,
        EndpointDescriptor.op_id,
    ).where(EndpointDescriptor.source_kind == "ingested")
    try:
        async with maker() as session:
            rows = (await session.execute(stmt)).all()
    except Exception as exc:
        _log.warning(
            "short_kubernetes_paths_not_checked",
            reason="MEHO could not read the stored ingested operations",
            error_type=type(exc).__name__,
        )
        return 0
    counts: Counter[tuple[str, str]] = Counter()
    for tenant_id, impl_id, version, op_id in rows:
        _, _, path = op_id.partition(":")
        if full_kubernetes_path(path) is not None:
            counts[(f"{impl_id}-{version}", str(tenant_id) if tenant_id else "built-in")] += 1
    for (connector_id, scope), count in sorted(counts.items()):
        _log.warning(
            "short_kubernetes_paths_stored",
            connector_id=connector_id,
            tenant_id=scope,
            count=count,
            reason=(
                "these operations came from an API file that MEHO now refuses: their "
                "paths look like Kubernetes paths without /api/v1 or "
                "/apis/<group>/<version>. Each of their calls now waits for a person "
                "where the same call on the full path waits"
            ),
        )
    return len(counts)
