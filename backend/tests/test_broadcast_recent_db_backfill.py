# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for durable persistence + recent DB backfill (#2547).

Broadcast v2 Initiative #2543, Task #2547 (T2). Two contracts:

* **Durable persistence.** ``publish_agent_announcement`` writes an
  append-only ``agent_announcement`` row keyed on the event's minted UUID
  (``event_id``), persisting the typed claim fields (T1 #2544) so the
  announcement survives a Valkey restart / the stream's ``MAXLEN ~`` trim.

* **Recent archive backfill.** When the requested window reaches before
  the stream's oldest surviving entry (or the stream is empty -- the
  Valkey-restart / ``FLUSHALL`` shape), ``meho.broadcast.recent`` reads
  the announcements that lived in that gap back from the DB, deduped
  against the stream page by ``event_id`` and bounded by the page limit.

These are the always-on gates (mocked stream + the real SQLite engine the
suite pre-migrates to head). The Docker-gated ``FLUSHALL`` round-trip in
``test_mcp_tool_broadcast_announce.py`` exercises the same seam against a
real Valkey container.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    AgentAnnouncementEvent,
    get_broadcast_client,
    list_recent_events_strict,
    publish_agent_announcement,
    reset_broadcast_client_for_testing,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement, Tenant
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import wrap_untrusted_text

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_broadcast_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin a stub broadcast URL + clear the cached client around each test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    reset_broadcast_client_for_testing()
    get_settings.cache_clear()


async def _seed_tenant() -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    tid = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(Tenant(id=tid, slug=f"t-{tid.hex[:8]}", name=f"Tenant {tid.hex[:6]}"))
        await session.commit()
    return tid


def _operator(tenant_id: uuid.UUID, sub: str = "op-1") -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="x",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_row(
    *,
    tenant_id: uuid.UUID,
    created_at: datetime,
    activity: str = "rotating tokens",
    principal_sub: str = "op-1",
    targets: list[str] | None = None,
    work_ref: str | None = None,
    ttl_minutes: int | None = None,
    row_id: uuid.UUID | None = None,
) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    rid = row_id or uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            AgentAnnouncement(
                id=rid,
                tenant_id=tenant_id,
                principal_sub=principal_sub,
                activity=activity,
                phase="update",
                targets=targets or [],
                work_ref=work_ref,
                ttl_minutes=ttl_minutes,
                created_at=created_at,
            )
        )
        await session.commit()
    return rid


# ---------------------------------------------------------------------------
# Durable persistence on publish
# ---------------------------------------------------------------------------


async def test_publish_persists_durable_row_keyed_on_event_uuid() -> None:
    """The publisher writes one durable row keyed on the event's UUID."""
    tenant_id = await _seed_tenant()
    run_id = uuid.uuid4()
    event = AgentAnnouncementEvent(
        tenant_id=tenant_id,
        principal_sub="op-1",
        activity="rotating tokens on cluster X",
        target="prod-vc-1",
        targets=["cluster-x"],
        scope="token rotation",
        planned_op_class="write",
        ttl_minutes=30,
        work_ref="gh:evoila/meho#123",
        run_id=run_id,
        phase="start",
        ts=datetime.now(UTC),
    )
    bc = get_broadcast_client()
    with patch.object(bc, "xadd", new=AsyncMock(return_value="1747800000000-0")):
        returned_cursor = await publish_agent_announcement(event)

    # Return value stays the stream cursor; the durable id is the UUID.
    assert returned_cursor == "1747800000000-0"
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(AgentAnnouncement, event.event_id)
    assert row is not None, "publish must persist a durable agent_announcement row"
    assert row.id == event.event_id
    assert row.tenant_id == tenant_id
    assert row.principal_sub == "op-1"
    assert row.activity == "rotating tokens on cluster X"
    assert row.target == "prod-vc-1"
    assert row.targets == ["cluster-x"]
    assert row.scope == "token rotation"
    assert row.planned_op_class == "write"
    assert row.ttl_minutes == 30
    assert row.work_ref == "gh:evoila/meho#123"
    assert row.run_id == run_id
    assert row.phase == "start"


# ---------------------------------------------------------------------------
# Recent DB backfill
# ---------------------------------------------------------------------------


