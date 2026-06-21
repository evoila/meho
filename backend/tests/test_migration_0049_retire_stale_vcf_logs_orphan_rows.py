# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0049_retire_stale_vcf_logs_orphan_rows``.

Initiative #1998 (G0.28 closed-loop dogfood hardening), Task #2001.
The migration DELETEs the built-in ``tenant_id IS NULL`` long-product
ingest orphans (``vcf-logs`` etc.) that migration ``0038``'s
collision-guard deliberately skipped -- the operation-row complement
of that backfill and of the ``targets.product`` reconciliation (#1814,
migration ``0047``). Those orphans are non-dispatchable and unreachable
by ``meho.connector.delete`` (#1910).

Test matrix
-----------

* **Orphan with live twin -> retired.** A long ``(vcf-logs, 9.0,
  vrli-rest)`` descriptor + group whose short ``(vrli, …)`` twin
  already exists is DELETEd; the short twin survives untouched.
* **All six splits, SQL-level counts.** One orphan + its short twin per
  split; post-migration every long-product count is 0 and every
  short-product count is 1, on both tables.
* **Tenant-scoped rows -> preserved.** ``tenant_id IS NOT NULL`` rows
  are operator-owned (#1699 scoping contract) and never deleted, even
  with a short twin present.
* **Twin-less long row -> preserved.** A long-product row with NO short
  twin is the only representation of that operation; the EXISTS guard
  leaves it alone (``0038`` rewrites it on the write side).
* **Foreign impl_id -> preserved.** A ``product='vcf-logs'`` row whose
  ``impl_id`` derives some other product is not retired (the predicate
  mirrors ``parse_connector_id``'s first-hyphen-segment rule).
* **Unmapped product -> preserved.** Aligned connectors
  (``vmware`` / ``vmware-rest``) are outside the mapping and untouched.
* **Idempotency.** Re-running ``upgrade()`` against an already-cleaned
  DB (stamp back + re-upgrade) deletes nothing further and does not
  disturb the surviving twins.
* **Dispatch parity post-migration.** The live short twin still
  resolves through ``connector_exists`` / ``count_known_ops`` after the
  orphan is gone -- the cleanup removes only the invisible long rows.

Sync-test constraint: ``alembic.command.upgrade`` drives env.py's
async cookbook through ``asyncio.run``, so test functions stay sync and
the dispatch-probe test wraps its async probe in its own
``asyncio.run`` with an engine-cache reset on each side. Mirrors
:mod:`tests.test_migration_0038_backfill_product_splits`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from meho_backplane.db.engine import dispose_engine, reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.operations._lookup import (
    connector_exists,
    count_known_ops,
    parse_connector_id,
)
from meho_backplane.settings import get_settings

#: The six long->short splits the migration retires, as
#: ``(long_product, impl_id, short_product)``. Mirrors ``_PRODUCT_SPLITS``
#: inside migration 0049 (which snapshots migration 0038's mapping).
_SPLITS: Final[list[tuple[str, str, str]]] = [
    ("hetzner-robot", "hetzner-rest", "hetzner"),
    ("sddc-manager", "sddc-rest", "sddc"),
    ("vcf-automation", "vcfa-rest", "vcfa"),
    ("vcf-fleet", "fleet-rest", "fleet"),
    ("vcf-logs", "vrli-rest", "vrli"),
    ("vcf-operations", "vrops-rest", "vrops"),
]

_VERSION: Final[str] = "9.0"

#: Stable seed timestamp -- lets assertions tell "row untouched" apart.
_SEED_TS: Final[str] = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Same harness as :mod:`tests.test_migration_0038_backfill_product_splits`:
    sync fixture (``alembic.command`` calls ``asyncio.run`` internally),
    per-test SQLite file under ``tmp_path``, settings + engine caches
    reset on both sides so the alembic env and the dispatch probes read
    *this* ``DATABASE_URL``.
    """
    db_path = tmp_path / "migration_0049.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, sync_url
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _insert_descriptor_row(
    sync_url: str,
    *,
    tenant_id: UUID | None,
    product: str,
    impl_id: str,
    op_id: str,
    version: str = _VERSION,
) -> UUID:
    """Insert one minimal ``endpoint_descriptor`` row at the migration base.

    Raw SQL (not the ORM) keeps the seed pinned to the schema the
    migration actually runs against. Columns with SQLite server defaults
    from migration 0005 (``tags``, ``parameter_schema``, ``safety_level``,
    ``requires_approval``, ``is_enabled``) are omitted; ``is_enabled``
    therefore defaults to 1, which the ``count_known_ops`` probe relies
    on. UUID binds use ``.hex`` per ``docs/codebase/migrations.md``.
    """
    row_id = uuid4()
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO endpoint_descriptor (
                        id, tenant_id, product, version, impl_id, op_id,
                        source_kind, created_at, updated_at
                    ) VALUES (
                        :id, :tenant_id, :product, :version, :impl_id, :op_id,
                        'ingested', :ts, :ts
                    )
                    """,
                ),
                {
                    "id": row_id.hex,
                    "tenant_id": tenant_id.hex if tenant_id is not None else None,
                    "product": product,
                    "version": version,
                    "impl_id": impl_id,
                    "op_id": op_id,
                    "ts": _SEED_TS,
                },
            )
    finally:
        sync_eng.dispose()
    return row_id


def _insert_group_row(
    sync_url: str,
    *,
    tenant_id: UUID | None,
    product: str,
    impl_id: str,
    group_key: str,
    version: str = _VERSION,
) -> UUID:
    """Insert one minimal ``operation_group`` row at the migration base."""
    row_id = uuid4()
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO operation_group (
                        id, tenant_id, product, version, impl_id, group_key,
                        name, when_to_use, review_status, created_at, updated_at
                    ) VALUES (
                        :id, :tenant_id, :product, :version, :impl_id, :group_key,
                        :name, 'seeded for 0049 retire test', 'enabled', :ts, :ts
                    )
                    """,
                ),
                {
                    "id": row_id.hex,
                    "tenant_id": tenant_id.hex if tenant_id is not None else None,
                    "product": product,
                    "version": version,
                    "impl_id": impl_id,
                    "group_key": group_key,
                    "name": group_key.title(),
                    "ts": _SEED_TS,
                },
            )
    finally:
        sync_eng.dispose()
    return row_id


def _row_exists(sync_url: str, table: str, row_id: UUID) -> bool:
    """Return whether a row with ``row_id`` is still present in ``table``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE id = :id"),
                {"id": row_id.hex},
            ).scalar_one()
            return int(count) == 1
    finally:
        sync_eng.dispose()


