# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The weekly knowledge upkeep job (:mod:`meho_backplane.lessons.upkeep`).

Runs on a test tenant: merge, retire, refresh, the list for a reviewer,
the report rows, and the loop's start and stop.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, LessonRetrieval, MemoryRevision
from meho_backplane.kb.service import KbService
from meho_backplane.lessons import upkeep
from meho_backplane.lessons.upkeep import (
    SYSTEM_OPERATOR_SUB,
    UPKEEP_PATH,
    UPKEEP_SYSTEM_TENANT_ID,
    run_upkeep,
    start_knowledge_upkeep,
    stop_knowledge_upkeep,
)
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.settings import get_settings
from tests._kb_embedding_stub import fake_embedding_service
from tests._lessons_helpers import seed_dispatch, seed_retrieval, seed_tenant

TENANT = uuid.UUID("00000000-0000-0000-0000-0000000057e1")
NOW = datetime.now(UTC)
TARGET = uuid.UUID("00000000-0000-0000-0000-00000000f001")
TARGET_2 = uuid.UUID("00000000-0000-0000-0000-00000000f002")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake_embedding_service(),
    ):
        yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _tenant() -> None:
    await seed_tenant(TENANT)


async def _status(slug: str) -> dict[str, object]:
    entry = await KbService().get_entry(TENANT, slug)
    assert entry is not None
    return entry.metadata


@pytest.mark.asyncio
async def test_duplicates_merge_into_the_confirmed_copy() -> None:
    service = KbService()
    await service.create_entry(
        TENANT, "a-old", "Restart  the agent\nfirst.", {"status": "candidate"}
    )
    await service.create_entry(
        TENANT, "b-confirmed", "restart the agent first.", {"status": "confirmed"}
    )
    await service.create_entry(TENANT, "c-other", "Something else.")

    reports = await run_upkeep(now=NOW)

    old = await _status("a-old")
    assert old["status"] == "replaced"
    assert old["replaced_by"] == "b-confirmed"
    assert (await _status("b-confirmed"))["status"] == "confirmed"
    assert "status" not in await _status("c-other")
    report = reports[TENANT]
    assert report.merged_count == 1
    assert [(m.slug, m.into) for m in report.merged] == [("a-old", "b-confirmed")]


@pytest.mark.asyncio
async def test_without_a_confirmed_copy_the_oldest_is_kept() -> None:
    service = KbService()
    await service.create_entry(TENANT, "z-first", "same body")
    await service.create_entry(TENANT, "a-second", "same body")
    await run_upkeep(now=NOW)
    assert (await _status("a-second"))["replaced_by"] == "z-first"
    assert "status" not in await _status("z-first")


@pytest.mark.asyncio
async def test_expired_entries_are_retired_and_others_stay() -> None:
    service = KbService()
    past = (NOW - timedelta(days=1)).isoformat()
    future = (NOW + timedelta(days=30)).isoformat()
    await service.create_entry(TENANT, "old", "old", {"status": "candidate", "expires_at": past})
    await service.create_entry(TENANT, "new", "new", {"status": "candidate", "expires_at": future})
    await service.create_entry(TENANT, "kept", "kept", {"status": "confirmed"})

    reports = await run_upkeep(now=NOW)

    assert (await _status("old"))["status"] == "retired"
    assert (await _status("new"))["status"] == "candidate"
    assert (await _status("kept"))["status"] == "confirmed"
    assert reports[TENANT].retired == ["old"]


@pytest.mark.asyncio
async def test_a_used_confirmed_entry_gets_confirmed_at_again() -> None:
    entry, _ = await KbService().create_entry(
        TENANT, "lesson", "body", {"status": "confirmed", "confirmed_at": "2026-01-01"}
    )
    session_id = uuid.uuid4()
    used_at = NOW - timedelta(hours=1)
    await seed_retrieval(
        tenant_id=TENANT,
        entry_id=entry.id,
        session_id=session_id,
        at=used_at - timedelta(minutes=5),
    )
    await seed_dispatch(tenant_id=TENANT, session_id=session_id, at=used_at, target_id=TARGET)

    reports = await run_upkeep(now=NOW)

    stored = await _status("lesson")
    assert datetime.fromisoformat(str(stored["confirmed_at"])) == used_at
    assert [r.slug for r in reports[TENANT].refreshed] == ["lesson"]
    again = await run_upkeep(now=NOW)
    assert again[TENANT].refreshed == [], "a second run changes nothing"


