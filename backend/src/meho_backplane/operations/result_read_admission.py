# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admission and a bounded worker pool for the two result-read paths (#3640).

Paging (:func:`~meho_backplane.operations.result_query.read_result_window`)
and structured query
(:func:`~meho_backplane.operations.result_query.run_result_query`) both load
a spilled row set from the shared Valkey. Query mode then builds a field
catalog, registers the rows in a fresh DuckDB engine, and runs one
``SELECT``. Without a bound, many concurrent reads each start that work. This
module bounds it in three ways, all checked **before** the Valkey ``GET``:

* **Per-principal rate check** -- a fixed-window ``INCR`` + ``EXPIRE``
  counter keyed ``(tenant, principal, window)``, the same pattern as
  :mod:`meho_backplane.broadcast.rate_limit`. Paging and query share one
  counter. ``result_query_rate_per_minute=0`` (the default) turns it off
  with no Valkey round-trip.
* **Process-wide worker slots** -- a non-queuing gate sized to the
  dedicated worker pool (``result_query_executor_workers``). When every
  slot is busy the read is rejected at once; it never waits in a queue.
* **Per-tenant lease** -- at most ``result_query_tenant_concurrency``
  reads of one tenant hold a slot at the same time, so one tenant cannot
  take every slot when the pool is sized larger than the tenant cap.

Draining a worker that ran past its deadline
============================================

A running :class:`concurrent.futures` worker cannot be cancelled
(``Future.cancel()`` returns ``False`` once it runs). So when a request
times out or is cancelled, the request returns at once but the slot and
the tenant lease stay held until the worker thread finishes. The worker's
own done-callback releases them, from the worker thread, so the release
does not depend on the request's event loop. While the worker runs, a
keeper task renews the tenant lease; the request awaits that keeper
through :func:`asyncio.shield`, so a request-side cancellation never
cancels the keeper and never releases the lease early. If the keeper
stops renewing (for example the event loop shut down), the lease's TTL
reclaims it.

Each lease carries an owner token. Releasing or renewing uses that token,
so a stale caller can never free a slot that a newer read now owns.

The app lifespan sizes the pool and the limits from settings at startup
(:func:`configure_result_read_admission`) and stops the pool at shutdown.
Before that, for example in unit tests that call the read cores directly,
the ``Settings`` field defaults apply.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.settings import Settings

__all__ = [
    "RESULT_READ_CAPACITY_RETRY_AFTER_SECONDS",
    "RESULT_READ_RATE_WINDOW_SECONDS",
    "ResultReadCapacityError",
    "ResultReadLease",
    "ResultReadRateLimitError",
    "ResultReadRejectedError",
    "admit_result_read",
    "configure_result_read_admission",
    "get_result_read_executor",
    "run_on_result_read_executor",
    "shutdown_result_read_admission",
]

_log = structlog.get_logger(__name__)

#: Fixed window of the per-principal rate check. The setting is "per
#: minute", so the window is 60 s; an operator tunes the count, not the
#: window length.
RESULT_READ_RATE_WINDOW_SECONDS: Final[int] = 60

#: Retry hint for a capacity rejection. There is no window to roll over:
#: a slot frees as soon as an in-flight read finishes, usually within a
#: second.
RESULT_READ_CAPACITY_RETRY_AFTER_SECONDS: Final[int] = 1

#: Time-to-live of a tenant lease that nobody renews. The keeper task
#: renews a lease every third of this while its worker runs, so the TTL
#: only matters when renewal stopped (the event loop is gone).
_TENANT_LEASE_TTL_SECONDS: Final[float] = 60.0
_TENANT_LEASE_RENEW_SECONDS: Final[float] = _TENANT_LEASE_TTL_SECONDS / 3


class ResultReadRejectedError(Exception):
    """A result read was refused before it touched the store.

    Recoverable: the caller waits ``retry_after_seconds`` and retries.
    ``reason`` is the machine-readable cause each transport echoes back
    (MCP ``data.reason``, REST ``detail.reason``).
    """

    reason: str = "rejected"

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ResultReadRateLimitError(ResultReadRejectedError):
    """The principal made more result reads than its per-minute cap."""

    reason = "rate_limited"

    def __init__(self, *, limit: int, retry_after_seconds: int) -> None:
        super().__init__(
            f"result read rate limit reached: {limit} reads per "
            f"{RESULT_READ_RATE_WINDOW_SECONDS}s for this principal. "
            f"Retry after {retry_after_seconds}s.",
            retry_after_seconds=retry_after_seconds,
        )
        self.limit = limit


