# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A policy change is in force right after its commit, also with a read in between.

The policy writes (tenant flight-recorder policy, announce gate, per-target
capture override, broadcast overrides) change a row in the request's session.
That session commits at the end of the request. These tests run a cached read
between the write and the commit, as a dispatch of the same tenant can, and
check that the read right after the commit sees the new policy.

Runs against the autouse ``sqlite+aiosqlite`` engine of the conftest.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from meho_backplane.api.v1.broadcast_overrides import (
    BroadcastOverrideCreate,
    create_override_impl,
    delete_override_impl,
)
from meho_backplane.api.v1.targets import _audit_flight_recorder_capture_write
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.announce_gate import (
    announce_gate_enabled,
    reset_announce_gate_cache_for_testing,
)
from meho_backplane.broadcast.overrides import (
    _load_tenant_rules,
    reset_overrides_cache_for_testing,
)
from meho_backplane.db.after_commit import clear_now_and_after_commit
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