def _read_updated_at(sync_url: str, table: str, row_id: UUID) -> str:
    """Return ``updated_at`` for one row, as a string."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text(f"SELECT updated_at FROM {table} WHERE id = :id"),
                {"id": row_id.hex},
            ).one()
            return str(row.updated_at)
    finally:
        sync_eng.dispose()


def _count_by_product(sync_url: str, table: str, product: str) -> int:
    """Return ``COUNT(*)`` for one product on one table."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE product = :product"),
                {"product": product},
            ).scalar_one()
            return int(count)
    finally:
        sync_eng.dispose()


def test_orphan_with_live_twin_is_retired(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The long orphan is deleted; its live short twin survives untouched.

    Both row surfaces move together -- the orphaned descriptor and its
    group are retired under the same predicate -- while the short twins
    (a separate group_id lineage) are left as the sole representation.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048")

    orphan_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )
    twin_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vrli",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )
    orphan_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        group_key="system",
    )
    twin_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="vrli",
        impl_id="vrli-rest",
        group_key="system",
    )

    command.upgrade(cfg, "head")

    for table, orphan_id, twin_id in (
        ("endpoint_descriptor", orphan_descriptor, twin_descriptor),
        ("operation_group", orphan_group, twin_group),
    ):
        assert not _row_exists(sync_url, table, orphan_id), (
            f"the stale long-product {table} orphan must be deleted"
        )
        assert _row_exists(sync_url, table, twin_id), (
            f"the live short-product {table} twin must survive"
        )
        assert _read_updated_at(sync_url, table, twin_id) == _SEED_TS, (
            f"the surviving {table} twin must not be touched"
        )


