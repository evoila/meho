# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``agents.delete`` cascades dependent ``scheduled_trigger`` rows (#1480).

Issue #1480 (G0.19 v0.10.0 dogfood hardening). Migration ``0035`` adds
``ON DELETE CASCADE`` to ``scheduled_trigger.agent_definition_id`` so a
definition that ever had a trigger created against it -- including a
now-**cancelled** one ``cancel()`` retains for audit -- is still
deletable. Before the fix the bulk Core ``DELETE`` in
:meth:`AgentDefinitionService.delete` hit the no-``ondelete`` FK and
surfaced an opaque ``-32603 "internal error: IntegrityError"`` on MCP
and an unhandled HTTP 500 on REST.

Why this module opts into SQLite FK enforcement
-----------------------------------------------

SQLite enforces foreign keys only when ``PRAGMA foreign_keys = ON`` is
issued per connection (default OFF). Production runs on PostgreSQL where
FKs -- and therefore the new ``ON DELETE CASCADE`` -- are always
enforced. Without the PRAGMA, the cascade never fires and a regression
(e.g. the migration silently dropping the cascade) would pass on SQLite
by accident. Setting ``MEHO_SQLITE_FOREIGN_KEYS=1`` and resetting the
engine attaches the PRAGMA listener (see
:func:`db.engine.create_engine_for_url`), mirroring the precedent in
:mod:`tests.test_topology_history_hook`.

The cascade is a **DB-level** FK clause, not an ORM ``cascade=``
relationship, precisely because :meth:`AgentDefinitionService.delete`
issues a bulk Core ``DELETE`` that bypasses the unit-of-work cascade.
The service-layer test below proves the bulk Core delete triggers the
DB-level cascade; the REST and MCP tests prove neither surface returns
the opaque error any more.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import (
    AgentDefinition,
    AgentPrincipal,
    ScheduledTrigger,
    ScheduledTriggerInFlightPolicy,
    ScheduledTriggerKind,
    ScheduledTriggerStatus,
    Tenant,
)
from meho_backplane.mcp.tools import agents as agents_mcp
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)


