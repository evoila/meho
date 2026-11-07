# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The four learning numbers (:mod:`meho_backplane.lessons.metrics`)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.kb.service import KbService
from meho_backplane.lessons.metrics import LearningMetrics, compute_learning_metrics
from meho_backplane.reflex.adoption import compute_reflex_report
from meho_backplane.settings import get_settings
from tests._kb_embedding_stub import fake_embedding_service
from tests._lessons_helpers import seed_dispatch, seed_retrieval

TENANT = uuid.UUID("00000000-0000-0000-0000-0000000057d1")
OTHER = uuid.UUID("00000000-0000-0000-0000-0000000057d2")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SINCE = T0
UNTIL = T0 + timedelta(days=1)
TARGET = uuid.UUID("00000000-0000-0000-0000-00000000e001")
TARGET_2 = uuid.UUID("00000000-0000-0000-0000-00000000e002")


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


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


async def _numbers(tenant_id: uuid.UUID | None = TENANT) -> LearningMetrics:
    async with get_sessionmaker()() as session:
        return await compute_learning_metrics(
            session, since=SINCE, until=UNTIL, tenant_id=tenant_id
        )


@pytest.mark.asyncio
async def test_an_empty_window_reads_none_not_zero() -> None:
    numbers = await _numbers()
    assert numbers.repeat_mistake_pct is None
    assert numbers.hit_rate_pct is None
    assert numbers.precision_pct is None
    assert numbers.first_correct_action_median_seconds is None
    assert numbers.first_correct_action_sessions == 0


@pytest.mark.asyncio
async def test_repeat_mistakes_count_only_across_sessions() -> None:
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    # Before the window, inside the look-back: the first failure.
    await seed_dispatch(
        tenant_id=TENANT,
        session_id=s1,
        at=T0 - timedelta(days=3),
        target_id=TARGET,
        status_code=500,
    )
    # A repeat: same operation, same target, another session.
    await seed_dispatch(
        tenant_id=TENANT, session_id=s2, at=_at(10), target_id=TARGET, status_code=500
    )
    # Not a repeat: the same session again, before any other session failed.
    await seed_dispatch(
        tenant_id=TENANT, session_id=s1, at=_at(5), target_id=TARGET, status_code=404
    )
    # Not a repeat: another target.
    await seed_dispatch(
        tenant_id=TENANT, session_id=s2, at=_at(30), target_id=TARGET_2, status_code=500
    )
    # Not a mistake: a rate limit, and a success.
    await seed_dispatch(
        tenant_id=TENANT, session_id=s2, at=_at(40), target_id=TARGET, status_code=429
    )
    await seed_dispatch(tenant_id=TENANT, session_id=s2, at=_at(50), target_id=TARGET)
    # Another tenant's failure on the same operation and target never counts.
    await seed_dispatch(
        tenant_id=OTHER, session_id=uuid.uuid4(), at=_at(5), target_id=TARGET, status_code=500
    )

    numbers = await _numbers()
    assert numbers.repeat_mistake_failures == 3
    assert numbers.repeat_mistake_repeats == 1
    assert numbers.repeat_mistake_pct == pytest.approx(33.33)


@pytest.mark.asyncio
async def test_hit_rate_counts_retrievals_followed_by_a_success_in_the_session() -> None:
    used_session, idle_session, failed_session = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    entry = uuid.uuid4()
    await seed_retrieval(tenant_id=TENANT, entry_id=entry, session_id=used_session, at=_at(0))
    await seed_dispatch(tenant_id=TENANT, session_id=used_session, at=_at(5), target_id=TARGET)
    # A success before the search does not use it.
    await seed_dispatch(tenant_id=TENANT, session_id=idle_session, at=_at(0))
    await seed_retrieval(tenant_id=TENANT, entry_id=entry, session_id=idle_session, at=_at(1))
    # A failed operation does not use it either.
    await seed_retrieval(tenant_id=TENANT, entry_id=entry, session_id=failed_session, at=_at(0))
    await seed_dispatch(tenant_id=TENANT, session_id=failed_session, at=_at(5), status_code=500)

    numbers = await _numbers()
    assert numbers.hit_rate_retrievals == 3
    assert numbers.hit_rate_used == 1
    assert numbers.hit_rate_pct == pytest.approx(33.33)


