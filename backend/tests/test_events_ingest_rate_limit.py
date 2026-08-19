# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-source ingest rate-limit unit tests (#2881).

Drives :func:`meho_backplane.events.ingest.rate_limit.enforce_ingest_rate_limit`
against an in-memory fake Valkey (no socket), mirroring
``test_broadcast_announce_rate_limit.py``. Acceptance-criteria coverage:

* the ``limit+1``-th delivery in one window is rejected with a
  ``Retry-After`` (criterion 1 / rate-limit)
* a second source in the same tenant is unaffected (per-``(tenant, source)``
  counter)
* ``limit == 0`` disables the limit with no Valkey round-trip
* a Valkey outage propagates (fail-loud; the endpoint maps it to 503)
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from meho_backplane.events.ingest.rate_limit import (
    INGEST_RATE_LIMIT_WINDOW_SECONDS,
    IngestRateLimitError,
    enforce_ingest_rate_limit,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_FIXED_NOW = 1_700_000_030.0  # 30 s into a fixed window


class _FakePipeline:
    def __init__(self, store: dict[str, int]) -> None:
        self._store = store
        self._ops: list[tuple[str, str, int]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def incr(self, name: str, amount: int = 1) -> None:
        self._ops.append(("incr", name, amount))

    def expire(self, name: str, time: int, *args: object, **kwargs: object) -> None:
        self._ops.append(("expire", name, time))

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op, name, arg in self._ops:
            if op == "incr":
                self._store[name] = self._store.get(name, 0) + arg
                results.append(self._store[name])
            else:
                results.append(True)
        return results


class _FakeValkey:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.pipeline_calls = 0

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        self.pipeline_calls += 1
        return _FakePipeline(self.store)


@pytest.fixture
def fake_valkey(monkeypatch: pytest.MonkeyPatch) -> _FakeValkey:
    fake = _FakeValkey()
    monkeypatch.setattr(
        "meho_backplane.events.ingest.rate_limit.get_broadcast_client",
        lambda: fake,
    )
    monkeypatch.setattr(
        "meho_backplane.events.ingest.rate_limit.time.time",
        lambda: _FIXED_NOW,
    )
    return fake


@pytest.mark.asyncio
async def test_within_limit_passes(fake_valkey: _FakeValkey) -> None:
    for _ in range(3):
        await enforce_ingest_rate_limit(_TENANT, "prod-am", limit=3)  # no raise


@pytest.mark.asyncio
async def test_over_limit_rejected_with_retry_after(fake_valkey: _FakeValkey) -> None:
    for _ in range(3):
        await enforce_ingest_rate_limit(_TENANT, "prod-am", limit=3)
    with pytest.raises(IngestRateLimitError) as exc:
        await enforce_ingest_rate_limit(_TENANT, "prod-am", limit=3)
    assert exc.value.limit == 3
    assert exc.value.window_seconds == INGEST_RATE_LIMIT_WINDOW_SECONDS
    # Retry-After counts the seconds until the current fixed window rolls.
    expected_retry = INGEST_RATE_LIMIT_WINDOW_SECONDS - (
        int(_FIXED_NOW) % INGEST_RATE_LIMIT_WINDOW_SECONDS
    )
    assert exc.value.retry_after_seconds == expected_retry
    assert 0 < exc.value.retry_after_seconds <= INGEST_RATE_LIMIT_WINDOW_SECONDS


@pytest.mark.asyncio
async def test_second_source_unaffected(fake_valkey: _FakeValkey) -> None:
    for _ in range(3):
        await enforce_ingest_rate_limit(_TENANT, "source-a", limit=3)
    # source-b has its own counter; the first call is well within the cap.
    await enforce_ingest_rate_limit(_TENANT, "source-b", limit=3)  # no raise


@pytest.mark.asyncio
async def test_zero_limit_disables_no_round_trip(fake_valkey: _FakeValkey) -> None:
    for _ in range(1000):
        await enforce_ingest_rate_limit(_TENANT, "prod-am", limit=0)
    assert fake_valkey.pipeline_calls == 0


@pytest.mark.asyncio
async def test_valkey_outage_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Valkey failure fails the delivery closed (raises, mapped to 503)."""

    def _boom() -> object:
        raise ConnectionError("valkey down")

    monkeypatch.setattr("meho_backplane.events.ingest.rate_limit.get_broadcast_client", _boom)
    with pytest.raises(ConnectionError):
        await enforce_ingest_rate_limit(_TENANT, "prod-am", limit=10)