@pytest.fixture(autouse=True)
def _enforce_sqlite_foreign_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Opt this module in to SQLite foreign-key enforcement.

    The whole point of #1480 is the ``ON DELETE CASCADE`` on
    ``scheduled_trigger.agent_definition_id``; that cascade only fires
    when SQLite has ``PRAGMA foreign_keys = ON``. Flipping
    ``MEHO_SQLITE_FOREIGN_KEYS=1`` and resetting the cached engine
    rebuilds it with the per-connection PRAGMA listener attached, on top
    of the per-test DB the conftest already migrated to head (so the
    0035 cascade FK is present). Mirrors
    :func:`tests.test_topology_history_hook._enforce_sqlite_foreign_keys`.
    """
    monkeypatch.setenv("MEHO_SQLITE_FOREIGN_KEYS", "1")
    reset_engine_for_testing()
    yield
    reset_engine_for_testing()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars :class:`Settings` requires (the conftest pins only URL)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_definition_with_triggers(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str = "incident-triage",
) -> uuid.UUID:
    """Insert a tenant + principal + definition + one active + one cancelled trigger.

    Returns the definition id. The two triggers cover both blocking
    shapes #1480 calls out: an ``active`` trigger (a live schedule) and
    a ``cancelled`` one (``cancel()`` retains the row for audit). Both
    hold the FK and -- pre-fix -- both blocked the delete.
    """
    now = datetime.now(UTC)
    definition_id = uuid.uuid4()
    existing = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    if existing.scalar_one_or_none() is None:
        # Commit the parent tenant on its own so the FK from
        # agent_definition / agent_principal resolves under PRAGMA
        # foreign_keys=ON regardless of unit-of-work flush ordering.
        session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="T"))
        await session.commit()
    session.add(
        AgentPrincipal(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=name,
            keycloak_client_id=f"agent:{name}",
            keycloak_internal_id=f"kc-internal-{tenant_id.hex[:8]}-{name}",
            owner_sub="op-admin",
            revoked=False,
            created_by_sub="op-admin",
        )
    )
    session.add(
        AgentDefinition(
            id=definition_id,
            tenant_id=tenant_id,
            name=name,
            identity_ref=f"agent:{name}",
            model_tier="standard",
            system_prompt="You triage incidents.",
            toolset={},
            turn_budget=10,
            created_by_sub="op-admin",
        )
    )
    for status in (ScheduledTriggerStatus.ACTIVE, ScheduledTriggerStatus.CANCELLED):
        session.add(
            ScheduledTrigger(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                agent_definition_id=definition_id,
                kind=ScheduledTriggerKind.CRON.value,
                cron_expr="*/5 * * * *",
                status=status.value,
                in_flight_policy=ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT.value,
                identity_sub="__scheduler__",
                created_by_sub="op-admin",
                created_at=now,
                updated_at=now,
            )
        )
    await session.commit()
    return definition_id


async def _count_triggers(session: AsyncSession, definition_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(ScheduledTrigger)
        .where(ScheduledTrigger.agent_definition_id == definition_id)
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Service layer -- the bulk Core DELETE triggers the DB-level cascade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_delete_cascades_active_and_cancelled_triggers() -> None:
    """``service.delete`` removes the definition and cascade-deletes its triggers.

    Proves the bulk Core ``DELETE ... RETURNING`` fires the DB-level
    ``ON DELETE CASCADE`` -- an ORM ``cascade=`` relationship would not,
    since the delete never goes through the unit of work.
    """
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        definition_id = await _seed_definition_with_triggers(session, tenant_id=tenant_id)
        assert await _count_triggers(session, definition_id) == 2

    service = AgentDefinitionService()
    deleted = await service.delete(tenant_id, "incident-triage")
    assert deleted is True

    async with sessionmaker() as session:
        gone = await session.execute(
            select(AgentDefinition).where(AgentDefinition.id == definition_id)
        )
        assert gone.scalar_one_or_none() is None
        # The dependent triggers -- active and cancelled alike -- went
        # with the parent. No orphan rows pinning the FK.
        assert await _count_triggers(session, definition_id) == 0


@pytest.mark.asyncio
async def test_service_delete_does_not_touch_other_definitions_triggers() -> None:
    """The cascade is scoped to the deleted definition's triggers only."""
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        keep_id = await _seed_definition_with_triggers(session, tenant_id=tenant_id, name="keep-me")
        drop_id = await _seed_definition_with_triggers(session, tenant_id=tenant_id, name="drop-me")

    service = AgentDefinitionService()
    assert await service.delete(tenant_id, "drop-me") is True

    async with sessionmaker() as session:
        assert await _count_triggers(session, drop_id) == 0
        # The untouched definition keeps both of its triggers.
        assert await _count_triggers(session, keep_id) == 2
        survivor = await session.execute(
            select(AgentDefinition).where(AgentDefinition.id == keep_id)
        )
        assert survivor.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# REST surface -- clean 204, not an unhandled 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_delete_scheduled_definition_returns_204() -> None:
    """``DELETE /api/v1/agents/{name}`` on a scheduled definition is a clean 204.

    Drives the real REST handler. Pre-fix this raised an uncaught
    :class:`IntegrityError` -> HTTP 500; with the cascade it deletes the
    definition + its triggers and returns 204 No Content.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        definition_id = await _seed_definition_with_triggers(session, tenant_id=OPERATOR_TENANT_ID)

    operator = build_operator(TenantRole.TENANT_ADMIN)
    from meho_backplane.api.v1.agents import delete_agent

    response = await delete_agent(name="incident-triage", operator=operator)
    assert response.status_code == 204

    async with sessionmaker() as session:
        gone = await session.execute(
            select(AgentDefinition).where(AgentDefinition.id == definition_id)
        )
        assert gone.scalar_one_or_none() is None
        assert await _count_triggers(session, definition_id) == 0


# ---------------------------------------------------------------------------
# MCP surface -- {removed: true}, not -32603
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_delete_scheduled_definition_returns_removed() -> None:
    """The ``meho.agents.delete`` handler returns ``{removed: true}``.

    Drives the real MCP tool handler. Pre-fix the uncaught
    :class:`IntegrityError` was mapped to ``-32603 "internal error:
    IntegrityError"`` by the dispatcher's bare-``except`` arm; with the
    cascade the handler returns its success payload.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        definition_id = await _seed_definition_with_triggers(session, tenant_id=OPERATOR_TENANT_ID)

    operator = build_operator(TenantRole.TENANT_ADMIN)
    result = await agents_mcp._delete_handler(operator, {"name": "incident-triage"})
    assert result == {"removed": True}

    async with sessionmaker() as session:
        gone = await session.execute(
            select(AgentDefinition).where(AgentDefinition.id == definition_id)
        )
        assert gone.scalar_one_or_none() is None
        assert await _count_triggers(session, definition_id) == 0


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_mcp_delete_scheduled_definition_over_the_wire(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """End-to-end: a JSON-RPC ``tools/call`` delete on a scheduled definition succeeds.

    Exercises the full dispatcher path -- the surface that produced the
    original ``-32603`` envelope. Asserts no JSON-RPC ``error`` object is
    present and the result is the ``{removed: true}`` payload.
    """
    client, operator = client_with_operator
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _seed_definition_with_triggers(session, tenant_id=operator.tenant_id)

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.agents.delete",
                "arguments": {"name": "incident-triage"},
            },
        },
    )
    body = response.json()
    assert "error" not in body, body
    payload: dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    assert payload == {"removed": True}
