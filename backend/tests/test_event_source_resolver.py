# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Resolver tests for the ``event_source`` registry (#2880).

Asserts the tenant-scoped admin resolver conflates missing/cross-tenant
into a uniform 404 with no near-miss oracle, and the global ingest
primitive returns a bare ``None`` (any status) for a live-miss.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.event_source.resolver import (
    EventSourceNotFoundError,
    resolve_event_source,
    resolve_event_source_by_slug,
)

from ._event_source_helpers import (
    _insert_event_source,
    _settings_env,  # noqa: F401  (autouse fixture)
)

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


@pytest.mark.asyncio
async def test_resolve_event_source_found() -> None:
    await _insert_event_source(tenant_id=_TENANT_A, name="am", slug="am-prod")
    async with get_sessionmaker()() as session:
        row = await resolve_event_source(session, _TENANT_A, "am-prod")
    assert row.slug == "am-prod"


@pytest.mark.asyncio
async def test_resolve_missing_is_uniform_404_without_matches() -> None:
    async with get_sessionmaker()() as session:
        with pytest.raises(EventSourceNotFoundError) as exc:
            await resolve_event_source(session, _TENANT_A, "nope")
    assert exc.value.status_code == 404
    # No existence-oracle: the detail carries no near-miss suggestions.
    assert "matches" not in exc.value.detail
    assert exc.value.detail == {"error": "no_event_source", "slug": "nope"}


@pytest.mark.asyncio
async def test_resolve_cross_tenant_is_same_404_as_missing() -> None:
    """A slug owned by another tenant returns the identical 404 as absent."""
    await _insert_event_source(tenant_id=_TENANT_B, name="am", slug="b-owned")
    async with get_sessionmaker()() as session:
        with pytest.raises(EventSourceNotFoundError) as cross:
            await resolve_event_source(session, _TENANT_A, "b-owned")
        with pytest.raises(EventSourceNotFoundError) as missing:
            await resolve_event_source(session, _TENANT_A, "b-owned")
    # Byte-for-byte identical detail -> no way to tell "not yours" from "absent".
    assert cross.value.detail == {"error": "no_event_source", "slug": "b-owned"}
    assert missing.value.detail == cross.value.detail


@pytest.mark.asyncio
async def test_resolve_soft_deleted_not_found() -> None:
    row = await _insert_event_source(tenant_id=_TENANT_A, name="am", slug="gone")
    async with get_sessionmaker()() as session:
        from meho_backplane.db.models import EventSource as EventSourceORM

        live = await session.get(EventSourceORM, row.id)
        assert live is not None
        live.deleted_at = datetime.now(UTC)
        await session.commit()
    async with get_sessionmaker()() as session:
        with pytest.raises(EventSourceNotFoundError):
            await resolve_event_source(session, _TENANT_A, "gone")


@pytest.mark.asyncio
async def test_resolve_by_slug_global_returns_row_any_status() -> None:
    """The ingest primitive resolves globally and returns paused rows too."""
    await _insert_event_source(tenant_id=_TENANT_B, name="am", slug="ingest-me", status="paused")
    async with get_sessionmaker()() as session:
        row = await resolve_event_source_by_slug(session, "ingest-me")
    assert row is not None
    assert row.status == "paused"


@pytest.mark.asyncio
async def test_resolve_by_slug_missing_returns_none() -> None:
    async with get_sessionmaker()() as session:
        assert await resolve_event_source_by_slug(session, "no-such-slug") is None


@pytest.mark.asyncio
async def test_resolve_by_slug_soft_deleted_returns_none() -> None:
    row = await _insert_event_source(tenant_id=_TENANT_A, name="am", slug="dead")
    async with get_sessionmaker()() as session:
        from meho_backplane.db.models import EventSource as EventSourceORM

        live = await session.get(EventSourceORM, row.id)
        assert live is not None
        live.deleted_at = datetime.now(UTC)
        await session.commit()
    async with get_sessionmaker()() as session:
        assert await resolve_event_source_by_slug(session, "dead") is None
