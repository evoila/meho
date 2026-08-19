# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0075_reap_epoch_versioned_connector_registrations``.

Initiative #3020 (G0.40 hardening wave 2), Task #2977. The migration
reaps ``endpoint_descriptor`` / ``operation_group`` rows whose
``version`` is a bare Unix-epoch integer (>= 9 digits) — the
non-idempotent ``fleet-rest-probe-<epoch>`` catalog debris a consumer
probe-then-ingest loop accumulated one-per-run.

Two properties distinguish it from the ``0049`` / ``0052`` twin-retire
migrations, and both are pinned here:

* **Tenant-scope-inclusive.** The epoch rows are consumer-side *tenant*
  ingests, so the cleanup spans every scope — a tenant-scoped epoch row
  IS reaped (:func:`test_tenant_scoped_epoch_rows_reaped`), the opposite
  of ``0049`` / ``0052`` which never touch tenant rows.
* **Keyed on the epoch-version shape, not a product/impl_id list.** A
  stable-version row survives regardless of impl_id
  (:func:`test_stable_version_rows_preserved`); a short integer version
  (< 9 digits) survives (:func:`test_short_integer_version_preserved`);
  ``endpoint_descriptor`` additionally guards on ``source_kind =
  'ingested'`` (:func:`test_typed_epoch_descriptor_preserved`).

Revision pin: every test drives the forward pass with
``command.upgrade(cfg, "0075")`` (this migration's own revision, NOT
``head``) so future sibling migrations cannot leak into the contract.
Sync-test constraint identical to
:mod:`tests.migrations.test_migration_0052_retire_registered_stub_twin_vcf_logs_orphan`:
``alembic.command`` drives env.py's async cookbook via ``asyncio.run``,
so the test functions stay sync.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

#: This migration's own revision -- the forward-pass target (NOT ``head``).
_THIS_REVISION: Final[str] = "0075"
#: The immediately preceding head this migration revises.
_DOWN_REVISION: Final[str] = "0074"

#: A representative Unix-epoch-seconds ``version`` (10 digits) -- the
#: shape the consumer probe-then-ingest loop stamped.
_EPOCH_VERSION: Final[str] = "1699999999"
#: A legitimate product-line version -- must always survive.
_STABLE_VERSION: Final[str] = "9.0"

#: Stable seed timestamp -- lets assertions tell "row untouched" apart.
_SEED_TS: Final[str] = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Same harness as
    :mod:`tests.migrations.test_migration_0052_retire_registered_stub_twin_vcf_logs_orphan`:
    sync fixture (``alembic.command`` calls ``asyncio.run`` internally),
    per-test SQLite file under ``tmp_path``, settings + engine caches
    reset on both sides so the alembic env reads *this* ``DATABASE_URL``.
    """
    db_path = tmp_path / "migration_0075.db"
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
    version: str,
    source_kind: str = "ingested",
) -> UUID:
    """Insert one minimal ``endpoint_descriptor`` row at the migration base.

    Raw SQL (not the ORM) keeps the seed pinned to the schema the
    migration runs against. UUID binds use ``.hex`` per
    ``docs/codebase/migrations.md``.
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
                        :source_kind, :ts, :ts
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
                    "source_kind": source_kind,
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
    version: str,
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
                        :name, 'seeded for 0075 reap test', 'enabled', :ts, :ts
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


def test_epoch_versioned_ingested_rows_reaped(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A built-in ``fleet-rest-probe-<epoch>`` descriptor + group are reaped.

    The signature #2977 defect: an ingested connector whose ``version``
    is a Unix epoch. Both row surfaces the dispatch / review layer reads
    are cleared.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    epoch_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id="fleet-rest-probe",
        op_id="GET:/api/probe",
        version=_EPOCH_VERSION,
    )
    epoch_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id="fleet-rest-probe",
        group_key="system",
        version=_EPOCH_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert not _row_exists(sync_url, "endpoint_descriptor", epoch_descriptor), (
        "the epoch-versioned descriptor must be reaped"
    )
    assert not _row_exists(sync_url, "operation_group", epoch_group), (
        "the epoch-versioned group must be reaped under the same predicate"
    )


