# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :func:`meho_backplane.tenancy.ensure.ensure_tenant`.

The SQLite half of the G0.8-T1 (#628) coverage matrix. The PG-real,
HTTP-surface half (clean-room first-write, concurrent first-write)
lives in :mod:`tests.integration.test_tenant_seed_pg`.

Coverage:

* **Empty table → row seeded.** Calling :func:`ensure_tenant` against
  an empty ``tenant`` table inserts exactly one row with the derived
  slug / name.
* **Idempotent.** Calling it N times for the same ``tenant_id``
  leaves exactly one row.
* **Pre-existing row untouched.** A ``tenant`` row that already has
  an operator-chosen ``slug`` / ``name`` (the v0.3 provisioning-API
  case) is not overwritten by a subsequent ``ensure_tenant`` — the
  ``ON CONFLICT DO NOTHING`` must never clobber.
* **SQLite dialect dispatch.** The SQLite ``insert`` branch is the
  one exercised here; the PG branch is exercised by the integration
  module. Both target ``tenant.id`` as the conflict key.

These run against the autouse ``sqlite+aiosqlite`` engine the
``_default_database_url`` conftest fixture pre-migrates to
``alembic upgrade head`` — the same path
:mod:`tests.test_db_documents` uses.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.settings import get_settings
from meho_backplane.tenancy import ensure_tenant


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    Mirrors :func:`tests.test_db_documents._required_settings_env` —
    the autouse ``_default_database_url`` fixture in
    :mod:`tests.conftest` only pins ``DATABASE_URL``; the Keycloak /
    Vault knobs come from each test file. The ``get_settings`` cache
    reset around the yield keeps a stale ``Settings`` from a previous
    test from leaking in.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _count(tenant_id: uuid.UUID) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(func.count()).select_from(Tenant).where(Tenant.id == tenant_id),
        )
        return int(result.scalar_one())


async def test_ensure_tenant_seeds_row_on_empty_table() -> None:
    """First call against an absent ``tenant_id`` inserts exactly one row."""
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session, session.begin():
        await ensure_tenant(tenant_id, session)

    async with sessionmaker() as session:
        row = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalars().one()
    assert row.id == tenant_id
    assert row.slug == f"tenant-{tenant_id.hex[:8]}"
    assert row.name == f"tenant-{tenant_id.hex[:8]}"


async def test_ensure_tenant_is_idempotent_across_repeated_calls() -> None:
    """Calling :func:`ensure_tenant` N times yields exactly one row."""
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()

    for _ in range(5):
        async with sessionmaker() as session, session.begin():
            await ensure_tenant(tenant_id, session)

    assert await _count(tenant_id) == 1


async def test_ensure_tenant_is_idempotent_under_concurrent_calls() -> None:
    """Concurrent first-writes for the same ``tenant_id`` → one row.

    The async-session contract is one connection per session, so each
    coroutine gets its own session; ``ON CONFLICT (id) DO NOTHING``
    is what keeps the racing inserts from raising or double-inserting.
    """
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()

    async def _one_call() -> None:
        async with sessionmaker() as session, session.begin():
            await ensure_tenant(tenant_id, session)

    await asyncio.gather(*(_one_call() for _ in range(8)))

    assert await _count(tenant_id) == 1


async def test_ensure_tenant_does_not_overwrite_existing_row() -> None:
    """A pre-existing operator-named row survives a later ``ensure_tenant``.

    Models the v0.3 provisioning-API case: a tenant row already has a
    human-chosen slug / name. ``ensure_tenant`` must no-op on the
    conflict, never clobber the operator's values back to the derived
    placeholder.
    """
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session, session.begin():
        session.add(
            Tenant(id=tenant_id, slug="rdc-internal", name="RDC Internal"),
        )

    async with sessionmaker() as session, session.begin():
        await ensure_tenant(tenant_id, session)

    async with sessionmaker() as session:
        row = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalars().one()
    assert row.slug == "rdc-internal"
    assert row.name == "RDC Internal"
