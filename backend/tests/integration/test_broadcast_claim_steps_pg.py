# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-PostgreSQL check that steps on one announcement claim run in order (#3736).

``update`` and ``completion`` lock the claim's ``start`` row
(``SELECT ... FOR UPDATE``) inside the transaction that also inserts the
step. Two steps on one claim therefore run one after the other: an
``update`` that waits behind a ``completion`` re-reads after the
completion commits, sees its row, and gets ``claim_completed``. Without
the lock both could pass the open-claim check and the update could
commit after the completion.

The same lock orders a retry behind the first copy of its step: the retry
key is looked up after the lock is held, so two identical completions
give one row and one replay, not ``claim_completed``.

Delivery to the stream is locked per stored row: a retry that overlaps
the first try's stream write waits for it and then writes nothing, so
feed watchers never see the same step twice.

SQLite (the unit-suite driver) has no row locks and drops the clause, so
this suite boots a real PostgreSQL and holds one request's lock on
purpose while the other tries to pass.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.broadcast import publisher
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.announcement_claims import (
    CLAIM_COMPLETED,
    AnnouncementClaimError,
)
from meho_backplane.broadcast.publisher import publish_agent_announcement
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement

# ``pg_engine`` (imported for its side effect of being a discoverable
# fixture) points the process sessionmaker at the testcontainer and seeds
# the two pinned tenant rows this suite scopes to.
from .conftest import pg_engine  # noqa: F401 -- pytest-discovered fixture

pytestmark = pytest.mark.asyncio

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OWNER = "svc-agent:triage"
_WAIT_SECONDS = 10.0
_HOLD_SECONDS = 1.0


def _docker_socket_present() -> bool:
    """Docker usable when the unix socket (or ``DOCKER_HOST``) is present."""
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_skip_no_docker = pytest.mark.skipif(
    not _docker_socket_present(),
    reason="Docker socket unavailable in this sandbox; runs in CI where Postgres is provisioned.",
)


def _event(phase: str, claim_id: uuid.UUID) -> AgentAnnouncementEvent:
    return AgentAnnouncementEvent.model_validate(
        {
            "tenant_id": _TENANT,
            "principal_sub": _OWNER,
            "activity": f"{phase} step",
            "phase": phase,
            "ttl_minutes": 30,
            "ts": datetime.now(UTC),
            "claim_id": claim_id,
            "owner_sub": _OWNER,
            "admission": "agent_principal",
        }
    )


