# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the pinned-connection advisory-lock helper (#3010).

The SQLite path only: on a dialect without advisory locks the helper
must yield ``True`` unconditionally (the single-replica test path every
tick-loop suite rides) and must not open a dedicated connection. The PG
semantics — same-connection lock/unlock across mid-lock commits, busy
second holder, no stranded lock in ``pg_locks`` — are exercised against
a real container in :mod:`tests.integration.test_advisory_lock_pg`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from meho_backplane.db import advisory
from meho_backplane.db.advisory import advisory_lock
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires at construction time."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_sqlite_dialect_always_yields_true() -> None:
    """No advisory locks on SQLite: the helper is a transparent no-op."""
    async with advisory_lock(0x1234, subsystem="test") as locked:
        assert locked is True


@pytest.mark.asyncio
async def test_sqlite_dialect_is_reentrant_across_keys() -> None:
    """Two nested holds (any keys) both proceed on the lockless dialect."""
    async with (
        advisory_lock(1, subsystem="test") as outer,
        advisory_lock(1, subsystem="test") as inner,
    ):
        assert outer is True
        assert inner is True


class _FakeConn:
    """Answers ``scalar`` calls in order and records the lock statements."""

    def __init__(self, answers: list[bool]) -> None:
        self.answers = answers
        self.statements: list[str] = []

    async def scalar(self, statement: Any, params: dict[str, Any]) -> bool:
        self.statements.append(str(statement))
        return self.answers.pop(0)

    async def invalidate(self) -> None:
        return None


def _fake_pg_engine(monkeypatch: pytest.MonkeyPatch, answers: list[bool]) -> _FakeConn:
    conn = _FakeConn(answers)

    @asynccontextmanager
    async def connect() -> AsyncIterator[_FakeConn]:
        yield conn

    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=connect)
    monkeypatch.setattr(advisory, "get_engine", lambda: engine)
    return conn


@pytest.mark.asyncio
async def test_wait_tries_the_key_again_until_it_is_free(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _fake_pg_engine(monkeypatch, [False, False, True, True])
    async with advisory_lock(7, subsystem="test", wait_seconds=5.0, poll_seconds=0.0) as locked:
        assert locked is True
    tries = [s for s in conn.statements if "pg_try_advisory_lock" in s]
    assert len(tries) == 3
    assert conn.statements[-1] == "SELECT pg_advisory_unlock(:k)"


@pytest.mark.asyncio
async def test_without_wait_a_held_key_is_busy_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _fake_pg_engine(monkeypatch, [False])
    async with advisory_lock(7, subsystem="test") as locked:
        assert locked is False
    assert conn.statements == ["SELECT pg_try_advisory_lock(:k)"]
