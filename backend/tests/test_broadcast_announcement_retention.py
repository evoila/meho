# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the durable-announcement retention prune (#2547).

Broadcast v2 Initiative #2543, Task #2547 (T2). Mirrors the coverage
matrix of ``test_topology_history_retention.py`` (the mold this prune
copies) for the ``agent_announcement`` archive:

* **Past-cutoff rows deleted, in-window rows survive.**
* **``BROADCAST_ANNOUNCEMENT_RETENTION_DAYS=0`` is a no-op (keep-forever),
  no audit row written** (mold parity with the topology no-op path).
* **Exactly one audit row per non-no-op tick** with the documented
  ``INTERNAL`` / ``broadcast.announcement.prune`` shape.
* **Empty tick still writes a zero-count audit row.**
* **Settings validators reject out-of-range values.**
* **Loop survives a bad tick; start/stop lifecycle is clean.**

Runs against the autouse SQLite-backed engine the suite pre-migrates to
``alembic upgrade head`` (so migration ``0066`` has created the table).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from meho_backplane.broadcast import announcement_retention
from meho_backplane.broadcast.announcement_retention import (
    ANNOUNCEMENT_RETENTION_PRUNE_PATH,
    ANNOUNCEMENT_RETENTION_SYSTEM_TENANT_ID,
    SYSTEM_OPERATOR_SUB,
    _run_one_prune_tick,
    start_announcement_retention_sweeper,
    stop_announcement_retention_sweeper,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement, AuditLog, Tenant
from meho_backplane.memory.audit import INTERNAL_METHOD
from meho_backplane.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant() -> uuid.UUID:
    """Insert one :class:`Tenant` and return its id (the FK parent)."""
    sessionmaker = get_sessionmaker()
    tid = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(Tenant(id=tid, slug=f"t-{tid.hex[:8]}", name=f"Tenant {tid.hex[:6]}"))
        await session.commit()
    return tid


async def _seed_announcement(
    *,
    tenant_id: uuid.UUID,
    created_at: datetime,
) -> uuid.UUID:
    """Insert one :class:`AgentAnnouncement` row and return its id."""
    sessionmaker = get_sessionmaker()
    row_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            AgentAnnouncement(
                id=row_id,
                tenant_id=tenant_id,
                principal_sub="op-1",
                activity="rotating tokens",
                phase="update",
                targets=[],
                created_at=created_at,
            )
        )
        await session.commit()
    return row_id


async def _list_announcements() -> list[AgentAnnouncement]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AgentAnnouncement))
        return list(result.scalars().all())


async def _list_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Settings validators
# ---------------------------------------------------------------------------


def _settings_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "keycloak_issuer_url": "https://keycloak.test/realms/meho",
        "keycloak_audience": "meho-backplane",
        "vault_addr": "https://vault.test",
        "database_url": "sqlite+aiosqlite:///./test.db",
    }
    base.update(overrides)
    return base


def test_retention_days_default_is_ninety() -> None:
    s = Settings(**_settings_kwargs())
    assert s.broadcast_announcement_retention_days == 90


def test_retention_days_accepts_zero_sentinel() -> None:
    s = Settings(**_settings_kwargs(broadcast_announcement_retention_days=0))
    assert s.broadcast_announcement_retention_days == 0


def test_retention_days_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(broadcast_announcement_retention_days=-1))


def test_retention_days_rejects_above_ten_years() -> None:
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(broadcast_announcement_retention_days=3651))


def test_prune_interval_rejects_below_one_minute() -> None:
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(broadcast_announcement_prune_interval_seconds=59))


def test_prune_interval_rejects_above_one_week() -> None:
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(broadcast_announcement_prune_interval_seconds=604801))


def test_prune_enabled_default_is_true() -> None:
    s = Settings(**_settings_kwargs())
    assert s.broadcast_announcement_prune_enabled is True


def test_prune_enabled_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROADCAST_ANNOUNCEMENT_PRUNE_ENABLED", "false")
    get_settings.cache_clear()
    assert get_settings().broadcast_announcement_prune_enabled is False


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_deletes_past_cutoff_rows_and_keeps_newer_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past-cutoff announcements are deleted; in-window rows survive."""
    monkeypatch.setenv("BROADCAST_ANNOUNCEMENT_RETENTION_DAYS", "30")
    get_settings.cache_clear()

    tenant_id = await _seed_tenant()
    old = await _seed_announcement(
        tenant_id=tenant_id,
        created_at=datetime.now(UTC) - timedelta(days=31),
    )
    recent = await _seed_announcement(
        tenant_id=tenant_id,
        created_at=datetime.now(UTC) - timedelta(days=1),
    )

    await _run_one_prune_tick()

    ids = {r.id for r in await _list_announcements()}
    assert old not in ids, "past-cutoff announcement was not deleted"
    assert recent in ids, "in-window announcement was wrongly deleted"


@pytest.mark.asyncio
async def test_tick_with_retention_zero_is_no_op_and_writes_no_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RETENTION_DAYS=0`` keeps every row and writes no audit row (heartbeat)."""
    monkeypatch.setenv("BROADCAST_ANNOUNCEMENT_RETENTION_DAYS", "0")
    get_settings.cache_clear()

    tenant_id = await _seed_tenant()
    very_old = await _seed_announcement(
        tenant_id=tenant_id,
        created_at=datetime.now(UTC) - timedelta(days=365 * 5),
    )

    await _run_one_prune_tick()

    assert any(r.id == very_old for r in await _list_announcements()), (
        "RETENTION_DAYS=0 must keep all rows"
    )
    assert await _list_audit_rows() == [], "no-op tick must not write an audit row"


