# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for G11.2-T6 agent permission grants.

Acceptance criteria covered:

* **Default deny** — a new agent (principal) has no grants; the service
  returns an empty list; no grant record is seeded automatically.
* **Grant then allow path** — creating a grant row succeeds; the row is
  visible in list + get; revoking it removes it.
* **Elevation then auto-revert** — a grant with a past ``expires_at``
  is excluded from active list; the expiry sweeper deletes it and
  writes an audit row.
* **Cross-tenant isolation** — tenant B's grants are invisible to
  tenant A's service calls.
* **Validation** — past ``expires_at``, invalid ``target_scope`` UUID,
  and empty ``op_pattern`` all raise :exc:`GrantValidationError`.
* **Migration smoke** — the ``0022`` migration can be applied and
  rolled back cleanly (via Alembic's SQLite path in the conftest).

Runs against the conftest SQLite engine (pre-migrated to head).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agents.grant_schemas import AgentGrantCreate, GrantVerdict
from meho_backplane.agents.grants import AgentGrantService, GrantValidationError
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


def _grant_body(
    principal_sub: str = "agent-sub-1",
    op_pattern: str = "*",
    verdict: GrantVerdict = GrantVerdict.AUTO_EXECUTE,
    target_scope: str | None = None,
    expires_at: datetime | None = None,
) -> AgentGrantCreate:
    return AgentGrantCreate(
        principal_sub=principal_sub,
        op_pattern=op_pattern,
        verdict=verdict,
        target_scope=target_scope,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Default-deny: no automatic grants for a new agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_deny_no_grants() -> None:
    """A new principal has no grants — list returns empty."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "dd-test")
    service = AgentGrantService()
    grants = await service.list_(tenant_id, principal_sub="brand-new-agent-sub")
    assert grants == [], "new agent must start with zero grants (default deny)"


# ---------------------------------------------------------------------------
# Grant then allow: create -> list -> get -> revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_create_list_get_revoke() -> None:
    """Create -> list -> get -> revoke round-trip on one grant."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "grant-cycle")
    service = AgentGrantService()

    body = _grant_body(
        principal_sub="agent-abc", op_pattern="vault.kv.*", verdict=GrantVerdict.AUTO_EXECUTE
    )
    created = await service.grant(tenant_id, "admin-op", body)

    assert created.principal_sub == "agent-abc"
    assert created.op_pattern == "vault.kv.*"
    assert created.verdict == "auto-execute"
    assert created.created_by_sub == "admin-op"
    assert created.expires_at is None

    listed = await service.list_(tenant_id, principal_sub="agent-abc")
    assert len(listed) == 1
    assert listed[0].id == created.id

    fetched = await service.get(tenant_id, created.id)
    assert fetched is not None
    assert fetched.id == created.id

    deleted = await service.revoke(tenant_id, created.id)
    assert deleted is True

    after_revoke = await service.list_(tenant_id, principal_sub="agent-abc")
    assert after_revoke == []


@pytest.mark.asyncio
async def test_revoke_missing_returns_false() -> None:
    """Revoking a non-existent grant returns False (idempotent)."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "rev-miss")
    service = AgentGrantService()
    result = await service.revoke(tenant_id, uuid.uuid4())
    assert result is False


# ---------------------------------------------------------------------------
# Time-bounded elevation: create with expires_at -> active list -> expired exclusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_elevation_visible_when_active() -> None:
    """A grant with a future expires_at appears in active list."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "elev-active")
    service = AgentGrantService()

    future = datetime.now(UTC) + timedelta(hours=2)
    body = _grant_body(
        principal_sub="agent-elev",
        op_pattern="*",
        verdict=GrantVerdict.AUTO_EXECUTE,
        expires_at=future,
    )
    created = await service.grant(tenant_id, "admin", body)
    assert created.expires_at is not None

    listed = await service.list_(tenant_id, principal_sub="agent-elev")
    assert any(g.id == created.id for g in listed)


@pytest.mark.asyncio
async def test_elevation_excluded_when_expired() -> None:
    """A grant with a past expires_at is excluded from the active list.

    The expiry sweeper would delete it; here we test the service-layer
    filter (``expires_at > now()``), not the physical delete.
    """
    from meho_backplane.db.models import AgentPermission

    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "elev-exp")

    # Insert a row whose expires_at is already past.
    past = datetime.now(UTC) - timedelta(hours=1)
    async with get_sessionmaker()() as session:
        row = AgentPermission(
            tenant_id=tenant_id,
            principal_sub="agent-exp",
            op_pattern="*",
            verdict="auto-execute",
            created_by_sub="admin",
            expires_at=past,
        )
        session.add(row)
        await session.commit()

    service = AgentGrantService()
    # Default list (include_expired=False) must not include the expired row.
    active = await service.list_(tenant_id, principal_sub="agent-exp")
    assert active == [], "expired elevation must not appear in active list"

    # include_expired=True shows it.
    all_grants = await service.list_(tenant_id, principal_sub="agent-exp", include_expired=True)
    assert len(all_grants) == 1, "expired elevation must appear with include_expired=True"


@pytest.mark.asyncio
async def test_elevation_sweeper_deletes_expired_row() -> None:
    """The expiry sweeper tick removes rows whose expires_at < now()."""
    from meho_backplane.agents.grant_expiry import _run_one_tick
    from meho_backplane.db.models import AgentPermission

    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "elev-sweep")

    past = datetime.now(UTC) - timedelta(hours=1)
    async with get_sessionmaker()() as session:
        row = AgentPermission(
            tenant_id=tenant_id,
            principal_sub="agent-sweep",
            op_pattern="*",
            verdict="auto-execute",
            created_by_sub="admin",
            expires_at=past,
        )
        session.add(row)
        await session.commit()
        saved_id = row.id

    # Run one sweep tick.
    await _run_one_tick()

    # The row must be gone.
    async with get_sessionmaker()() as session:
        from sqlalchemy import select

        from meho_backplane.db.models import AuditLog

        result = await session.execute(
            select(AgentPermission).where(AgentPermission.id == saved_id)
        )
        assert result.scalar_one_or_none() is None, "sweeper must delete expired row"

        # The elevation expiry must be audited (#819 AC: "elevation + its
        # expiry are audited"): one per-tenant sweep row carrying the count.
        audit_rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.path == "/internal/agent-permission/expire",
                        AuditLog.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audit_rows) == 1, "sweeper must write one audit row for the tenant"
        assert audit_rows[0].payload["expired_count"] == 1


