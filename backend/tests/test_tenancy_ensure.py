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
* **Slug collision regression.** Two distinct ``tenant_id`` UUIDs
  sharing an 8-hex prefix both seed without raising. Guards against
  the truncated-slug regression: a prefix-derived slug would break the
  slug↔id bijection, letting two distinct tenants collide on the
  ``tenant.slug`` unique index and raise an ``IntegrityError`` on the
  second insert. The full-UUID slug keeps the slug exactly as unique
  as the ``id``.
* **Pre-existing row untouched.** A ``tenant`` row that already has
  an operator-chosen ``slug`` / ``name`` (the v0.3 provisioning-API
  case) is not overwritten by a subsequent ``ensure_tenant`` — the
  ``ON CONFLICT DO NOTHING`` must never clobber.
* **SQLite dialect dispatch.** The SQLite ``insert`` branch is the
  one exercised here; the PG branch is exercised by the integration
  module. Both arbitrate ``ON CONFLICT DO NOTHING`` against every
  unique index (no named ``index_elements``).

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
    assert row.slug == f"tenant-{tenant_id}"
    assert row.name == f"tenant-{tenant_id}"


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
    coroutine gets its own session; ``ON CONFLICT DO NOTHING``
    is what keeps the racing inserts from raising or double-inserting.

    Note: SQLite serialises writes, so this case never exercised the
    PostgreSQL concurrent-race (#983) that the named-``id`` arbiter
    could not survive; the PG-real reproduction lives in
    :mod:`tests.integration.test_tenant_seed_pg`.
    """
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()

    async def _one_call() -> None:
        async with sessionmaker() as session, session.begin():
            await ensure_tenant(tenant_id, session)

    await asyncio.gather(*(_one_call() for _ in range(8)))

    assert await _count(tenant_id) == 1


async def test_ensure_tenant_seeds_two_uuids_sharing_an_8hex_prefix() -> None:
    """Two UUIDs with the same first 8 hex chars both seed cleanly.

    Regression for the truncated-slug bug: the derived slug must be
    bijective with the ``id`` primary key. A ``tenant-<first-8-hex>``
    slug collides for two distinct UUIDs sharing that prefix and raises
    an ``IntegrityError`` on the ``tenant.slug`` unique index for the
    second insert; deriving from the full UUID keeps the slug exactly
    as unique as the ``id`` so the two inserts stay independent.
    """
    shared_prefix = uuid.uuid4().hex[:8]
    tenant_a = uuid.UUID(shared_prefix + uuid.uuid4().hex[8:])
    tenant_b = uuid.UUID(shared_prefix + uuid.uuid4().hex[8:])
    assert tenant_a != tenant_b
    assert tenant_a.hex[:8] == tenant_b.hex[:8]

    sessionmaker = get_sessionmaker()
    for tenant_id in (tenant_a, tenant_b):
        # No IntegrityError on the second insert is the assertion.
        async with sessionmaker() as session, session.begin():
            await ensure_tenant(tenant_id, session)

    assert await _count(tenant_a) == 1
    assert await _count(tenant_b) == 1

    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(Tenant).where(Tenant.id.in_([tenant_a, tenant_b])),
                )
            )
            .scalars()
            .all()
        )
    assert {r.slug for r in rows} == {f"tenant-{tenant_a}", f"tenant-{tenant_b}"}


async def test_ensure_tenant_does_not_overwrite_existing_row() -> None:
    """A pre-existing operator-named row survives a later ``ensure_tenant``.

    Models the v0.3 provisioning-API case: a tenant row already has a
    human-chosen slug / name. ``ensure_tenant`` must no-op on the
    conflict, never clobber the operator's values back to the derived
    placeholder.

    A throwaway slug (not ``rdc-internal``) keeps this test independent
    of migration ``0018``'s seeded row in the per-worker schema template
    (:func:`tests.conftest._schema_template_db`); the contract under
    test -- that an operator-named row survives -- is the same
    regardless of which slug carries the human label.
    """
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session, session.begin():
        session.add(
            Tenant(id=tenant_id, slug="operator-named", name="Operator Named"),
        )

    async with sessionmaker() as session, session.begin():
        await ensure_tenant(tenant_id, session)

    async with sessionmaker() as session:
        row = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalars().one()
    assert row.slug == "operator-named"
    assert row.name == "Operator Named"
