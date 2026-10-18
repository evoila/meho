# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-PostgreSQL check that archive sequence numbers follow commit order (#3748).

The publisher takes the tenant's next sequence number with
``UPDATE agent_announcement_sequence ... RETURNING`` in the same
transaction as the announcement INSERT. The UPDATE locks the counter row
until that transaction ends. This suite holds one writer's transaction
open on purpose, right after it took its number, and checks:

* a second writer in the same tenant waits, and gets the next number
  only after the first one committed, so numbers follow commit order;
* while the first writer is held, readers see neither row, so a reader's
  high-water mark never skips a row that commits later;
* when the held writer rolls back, the waiting writer gets the number it
  gave back, so committed numbers have no holes;
* a writer in another tenant does not wait at all;
* readers that number rows an older pod wrote without ``seq`` (a rolling
  upgrade) take the same lock, so three readers and a publisher at once
  hand out each number exactly once.

SQLite (the unit-suite driver) has one writer at a time and no row
locks, so these checks need a real PostgreSQL.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import publisher
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.archive_cursor import encode_watch_checkpoint
from meho_backplane.broadcast.archive_reader import (
    AnnouncementFilter,
    read_announcement_history,
)
from meho_backplane.broadcast.archive_sequence import archive_watermark, next_announcement_seq
from meho_backplane.broadcast.publisher import publish_agent_announcement
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement
from tests.broadcast_admission_helpers import FakeStreamClient

# ``pg_engine`` points the process sessionmaker at the testcontainer and
# seeds the two pinned tenant rows this suite scopes to.
from .conftest import pg_engine  # noqa: F401 -- pytest-discovered fixture

pytestmark = pytest.mark.asyncio

#: The two tenants ``pg_engine`` seeds on entry.
_TENANT: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_TENANT: uuid.UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_WAIT_SECONDS = 10.0
_HOLD_SECONDS = 1.0


def _docker_socket_present() -> bool:
    """Docker usable when the unix socket (or ``DOCKER_HOST``) is present."""
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_skip_no_docker = pytest.mark.skipif(
    not _docker_socket_present(),
    reason="Docker socket unavailable in this sandbox; runs in CI where Postgres is provisioned.",
)


def _event(activity: str, tenant_id: uuid.UUID = _TENANT) -> AgentAnnouncementEvent:
    return AgentAnnouncementEvent.model_validate(
        {
            "tenant_id": tenant_id,
            "principal_sub": "svc-agent:triage",
            "activity": activity,
            "phase": "start",
            "ttl_minutes": 30,
            "ts": datetime.now(UTC),
            "claim_id": uuid.uuid4(),
            "owner_sub": "svc-agent:triage",
            "admission": "agent_principal",
        }
    )


@pytest.fixture(autouse=True)
def fake_stream(monkeypatch: pytest.MonkeyPatch) -> FakeStreamClient:
    """Fake the Valkey ``XADD``; the database is the real one."""
    client = FakeStreamClient()
    monkeypatch.setattr(publisher, "get_broadcast_client", lambda: client)
    return client


