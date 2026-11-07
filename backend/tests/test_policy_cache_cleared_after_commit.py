# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A policy change is in force right after its commit, also with a read in between.

The policy writes (tenant flight-recorder policy, announce gate, per-target
capture override, broadcast overrides) change a row in the request's session.
That session commits at the end of the request. These tests run a cached read
between the write and the commit, as a dispatch of the same tenant can, and
check that the read right after the commit sees the new policy. A second set
pauses a read after its database read, commits the change, lets the read
finish, and checks that the paused read did not put the old policy into the
cache.

Runs against the autouse ``sqlite+aiosqlite`` engine of the conftest.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from meho_backplane.api.v1.broadcast_overrides import (
    BroadcastOverrideCreate,
    create_override_impl,
    delete_override_impl,
)
from meho_backplane.api.v1.targets import _audit_flight_recorder_capture_write
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import announce_gate, overrides
from meho_backplane.broadcast.announce_gate import (
    announce_gate_enabled,
    reset_announce_gate_cache_for_testing,
)
from meho_backplane.broadcast.overrides import (
    _load_tenant_rules,
    reset_overrides_cache_for_testing,
)
from meho_backplane.db.after_commit import clear_now_and_after_commit
from meho_backplane.db.change_counter import ChangeCounter
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target, Tenant
from meho_backplane.flight_recorder import config as fr_config
from meho_backplane.flight_recorder.config import (
    reset_flight_recorder_config_cache_for_testing,
    should_capture,
    should_expose_to_agent,
)
from meho_backplane.settings import get_settings
from meho_backplane.tenancy.policies import (
    set_announce_gate_enabled,
    set_flight_recorder_policy,
)


@pytest.fixture(autouse=True)
def _settings_and_clean_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    reset_announce_gate_cache_for_testing()
    reset_overrides_cache_for_testing()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    reset_announce_gate_cache_for_testing()
    reset_overrides_cache_for_testing()


async def _seed_tenant(slug: str, **columns: Any) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}", **columns))
        await session.commit()
    return tenant_id


