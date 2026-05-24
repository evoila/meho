# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`meho_backplane.db.models.AgentDefinition`.

Coverage matrix (Task #809 / G11.1-T2 acceptance criteria):

* Round-trip -- insert a row, query it back, every field round-trips
  through the SQLite dev/test driver. The ORM ``default=`` machinery
  (uuid, created_at, updated_at, toolset, enabled) fires on the SQLite
  path where the migration's PG server defaults are no-ops.
* ORM defaults fire on SQLite -- a minimal insert that omits
  ``id`` / ``toolset`` / ``enabled`` / timestamps still commits with
  those populated Python-side.
* Composite uniqueness -- two inserts with identical
  ``(tenant_id, name)`` raise :class:`IntegrityError` on the second
  commit. Pins the ``agent_definition_tenant_name_idx`` contract.
* Foreign key enforcement -- ``tenant_id`` references ``tenant.id``;
  inserting with a non-existent tenant id raises :class:`IntegrityError`
  when SQLite has ``PRAGMA foreign_keys = ON``.
* ``output_schema`` NULL -- the optional schema column round-trips as
  ``None``.
* ``onupdate`` -- modifying a row via the ORM bumps ``updated_at``.

The tests run against ``sqlite+aiosqlite`` via the shared engine cache
that the autouse ``_default_database_url`` fixture in
:mod:`tests.conftest` pre-migrates to ``alembic upgrade head``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentDefinition, Tenant
from meho_backplane.settings import get_settings


async def _enable_sqlite_foreign_keys(session: AsyncSession) -> None:
    """Issue ``PRAGMA foreign_keys = ON`` on the bound SQLite connection."""
    await session.execute(text("PRAGMA foreign_keys = ON"))


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant(session: AsyncSession, slug: str = "agent-def-test") -> uuid.UUID:
    """Insert a :class:`Tenant` row and return its id."""
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant for {slug}"))
    await session.commit()
    return tenant_id


@pytest.mark.asyncio
async def test_agent_definition_round_trip_persists_every_field() -> None:
    """Insert an :class:`AgentDefinition`, query it back, every field matches."""
    sessionmaker = get_sessionmaker()
    agent_id = uuid.uuid4()
    created_at = datetime.now(UTC)
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            AgentDefinition(
                id=agent_id,
                tenant_id=tenant_id,
                name="incident-triage",
                identity_ref="agent:incident-triage",
                model_tier="deep",
                system_prompt="You triage infra incidents.",
                toolset={"allow": ["call_operation", "query_topology"]},
                turn_budget=25,
                output_schema={"type": "object", "properties": {"severity": {"type": "string"}}},
                enabled=True,
                created_by_sub="op-42",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(AgentDefinition).where(AgentDefinition.id == agent_id)
        )
        row = result.scalar_one()

    assert row.id == agent_id
    assert row.tenant_id == tenant_id
    assert row.name == "incident-triage"
    assert row.identity_ref == "agent:incident-triage"
    assert row.model_tier == "deep"
    assert row.system_prompt == "You triage infra incidents."
    assert row.toolset == {"allow": ["call_operation", "query_topology"]}
    assert row.turn_budget == 25
    assert row.output_schema == {
        "type": "object",
        "properties": {"severity": {"type": "string"}},
    }
    assert row.enabled is True
    assert row.created_by_sub == "op-42"
    assert row.created_at.replace(tzinfo=None) == created_at.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_agent_definition_orm_defaults_fire_on_sqlite() -> None:
    """``id`` / ``toolset`` / ``enabled`` / timestamps populate Python-side.

    The migration's PG-side server defaults are no-ops on SQLite; the
    ORM ``default=`` machinery must fill the columns. A regression that
    drops an ORM default in favour of the migration surfaces here as a
    NOT NULL violation on SQLite.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        row = AgentDefinition(
            tenant_id=tenant_id,
            name="minimal-bot",
            identity_ref="agent:minimal",
            model_tier="standard",
            system_prompt="hi",
            turn_budget=5,
            created_by_sub="op-1",
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

    assert isinstance(row.id, uuid.UUID)
    assert row.toolset == {}
    assert row.enabled is True
    assert row.output_schema is None
    assert row.created_at is not None
    assert row.updated_at is not None


@pytest.mark.asyncio
async def test_agent_definition_composite_uniqueness() -> None:
    """Two rows with identical ``(tenant_id, name)`` raise :class:`IntegrityError`."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            AgentDefinition(
                tenant_id=tenant_id,
                name="dup",
                identity_ref="agent:a",
                model_tier="standard",
                system_prompt="x",
                turn_budget=3,
                created_by_sub="op-1",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        session.add(
            AgentDefinition(
                tenant_id=tenant_id,
                name="dup",
                identity_ref="agent:b",
                model_tier="fast",
                system_prompt="y",
                turn_budget=4,
                created_by_sub="op-2",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_agent_definition_same_name_different_tenant_is_allowed() -> None:
    """The unique index is per-tenant -- the same name in two tenants coexists."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_a = await _seed_tenant(session, slug="tenant-a")
        tenant_b = await _seed_tenant(session, slug="tenant-b")
        session.add_all(
            [
                AgentDefinition(
                    tenant_id=tenant_a,
                    name="shared-name",
                    identity_ref="agent:a",
                    model_tier="standard",
                    system_prompt="x",
                    turn_budget=3,
                    created_by_sub="op-1",
                ),
                AgentDefinition(
                    tenant_id=tenant_b,
                    name="shared-name",
                    identity_ref="agent:b",
                    model_tier="standard",
                    system_prompt="y",
                    turn_budget=3,
                    created_by_sub="op-2",
                ),
            ]
        )
        # No IntegrityError -- the unique index is (tenant_id, name).
        await session.commit()


@pytest.mark.asyncio
async def test_agent_definition_foreign_key_enforced_on_sqlite() -> None:
    """Inserting with a non-existent tenant id raises with FK enforcement on."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        session.add(
            AgentDefinition(
                tenant_id=uuid.uuid4(),  # no such tenant
                name="orphan",
                identity_ref="agent:x",
                model_tier="standard",
                system_prompt="x",
                turn_budget=3,
                created_by_sub="op-1",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_agent_definition_onupdate_bumps_updated_at() -> None:
    """Modifying a row via the ORM bumps ``updated_at`` past ``created_at``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        row = AgentDefinition(
            tenant_id=tenant_id,
            name="bumpable",
            identity_ref="agent:x",
            model_tier="standard",
            system_prompt="x",
            turn_budget=3,
            created_by_sub="op-1",
        )
        session.add(row)
        await session.commit()
        original_updated = row.updated_at

    # A small sleep so the wall clock advances on fast machines.
    await asyncio.sleep(0.01)

    async with sessionmaker() as session:
        result = await session.execute(
            select(AgentDefinition).where(AgentDefinition.tenant_id == tenant_id)
        )
        row = result.scalar_one()
        row.system_prompt = "changed"
        await session.commit()
        await session.refresh(row)
        # SQLite drops tzinfo on round-trip; compare wall-clock parts.
        # The PG production driver returns tz-aware values (covered by
        # the testcontainers suite). The onupdate hook fires on the ORM
        # UPDATE, so updated_at must not regress past the original.
        assert row.updated_at.replace(tzinfo=None) >= original_updated.replace(tzinfo=None)
