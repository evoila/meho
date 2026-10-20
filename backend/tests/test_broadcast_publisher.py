# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Broadcast publisher after #3758.

Broadcast carries deliberate agent announcements only. Covers:

* The automatic publish path is gone: ``publish_event`` no longer exists
  on the broadcast package or the publisher module.
* An announcement is stored in the archive, then written once to
  ``meho:feed:{tenant_id}`` as a single ``event`` field with
  ``maxlen=BROADCAST_MAXLEN`` and ``approximate=True`` (``MAXLEN ~``).
* Per-tenant isolation: two tenants land on two stream keys.
* A failed stream write keeps the archive row, returns
  ``delivery_pending=True`` and increments
  ``broadcast_publish_errors_total`` (the counter the chart's
  ``MehoBroadcastPublishErrors`` alert watches).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

import meho_backplane.broadcast as broadcast_pkg
from meho_backplane.broadcast import (
    BROADCAST_MAXLEN,
    BROADCAST_PUBLISH_ERRORS_TOTAL,
    get_broadcast_client,
    publish_agent_announcement,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast import publisher as publisher_module
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement, Tenant
from meho_backplane.settings import get_settings
from tests.broadcast_admission_helpers import fake_stream_write, trusted_event

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
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="Tenant"))
        await session.commit()
    return tenant_id


async def test_automatic_publish_path_is_gone() -> None:
    """Nothing outside the announce path can write a Broadcast event (#3758)."""
    assert not hasattr(publisher_module, "publish_event")
    assert not hasattr(broadcast_pkg, "publish_event")
    assert "publish_event" not in broadcast_pkg.__all__
    assert not hasattr(publisher_module, "BROADCAST_EVENTS_PUBLISHED_TOTAL")


async def test_announcement_xadds_to_tenant_stream_with_maxlen() -> None:
    """One XADD on the tenant key, a single ``event`` field, ``MAXLEN ~``."""
    tenant_id = await _seed_tenant()
    event = trusted_event(tenant_id=tenant_id)
    xadd = AsyncMock(return_value="1747800000000-0")
    with fake_stream_write(get_broadcast_client(), xadd):
        result = await publish_agent_announcement(event, idempotency_key="k-1")

    assert result.stream_entry_id == "1747800000000-0"
    assert result.delivery_pending is False
    xadd.assert_awaited_once()
    args, kwargs = xadd.await_args
    assert args[0] == f"meho:feed:{tenant_id}"
    assert set(args[1]) == {"event"}
    assert json.loads(args[1]["event"])["event_id"] == str(event.event_id)
    assert kwargs == {"maxlen": BROADCAST_MAXLEN, "approximate": True}


async def test_per_tenant_isolation() -> None:
    """Two tenants' announcements land on two different stream keys."""
    tenant_a = await _seed_tenant()
    tenant_b = await _seed_tenant()
    xadd = AsyncMock(side_effect=["1-0", "2-0"])
    with fake_stream_write(get_broadcast_client(), xadd):
        await publish_agent_announcement(trusted_event(tenant_id=tenant_a))
        await publish_agent_announcement(trusted_event(tenant_id=tenant_b))

    keys = [call.args[0] for call in xadd.await_args_list]
    assert keys == [f"meho:feed:{tenant_a}", f"meho:feed:{tenant_b}"]


async def test_stream_failure_counts_error_and_keeps_archive_row() -> None:
    """A Valkey failure after the archive commit is fail-soft and counted."""
    tenant_id = await _seed_tenant()
    event = trusted_event(tenant_id=tenant_id)
    before = BROADCAST_PUBLISH_ERRORS_TOTAL._value.get()
    xadd = AsyncMock(side_effect=RedisConnectionError("unreachable"))
    with fake_stream_write(get_broadcast_client(), xadd):
        result = await publish_agent_announcement(event, idempotency_key="k-2")

    assert result.delivery_pending is True
    assert result.stream_entry_id is None
    assert BROADCAST_PUBLISH_ERRORS_TOTAL._value.get() == before + 1
    async with get_sessionmaker()() as session:
        row = await session.get(AgentAnnouncement, event.event_id)
    assert row is not None
    assert row.stream_entry_id is None