def _operator(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="tenant-admin",
        name=None,
        email=None,
        raw_jwt="made-up-token",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# --------------------------------------------------------------------------
# The helper
# --------------------------------------------------------------------------


def _sync_session() -> Session:
    return Session(create_engine("sqlite://"))


def test_helper_clears_now_and_once_after_the_commit() -> None:
    calls: list[str] = []
    session = _sync_session()
    session.execute(text("select 1"))
    clear_now_and_after_commit(session, lambda: calls.append("clear"))
    assert calls == ["clear"]
    session.commit()
    assert calls == ["clear", "clear"]
    session.execute(text("select 1"))
    session.commit()
    assert calls == ["clear", "clear"]


def test_helper_waits_for_the_outer_commit_after_a_savepoint() -> None:
    calls: list[str] = []
    session = _sync_session()
    session.execute(text("select 1"))
    with session.begin_nested():
        clear_now_and_after_commit(session, lambda: calls.append("clear"))
    # Releasing the savepoint fires after_commit, but nothing is saved yet.
    assert calls == ["clear"]
    session.commit()
    assert calls == ["clear", "clear"]


def test_helper_does_not_clear_again_after_a_rollback() -> None:
    calls: list[str] = []
    session = _sync_session()
    session.execute(text("select 1"))
    clear_now_and_after_commit(session, lambda: calls.append("clear"))
    session.rollback()
    assert calls == ["clear"]


def test_helper_without_a_session_clears_once() -> None:
    calls: list[str] = []
    clear_now_and_after_commit(None, lambda: calls.append("clear"))
    assert calls == ["clear"]


def test_counter_notes_a_change_since_the_count_was_taken() -> None:
    counter = ChangeCounter()
    key = uuid.uuid4()
    seen = counter.current(key)
    assert seen == 0
    assert counter.unchanged_since(key, seen)
    counter.bump(key)
    assert not counter.unchanged_since(key, seen)
    assert counter.unchanged_since(key, counter.current(key))
    assert counter.unchanged_since(uuid.uuid4(), 0)
    counter.reset_for_testing()
    assert counter.current(key) == 0


# --------------------------------------------------------------------------
# Tenant flight-recorder policy and announce gate
# --------------------------------------------------------------------------


async def test_flight_recorder_policy_read_during_the_change() -> None:
    tenant_id = await _seed_tenant(
        "fr-commit-race", flight_recorder_enabled=True, flight_recorder_agent_readable=True
    )
    async with get_sessionmaker()() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        set_flight_recorder_policy(
            tenant, {"flight_recorder_enabled": False, "flight_recorder_agent_readable": False}
        )
        # A dispatch reads the policy before the change is saved: it sees the
        # saved (old) policy and caches it.
        assert await should_expose_to_agent(tenant_id=tenant_id) is True
        assert await should_capture(tenant_id=tenant_id) is True
        await session.commit()

    assert await should_expose_to_agent(tenant_id=tenant_id) is False
    assert await should_capture(tenant_id=tenant_id) is False


async def test_flight_recorder_policy_read_during_a_change_in_a_savepoint() -> None:
    """The settings import writes the policy inside a savepoint.

    Releasing the savepoint writes the row inside the open transaction. On the
    test database a second session would already see that row, so the read of
    another connection (which sees the old, saved row) is put into the cache
    by hand.
    """
    tenant_id = await _seed_tenant(
        "fr-commit-race-nested", flight_recorder_enabled=True, flight_recorder_agent_readable=True
    )
    async with get_sessionmaker()() as session:
        async with session.begin_nested():
            tenant = await session.get(Tenant, tenant_id)
            assert tenant is not None
            set_flight_recorder_policy(tenant, {"flight_recorder_agent_readable": False})
        fr_config._TENANT_CACHE[tenant_id] = ((True, None, True), time.monotonic() + 600.0)
        await session.commit()

    assert await should_expose_to_agent(tenant_id=tenant_id) is False


async def test_flight_recorder_policy_rolled_back_keeps_the_old_policy() -> None:
    tenant_id = await _seed_tenant(
        "fr-commit-rollback", flight_recorder_enabled=True, flight_recorder_agent_readable=True
    )
    async with get_sessionmaker()() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        set_flight_recorder_policy(tenant, {"flight_recorder_agent_readable": False})
        await session.rollback()

    assert await should_expose_to_agent(tenant_id=tenant_id) is True


async def test_announce_gate_read_during_the_change() -> None:
    tenant_id = await _seed_tenant("gate-commit-race", announce_gate_enabled=False)
    async with get_sessionmaker()() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        assert set_announce_gate_enabled(tenant, True) is True
        assert await announce_gate_enabled(tenant_id) is False
        await session.commit()

    assert await announce_gate_enabled(tenant_id) is True


# --------------------------------------------------------------------------
# Per-target capture override
# --------------------------------------------------------------------------


async def test_target_capture_override_read_during_the_change() -> None:
    tenant_id = await _seed_tenant("fr-target-commit-race", flight_recorder_enabled=False)
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            Target(
                id=target_id,
                tenant_id=tenant_id,
                name="tgt",
                product="vmware-rest",
                host="vendor.example",
                flight_recorder_capture=True,
            )
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        target = await session.get(Target, target_id)
        assert target is not None
        target.flight_recorder_capture = False
        _audit_flight_recorder_capture_write(target, tenant_id=tenant_id, before=True)
        assert await should_capture(tenant_id=tenant_id, target_id=target_id) is True
        await session.commit()

    assert await should_capture(tenant_id=tenant_id, target_id=target_id) is False


# --------------------------------------------------------------------------
# Broadcast overrides
# --------------------------------------------------------------------------


async def test_broadcast_override_create_and_delete_read_during_the_change() -> None:
    tenant_id = await _seed_tenant("override-commit-race")
    operator = _operator(tenant_id)

    async with get_sessionmaker()() as session:
        row = await create_override_impl(
            payload=BroadcastOverrideCreate(op_id_pattern="vault.kv.*", detail="aggregate"),
            operator=operator,
            session=session,
        )
        override_id = row.id
        assert await _load_tenant_rules(tenant_id) == []
        await session.commit()

    assert [rule.op_id_pattern for rule in await _load_tenant_rules(tenant_id)] == ["vault.kv.*"]

    async with get_sessionmaker()() as session:
        await delete_override_impl(override_id=override_id, operator=operator, session=session)
        assert len(await _load_tenant_rules(tenant_id)) == 1
        await session.commit()

    assert await _load_tenant_rules(tenant_id) == []


# --------------------------------------------------------------------------
# A read that is paused over the commit
# --------------------------------------------------------------------------


class _PausedRead:
    """Pause the next cached read after its database read, until released."""

    def __init__(self) -> None:
        self.read_done = asyncio.Event()
        self.release = asyncio.Event()
        self._armed = True

    async def after_read(self) -> None:
        if not self._armed:
            return
        self._armed = False
        self.read_done.set()
        await self.release.wait()


class _PausingSession:
    def __init__(self, inner: AsyncSession, pause: _PausedRead) -> None:
        self._inner = inner
        self._pause = pause

    async def __aenter__(self) -> _PausingSession:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._inner.__aexit__(*exc)

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.execute(*args, **kwargs)
        await self._pause.after_read()
        return result


def _pause_next_read(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> _PausedRead:
    """Make the cached reads of *module* pause once, after the first database read."""
    pause = _PausedRead()
    real = get_sessionmaker()
    monkeypatch.setattr(module, "get_sessionmaker", lambda: lambda: _PausingSession(real(), pause))
    return pause


async def test_flight_recorder_policy_read_paused_over_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await _seed_tenant(
        "fr-paused-read", flight_recorder_enabled=True, flight_recorder_agent_readable=True
    )
    pause = _pause_next_read(monkeypatch, fr_config)
    reader = asyncio.create_task(should_expose_to_agent(tenant_id=tenant_id))
    await pause.read_done.wait()

    async with get_sessionmaker()() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        set_flight_recorder_policy(
            tenant, {"flight_recorder_enabled": False, "flight_recorder_agent_readable": False}
        )
        await session.commit()

    pause.release.set()
    # The paused read saw the old row. It returns that answer but does not cache it.
    assert await reader is True
    assert await should_expose_to_agent(tenant_id=tenant_id) is False
    assert await should_capture(tenant_id=tenant_id) is False


async def test_announce_gate_read_paused_over_the_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = await _seed_tenant("gate-paused-read", announce_gate_enabled=False)
    pause = _pause_next_read(monkeypatch, announce_gate)
    reader = asyncio.create_task(announce_gate_enabled(tenant_id))
    await pause.read_done.wait()

    async with get_sessionmaker()() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        assert set_announce_gate_enabled(tenant, True) is True
        await session.commit()

    pause.release.set()
    assert await reader is False
    assert await announce_gate_enabled(tenant_id) is True


async def test_target_capture_override_read_paused_over_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await _seed_tenant("fr-target-paused-read", flight_recorder_enabled=False)
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            Target(
                id=target_id,
                tenant_id=tenant_id,
                name="tgt",
                product="vmware-rest",
                host="vendor.example",
                flight_recorder_capture=True,
            )
        )
        await session.commit()

    pause = _pause_next_read(monkeypatch, fr_config)
    reader = asyncio.create_task(should_capture(tenant_id=tenant_id, target_id=target_id))
    await pause.read_done.wait()

    async with get_sessionmaker()() as session:
        target = await session.get(Target, target_id)
        assert target is not None
        target.flight_recorder_capture = False
        _audit_flight_recorder_capture_write(target, tenant_id=tenant_id, before=True)
        await session.commit()

    pause.release.set()
    assert await reader is True
    assert await should_capture(tenant_id=tenant_id, target_id=target_id) is False


async def test_broadcast_override_load_paused_over_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await _seed_tenant("override-paused-read")
    operator = _operator(tenant_id)
    pause = _pause_next_read(monkeypatch, overrides)
    reader = asyncio.create_task(_load_tenant_rules(tenant_id))
    await pause.read_done.wait()

    async with get_sessionmaker()() as session:
        await create_override_impl(
            payload=BroadcastOverrideCreate(op_id_pattern="vault.kv.*", detail="aggregate"),
            operator=operator,
            session=session,
        )
        await session.commit()

    pause.release.set()
    assert await reader == []
    assert [rule.op_id_pattern for rule in await _load_tenant_rules(tenant_id)] == ["vault.kv.*"]