@pytest.mark.asyncio
async def test_tick_writes_one_audit_row_with_expected_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick that drops N rows writes exactly one INTERNAL audit row."""
    monkeypatch.setenv("BROADCAST_ANNOUNCEMENT_RETENTION_DAYS", "30")
    get_settings.cache_clear()

    tenant_id = await _seed_tenant()
    for _ in range(3):
        await _seed_announcement(
            tenant_id=tenant_id,
            created_at=datetime.now(UTC) - timedelta(days=60),
        )

    await _run_one_prune_tick()

    audit_rows = await _list_audit_rows()
    assert len(audit_rows) == 1, f"expected exactly one audit row per tick; got {len(audit_rows)}"
    row = audit_rows[0]
    assert row.method == INTERNAL_METHOD
    assert row.path == ANNOUNCEMENT_RETENTION_PRUNE_PATH
    assert row.operator_sub == SYSTEM_OPERATOR_SUB
    assert row.tenant_id == ANNOUNCEMENT_RETENTION_SYSTEM_TENANT_ID
    assert row.status_code == 200
    assert row.payload["dropped_rows"] == 3
    assert row.payload["retention_days"] == 30
    assert isinstance(row.payload["cutoff"], str)
    assert "+00:00" in row.payload["cutoff"] or row.payload["cutoff"].endswith("Z")


@pytest.mark.asyncio
async def test_tick_with_no_past_cutoff_rows_writes_zero_count_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick with nothing to prune still emits one zero-count audit row."""
    monkeypatch.setenv("BROADCAST_ANNOUNCEMENT_RETENTION_DAYS", "30")
    get_settings.cache_clear()

    tenant_id = await _seed_tenant()
    await _seed_announcement(
        tenant_id=tenant_id,
        created_at=datetime.now(UTC) - timedelta(days=1),
    )

    await _run_one_prune_tick()

    audit_rows = await _list_audit_rows()
    assert len(audit_rows) == 1
    assert audit_rows[0].payload["dropped_rows"] == 0


# ---------------------------------------------------------------------------
# Loop survival + lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_survives_tick_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing tick logs and the loop reaches the next sleep (loop-survival)."""
    monkeypatch.setenv("BROADCAST_ANNOUNCEMENT_PRUNE_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("BROADCAST_ANNOUNCEMENT_RETENTION_DAYS", "90")
    get_settings.cache_clear()

    tick_calls = 0
    sleep_calls = 0

    async def _flaky_tick() -> None:
        nonlocal tick_calls
        tick_calls += 1
        if tick_calls == 1:
            raise RuntimeError("transient DB blip")

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    with (
        patch(
            "meho_backplane.broadcast.announcement_retention._run_one_prune_tick",
            new=_flaky_tick,
        ),
        patch("asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await announcement_retention._prune_loop()

    assert tick_calls == 1
    assert sleep_calls == 2


@pytest.mark.asyncio
async def test_start_and_stop_sweeper_lifecycle() -> None:
    """start + stop the task with no destroyed-task warnings."""
    with (
        patch(
            "meho_backplane.broadcast.announcement_retention._run_one_prune_tick",
            new=AsyncMock(),
        ),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        task = start_announcement_retention_sweeper()
        assert not task.done()
        await stop_announcement_retention_sweeper(task)
        assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_loop_sleeps_configured_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prune loop honours ``BROADCAST_ANNOUNCEMENT_PRUNE_INTERVAL_SECONDS``."""
    monkeypatch.setenv("BROADCAST_ANNOUNCEMENT_PRUNE_INTERVAL_SECONDS", "1234")
    get_settings.cache_clear()

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError

    with (
        patch(
            "meho_backplane.broadcast.announcement_retention._run_one_prune_tick",
            new=AsyncMock(),
        ),
        patch("asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await announcement_retention._prune_loop()

    assert sleeps == [1234]
