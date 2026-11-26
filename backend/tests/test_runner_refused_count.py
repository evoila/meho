# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A refused runner result is counted once, by a mark in the database.

MEHO refuses a result whose runner time is too far ahead of its clock. The
counter ``runner_check_results_total{outcome="future_time"}`` rises once for
it: the mark that it was counted is a row in ``runner_refused_results``, so
the count holds for every MEHO process on the database and has no limit of
results kept in memory. The clean-up job of the runner results removes old
marks. The test with two MEHO processes on one PostgreSQL database is in
:mod:`tests.integration.test_runner_refused_count_pg`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from meho_backplane.checks.evidence_retention import _delete_runner_results_older_than
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerRefusedResult
from meho_backplane.gateway.runner_checks import ScreenedBatch, ingest_and_count
from meho_backplane.runner.wire import RefusedResult

from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401  (autouse)
    _isolated_jwks_cache,  # noqa: F401  (autouse)
    _settings_env,  # noqa: F401  (autouse)
)
from .test_gateway_assignment_api import _TENANT, _seed_identities
from .test_runner_checks_count import _outcome

pytestmark = pytest.mark.asyncio


async def _refuse(*uids: str, runner: str = "runner-a") -> None:
    await ingest_and_count(
        tenant_id=_TENANT,
        runner_name=runner,
        screened=ScreenedBatch(
            results=[], refused=[RefusedResult(result_uid=u, reason="future_time") for u in uids]
        ),
        received_at=datetime.now(UTC),
    )


async def _marks() -> int:
    async with get_sessionmaker()() as session:
        return int(await session.scalar(select(func.count()).select_from(RunnerRefusedResult)) or 0)


async def test_a_refused_result_sent_again_after_10000_others_is_not_counted_again() -> None:
    await _seed_identities()
    before = _outcome("future_time")
    await _refuse("first")
    await _refuse(*(f"other{n}" for n in range(10_000)))
    assert _outcome("future_time") - before == 10_001

    await _refuse("first")

    assert _outcome("future_time") - before == 10_001


async def test_each_runner_and_each_result_id_is_counted_once() -> None:
    await _seed_identities()
    before = _outcome("future_time")

    await _refuse("same", "same", "other")
    await _refuse("same", runner="runner-b")
    await _refuse("same", "other")

    assert _outcome("future_time") - before == 3
    assert await _marks() == 3


async def test_the_clean_up_job_removes_old_marks_and_keeps_new_ones() -> None:
    await _seed_identities()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        for uid, at in (("old", now - timedelta(days=40)), ("new", now)):
            session.add(
                RunnerRefusedResult(
                    tenant_id=_TENANT, runner_name="runner-a", result_uid=uid, refused_at=at
                )
            )
        await session.commit()

    await _delete_runner_results_older_than(now - timedelta(days=30))

    async with get_sessionmaker()() as session:
        kept = (await session.scalars(select(RunnerRefusedResult.result_uid))).all()
    assert kept == ["new"]
