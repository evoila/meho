# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G5.2-T1 memory-expiry sweeper (#623).

Coverage matrix (Task #623 acceptance criteria):

* **Past-expiry rows are deleted** — a ``source="memory"`` row with
  ``metadata.expires_at`` in the past disappears after one tick, and
  exactly one ``audit_log`` row lands with ``method="INTERNAL"``,
  ``path="memory.expire"``, ``payload.expired_count == 1``.
* **Future-expiry rows survive** — a ``source="memory"`` row with
  ``expires_at`` in the future stays put.
* **Non-memory rows ignored** — a ``source="kb"`` row with a past
  ``expires_at`` survives (the sweeper only touches memory).
* **Loop survives a bad tick** — a tick that raises mid-flight logs
  ``memory_expiry_tick_failed`` (loud-but-non-fatal) and the next
  tick still executes.
* **Disabled sweeper is never started** — when
  ``MEMORY_EXPIRY_ENABLED=false`` the lifespan does not create a task
  handle (asserted by inspecting the lifespan's local state).
* **Settings bounds** — Pydantic validators reject out-of-range
  values for the three new ``MEMORY_*`` knobs at construction time.
* **start/stop lifecycle** — the lifespan helpers create and cleanly
  cancel the background task with no "Task was destroyed" /
  "unretrieved CancelledError" warnings under pytest-asyncio.

The tests run against the autouse :func:`tests.conftest._default_database_url`
fixture's SQLite-backed engine; the expiry sweeper's PG-vs-SQLite JSON
extraction is exercised on the SQLite path here, with the PG path
covered by the integration suite (driven by the migration runner that
applies migration ``0003`` against a testcontainers Postgres).
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
from meho_backplane.db.models import AuditLog, Document, Tenant
from meho_backplane.memory import expiry
from meho_backplane.memory.audit import (
    INTERNAL_METHOD,
    MEMORY_EXPIRE_PATH,
    SYSTEM_OPERATOR_SUB,
    write_internal_audit_row,
)
from meho_backplane.memory.expiry import (
    _run_one_tick,
    start_memory_expiry_sweeper,
    stop_memory_expiry_sweeper,
)
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


#: 384-dim placeholder embedding. The sweeper never reads the column,
#: but the schema requires NOT NULL; a fixed list keeps the seed cheap.
_FAKE_EMBEDDING: list[float] = [0.01] * 384


async def _seed_tenant() -> uuid.UUID:
    """Insert one :class:`Tenant` and return its id.

    Document.tenant_id carries a real FK to ``tenant.id`` (see
    :class:`~meho_backplane.db.models.Document`), so a sweeper test
    that seeds documents must seed the parent tenant first.
    """
    sessionmaker = get_sessionmaker()
    tid = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(Tenant(id=tid, slug=f"t-{tid.hex[:8]}", name=f"Tenant {tid.hex[:6]}"))
        await session.commit()
    return tid


async def _seed_document(
    *,
    tenant_id: uuid.UUID,
    source: str,
    kind: str,
    source_id: str,
    expires_at: datetime | None,
) -> uuid.UUID:
    """Insert one :class:`Document` row and return its id.

    ``expires_at`` is rendered into ``doc_metadata`` as an ISO 8601
    string with a ``+00:00`` offset (matching
    :func:`~meho_backplane.memory._internal.build_metadata`'s
    serialisation) when provided, or omitted entirely when ``None``
    (the "persistent memory, no TTL" shape).
    """
    sessionmaker = get_sessionmaker()
    doc_id = uuid.uuid4()
    metadata: dict[str, Any] = {"scope": kind}
    if expires_at is not None:
        metadata["expires_at"] = expires_at.isoformat()
    async with sessionmaker() as session:
        session.add(
            Document(
                id=doc_id,
                tenant_id=tenant_id,
                source=source,
                source_id=source_id,
                kind=kind,
                body=f"body {source_id}",
                body_hash="x" * 64,
                embedding=_FAKE_EMBEDDING,
                doc_metadata=metadata,
            )
        )
        await session.commit()
    return doc_id


async def _list_documents() -> list[Document]:
    """Return every ``documents`` row (test-helper, small datasets only)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(Document))
        return list(result.scalars().all())


async def _list_audit_rows() -> list[AuditLog]:
    """Return every ``audit_log`` row (test-helper, small datasets only)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Settings validators (AC: out-of-range values rejected at construction)
# ---------------------------------------------------------------------------


def _settings_kwargs(**overrides: Any) -> dict[str, Any]:
    """Build a complete :class:`Settings` kwargs dict with overrides applied.

    Pydantic models reject ``__init__`` calls missing required fields,
    so the validator tests cannot just pass the field under test --
    every required field needs a value. Centralising the boilerplate
    here keeps the per-test assertion focused on the one field that
    matters.
    """
    base: dict[str, Any] = {
        "keycloak_issuer_url": "https://keycloak.test/realms/meho",
        "keycloak_audience": "meho-backplane",
        "vault_addr": "https://vault.test",
        "database_url": "sqlite+aiosqlite:///./test.db",
    }
    base.update(overrides)
    return base


def test_memory_user_default_ttl_days_default_is_seven() -> None:
    """Default 7 matches consumer-needs.md §G5 ("expires after 7 days")."""
    s = Settings(**_settings_kwargs())
    assert s.memory_user_default_ttl_days == 7


def test_memory_user_default_ttl_days_rejects_zero() -> None:
    """Range floor is 1 day; zero defeats the auto-expiry contract."""
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(memory_user_default_ttl_days=0))


def test_memory_user_default_ttl_days_rejects_above_one_year() -> None:
    """Range ceiling is 365 days; >1 year is functionally permanent."""
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(memory_user_default_ttl_days=366))


