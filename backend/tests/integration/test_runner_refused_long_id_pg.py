# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres check: a refused result with a long result id never blocks the spool.

MEHO keeps the mark of a counted refused result only for a result id of 1 to
64 lowercase letters, digits and underscores. This test runs the real runner
loop against the real results route on PostgreSQL. A saved batch holds one
result whose runner time is ahead of MEHO's clock and whose id is far longer
than 64 characters. Each post of it answers 422 ``results_refused``. The
runner sets it aside once its refusals span ``SET_ASIDE_MIN_SPAN``, and a
later batch of the same runner then reaches MEHO.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import func, select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerAssignmentRow, RunnerRefusedResult
from meho_backplane.runner import loop as runner_loop
from meho_backplane.runner import wire
from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.loop import SET_ASIDE_MIN_SPAN, RunnerState, run_one_tick
from meho_backplane.runner.spool import ResultSpool
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks
from tests.test_gateway_assignment_api import (
    _OP_SAFE,
    _RUNNER_A_ID,
    _TENANT,
    _build_app,
    _runner_token,
    _seed_identities,
)
from tests.test_runner_checks_count import _KEY, _seed_sensor

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


class _Clock:
    """A clock for the runner loop that moves only when the test moves it."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _batch(sensor_id: uuid.UUID, uid: str, produced_at: datetime) -> wire.RunnerResultBatch:
    return wire.RunnerResultBatch(
        runner_id="runner-a",
        results=[
            wire.RunnerResult(
                result_uid=uid,
                check_ref=str(sensor_id),
                op_id=_OP_SAFE,
                status="ok",
                result={"count": 1},
                produced_at=produced_at,
            )
        ],
    )


class _Meho:
    """The real app; answers each post and keeps the status per result id posted."""

    def __init__(self) -> None:
        self.app = _build_app()
        self.answers: list[tuple[tuple[str, ...], int]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assignment"):
            return httpx.Response(304)
        batch = wire.RunnerResultBatch.model_validate_json(request.content)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://testserver"
        ) as client:
            with respx.mock(assert_all_mocked=False, assert_all_called=False) as mock_router:
                mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
                resp = await client.post(
                    request.url.path, content=request.content, headers=dict(request.headers)
                )
        self.answers.append((tuple(r.result_uid for r in batch.results), resp.status_code))
        return httpx.Response(resp.status_code, content=resp.content)

    def of(self, uid: str) -> list[int]:
        return [status for uids, status in self.answers if uid in uids]


async def test_a_refused_result_with_a_long_id_is_set_aside_and_later_batches_go_on(
    pg_engine: None,  # noqa: F811 (fixture)
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(runner_loop, "_now", clock)
    await _seed_identities()
    sensor_id = await _seed_sensor()
    async with get_sessionmaker()() as session:
        session.add(RunnerAssignmentRow(tenant_id=_TENANT, runner_name="runner-a", items=[]))
        await session.commit()
    long_uid = secrets.token_hex(6_000)  # 12,000 characters, made at run time
    later_uid = uuid.uuid4().hex
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch(sensor_id, long_uid, datetime.now(UTC) + timedelta(minutes=10)))
    spool.write_batch(_batch(sensor_id, later_uid, datetime.now(UTC)))
    meho = _Meho()
    state = RunnerState()
    half = SET_ASIDE_MIN_SPAN / 2

    async with RunnerClient(
        central_url="https://central.example",
        runner_id="runner-a",
        token=_runner_token(_KEY, runner_id=_RUNNER_A_ID),
        transport=httpx.MockTransport(meho),
    ) as runner:
        for _ in range(3):  # the third refusal comes SET_ASIDE_MIN_SPAN after the first
            await run_one_tick(client=runner, spool=spool, state=state, runner_id="runner-a")
            clock.now += half

    assert meho.of(long_uid) == [422, 422, 422]
    assert meho.of(later_uid) == [200]
    assert 500 not in [status for _uids, status in meho.answers]
    assert spool.load_oldest_first() == []
    assert spool.set_aside_summary()[0] == 1
    async with get_sessionmaker()() as session:
        marks = await session.scalar(select(func.count()).select_from(RunnerRefusedResult))
    assert marks == 0