def test_all_six_splits_retire_to_expected_counts(
    alembic_cfg: tuple[Config, str],
) -> None:
    """SQL-level validation: post-migration only the short twins remain.

    One orphan + its short twin seeded per split; after the upgrade
    every long-product count is 0 and every short-product count is 1,
    on both tables.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048")

    for long_product, impl_id, short_product in _SPLITS:
        for product in (long_product, short_product):
            _insert_descriptor_row(
                sync_url,
                tenant_id=None,
                product=product,
                impl_id=impl_id,
                op_id="GET:/api/v2/version",
            )
            _insert_group_row(
                sync_url,
                tenant_id=None,
                product=product,
                impl_id=impl_id,
                group_key="system",
            )

    command.upgrade(cfg, "head")

    for long_product, _, short_product in _SPLITS:
        for table in ("endpoint_descriptor", "operation_group"):
            assert _count_by_product(sync_url, table, long_product) == 0, (
                f"no {table} row may remain under {long_product!r} after retire"
            )
            assert _count_by_product(sync_url, table, short_product) == 1, (
                f"the live {table} short twin under {short_product!r} must remain"
            )


def test_tenant_scoped_rows_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``tenant_id IS NOT NULL`` rows are operator-owned and never deleted.

    Even with a global short twin present, a tenant-scoped long row is
    out of scope (#1699 scoping contract -- distinct shadow copies).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048")

    tenant_id = uuid4()
    tenant_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=tenant_id,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )
    tenant_group = _insert_group_row(
        sync_url,
        tenant_id=tenant_id,
        product="vcf-logs",
        impl_id="vrli-rest",
        group_key="system",
    )
    # A global short twin exists, so only the NULL-tenant boundary keeps
    # the tenant rows alive.
    _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vrli",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )
    _insert_group_row(
        sync_url,
        tenant_id=None,
        product="vrli",
        impl_id="vrli-rest",
        group_key="system",
    )

    command.upgrade(cfg, "head")

    for table, row_id in (
        ("endpoint_descriptor", tenant_descriptor),
        ("operation_group", tenant_group),
    ):
        assert _row_exists(sync_url, table, row_id), (
            f"tenant-scoped {table} row must never be deleted"
        )


def test_twin_less_long_row_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A long-product row with NO short twin is the sole representation.

    The EXISTS guard retires a long orphan only when a live short twin
    is present. A twin-less long row is left for ``0038``'s rewrite (the
    write-side path); deleting it would lose the only copy of that
    operation.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048")

    lone_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )
    lone_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        group_key="system",
    )

    command.upgrade(cfg, "head")

    for table, row_id in (
        ("endpoint_descriptor", lone_descriptor),
        ("operation_group", lone_group),
    ):
        assert _row_exists(sync_url, table, row_id), (
            f"a twin-less long {table} row must be preserved (no live short twin)"
        )


def test_foreign_impl_id_rows_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The retire follows ``parse_connector_id``, not a blanket product match.

    A ``product='vcf-logs'`` row whose ``impl_id`` first-hyphen-segment
    is not ``vrli`` is not what the dispatcher resolves under ``vrli``,
    so the migration leaves it alone even if a ``vrli`` twin exists.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048")

    foreign_impl = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="custom-rest",
        op_id="GET:/api/v2/version",
    )
    # A vrli twin on a *different* impl_id must not make the foreign row
    # eligible -- the twin probe correlates impl_id.
    _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vrli",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )

    command.upgrade(cfg, "head")

    assert _row_exists(sync_url, "endpoint_descriptor", foreign_impl), (
        "a long-product row whose impl_id derives another product must be preserved"
    )


def test_unmapped_product_rows_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Aligned connectors outside the split mapping are never touched."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048")

    aligned = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vmware",
        impl_id="vmware-rest",
        op_id="GET:/api/vcenter/cluster",
    )

    command.upgrade(cfg, "head")

    assert _row_exists(sync_url, "endpoint_descriptor", aligned), (
        "aligned connectors are outside the mapping and must not be deleted"
    )


def test_re_running_migration_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Replaying ``upgrade()`` on a cleaned DB deletes nothing further.

    Same stamp-back replay shape as the 0038 test: after the first pass
    the orphan is gone, so a second execution of the migration body is a
    filter-shaped no-op -- the surviving short twin must stay put. Both
    passes pin the target to ``0049`` so future schema migrations cannot
    leak into the replay.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048")

    orphan = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )
    twin = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vrli",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )

    command.upgrade(cfg, "0049")
    assert not _row_exists(sync_url, "endpoint_descriptor", orphan)  # first pass landed
    assert _row_exists(sync_url, "endpoint_descriptor", twin)

    command.stamp(cfg, "0048")
    command.upgrade(cfg, "0049")

    assert not _row_exists(sync_url, "endpoint_descriptor", orphan), (
        "the orphan stays gone on replay"
    )
    assert _row_exists(sync_url, "endpoint_descriptor", twin), (
        "the live short twin must survive an idempotent replay"
    )
    assert _read_updated_at(sync_url, "endpoint_descriptor", twin) == _SEED_TS, (
        "a no-op replay must not disturb the surviving twin"
    )


def test_live_short_twin_still_dispatches_post_migration(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The cleanup removes only invisible long rows; the short twin stays live.

    The dispatch surface keys on the triple ``parse_connector_id``
    derives. The long orphan was already invisible to it (the #1910
    symptom); after the migration the short twin still resolves and
    counts its enabled ops -- dispatch parity is preserved.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048")

    for op_id in ("GET:/api/v2/version", "GET:/api/v2/events"):
        # The invisible long orphan...
        _insert_descriptor_row(
            sync_url,
            tenant_id=None,
            product="vcf-logs",
            impl_id="vrli-rest",
            op_id=op_id,
        )
        # ...and its live short twin (what dispatch actually resolves).
        _insert_descriptor_row(
            sync_url,
            tenant_id=None,
            product="vrli",
            impl_id="vrli-rest",
            op_id=op_id,
        )

    product, version, impl_id = parse_connector_id("vrli-rest-9.0")
    assert (product, version, impl_id) == ("vrli", "9.0", "vrli-rest")

    async def _probe() -> tuple[bool, int]:
        # Fresh engine on this event loop; disposed before the loop
        # closes so no aiosqlite connection outlives its loop.
        reset_engine_for_testing()
        try:
            exists = await connector_exists(
                tenant_id=uuid4(),
                product=product,
                version=version,
                impl_id=impl_id,
            )
            known = await count_known_ops(
                product=product,
                version=version,
                impl_id=impl_id,
            )
            return exists, known
        finally:
            await dispose_engine()

    command.upgrade(cfg, "head")

    exists_after, known_after = asyncio.run(_probe())
    assert exists_after is True, "the live short twin must still resolve post-cleanup"
    assert known_after == 2, "both enabled ops on the short twin must still count"
    # And the long orphans are gone.
    assert _count_by_product(sync_url, "endpoint_descriptor", "vcf-logs") == 0
