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

from collections.abc import Iterator

import pytest

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
