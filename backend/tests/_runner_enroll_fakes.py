# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An in-memory Valkey for the runner enrollment limits (no socket)."""

from __future__ import annotations

import time
from typing import Any

LIMITS = "meho_backplane.auth.runner_enrollment_limits"


class FakePipeline:
    def __init__(self, valkey: FakeValkey) -> None:
        self._valkey = valkey
        self._ops: list[tuple[str, str, int]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def incr(self, name: str) -> None:
        self._ops.append(("incr", name, 0))

    def expire(self, name: str, time: int) -> None:
        self._ops.append(("expire", name, time))

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op, name, seconds in self._ops:
            if op == "incr":
                results.append(self._valkey.add(name, 1))
            else:
                results.append(self._valkey.set_expiry(name, seconds))
        return results


class FakeValkey:
    """Keys end at their expiry time, read from ``time.time()`` (tests patch it)."""

    def __init__(self) -> None:
        self.store: dict[str, int | str] = {}
        self.expires_at: dict[str, float] = {}
        self.calls = 0

    def _drop_ended(self) -> None:
        now = time.time()
        for name in [n for n, end in self.expires_at.items() if end <= now]:
            self.store.pop(name, None)
            self.expires_at.pop(name, None)

    def add(self, name: str, amount: int) -> int:
        self._drop_ended()
        value = int(self.store.get(name, 0)) + amount
        self.store[name] = value
        return value

    def set_expiry(self, name: str, seconds: int) -> bool:
        self._drop_ended()
        if name not in self.store:
            return False
        self.expires_at[name] = time.time() + seconds
        return True

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        self.calls += 1
        return FakePipeline(self)

    async def set(
        self, name: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        self.calls += 1
        self._drop_ended()
        if nx and name in self.store:
            return None
        self.store[name] = value
        self.expires_at.pop(name, None)
        if ex is not None:
            self.set_expiry(name, ex)
        return True

    async def incr(self, name: str) -> int:
        self.calls += 1
        return self.add(name, 1)

    async def incrby(self, name: str, amount: int) -> int:
        self.calls += 1
        return self.add(name, amount)

    async def expire(self, name: str, time: int) -> bool:
        self.calls += 1
        return self.set_expiry(name, time)

    async def delete(self, *names: str) -> int:
        self.calls += 1
        self._drop_ended()
        gone = 0
        for name in names:
            gone += int(self.store.pop(name, None) is not None)
            self.expires_at.pop(name, None)
        return gone

    async def getdel(self, name: str) -> str | None:
        self.calls += 1
        self._drop_ended()
        value = self.store.pop(name, None)
        self.expires_at.pop(name, None)
        return None if value is None else str(value)


class BrokenValkey:
    """A Valkey that cannot be reached: every call raises ``ConnectionError``."""

    def pipeline(self, transaction: bool = True) -> Any:
        raise ConnectionError("valkey is down")

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("valkey is down")

    async def incr(self, *args: Any) -> Any:
        raise ConnectionError("valkey is down")

    async def incrby(self, *args: Any) -> Any:
        raise ConnectionError("valkey is down")

    async def delete(self, *args: Any) -> Any:
        raise ConnectionError("valkey is down")

    async def getdel(self, *args: Any) -> Any:
        raise ConnectionError("valkey is down")
