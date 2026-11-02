# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admission, worker pool, deadline, and drain for the result reads (#3640).

Drives :mod:`meho_backplane.operations.result_read_admission` through the
read cores in :mod:`meho_backplane.operations.result_query` against the
production store over an in-memory client (no Valkey), and checks the error
mapping on the MCP tool and the REST route. What these tests pin:

* the dedicated pool is sized from ``result_query_executor_workers``
* admission runs before the store is read: a refused read makes no fetch
* the per-tenant cap and the per-principal rate check refuse extra reads
* one deadline covers the store fetch on every read path (paging, query,
  ``path``), not only the ``SELECT``
* the record decode runs on the worker pool: a slow decode leaves the
  event loop free and keeps its slot until it finishes
* the response build and its wire-size check run on the worker pool too,
  under the same deadline
* a worker that outlives its request keeps its slot until it finishes
* a lease nobody renews is reclaimed by its TTL; owner tokens stop a stale
  release from freeing a newer lease
"""

from __future__ import annotations

import asyncio
import threading
import time
import types
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import HTTPException

import meho_backplane.operations.result_query as rq
import meho_backplane.operations.result_read_admission as admission
from meho_backplane.api.v1 import operations as rest_operations
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.server import McpInvalidParamsError, McpRateLimitedError
from meho_backplane.mcp.tools import result_query as mcp_result_query
from meho_backplane.operations.result_query import ResultQuerySpec
from meho_backplane.settings import Settings
from tests._result_store_helpers import MemoryResultStore

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
        result_query_response_wire_max_bytes=2097152,
        result_query_timeout_seconds=timeout,
    )


def _rows(n: int = 5) -> list[dict[str, Any]]:
    return [{"id": i, "name": f"vm-{i}"} for i in range(n)]


def _store(gate: asyncio.Event | None = None, delay: float = 0.0) -> MemoryResultStore:
    """The production store over an in-memory client, serving rows for any handle.

    *gate* and *delay* hold every ``GET``; ``store.client.get_calls`` counts
    them, which is how a test shows a refused read never reached the store.
    """
    store = MemoryResultStore()
    store.seed_any_handle(operator_sub="op-a", rows=_rows())
    store.client.gate = gate
    store.client.delay = delay
    return store


#: One read per path, each over a fresh handle. The ``path`` read needs a
#: document record, which ``_seed_for`` stores under that handle.
_READS: dict[str, Callable[[Operator, uuid.UUID], Awaitable[dict[str, Any]]]] = {
    "paging": lambda op, h: rq.read_result_window(op, h, 0, 2),
    "query": lambda op, h: rq.run_result_query(op, h, ResultQuerySpec()),
    "path": lambda op, h: rq.run_result_query(op, h, ResultQuerySpec(path="/name")),
}
#: The store method each path decodes its record with.
_DECODERS = {"paging": "decode_window", "query": "decode_rows", "path": "decode_document"}


def _seed_for(store: MemoryResultStore, path: str, handle_id: uuid.UUID) -> None:
    if path == "path":
        store.seed_document(
            tenant_id=_TENANT, operator_sub="op-a", handle_id=handle_id, document={"name": "vm"}
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


def _freeze_rate_clock(monkeypatch: pytest.MonkeyPatch, now: float) -> list[float]:
    """Pin the wall clock the rate check reads; set ``clock[0]`` to move it.

    The rate window is part of the Valkey key, so a test on the real clock
    lands in two windows when it runs across a minute boundary.
    """
    clock = [now]
    fake_time = types.SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic)
    monkeypatch.setattr(admission, "time", fake_time)
    return clock


def _install(monkeypatch: pytest.MonkeyPatch, store: MemoryResultStore, timeout: int = 10) -> None:
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
    _install(monkeypatch, _store())
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
    store = _store(gate)
    _install(monkeypatch, store)
    tasks = [
        asyncio.create_task(rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec()))
        for _ in range(4)
    ]
    await _wait_until(lambda: store.client.get_calls == 4)

    with pytest.raises(rq.ResultReadCapacityError):
        await rq.run_result_query(_operator(sub="op-b"), uuid.uuid4(), ResultQuerySpec())
    assert store.client.get_calls == 4

    gate.set()
    results = await asyncio.gather(*tasks)
    assert all(r["returned_rows"] == 5 for r in results)
    await _wait_until(lambda: _state().gate.in_use == 0)


async def test_paging_overflow_is_refused_before_fetch_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    store = _store(gate)
    _install(monkeypatch, store)
    tasks = [
        asyncio.create_task(rq.read_result_window(_operator(), uuid.uuid4(), 0, 2))
        for _ in range(4)
    ]
    await _wait_until(lambda: store.client.get_calls == 4)

    with pytest.raises(rq.ResultReadCapacityError):
        await rq.read_result_window(_operator(), uuid.uuid4(), 0, 2)
    assert store.client.get_calls == 4

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
    store = _store(gate)
    _install(monkeypatch, store)
    tasks = [
        asyncio.create_task(rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec()))
        for _ in range(4)
    ]
    await _wait_until(lambda: store.client.get_calls == 4)

    with pytest.raises(rq.ResultReadCapacityError) as excinfo:
        await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    assert excinfo.value.scope == "tenant"
    assert excinfo.value.reason == "capacity_exceeded"
    assert store.client.get_calls == 4

    # Another tenant still has room in the process-wide gate.
    other = asyncio.create_task(
        rq.run_result_query(_operator(tenant_id=_OTHER_TENANT), uuid.uuid4(), ResultQuerySpec())
    )
    await _wait_until(lambda: store.client.get_calls == 5)

    gate.set()
    await asyncio.gather(*tasks, other)


async def test_rate_check_is_shared_by_paging_and_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission.configure_result_read_admission(_settings(result_query_rate_per_minute=2))
    valkey = _FakeValkey()
    monkeypatch.setattr(admission, "get_broadcast_client", lambda: valkey)
    _freeze_rate_clock(monkeypatch, 1_800_000_000.0)
    store = _store()
    _install(monkeypatch, store)

    await rq.read_result_window(_operator(), uuid.uuid4(), 0, 2)
    await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    with pytest.raises(rq.ResultReadRateLimitError) as excinfo:
        await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())

    assert excinfo.value.reason == "rate_limited"
    assert excinfo.value.retry_after_seconds == 60
    assert store.client.get_calls == 2
    assert _state().gate.in_use == 0
    # Another principal has its own counter.
    other_handle = uuid.uuid4()
    store.seed(tenant_id=_TENANT, operator_sub="op-b", handle_id=other_handle, rows=_rows())
    await rq.run_result_query(_operator(sub="op-b"), other_handle, ResultQuerySpec())


async def test_rate_count_starts_again_in_the_next_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission.configure_result_read_admission(_settings(result_query_rate_per_minute=2))
    valkey = _FakeValkey()
    monkeypatch.setattr(admission, "get_broadcast_client", lambda: valkey)
    clock = _freeze_rate_clock(monkeypatch, 1_800_000_059.0)
    _install(monkeypatch, _store())

    await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    with pytest.raises(rq.ResultReadRateLimitError) as excinfo:
        await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    assert excinfo.value.retry_after_seconds == 1

    # One second later a new window starts, and the count starts at zero.
    clock[0] += 1
    await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    with pytest.raises(rq.ResultReadRateLimitError):
        await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())


async def test_rate_check_off_makes_no_valkey_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_client() -> Any:
        raise AssertionError("rate check must not reach Valkey when the cap is 0")

    monkeypatch.setattr(admission, "get_broadcast_client", _no_client)
    _install(monkeypatch, _store())
    await rq.read_result_window(_operator(), uuid.uuid4(), 0, 2)


# ---------------------------------------------------------------------------
# Whole-pipeline deadline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", list(_READS), ids=list(_READS))
async def test_slow_fetch_hits_the_deadline_on_every_path(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A store fetch past the budget is a timeout on paging, query, and ``path``."""
    store = _store(delay=30)
    _install(monkeypatch, store, timeout=1)
    handle_id = uuid.uuid4()
    _seed_for(store, path, handle_id)
    async with asyncio.timeout(5):
        with pytest.raises(rq.ResultQueryTimeoutError) as excinfo:
            await _READS[path](_operator(), handle_id)
    assert excinfo.value.timeout_seconds == 1
    assert "limit" in str(excinfo.value)
    assert store.client.get_calls == 1
    # No worker was started, so the slot is free at once.
    assert _state().gate.in_use == 0
    assert _state().leases.count(_TENANT) == 0


