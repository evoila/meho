# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Archive-authoritative announcements with commit-order cursors (#3748).

Unit coverage against the real SQLite test database; only Valkey is
faked. The PostgreSQL lock behaviour (a held writer blocks the next one;
a rollback gives its number back to the waiter) is in
``tests/integration/test_broadcast_archive_cursor_pg.py``.

* The per-tenant sequence counts up by one per committed row, per tenant,
  and a rolled-back write gives its number back.
* Cursors are opaque, versioned and tenant-bound.
* ``recent`` returns a history page cursor and a separate watch
  checkpoint, also on an empty archive and at the tail.
* A scan whose rows the filter hides still moves the checkpoint; a
  reconnect with the returned checkpoint never repeats an event id.
* A stream failure after the archive commit returns ``delivery_pending``;
  the retry with the same key finishes the delivery without a second row.
* Rows removed by retention give an explicit ``gap``; a checkpoint ahead
  of the archive gives a ``checkpoint_ahead`` reset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import delete, func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import archive_reader, publisher
from meho_backplane.broadcast.archive_cursor import (
    HistoryPosition,
    InvalidArchiveCursorError,
    decode_history_cursor,
    decode_watch_checkpoint,
    encode_history_cursor,
    encode_watch_checkpoint,
)
from meho_backplane.broadcast.archive_reader import (
    AnnouncementFilter,
    read_announcement_history,
    watch_announcements,
)
from meho_backplane.broadcast.archive_sequence import archive_watermark, next_announcement_seq
from meho_backplane.broadcast.publisher import publish_agent_announcement
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement
from tests.broadcast_admission_helpers import FakeStreamClient, seed_announcement, trusted_event
from tests.mcp_test_fixtures import (
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_ALL = AnnouncementFilter()


@pytest.fixture(autouse=True)
def _fake_valkey(monkeypatch: pytest.MonkeyPatch) -> FakeStreamClient:
    """Fake the stream for the publisher and the watch wake-up (a quiet stream)."""
    client = FakeStreamClient()
    monkeypatch.setattr(publisher, "get_broadcast_client", lambda: client)
    wake = AsyncMock()
    wake.xrevrange = AsyncMock(return_value=[])
    wake.xread = AsyncMock(return_value=None)
    monkeypatch.setattr(archive_reader, "get_broadcast_client", lambda: wake)
    monkeypatch.setattr(archive_reader, "get_broadcast_blocking_client", lambda: wake)
    return client


def _reader(tenant_id: UUID) -> Operator:
    return Operator(
        sub="reader-1",
        raw_jwt="x",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _ids(result: dict[str, Any]) -> list[str]:
    return [event["event_id"] for event in result["events"]]


async def _seqs(tenant_id: UUID) -> list[int]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(AgentAnnouncement.seq)
            .where(AgentAnnouncement.tenant_id == tenant_id)
            .order_by(AgentAnnouncement.seq)
        )
        return [int(seq) for seq in rows.scalars().all()]


async def _prune(tenant_id: UUID, *, up_to_seq: int) -> None:
    """Delete rows like the retention prune does (the counter row stays)."""
    async with get_sessionmaker()() as session:
        await session.execute(
            delete(AgentAnnouncement).where(
                AgentAnnouncement.tenant_id == tenant_id, AgentAnnouncement.seq <= up_to_seq
            )
        )
        await session.commit()


async def _watch(tenant_id: UUID, checkpoint: str, **flt: Any) -> dict[str, Any]:
    return await watch_announcements(
        _reader(tenant_id),
        checkpoint=checkpoint,
        flt=AnnouncementFilter(**flt),
        timeout_ms=100,
    )


# ---------------------------------------------------------------------------
# Sequence: commit order, per tenant, no holes after a rollback
# ---------------------------------------------------------------------------


async def test_sequence_counts_per_tenant_in_commit_order() -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    for tenant_id in (tenant_a, tenant_b, tenant_a, tenant_a, tenant_b):
        await publish_agent_announcement(trusted_event(tenant_id=tenant_id))
    assert await _seqs(tenant_a) == [1, 2, 3]
    assert await _seqs(tenant_b) == [1, 2]


async def test_rolled_back_write_gives_its_number_back() -> None:
    tenant_id = uuid4()
    await publish_agent_announcement(trusted_event(tenant_id=tenant_id))
    async with get_sessionmaker()() as session:
        assert await next_announcement_seq(session, tenant_id) == 2
        await session.rollback()
    result = await publish_agent_announcement(trusted_event(tenant_id=tenant_id))
    assert result.seq == 2
    assert await _seqs(tenant_id) == [1, 2]
    async with get_sessionmaker()() as session:
        assert await archive_watermark(session, tenant_id) == 2


async def test_lost_start_race_gives_its_number_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The IntegrityError path rolls the counter back with the insert."""
    tenant_id = uuid4()
    winner = trusted_event(tenant_id=tenant_id, activity="same start")
    await publish_agent_announcement(winner, idempotency_key="s1")
    # Make the retry miss the key lookup once, so it reaches the INSERT and loses.
    original = publisher._find_by_idempotency_key
    calls = 0

    async def _miss_first(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return None if calls == 1 else await original(*args, **kwargs)

    monkeypatch.setattr(publisher, "_find_by_idempotency_key", _miss_first)
    retry = trusted_event(tenant_id=tenant_id, activity="same start", owner_sub=winner.owner_sub)
    replay = await publish_agent_announcement(retry, idempotency_key="s1")
    assert replay.replayed is True
    assert replay.seq == 1
    later = await publish_agent_announcement(trusted_event(tenant_id=tenant_id))
    assert later.seq == 2


# ---------------------------------------------------------------------------
# Cursor codec
# ---------------------------------------------------------------------------


def test_cursors_round_trip_and_are_tenant_bound() -> None:
    tenant_id, other = uuid4(), uuid4()
    checkpoint = encode_watch_checkpoint(tenant_id, 42)
    assert checkpoint.startswith("w1.")
    assert len(checkpoint) <= 64
    assert decode_watch_checkpoint(checkpoint, tenant_id) == 42
    since = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    page = encode_history_cursor(tenant_id, HistoryPosition(watermark=9, after_seq=3, since=since))
    assert page.startswith("h1.")
    assert len(page) <= 64
    assert decode_history_cursor(page, tenant_id) == HistoryPosition(9, 3, since)
    with pytest.raises(InvalidArchiveCursorError, match="another tenant"):
        decode_watch_checkpoint(checkpoint, other)
    with pytest.raises(InvalidArchiveCursorError, match="another tenant"):
        decode_history_cursor(page, other)


@pytest.mark.parametrize("raw", ["", "w1.", "w1.!!", "w2.AAAA", "1747800000000-0", "h1.AAAA"])
def test_malformed_or_wrong_kind_checkpoints_are_refused(raw: str) -> None:
    with pytest.raises(InvalidArchiveCursorError):
        decode_watch_checkpoint(raw, uuid4())


# ---------------------------------------------------------------------------
# recent: separate history page cursor and watch checkpoint
# ---------------------------------------------------------------------------


async def test_empty_archive_returns_both_cursor_kinds() -> None:
    tenant_id = uuid4()
    result = await read_announcement_history(_reader(tenant_id), cursor=None, flt=_ALL, limit=10)
    assert result == {
        "events": [],
        "next_cursor": None,
        "watch_checkpoint": encode_watch_checkpoint(tenant_id, 0),
        "gap": None,
    }


async def test_tail_read_ends_history_and_hands_over_to_watch() -> None:
    tenant_id = uuid4()
    for _ in range(3):
        await seed_announcement(tenant_id)
    op = _reader(tenant_id)

    page = await read_announcement_history(op, cursor=None, flt=_ALL, limit=2)
    assert page["next_cursor"] is not None
    assert page["next_cursor"] != page["watch_checkpoint"]
    tail = await read_announcement_history(op, cursor=page["next_cursor"], flt=_ALL, limit=2)
    assert tail["next_cursor"] is None
    assert tail["watch_checkpoint"] == page["watch_checkpoint"]
    assert decode_watch_checkpoint(tail["watch_checkpoint"], tenant_id) == 3

    later, _ = await seed_announcement(tenant_id)
    watched = await _watch(tenant_id, tail["watch_checkpoint"])
    assert _ids(watched) == [str(later.event_id)]


async def test_filtered_empty_recent_still_moves_the_checkpoint() -> None:
    tenant_id = uuid4()
    for _ in range(2):
        await seed_announcement(tenant_id, principal_sub="op-bob")
    result = await read_announcement_history(
        _reader(tenant_id), cursor=None, flt=AnnouncementFilter(principal="op-alice"), limit=10
    )
    assert result["events"] == []
    assert decode_watch_checkpoint(result["watch_checkpoint"], tenant_id) == 2


# ---------------------------------------------------------------------------
# watch: filtered-empty scans, reconnects and wake failures
# ---------------------------------------------------------------------------


async def test_filtered_empty_watch_moves_the_checkpoint() -> None:
    tenant_id = uuid4()
    for _ in range(3):
        await seed_announcement(tenant_id, principal_sub="op-bob")
    result = await _watch(tenant_id, encode_watch_checkpoint(tenant_id, 0), principal="op-alice")
    assert result["events"] == []
    assert decode_watch_checkpoint(result["next_cursor"], tenant_id) == 3


async def test_reconnect_with_the_returned_checkpoint_never_repeats_an_event() -> None:
    tenant_id = uuid4()
    first_batch = [str((await seed_announcement(tenant_id))[0].event_id) for _ in range(2)]
    start = encode_watch_checkpoint(tenant_id, 0)
    first = await _watch(tenant_id, start)
    assert _ids(first) == first_batch

    second_batch = [str((await seed_announcement(tenant_id))[0].event_id) for _ in range(2)]
    second = await _watch(tenant_id, first["next_cursor"])
    assert _ids(second) == second_batch
    again = await _watch(tenant_id, second["next_cursor"])
    assert again["events"] == []
    assert again["next_cursor"] == second["next_cursor"]


async def test_full_scan_page_returns_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive_reader, "WATCH_SCAN_LIMIT", 2)
    tenant_id = uuid4()
    seeded = [str((await seed_announcement(tenant_id))[0].event_id) for _ in range(3)]
    first = await _watch(tenant_id, encode_watch_checkpoint(tenant_id, 0))
    second = await _watch(tenant_id, first["next_cursor"])
    assert _ids(first) + _ids(second) == seeded


# ---------------------------------------------------------------------------
# Publisher: archive committed, stream failed
# ---------------------------------------------------------------------------


async def test_stream_failure_returns_delivery_pending_and_retry_is_idempotent(
    _fake_valkey: FakeStreamClient,
) -> None:
    tenant_id = uuid4()
    outcomes: list[Any] = [RedisConnectionError("valkey down"), "1700000000000-9"]

    async def _xadd(*_args: Any, **_kwargs: Any) -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return str(outcome)

    _fake_valkey.xadd.side_effect = _xadd
    event = trusted_event(tenant_id=tenant_id)

    first = await publish_agent_announcement(event, idempotency_key="k1")
    assert first.delivery_pending is True
    assert first.replayed is False
    assert first.seq == 1
    # Readers already see it: the archive is the record.
    recent = await read_announcement_history(_reader(tenant_id), cursor=None, flt=_ALL, limit=10)
    assert _ids(recent) == [str(event.event_id)]

    retry = await publish_agent_announcement(event, idempotency_key="k1")
    assert retry.delivery_pending is False
    assert retry.replayed is True
    assert retry.seq == 1
    assert retry.stream_entry_id == "1700000000000-9"
    assert await _seqs(tenant_id) == [1]
    assert _fake_valkey.xadd.await_count == 2

    again = await publish_agent_announcement(event, idempotency_key="k1")
    assert again.replayed is True
    assert again.stream_entry_id is None
    assert _fake_valkey.xadd.await_count == 2


# ---------------------------------------------------------------------------
# Retention gaps and resets
# ---------------------------------------------------------------------------


async def test_watch_reports_rows_removed_by_retention() -> None:
    tenant_id = uuid4()
    seeded = [str((await seed_announcement(tenant_id))[0].event_id) for _ in range(5)]
    await _prune(tenant_id, up_to_seq=3)

    result = await _watch(tenant_id, encode_watch_checkpoint(tenant_id, 1))

    assert result["gap"]["reason"] == "retention_trimmed"
    assert result["gap"]["missed_events"] == 2
    assert _ids(result) == seeded[3:]
    assert decode_watch_checkpoint(result["next_cursor"], tenant_id) == 5


async def test_watch_reports_a_fully_pruned_archive() -> None:
    tenant_id = uuid4()
    for _ in range(4):
        await seed_announcement(tenant_id)
    await _prune(tenant_id, up_to_seq=4)
    result = await _watch(tenant_id, encode_watch_checkpoint(tenant_id, 1))
    assert result["gap"]["missed_events"] == 3
    assert result["events"] == []
    assert decode_watch_checkpoint(result["next_cursor"], tenant_id) == 4


async def test_history_page_reports_rows_pruned_between_pages() -> None:
    tenant_id = uuid4()
    for _ in range(5):
        await seed_announcement(tenant_id)
    op = _reader(tenant_id)
    page = await read_announcement_history(op, cursor=None, flt=_ALL, limit=1)
    await _prune(tenant_id, up_to_seq=3)
    rest = await read_announcement_history(op, cursor=page["next_cursor"], flt=_ALL, limit=10)
    assert rest["gap"]["missed_events"] == 2
    assert len(rest["events"]) == 2


async def test_window_older_than_retention_is_not_called_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    for _ in range(3):
        await seed_announcement(tenant_id)
    await _prune(tenant_id, up_to_seq=1)
    since = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    result = await read_announcement_history(_reader(tenant_id), cursor=since, flt=_ALL, limit=10)
    assert result["gap"]["reason"] == "retention_trimmed"
    assert result["gap"]["missed_events"] is None
    assert len(result["events"]) == 2

    # Inside the retention window nothing could have been pruned.
    recent = await read_announcement_history(_reader(tenant_id), cursor=None, flt=_ALL, limit=10)
    assert recent["gap"] is None


async def test_checkpoint_ahead_of_the_archive_resets() -> None:
    tenant_id = uuid4()
    await seed_announcement(tenant_id)
    result = await _watch(tenant_id, encode_watch_checkpoint(tenant_id, 99))
    assert result["gap"]["reason"] == "checkpoint_ahead"
    assert decode_watch_checkpoint(result["next_cursor"], tenant_id) == 1


async def test_rows_without_a_number_are_skipped() -> None:
    """A row an older pod wrote without ``seq`` has no place in the order."""
    tenant_id = uuid4()
    numbered, _ = await seed_announcement(tenant_id)
    legacy = publisher._announcement_row(trusted_event(tenant_id=tenant_id), None, 0)
    legacy.seq = None
    async with get_sessionmaker()() as session:
        session.add(legacy)
        await session.commit()
        total = (
            await session.execute(
                select(func.count()).where(AgentAnnouncement.tenant_id == tenant_id)
            )
        ).scalar_one()
    assert total == 2
    result = await read_announcement_history(_reader(tenant_id), cursor=None, flt=_ALL, limit=10)
    assert _ids(result) == [str(numbered.event_id)]
