# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""DB-layer tests for the ``event_source`` model (#2880).

Covers the columns, the two partial unique indexes (``name`` unique per
tenant, ``slug`` unique globally -- both only among live rows), the
soft-delete name/slug re-use property, and the CHECK constraints.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EventSource as EventSourceORM

from ._event_source_helpers import (
    _settings_env,  # noqa: F401  (autouse fixture)
)

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


async def _add(**kwargs: object) -> EventSourceORM:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": _TENANT_A,
        "name": "am-prod",
        "slug": "am-prod",
        "kind": "alertmanager",
        "auth_strategy": "hmac-sha256",
        "secret_ref": None,
        "status": "active",
        "extras": {},
        "created_by_sub": "admin-1",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    row = EventSourceORM(**defaults)
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
    return row


@pytest.mark.asyncio
async def test_columns_round_trip() -> None:
    """A row persists and reads back every column, extras included."""
    row = await _add(extras={"rate_limit": 60, "body_cap": 1048576})
    async with get_sessionmaker()() as session:
        fetched = await session.get(EventSourceORM, row.id)
    assert fetched is not None
    assert fetched.name == "am-prod"
    assert fetched.slug == "am-prod"
    assert fetched.kind == "alertmanager"
    assert fetched.auth_strategy == "hmac-sha256"
    assert fetched.status == "active"
    assert fetched.extras == {"rate_limit": 60, "body_cap": 1048576}
    assert fetched.created_by_sub == "admin-1"
    assert fetched.deleted_at is None


@pytest.mark.asyncio
async def test_name_unique_per_tenant_collides() -> None:
    """Two live rows with the same (tenant_id, name) collide."""
    await _add(name="dup", slug="slug-1")
    with pytest.raises(IntegrityError):
        await _add(name="dup", slug="slug-2")


@pytest.mark.asyncio
async def test_same_name_different_tenant_ok() -> None:
    """The same name in a different tenant does not collide."""
    await _add(name="shared", slug="slug-a", tenant_id=_TENANT_A)
    # Different tenant, different slug -> both allowed.
    await _add(name="shared", slug="slug-b", tenant_id=_TENANT_B)


@pytest.mark.asyncio
async def test_slug_unique_globally_collides_across_tenants() -> None:
    """A slug is globally unique -- even across tenants it collides."""
    await _add(name="n1", slug="global-slug", tenant_id=_TENANT_A)
    with pytest.raises(IntegrityError):
        await _add(name="n2", slug="global-slug", tenant_id=_TENANT_B)


@pytest.mark.asyncio
async def test_soft_delete_frees_name_and_slug() -> None:
    """A soft-deleted tombstone frees both name and slug for re-use."""
    first = await _add(name="reuse", slug="reuse")
    async with get_sessionmaker()() as session:
        row = await session.get(EventSourceORM, first.id)
        assert row is not None
        row.deleted_at = datetime.now(UTC)
        await session.commit()
    # A live re-creation with the same name AND slug now succeeds.
    await _add(name="reuse", slug="reuse")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "bad_value"),
    [
        ("kind", "syslog"),
        ("auth_strategy", "mtls"),
        ("status", "archived"),
    ],
)
async def test_check_constraints_reject_unknown_values(column: str, bad_value: str) -> None:
    """The ck_event_source_* CHECK constraints reject out-of-set values."""
    with pytest.raises(IntegrityError):
        await _add(**{column: bad_value})