@pytest.mark.asyncio
async def test_a_retrieval_without_a_later_success_refreshes_nothing() -> None:
    entry, _ = await KbService().create_entry(TENANT, "lesson", "body", {"status": "confirmed"})
    session_id = uuid.uuid4()
    await seed_dispatch(tenant_id=TENANT, session_id=session_id, at=NOW - timedelta(hours=2))
    await seed_retrieval(
        tenant_id=TENANT, entry_id=entry.id, session_id=session_id, at=NOW - timedelta(hours=1)
    )
    await seed_dispatch(
        tenant_id=TENANT, session_id=session_id, at=NOW - timedelta(minutes=5), status_code=500
    )
    reports = await run_upkeep(now=NOW)
    assert "confirmed_at" not in await _status("lesson")
    assert reports[TENANT].refreshed == []


@pytest.mark.asyncio
async def test_a_used_confirmed_memory_entry_gets_confirmed_at_and_a_revision() -> None:
    operator = Operator(
        sub="admin",
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )
    memory = await MemoryService().remember(
        operator,
        scope=MemoryScope.TENANT,
        slug="m",
        body="memory",
        metadata={"status": "confirmed"},
    )
    session_id = uuid.uuid4()
    used_at = NOW - timedelta(hours=1)
    await seed_retrieval(
        tenant_id=TENANT,
        entry_id=memory.id,
        session_id=session_id,
        at=used_at - timedelta(minutes=1),
        store="memory",
    )
    await seed_dispatch(tenant_id=TENANT, session_id=session_id, at=used_at)

    reports = await run_upkeep(now=NOW)

    assert [(r.store, r.slug) for r in reports[TENANT].refreshed] == [("memory", memory.slug)]
    async with get_sessionmaker()() as session:
        revisions = (
            await session.scalars(
                select(MemoryRevision)
                .where(MemoryRevision.entry_id == memory.id)
                .order_by(MemoryRevision.revision_number)
            )
        ).all()
    assert revisions[-1].written_by_sub == SYSTEM_OPERATOR_SUB
    assert datetime.fromisoformat(str(revisions[-1].doc_metadata["confirmed_at"])) == used_at


@pytest.mark.asyncio
async def test_a_used_candidate_is_listed_for_a_reviewer_and_never_confirmed() -> None:
    entry, _ = await KbService().create_entry(TENANT, "idea", "body", {"status": "candidate"})
    s1, s2, s3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for session_id, target in ((s1, TARGET), (s2, TARGET_2), (s3, TARGET)):
        await seed_retrieval(
            tenant_id=TENANT, entry_id=entry.id, session_id=session_id, at=NOW - timedelta(hours=2)
        )
        await seed_dispatch(
            tenant_id=TENANT, session_id=session_id, at=NOW - timedelta(hours=1), target_id=target
        )
    # Two searches in one session are one story, not two.
    await seed_retrieval(
        tenant_id=TENANT, entry_id=entry.id, session_id=s1, at=NOW - timedelta(minutes=90)
    )

    reports = await run_upkeep(now=NOW)

    (candidate,) = reports[TENANT].for_review
    assert (candidate.slug, candidate.sessions, candidate.targets) == ("idea", 3, 2)
    stored = await _status("idea")
    assert stored["status"] == "candidate"
    assert "confirmed_at" not in stored


@pytest.mark.asyncio
async def test_old_retrievals_are_deleted() -> None:
    await seed_retrieval(
        tenant_id=TENANT,
        entry_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        at=NOW - timedelta(days=200),
    )
    await seed_retrieval(
        tenant_id=TENANT,
        entry_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        at=NOW - timedelta(days=10),
    )
    await run_upkeep(now=NOW)
    async with get_sessionmaker()() as session:
        left = (await session.scalars(select(LessonRetrieval))).all()
    assert len(left) == 1


@pytest.mark.asyncio
async def test_a_tick_writes_one_report_per_tenant_and_a_total() -> None:
    service = KbService()
    past = (NOW - timedelta(days=1)).isoformat()
    await service.create_entry(TENANT, "old", "old", {"status": "candidate", "expires_at": past})
    await service.create_entry(TENANT, "copy-1", "same")
    await service.create_entry(TENANT, "copy-2", "same")

    await upkeep._run_one_tick()

    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.path == UPKEEP_PATH, AuditLog.operator_sub == SYSTEM_OPERATOR_SUB
                )
            )
        ).all()
    by_tenant = {row.tenant_id: row.payload for row in rows}
    assert {TENANT, UPKEEP_SYSTEM_TENANT_ID} <= set(by_tenant)
    report = by_tenant[TENANT]
    assert report["retired"] == ["old"]
    assert report["merged"] == [{"slug": "copy-2", "into": "copy-1"}]
    assert report["refreshed"] == []
    assert set(report["numbers"]) >= {
        "repeat_mistake_pct",
        "hit_rate_pct",
        "precision_pct",
        "first_correct_action_median_seconds",
    }
    assert by_tenant[UPKEEP_SYSTEM_TENANT_ID]["merged_count"] == 1
    assert by_tenant[UPKEEP_SYSTEM_TENANT_ID]["retired_count"] == 1


