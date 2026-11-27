# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The runner's set-aside notices over a restart, a notice that MEHO refuses, and timing.

* A runner that starts sends the notices of set-aside batches that MEHO has
  not saved, read from its set-aside folder. A notice that MEHO saved is not
  sent again. The first post after a start carries the count of the folder,
  also without a notice.
* After three answers in a row with a client error that a retry does not
  change, over at least ``SET_ASIDE_MIN_SPAN``, the runner drops the notices
  of that post with one warning. Other failures keep them.
* A batch is set aside only when its three refusals span at least
  ``SET_ASIDE_MIN_SPAN``.

The helpers come from :mod:`tests.test_runner_set_aside`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerAssignmentRow
from meho_backplane.runner import loop as runner_loop
from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.loop import (
    NOTICE_DROP_AFTER_TRIES,
    SET_ASIDE_AFTER_TRIES,
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
from .test_runner_checks_count import _KEY
from .test_runner_set_aside import _bad_400, _batch, _Central, _RefusesResults, _ticks

pytestmark = pytest.mark.asyncio


def _set_aside_on_disk(spool: ResultSpool, uid: str) -> str:
    """One batch with the result *uid* in the set-aside folder, as an earlier run left it."""
    path = spool.write_batch(_batch(uid))
    spool.set_aside(path, _batch(uid), status=422, code="results_refused")
    return path.name


def _told(central: _Central) -> list[str]:
    return [n.result_uid for b in central.received for n in b.set_aside]


def _record(tmp_path: Path, name: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads((tmp_path / "set-aside" / name).read_text())
    return loaded


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


# ---------------------------------------------------------------------------
# Notices over a restart
# ---------------------------------------------------------------------------


async def test_a_started_runner_sends_the_notice_of_a_batch_set_aside_before(
    tmp_path: Path,
) -> None:
    spool = ResultSpool(tmp_path, max_files=10)
    name = _set_aside_on_disk(spool, "bad")
    central = _Central()

    await _ticks(central, spool, RunnerState(), 1)

    assert _told(central) == ["bad"]
    assert central.received[0].set_aside_total == 1
    assert _record(tmp_path, name)["notice_done"] is True


async def test_a_notice_that_meho_saved_is_not_sent_again_after_a_restart(
    tmp_path: Path,
) -> None:
    spool = ResultSpool(tmp_path, max_files=10)
    _set_aside_on_disk(spool, "bad")
    central = _Central()
    await _ticks(central, spool, RunnerState(), 2)

    await _ticks(central, spool, RunnerState(), 2)  # a restart

    assert _told(central) == ["bad"]
    assert [b.set_aside_total for b in central.received] == [1, 1]


async def test_a_notice_that_was_not_sent_goes_out_after_a_restart(tmp_path: Path) -> None:
    spool = ResultSpool(tmp_path, max_files=10)
    _set_aside_on_disk(spool, "bad")
    await _ticks(_RefusesResults(notice_answers=[503]), spool, RunnerState(), 1)

    central = _Central()
    await _ticks(central, spool, RunnerState(), 1)

    assert _told(central) == ["bad"]


async def test_the_first_post_after_a_start_carries_the_count_also_without_a_notice(
    tmp_path: Path,
) -> None:
    central = _Central()
    state = RunnerState()

    await _ticks(central, ResultSpool(tmp_path, max_files=10), state, 3)

    assert [(b.results, b.set_aside, b.set_aside_total) for b in central.received] == [([], [], 0)]
    assert state.summary_sent is True


async def test_a_runner_that_starts_with_an_empty_folder_sets_the_stored_count_to_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole way: the runner loop, the results endpoint and the stored count."""
    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()
    await _seed_identities()
    async with get_sessionmaker()() as session:
        session.add(
            RunnerAssignmentRow(
                tenant_id=_TENANT, runner_name="runner-a", items=[], set_aside_count=3
            )
        )
        await session.commit()
    app = TestClient(_build_app())

    async def central(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assignment"):
            return httpx.Response(304)
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
            resp = app.post(request.url.path, content=request.content, headers=request.headers)
        return httpx.Response(resp.status_code, content=resp.content)

    async with RunnerClient(
        central_url="https://central.example",
        runner_id="runner-a",
        token=_runner_token(_KEY, runner_id=_RUNNER_A_ID),
        transport=httpx.MockTransport(central),
    ) as runner:
        await run_one_tick(
            client=runner,
            spool=ResultSpool(tmp_path, max_files=10),
            state=RunnerState(),
            runner_id="runner-a",
        )

    async with get_sessionmaker()() as session:
        row = (await session.execute(select(RunnerAssignmentRow))).scalars().one()
    assert row.set_aside_count == 0


# ---------------------------------------------------------------------------
# A notice post that MEHO refuses
# ---------------------------------------------------------------------------


async def test_a_notice_refused_three_times_with_a_400_is_dropped_with_one_warning(
    tmp_path: Path, clock: _Clock
) -> None:
    spool = ResultSpool(tmp_path, max_files=10)
    name = _set_aside_on_disk(spool, "bad")
    central = _RefusesResults(notice_answers=[400] * 10)
    state = RunnerState()
    half = int(SET_ASIDE_MIN_SPAN.total_seconds()) // 2

    with capture_logs() as logs:
        await _refusal_ticks(central, spool, state, clock, [half] * (NOTICE_DROP_AFTER_TRIES - 1))

    assert state.notices == []
    drops = [e for e in logs if e["event"] == "runner_set_aside_notices_dropped"]
    assert [(e["status"], e["dropped"], e["log_level"]) for e in drops] == [(400, 1, "warning")]
    assert _record(tmp_path, name)["notice_done"] is True

    after_restart = _Central()
    await _ticks(after_restart, spool, RunnerState(), 2)

    assert _told(after_restart) == []
    assert [b.set_aside_total for b in after_restart.received] == [1]  # only the count


@pytest.mark.parametrize("status", [503, 500, 429, 401])
async def test_a_notice_that_gets_a_failure_that_can_pass_is_kept(
    tmp_path: Path, status: int
) -> None:
    spool = ResultSpool(tmp_path, max_files=10)
    _set_aside_on_disk(spool, "bad")
    central = _RefusesResults(notice_answers=[status] * (NOTICE_DROP_AFTER_TRIES + 2))
    state = RunnerState()

    with capture_logs() as logs:
        await _ticks(central, spool, state, NOTICE_DROP_AFTER_TRIES + 2)

    assert [n.result_uid for n in state.notices] == ["bad"]
    assert not [e for e in logs if e["event"] == "runner_set_aside_notices_dropped"]

    await _ticks(central, spool, state, 1)

    assert central.told() == ["bad"]


# ---------------------------------------------------------------------------
# A minimum time for three refusals
# ---------------------------------------------------------------------------


async def _refusal_ticks(
    central: _Central, spool: ResultSpool, state: RunnerState, clock: _Clock, gaps: list[int]
) -> None:
    """One tick, then one more tick after each gap in seconds."""
    await _ticks(central, spool, state, 1)
    for gap in gaps:
        clock.now += timedelta(seconds=gap)
        await _ticks(central, spool, state, 1)


async def test_three_refusals_within_the_minimum_time_keep_the_batch_in_its_place(
    tmp_path: Path, clock: _Clock
) -> None:
    central = _Central(_bad_400())
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    almost = int(SET_ASIDE_MIN_SPAN.total_seconds()) - 1

    await _refusal_ticks(central, spool, RunnerState(), clock, [1, almost - 1])

    assert central.refused_posts == SET_ASIDE_AFTER_TRIES
    assert spool.set_aside_summary()[0] == 0
    assert [r.result_uid for _p, b in spool.load_oldest_first() for r in b.results] == ["bad"]


async def test_three_refusals_that_span_the_minimum_time_set_the_batch_aside(
    tmp_path: Path, clock: _Clock
) -> None:
    central = _Central(_bad_400())
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    span = int(SET_ASIDE_MIN_SPAN.total_seconds())

    await _refusal_ticks(central, spool, RunnerState(), clock, [60, span - 60])

    assert spool.set_aside_summary()[0] == 1
    assert spool.load_oldest_first() == []


async def test_quick_refusals_set_the_batch_aside_once_the_minimum_time_has_passed(
    tmp_path: Path, clock: _Clock
) -> None:
    central = _Central(_bad_400())
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    state = RunnerState()
    await _refusal_ticks(central, spool, state, clock, [1, 1, 1])
    assert spool.set_aside_summary()[0] == 0

    clock.now += SET_ASIDE_MIN_SPAN

    await _ticks(central, spool, state, 1)
    assert spool.set_aside_summary()[0] == 1
