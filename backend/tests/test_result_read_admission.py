# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admission, worker pool, deadline, and drain for the result reads (#3640).

Drives :mod:`meho_backplane.operations.result_read_admission` through the two
read cores in :mod:`meho_backplane.operations.result_query` against a fake
store (no Valkey), and checks the error mapping on the MCP tool and the REST
route. What these tests pin:

* the dedicated pool is sized from ``result_query_executor_workers``
* admission runs before the store is read: a refused read makes no fetch
* the per-tenant cap and the per-principal rate check refuse extra reads
* one deadline covers the store fetch, not only the ``SELECT``
* a worker that outlives its request keeps its slot until it finishes
* a lease nobody renews is reclaimed by its TTL; owner tokens stop a stale
  release from freeing a newer lease
"""

from __future__ import annotations

import asyncio
import threading
import types
import uuid
from typing import Any

import pytest
from fastapi import HTTPException

import meho_backplane.operations.result_query as rq
import meho_backplane.operations.result_read_admission as admission
from meho_backplane.api.v1 import operations as rest_operations
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import SpilledRowSet, SpilledWindow
from meho_backplane.mcp.server import McpRateLimitedError
from meho_backplane.mcp.tools import result_query as mcp_result_query
from meho_backplane.operations.result_query import ResultQuerySpec
from meho_backplane.settings import Settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000036a0")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000036a1")


def _operator(sub: str = "op-a", tenant_id: uuid.UUID = _TENANT) -> Operator:
    return Operator(
        sub=sub,
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


def _settings(**overrides: Any) -> Settings:
    return Settings(
        keycloak_issuer_url="https://keycloak.test/realms/meho",
        keycloak_audience="meho-backplane",
        vault_addr="https://vault.test",
        database_url="sqlite+aiosqlite:///:memory:",
        **overrides,
    )


def _core_settings(timeout: int = 10) -> Any:
    return types.SimpleNamespace(
        result_query_max_output_rows=500,
        result_query_max_output_bytes=262144,
        result_query_timeout_seconds=timeout,
    )


def _rows(n: int = 5) -> list[dict[str, Any]]:
    return [{"id": i, "name": f"vm-{i}"} for i in range(n)]


class _GatedStore:
    """Fake store whose fetches wait on *gate* and count their calls."""

    def __init__(self, gate: asyncio.Event | None = None, delay: float = 0.0) -> None:
        self.gate = gate
        self.delay = delay
        self.fetch_rows_calls = 0
        self.fetch_window_calls = 0

    async def _wait(self) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)

    async def fetch_rows(self, **_: Any) -> SpilledRowSet:
        self.fetch_rows_calls += 1
        await self._wait()
        return SpilledRowSet(rows=_rows(), total_rows=5, stored_rows=5)

    async def fetch_window(self, *, offset: int, limit: int, **_: Any) -> SpilledWindow:
        self.fetch_window_calls += 1
        await self._wait()
        return SpilledWindow(
            rows=_rows()[offset : offset + limit], total_rows=5, stored_rows=5, truncated=False
        )


class _FakePipeline:
    def __init__(self, store: dict[str, int]) -> None:
        self._store = store
        self._keys: list[str] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def incr(self, name: str) -> None:
        self._keys.append(name)

    def expire(self, name: str, time: int) -> None:
        pass

    async def execute(self) -> list[Any]:
        key = self._keys[0]
        self._store[key] = self._store.get(key, 0) + 1
        return [self._store[key], True]


class _FakeValkey:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self.store)


@pytest.fixture(autouse=True)
def _fresh_admission() -> Any:
    admission.shutdown_result_read_admission()
    yield
    admission.shutdown_result_read_admission()


def _install(monkeypatch: pytest.MonkeyPatch, store: _GatedStore, timeout: int = 10) -> None:
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(rq, "get_settings", lambda: _core_settings(timeout))


async def _wait_until(predicate: Any, seconds: float = 5.0) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


def _state() -> Any:
    return admission._state()


# ---------------------------------------------------------------------------
# Dedicated pool sized from settings
# ---------------------------------------------------------------------------


def test_executor_size_matches_setting() -> None:
    settings = _settings(result_query_executor_workers=6)
    admission.configure_result_read_admission(settings)
    executor = admission.get_result_read_executor()
    assert executor._max_workers == settings.result_query_executor_workers
    assert _state().gate.capacity == 6


def test_unconfigured_pool_uses_the_setting_default() -> None:
    default = Settings.model_fields["result_query_executor_workers"].default
    assert default == 4
    assert admission.get_result_read_executor()._max_workers == default


async def test_query_runs_on_the_dedicated_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _GatedStore())
    seen: list[str] = []
    real = rq._execute_sync

    def _record(engine: Any, compiled: Any) -> Any:
        seen.append(threading.current_thread().name)
        return real(engine, compiled)

    monkeypatch.setattr(rq, "_execute_sync", _record)
    result = await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    assert result["returned_rows"] == 5
    assert seen and seen[0].startswith("result-read")


# ---------------------------------------------------------------------------
# Admission before the store is read
# ---------------------------------------------------------------------------


async def test_query_overflow_is_refused_before_fetch_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    store = _GatedStore(gate)
    _install(monkeypatch, store)
    tasks = [
        asyncio.create_task(rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec()))
        for _ in range(4)
    ]
    await _wait_until(lambda: store.fetch_rows_calls == 4)

    with pytest.raises(rq.ResultReadCapacityError):
        await rq.run_result_query(_operator(sub="op-b"), uuid.uuid4(), ResultQuerySpec())
    assert store.fetch_rows_calls == 4

    gate.set()
    results = await asyncio.gather(*tasks)
    assert all(r["returned_rows"] == 5 for r in results)
    await _wait_until(lambda: _state().gate.in_use == 0)


async def test_paging_overflow_is_refused_before_fetch_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    store = _GatedStore(gate)
    _install(monkeypatch, store)
    tasks = [
        asyncio.create_task(rq.read_result_window(_operator(), uuid.uuid4(), 0, 2))
        for _ in range(4)
    ]
    await _wait_until(lambda: store.fetch_window_calls == 4)

    with pytest.raises(rq.ResultReadCapacityError):
        await rq.read_result_window(_operator(), uuid.uuid4(), 0, 2)
    assert store.fetch_window_calls == 4

    gate.set()
    await asyncio.gather(*tasks)
    assert _state().gate.in_use == 0


async def test_tenant_cap_admits_four_and_refuses_the_fifth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission.configure_result_read_admission(
        _settings(result_query_executor_workers=8, result_query_tenant_concurrency=4)
    )
    gate = asyncio.Event()
    store = _GatedStore(gate)
    _install(monkeypatch, store)
    tasks = [
        asyncio.create_task(rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec()))
        for _ in range(4)
    ]
    await _wait_until(lambda: store.fetch_rows_calls == 4)

    with pytest.raises(rq.ResultReadCapacityError) as excinfo:
        await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    assert excinfo.value.scope == "tenant"
    assert excinfo.value.reason == "capacity_exceeded"
    assert store.fetch_rows_calls == 4

    # Another tenant still has room in the process-wide gate.
    other = asyncio.create_task(
        rq.run_result_query(_operator(tenant_id=_OTHER_TENANT), uuid.uuid4(), ResultQuerySpec())
    )
    await _wait_until(lambda: store.fetch_rows_calls == 5)

    gate.set()
    await asyncio.gather(*tasks, other)


async def test_rate_check_is_shared_by_paging_and_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission.configure_result_read_admission(_settings(result_query_rate_per_minute=2))
    valkey = _FakeValkey()
    monkeypatch.setattr(admission, "get_broadcast_client", lambda: valkey)
    store = _GatedStore()
    _install(monkeypatch, store)

    await rq.read_result_window(_operator(), uuid.uuid4(), 0, 2)
    await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    with pytest.raises(rq.ResultReadRateLimitError) as excinfo:
        await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())

    assert excinfo.value.reason == "rate_limited"
    assert 0 < excinfo.value.retry_after_seconds <= 60
    assert (store.fetch_window_calls, store.fetch_rows_calls) == (1, 1)
    assert _state().gate.in_use == 0
    # Another principal has its own counter.
    await rq.run_result_query(_operator(sub="op-b"), uuid.uuid4(), ResultQuerySpec())


async def test_rate_check_off_makes_no_valkey_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_client() -> Any:
        raise AssertionError("rate check must not reach Valkey when the cap is 0")

    monkeypatch.setattr(admission, "get_broadcast_client", _no_client)
    _install(monkeypatch, _GatedStore())
    await rq.read_result_window(_operator(), uuid.uuid4(), 0, 2)


# ---------------------------------------------------------------------------
# Whole-pipeline deadline
# ---------------------------------------------------------------------------


async def test_slow_fetch_rows_hits_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _GatedStore(delay=30)
    _install(monkeypatch, store, timeout=1)
    async with asyncio.timeout(5):
        with pytest.raises(rq.ResultQueryTimeoutError):
            await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    assert store.fetch_rows_calls == 1
    # No worker was started, so the slot is free at once.
    assert _state().gate.in_use == 0
    assert _state().leases.count(_TENANT) == 0


# ---------------------------------------------------------------------------
# Shielded drain: a running worker keeps its slot until it finishes
# ---------------------------------------------------------------------------


def _blocking_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    started, release = threading.Event(), threading.Event()
    real = rq._execute_sync

    def _blocked(engine: Any, compiled: Any) -> Any:
        started.set()
        release.wait(10)
        return real(engine, compiled)

    monkeypatch.setattr(rq, "_execute_sync", _blocked)
    return started, release


async def test_cancelled_query_keeps_its_lease_until_the_worker_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _GatedStore())
    started, release = _blocking_execute(monkeypatch)
    task = asyncio.create_task(rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec()))
    await _wait_until(started.is_set)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The worker thread is still running: its slot and lease stay held.
    assert _state().gate.in_use == 1
    assert _state().leases.count(_TENANT) == 1

    release.set()
    await _wait_until(lambda: _state().gate.in_use == 0)
    assert _state().leases.count(_TENANT) == 0


async def test_timed_out_query_returns_at_once_and_drains_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _GatedStore(), timeout=1)
    _, release = _blocking_execute(monkeypatch)
    async with asyncio.timeout(5):
        with pytest.raises(rq.ResultQueryTimeoutError):
            await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    assert _state().gate.in_use == 1
    assert _state().leases.count(_TENANT) == 1

    release.set()
    await _wait_until(lambda: _state().leases.count(_TENANT) == 0)
    assert _state().gate.in_use == 0


async def test_draining_worker_renews_its_tenant_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admission, "_TENANT_LEASE_TTL_SECONDS", 0.2)
    monkeypatch.setattr(admission, "_TENANT_LEASE_RENEW_SECONDS", 0.05)
    admission.configure_result_read_admission(
        _settings(result_query_executor_workers=4, result_query_tenant_concurrency=1)
    )
    _install(monkeypatch, _GatedStore())
    started, release = _blocking_execute(monkeypatch)
    task = asyncio.create_task(rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec()))
    await _wait_until(started.is_set)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Well past the TTL, the renewed lease still blocks a second read.
    await asyncio.sleep(0.5)
    with pytest.raises(rq.ResultReadCapacityError):
        await rq.read_result_window(_operator(), uuid.uuid4(), 0, 2)

    release.set()
    await _wait_until(lambda: _state().leases.count(_TENANT) == 0)
    await rq.read_result_window(_operator(), uuid.uuid4(), 0, 2)


def test_unrenewed_lease_is_reclaimed_by_its_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admission, "_TENANT_LEASE_TTL_SECONDS", 0.0)
    leases = admission._TenantLeases()
    stale = leases.acquire(_TENANT, cap=1)
    assert stale is not None
    fresh = leases.acquire(_TENANT, cap=1)
    assert fresh is not None and fresh != stale


def test_stale_owner_cannot_release_a_newer_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admission, "_TENANT_LEASE_TTL_SECONDS", 0.0)
    leases = admission._TenantLeases()
    stale = leases.acquire(_TENANT, cap=1)
    fresh = leases.acquire(_TENANT, cap=1)
    assert stale is not None and fresh is not None

    leases.release(_TENANT, stale)
    assert leases.count(_TENANT) == 1


# ---------------------------------------------------------------------------
# Transport mapping: recoverable MCP error and REST 429
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (admission.ResultReadCapacityError("process"), "capacity_exceeded"),
        (admission.ResultReadRateLimitError(limit=5, retry_after_seconds=12), "rate_limited"),
    ],
)
@pytest.mark.parametrize("arguments", [{}, {"query": {}}], ids=["paging", "query"])
async def test_mcp_maps_a_refused_read_to_a_recoverable_error(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    reason: str,
    arguments: dict[str, Any],
) -> None:
    async def _refuse(*_: Any, **__: Any) -> Any:
        raise error

    monkeypatch.setattr(mcp_result_query, "read_result_window", _refuse)
    monkeypatch.setattr(mcp_result_query, "run_result_query", _refuse)
    with pytest.raises(McpRateLimitedError) as excinfo:
        await mcp_result_query._result_query_handler(
            _operator(), {"handle_id": str(uuid.uuid4()), **arguments}
        )
    assert excinfo.value.data is not None
    assert excinfo.value.data["reason"] == reason
    assert excinfo.value.data["retry_after_seconds"] >= 1


@pytest.mark.parametrize("query", [None, ResultQuerySpec()], ids=["paging", "query"])
async def test_rest_maps_a_refused_read_to_429(
    monkeypatch: pytest.MonkeyPatch, query: ResultQuerySpec | None
) -> None:
    async def _refuse(*_: Any, **__: Any) -> Any:
        raise admission.ResultReadRateLimitError(limit=5, retry_after_seconds=12)

    monkeypatch.setattr(rest_operations, "read_result_window", _refuse)
    monkeypatch.setattr(rest_operations, "run_result_query", _refuse)
    body = rest_operations.ResultQueryBody(handle_id=uuid.uuid4(), query=query)
    with pytest.raises(HTTPException) as excinfo:
        await rest_operations.post_result_query(body, _operator())
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers == {"Retry-After": "12"}
    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail["reason"] == "rate_limited"
