# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0046_reconcile_vrli_target_product``.

Initiative #1800 (G0.26 v0.16.0 dogfood hardening), Task #1798 (T4). The
migration reconciles existing operator ``targets.product`` values from
the historical ``"vcf-logs"`` spelling to the canonical ``"vrli"`` after
:class:`~meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector`
realigned to ``product="vrli"`` — the data half of the SEV-2 fix.

Test matrix
-----------

* **Live ``vcf-logs`` target → reconciled.** A non-soft-deleted row
  carries ``product='vrli'`` (and a bumped ``updated_at``) after
  ``upgrade head``.
* **Soft-deleted ``vcf-logs`` target → preserved.** A row with
  ``deleted_at`` set is a tombstone and is left under the stale spelling.
* **Unrelated product → untouched.** A ``product='vmware'`` row is not
  rewritten (the migration is scoped to the one vRLI rename).
* **Idempotency.** Re-running ``upgrade()`` against an already-reconciled
  DB (stamp back + re-upgrade, the replay shape the sibling
  :mod:`tests.test_migration_0038_backfill_product_splits` established)
  changes nothing.
* **Downgrade inverse.** ``downgrade`` rewrites live ``vrli`` rows back
  to ``vcf-logs`` (the best-effort image-revert inverse).

Sync-test constraint: ``alembic.command.upgrade`` drives env.py's async
cookbook through ``asyncio.run``, so the test functions stay sync.
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

_VERSION: Final[str] = "9.0"

#: Deliberately old seed timestamp — lets assertions tell "migration
#: bumped updated_at" apart from "row untouched" without sleeping.
_SEED_TS: Final[str] = "2026-01-01T00:00:00+00:00"

#: A tenant id every seeded row shares (targets are per-tenant; the
#: rename is tenant-agnostic).
_TENANT: Final[UUID] = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Same harness as :mod:`tests.test_migration_0038_backfill_product_splits`:
    sync fixture (``alembic.command`` calls ``asyncio.run`` internally),
    per-test SQLite file under ``tmp_path``, settings + engine caches
    reset on both sides so the alembic env reads *this* ``DATABASE_URL``.
    """
    db_path = tmp_path / "migration_0046.db"
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


def _insert_target(
    sync_url: str,
    *,
    name: str,
    product: str,
    deleted_at: str | None = None,
) -> UUID:
    """Insert one minimal ``targets`` row at the migration's down-revision.

    Raw SQL (not the ORM) keeps the seed pinned to the schema the
    migration runs against. Columns with SQLite server defaults
    (``aliases``, ``auth_model``, ``vpn_required``, ``extras``) are
    omitted; the NOT-NULL columns without a SQLite default
    (``created_at`` / ``updated_at``) are supplied explicitly. UUID binds
    use ``.hex`` per ``docs/codebase/migrations.md``.
    """
    row_id = uuid4()
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO targets (
                        id, tenant_id, name, product, host,
                        deleted_at, created_at, updated_at
                    ) VALUES (
                        :id, :tenant_id, :name, :product, :host,
                        :deleted_at, :ts, :ts
                    )
                    """,
                ),
                {
                    "id": row_id.hex,
                    "tenant_id": _TENANT.hex,
                    "name": name,
                    "product": product,
                    "host": "vrli.example.test",
                    "deleted_at": deleted_at,
                    "ts": _SEED_TS,
                },
            )
    finally:
        sync_eng.dispose()
    return row_id


def _read_row(sync_url: str, row_id: UUID) -> tuple[str, str]:
    """Return ``(product, updated_at)`` for one target row, as strings."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text("SELECT product, updated_at FROM targets WHERE id = :id"),
                {"id": row_id.hex},
            ).one()
            return (str(row.product), str(row.updated_at))
    finally:
        sync_eng.dispose()


def test_live_vcf_logs_target_reconciled_to_vrli(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A live ``product='vcf-logs'`` target carries ``'vrli'`` after upgrade.

    ``updated_at`` is bumped so operator tooling driven off the column
    sees the change.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0045")

    row_id = _insert_target(sync_url, name="vrli-prod", product="vcf-logs")

    command.upgrade(cfg, "head")

    product, updated_at = _read_row(sync_url, row_id)
    assert product == "vrli", "live vcf-logs target must be reconciled to the canonical token"
    assert updated_at != _SEED_TS, "updated_at must be bumped by the rewrite"


def test_soft_deleted_vcf_logs_target_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A soft-deleted ``vcf-logs`` tombstone is left under the stale spelling."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0045")

    row_id = _insert_target(
        sync_url,
        name="vrli-deleted",
        product="vcf-logs",
        deleted_at="2026-02-01T00:00:00+00:00",
    )

    command.upgrade(cfg, "head")

    product, updated_at = _read_row(sync_url, row_id)
    assert (product, updated_at) == ("vcf-logs", _SEED_TS), (
        "soft-deleted target must keep its product and updated_at"
    )


def test_unrelated_product_untouched(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A ``product='vmware'`` target is not rewritten — the rename is vRLI-scoped."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0045")

    row_id = _insert_target(sync_url, name="vc-prod", product="vmware")

    command.upgrade(cfg, "head")

    product, updated_at = _read_row(sync_url, row_id)
    assert (product, updated_at) == ("vmware", _SEED_TS), "unrelated product must be untouched"


def test_upgrade_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Re-running the upgrade against an already-reconciled DB changes nothing.

    Stamp back to the down-revision and upgrade again — the replay shape
    the sibling 0038 / 0011 tests use. The second pass finds no
    ``vcf-logs`` rows, so the already-``vrli`` row keeps its
    post-first-migration ``updated_at`` (no second bump).

    Both passes pin the target to ``0046`` (not ``head``) so future
    non-idempotent schema migrations cannot leak into the stamp-back
    replay: ``command.stamp`` only rewrites alembic's version table — it
    does not run downgrade SQL — so any column a later migration adds on
    the first upgrade is still physically present, and replaying its
    non-idempotent DDL through ``head`` would fail (e.g. 0050's
    ``add_column("targets", "tls_server_name")`` → "duplicate column").
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0045")

    row_id = _insert_target(sync_url, name="vrli-prod", product="vcf-logs")

    command.upgrade(cfg, "0046")
    _product_after_first, updated_after_first = _read_row(sync_url, row_id)

    # Stamp back and replay upgrade() against the reconciled DB.
    command.stamp(cfg, "0045")
    command.upgrade(cfg, "0046")

    product_after_second, updated_after_second = _read_row(sync_url, row_id)
    assert product_after_second == "vrli"
    assert updated_after_second == updated_after_first, (
        "idempotent re-run must not bump updated_at a second time"
    )


def test_downgrade_restores_vcf_logs(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade`` rewrites live ``vrli`` targets back to ``vcf-logs``.

    The best-effort image-revert inverse: ``vcf-logs`` was dispatchable
    on the pre-#1798 image, so restoring it keeps the target resolvable
    against the older code.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    row_id = _insert_target(sync_url, name="vrli-prod", product="vrli")

    command.downgrade(cfg, "0045")

    product, updated_at = _read_row(sync_url, row_id)
    assert product == "vcf-logs", "downgrade must restore the historical spelling"
    assert updated_at != _SEED_TS, "downgrade bumps updated_at on the rewritten row"
