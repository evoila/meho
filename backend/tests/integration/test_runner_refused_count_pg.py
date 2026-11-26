# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two MEHO processes on one PostgreSQL database: a refused result counts once.

Each process is its own Python process (:mod:`tests.integration._meho_process`)
with its own counter, like two backend pods or two uvicorn workers. Both
take the same batches of the runner, one process after the other and both
at the same time:

* a batch whose one result has a runner time 10 minutes ahead of MEHO's
  clock, twice: every post answers 422, and the counter
  ``runner_check_results_total{outcome="future_time"}``, summed over both
  processes, rises by 1;
* a set-aside notice, twice: every post answers 200, the check reads
  ``unknown`` once and one Dashboard mail goes out in all.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerAssignmentRow, SensorResult
from meho_backplane.settings import get_settings
from tests.test_gateway_assignment_api import _TENANT, _seed_identities
from tests.test_runner_checks_count import _result, _seed_dashboard, _seed_sensor, _sensor

from ._meho_process import ProcessReport, run_meho_process
from .conftest import DOCKER_AVAILABLE, SKIP_REASON, pg_engine  # noqa: F401 (fixture)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON),
]


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


async def _bodies() -> tuple[list[dict[str, Any]], uuid.UUID]:
    """The posts of each process, and the check of the set-aside notice."""
    await _seed_identities()
    refused_check = await _seed_sensor(name="refused")
    noticed_check = await _seed_sensor(name="noticed")
    await _seed_dashboard(noticed_check, memo="ok")
    async with get_sessionmaker()() as session:
        session.add(RunnerAssignmentRow(tenant_id=_TENANT, runner_name="runner-a", items=[]))
        await session.commit()
    ahead = datetime.now(UTC) + timedelta(minutes=10)
    refused = {
        "runner_id": "runner-a",
        "results": [_result(refused_check, uid=uuid.uuid4().hex, produced_at=ahead)],
    }
    notice = {
        "runner_id": "runner-a",
        "results": [],
        "set_aside": [
            {
                "check_ref": str(noticed_check),
                "result_uid": uuid.uuid4().hex,
                "status": 422,
                "code": "results_refused",
                "produced_at": datetime.now(UTC).isoformat(),
            }
        ],
        "set_aside_total": 1,
    }
    return [refused, refused, notice, notice], noticed_check


def _run_two_processes(bodies: list[dict[str, Any]], *, together: bool) -> list[ProcessReport]:
    """Run two MEHO processes; *together* makes each post of one wait for the other."""
    ctx = multiprocessing.get_context("spawn")
    out: Any = ctx.Queue()
    barrier = ctx.Barrier(2) if together else None
    processes = [
        ctx.Process(target=run_meho_process, args=(bodies, barrier, out)) for _ in range(2)
    ]
    reports: list[ProcessReport] = []
    if together:
        for process in processes:
            process.start()
        reports = [out.get(timeout=180) for _ in processes]
    else:
        for process in processes:
            process.start()
            reports.append(out.get(timeout=180))
            process.join(timeout=60)
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0
    return reports


async def _unknown_readings(sensor_id: uuid.UUID) -> int:
    async with get_sessionmaker()() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(SensorResult)
            .where(SensorResult.sensor_id == sensor_id, SensorResult.state == "unknown")
        )
    return int(count or 0)


@pytest.mark.parametrize("together", [False, True], ids=["one-after-the-other", "same-time"])
async def test_two_meho_processes_count_a_refused_result_once_and_record_a_notice_once(
    pg_engine: None,  # noqa: F811 (fixture)
    together: bool,
) -> None:
    bodies, noticed_check = await _bodies()

    reports = await asyncio.to_thread(_run_two_processes, bodies, together=together)

    assert [answers for answers, _count, _mails in reports] == [[422, 422, 200, 200]] * 2
    assert sum(count for _answers, count, _mails in reports) == 1
    assert sum(mails for _answers, _count, mails in reports) == 1
    row = await _sensor(noticed_check)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    assert row.last_evidence["reason"] == "result_refused"
    assert await _unknown_readings(noticed_check) == 1
