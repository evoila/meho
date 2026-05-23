# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G9.3-T6 topology-history retention prune (#858).

Coverage matrix (Task #858 acceptance criteria):

* **Past-cutoff rows are deleted, newer rows survive** -- one
  ``GraphNodeHistory`` row with ``valid_from`` past the cutoff and one
  inside the retention window: after one tick the past-cutoff row is
  gone and the in-window row stays. Same shape for
  ``GraphEdgeHistory``.
* **TOPOLOGY_HISTORY_RETENTION_DAYS=0 is a no-op (keep-forever)** -- a
  past-cutoff row survives a tick when the setting is ``0``; no audit
  row is written (the no-op-tick path is a logged heartbeat only, not
  an audit-row emitter).
* **Exactly one audit row per tick** -- a tick that drops N node rows
  + M edge rows writes one ``AuditLog`` row with ``method='INTERNAL'``,
  ``path='topology.history.prune'``,
  ``operator_sub='system:topology-history-retention'``,
  ``payload={"dropped_node_rows": N, "dropped_edge_rows": M,
  "retention_days": D, "cutoff": <iso-ts>}``.
* **Settings bounds** -- Pydantic validators reject out-of-range values
  for the three new ``TOPOLOGY_HISTORY_*`` knobs at construction time
  (range floors / ceilings / opt-out sentinel handling).
* **Loop survives a bad tick** -- a tick that raises mid-flight logs
  ``topology_history_retention_tick_failed`` (loud-but-non-fatal) and
  the next sleep is still reached.
* **start/stop lifecycle** -- the lifespan helpers create and cleanly
  cancel the background task with no "Task was destroyed" /
  "unretrieved CancelledError" warnings under pytest-asyncio.
* **Loop honours configured cadence** -- the sweeper sleeps the
  configured ``TOPOLOGY_HISTORY_PRUNE_INTERVAL_SECONDS`` between ticks.

The tests run against the autouse ``_default_database_url`` SQLite-backed
engine; the prune's bounded DELETE rides the migration-0012-declared
``(tenant_id, valid_from DESC)`` index on PG, but the unit suite
exercises the same SQLAlchemy 2.x ``delete().where(...)`` statement on
SQLite (sequential scan -- functionally identical, cheap on the small
fixtures seeded here). The PG path is covered by the integration suite
once #862 (T7) lands its 1M-row performance fixture.
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

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    GraphEdgeHistory,
    GraphNodeHistory,
    Tenant,
)
from meho_backplane.memory.audit import INTERNAL_METHOD
from meho_backplane.settings import Settings, get_settings
from meho_backplane.topology import history_retention
from meho_backplane.topology.history_retention import (
    SYSTEM_OPERATOR_SUB,
    TOPOLOGY_HISTORY_PRUNE_PATH,
    TOPOLOGY_HISTORY_SYSTEM_TENANT_ID,
    _run_one_prune_tick,
    start_topology_history_retention_sweeper,
    stop_topology_history_retention_sweeper,
)


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
    """Insert one :class:`Tenant` and return its id.

    History rows carry a real ``REFERENCES tenant(id)`` FK on
    ``tenant_id`` (see :class:`~meho_backplane.db.models.GraphNodeHistory`),
    so any test seeding history rows must seed the parent tenant first.
    """
    sessionmaker = get_sessionmaker()
    tid = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(Tenant(id=tid, slug=f"t-{tid.hex[:8]}", name=f"Tenant {tid.hex[:6]}"))
        await session.commit()
    return tid