def test_settings_default_weekly_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    assert settings.knowledge_upkeep_enabled is True
    assert settings.knowledge_upkeep_interval_seconds == 604800
    monkeypatch.setenv("KNOWLEDGE_UPKEEP_ENABLED", "false")
    get_settings.cache_clear()
    assert get_settings().knowledge_upkeep_enabled is False
    for bad in ("59", "604801"):
        monkeypatch.setenv("KNOWLEDGE_UPKEEP_INTERVAL_SECONDS", bad)
        get_settings.cache_clear()
        with pytest.raises(ValidationError):
            get_settings()


@pytest.mark.asyncio
async def test_the_loop_starts_and_stops_cleanly() -> None:
    task = start_knowledge_upkeep()
    await asyncio.sleep(0)
    assert not task.done()
    await stop_knowledge_upkeep(task)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_the_same_body_in_two_tenants_is_not_merged() -> None:
    other = uuid.UUID("00000000-0000-0000-0000-0000000057e2")
    await KbService().create_entry(TENANT, "note", "same body")
    await KbService().create_entry(other, "note", "same body")
    reports = await run_upkeep(now=NOW)
    assert reports[TENANT].merged == []
    assert "status" not in await _status("note")


@pytest.mark.asyncio
async def test_a_merge_twice_changes_nothing_more() -> None:
    await KbService().create_entry(TENANT, "a", "same")
    await KbService().create_entry(TENANT, "b", "same")
    first = await run_upkeep(now=NOW)
    second = await run_upkeep(now=NOW)
    assert first[TENANT].merged_count == 1
    assert second[TENANT].merged_count == 0


@pytest.mark.asyncio
async def test_an_entry_confirmed_after_the_read_is_not_retired() -> None:
    past = (NOW - timedelta(days=1)).isoformat()
    await KbService().create_entry(
        TENANT, "old", "old", {"status": "candidate", "expires_at": past}
    )
    real_change = upkeep._change_metadata

    async def reviewer_writes_first(doc_id: uuid.UUID, change: object) -> object:
        # A reviewer confirms the entry between the job's read and its write.
        await KbService().create_entry(TENANT, "old", "old", {"status": "confirmed"})
        return await real_change(doc_id, change)  # type: ignore[arg-type]

    with patch.object(upkeep, "_change_metadata", reviewer_writes_first):
        reports = await run_upkeep(now=NOW)

    assert reports[TENANT].retired == []
    assert (await _status("old"))["status"] == "confirmed"


def test_report_lists_are_cut_but_counts_are_complete() -> None:
    changes = upkeep._Changes()
    changes.retired[TENANT] = [f"slug-{i:03}" for i in range(upkeep.REPORT_LIST_LIMIT + 5)]
    numbers = upkeep.LearningMetrics(
        repeat_mistake_pct=None,
        repeat_mistake_failures=0,
        repeat_mistake_repeats=0,
        hit_rate_pct=None,
        hit_rate_retrievals=0,
        hit_rate_used=0,
        precision_pct=None,
        precision_judged=0,
        precision_right=0,
        first_correct_action_median_seconds=None,
        first_correct_action_sessions=0,
    )
    report = upkeep._report(TENANT, changes, since=NOW, now=NOW, numbers=numbers)
    assert report.retired_count == upkeep.REPORT_LIST_LIMIT + 5
    assert len(report.retired) == upkeep.REPORT_LIST_LIMIT


@pytest.mark.asyncio
async def test_the_loop_survives_a_failed_run() -> None:
    sleeps = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    tick = AsyncMock(side_effect=[RuntimeError("database down"), None])
    with (
        patch.object(upkeep, "wait_until_first_run", AsyncMock()),
        patch.object(upkeep, "_run_one_tick", tick),
        patch.object(upkeep.asyncio, "sleep", fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await upkeep._upkeep_loop()
    assert tick.await_count == 2