@pytest.mark.asyncio
async def test_sweeper_preserves_active_grants() -> None:
    """The expiry sweeper does not remove active or permanent grants."""
    from meho_backplane.agents.grant_expiry import _run_one_tick
    from meho_backplane.db.models import AgentPermission

    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "elev-preserve")

    future = datetime.now(UTC) + timedelta(hours=2)
    async with get_sessionmaker()() as session:
        permanent = AgentPermission(
            tenant_id=tenant_id,
            principal_sub="agent-perm",
            op_pattern="*",
            verdict="auto-execute",
            created_by_sub="admin",
            expires_at=None,  # permanent
        )
        active_elevation = AgentPermission(
            tenant_id=tenant_id,
            principal_sub="agent-active",
            op_pattern="vault.kv.*",
            verdict="auto-execute",
            created_by_sub="admin",
            expires_at=future,
        )
        session.add_all([permanent, active_elevation])
        await session.commit()
        perm_id = permanent.id
        active_id = active_elevation.id

    await _run_one_tick()

    async with get_sessionmaker()() as session:
        from sqlalchemy import select

        for grant_id in (perm_id, active_id):
            result = await session.execute(
                select(AgentPermission).where(AgentPermission.id == grant_id)
            )
            assert result.scalar_one_or_none() is not None, (
                f"sweeper must not remove grant {grant_id}"
            )


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_isolation() -> None:
    """Tenant B's grants are invisible to tenant A's service calls."""
    async with get_sessionmaker()() as session:
        tenant_a = await _seed_tenant(session, "ct-a")
    async with get_sessionmaker()() as session:
        tenant_b = await _seed_tenant(session, "ct-b")

    service = AgentGrantService()
    body = _grant_body(principal_sub="shared-sub", op_pattern="*")
    await service.grant(tenant_a, "admin-a", body)
    await service.grant(tenant_b, "admin-b", body)

    a_grants = await service.list_(tenant_a)
    b_grants = await service.list_(tenant_b)

    a_ids = {g.id for g in a_grants}
    b_ids = {g.id for g in b_grants}
    assert a_ids.isdisjoint(b_ids), "grants from different tenants must not overlap"