async def test_recent_backfills_from_db_when_stream_empty() -> None:
    """Empty stream (Valkey restart / FLUSHALL) -> recent returns the DB row.

    The core acceptance criterion: an announcement archived in the DB is
    still returned by a wide-window ``recent`` after the stream is wiped.
    """
    tenant_id = await _seed_tenant()
    op = _operator(tenant_id)
    rid = await _seed_row(
        tenant_id=tenant_id,
        created_at=datetime.now(UTC) - timedelta(minutes=5),
        targets=["cluster-x"],
    )
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])):
        result = await list_recent_events_strict(op, limit=100)

    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["event_id"] == str(rid)
    # A DB-sourced row carries no stream cursor.
    assert event["cursor"] is None
    assert event["event_kind"] == "agent_announcement"
    # Free-text stays enveloped on serve even from the archive.
    assert event["activity"] == wrap_untrusted_text("rotating tokens")


async def test_recent_backfill_respects_tenant_scope() -> None:
    """Backfill never leaks another tenant's archived announcements."""
    tenant_a = await _seed_tenant()
    tenant_b = await _seed_tenant()
    await _seed_row(tenant_id=tenant_b, created_at=datetime.now(UTC) - timedelta(minutes=5))
    op_a = _operator(tenant_a)
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])):
        result = await list_recent_events_strict(op_a, limit=100)
    assert result["events"] == []


async def test_recent_backfill_applies_work_ref_filter() -> None:
    """The work_ref filter narrows the archive backfill too."""
    tenant_id = await _seed_tenant()
    op = _operator(tenant_id)
    match = await _seed_row(
        tenant_id=tenant_id,
        created_at=datetime.now(UTC) - timedelta(minutes=5),
        work_ref="gh:evoila/meho#123",
    )
    await _seed_row(
        tenant_id=tenant_id,
        created_at=datetime.now(UTC) - timedelta(minutes=6),
        work_ref="gh:evoila/meho#999",
    )
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])):
        result = await list_recent_events_strict(op, limit=100, work_ref="gh:evoila/meho#123")
    assert [e["event_id"] for e in result["events"]] == [str(match)]


async def test_recent_backfill_active_only_excludes_expired_claim() -> None:
    """``active_only`` drops archived claims whose TTL has already elapsed."""
    tenant_id = await _seed_tenant()
    op = _operator(tenant_id)
    # Created 40 min ago with a 30-min TTL -> already expired.
    await _seed_row(
        tenant_id=tenant_id,
        created_at=datetime.now(UTC) - timedelta(minutes=40),
        ttl_minutes=30,
    )
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])):
        # Wide window so the expired claim is inside the look-back.
        active = await list_recent_events_strict(
            op,
            since=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            active_only=True,
            limit=100,
        )
        allrows = await list_recent_events_strict(
            op,
            since=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            limit=100,
        )
    assert active["events"] == []
    assert len(allrows["events"]) == 1


async def test_recent_backfill_short_circuits_on_op_class_filter() -> None:
    """An op_class filter can't match an announcement -> no backfill query."""
    tenant_id = await _seed_tenant()
    op = _operator(tenant_id)
    await _seed_row(tenant_id=tenant_id, created_at=datetime.now(UTC) - timedelta(minutes=5))
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])):
        result = await list_recent_events_strict(op, op_class="write", limit=100)
    assert result["events"] == []


async def test_recent_no_backfill_when_paginating_by_cursor() -> None:
    """A stream-cursor ``since`` is forward pagination -> archive not backfilled."""
    tenant_id = await _seed_tenant()
    op = _operator(tenant_id)
    await _seed_row(tenant_id=tenant_id, created_at=datetime.now(UTC) - timedelta(minutes=5))
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])):
        result = await list_recent_events_strict(op, since="1747800000000-0", limit=100)
    assert result["events"] == []


async def test_recent_dedups_backfill_against_stream_by_event_id() -> None:
    """A row on both the stream and the DB is not double-counted."""
    tenant_id = await _seed_tenant()
    op = _operator(tenant_id)
    shared_id = uuid.uuid4()
    created = datetime.now(UTC) - timedelta(minutes=10)
    await _seed_row(tenant_id=tenant_id, created_at=created, row_id=shared_id)
    # Same event on the stream: build the wire JSON the publisher would emit.
    event = AgentAnnouncementEvent(
        event_id=shared_id,
        tenant_id=tenant_id,
        principal_sub="op-1",
        activity="rotating tokens",
        phase="update",
        ts=created,
    )
    entry = ("1747800000000-0", {"event": event.model_dump_json()})
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[entry])):
        result = await list_recent_events_strict(
            op,
            since=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            limit=100,
        )
    ids = [e["event_id"] for e in result["events"]]
    assert ids == [str(shared_id)], "the shared announcement must appear exactly once"
