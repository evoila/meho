# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""When the runner drops its set-aside notices, and a mark it cannot write.

* The runner drops the notices of a post only after three client errors in a
  row that span at least ``SET_ASIDE_MIN_SPAN``. Quicker refusals keep them,
  and MEHO shows the check as ``unknown`` once it saves them. These tests run
  the real runner loop against the real results route, with a test clock and
  ticks 60 seconds apart.
* When the runner cannot write the ``notice_done`` mark of a set-aside file,
  it logs one warning with the file name and goes on with its tick: it
  fetches the assignment and runs its due checks.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerAssignmentRow
from meho_backplane.runner import loop as runner_loop
from meho_backplane.runner import wire
from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.loop import (
    NOTICE_DROP_AFTER_TRIES,
    SET_ASIDE_MIN_SPAN,
    RunnerState,
    run_one_tick,
)
from meho_backplane.runner.spool import ResultSpool
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401  (autouse)
    _isolated_jwks_cache,  # noqa: F401  (autouse)
    _settings_env,  # noqa: F401  (autouse)
)
from .test_gateway_assignment_api import (
    _RUNNER_A_ID,
    _TENANT,
    _build_app,
    _runner_token,
    _seed_identities,
)
from .test_runner_checks_count import _KEY, _seed_sensor, _sensor
from .test_runner_set_aside import _every_tick_assignment

pytestmark = pytest.mark.asyncio

_TICK = timedelta(seconds=60)