class _HoldAfterNumber:
    """Hold the first writer's transaction open right after it took its number.

    The counter row stays locked until the transaction ends. With
    ``fail=True`` the held writer raises after the release, so its
    transaction rolls back.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> None:
        self.taken = asyncio.Event()
        self.release = asyncio.Event()
        self.held_seq: int | None = None
        held = False

        async def gated(session: AsyncSession, tenant_id: uuid.UUID) -> int:
            nonlocal held
            seq = await next_announcement_seq(session, tenant_id)
            if not held:
                held = True
                self.held_seq = seq
                self.taken.set()
                await self.release.wait()
                if fail:
                    raise RuntimeError("held writer fails before commit")
            return seq

        monkeypatch.setattr(publisher, "next_announcement_seq", gated)


async def _seed_unnumbered(tenant_id: uuid.UUID, activities: list[str]) -> None:
    """Store rows the way a pod on the previous release does: without ``seq``."""
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        for offset, activity in enumerate(activities):
            event = _event(activity, tenant_id).model_copy(
                update={"ts": now - timedelta(minutes=len(activities) - offset)}
            )
            row = publisher._announcement_row(event, None, 0)
            row.seq = None
            session.add(row)
        await session.commit()


async def _rows(tenant_id: uuid.UUID) -> list[tuple[int, str]]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(AgentAnnouncement.seq, AgentAnnouncement.activity)
            .where(AgentAnnouncement.tenant_id == tenant_id)
            .order_by(AgentAnnouncement.seq)
        )
        return [(int(seq), str(activity)) for seq, activity in rows.all()]


def _reader(tenant_id: uuid.UUID) -> Operator:
    return Operator(sub="reader", raw_jwt="x", tenant_id=tenant_id, tenant_role=TenantRole.OPERATOR)


async def _recent(tenant_id: uuid.UUID) -> dict[str, Any]:
    return await read_announcement_history(
        _reader(tenant_id), cursor=None, flt=AnnouncementFilter(), limit=100
    )


@_skip_no_docker
async def test_held_writer_makes_the_next_one_wait_and_keeps_commit_order(
    pg_engine: None,  # noqa: F811 -- fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await publish_agent_announcement(_event("first"))
    hold = _HoldAfterNumber(monkeypatch)

    held_task = asyncio.create_task(publish_agent_announcement(_event("held")))
    await asyncio.wait_for(hold.taken.wait(), _WAIT_SECONDS)
    assert hold.held_seq == 2

    waiting_task = asyncio.create_task(publish_agent_announcement(_event("waiting")))
    done, _pending = await asyncio.wait({waiting_task}, timeout=_HOLD_SECONDS)
    assert not done, "a second writer took a number while the first one held the counter"

    # Readers see only what committed; the high-water mark does not move
    # past the held row, so no later commit can land below it.
    during = await _recent(_TENANT)
    assert len(during["events"]) == 1
    assert during["watch_checkpoint"] == encode_watch_checkpoint(_TENANT, 1)

    hold.release.set()
    held = await asyncio.wait_for(held_task, _WAIT_SECONDS)
    waiting = await asyncio.wait_for(waiting_task, _WAIT_SECONDS)

    assert held.seq == 2
    assert waiting.seq == 3
    assert [seq for seq, _activity in await _rows(_TENANT)] == [1, 2, 3]
    after = await _recent(_TENANT)
    assert after["watch_checkpoint"] == encode_watch_checkpoint(_TENANT, 3)


@_skip_no_docker
async def test_rolled_back_writer_gives_its_number_to_the_waiter(
    pg_engine: None,  # noqa: F811 -- fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hold = _HoldAfterNumber(monkeypatch, fail=True)

    failing_task = asyncio.create_task(publish_agent_announcement(_event("rolled back")))
    await asyncio.wait_for(hold.taken.wait(), _WAIT_SECONDS)
    assert hold.held_seq == 1

    waiting_task = asyncio.create_task(publish_agent_announcement(_event("waiting")))
    done, _pending = await asyncio.wait({waiting_task}, timeout=_HOLD_SECONDS)
    assert not done, "a second writer took a number while the first one held the counter"

    hold.release.set()
    with pytest.raises(RuntimeError, match="held writer fails"):
        await asyncio.wait_for(failing_task, _WAIT_SECONDS)
    waiting = await asyncio.wait_for(waiting_task, _WAIT_SECONDS)

    assert waiting.seq == 1
    assert await _rows(_TENANT) == [(1, "waiting")]


@_skip_no_docker
async def test_other_tenant_does_not_wait(
    pg_engine: None,  # noqa: F811 -- fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hold = _HoldAfterNumber(monkeypatch)
    held_task = asyncio.create_task(publish_agent_announcement(_event("held")))
    await asyncio.wait_for(hold.taken.wait(), _WAIT_SECONDS)

    other = await asyncio.wait_for(
        publish_agent_announcement(_event("other tenant", _OTHER_TENANT)), _HOLD_SECONDS * 5
    )
    assert other.seq == 1

    hold.release.set()
    held = await asyncio.wait_for(held_task, _WAIT_SECONDS)
    assert held.seq == 1


@_skip_no_docker
async def test_concurrent_readers_number_older_writes_exactly_once(
    pg_engine: None,  # noqa: F811 -- fixture
) -> None:
    await _seed_unnumbered(_TENANT, ["legacy a", "legacy b", "legacy c"])

    reads = [_recent(_TENANT) for _ in range(3)]
    results = await asyncio.gather(*reads, publish_agent_announcement(_event("new")))

    rows = await _rows(_TENANT)
    assert sorted(seq for seq, _activity in rows) == [1, 2, 3, 4]
    legacy = [seq for seq, activity in rows if activity.startswith("legacy")]
    assert legacy == sorted(legacy), "older rows keep their time order"
    assert legacy[-1] - legacy[0] == 2, "older rows got consecutive numbers"
    async with get_sessionmaker()() as session:
        assert await archive_watermark(session, _TENANT) == 4
    for read in results[:3]:
        assert read["gap"] is None
    after = await _recent(_TENANT)
    assert len(after["events"]) == 4
    assert after["watch_checkpoint"] == encode_watch_checkpoint(_TENANT, 4)
