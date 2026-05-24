# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`meho_backplane.agents.service.AgentDefinitionService`.

Coverage (Task #809 / G11.1-T2 acceptance criteria):

* Round-trip -- create -> list -> get -> update -> delete on one
  definition, every field surviving.
* Duplicate create raises :class:`AgentDefinitionExistsError`.
* Cross-tenant isolation -- tenant B's definition is never visible to
  tenant A's list / get / update / delete (the cross-tenant calls
  return ``None`` / ``False``, not the other tenant's row).
* Partial update -- a single-field update leaves the rest unchanged.
* delete returns ``True`` on a hit, ``False`` on a miss (idempotent
  absence signal).

Runs against the conftest SQLite engine, pre-migrated to head.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agents.schemas import (
    AgentDefinitionCreate,
    AgentDefinitionUpdate,
    AgentModelTier,
)
from meho_backplane.agents.service import (
    AgentDefinitionExistsError,
    AgentDefinitionService,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant(session: AsyncSession, slug: str) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
    await session.commit()
    return tenant_id


def _create_body(name: str = "triage") -> AgentDefinitionCreate:
    return AgentDefinitionCreate(
        name=name,
        identity_ref="agent:triage",
        model_tier=AgentModelTier.DEEP,
        system_prompt="You triage incidents.",
        toolset={"allow": ["call_operation"]},
        turn_budget=25,
        output_schema={"type": "object"},
        enabled=True,
    )


@pytest.mark.asyncio
async def test_full_crud_round_trip() -> None:
    """create -> list -> get -> update -> delete round-trips on one row."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "rt")
    service = AgentDefinitionService()

    created = await service.create(tenant_id, "op-1", _create_body())
    assert created.name == "triage"
    assert created.model_tier == "deep"
    assert created.created_by_sub == "op-1"
    assert created.toolset == {"allow": ["call_operation"]}

    listed = await service.list_(tenant_id)
    assert [a.name for a in listed] == ["triage"]

    fetched = await service.get(tenant_id, "triage")
    assert fetched is not None
    assert fetched.id == created.id

    updated = await service.update(
        tenant_id,
        "triage",
        AgentDefinitionUpdate(turn_budget=50, enabled=False),
    )
    assert updated is not None
    assert updated.turn_budget == 50
    assert updated.enabled is False
    # Unchanged fields survive the partial update.
    assert updated.model_tier == "deep"
    assert updated.system_prompt == "You triage incidents."

    assert await service.delete(tenant_id, "triage") is True
    assert await service.get(tenant_id, "triage") is None
    assert await service.list_(tenant_id) == []


@pytest.mark.asyncio
async def test_duplicate_create_raises_exists() -> None:
    """A second create on the same ``(tenant, name)`` raises."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "dup")
    service = AgentDefinitionService()
    await service.create(tenant_id, "op-1", _create_body("dup-agent"))
    with pytest.raises(AgentDefinitionExistsError):
        await service.create(tenant_id, "op-2", _create_body("dup-agent"))


@pytest.mark.asyncio
async def test_model_tier_update_round_trips_value() -> None:
    """Updating model_tier stores the wire string, not the enum repr."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "tier")
    service = AgentDefinitionService()
    await service.create(tenant_id, "op-1", _create_body("tier-agent"))
    updated = await service.update(
        tenant_id,
        "tier-agent",
        AgentDefinitionUpdate(model_tier=AgentModelTier.FAST),
    )
    assert updated is not None
    assert updated.model_tier == "fast"


@pytest.mark.asyncio
async def test_cross_tenant_isolation() -> None:
    """Tenant B's definition is invisible to tenant A on every accessor."""
    async with get_sessionmaker()() as session:
        tenant_a = await _seed_tenant(session, "iso-a")
        tenant_b = await _seed_tenant(session, "iso-b")
    service = AgentDefinitionService()
    await service.create(tenant_b, "op-b", _create_body("b-agent"))

    # Tenant A sees nothing.
    assert await service.list_(tenant_a) == []
    assert await service.get(tenant_a, "b-agent") is None
    # Cross-tenant update / delete are no-ops (return None / False), and
    # crucially do NOT mutate tenant B's row.
    assert await service.update(tenant_a, "b-agent", AgentDefinitionUpdate(enabled=False)) is None
    assert await service.delete(tenant_a, "b-agent") is False

    # Tenant B's row is intact and unchanged.
    b_row = await service.get(tenant_b, "b-agent")
    assert b_row is not None
    assert b_row.enabled is True


@pytest.mark.asyncio
async def test_get_update_delete_missing_return_none_false() -> None:
    """Absent name returns ``None`` / ``False`` (the 404 the boundary renders)."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "missing")
    service = AgentDefinitionService()
    assert await service.get(tenant_id, "nope") is None
    assert await service.update(tenant_id, "nope", AgentDefinitionUpdate(enabled=False)) is None
    assert await service.delete(tenant_id, "nope") is False


@pytest.mark.asyncio
async def test_list_is_name_sorted() -> None:
    """``list_`` returns definitions sorted by name."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "sort")
    service = AgentDefinitionService()
    for name in ("zeta", "alpha", "mike"):
        await service.create(tenant_id, "op-1", _create_body(name))
    assert [a.name for a in await service.list_(tenant_id)] == ["alpha", "mike", "zeta"]