class ResultReadCapacityError(ResultReadRejectedError):
    """Every result-read slot (process-wide or for this tenant) is busy."""

    reason = "capacity_exceeded"

    def __init__(self, scope: str) -> None:
        super().__init__(
            f"too many result reads are running ({scope} limit reached). "
            f"Retry after {RESULT_READ_CAPACITY_RETRY_AFTER_SECONDS}s.",
            retry_after_seconds=RESULT_READ_CAPACITY_RETRY_AFTER_SECONDS,
        )
        self.scope = scope


class _SlotGate:
    """Non-queuing counter of busy worker slots (thread-safe)."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.in_use = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self.in_use >= self.capacity:
                return False
            self.in_use += 1
            return True

    def release(self) -> None:
        with self._lock:
            self.in_use = max(self.in_use - 1, 0)


class _TenantLeases:
    """Per-tenant leases with owner tokens and a TTL backstop (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[UUID, dict[str, float]] = {}

    def acquire(self, tenant_id: UUID, cap: int) -> str | None:
        now = time.monotonic()
        with self._lock:
            held = self._leases.setdefault(tenant_id, {})
            for token in [t for t, expires in held.items() if expires <= now]:
                del held[token]
            if len(held) >= cap:
                return None
            token = uuid.uuid4().hex
            held[token] = now + _TENANT_LEASE_TTL_SECONDS
            return token

    def renew(self, tenant_id: UUID, token: str) -> None:
        with self._lock:
            held = self._leases.get(tenant_id)
            if held is not None and token in held:
                held[token] = time.monotonic() + _TENANT_LEASE_TTL_SECONDS

    def release(self, tenant_id: UUID, token: str) -> None:
        with self._lock:
            held = self._leases.get(tenant_id)
            if held is None:
                return
            held.pop(token, None)
            if not held:
                del self._leases[tenant_id]

    def count(self, tenant_id: UUID) -> int:
        """Leases held by *tenant_id*, expired ones included until purged."""
        with self._lock:
            return len(self._leases.get(tenant_id, {}))


@dataclass(frozen=True)
class _Limits:
    workers: int
    tenant_concurrency: int
    rate_per_minute: int

    @classmethod
    def from_settings(cls, settings: Settings) -> _Limits:
        return cls(
            workers=settings.result_query_executor_workers,
            tenant_concurrency=settings.result_query_tenant_concurrency,
            rate_per_minute=settings.result_query_rate_per_minute,
        )


#: Used until the app lifespan calls :func:`configure_result_read_admission`
#: (unit tests that drive the read cores directly). Taken from the
#: ``Settings`` field defaults so the two can never drift.
_DEFAULT_LIMITS: Final[_Limits] = _Limits(
    workers=Settings.model_fields["result_query_executor_workers"].default,
    tenant_concurrency=Settings.model_fields["result_query_tenant_concurrency"].default,
    rate_per_minute=Settings.model_fields["result_query_rate_per_minute"].default,
)


@dataclass
class _State:
    limits: _Limits
    executor: ThreadPoolExecutor
    gate: _SlotGate
    leases: _TenantLeases


_STATE_LOCK = threading.Lock()
_STATE: _State | None = None
#: Keeper tasks of requests that gave up waiting; held so they are not
#: garbage-collected before their worker drains.
_KEEPERS: set[asyncio.Task[Any]] = set()


def _new_state(limits: _Limits) -> _State:
    return _State(
        limits=limits,
        executor=ThreadPoolExecutor(max_workers=limits.workers, thread_name_prefix="result-read"),
        gate=_SlotGate(limits.workers),
        leases=_TenantLeases(),
    )


def _state() -> _State:
    global _STATE
    with _STATE_LOCK:
        if _STATE is None:
            _STATE = _new_state(_DEFAULT_LIMITS)
        return _STATE


def configure_result_read_admission(settings: Settings) -> None:
    """Size the pool and the limits from *settings* (app startup).

    Replaces any earlier pool. Work already running on the old pool
    finishes and releases its slot on the old gate.
    """
    global _STATE
    with _STATE_LOCK:
        old, _STATE = _STATE, _new_state(_Limits.from_settings(settings))
    if old is not None:
        old.executor.shutdown(wait=False)


def shutdown_result_read_admission() -> None:
    """Stop the pool (app shutdown). Running work is not interrupted."""
    global _STATE
    with _STATE_LOCK:
        old, _STATE = _STATE, None
    if old is not None:
        old.executor.shutdown(wait=False)


def get_result_read_executor() -> ThreadPoolExecutor:
    """The dedicated worker pool."""
    return _state().executor


class ResultReadLease:
    """One admitted read: a worker slot plus a tenant lease.

    :meth:`release` is safe to call twice and from any thread. Once work
    is handed to the worker pool, only the worker's done-callback
    releases the lease, so it stays held until the thread finishes.
    """

    def __init__(self, gate: _SlotGate, leases: _TenantLeases, tenant_id: UUID, token: str) -> None:
        self._gate = gate
        self._leases = leases
        self.tenant_id = tenant_id
        self.token = token
        self.handed_off = False
        self._released = False
        self._lock = threading.Lock()

    def renew(self) -> None:
        self._leases.renew(self.tenant_id, self.token)

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._leases.release(self.tenant_id, self.token)
        self._gate.release()


async def _check_rate(tenant_id: UUID, principal_sub: str, limit: int) -> None:
    """Count this read in the principal's window; raise when over the cap."""
    if limit <= 0:
        return
    window = RESULT_READ_RATE_WINDOW_SECONDS
    now = int(time.time())
    key = f"meho:ratelimit:result_read:{tenant_id}:{principal_sub}:{now // window}"
    async with get_broadcast_client().pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window)
        count, _ = await pipe.execute()
    if count > limit:
        raise ResultReadRateLimitError(limit=limit, retry_after_seconds=window - (now % window))


