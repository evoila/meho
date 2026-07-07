# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0052_retire_registered_stub_twin_vcf_logs_orphan``.

Initiative #2065 (G0.29 v0.19.0 closed-loop dogfood hardening), Task #2068.
The forward complement of migration ``0049`` (#2001 / PR #2015): it
retires the long-product ``vcf-logs`` ingest orphan for the case
``0049``'s per-op ``EXISTS`` twin probe could not reach -- a
short-product representation that is a **0-row class-registry stub at a
divergent version**.

Revision pin
------------

Every test drives the forward pass with ``command.upgrade(cfg, "0052")``
(this migration's own revision), NOT ``"head"``. Pinning keeps the
contract stable against future sibling migrations and avoids the
stamp-back replay footgun. The idempotency test replays via
``stamp("0051") -> upgrade("0052")``.

The load-bearing case
---------------------

:func:`test_retire_with_zero_row_registered_stub_twin_at_divergent_version`
seeds **only** the long orphan -- no ``endpoint_descriptor`` /
``operation_group`` short-twin rows at all -- mirroring a short product
that is registered purely via the v2 connector *class* registry. A naive
DB-``EXISTS``-twin implementation (``0049``'s shape, or any connector-
grain probe against these tables) matches zero rows and retires nothing,
so it MUST fail this test. This migration keys on the orphan's own
``impl_id`` and retires it.

Sync-test constraint: ``alembic.command.upgrade`` drives env.py's async
cookbook through ``asyncio.run``, so test functions stay sync and the
dispatch-probe test wraps its async probe in its own ``asyncio.run``
with an engine-cache reset on each side. Mirrors
:mod:`tests.test_migration_0049_retire_stale_vcf_logs_orphan_rows`.
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

#: This migration's own revision -- the forward-pass target (NOT ``head``).
_THIS_REVISION: Final[str] = "0052"
#: The immediately preceding head this migration revises.
_DOWN_REVISION: Final[str] = "0051"

#: The six long->short splits the migration retires, as
#: ``(long_product, impl_id, short_product)``. Mirrors ``_PRODUCT_SPLITS``
#: inside migration 0052 (which snapshots 0049 / 0038's mapping). The
#: ``impl_id`` here derives ``short_product`` via ``parse_connector_id``.
_SPLITS: Final[list[tuple[str, str, str]]] = [
    ("hetzner-robot", "hetzner-rest", "hetzner"),
    ("sddc-manager", "sddc-rest", "sddc"),
    ("vcf-automation", "vcfa-rest", "vcfa"),
    ("vcf-fleet", "fleet-rest", "fleet"),
    ("vcf-logs", "vrli-rest", "vrli"),
    ("vcf-operations", "vrops-rest", "vrops"),
]

#: The orphan's divergent version (``9.0.2``) vs the registered stub's
#: base version (``9.0``) -- the version mismatch that broke 0049's probe.
_ORPHAN_VERSION: Final[str] = "9.0.2"
_STUB_VERSION: Final[str] = "9.0"

#: Stable seed timestamp -- lets assertions tell "row untouched" apart.
_SEED_TS: Final[str] = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Same harness as
    :mod:`tests.test_migration_0049_retire_stale_vcf_logs_orphan_rows`:
    sync fixture (``alembic.command`` calls ``asyncio.run`` internally),
    per-test SQLite file under ``tmp_path``, settings + engine caches
    reset on both sides so the alembic env and the dispatch probes read
    *this* ``DATABASE_URL``.
    """
    db_path = tmp_path / "migration_0052.db"
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
    version: str = _ORPHAN_VERSION,
) -> UUID:
    """Insert one minimal ``endpoint_descriptor`` row at the migration base.

    Raw SQL (not the ORM) keeps the seed pinned to the schema the
    migration runs against. Columns with SQLite server defaults from
    migration 0005 (``tags``, ``parameter_schema``, ``safety_level``,
    ``requires_approval``, ``is_enabled``) are omitted; ``is_enabled``
    defaults to 1, which the ``count_known_ops`` probe relies on. UUID
    binds use ``.hex`` per ``docs/codebase/migrations.md``.
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
    version: str = _ORPHAN_VERSION,
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
                        :name, 'seeded for 0052 retire test', 'enabled', :ts, :ts
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


def test_retire_with_zero_row_registered_stub_twin_at_divergent_version(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The case ``0049`` missed: retire with NO DB-visible short twin.

    The short (``vrli``) representation is a v2 class-registry stub with
    **zero** ``endpoint_descriptor`` / ``operation_group`` rows, sitting
    at a divergent version (``9.0`` vs the orphan's ``9.0.2``). ``0049``'s
    per-op ``EXISTS`` probe -- and any connector-grain DB twin probe --
    matches nothing here and would retire nothing.

    A naive DB-``EXISTS``-twin implementation MUST fail this test: it
    would leave both orphan rows in place.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    # Seed ONLY the long orphan -- no short-product descriptor/group rows.
    orphan_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
        version=_ORPHAN_VERSION,
    )
    orphan_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        group_key="system",
        version=_ORPHAN_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert not _row_exists(sync_url, "endpoint_descriptor", orphan_descriptor), (
        "the orphan descriptor must be retired even with a 0-row registered "
        "stub twin at a divergent version -- keyed on its own impl_id, not a "
        "DB twin (the 0049 miss)"
    )
    assert not _row_exists(sync_url, "operation_group", orphan_group), (
        "the orphan group must be retired under the same impl_id predicate"
    )


def test_retire_with_full_twin_still_retires(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The case ``0049`` already handled: full per-op short twin present.

    A short twin carrying the same ``op_id`` / ``group_key`` is present.
    This migration still retires the long orphan (keyed on impl_id) and
    leaves the short twin untouched -- parity with ``0049`` on its own
    happy path.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    orphan_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
        version=_ORPHAN_VERSION,
    )
    twin_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vrli",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
        version=_STUB_VERSION,
    )
    orphan_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        group_key="system",
        version=_ORPHAN_VERSION,
    )
    twin_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="vrli",
        impl_id="vrli-rest",
        group_key="system",
        version=_STUB_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    for table, orphan_id, twin_id in (
        ("endpoint_descriptor", orphan_descriptor, twin_descriptor),
        ("operation_group", orphan_group, twin_group),
    ):
        assert not _row_exists(sync_url, table, orphan_id), (
            f"the long-product {table} orphan must be retired"
        )
        assert _row_exists(sync_url, table, twin_id), (
            f"the live short-product {table} twin must survive untouched"
        )
        assert _read_updated_at(sync_url, table, twin_id) == _SEED_TS, (
            f"the surviving {table} twin must not be touched"
        )


def test_all_six_splits_retire_without_a_twin(
    alembic_cfg: tuple[Config, str],
) -> None:
    """SQL-level: every long-product orphan retires with no short twin.

    One orphan per split, seeded with NO short-product rows (the
    registered-stub-twin shape). Post-migration every long-product count
    is 0 on both tables.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    for long_product, impl_id, _short_product in _SPLITS:
        _insert_descriptor_row(
            sync_url,
            tenant_id=None,
            product=long_product,
            impl_id=impl_id,
            op_id="GET:/api/v2/version",
            version=_ORPHAN_VERSION,
        )
        _insert_group_row(
            sync_url,
            tenant_id=None,
            product=long_product,
            impl_id=impl_id,
            group_key="system",
            version=_ORPHAN_VERSION,
        )

    command.upgrade(cfg, _THIS_REVISION)

    for long_product, _, _ in _SPLITS:
        for table in ("endpoint_descriptor", "operation_group"):
            assert _count_by_product(sync_url, table, long_product) == 0, (
                f"no {table} row may remain under {long_product!r} after retire"
            )


def test_foreign_impl_id_rows_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The retire follows ``parse_connector_id``, not a blanket product match.

    A ``product='vcf-logs'`` row whose ``impl_id`` first-hyphen-segment
    is not ``vrli`` derives some other product, so the dispatcher would
    not resolve it under ``vrli`` -- it is left alone (impl_id-family
    scoping), even absent any twin.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    foreign_impl = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="custom-rest",
        op_id="GET:/api/v2/version",
        version=_ORPHAN_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert _row_exists(sync_url, "endpoint_descriptor", foreign_impl), (
        "a vcf-logs row whose impl_id derives a non-split product must be "
        "preserved (impl_id-family scoping)"
    )


def test_tenant_scoped_rows_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``tenant_id IS NOT NULL`` rows are operator-owned and never deleted.

    Even a tenant-scoped ``vcf-logs`` / ``vrli-rest`` row -- one that
    would match the product + impl_id predicate on the NULL-tenant side
    -- is out of scope (#1699 scoping contract).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    tenant_id = uuid4()
    tenant_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=tenant_id,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
        version=_ORPHAN_VERSION,
    )
    tenant_group = _insert_group_row(
        sync_url,
        tenant_id=tenant_id,
        product="vcf-logs",
        impl_id="vrli-rest",
        group_key="system",
        version=_ORPHAN_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    for table, row_id in (
        ("endpoint_descriptor", tenant_descriptor),
        ("operation_group", tenant_group),
    ):
        assert _row_exists(sync_url, table, row_id), (
            f"tenant-scoped {table} row must never be deleted"
        )


def test_re_running_migration_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Replaying ``upgrade()`` on a cleaned DB deletes nothing further.

    Stamp-back replay pinned to this migration's own revision
    (``stamp("0051") -> upgrade("0052")``), never ``head`` -- so future
    schema migrations cannot leak into the replay.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    orphan = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
        version=_ORPHAN_VERSION,
    )
    # A tenant row that must survive both passes untouched.
    survivor_tenant = uuid4()
    survivor = _insert_descriptor_row(
        sync_url,
        tenant_id=survivor_tenant,
        product="vcf-logs",
        impl_id="vrli-rest",
        op_id="GET:/api/v2/version",
        version=_ORPHAN_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)
    assert not _row_exists(sync_url, "endpoint_descriptor", orphan)  # first pass landed
    assert _row_exists(sync_url, "endpoint_descriptor", survivor)

    command.stamp(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _THIS_REVISION)

    assert not _row_exists(sync_url, "endpoint_descriptor", orphan), (
        "the orphan stays gone on replay"
    )
    assert _row_exists(sync_url, "endpoint_descriptor", survivor), (
        "the tenant survivor must persist across an idempotent replay"
    )
    assert _read_updated_at(sync_url, "endpoint_descriptor", survivor) == _SEED_TS, (
        "a no-op replay must not disturb the surviving row"
    )


def test_live_short_representation_dispatches_post_migration(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Dispatch parity: the short spelling still resolves its ingested ops.

    Seeds the invisible long orphan plus short-spelling ``vrli`` op rows
    (standing in for a short representation that HAS ingested ops -- the
    strongest dispatch-parity check). After the migration the orphan is
    gone and the short product still resolves and counts its enabled ops.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    for op_id in ("GET:/api/v2/version", "GET:/api/v2/events"):
        _insert_descriptor_row(
            sync_url,
            tenant_id=None,
            product="vcf-logs",
            impl_id="vrli-rest",
            op_id=op_id,
            version=_STUB_VERSION,
        )
        _insert_descriptor_row(
            sync_url,
            tenant_id=None,
            product="vrli",
            impl_id="vrli-rest",
            op_id=op_id,
            version=_STUB_VERSION,
        )

    product, version, impl_id = parse_connector_id("vrli-rest-9.0")
    assert (product, version, impl_id) == ("vrli", "9.0", "vrli-rest")

    async def _probe() -> tuple[bool, int]:
        reset_engine_for_testing()
        try:
            probe_tenant = uuid4()
            exists = await connector_exists(
                tenant_id=probe_tenant,
                product=product,
                version=version,
                impl_id=impl_id,
            )
            known = await count_known_ops(
                tenant_id=probe_tenant,
                product=product,
                version=version,
                impl_id=impl_id,
            )
            return exists, known
        finally:
            await dispose_engine()

    command.upgrade(cfg, _THIS_REVISION)

    exists_after, known_after = asyncio.run(_probe())
    assert exists_after is True, "the live short representation must still resolve"
    assert known_after == 2, "both enabled ops on the short spelling must still count"
    assert _count_by_product(sync_url, "endpoint_descriptor", "vcf-logs") == 0, (
        "the long orphans are gone"
    )