@pytest.fixture
def fake_stream(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Fake the Valkey ``XADD``; the database is the real one."""
    counter = itertools.count(1)
    xadd = AsyncMock(side_effect=lambda *_a, **_k: f"1700000000000-{next(counter)}")
    client = AsyncMock()
    client.xadd = xadd
    monkeypatch.setattr(publisher, "get_broadcast_client", lambda: client)
    return xadd


class _HoldAfterCheck:
    """Hold one phase's transaction open right after its open-claim check passed.

    The lock on the claim's start row is taken inside the check, so the
    held transaction keeps that lock until it is released here.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
        self.checked = asyncio.Event()
        self.release = asyncio.Event()
        original = publisher._check_open_claim

        async def gated(
            session: Any, event: AgentAnnouncementEvent, idempotency_key: str | None
        ) -> Any:
            stored = await original(session, event, idempotency_key)
            if event.phase == phase:
                self.checked.set()
                await self.release.wait()
            return stored

        monkeypatch.setattr(publisher, "_check_open_claim", gated)


class _HoldStreamWrite:
    """Hold one phase's first stream write open while its row lock is taken.

    ``_deliver`` locks the archive row before the ``XADD``, so the held
    write keeps that lock until it is released here.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        counter = itertools.count(1)
        held = False

        async def xadd(_key: str, fields: dict[str, str], **_kwargs: Any) -> str:
            nonlocal held
            if not held and json.loads(fields["event"])["phase"] == phase:
                held = True
                self.started.set()
                await self.release.wait()
            return f"1700000000000-{next(counter)}"

        self.xadd = AsyncMock(side_effect=xadd)
        client = AsyncMock()
        client.xadd = self.xadd
        monkeypatch.setattr(publisher, "get_broadcast_client", lambda: client)


async def _phases(claim_id: uuid.UUID) -> list[str]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(AgentAnnouncement.phase)
            .where(AgentAnnouncement.claim_id == claim_id)
            .order_by(AgentAnnouncement.created_at)
        )
        return [str(phase) for phase in rows.scalars().all()]


@_skip_no_docker
async def test_update_cannot_follow_a_completion(
    pg_engine: None,  # noqa: F811 -- fixture
    fake_stream: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An update that races a completion waits for it and is then refused."""
    claim_id = uuid.uuid4()
    await publish_agent_announcement(_event("start", claim_id))
    hold = _HoldAfterCheck(monkeypatch, "completion")

    completion_task = asyncio.create_task(
        publish_agent_announcement(_event("completion", claim_id), idempotency_key="c1")
    )
    await asyncio.wait_for(hold.checked.wait(), _WAIT_SECONDS)

    # The completion passed its check and holds the start row lock. The
    # update must now wait: it cannot pass its own check meanwhile.
    update_task = asyncio.create_task(
        publish_agent_announcement(_event("update", claim_id), idempotency_key="u1")
    )
    done, _pending = await asyncio.wait({update_task}, timeout=_HOLD_SECONDS)
    assert not done, "the update passed the open-claim check while a completion held the lock"

    hold.release.set()
    completion = await asyncio.wait_for(completion_task, _WAIT_SECONDS)
    assert completion.replayed is False

    with pytest.raises(AnnouncementClaimError) as excinfo:
        await asyncio.wait_for(update_task, _WAIT_SECONDS)
    assert excinfo.value.code == CLAIM_COMPLETED

    assert await _phases(claim_id) == ["start", "completion"]
    assert fake_stream.await_count == 2


@_skip_no_docker
async def test_completion_waits_for_an_update_in_flight(
    pg_engine: None,  # noqa: F811 -- fixture
    fake_stream: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock only orders the steps; a completion after an update still lands."""
    claim_id = uuid.uuid4()
    await publish_agent_announcement(_event("start", claim_id))
    hold = _HoldAfterCheck(monkeypatch, "update")

    update_task = asyncio.create_task(
        publish_agent_announcement(_event("update", claim_id), idempotency_key="u1")
    )
    await asyncio.wait_for(hold.checked.wait(), _WAIT_SECONDS)
    completion_task = asyncio.create_task(
        publish_agent_announcement(_event("completion", claim_id), idempotency_key="c1")
    )
    done, _pending = await asyncio.wait({completion_task}, timeout=_HOLD_SECONDS)
    assert not done, "the completion passed the open-claim check while an update held the lock"

    hold.release.set()
    update = await asyncio.wait_for(update_task, _WAIT_SECONDS)
    completion = await asyncio.wait_for(completion_task, _WAIT_SECONDS)
    assert update.replayed is False
    assert completion.replayed is False

    assert await _phases(claim_id) == ["start", "update", "completion"]
    assert fake_stream.await_count == 3


@_skip_no_docker
async def test_identical_completion_retry_replays_after_waiting(
    pg_engine: None,  # noqa: F811 -- fixture
    fake_stream: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry that waits behind the first copy of its step replays it."""
    claim_id = uuid.uuid4()
    await publish_agent_announcement(_event("start", claim_id))
    hold = _HoldAfterCheck(monkeypatch, "completion")

    first_task = asyncio.create_task(
        publish_agent_announcement(_event("completion", claim_id), idempotency_key="c1")
    )
    await asyncio.wait_for(hold.checked.wait(), _WAIT_SECONDS)

    # The first completion found no stored key and holds the start row
    # lock. The retry with the same key must wait for it.
    retry_task = asyncio.create_task(
        publish_agent_announcement(_event("completion", claim_id), idempotency_key="c1")
    )
    done, _pending = await asyncio.wait({retry_task}, timeout=_HOLD_SECONDS)
    assert not done, "the retry passed the open-claim check while the first copy held the lock"

    hold.release.set()
    first = await asyncio.wait_for(first_task, _WAIT_SECONDS)
    retry = await asyncio.wait_for(retry_task, _WAIT_SECONDS)

    assert first.replayed is False
    assert retry.replayed is True
    assert retry.event.event_id == first.event.event_id
    # Exactly one of the two wrote the stream entry.
    assert sum(result.cursor is not None for result in (first, retry)) == 1

    assert await _phases(claim_id) == ["start", "completion"]
    assert fake_stream.await_count == 2


@_skip_no_docker
async def test_overlapping_retries_deliver_a_stored_step_once(
    pg_engine: None,  # noqa: F811 -- fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry that overlaps the first stream write waits and writes nothing."""
    hold = _HoldStreamWrite(monkeypatch, "completion")
    claim_id = uuid.uuid4()
    await publish_agent_announcement(_event("start", claim_id))

    first_task = asyncio.create_task(
        publish_agent_announcement(_event("completion", claim_id), idempotency_key="c1")
    )
    await asyncio.wait_for(hold.started.wait(), _WAIT_SECONDS)

    # The row is stored without a stream entry id, and the first stream
    # write is in flight with the row lock held. The retry finds the row
    # and must wait for the lock before it may deliver.
    retry_task = asyncio.create_task(
        publish_agent_announcement(_event("completion", claim_id), idempotency_key="c1")
    )
    done, _pending = await asyncio.wait({retry_task}, timeout=_HOLD_SECONDS)
    assert not done, "the retry delivered while the first stream write held the row lock"

    hold.release.set()
    first = await asyncio.wait_for(first_task, _WAIT_SECONDS)
    retry = await asyncio.wait_for(retry_task, _WAIT_SECONDS)

    assert first.replayed is False
    assert first.cursor == "1700000000000-2"
    assert retry.replayed is True
    assert retry.cursor is None
    assert retry.event.event_id == first.event.event_id

    assert await _phases(claim_id) == ["start", "completion"]
    assert hold.xadd.await_count == 2
