# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An in-memory Valkey for the runner enrollment limits (no socket)."""

from __future__ import annotations

from typing import Any

LIMITS = "meho_backplane.auth.runner_enrollment_limits"


class FakePipeline:
    def __init__(self, store: dict[str, int]) -> None:
        self._store = store
        self._keys: list[str] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def incr(self, name: str) -> None:
        self._keys.append(name)

    def expire(self, name: str, time: int) -> None:
        return None

    async def execute(self) -> list[Any]:
        counts = []
        for name in self._keys:
            self._store[name] = self._store.get(name, 0) + 1
            counts.append(self._store[name])
        return [*counts, True]


class FakeValkey:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.calls = 0

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        self.calls += 1
        return FakePipeline(self.store)

    async def incr(self, name: str) -> int:
        self.calls += 1
        self.store[name] = self.store.get(name, 0) + 1
        return self.store[name]

    async def incrby(self, name: str, amount: int) -> int:
        self.calls += 1
        self.store[name] = self.store.get(name, 0) + amount
        return self.store[name]

    async def decr(self, name: str) -> int:
        self.calls += 1
        self.store[name] = self.store.get(name, 0) - 1
        return self.store[name]

    async def expire(self, name: str, time: int) -> bool:
        return True

    async def getdel(self, name: str) -> str | None:
        self.calls += 1
        value = self.store.pop(name, None)
        return None if value is None else str(value)


class BrokenValkey:
    """A Valkey that cannot be reached."""

    def pipeline(self, transaction: bool = True) -> Any:
        raise ConnectionError("valkey is down")

    def __getattr__(self, name: str) -> Any:
        raise ConnectionError("valkey is down")
