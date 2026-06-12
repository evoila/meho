# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0038_backfill_endpoint_descriptor_product_splits``.

Initiative #1691 (G0.25 v0.14.0 cycle-10 dogfood hardening), Task
#1701 (CI-3). The migration reconciles pre-v0.14.0 built-in rows
persisted under the long registry product spellings (``vcf-logs``
etc.) to the short dispatch-canonical spellings (``vrli`` etc.) that
PR #1677 (#1647) made the register-time path write -- the data half
of a fix that was otherwise forward-only.

Test matrix
-----------

* **Long rows -> reconciled.** Descriptor + group rows seeded under
  ``(vcf-logs, 9.0, vrli-rest)`` carry ``product='vrli'`` (and a
  bumped ``updated_at``) after ``upgrade head``.
* **All six splits, SQL-level counts.** One descriptor + one group
  per split; post-migration each long-product count is 0 and each
  short-product count is 1, on both tables.
* **Tenant-scoped rows -> preserved.** ``tenant_id IS NOT NULL`` rows
  are operator-owned (#1699 scoping contract) and never rewritten.
* **Short twin -> long row skipped, no crash.** When a post-upgrade
  re-ingest already created the short-spelling row on the same
  global natural key, the long row is left untouched instead of
  colliding with the partial unique indexes from migration ``0005``.
* **Foreign impl_id -> preserved.** A ``product='vcf-logs'`` row
  whose ``impl_id`` derives some other product is not rewritten (the
  predicate mirrors ``parse_connector_id``'s first-hyphen-segment
  rule, not a blanket product rename); unmapped products are
  untouched.
* **Idempotency.** Re-running the migration's ``upgrade()`` against
  an already-reconciled DB (stamp back + re-upgrade, the same replay
  shape :mod:`tests.test_migration_0011_backfill_when_to_use`
  established) changes nothing, including ``updated_at``.
* **Dispatch probes resolve post-migration.** The acceptance gate
  from #1701: ``connector_exists`` / ``count_known_ops`` keyed on the
  ``parse_connector_id``-derived triple miss the orphaned rows before
  the migration and resolve them after.

Sync-test constraint: ``alembic.command.upgrade`` drives env.py's
async cookbook through ``asyncio.run``, so test functions stay sync
and the dispatch-probe test wraps its async probe in its own
``asyncio.run`` with an engine-cache reset on each side.
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

#: The six long->short splits the migration reconciles, as
#: ``(registry_product, impl_id, dispatch_product)``. Mirrors
#: ``_VCF_PRODUCT_SPLITS`` in :mod:`tests.test_operations_register_ingested`
#: (the register-time fixture) and the snapshot inside migration 0038.
_SPLITS: Final[list[tuple[str, str, str]]] = [
    ("hetzner-robot", "hetzner-rest", "hetzner"),
    ("sddc-manager", "sddc-rest", "sddc"),
    ("vcf-automation", "vcfa-rest", "vcfa"),
    ("vcf-fleet", "fleet-rest", "fleet"),
    ("vcf-logs", "vrli-rest", "vrli"),
    ("vcf-operations", "vrops-rest", "vrops"),
]

_VERSION: Final[str] = "9.0"

#: Deliberately old seed timestamp -- lets assertions tell "migration
#: bumped updated_at" apart from "row untouched" without sleeping.
_SEED_TS: Final[str] = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Same harness as :mod:`tests.test_migration_0011_backfill_when_to_use`:
    sync fixture (``alembic.command`` calls ``asyncio.run`` internally),
    per-test SQLite file under ``tmp_path``, settings + engine caches
    reset on both sides so the alembic env and the dispatch probes read
    *this* ``DATABASE_URL``.
    """
    db_path = tmp_path / "migration_0038.db"
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
    """Insert one minimal ``endpoint_descriptor`` row at revision 0037.

    Raw SQL (not the ORM) keeps the seed pinned to the schema the
    migration actually runs against. Columns with SQLite server
    defaults from migration 0005 (``tags``, ``parameter_schema``,
    ``safety_level``, ``requires_approval``, ``is_enabled``) are
    omitted; ``is_enabled`` therefore defaults to 1, which the
    ``count_known_ops`` probe relies on. UUID binds use ``.hex`` per
    the convention in ``docs/codebase/migrations.md``.
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
    """Insert one minimal ``operation_group`` row at revision 0037."""
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
                        :name, 'seeded for 0038 backfill test', 'enabled', :ts, :ts
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


def _read_row(sync_url: str, table: str, row_id: UUID) -> tuple[str, str]:
    """Return ``(product, updated_at)`` for one row, as strings."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text(f"SELECT product, updated_at FROM {table} WHERE id = :id"),
                {"id": row_id.hex},
            ).one()
            return (str(row.product), str(row.updated_at))
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


def test_long_product_rows_reconciled_to_dispatch_spelling(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Built-in long-spelling rows carry the short product after upgrade.

    Both row surfaces move together -- the descriptor and its group
    are rewritten under the same predicate -- and ``updated_at`` is
    bumped so operator tooling driven off the column sees the change.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0037")

    descriptor_id = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )
    group_id = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        group_key="system",
    )

    command.upgrade(cfg, "head")

    for table, row_id in (
        ("endpoint_descriptor", descriptor_id),
        ("operation_group", group_id),
    ):
        product, updated_at = _read_row(sync_url, table, row_id)
        assert product == "vrli", f"{table} row must carry the dispatch product"
        assert updated_at != _SEED_TS, f"{table}.updated_at must be bumped by the rewrite"


def test_all_six_splits_reconcile_to_expected_counts(
    alembic_cfg: tuple[Config, str],
) -> None:
    """SQL-level validation: post-migration counts match the reconciled state.

    One descriptor + one group seeded per split under the long
    spelling; after the upgrade every long-product count is 0 and
    every short-product count is 1, on both tables (#1701 acceptance
    criterion).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0037")

    for long_product, impl_id, _ in _SPLITS:
        _insert_descriptor_row(
            sync_url,
            tenant_id=None,
            product=long_product,
            impl_id=impl_id,
            op_id="GET:/api/v2/version",
        )
        _insert_group_row(
            sync_url,
            tenant_id=None,
            product=long_product,
            impl_id=impl_id,
            group_key="system",
        )

    command.upgrade(cfg, "head")

    for long_product, _, short_product in _SPLITS:
        for table in ("endpoint_descriptor", "operation_group"):
            assert _count_by_product(sync_url, table, long_product) == 0, (
                f"no {table} row may remain under {long_product!r} after the backfill"
            )
            assert _count_by_product(sync_url, table, short_product) == 1, (
                f"exactly the seeded {table} row must surface under {short_product!r}"
            )


def test_tenant_scoped_rows_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``tenant_id IS NOT NULL`` rows are operator-owned and never rewritten.

    The #1699 scoping contract keeps the NULL and per-tenant
    namespaces as deliberately distinct shadow copies; operators
    re-ingest under their tenant to update theirs (#1701 out-of-scope
    note).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0037")

    tenant_id = uuid4()
    descriptor_id = _insert_descriptor_row(
        sync_url,
        tenant_id=tenant_id,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )
    group_id = _insert_group_row(
        sync_url,
        tenant_id=tenant_id,
        product="vcf-logs",
        impl_id="vrli-rest",
        group_key="system",
    )

    command.upgrade(cfg, "head")

    for table, row_id in (
        ("endpoint_descriptor", descriptor_id),
        ("operation_group", group_id),
    ):
        product, updated_at = _read_row(sync_url, table, row_id)
        assert product == "vcf-logs", f"tenant-scoped {table} row must keep its product"
        assert updated_at == _SEED_TS, f"tenant-scoped {table} row must not be touched"


def test_short_twin_collision_is_skipped_not_crashed(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A pre-existing short-spelling twin parks the long row instead of crashing.

    The re-ingested-after-upgrade scenario: rewriting the long row
    would collide with ``endpoint_descriptor_global_idx`` /
    ``operation_group_global_idx`` (migration 0005) on the short
    twin's natural key. The ``NOT EXISTS`` guard skips it so the helm
    pre-upgrade migration Job cannot fail with ``IntegrityError``; the
    skipped row stays exactly as orphaned as it already was.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0037")

    long_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )
    short_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vrli",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )
    long_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        group_key="system",
    )
    short_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="vrli",
        impl_id="vrli-rest",
        group_key="system",
    )

    # Must not raise -- the guard, not the unique index, decides.
    command.upgrade(cfg, "head")

    for table, long_id, short_id in (
        ("endpoint_descriptor", long_descriptor, short_descriptor),
        ("operation_group", long_group, short_group),
    ):
        long_product, long_updated = _read_row(sync_url, table, long_id)
        assert long_product == "vcf-logs", f"collided {table} row must be skipped"
        assert long_updated == _SEED_TS, f"collided {table} row must not be touched"
        short_product, short_updated = _read_row(sync_url, table, short_id)
        assert short_product == "vrli", f"re-ingested {table} twin must stay live"
        assert short_updated == _SEED_TS, f"re-ingested {table} twin must not be touched"