async def _seed_node_history(
    *,
    tenant_id: uuid.UUID,
    valid_from: datetime,
    change_kind: str = "created",
) -> int:
    """Insert one :class:`GraphNodeHistory` row and return its ``history_id``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = GraphNodeHistory(
            node_id=uuid.uuid4(),
            tenant_id=tenant_id,
            change_kind=change_kind,
            snapshot={"before": None, "after": {"name": "n"}},
            audit_id=None,
            valid_from=valid_from,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.history_id


async def _seed_edge_history(
    *,
    tenant_id: uuid.UUID,
    valid_from: datetime,
    change_kind: str = "created",
) -> int:
    """Insert one :class:`GraphEdgeHistory` row and return its ``history_id``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = GraphEdgeHistory(
            edge_id=uuid.uuid4(),
            tenant_id=tenant_id,
            change_kind=change_kind,
            snapshot={"before": None, "after": {"kind": "depends-on"}},
            audit_id=None,
            valid_from=valid_from,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.history_id


async def _list_node_history() -> list[GraphNodeHistory]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(GraphNodeHistory))
        return list(result.scalars().all())


async def _list_edge_history() -> list[GraphEdgeHistory]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(GraphEdgeHistory))
        return list(result.scalars().all())


async def _list_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Settings validators (AC: out-of-range values rejected at construction)
# ---------------------------------------------------------------------------


def _settings_kwargs(**overrides: Any) -> dict[str, Any]:
    """Build a complete :class:`Settings` kwargs dict with overrides."""
    base: dict[str, Any] = {
        "keycloak_issuer_url": "https://keycloak.test/realms/meho",
        "keycloak_audience": "meho-backplane",
        "vault_addr": "https://vault.test",
        "database_url": "sqlite+aiosqlite:///./test.db",
    }
    base.update(overrides)
    return base


def test_topology_history_retention_days_default_is_ninety() -> None:
    """Default 90 matches Initiative #365 work-item #8 (quarterly review)."""
    s = Settings(**_settings_kwargs())
    assert s.topology_history_retention_days == 90


def test_topology_history_retention_days_accepts_zero_sentinel() -> None:
    """``0`` is the keep-forever opt-out sentinel."""
    s = Settings(**_settings_kwargs(topology_history_retention_days=0))
    assert s.topology_history_retention_days == 0


def test_topology_history_retention_days_rejects_negative() -> None:
    """Range floor is 0 (the sentinel); -1 is not a meaningful retention."""
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(topology_history_retention_days=-1))


def test_topology_history_retention_days_rejects_above_ten_years() -> None:
    """Range ceiling is 3650 days (10y); above is functionally permanent."""
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(topology_history_retention_days=3651))


def test_topology_history_prune_interval_default_is_one_week() -> None:
    """Default 604800 (7d / weekly) matches Initiative #365's stated cadence."""
    s = Settings(**_settings_kwargs())
    assert s.topology_history_prune_interval_seconds == 604800


def test_topology_history_prune_interval_rejects_below_one_minute() -> None:
    """Range floor is 60s; below one minute competes with write load."""
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(topology_history_prune_interval_seconds=59))


def test_topology_history_prune_interval_rejects_above_one_week() -> None:
    """Range ceiling is 604800s (1w); above is slower than the docs say."""
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(topology_history_prune_interval_seconds=604801))


def test_topology_history_prune_enabled_default_is_true() -> None:
    """Default True: the in-process prune is the shipped retention mechanism."""
    s = Settings(**_settings_kwargs())
    assert s.topology_history_prune_enabled is True