@contextlib.asynccontextmanager
async def admit_result_read(operator: Operator) -> AsyncIterator[ResultReadLease]:
    """Admit one result read, or raise before any store access.

    Order: rate check, then a process-wide worker slot, then a tenant
    lease. Raises :class:`ResultReadRateLimitError` or
    :class:`ResultReadCapacityError`; nothing is held after a rejection.
    On exit the lease is released, unless it was handed to the worker
    pool with :func:`run_on_result_read_executor` -- then the worker
    releases it when it finishes.

    A Valkey failure in the rate check propagates as-is (fail closed, the
    same as the other limiters on that client).
    """
    state = _state()
    tenant_id = operator.tenant_id
    await _check_rate(tenant_id, operator.sub, state.limits.rate_per_minute)

    gate, leases = state.gate, state.leases
    if not gate.try_acquire():
        _log.info("result_read_rejected", reason="capacity_exceeded", scope="process")
        raise ResultReadCapacityError("process")
    token = leases.acquire(tenant_id, state.limits.tenant_concurrency)
    if token is None:
        gate.release()
        _log.info(
            "result_read_rejected",
            reason="capacity_exceeded",
            scope="tenant",
            tenant_id=str(tenant_id),
        )
        raise ResultReadCapacityError("tenant")

    lease = ResultReadLease(gate, leases, tenant_id, token)
    try:
        yield lease
    finally:
        if not lease.handed_off:
            lease.release()


async def _keep_until_drained[T](lease: ResultReadLease | None, waiter: asyncio.Future[T]) -> T:
    """Wait for the worker, renewing the tenant lease while it runs."""
    try:
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=_TENANT_LEASE_RENEW_SECONDS)
            if done:
                return waiter.result()
            if lease is not None:
                lease.renew()
    except asyncio.CancelledError:
        # Only reached when the event loop shuts down: stop the result
        # from being delivered to a closed loop. The worker's
        # done-callback still releases the lease.
        waiter.cancel()
        raise


async def run_on_result_read_executor[T](
    lease: ResultReadLease | None,
    func: Callable[..., T],
    *args: object,
    on_abandon: Callable[[], None] | None = None,
) -> T:
    """Run ``func(*args)`` on the dedicated pool and await its result.

    With a *lease*, the lease is handed to the worker: its done-callback
    releases it when the thread finishes. If the caller is cancelled (a
    deadline or a dropped request), *on_abandon* runs (for example to
    interrupt a DuckDB query), the cancellation propagates at once, and
    the worker keeps its slot until it drains.
    """
    future: Future[T] = _state().executor.submit(func, *args)
    if lease is not None:
        lease.handed_off = True
        future.add_done_callback(lambda _done: lease.release())
    keeper: asyncio.Task[T] = asyncio.get_running_loop().create_task(
        _keep_until_drained(lease, asyncio.wrap_future(future))
    )
    try:
        return await asyncio.shield(keeper)
    except asyncio.CancelledError:
        if not keeper.done():
            _KEEPERS.add(keeper)
            keeper.add_done_callback(_KEEPERS.discard)
            keeper.add_done_callback(_consume_result)
            if on_abandon is not None:
                on_abandon()
        raise


def _consume_result(task: asyncio.Task[Any]) -> None:
    """Retrieve an abandoned worker's outcome so it is not logged as lost."""
    if not task.cancelled():
        task.exception()