def _blocking_decoder(
    monkeypatch: pytest.MonkeyPatch, store: MemoryResultStore, name: str
) -> tuple[threading.Event, threading.Event]:
    """Make the store's *name* decoder block until *release* is set."""
    started, release = threading.Event(), threading.Event()
    real = getattr(store, name)

    def _blocked(raw: bytes, **kwargs: Any) -> Any:
        started.set()
        release.wait(10)
        return real(raw, **kwargs)

    monkeypatch.setattr(store, name, _blocked)
    return started, release


@pytest.mark.parametrize("path", list(_READS), ids=list(_READS))
async def test_slow_decode_runs_off_the_loop_and_keeps_its_slot(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The record decode is worker work: the loop stays free while it blocks.

    While the decoder blocks, the event loop still runs (another read is
    admitted and completes at once), the slot and the tenant lease stay
    held, and a cancelled request leaves them held until the decoder
    finishes.
    """
    store = _store()
    _install(monkeypatch, store)
    handle_id = uuid.uuid4()
    _seed_for(store, path, handle_id)
    started, release = _blocking_decoder(monkeypatch, store, _DECODERS[path])

    task = asyncio.create_task(_READS[path](_operator(), handle_id))
    await _wait_until(started.is_set)
    # A read on another path (its decoder is not blocked) goes through at once.
    probe = "query" if path == "paging" else "paging"
    async with asyncio.timeout(5):
        await _READS[probe](_operator(tenant_id=_OTHER_TENANT), uuid.uuid4())
    assert _state().gate.in_use == 1
    assert _state().leases.count(_TENANT) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _state().gate.in_use == 1
    assert _state().leases.count(_TENANT) == 1

    release.set()
    await _wait_until(lambda: _state().gate.in_use == 0)
    assert _state().leases.count(_TENANT) == 0


async def test_timed_out_decode_returns_at_once_and_drains_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store()
    _install(monkeypatch, store, timeout=1)
    _, release = _blocking_decoder(monkeypatch, store, "decode_window")
    async with asyncio.timeout(5):
        with pytest.raises(rq.ResultQueryTimeoutError):
            await rq.read_result_window(_operator(), uuid.uuid4(), 0, 2)
    assert _state().gate.in_use == 1

    release.set()
    await _wait_until(lambda: _state().gate.in_use == 0)
    assert _state().leases.count(_TENANT) == 0


def _blocking_wire_check(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    """Make the first wire-size measurement block until *release* is set.

    Only the first call blocks, so a probe read on another path can go
    through while the first read's response is still being measured.
    """
    started, release = threading.Event(), threading.Event()
    real = rq.measure_delivered_wire_bytes

    def _blocked(result: Any, **kwargs: Any) -> int:
        if not started.is_set():
            started.set()
            release.wait(10)
        return real(result, **kwargs)

    monkeypatch.setattr(rq, "measure_delivered_wire_bytes", _blocked)
    return started, release


@pytest.mark.parametrize("path", list(_READS), ids=list(_READS))
async def test_slow_wire_check_runs_off_the_loop_and_keeps_its_slot(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The response build and its wire check are worker work on every path.

    The wire check encodes the whole response again. While it blocks, the
    event loop still runs (a read on another path completes at once), the
    slot and the tenant lease stay held, and a cancelled request leaves
    them held until the check finishes.
    """
    store = _store()
    _install(monkeypatch, store)
    handle_id = uuid.uuid4()
    _seed_for(store, path, handle_id)
    started, release = _blocking_wire_check(monkeypatch)

    task = asyncio.create_task(_READS[path](_operator(), handle_id))
    await _wait_until(started.is_set)
    probe = "query" if path == "paging" else "paging"
    async with asyncio.timeout(5):
        await _READS[probe](_operator(tenant_id=_OTHER_TENANT), uuid.uuid4())
    assert _state().gate.in_use == 1
    assert _state().leases.count(_TENANT) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _state().gate.in_use == 1
    assert _state().leases.count(_TENANT) == 1

    release.set()
    await _wait_until(lambda: _state().gate.in_use == 0)
    assert _state().leases.count(_TENANT) == 0


async def test_timed_out_wire_check_returns_at_once_and_drains_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A query whose wire check runs past the budget is a timeout, not a hang."""
    _install(monkeypatch, _store(), timeout=1)
    _, release = _blocking_wire_check(monkeypatch)
    async with asyncio.timeout(5):
        with pytest.raises(rq.ResultQueryTimeoutError):
            await rq.run_result_query(_operator(), uuid.uuid4(), ResultQuerySpec())
    assert _state().gate.in_use == 1
    assert _state().leases.count(_TENANT) == 1

    release.set()
    await _wait_until(lambda: _state().leases.count(_TENANT) == 0)
    assert _state().gate.in_use == 0


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
    _install(monkeypatch, _store())
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
    _install(monkeypatch, _store(), timeout=1)
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
    _install(monkeypatch, _store())
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


@pytest.mark.parametrize("arguments", [{}, {"query": {}}], ids=["paging", "query"])
async def test_mcp_maps_a_timed_out_read_to_query_timeout(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]
) -> None:
    handle_id = uuid.uuid4()

    async def _expire(*_: Any, **__: Any) -> Any:
        raise rq.ResultQueryTimeoutError(
            handle_id, 10, remediation=rq.ResultQueryTimeoutError.PAGING_REMEDIATION
        )

    monkeypatch.setattr(mcp_result_query, "read_result_window", _expire)
    monkeypatch.setattr(mcp_result_query, "run_result_query", _expire)
    with pytest.raises(McpInvalidParamsError) as excinfo:
        await mcp_result_query._result_query_handler(
            _operator(), {"handle_id": str(handle_id), **arguments}
        )
    assert excinfo.value.data == {"reason": "query_timeout"}
    assert "lower `limit`" in str(excinfo.value)


@pytest.mark.parametrize("query", [None, ResultQuerySpec()], ids=["paging", "query"])
async def test_rest_maps_a_timed_out_read_to_422(
    monkeypatch: pytest.MonkeyPatch, query: ResultQuerySpec | None
) -> None:
    async def _expire(*_: Any, **__: Any) -> Any:
        raise rq.ResultQueryTimeoutError(uuid.uuid4(), 10)

    monkeypatch.setattr(rest_operations, "read_result_window", _expire)
    monkeypatch.setattr(rest_operations, "run_result_query", _expire)
    body = rest_operations.ResultQueryBody(handle_id=uuid.uuid4(), query=query)
    with pytest.raises(HTTPException) as excinfo:
        await rest_operations.post_result_query(body, _operator())
    assert excinfo.value.status_code == 422
    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail["reason"] == "query_timeout"