def test_settings_prune_enabled_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TOPOLOGY_HISTORY_PRUNE_ENABLED=false`` resolves to ``False``."""
    monkeypatch.setenv("TOPOLOGY_HISTORY_PRUNE_ENABLED", "false")
    get_settings.cache_clear()
    s = get_settings()
    assert s.topology_history_prune_enabled is False


# ---------------------------------------------------------------------------
# Happy path -- past-cutoff rows deleted, newer rows survive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_deletes_past_cutoff_rows_and_keeps_newer_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past-cutoff history rows are deleted; in-window rows survive.

    Acceptance criterion: "deletes rows with ``valid_from < now() -
    TOPOLOGY_HISTORY_RETENTION_DAYS`` and leaves newer rows".
    """
    monkeypatch.setenv("TOPOLOGY_HISTORY_RETENTION_DAYS", "30")
    get_settings.cache_clear()

    tenant_id = await _seed_tenant()
    # Past cutoff: 31 days old (> 30-day retention).
    old_node = await _seed_node_history(
        tenant_id=tenant_id,
        valid_from=datetime.now(UTC) - timedelta(days=31),
    )
    old_edge = await _seed_edge_history(
        tenant_id=tenant_id,
        valid_from=datetime.now(UTC) - timedelta(days=31),
    )
    # In window: 1 day old.
    new_node = await _seed_node_history(
        tenant_id=tenant_id,
        valid_from=datetime.now(UTC) - timedelta(days=1),
    )
    new_edge = await _seed_edge_history(
        tenant_id=tenant_id,
        valid_from=datetime.now(UTC) - timedelta(days=1),
    )

    await _run_one_prune_tick()

    node_rows = await _list_node_history()
    edge_rows = await _list_edge_history()
    node_ids = {r.history_id for r in node_rows}
    edge_ids = {r.history_id for r in edge_rows}

    assert old_node not in node_ids, "past-cutoff node history row was not deleted"
    assert old_edge not in edge_ids, "past-cutoff edge history row was not deleted"
    assert new_node in node_ids, "in-window node history row was wrongly deleted"
    assert new_edge in edge_ids, "in-window edge history row was wrongly deleted"


# ---------------------------------------------------------------------------
# Opt-out sentinel -- TOPOLOGY_HISTORY_RETENTION_DAYS=0 is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_with_retention_zero_is_no_op_and_writes_no_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RETENTION_DAYS=0`` keeps the loop running but every tick is a no-op.

    Acceptance criterion: "``TOPOLOGY_HISTORY_RETENTION_DAYS=0`` -> prune
    is a no-op (keep forever); asserted". The no-op-tick path also
    deliberately skips writing an audit row (heartbeat is log-only) so
    weekly ticks do not flood ``audit_log`` with N empty-payload rows.
    """
    monkeypatch.setenv("TOPOLOGY_HISTORY_RETENTION_DAYS", "0")
    get_settings.cache_clear()

    tenant_id = await _seed_tenant()
    # A row that would have been deleted under any non-zero retention.
    very_old_node = await _seed_node_history(
        tenant_id=tenant_id,
        valid_from=datetime.now(UTC) - timedelta(days=365 * 5),
    )

    await _run_one_prune_tick()

    node_rows = await _list_node_history()
    assert any(r.history_id == very_old_node for r in node_rows), (
        "RETENTION_DAYS=0 must keep all rows; very-old row was deleted"
    )
    audit_rows = await _list_audit_rows()
    assert audit_rows == [], "no-op tick must not write an audit row (heartbeat is log-only)"


# ---------------------------------------------------------------------------
# Audit-row shape -- exactly one row per tick with the documented payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_writes_one_audit_row_with_expected_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick that drops N node rows + M edge rows writes one audit row.

    Acceptance criterion: "Each run writes exactly one audit row
    (``op_id='topology.history.prune'``, ``op_class='write'``) with the
    dropped-row count in ``payload``."
    """
    monkeypatch.setenv("TOPOLOGY_HISTORY_RETENTION_DAYS", "30")
    get_settings.cache_clear()

    tenant_id = await _seed_tenant()
    # Two node rows + one edge row past the cutoff.
    for _ in range(2):
        await _seed_node_history(
            tenant_id=tenant_id,
            valid_from=datetime.now(UTC) - timedelta(days=60),
        )
    await _seed_edge_history(
        tenant_id=tenant_id,
        valid_from=datetime.now(UTC) - timedelta(days=60),
    )

    await _run_one_prune_tick()

    audit_rows = await _list_audit_rows()
    assert len(audit_rows) == 1, f"expected exactly one audit row per tick; got {len(audit_rows)}"
    row = audit_rows[0]
    assert row.method == INTERNAL_METHOD
    assert row.path == TOPOLOGY_HISTORY_PRUNE_PATH
    assert row.operator_sub == SYSTEM_OPERATOR_SUB
    assert row.tenant_id == TOPOLOGY_HISTORY_SYSTEM_TENANT_ID
    assert row.status_code == 200
    assert row.payload["dropped_node_rows"] == 2
    assert row.payload["dropped_edge_rows"] == 1
    assert row.payload["retention_days"] == 30
    # ``cutoff`` is rendered as an ISO 8601 string with the ``+00:00`` offset
    # so audit-query consumers can compare it lexicographically against
    # other UTC-normalised timestamps.
    assert isinstance(row.payload["cutoff"], str)
    assert "+00:00" in row.payload["cutoff"] or row.payload["cutoff"].endswith("Z")