def test_foreign_impl_id_rows_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The rewrite follows ``parse_connector_id``, not a blanket product rename.

    A ``product='vcf-logs'`` row whose ``impl_id`` first-hyphen-segment
    is not ``vrli`` would still be unreachable under ``vrli`` after a
    rewrite, so the migration leaves it alone. Aligned connectors
    (``vmware`` / ``vmware-rest``) are not in the mapping and are
    untouched.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0037")

    foreign_impl = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="custom-rest",
        op_id="GET:/api/v2/version",
    )
    aligned = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vmware",
        impl_id="vmware-rest",
        op_id="GET:/api/vcenter/cluster",
    )

    command.upgrade(cfg, "head")

    product, updated_at = _read_row(sync_url, "endpoint_descriptor", foreign_impl)
    assert (product, updated_at) == ("vcf-logs", _SEED_TS), (
        "a long-product row whose impl_id derives another product must not be rewritten"
    )
    product, updated_at = _read_row(sync_url, "endpoint_descriptor", aligned)
    assert (product, updated_at) == ("vmware", _SEED_TS), (
        "aligned connectors are outside the mapping and must not move"
    )


def test_re_running_migration_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Replaying ``upgrade()`` on a reconciled DB changes nothing.

    Same stamp-back replay shape as the 0011 test: after the first
    pass the rows no longer match ``product = '<long>'``, so a second
    execution of the migration body is filter-shaped no-op --
    ``updated_at`` must not move either. Both passes pin the target
    to ``0038`` so future non-idempotent schema migrations cannot
    leak into the replay.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0037")

    descriptor_id = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
    )

    command.upgrade(cfg, "0038")
    first_product, first_updated = _read_row(sync_url, "endpoint_descriptor", descriptor_id)
    assert first_product == "vrli"  # sanity: first pass landed

    command.stamp(cfg, "0037")
    command.upgrade(cfg, "0038")

    second_product, second_updated = _read_row(sync_url, "endpoint_descriptor", descriptor_id)
    assert second_product == first_product
    assert second_updated == first_updated, (
        "second invocation must not bump updated_at -- the product predicate "
        "filters reconciled rows out before the UPDATE runs"
    )


def test_dispatch_probes_resolve_reconciled_connector(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``connector_exists`` / ``count_known_ops`` flip from miss to hit.

    The #1701 acceptance gate, end to end: the dispatch surface keys
    on the triple ``parse_connector_id`` derives from the
    connector_id. Before the migration the orphaned long rows are
    invisible to it (the CI-3 symptom); after the migration the
    probes resolve the connector and count its enabled ops.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0037")

    for op_id in ("GET:/api/v2/version", "GET:/api/v2/events"):
        _insert_descriptor_row(
            sync_url,
            tenant_id=None,
            product="vcf-logs",
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

    exists_before, known_before = asyncio.run(_probe())
    assert exists_before is False, "orphaned long rows must be invisible pre-migration"
    assert known_before == 0

    command.upgrade(cfg, "head")

    exists_after, known_after = asyncio.run(_probe())
    assert exists_after is True, "reconciled rows must resolve through the dispatch probe"
    assert known_after == 2, "both enabled ops must count under the short product"