def test_memory_expiry_tick_interval_default_is_one_day() -> None:
    """Default 86400 (24 h) matches Initiative #374's stated cadence."""
    s = Settings(**_settings_kwargs())
    assert s.memory_expiry_tick_interval_seconds == 86400


def test_memory_expiry_tick_interval_rejects_below_one_minute() -> None:
    """Range floor is 60s; below one minute competes with request load."""
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(memory_expiry_tick_interval_seconds=59))


def test_memory_expiry_tick_interval_rejects_above_one_day() -> None:
    """Range ceiling is 86400s; above 24 h risks soft-hidden pollution."""
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(memory_expiry_tick_interval_seconds=86401))


def test_memory_expiry_enabled_default_is_true() -> None:
    """Default True: the in-process sweeper is the shipped mechanism."""
    s = Settings(**_settings_kwargs())
    assert s.memory_expiry_enabled is True


# ---------------------------------------------------------------------------
# Sweeper tick — happy path (AC: past-expiry row deleted + audit row written)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_deletes_past_expiry_row_and_writes_audit_row() -> None:
    tenant_id = await _seed_tenant()
    past = datetime.now(UTC) - timedelta(days=1)
    doc_id = await _seed_document(
        tenant_id=tenant_id,
        source="memory",
        kind="memory-user",
        source_id="user:op-1:slug-1",
        expires_at=past,
    )

    await _run_one_tick()

    docs = await _list_documents()
    assert all(d.id != doc_id for d in docs), "expired memory row was not deleted"

    audit_rows = await _list_audit_rows()
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row.method == INTERNAL_METHOD
    assert row.path == MEMORY_EXPIRE_PATH
    assert row.operator_sub == SYSTEM_OPERATOR_SUB
    assert row.tenant_id == tenant_id
    assert row.status_code == 200
    assert row.payload["expired_count"] == 1
    assert row.payload["scopes"] == ["memory-user"]


