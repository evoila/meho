# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0088_reap_probe_epoch_impl_id_registrations``.

Initiative #3020 (G0.40 hardening), Task #3061 — the #2977 follow-up.
Sibling of ``0075``: the migration reaps ``endpoint_descriptor`` /
``operation_group`` rows whose ``impl_id`` ends in ``-probe-<epoch>`` (a
``-probe-`` segment then a bare Unix-epoch integer, >= 9 digits) — the
``fleet-rest-probe-<epoch>`` catalog debris that carries a *legitimate*
``version="9.0"``, so ``0075``'s version-keyed predicate never touched it.

Three properties distinguish it, and all are pinned here:

* **Keyed on the impl_id tail, not the version.** The reaped rows carry a
  real product-line ``version`` (``9.0``); the epoch is in the ``impl_id``
  (:func:`test_probe_epoch_impl_id_rows_reaped`). A stable ``impl_id``
  under the same version survives (:func:`test_stable_impl_id_rows_preserved`).
* **The ``-probe-`` anchor discriminates.** A bare ``-probe`` impl_id with
  no epoch tail — the "one probe registration per impl" pattern #2977 asked
  for — survives (:func:`test_bare_probe_impl_id_preserved`); a sub-9-digit
  tail is not an epoch and survives
  (:func:`test_short_probe_tail_preserved`).
* **Tenant-scope-inclusive + ``source_kind`` guarded.** A tenant-scoped
  epoch row IS reaped (:func:`test_tenant_scoped_probe_epoch_rows_reaped`);
  ``endpoint_descriptor`` guards on ``source_kind = 'ingested'`` so a typed
  row survives (:func:`test_typed_probe_epoch_descriptor_preserved`).

Revision pin: every test drives the forward pass with
``command.upgrade(cfg, "0088")`` (this migration's own revision, NOT
``head``) so future sibling migrations cannot leak into the contract.
Sync-test constraint identical to
:mod:`tests.migrations.test_migration_0075_reap_epoch_versioned_connector_registrations`:
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
_THIS_REVISION: Final[str] = "0088"
#: The immediately preceding head this migration revises.
_DOWN_REVISION: Final[str] = "0087"

#: A representative probe-epoch ``impl_id`` -- the exact shape the lab
#: still carried post-``0075`` (``fleet-rest-probe-<epoch>``, epoch in the
#: impl_id tail, a *legitimate* version alongside).
_PROBE_EPOCH_IMPL_ID: Final[str] = "fleet-rest-probe-1784123249"
#: The stable, dispatch-canonical fleet impl_id -- must always survive.
_STABLE_IMPL_ID: Final[str] = "fleet-rest"
#: The stable "one probe registration per impl" impl_id (no epoch tail) --
#: the #2977 ask (c) pattern; must survive the ``-probe-<epoch>`` reap.
_BARE_PROBE_IMPL_ID: Final[str] = "fleet-rest-probe"
#: A real product-line version the reaped rows legitimately carry.
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
    :mod:`tests.migrations.test_migration_0075_reap_epoch_versioned_connector_registrations`:
    sync fixture (``alembic.command`` calls ``asyncio.run`` internally),
    per-test SQLite file under ``tmp_path``, settings + engine caches
    reset on both sides so the alembic env reads *this* ``DATABASE_URL``.
    """
    db_path = tmp_path / "migration_0088.db"
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
                        :name, 'seeded for 0088 reap test', 'enabled', :ts, :ts
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


def test_probe_epoch_impl_id_rows_reaped(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A ``fleet-rest-probe-<epoch>`` descriptor + group are reaped.

    The signature #3061 defect: an ingested connector whose ``impl_id``
    ends in a per-run epoch while the ``version`` is a *legitimate* ``9.0``
    (which is why ``0075``'s version-keyed reap missed it). Both row
    surfaces the dispatch / review layer reads are cleared.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    epoch_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id=_PROBE_EPOCH_IMPL_ID,
        op_id="GET:/api/probe",
        version=_STABLE_VERSION,
    )
    epoch_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id=_PROBE_EPOCH_IMPL_ID,
        group_key="system",
        version=_STABLE_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert not _row_exists(sync_url, "endpoint_descriptor", epoch_descriptor), (
        "the probe-epoch-impl_id descriptor must be reaped"
    )
    assert not _row_exists(sync_url, "operation_group", epoch_group), (
        "the probe-epoch-impl_id group must be reaped under the same predicate"
    )


def test_stable_impl_id_rows_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The genuine ``fleet-rest`` connector survives untouched.

    Same product + version as the epoch row, but a stable dispatch-canonical
    ``impl_id`` -- proves the reap keys on the ``-probe-<epoch>`` tail, not
    on product or version.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    stable_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id=_STABLE_IMPL_ID,
        op_id="GET:/api/inventory",
        version=_STABLE_VERSION,
    )
    stable_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id=_STABLE_IMPL_ID,
        group_key="inventory",
        version=_STABLE_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    for table, row_id in (
        ("endpoint_descriptor", stable_descriptor),
        ("operation_group", stable_group),
    ):
        assert _row_exists(sync_url, table, row_id), f"the stable-impl_id {table} row must survive"
        assert _read_updated_at(sync_url, table, row_id) == _SEED_TS, (
            f"the surviving {table} row must not be touched"
        )


def test_bare_probe_impl_id_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A bare ``-probe`` impl_id (no epoch tail) survives.

    ``fleet-rest-probe`` is the stable "one probe registration per impl"
    pattern #2977 ask (c) proposed -- it ends in ``-probe`` with no
    ``-<epoch>`` tail, so the ``-probe-[0-9]{9,}$`` predicate must not
    reach it. This is the discriminator that separates the accreting shape
    from the legitimate probe impl.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    bare_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id=_BARE_PROBE_IMPL_ID,
        op_id="GET:/api/probe",
        version=_STABLE_VERSION,
    )
    bare_group = _insert_group_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id=_BARE_PROBE_IMPL_ID,
        group_key="system",
        version=_STABLE_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert _row_exists(sync_url, "endpoint_descriptor", bare_descriptor), (
        "a bare -probe impl_id (no epoch tail) must survive"
    )
    assert _row_exists(sync_url, "operation_group", bare_group), (
        "a bare -probe group (no epoch tail) must survive"
    )


def test_short_probe_tail_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A sub-9-digit ``-probe-`` tail is not an epoch and survives.

    ``fleet-rest-probe-12345678`` (8 trailing digits) is below the 9-digit
    epoch floor, so the predicate leaves it alone -- the threshold does not
    over-reach onto a short numeric probe suffix.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    short_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id="fleet-rest-probe-12345678",
        op_id="GET:/api/probe",
        version=_STABLE_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert _row_exists(sync_url, "endpoint_descriptor", short_descriptor), (
        "a sub-9-digit -probe- tail is not an epoch and must survive"
    )


def test_probe_epoch_not_at_tail_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """An epoch that is not the impl_id tail is not the accretion shape.

    ``fleet-rest-probe-123456789-extra`` carries a 9-digit run after
    ``-probe-`` but the epoch does not reach the end of the string, so the
    ``-probe-[0-9]{9,}$`` predicate must not match it. Pins that the SQLite
    branch (``NOT GLOB '*-probe-*[^0-9]*'``) agrees with the PG ``$`` anchor
    and does not over-reap -- the safe direction for a destructive DELETE.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    non_tail_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id="fleet-rest-probe-123456789-extra",
        op_id="GET:/api/probe",
        version=_STABLE_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert _row_exists(sync_url, "endpoint_descriptor", non_tail_descriptor), (
        "an epoch not at the impl_id tail is not the accretion shape and must survive"
    )