class _Clock:
    """A clock for the runner loop that moves only when the test moves it."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(runner_loop, "_now", fake)
    return fake


def _set_aside_file(spool: ResultSpool, check_ref: str) -> str:
    """One set-aside batch without the ``notice_done`` mark, as an earlier run left it."""
    batch = wire.RunnerResultBatch(
        runner_id="runner-a",
        results=[
            wire.RunnerResult(
                result_uid="refused_one",
                check_ref=check_ref,
                op_id="op",
                status="ok",
                result={"count": 1},
                produced_at=datetime.now(UTC),
            )
        ],
    )
    path = spool.write_batch(batch)
    spool.set_aside(path, batch, status=422, code="results_refused")
    return path.name


def _notice_done(spool_dir: Path, name: str) -> bool:
    record = json.loads((spool_dir / "set-aside" / name).read_text())
    return bool(record["notice_done"])


class _Front:
    """MEHO behind a front that answers 404 to each post while ``down`` is true."""

    def __init__(self) -> None:
        self.app = TestClient(_build_app())
        self.down = True
        self.answers: list[int] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assignment"):
            return httpx.Response(304)
        if self.down:
            self.answers.append(404)
            return httpx.Response(404)
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
            resp = self.app.post(request.url.path, content=request.content, headers=request.headers)
        self.answers.append(resp.status_code)
        return httpx.Response(resp.status_code, content=resp.content)


async def _seed_runner_check(monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()
    await _seed_identities()
    sensor_id = await _seed_sensor()
    async with get_sessionmaker()() as session:
        session.add(RunnerAssignmentRow(tenant_id=_TENANT, runner_name="runner-a", items=[]))
        await session.commit()
    return sensor_id


async def _ticks_60s_apart(
    front: _Front, spool: ResultSpool, state: RunnerState, clock: _Clock, count: int
) -> None:
    async with RunnerClient(
        central_url="https://central.example",
        runner_id="runner-a",
        token=_runner_token(_KEY, runner_id=_RUNNER_A_ID),
        transport=httpx.MockTransport(front),
    ) as runner:
        for _ in range(count):
            await run_one_tick(client=runner, spool=spool, state=state, runner_id="runner-a")
            clock.now += _TICK


def _drops(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] == "runner_set_aside_notices_dropped"]


# ---------------------------------------------------------------------------
# The minimum time before notices are dropped
# ---------------------------------------------------------------------------


async def test_three_quick_notice_refusals_keep_the_notice_and_meho_saves_it_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    sensor_id = await _seed_runner_check(monkeypatch)
    spool = ResultSpool(tmp_path, max_files=10)
    name = _set_aside_file(spool, str(sensor_id))
    front = _Front()
    state = RunnerState()
    quick = NOTICE_DROP_AFTER_TRIES
    assert (quick - 1) * _TICK < SET_ASIDE_MIN_SPAN

    with capture_logs() as logs:
        await _ticks_60s_apart(front, spool, state, clock, quick)
        assert [n.result_uid for n in state.notices] == ["refused_one"]
        assert not _notice_done(tmp_path, name)

        front.down = False
        await _ticks_60s_apart(front, spool, state, clock, 1)

    assert front.answers == [404] * quick + [200]
    assert not _drops(logs)
    assert _notice_done(tmp_path, name)
    row = await _sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    assert row.last_evidence["reason"] == "result_refused"


async def test_notice_refusals_that_span_the_minimum_time_drop_the_notice_with_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    sensor_id = await _seed_runner_check(monkeypatch)
    spool = ResultSpool(tmp_path, max_files=10)
    name = _set_aside_file(spool, str(sensor_id))
    front = _Front()
    state = RunnerState()
    spanning = int(SET_ASIDE_MIN_SPAN / _TICK) + 1  # the last one 5 minutes after the first

    with capture_logs() as logs:
        await _ticks_60s_apart(front, spool, state, clock, spanning - 1)
        assert not _drops(logs)
        assert [n.result_uid for n in state.notices] == ["refused_one"]

        await _ticks_60s_apart(front, spool, state, clock, 1)

    drops = _drops(logs)
    assert [(e["status"], e["dropped"], e["log_level"]) for e in drops] == [(404, 1, "warning")]
    assert state.notices == []
    assert _notice_done(tmp_path, name)
    row = await _sensor(sensor_id)
    assert row.last_state == "ok"


# ---------------------------------------------------------------------------
# A mark that cannot be written
# ---------------------------------------------------------------------------


@pytest.fixture
def read_only_set_aside(tmp_path: Path) -> Iterator[Path]:
    folder = tmp_path / "set-aside"
    folder.mkdir()
    yield folder
    os.chmod(folder, stat.S_IRWXU)


class _Counting:
    """A fake MEHO that saves every post and counts the assignment fetches."""

    def __init__(self) -> None:
        self.fetches = 0
        self.notice_posts = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assignment"):
            self.fetches += 1
            return httpx.Response(304)
        batch = wire.RunnerResultBatch.model_validate_json(request.content)
        self.notice_posts += 1 if batch.set_aside else 0
        return httpx.Response(200, json={"accepted": len(batch.results), "duplicates": 0})


async def test_a_mark_that_cannot_be_written_leaves_the_tick_going(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_only_set_aside: Path
) -> None:
    spool = ResultSpool(tmp_path, max_files=10)
    name = _set_aside_file(spool, "chk")
    os.chmod(read_only_set_aside, stat.S_IRUSR | stat.S_IXUSR)
    runs: list[str] = []

    async def execute(item: wire.RunnerWorkItem) -> wire.RunnerResult:
        runs.append(item.check_ref)
        return wire.RunnerResult(
            result_uid=f"run_{len(runs)}",
            check_ref=item.check_ref,
            op_id=item.op_id,
            status="ok",
            result={"count": 1},
            produced_at=datetime.now(UTC),
        )

    monkeypatch.setattr(runner_loop, "execute_work_item", execute)
    meho = _Counting()
    state = RunnerState(assignment=_every_tick_assignment("chk"), assignment_version="v1")

    with capture_logs() as logs:
        async with RunnerClient(
            central_url="https://central.example",
            runner_id="runner-a",
            token="tok",
            transport=httpx.MockTransport(meho),
        ) as runner:
            for tick in range(1, 4):
                await run_one_tick(client=runner, spool=spool, state=state, runner_id="runner-a")
                assert (meho.fetches, len(runs)) == (tick, tick)

    failed = [e for e in logs if e["event"] == "runner_set_aside_mark_failed"]
    assert [(e["path"], e["log_level"]) for e in failed] == [(name, "warning")] * 3
    assert meho.notice_posts == 3
    assert not _notice_done(tmp_path, name)