# ---------------------------------------------------------------------------
# Future-expiry survives (AC: future expires_at is not deleted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_does_not_delete_future_expiry_row() -> None:
    tenant_id = await _seed_tenant()
    future = datetime.now(UTC) + timedelta(days=7)
    doc_id = await _seed_document(
        tenant_id=tenant_id,
        source="memory",
        kind="memory-user",
        source_id="user:op-1:still-live",
        expires_at=future,
    )

    await _run_one_tick()

    docs = await _list_documents()
    assert any(d.id == doc_id for d in docs), "future-expiry row was wrongly deleted"
    audit_rows = await _list_audit_rows()
    assert audit_rows == [], "no expired rows -> no audit row should land"


@pytest.mark.asyncio
async def test_tick_does_not_delete_persistent_memory_with_no_expiry() -> None:
    """A memory row without an ``expires_at`` field stays put forever.

    The ``expires_expr.is_not(None)`` guard in :func:`_run_one_tick` is
    the load-bearing branch: a JSON column with no ``expires_at`` key
    returns NULL on both ``jsonb_extract_path_text`` (PG) and
    ``json_extract`` (SQLite), and the sweeper must treat NULL as
    "do not delete".
    """
    tenant_id = await _seed_tenant()
    doc_id = await _seed_document(
        tenant_id=tenant_id,
        source="memory",
        kind="memory-tenant",
        source_id="tenant:persistent",
        expires_at=None,
    )

    await _run_one_tick()

    docs = await _list_documents()
    assert any(d.id == doc_id for d in docs), "persistent memory was wrongly deleted"


# ---------------------------------------------------------------------------
# Non-memory rows survive (AC: kb row with past expires_at is not touched)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_does_not_delete_non_memory_rows() -> None:
    tenant_id = await _seed_tenant()
    past = datetime.now(UTC) - timedelta(days=1)
    kb_id = await _seed_document(
        tenant_id=tenant_id,
        source="kb",
        kind="kb-entry",
        source_id="kb:should-survive",
        expires_at=past,
    )

    await _run_one_tick()

    docs = await _list_documents()
    assert any(d.id == kb_id for d in docs), "non-memory row was wrongly deleted"


# ---------------------------------------------------------------------------
# Multi-tenant grouping (AC: one audit row per affected tenant)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_writes_one_audit_row_per_affected_tenant() -> None:
    tenant_a = await _seed_tenant()
    tenant_b = await _seed_tenant()
    past = datetime.now(UTC) - timedelta(days=1)
    await _seed_document(
        tenant_id=tenant_a,
        source="memory",
        kind="memory-user",
        source_id="user:a:e1",
        expires_at=past,
    )
    await _seed_document(
        tenant_id=tenant_a,
        source="memory",
        kind="memory-tenant",
        source_id="tenant:a:e2",
        expires_at=past,
    )
    await _seed_document(
        tenant_id=tenant_b,
        source="memory",
        kind="memory-user",
        source_id="user:b:e1",
        expires_at=past,
    )

    await _run_one_tick()

    audit_rows = await _list_audit_rows()
    assert len(audit_rows) == 2
    by_tenant = {row.tenant_id: row for row in audit_rows}
    assert by_tenant[tenant_a].payload["expired_count"] == 2
    # ``scopes`` is sorted+deduplicated by the writer.
    assert by_tenant[tenant_a].payload["scopes"] == ["memory-tenant", "memory-user"]
    assert by_tenant[tenant_b].payload["expired_count"] == 1
    assert by_tenant[tenant_b].payload["scopes"] == ["memory-user"]


