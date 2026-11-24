# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tool calls sent at the same moment at the start of an MCP session (#6829).

An agent client once sent two tool calls at the same moment as the first
calls of a new session, and one of them ended with "Connection closed".
These tests start the app on a real local HTTP server and send many such
calls. For each group they count the answers, the JSON-RPC errors, the
HTTP errors and the closed connections with no answer. The last must be
zero.

A group is one number of calls (1, 2, 5, 20), one way of connecting (a new
connection for each call, or a pool of kept-open connections), and one
pause between ``notifications/initialized`` and the first calls (none, a
short random one, or one just around the server's keep-alive time).

The default size is small, for CI. For the big run:

    MEHO_MCP_PARALLEL_FULL=1 uv run pytest -s -o tmp_path_retention_policy=failed \\
        tests/test_mcp_parallel_first_calls.py

The odd cases and the log line of a call that ends with no answer are
checked in ``tests/test_mcp_parallel_first_calls_cases.py``.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

from meho_backplane.settings import get_settings
from tests.mcp_live_server import (
    CLIENT_IDLE_LIMIT,
    LiveServer,
    Tally,
    live_server,
    new_client,
    run_session,
    tool_call,
)
from tests.mcp_test_fixtures import (
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

FULL = os.environ.get("MEHO_MCP_PARALLEL_FULL") == "1"

#: The big run uses the real times (the server and its clients both use 5
#: seconds on a default start command). The CI form scales every time down by
#: the same factor, so the pause "around the keep-alive time" stays short.
SCALE = 1.0 if FULL else 0.06
CLIENT_IDLE_LIMIT_SECONDS = CLIENT_IDLE_LIMIT * SCALE

CALL_COUNTS = (1, 2, 5, 20)
WAYS = ("new connection", "pooled")
PAUSES = ("none", "short", "keep-alive")

#: Sessions for each group, by the number of calls: the big run, the big run
#: with the long pause (a session is slow then), and the CI form.
SESSIONS_FULL = {1: 200, 2: 700, 5: 100, 20: 30}
SESSIONS_FULL_KEEP_ALIVE = {1: 50, 2: 150, 5: 40, 20: 10}
SESSIONS_CI = {1: 1, 2: 4, 5: 1, 20: 1}

#: Working-surface tools that need no set-up beyond the test database.
TOOLS = ("meho_broadcast_recent", "list_targets")


def _pause(name: str) -> tuple[float, float]:
    if name == "none":
        return (0.0, 0.0)
    if name == "short":
        return (0.0, 0.3 * SCALE)
    return (CLIENT_IDLE_LIMIT_SECONDS - 0.05 * SCALE, CLIENT_IDLE_LIMIT_SECONDS + 0.05 * SCALE)


def _session_count(calls: int, pause: str) -> int:
    if not FULL:
        return SESSIONS_CI[calls]
    return (SESSIONS_FULL_KEEP_ALIVE if pause == "keep-alive" else SESSIONS_FULL)[calls]


async def _run_group(server: LiveServer, calls: int, way: str, pause: str) -> tuple[Tally, float]:
    tally = Tally()
    started = time.monotonic()
    gate = asyncio.Semaphore(40)
    shared = new_client(server, pooled=True) if way == "pooled" else None

    async def _one(index: int) -> None:
        batch = [tool_call(TOOLS[(index + i) % len(TOOLS)], call_id=i + 1) for i in range(calls)]
        async with gate:
            await run_session(server, shared, batch, _pause(pause), tally)

    try:
        await asyncio.gather(*(_one(i) for i in range(_session_count(calls, pause))))
    finally:
        if shared is not None:
            await shared.aclose()
    return tally, time.monotonic() - started


async def test_parallel_first_calls_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every call gets an answer or a JSON-RPC error. No connection ends with no answer."""
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    rows: list[str] = []
    failures: list[str] = []
    started = time.monotonic()
    async with live_server(SCALE) as server:
        for calls in CALL_COUNTS:
            for way in WAYS:
                for pause in PAUSES:
                    with capture_logs(processors=[merge_contextvars]) as logs:
                        tally, seconds = await _run_group(server, calls, way, pause)
                    other = {k: v for k, v in tally.outcomes.items() if k != "answer"}
                    rows.append(
                        f"{calls:>2}  {way:<14} {pause:<10} sessions={tally.sessions:>5} "
                        f"answers={tally.outcomes['answer']:>6} other={other or 0} "
                        f"time={seconds:5.1f}s"
                    )
                    for kind, text in tally.samples.items():
                        print(f"sample of {kind}: {text}")
                    # A JSON-RPC error is an answer too.
                    lost = {k: v for k, v in other.items() if not k.startswith("rpc_error")}
                    if lost:
                        failures.append(f"{calls} {way} {pause}: {lost}")
                        lines = [
                            entry
                            for entry in logs
                            if entry.get("mcp_session_id") in tally.closed_sessions
                        ]
                        print(f"server log lines of the closed sessions: {lines}")
    table = "\n".join(rows)
    print(f"\ncalls way            pause\n{table}")
    print(f"total time {time.monotonic() - started:.1f}s")
    assert not failures, failures