# ---------------------------------------------------------------------------
# Empty tick -- no past-cutoff rows still writes one audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_with_no_past_cutoff_rows_writes_zero_count_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick with nothing to prune still emits one audit row with zero counts.

    Operators want monotonic confirmation the prune ran -- a missing
    audit row would be indistinguishable from a stuck loop. Memory-
    expiry's sweeper deliberately skips the audit row on the empty case
    (its sweep cadence is daily, so the rate is acceptable); the prune
    runs weekly so the audit-row volume is one row per week per
    deployment, which is below noise. Always writing the row also keeps
    the operator-facing "when did the last prune run" query trivial.
    """
    monkeypatch.setenv("TOPOLOGY_HISTORY_RETENTION_DAYS", "30")
    get_settings.cache_clear()

    tenant_id = await _seed_tenant()
    await _seed_node_history(
        tenant_id=tenant_id,
        valid_from=datetime.now(UTC) - timedelta(days=1),
    )

    await _run_one_prune_tick()

    audit_rows = await _list_audit_rows()
    assert len(audit_rows) == 1
    assert audit_rows[0].payload["dropped_node_rows"] == 0
    assert audit_rows[0].payload["dropped_edge_rows"] == 0


# ---------------------------------------------------------------------------
# Loop survives bad ticks (AC: monkeypatched raise -> next sleep reached)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_survives_tick_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing tick logs and the loop reaches the next sleep.

    Mirrors the memory-expiry sweeper's loop-survival assertion: the
    first tick raises, the second sleep is what stops the loop via
    ``CancelledError`` -- but we assert that the second sleep *was
    reached*, proving the loop did not die on the first tick's failure.
    """
    monkeypatch.setenv("TOPOLOGY_HISTORY_PRUNE_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("TOPOLOGY_HISTORY_RETENTION_DAYS", "90")
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
            # One sleep + one (failed) tick + one more sleep, then
            # stop the loop -- the second sleep proves loop survival.
            raise asyncio.CancelledError

    with (
        patch(
            "meho_backplane.topology.history_retention._run_one_prune_tick",
            new=_flaky_tick,
        ),
        patch("asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await history_retention._prune_loop()

    assert tick_calls == 1
    assert sleep_calls == 2


# ---------------------------------------------------------------------------
# Lifecycle (AC: start/stop clean, no destroyed-task warnings)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_stop_sweeper_lifecycle() -> None:
    """start + stop the task with no destroyed-task warnings.

    The patch on ``_run_one_prune_tick`` keeps the loop body cheap; the
    ``stop_*`` helper's ``contextlib.suppress(CancelledError)`` is what
    prevents the "Task was destroyed but it is pending" warning
    pytest-asyncio would otherwise raise on unawaited cancelled tasks.
    """
    with (
        patch(
            "meho_backplane.topology.history_retention._run_one_prune_tick",
            new=AsyncMock(),
        ),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        task = start_topology_history_retention_sweeper()
        assert not task.done()
        await stop_topology_history_retention_sweeper(task)
        assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_loop_sleeps_configured_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prune loop honours ``TOPOLOGY_HISTORY_PRUNE_INTERVAL_SECONDS``."""
    monkeypatch.setenv("TOPOLOGY_HISTORY_PRUNE_INTERVAL_SECONDS", "1234")
    get_settings.cache_clear()

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError

    with (
        patch(
            "meho_backplane.topology.history_retention._run_one_prune_tick",
            new=AsyncMock(),
        ),
        patch("asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await history_retention._prune_loop()

    assert sleeps == [1234]
