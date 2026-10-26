# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Durable persistence of agent announcements (#2547).

``publish_agent_announcement`` writes an append-only ``agent_announcement``
row keyed on the event's minted UUID (``event_id``), persisting the typed
claim fields (T1 #2544) so the announcement survives a Valkey restart or
the stream's ``MAXLEN ~`` trim.

The stream backfill this file also covered was removed in #3758: every
Broadcast reader now reads the archive directly (see
``test_broadcast_agent_intent_cutover.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from meho_backplane.broadcast import (
    AgentAnnouncementEvent,
    get_broadcast_client,
    publish_agent_announcement,
    reset_broadcast_client_for_testing,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement, Tenant
from meho_backplane.settings import get_settings
from tests.broadcast_admission_helpers import fake_stream_write

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_broadcast_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin a stub broadcast URL + clear the cached client around each test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    reset_broadcast_client_for_testing()
    get_settings.cache_clear()


async def _seed_tenant() -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    tid = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(Tenant(id=tid, slug=f"t-{tid.hex[:8]}", name=f"Tenant {tid.hex[:6]}"))
        await session.commit()
    return tid


# ---------------------------------------------------------------------------
# Durable persistence on publish
# ---------------------------------------------------------------------------


async def test_publish_persists_durable_row_keyed_on_event_uuid() -> None:
    """The publisher writes one durable row keyed on the event's UUID."""
    tenant_id = await _seed_tenant()
    run_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    event = AgentAnnouncementEvent(
        tenant_id=tenant_id,
        principal_sub="op-1",
        activity="rotating tokens on cluster X",
        target="prod-vc-1",
        targets=["cluster-x"],
        scope="token rotation",
        planned_op_class="write",
        ttl_minutes=30,
        work_ref="gh:evoila/meho#123",
        run_id=run_id,
        phase="start",
        ts=datetime.now(UTC),
        claim_id=claim_id,
        owner_sub="agent:op-1",
        actor_sub="agent:op-1",
        admission="hosted_run",
    )
    bc = get_broadcast_client()
    with fake_stream_write(bc, AsyncMock(return_value="1747800000000-0")):
        result = await publish_agent_announcement(event, idempotency_key="key-1")

    # The result carries the stream entry id and the archive number (#3748).
    assert result.stream_entry_id == "1747800000000-0"
    assert result.seq == 1
    assert result.replayed is False
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(AgentAnnouncement, event.event_id)
    assert row is not None, "publish must persist a durable agent_announcement row"
    assert row.id == event.event_id
    assert row.tenant_id == tenant_id
    assert row.principal_sub == "op-1"
    assert row.activity == "rotating tokens on cluster X"
    assert row.target == "prod-vc-1"
    assert row.targets == ["cluster-x"]
    assert row.scope == "token rotation"
    assert row.planned_op_class == "write"
    assert row.ttl_minutes == 30
    assert row.work_ref == "gh:evoila/meho#123"
    assert row.run_id == run_id
    assert row.phase == "start"
    # #3736 trusted claim columns.
    assert row.claim_id == claim_id
    assert row.owner_sub == "agent:op-1"
    assert row.actor_sub == "agent:op-1"
    assert row.admission == "hosted_run"
    assert row.idempotency_key == "key-1"