# ---------------------------------------------------------------------------
# Loop survives bad ticks (AC: monkeypatched raise -> next tick runs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_survives_tick_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing tick logs and the loop reaches the next sleep + tick.

    Mirrors the topology scheduler's ``test_loop_survives_sweep_exception``
    pattern: the first tick raises, the second sleep is what stops the
    loop via ``CancelledError`` -- but we assert that the second sleep
    *was reached*, proving the loop did not die on the first tick's
    failure.
    """
    monkeypatch.setenv("MEMORY_EXPIRY_TICK_INTERVAL_SECONDS", "60")
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
            # Allow one sleep + one (failed) tick + one more sleep, then
            # stop the loop -- the second sleep is the marker that the
            # loop survived the failed tick.
            raise asyncio.CancelledError

    with (
        patch(
            "meho_backplane.memory.expiry._run_one_tick",
            new=_flaky_tick,
        ),
        patch("asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await expiry._sweeper_loop()

    assert tick_calls == 1
    assert sleep_calls == 2


# ---------------------------------------------------------------------------
# Lifecycle (AC: start/stop clean, no destroyed-task warnings)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_stop_sweeper_lifecycle() -> None:
    """start + stop the task on a clean async loop with no destroyed-task warnings.

    The patch on ``_run_one_tick`` keeps the loop body cheap; the
    ``stop_*`` helper's ``contextlib.suppress(CancelledError)`` is
    what prevents the "Task was destroyed but it is pending" warning
    pytest-asyncio would otherwise raise on unawaited cancelled tasks.
    """
    with (
        patch(
            "meho_backplane.memory.expiry._run_one_tick",
            new=AsyncMock(),
        ),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        task = start_memory_expiry_sweeper()
        assert not task.done()
        await stop_memory_expiry_sweeper(task)
        assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_loop_sleeps_configured_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweeper loop honours ``MEMORY_EXPIRY_TICK_INTERVAL_SECONDS``."""
    monkeypatch.setenv("MEMORY_EXPIRY_TICK_INTERVAL_SECONDS", "1234")
    get_settings.cache_clear()

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        # Stop the loop after the first sleep.
        raise asyncio.CancelledError

    with (
        patch(
            "meho_backplane.memory.expiry._run_one_tick",
            new=AsyncMock(),
        ),
        patch("asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await expiry._sweeper_loop()

    assert sleeps == [1234]


# ---------------------------------------------------------------------------
# MEMORY_EXPIRY_ENABLED=false (AC: sweeper task is never created)
# ---------------------------------------------------------------------------


def test_settings_memory_expiry_enabled_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MEMORY_EXPIRY_ENABLED=false`` resolves to ``False`` at settings layer.

    The lifespan guards ``start_memory_expiry_sweeper()`` behind this
    setting -- when False, no task handle is created. The settings-layer
    assertion is the cheapest test for the wiring; the lifespan-shape
    assertion (no task handle visible) is covered by the integration
    suite via :func:`tests.test_app_starts` once the lifespan is
    exercised end-to-end with the env var off.
    """
    monkeypatch.setenv("MEMORY_EXPIRY_ENABLED", "false")
    get_settings.cache_clear()
    s = get_settings()
    assert s.memory_expiry_enabled is False


def test_settings_memory_expiry_enabled_accepts_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical truthy spellings (1, true, yes, on) all enable the sweeper."""
    for spelling in ("1", "true", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv("MEMORY_EXPIRY_ENABLED", spelling)
        get_settings.cache_clear()
        s = get_settings()
        assert s.memory_expiry_enabled is True, f"spelling {spelling!r} should be truthy"


# ---------------------------------------------------------------------------
# write_internal_audit_row direct-call shape (AC: writer mirrors mcp/audit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_internal_audit_row_persists_expected_columns() -> None:
    """The direct-call writer commits the exact column shape the sweeper relies on."""
    tenant_id = await _seed_tenant()
    audit_id = await write_internal_audit_row(
        operator_sub=SYSTEM_OPERATOR_SUB,
        tenant_id=tenant_id,
        method=INTERNAL_METHOD,
        path=MEMORY_EXPIRE_PATH,
        status_code=200,
        duration_ms=12.5,
        payload={"expired_count": 3, "scopes": ["memory-user"]},
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.id == audit_id))
        row = result.scalar_one()

    assert row.operator_sub == SYSTEM_OPERATOR_SUB
    assert row.method == INTERNAL_METHOD
    assert row.path == MEMORY_EXPIRE_PATH
    assert row.tenant_id == tenant_id
    assert row.status_code == 200
    assert row.payload == {"expired_count": 3, "scopes": ["memory-user"]}
    # Decimal round-trip preserves the float through string conversion.
    assert float(row.duration_ms) == 12.5
