# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the flight-recorder capture + retention resolver (#3212, F1/F4).

Covers :mod:`meho_backplane.flight_recorder.config`:

* :func:`should_capture` resolution precedence (kill switch > per-target
  override > per-tenant default) and its fail-open-to-``False`` behaviour;
* :func:`resolve_retention_days` (per-tenant override vs global default,
  fail-open to default);
* :func:`should_expose_to_agent` resolution (kill switch > per-tenant explicit
  override > inherit the capture default) and its fail-open-to-``False``
  behaviour (F5, #3216);
* :func:`compute_expires_at` window math (pure).

Runs against the autouse ``sqlite+aiosqlite`` engine the conftest pre-migrates
to head. The per-key resolver cache is cleared before each assertion group so a
cached value from a prior seed never masks the DB read under test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target, Tenant
from meho_backplane.flight_recorder import config as fr_config
from meho_backplane.flight_recorder.config import (
    compute_expires_at,
    invalidate_target_override_cache,
    invalidate_tenant_policy_cache,
    reset_flight_recorder_config_cache_for_testing,
    resolve_retention_days,
    should_capture,
    should_expose_to_agent,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()


async def _seed_tenant(
    session: AsyncSession,
    *,
    slug: str,
    enabled: bool = False,
    retention_days: int | None = None,
    agent_readable: bool | None = None,
) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    session.add(
        Tenant(
            id=tenant_id,
            slug=slug,
            name=f"Tenant {slug}",
            flight_recorder_enabled=enabled,
            flight_recorder_retention_days=retention_days,
            flight_recorder_agent_readable=agent_readable,
        )
    )
    await session.commit()
    return tenant_id


async def _seed_target(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    override: bool | None,
) -> uuid.UUID:
    target_id = uuid.uuid4()
    session.add(
        Target(
            id=target_id,
            tenant_id=tenant_id,
            name=name,
            product="vmware-rest",
            host="vendor.example",
            flight_recorder_capture=override,
        )
    )
    await session.commit()
    return target_id


# --------------------------------------------------------------------------
# compute_expires_at — pure window math (F4)
# --------------------------------------------------------------------------


def test_compute_expires_at_adds_retention_days() -> None:
    created = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
    assert compute_expires_at(created, 7) == created + timedelta(days=7)
    assert compute_expires_at(created, 14) == created + timedelta(days=14)


# --------------------------------------------------------------------------
# should_capture — precedence + kill switch (F1)
# --------------------------------------------------------------------------


async def test_capture_off_by_default_for_non_lab_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-default-off")
    assert await should_capture(tenant_id=tenant_id) is False


async def test_capture_on_for_lab_class_tenant_default() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-lab-on", enabled=True)
    assert await should_capture(tenant_id=tenant_id) is True


async def test_per_target_override_forces_capture_on_over_tenant_off() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-t-on", enabled=False)
        target_id = await _seed_target(session, tenant_id=tenant_id, name="t-on", override=True)
    assert await should_capture(tenant_id=tenant_id, target_id=target_id) is True


async def test_per_target_override_forces_capture_off_over_tenant_on() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-t-off", enabled=True)
        target_id = await _seed_target(session, tenant_id=tenant_id, name="t-off", override=False)
    assert await should_capture(tenant_id=tenant_id, target_id=target_id) is False


async def test_null_target_override_inherits_tenant_default() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-inherit", enabled=True)
        target_id = await _seed_target(session, tenant_id=tenant_id, name="inh", override=None)
    assert await should_capture(tenant_id=tenant_id, target_id=target_id) is True


async def test_kill_switch_overrides_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """The global kill switch beats an ON tenant AND a force-on target override."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-kill", enabled=True)
        target_id = await _seed_target(session, tenant_id=tenant_id, name="kill", override=True)
    monkeypatch.setenv("FLIGHT_RECORDER_ENABLED", "false")
    get_settings.cache_clear()
    assert await should_capture(tenant_id=tenant_id, target_id=target_id) is False


async def test_unknown_tenant_fails_closed_to_no_capture() -> None:
    assert await should_capture(tenant_id=uuid.uuid4()) is False


async def test_should_capture_fails_open_to_false_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver DB error must never fail a dispatch — it resolves to no-capture."""

    async def _boom(_tenant_id: uuid.UUID) -> tuple[bool, int | None, bool | None]:
        raise RuntimeError("db down")

    monkeypatch.setattr(fr_config, "_resolve_tenant_policy", _boom)
    assert await should_capture(tenant_id=uuid.uuid4()) is False


# --------------------------------------------------------------------------
# resolve_retention_days — per-tenant override vs default (F4)
# --------------------------------------------------------------------------


async def test_retention_defaults_to_global_when_tenant_unset() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-ret-default", retention_days=None)
    assert await resolve_retention_days(tenant_id) == 7


async def test_retention_uses_per_tenant_override_when_set() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-ret-lab", retention_days=14)
    assert await resolve_retention_days(tenant_id) == 14


async def test_retention_honours_global_default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHT_RECORDER_RETENTION_DAYS_DEFAULT", "30")
    get_settings.cache_clear()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-ret-env", retention_days=None)
    assert await resolve_retention_days(tenant_id) == 30


async def test_retention_fails_open_to_default_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(_tenant_id: uuid.UUID) -> tuple[bool, int | None, bool | None]:
        raise RuntimeError("db down")

    monkeypatch.setattr(fr_config, "_resolve_tenant_policy", _boom)
    assert await resolve_retention_days(uuid.uuid4()) == 7


# --------------------------------------------------------------------------
# should_expose_to_agent — per-tenant agent gate (F5, #3216)
# --------------------------------------------------------------------------


async def test_agent_read_off_by_default_for_non_lab_tenant() -> None:
    """NULL override + capture OFF => agents cannot read (inherits capture)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-agent-default-off")
    assert await should_expose_to_agent(tenant_id=tenant_id) is False


async def test_agent_read_inherits_capture_default_lab_on() -> None:
    """NULL override + capture ON => agents can read: "follows the F1 default (lab-on)"."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-agent-inherit-on", enabled=True)
    assert await should_expose_to_agent(tenant_id=tenant_id) is True


async def test_agent_read_explicit_off_while_capture_on_keeps_operator_access() -> None:
    """override=False withholds from agents even with capture ON.

    This is the F5 "independent of the operator plane" gate: capture stays ON
    (so the operator plane keeps full access) while agents are cut off.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(
            session, slug="fr-agent-off", enabled=True, agent_readable=False
        )
    assert await should_expose_to_agent(tenant_id=tenant_id) is False
    # Capture (and thus the operator plane's data source) is unaffected.
    assert await should_capture(tenant_id=tenant_id) is True


async def test_agent_read_explicit_on_while_capture_off() -> None:
    """override=True forces agent access on even when the capture default is OFF."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(
            session, slug="fr-agent-force-on", enabled=False, agent_readable=True
        )
    assert await should_expose_to_agent(tenant_id=tenant_id) is True


