# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The lock of the Keycloak writes that keep MEHO's access, on a real PostgreSQL.

A slow Keycloak read (the count of platform admins) keeps the lock's
transaction open and idle. What is proven:

* with a short ``idle_in_transaction_session_timeout``, the database does
  not end the lock's transaction: a second holder cannot take the lock, and
  the first write still confirms it;
* when the database ends the lock's session during the count, the write is
  refused before it is sent.

The ``pg_engine`` fixture from :mod:`tests.integration.conftest` points the
process-wide engine at a PostgreSQL container and skips the module when
Docker is not there.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from meho_backplane.connectors.keycloak.ops_meho_access_lock import (
    _db_key,
    confirm_meho_access_lock,
    hold_meho_access_lock,
    meho_access_scope,
)
from meho_backplane.connectors.keycloak.ops_user_meho_access_guard import (
    MehoAccessRefusedError,
    refuse_last_admin_loss,
)
from meho_backplane.db.engine import get_engine

pytestmark = pytest.mark.usefixtures("pg_engine")

_REALM = "made-up-login-realm"
#: Longer than the idle limit below, as a slow Keycloak read is.
_SLOW_READ_S = 1.0


@pytest.fixture
async def short_idle_limit() -> AsyncIterator[None]:
    """New connections end a transaction that is idle for 200 ms."""
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execute(
            text("ALTER ROLE CURRENT_USER SET idle_in_transaction_session_timeout = '200ms'")
        )
        await conn.commit()
    await engine.dispose()
    try:
        yield
    finally:
        async with engine.connect() as conn:
            await conn.execute(
                text("ALTER ROLE CURRENT_USER RESET idle_in_transaction_session_timeout")
            )
            await conn.commit()
        await engine.dispose()


async def _other_session_takes_the_lock() -> bool:
    async with get_engine().connect() as conn:
        taken = await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": _db_key(_REALM)})
        if taken:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _db_key(_REALM)})
        await conn.commit()
    return bool(taken)


async def test_a_short_idle_limit_does_not_end_the_lock(short_idle_limit: None) -> None:
    async def _slow_count() -> bool:
        await asyncio.sleep(_SLOW_READ_S)
        # Another pod cannot take the lock while this write counts.
        assert not await _other_session_takes_the_lock()
        return False

    async with meho_access_scope():
        await refuse_last_admin_loss("keycloak.user.disable", _REALM, "refused", _slow_count)
        await asyncio.sleep(_SLOW_READ_S)
        await confirm_meho_access_lock(_REALM)
        assert not await _other_session_takes_the_lock()
    assert await _other_session_takes_the_lock()


async def _end_the_lock_session() -> None:
    async with get_engine().connect() as conn:
        ended = await conn.scalar(
            text(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_locks "
                "WHERE locktype = 'advisory' AND granted AND pid <> pg_backend_pid()"
            )
        )
        await conn.commit()
    assert ended == 1


async def test_a_write_whose_lock_session_ended_is_refused_before_it_is_sent() -> None:
    sent: list[bool] = []

    async def _count_while_the_database_ends_the_session() -> bool:
        await _end_the_lock_session()
        return False

    with pytest.raises(MehoAccessRefusedError) as refused:
        async with meho_access_scope():
            await refuse_last_admin_loss(
                "keycloak.user.disable",
                _REALM,
                "refused",
                _count_while_the_database_ends_the_session,
            )
            sent.append(True)
    assert "meho_access_not_checked" in str(refused.value)
    assert sent == []
    assert await _other_session_takes_the_lock()


async def test_holding_the_lock_twice_in_one_scope_takes_it_once() -> None:
    async with meho_access_scope():
        await hold_meho_access_lock(_REALM)
        await hold_meho_access_lock(_REALM.upper())
        await confirm_meho_access_lock(_REALM)
    assert await _other_session_takes_the_lock()
