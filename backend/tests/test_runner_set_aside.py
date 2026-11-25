# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A batch that MEHO refuses on every try is set aside; later results go on.

The runner keeps a saved batch in its place while a send can still pass (no
connection, a time-out, a 5xx answer, a rate limit, a login to renew), so
results reach MEHO in order. A client error that a retry does not change sets
the batch aside after three tries, with the status and the code. The runner
then tells MEHO, and the checks of those results read ``unknown``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerAssignmentRow
from meho_backplane.gateway.repository import get_set_aside_markers
from meho_backplane.gateway.runner_checks import record_set_aside
from meho_backplane.runner import wire
from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.loop import SET_ASIDE_AFTER_TRIES, RunnerState, run_one_tick
from meho_backplane.runner.spool import ResultSpool

from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401  (autouse)
    _isolated_jwks_cache,  # noqa: F401  (autouse)
    _settings_env,  # noqa: F401  (autouse)
)
from .test_gateway_assignment_api import _TENANT, _seed_identities
from .test_runner_checks_count import _seed_sensor, _sensor


def _result(uid: str, check_ref: str = "chk") -> wire.RunnerResult:
    return wire.RunnerResult(
        result_uid=uid,
        check_ref=check_ref,
        op_id="op",
        status="ok",
        result={"count": 1},
        produced_at=datetime.now(UTC),
    )


def _batch(*uids: str) -> wire.RunnerResultBatch:
    return wire.RunnerResultBatch(runner_id="runner-a", results=[_result(u) for u in uids])


class _Central:
    """A fake central: answers each post of a batch that holds ``bad`` with *bad_answer*."""

    def __init__(self, bad_answer: httpx.Response | None = None) -> None:
        self.bad_answer = bad_answer
        self.received: list[wire.RunnerResultBatch] = []
        self.refused_posts = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assignment"):
            return httpx.Response(304)
        batch = wire.RunnerResultBatch.model_validate_json(request.content)
        if self.bad_answer is not None and any(r.result_uid == "bad" for r in batch.results):
            self.refused_posts += 1
            return self.bad_answer
        self.received.append(batch)
        return httpx.Response(200, json={"accepted": len(batch.results), "duplicates": 0})

    def uids(self) -> list[str]:
        return [r.result_uid for b in self.received for r in b.results]


def _client(central: _Central) -> RunnerClient:
    return RunnerClient(
        central_url="https://central.example",
        runner_id="runner-a",
        token="tok",
        transport=httpx.MockTransport(central),
    )


async def _ticks(central: _Central, spool: ResultSpool, state: RunnerState, count: int) -> None:
    async with _client(central) as runner:
        for _ in range(count):
            await run_one_tick(client=runner, spool=spool, state=state, runner_id="runner-a")


def _bad_400() -> httpx.Response:
    return httpx.Response(400, json={"detail": {"code": "results_refused", "note": "free text"}})


@pytest.mark.asyncio
async def test_a_batch_refused_every_time_is_set_aside_and_later_results_go_on(
    tmp_path: Path,
) -> None:
    central = _Central(_bad_400())
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    spool.write_batch(_batch("next-1"))
    spool.write_batch(_batch("next-2"))
    state = RunnerState()

    await _ticks(central, spool, state, SET_ASIDE_AFTER_TRIES - 1)
    assert central.uids() == []  # still waiting in its place

    await _ticks(central, spool, state, 1)

    assert central.uids() == ["next-1", "next-2"]  # in order
    assert spool.load_oldest_first() == []
    count, newest = spool.set_aside_summary()
    assert count == 1 and newest is not None
    kept = next((tmp_path / "set-aside").glob("*.json"))
    record = json.loads(kept.read_text())
    assert record["status"] == 400
    assert record["code"] is None or record["code"] == "results_refused"
    assert "free text" not in kept.read_text()
    assert [r["result_uid"] for r in record["batch"]["results"]] == ["bad"]


@pytest.mark.asyncio
async def test_the_runner_tells_central_about_the_set_aside_results(tmp_path: Path) -> None:
    central = _Central(httpx.Response(400, json={"detail": {"code": "results_refused"}}))
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    state = RunnerState()

    await _ticks(central, spool, state, SET_ASIDE_AFTER_TRIES)

    notices = [n for b in central.received for n in b.set_aside]
    assert [(n.result_uid, n.status, n.code) for n in notices] == [("bad", 400, "results_refused")]
    assert central.received[-1].set_aside_total == 1
    assert state.notices == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503, 401, 403, 408, 429])
async def test_a_failure_that_can_pass_keeps_its_place_and_holds_later_results_back(
    tmp_path: Path, status: int
) -> None:
    central = _Central(httpx.Response(status))
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    spool.write_batch(_batch("next"))
    state = RunnerState()

    await _ticks(central, spool, state, SET_ASIDE_AFTER_TRIES + 3)

    assert central.uids() == []
    assert len(spool.load_oldest_first()) == 2
    assert spool.set_aside_summary()[0] == 0


@pytest.mark.asyncio
async def test_no_connection_keeps_the_place(tmp_path: Path) -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    spool.write_batch(_batch("next"))
    state = RunnerState()
    async with RunnerClient(
        central_url="https://central.example",
        runner_id="runner-a",
        token="tok",
        transport=httpx.MockTransport(down),
    ) as runner:
        for _ in range(SET_ASIDE_AFTER_TRIES + 2):
            await run_one_tick(client=runner, spool=spool, state=state, runner_id="runner-a")

    assert len(spool.load_oldest_first()) == 2
    assert spool.set_aside_summary()[0] == 0


@pytest.mark.asyncio
async def test_a_set_aside_batch_is_not_sent_again(tmp_path: Path) -> None:
    central = _Central(_bad_400())
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    state = RunnerState()
    await _ticks(central, spool, state, SET_ASIDE_AFTER_TRIES)
    posts_before = central.refused_posts

    await _ticks(central, spool, state, 3)

    assert central.refused_posts == posts_before


@pytest.mark.asyncio
async def test_central_shows_the_checks_of_refused_results_as_unknown_and_keeps_the_count() -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    async with get_sessionmaker()() as session:
        session.add(RunnerAssignmentRow(tenant_id=_TENANT, runner_name="runner-a", items=[]))
        await session.commit()
    notice = wire.SetAsideNotice(
        check_ref=str(sensor_id),
        result_uid="bad",
        status=400,
        code="results_refused",
        produced_at=datetime.now(UTC),
    )
    when = datetime.now(UTC)

    for _ in range(2):  # a notice sent twice writes once
        await record_set_aside(
            tenant_id=_TENANT, runner_name="runner-a", notices=[notice], total=2, last_at=when
        )

    row = await _sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    assert row.last_evidence["reason"] == "result_refused"
    assert row.last_evidence["http_status"] == 400
    async with get_sessionmaker()() as session:
        markers = await get_set_aside_markers(session, tenant_id=_TENANT)
        stored = (await session.execute(select(RunnerAssignmentRow))).scalars().one()
    assert markers["runner-a"][0] == 2
    assert stored.set_aside_last_at is not None