@pytest.mark.asyncio
async def test_precision_leaves_out_wrong_and_stale_entries() -> None:
    service = KbService()
    right, _ = await service.create_entry(TENANT, "right", "a", {"status": "confirmed"})
    later_disputed, _ = await service.create_entry(TENANT, "disputed", "b", {"status": "disputed"})
    stale, _ = await service.create_entry(TENANT, "stale", "c", {"status": "candidate"})
    retired, _ = await service.create_entry(TENANT, "retired", "d", {"status": "retired"})
    session_id = uuid.uuid4()
    await seed_retrieval(
        tenant_id=TENANT, entry_id=right.id, session_id=session_id, at=_at(0), status="confirmed"
    )
    # Was confirmed when found, is disputed now: it was wrong.
    await seed_retrieval(
        tenant_id=TENANT,
        entry_id=later_disputed.id,
        session_id=session_id,
        at=_at(0),
        status="confirmed",
    )
    await seed_retrieval(
        tenant_id=TENANT,
        entry_id=stale.id,
        session_id=session_id,
        at=_at(0),
        status="candidate",
        expired=True,
    )
    await seed_retrieval(
        tenant_id=TENANT, entry_id=retired.id, session_id=session_id, at=_at(0), status="retired"
    )
    # A deleted entry cannot be judged.
    await seed_retrieval(tenant_id=TENANT, entry_id=uuid.uuid4(), session_id=session_id, at=_at(0))

    numbers = await _numbers()
    assert numbers.precision_judged == 4
    assert numbers.precision_right == 1
    assert numbers.precision_pct == 25.0


@pytest.mark.asyncio
async def test_time_to_first_correct_action_on_a_known_system() -> None:
    earlier, newcomer, unknown = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # Another session worked on TARGET before: TARGET is known.
    await seed_dispatch(
        tenant_id=TENANT, session_id=earlier, at=T0 - timedelta(days=2), target_id=TARGET
    )
    # The new session starts with a search, fails once, then succeeds 3 minutes in.
    await seed_retrieval(tenant_id=TENANT, entry_id=uuid.uuid4(), session_id=newcomer, at=_at(0))
    await seed_dispatch(
        tenant_id=TENANT, session_id=newcomer, at=_at(1), target_id=TARGET, status_code=500
    )
    await seed_dispatch(tenant_id=TENANT, session_id=newcomer, at=_at(3), target_id=TARGET)
    # A first success on a target nobody worked on before is not counted.
    await seed_dispatch(tenant_id=TENANT, session_id=unknown, at=_at(2), target_id=TARGET_2)

    numbers = await _numbers()
    assert numbers.first_correct_action_sessions == 1
    assert numbers.first_correct_action_median_seconds == 180.0


@pytest.mark.asyncio
async def test_a_target_known_only_from_another_tenant_does_not_count() -> None:
    await seed_dispatch(
        tenant_id=OTHER, session_id=uuid.uuid4(), at=T0 - timedelta(days=2), target_id=TARGET
    )
    await seed_dispatch(tenant_id=TENANT, session_id=uuid.uuid4(), at=_at(3), target_id=TARGET)
    numbers = await _numbers()
    assert numbers.first_correct_action_sessions == 0


@pytest.mark.asyncio
async def test_the_reflex_report_carries_the_learning_numbers() -> None:
    session_id = uuid.uuid4()
    await seed_retrieval(tenant_id=TENANT, entry_id=uuid.uuid4(), session_id=session_id, at=_at(0))
    await seed_dispatch(tenant_id=TENANT, session_id=session_id, at=_at(1))
    async with get_sessionmaker()() as session:
        report = await compute_reflex_report(
            session=session, since=SINCE, until=UNTIL, tenant_id=TENANT
        )
    assert report.learning.hit_rate_used == 1
    assert report.model_dump(mode="json")["learning"]["hit_rate_pct"] == 100.0