async def test_agent_read_kill_switch_overrides_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global kill switch beats an explicit agent-readable=True override."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(
            session, slug="fr-agent-kill", enabled=True, agent_readable=True
        )
    monkeypatch.setenv("FLIGHT_RECORDER_ENABLED", "false")
    get_settings.cache_clear()
    assert await should_expose_to_agent(tenant_id=tenant_id) is False


async def test_agent_read_unknown_tenant_fails_closed() -> None:
    assert await should_expose_to_agent(tenant_id=uuid.uuid4()) is False


async def test_agent_read_fails_open_to_false_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver DB error withholds agent access (doubt reduces exposure)."""

    async def _boom(_tenant_id: uuid.UUID) -> tuple[bool, int | None, bool | None]:
        raise RuntimeError("db down")

    monkeypatch.setattr(fr_config, "_resolve_tenant_policy", _boom)
    assert await should_expose_to_agent(tenant_id=uuid.uuid4()) is False


# --------------------------------------------------------------------------
# cache reset helper isolates seeds
# --------------------------------------------------------------------------


async def test_cache_reset_reflects_updated_tenant_flag() -> None:
    """After a cache reset the resolver re-reads the (now flipped) tenant flag."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-flip", enabled=False)
    assert await should_capture(tenant_id=tenant_id) is False

    async with sessionmaker() as session:
        row = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        row.flight_recorder_enabled = True
        await session.commit()

    # Without a reset the 60s-TTL cache would still answer False.
    reset_flight_recorder_config_cache_for_testing()
    assert await should_capture(tenant_id=tenant_id) is True


# --------------------------------------------------------------------------
# targeted cache invalidation (#3272 -- the operator-mutation eviction path)
# --------------------------------------------------------------------------


async def test_invalidate_tenant_policy_cache_reflects_flipped_flag() -> None:
    """Evicting one tenant's cache re-reads its flipped policy without a restart."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-tenant-evict", enabled=False)
    assert await should_capture(tenant_id=tenant_id) is False  # primes the cache

    async with sessionmaker() as session:
        row = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        row.flight_recorder_enabled = True
        await session.commit()

    # The mutation surface calls exactly this after a policy write.
    invalidate_tenant_policy_cache(tenant_id)
    assert await should_capture(tenant_id=tenant_id) is True


async def test_invalidate_tenant_policy_cache_is_idempotent() -> None:
    """Popping an uncached / already-evicted tenant is a harmless no-op."""
    invalidate_tenant_policy_cache(uuid.uuid4())  # must not raise


async def test_invalidate_target_override_cache_reflects_flipped_override() -> None:
    """Evicting one target's cache re-reads its flipped tri-state override."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-target-evict", enabled=False)
        target_id = await _seed_target(session, tenant_id=tenant_id, name="tgt", override=None)
    # Inherit -> tenant default (OFF); primes both caches.
    assert await should_capture(tenant_id=tenant_id, target_id=target_id) is False

    async with sessionmaker() as session:
        row = (await session.execute(select(Target).where(Target.id == target_id))).scalar_one()
        row.flight_recorder_capture = True
        await session.commit()

    invalidate_target_override_cache(target_id)
    assert await should_capture(tenant_id=tenant_id, target_id=target_id) is True
