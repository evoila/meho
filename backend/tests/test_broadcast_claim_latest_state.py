# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Claim state comes from the newest committed step (#3762).

* ``active_only`` reads drop every step of a claim that has ended, also a
  start whose own TTL has not run out;
* the target advisory and the caller's claim lookup pick the newest step
  by commit order, also when the timestamps say something else.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import client as broadcast_client
from meho_backplane.broadcast.announce_claim_lookup import announce_claim_gap
from meho_backplane.broadcast.archive_cursor import encode_watch_checkpoint
from meho_backplane.broadcast.archive_reader import (
    AnnouncementFilter,
    latest_announcements,
    read_announcement_history,
    watch_announcements,
)
from meho_backplane.broadcast.publisher import _announcement_row
from meho_backplane.broadcast.target_advisory import (
    ADVISORY_EXTRAS_KEY,
    build_target_activity_advisory,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.settings import get_settings

from .broadcast_admission_helpers import seed_announcement, trusted_event

_TARGET = "cluster-x"
_PEER = "user-a"
_ME = "user-b"
_ACTIVE_ONLY = AnnouncementFilter(active_only=True)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("DISPATCH_ACTIVITY_ADVISORY_WINDOW_MINUTES", "30")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_stream() -> Iterator[None]:
    """The stream is only a wake-up: a failing one makes a watch poll the archive."""
    stand_in = MagicMock(xrevrange=AsyncMock(side_effect=RuntimeError("no stream")))
    with patch.object(broadcast_client, "_CLIENT", new=stand_in):
        yield


@pytest.fixture
def tenant_id() -> UUID:
    return uuid4()


def _operator(tenant_id: UUID, sub: str = _ME) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _step(
    tenant_id: UUID,
    claim_id: UUID,
    phase: str,
    *,
    minutes_ago: float = 1,
    principal_sub: str = _PEER,
    ttl_minutes: int | None = 60,
) -> str:
    """Store one step of *claim_id* and return its event id."""
    fields: dict[str, Any] = {
        "principal_sub": principal_sub,
        "claim_id": claim_id,
        "phase": phase,
        "target": _TARGET,
        "ttl_minutes": ttl_minutes,
        "planned_op_class": "write",
        "ts": datetime.now(UTC) - timedelta(minutes=minutes_ago),
    }
    event, _ = await seed_announcement(tenant_id, **fields)
    return str(event.event_id)


async def _recent_ids(tenant_id: UUID) -> list[str]:
    answer = await read_announcement_history(
        _operator(tenant_id),
        cursor=(datetime.now(UTC) - timedelta(hours=3)).isoformat(),
        flt=_ACTIVE_ONLY,
        limit=100,
    )
    return [event["event_id"] for event in answer["events"]]


async def _watch_ids(tenant_id: UUID) -> list[str]:
    answer = await watch_announcements(
        _operator(tenant_id),
        checkpoint=encode_watch_checkpoint(tenant_id, 0),
        flt=_ACTIVE_ONLY,
        timeout_ms=100,
    )
    return [event["event_id"] for event in answer["events"]]


async def _latest_ids(tenant_id: UUID) -> list[str]:
    found = await latest_announcements(_operator(tenant_id), limit=100, flt=_ACTIVE_ONLY)
    return [str(item.event.event_id) for item in found.events]


_ACTIVE_READS = [_recent_ids, _watch_ids, _latest_ids]


@pytest.mark.parametrize("read", _ACTIVE_READS)
async def test_active_only_drops_start_of_a_completed_claim(read: Any, tenant_id: UUID) -> None:
    claim_id = uuid4()
    await _step(tenant_id, claim_id, "start", minutes_ago=3)
    await _step(tenant_id, claim_id, "update", minutes_ago=2)
    await _step(tenant_id, claim_id, "completion", ttl_minutes=None)

    assert await read(tenant_id) == []


@pytest.mark.parametrize("read", _ACTIVE_READS)
async def test_active_only_keeps_all_steps_of_a_running_claim(read: Any, tenant_id: UUID) -> None:
    claim_id = uuid4()
    start = await _step(tenant_id, claim_id, "start", minutes_ago=3)
    update = await _step(tenant_id, claim_id, "update", minutes_ago=2)

    assert await read(tenant_id) == [start, update]


@pytest.mark.parametrize("read", _ACTIVE_READS)
async def test_active_only_drops_claim_whose_newest_ttl_ran_out(read: Any, tenant_id: UUID) -> None:
    claim_id = uuid4()
    await _step(tenant_id, claim_id, "start", minutes_ago=30, ttl_minutes=120)
    await _step(tenant_id, claim_id, "update", minutes_ago=20, ttl_minutes=5)

    assert await read(tenant_id) == []


@pytest.mark.parametrize("read", _ACTIVE_READS)
async def test_completion_only_ends_its_own_claim(read: Any, tenant_id: UUID) -> None:
    other = await _step(tenant_id, uuid4(), "start", minutes_ago=4)
    done = uuid4()
    await _step(tenant_id, done, "start", minutes_ago=3)
    await _step(tenant_id, done, "completion", ttl_minutes=None)

    assert await read(tenant_id) == [other]


async def test_completion_in_another_tenant_does_not_end_the_claim(tenant_id: UUID) -> None:
    claim_id = uuid4()
    start = await _step(tenant_id, claim_id, "start", minutes_ago=3)
    await _step(uuid4(), claim_id, "completion", ttl_minutes=None)

    assert await _recent_ids(tenant_id) == [start]


async def test_completion_after_the_page_still_ends_the_claim(tenant_id: UUID) -> None:
    claim_id = uuid4()
    await _step(tenant_id, claim_id, "start", minutes_ago=3)
    other = await _step(tenant_id, uuid4(), "start", minutes_ago=2)
    await _step(tenant_id, claim_id, "completion", ttl_minutes=None)

    first_page = await read_announcement_history(
        _operator(tenant_id),
        cursor=(datetime.now(UTC) - timedelta(hours=3)).isoformat(),
        flt=_ACTIVE_ONLY,
        limit=2,
    )

    assert first_page["next_cursor"] is not None
    assert [event["event_id"] for event in first_page["events"]] == [other]


async def test_filter_off_still_returns_every_step(tenant_id: UUID) -> None:
    claim_id = uuid4()
    start = await _step(tenant_id, claim_id, "start", minutes_ago=3)
    completion = await _step(tenant_id, claim_id, "completion", ttl_minutes=None)

    found = await latest_announcements(_operator(tenant_id), limit=100)

    assert [str(item.event.event_id) for item in found.events] == [start, completion]


async def test_advisory_trusts_commit_order_over_a_newer_timestamp(tenant_id: UUID) -> None:
    claim_id = uuid4()
    await _step(tenant_id, claim_id, "update", minutes_ago=1)
    await _step(tenant_id, claim_id, "completion", minutes_ago=5, ttl_minutes=None)

    assert (
        await build_target_activity_advisory(
            _operator(tenant_id), op_class="write", target_name=_TARGET
        )
        == {}
    )


async def test_advisory_reports_the_step_committed_last(tenant_id: UUID) -> None:
    claim_id = uuid4()
    await _step(tenant_id, claim_id, "start", minutes_ago=1)
    await _step(tenant_id, claim_id, "update", minutes_ago=5)

    advisory = await build_target_activity_advisory(
        _operator(tenant_id), op_class="write", target_name=_TARGET
    )

    assert [entry["phase"] for entry in advisory[ADVISORY_EXTRAS_KEY]] == ["update"]


async def test_advisory_treats_an_unnumbered_step_as_newest(tenant_id: UUID) -> None:
    claim_id = uuid4()
    await _step(tenant_id, claim_id, "start", minutes_ago=1)
    completion = trusted_event(
        tenant_id=tenant_id,
        principal_sub=_PEER,
        claim_id=claim_id,
        phase="completion",
        target=_TARGET,
        ts=datetime.now(UTC) - timedelta(minutes=5),
    )
    row = _announcement_row(completion, None, 0)
    row.seq = None
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()

    assert (
        await build_target_activity_advisory(
            _operator(tenant_id), op_class="write", target_name=_TARGET
        )
        == {}
    )


async def test_claim_lookup_trusts_commit_order_over_a_newer_timestamp(tenant_id: UUID) -> None:
    claim_id = uuid4()
    await _step(tenant_id, claim_id, "update", minutes_ago=1, principal_sub=_ME)
    await _step(
        tenant_id, claim_id, "completion", minutes_ago=5, principal_sub=_ME, ttl_minutes=None
    )

    gap = await announce_claim_gap(_operator(tenant_id), op_class="write", target_name=_TARGET)

    assert gap == "no_claim"


async def test_claim_lookup_uses_the_step_committed_last(tenant_id: UUID) -> None:
    claim_id = uuid4()
    await _step(tenant_id, claim_id, "start", minutes_ago=5, principal_sub=_ME, ttl_minutes=1)
    await _step(tenant_id, claim_id, "update", minutes_ago=30, principal_sub=_ME)

    gap = await announce_claim_gap(_operator(tenant_id), op_class="write", target_name=_TARGET)

    assert gap is None
