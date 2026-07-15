# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the satellite-runner tick loop (#2497).

A single tick is driven end-to-end against :class:`httpx.MockTransport`
(central routes land in #2499) and the real executor: it fetches a canned
assignment, executes a ``net.tcp_check`` against a live loopback listener,
and POSTs the result batch. A second test proves that when the fetch
fails, the loop reuses the cached assignment and still executes it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.loop import RunnerState, run_one_tick
from meho_backplane.runner.spool import ResultSpool
from meho_backplane.runner.wire import RunnerAssignment, RunnerPrincipal, RunnerWorkItem

_Handler = Callable[[httpx.Request], httpx.Response]


@asynccontextmanager
async def _loopback_listener() -> AsyncIterator[int]:
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _assignment(port: int, *, version: str) -> RunnerAssignment:
    item = RunnerWorkItem(
        check_ref="c1",
        op_id="net.tcp_check",
        product="net",
        version="1.x",
        impl_id="net-probe",
        handler_ref="meho_backplane.connectors.net.ops.net_tcp_check",
        params={"host": "127.0.0.1", "port": port},
        safety_level="safe",
        principal=RunnerPrincipal(
            sub="s", tenant_id=uuid.uuid4(), tenant_role=TenantRole.READ_ONLY
        ),
    )
    return RunnerAssignment(assignment_version=version, items=[item])


async def _run_tick(handler: _Handler, spool: ResultSpool, state: RunnerState) -> None:
    client = RunnerClient(
        central_url="https://central.example",
        runner_id="r1",
        token="tok",
        transport=httpx.MockTransport(handler),
    )
    async with client:
        await run_one_tick(client=client, spool=spool, state=state, runner_id="r1")


async def test_tick_fetch_execute_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEHO_NETDIAG_PROBE_ALLOWLIST", "127.0.0.1")
    posted: list[str] = []

    async with _loopback_listener() as port:
        assignment_json = _assignment(port, version="v1").model_dump(mode="json")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/assignment"):
                return httpx.Response(200, json=assignment_json)
            posted.append(request.content.decode())
            return httpx.Response(202)

        state = RunnerState()
        await _run_tick(handler, ResultSpool(tmp_path, max_files=100), state)

    assert state.assignment_version == "v1"
    assert state.assignment is not None
    # Exactly one result batch was POSTed, carrying the executed op's ok result.
    assert len(posted) == 1
    body = posted[0].replace(" ", "")
    assert '"status":"ok"' in body
    assert '"connected":true' in body


async def test_fetch_failure_reuses_cached_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEHO_NETDIAG_PROBE_ALLOWLIST", "127.0.0.1")
    posted: list[str] = []

    async with _loopback_listener() as port:

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/assignment"):
                raise httpx.ConnectError("uplink down")
            posted.append(request.content.decode())
            return httpx.Response(202)

        # Prime the cache as if a prior tick had fetched this assignment.
        state = RunnerState(
            assignment=_assignment(port, version="cached-v"),
            assignment_version="cached-v",
        )
        await _run_tick(handler, ResultSpool(tmp_path, max_files=100), state)

    # Cache untouched by the failed fetch; the cached assignment still ran.
    assert state.assignment_version == "cached-v"
    assert len(posted) == 1
    assert '"status":"ok"' in posted[0].replace(" ", "")