def test_stable_version_rows_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A legitimate stable-version connector survives untouched.

    Same impl_id family as the epoch row, but a real product-line
    ``version`` (``9.0``) -- proves the reap keys on the epoch shape, not
    on impl_id or product.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    stable_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id="fleet-rest",
        op_id="GET:/api/inventory",
        version=_STABLE_VERSION,
    )
    stable_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id="fleet-rest",
        group_key="inventory",
        version=_STABLE_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    for table, row_id in (
        ("endpoint_descriptor", stable_descriptor),
        ("operation_group", stable_group),
    ):
        assert _row_exists(sync_url, table, row_id), f"the stable-version {table} row must survive"
        assert _read_updated_at(sync_url, table, row_id) == _SEED_TS, (
            f"the surviving {table} row must not be touched"
        )


def test_tenant_scoped_epoch_rows_reaped(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The #2977 sharpening: tenant-scoped epoch rows ARE reaped.

    Unlike ``0049`` / ``0052`` (which never delete a tenant row), the
    epoch debris is a consumer-side tenant ingest, so the cleanup is
    tenant-scope-inclusive.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    tenant_id = uuid4()
    tenant_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=tenant_id,
        product="fleet",
        impl_id="fleet-rest-probe",
        op_id="GET:/api/probe",
        version=_EPOCH_VERSION,
    )
    tenant_group = _insert_group_row(
        sync_url,
        tenant_id=tenant_id,
        product="fleet",
        impl_id="fleet-rest-probe",
        group_key="system",
        version=_EPOCH_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert not _row_exists(sync_url, "endpoint_descriptor", tenant_descriptor), (
        "a tenant-scoped epoch descriptor must be reaped (tenant-inclusive)"
    )
    assert not _row_exists(sync_url, "operation_group", tenant_group), (
        "a tenant-scoped epoch group must be reaped (tenant-inclusive)"
    )


def test_typed_epoch_descriptor_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``source_kind`` guard: a typed descriptor is never reaped.

    Typed / composite connectors are hand-coded and can never carry an
    epoch version, but the ``source_kind = 'ingested'`` guard makes the
    ``endpoint_descriptor`` blast radius provably ingest-only even in the
    impossible case a typed row's version were all-digits.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    typed_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id="fleet-rest-probe",
        op_id="GET:/api/probe",
        version=_EPOCH_VERSION,
        source_kind="typed",
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert _row_exists(sync_url, "endpoint_descriptor", typed_descriptor), (
        "a typed descriptor must survive the ingested-only reap guard"
    )


def test_short_integer_version_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A short integer version (< 9 digits) is not an epoch and survives.

    A calendar-year-ish bare integer (``2026``) is well below the
    9-digit epoch floor, so it is left alone -- the threshold does not
    over-reach onto plausible integer versions.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    short_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="widget",
        impl_id="widget-rest",
        op_id="GET:/api/x",
        version="2026",
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert _row_exists(sync_url, "endpoint_descriptor", short_descriptor), (
        "a sub-9-digit integer version is not an epoch and must survive"
    )


def test_re_running_migration_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Replaying ``upgrade()`` on a cleaned DB deletes nothing further.

    Stamp-back replay pinned to this migration's own revision
    (``stamp("0074") -> upgrade("0075")``), never ``head``.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    epoch = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id="fleet-rest-probe",
        op_id="GET:/api/probe",
        version=_EPOCH_VERSION,
    )
    survivor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id="fleet-rest",
        op_id="GET:/api/inventory",
        version=_STABLE_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)
    assert not _row_exists(sync_url, "endpoint_descriptor", epoch)
    assert _row_exists(sync_url, "endpoint_descriptor", survivor)

    command.stamp(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _THIS_REVISION)

    assert not _row_exists(sync_url, "endpoint_descriptor", epoch), (
        "the epoch row stays gone on replay"
    )
    assert _row_exists(sync_url, "endpoint_descriptor", survivor), (
        "the stable survivor must persist across an idempotent replay"
    )
    assert _read_updated_at(sync_url, "endpoint_descriptor", survivor) == _SEED_TS, (
        "a no-op replay must not disturb the surviving row"
    )


def test_downgrade_is_a_clean_noop(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade()`` runs without error and touches nothing.

    The reap is one-directional (the debris carried no operator value),
    so the downgrade is a documented no-op -- a surviving row is
    unchanged across the up/down cycle.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    survivor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id="fleet-rest",
        op_id="GET:/api/inventory",
        version=_STABLE_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)
    command.downgrade(cfg, _DOWN_REVISION)

    assert _row_exists(sync_url, "endpoint_descriptor", survivor), (
        "the no-op downgrade must leave the surviving row in place"
    )
    assert _read_updated_at(sync_url, "endpoint_descriptor", survivor) == _SEED_TS, (
        "the no-op downgrade must not disturb the surviving row"
    )