def test_tenant_scoped_probe_epoch_rows_reaped(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Tenant-scoped probe-epoch rows ARE reaped (tenant-inclusive).

    Unlike ``0049`` / ``0052`` (which never delete a tenant row), the
    epoch debris is a consumer-side tenant ingest, so the cleanup spans
    every scope -- same posture as ``0075``.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    tenant_id = uuid4()
    tenant_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=tenant_id,
        product="fleet",
        impl_id=_PROBE_EPOCH_IMPL_ID,
        op_id="GET:/api/probe",
        version=_STABLE_VERSION,
    )
    tenant_group = _insert_group_row(
        sync_url,
        tenant_id=tenant_id,
        product="fleet",
        impl_id=_PROBE_EPOCH_IMPL_ID,
        group_key="system",
        version=_STABLE_VERSION,
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert not _row_exists(sync_url, "endpoint_descriptor", tenant_descriptor), (
        "a tenant-scoped probe-epoch descriptor must be reaped (tenant-inclusive)"
    )
    assert not _row_exists(sync_url, "operation_group", tenant_group), (
        "a tenant-scoped probe-epoch group must be reaped (tenant-inclusive)"
    )


def test_typed_probe_epoch_descriptor_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``source_kind`` guard: a typed descriptor is never reaped.

    Typed / composite connectors are hand-coded and can never carry a
    probe-epoch impl_id, but the ``source_kind = 'ingested'`` guard makes
    the ``endpoint_descriptor`` blast radius provably ingest-only even in
    the impossible case a typed row's impl_id took the shape.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    typed_descriptor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id=_PROBE_EPOCH_IMPL_ID,
        op_id="GET:/api/probe",
        version=_STABLE_VERSION,
        source_kind="typed",
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert _row_exists(sync_url, "endpoint_descriptor", typed_descriptor), (
        "a typed descriptor must survive the ingested-only reap guard"
    )


def test_re_running_migration_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Replaying ``upgrade()`` on a cleaned DB deletes nothing further.

    Stamp-back replay pinned to this migration's own revision
    (``stamp("0087") -> upgrade("0088")``), never ``head``.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    epoch = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id=_PROBE_EPOCH_IMPL_ID,
        op_id="GET:/api/probe",
        version=_STABLE_VERSION,
    )
    survivor = _insert_descriptor_row(
        sync_url,
        tenant_id=None,
        product="fleet",
        impl_id=_STABLE_IMPL_ID,
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
        impl_id=_STABLE_IMPL_ID,
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
