# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`meho_backplane.agents.service.AgentDefinitionService`.

Coverage (Task #809 / G11.1-T2 acceptance criteria, extended by
Task #1099 / G11.2-T8):

* Round-trip -- create -> list -> get -> update -> delete on one
  definition, every field surviving.
* Duplicate create raises :class:`AgentDefinitionExistsError`.
* Cross-tenant isolation -- tenant B's definition is never visible to
  tenant A's list / get / update / delete (the cross-tenant calls
  return ``None`` / ``False``, not the other tenant's row).
* Partial update -- a single-field update leaves the rest unchanged.
* delete returns ``True`` on a hit, ``False`` on a miss (idempotent
  absence signal).
* **G11.2-T8**: ``identity_ref`` is validated against the
  ``agent_principal`` registry on create and on update -- unknown /
  revoked / cross-tenant references are rejected with
  :class:`AgentIdentityRefInvalidError`. Updates that don't touch
  ``identity_ref`` skip the validation.

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
    AgentIdentityRefInvalidError,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, Tenant
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


async def _seed_principal(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
    *,
    revoked: bool = False,
) -> None:
    """Seed an ``agent_principal`` row matching ``identity_ref="agent:<name>"``.

    Tests that exercise :meth:`AgentDefinitionService.create` /
    :meth:`AgentDefinitionService.update` need a principal seeded for
    the identity_ref they pass, otherwise the new validation in T8
    rejects the call. The seed values mirror what
    :class:`~meho_backplane.auth.agent_principals.AgentPrincipalService.register`
    persists in the real lifecycle: ``keycloak_client_id="agent:<name>"``
    is the convention the validator matches on.
    """
    session.add(
        AgentPrincipal(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=name,
            keycloak_client_id=f"agent:{name}",
            keycloak_internal_id=f"kc-internal-{name}",
            owner_sub="op-1",
            revoked=revoked,
            created_by_sub="op-1",
        )
    )
    await session.commit()


def _create_body(
    name: str = "triage",
    *,
    identity_ref: str | None = None,
) -> AgentDefinitionCreate:
    """Build a valid :class:`AgentDefinitionCreate` body for *name*.

    ``identity_ref`` defaults to ``"agent:<name>"`` so a parallel call to
    :func:`_seed_principal(session, tenant_id, name)` is sufficient to
    make the service's T8 validation pass. Tests that need a different
    identity_ref (e.g. cross-tenant probes) pass it explicitly.
    """
    return AgentDefinitionCreate(
        name=name,
        identity_ref=identity_ref if identity_ref is not None else f"agent:{name}",
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
        await _seed_principal(session, tenant_id, "triage")
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
        await _seed_principal(session, tenant_id, "dup-agent")
    service = AgentDefinitionService()
    await service.create(tenant_id, "op-1", _create_body("dup-agent"))
    with pytest.raises(AgentDefinitionExistsError):
        await service.create(tenant_id, "op-2", _create_body("dup-agent"))


@pytest.mark.asyncio
async def test_model_tier_update_round_trips_value() -> None:
    """Updating model_tier stores the wire string, not the enum repr."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "tier")
        await _seed_principal(session, tenant_id, "tier-agent")
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
        await _seed_principal(session, tenant_b, "b-agent")
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
        for name in ("zeta", "alpha", "mike"):
            await _seed_principal(session, tenant_id, name)
    service = AgentDefinitionService()
    for name in ("zeta", "alpha", "mike"):
        await service.create(tenant_id, "op-1", _create_body(name))
    assert [a.name for a in await service.list_(tenant_id)] == ["alpha", "mike", "zeta"]


# ---------------------------------------------------------------------------
# G11.2-T8 (#1099) -- identity_ref validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_unknown_identity_ref_rejected() -> None:
    """create raises ``AgentIdentityRefInvalidError`` when no principal matches."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "t8-unknown")
        # NOTE: deliberately no _seed_principal call -- the identity_ref
        # below has no match in the registry.
    service = AgentDefinitionService()
    with pytest.raises(AgentIdentityRefInvalidError) as exc_info:
        await service.create(tenant_id, "op-1", _create_body("orphan"))
    assert exc_info.value.identity_ref == "agent:orphan"
    assert exc_info.value.reason == "unknown"

    # The reject must not have left a half-written row.
    assert await service.list_(tenant_id) == []


@pytest.mark.asyncio
async def test_create_revoked_identity_ref_rejected() -> None:
    """create raises when the matching principal is marked revoked."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "t8-revoked")
        await _seed_principal(session, tenant_id, "revoked-bot", revoked=True)
    service = AgentDefinitionService()
    with pytest.raises(AgentIdentityRefInvalidError) as exc_info:
        await service.create(tenant_id, "op-1", _create_body("revoked-bot"))
    assert exc_info.value.identity_ref == "agent:revoked-bot"
    assert exc_info.value.reason == "revoked"


@pytest.mark.asyncio
async def test_create_cross_tenant_identity_ref_rejected() -> None:
    """create rejects an identity_ref that resolves only in *another* tenant.

    The principal exists in tenant B; tenant A's create must still see
    it as ``unknown`` -- the existence of tenant B's principal is never
    leaked across the tenant boundary (the structured reason is
    ``unknown``, not ``cross_tenant``, for the same reason the boundary
    layer collapses every reason into a single 4xx code).
    """
    async with get_sessionmaker()() as session:
        tenant_a = await _seed_tenant(session, "t8-xa")
        tenant_b = await _seed_tenant(session, "t8-xb")
        await _seed_principal(session, tenant_b, "shared-name")
    service = AgentDefinitionService()
    with pytest.raises(AgentIdentityRefInvalidError) as exc_info:
        await service.create(tenant_a, "op-1", _create_body("shared-name"))
    assert exc_info.value.reason == "unknown"

    # Tenant B's create still succeeds -- the principal is valid there.
    created = await service.create(tenant_b, "op-1", _create_body("shared-name"))
    assert created.identity_ref == "agent:shared-name"


@pytest.mark.asyncio
async def test_update_changes_identity_ref_validates() -> None:
    """An update that sets a *new* identity_ref re-runs the validation."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "t8-upd")
        await _seed_principal(session, tenant_id, "first")
        # 'second' is unknown by design -- the update should reject.
    service = AgentDefinitionService()
    await service.create(tenant_id, "op-1", _create_body("agent-x", identity_ref="agent:first"))

    with pytest.raises(AgentIdentityRefInvalidError) as exc_info:
        await service.update(
            tenant_id,
            "agent-x",
            AgentDefinitionUpdate(identity_ref="agent:second"),
        )
    assert exc_info.value.identity_ref == "agent:second"
    assert exc_info.value.reason == "unknown"

    # The reject must not have persisted the new identity_ref. The
    # row's identity_ref should still be the originally-validated value.
    row = await service.get(tenant_id, "agent-x")
    assert row is not None
    assert row.identity_ref == "agent:first"


@pytest.mark.asyncio
async def test_update_other_fields_skips_identity_ref_revalidation() -> None:
    """A PATCH that doesn't include identity_ref doesn't re-validate it.

    Otherwise revoking a principal *after* an AgentDefinition was
    created would silently block every subsequent unrelated update
    (a turn_budget bump, a system_prompt edit). The runtime-time
    check that the principal is still live at invocation time is
    G11.3's responsibility, not this validator's.
    """
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "t8-skip")
        await _seed_principal(session, tenant_id, "stable-bot")
    service = AgentDefinitionService()
    await service.create(tenant_id, "op-1", _create_body("stable-bot"))

    # Revoke the principal AFTER the definition was created. A subsequent
    # update that doesn't touch identity_ref must still succeed.
    async with get_sessionmaker()() as session:
        principal = (
            (
                await session.execute(
                    AgentPrincipal.__table__.select().where(
                        AgentPrincipal.tenant_id == tenant_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        from sqlalchemy import update as sa_update

        await session.execute(
            sa_update(AgentPrincipal)
            .where(AgentPrincipal.id == principal["id"])
            .values(revoked=True)
        )
        await session.commit()

    updated = await service.update(
        tenant_id,
        "stable-bot",
        AgentDefinitionUpdate(turn_budget=99),
    )
    assert updated is not None
    assert updated.turn_budget == 99