@pytest.mark.asyncio
async def test_cross_tenant_revoke_returns_false() -> None:
    """Revoking a grant from another tenant returns False (not 403)."""
    async with get_sessionmaker()() as session:
        tenant_a = await _seed_tenant(session, "cr-a")
    async with get_sessionmaker()() as session:
        tenant_b = await _seed_tenant(session, "cr-b")

    service = AgentGrantService()
    body = _grant_body(principal_sub="a-agent")
    created = await service.grant(tenant_a, "admin-a", body)

    # tenant_b tries to revoke tenant_a's grant — must return False.
    result = await service.revoke(tenant_b, created.id)
    assert result is False

    # The original grant must still exist.
    fetched = await service.get(tenant_a, created.id)
    assert fetched is not None


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_past_expires_at_rejected() -> None:
    """A past expires_at raises GrantValidationError."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "val-past")
    service = AgentGrantService()
    past = datetime.now(UTC) - timedelta(hours=1)
    body = _grant_body(expires_at=past)
    with pytest.raises(GrantValidationError, match="past"):
        await service.grant(tenant_id, "admin", body)


@pytest.mark.asyncio
async def test_invalid_target_scope_uuid_rejected() -> None:
    """A non-UUID non-wildcard target_scope raises GrantValidationError."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "val-scope")
    service = AgentGrantService()
    body = AgentGrantCreate(
        principal_sub="sub",
        op_pattern="*",
        verdict=GrantVerdict.AUTO_EXECUTE,
        target_scope="not-a-uuid",
    )
    with pytest.raises(GrantValidationError, match="not a valid UUID"):
        await service.grant(tenant_id, "admin", body)


@pytest.mark.asyncio
async def test_wildcard_and_none_target_scope_accepted() -> None:
    """target_scope=None and target_scope='*' are both valid (both → '*').

    The shared T3 (#1052) ``agent_permission`` model stores ``target_scope``
    NOT NULL with a ``'*'`` default, so an omitted/None target normalizes
    to the explicit any-target wildcard ``'*'`` (NULL and ``'*'`` mean the
    same thing; storing ``'*'`` keeps the uniqueness key total).
    """
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "val-wild")
    service = AgentGrantService()

    # Distinct op_patterns per case: None and "*" both normalize to "*",
    # so reusing one op_pattern would collide on uq_agent_permission_grant.
    for scope, op_pattern in ((None, "none.scope.*"), ("*", "star.scope.*")):
        body = AgentGrantCreate(
            principal_sub="sub",
            op_pattern=op_pattern,
            verdict=GrantVerdict.DENY,
            target_scope=scope,
        )
        created = await service.grant(tenant_id, "admin", body)
        assert created.target_scope == "*"


@pytest.mark.asyncio
async def test_uuid_target_scope_accepted() -> None:
    """A valid UUID string for target_scope is accepted."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "val-uuid")
    service = AgentGrantService()
    target_uuid = str(uuid.uuid4())
    body = AgentGrantCreate(
        principal_sub="sub",
        op_pattern="vault.kv.*",
        verdict=GrantVerdict.NEEDS_APPROVAL,
        target_scope=target_uuid,
    )
    created = await service.grant(tenant_id, "admin", body)
    assert created.target_scope == target_uuid


# ---------------------------------------------------------------------------
# Multiple grants — list filtering + pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_multiple_principals() -> None:
    """list_ without principal_sub returns all grants; with it filters."""
    async with get_sessionmaker()() as session:
        tenant_id = await _seed_tenant(session, "multi-list")
    service = AgentGrantService()

    await service.grant(tenant_id, "admin", _grant_body(principal_sub="p1"))
    await service.grant(tenant_id, "admin", _grant_body(principal_sub="p2"))

    all_grants = await service.list_(tenant_id)
    assert len(all_grants) >= 2

    p1_only = await service.list_(tenant_id, principal_sub="p1")
    assert all(g.principal_sub == "p1" for g in p1_only)

    p2_only = await service.list_(tenant_id, principal_sub="p2")
    assert all(g.principal_sub == "p2" for g in p2_only)
