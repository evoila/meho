# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-PostgreSQL check that the claim lookup uses its index (#5005).

The caller's active-claim lookup (:func:`announce_claim_gap`) runs on every
write from an agent session. Migration ``0120`` added
``agent_announcement_tenant_principal_created_at_idx`` so the lookup reads
only the caller's own rows. Before it, the only fitting index was
``(tenant_id, created_at DESC)``, and on a busy tenant PostgreSQL walked
every announcement of the last day to find the caller's few rows.

The test fills one tenant with announcements from many principals, runs
``ANALYZE``, catches the exact ``SELECT`` the lookup sends, and runs
``EXPLAIN`` on it with the same parameters. SQLite (the unit-suite driver)
plans differently, so this check needs a real PostgreSQL.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, insert, text

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.announce_claim_lookup import announce_claim_gap
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement

# ``pg_engine`` (imported for its side effect of being a discoverable
# fixture) points the process engine at the testcontainer and seeds the two
# pinned tenant rows this suite scopes to.
from .conftest import pg_engine  # noqa: F401 -- pytest-discovered fixture

pytestmark = pytest.mark.asyncio

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ME = "agent-op"
_VC = "vc-lab-1"
_OP = "POST:/content/library/item"
_INDEX = "agent_announcement_tenant_principal_created_at_idx"
_PEERS = 40
_ROWS_PER_PEER = 150


def _operator() -> Operator:
    return Operator(
        sub=_ME,
        name=_ME,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _row(principal_sub: str, *, minutes_ago: float) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "tenant_id": _TENANT,
        "principal_sub": principal_sub,
        "activity": "working on the lab",
        "target": _VC,
        "targets": [],
        "phase": "start",
        "planned_op_class": "write",
        "ttl_minutes": 60,
        "created_at": datetime.now(UTC) - timedelta(minutes=minutes_ago),
    }


async def _seed_busy_tenant() -> None:
    """Many peers announce all day; the caller made one claim a minute ago."""
    rows = [
        _row(f"peer-{peer}", minutes_ago=i * 8)
        for peer in range(_PEERS)
        for i in range(_ROWS_PER_PEER)
    ]
    rows.append(_row(_ME, minutes_ago=1))
    async with get_sessionmaker()() as session:
        await session.execute(insert(AgentAnnouncement), rows)
        await session.commit()
    async with get_engine().connect() as conn:
        await conn.execute(text("ANALYZE agent_announcement"))
        await conn.commit()


async def _lookup_select() -> tuple[str, Any]:
    """Run the lookup once; return the ``SELECT`` it sent and its parameters."""
    sync_engine = get_engine().sync_engine
    sent: list[tuple[str, Any]] = []

    def _capture(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if "FROM agent_announcement" in statement:
            sent.append((statement, parameters))

    event.listen(sync_engine, "before_cursor_execute", _capture)
    try:
        assert await announce_claim_gap(_operator(), op_id=_OP, target_name=_VC) is None
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture)
    assert len(sent) == 1, sent
    return sent[0]


def _plan_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [node, *(n for child in node.get("Plans", []) for n in _plan_nodes(child))]


async def test_claim_lookup_reads_through_the_principal_index(
    pg_engine: None,  # noqa: F811 -- fixture
) -> None:
    await _seed_busy_tenant()
    statement, parameters = await _lookup_select()

    async with get_engine().connect() as conn:
        raw = (
            await conn.exec_driver_sql("EXPLAIN (FORMAT JSON) " + statement, parameters)
        ).scalar_one()
    plan = raw if isinstance(raw, list) else json.loads(raw)
    nodes = _plan_nodes(plan[0]["Plan"])

    assert _INDEX in {node.get("Index Name") for node in nodes}, json.dumps(plan, indent=1)
    assert not [n for n in nodes if n["Node Type"] == "Seq Scan"], json.dumps(plan, indent=1)
