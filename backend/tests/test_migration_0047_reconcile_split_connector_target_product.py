# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0047_reconcile_split_connector_target_product``.

Initiative #1810 (retire the long↔short connector product divergence),
Task #1814 (the ATOMIC realign + migration). The migration reconciles
existing operator ``targets.product`` values from the five historical
long spellings (``sddc-manager`` / ``vcf-automation`` / ``vcf-fleet`` /
``vcf-operations`` / ``hetzner-robot``) to their short, dispatch-
canonical token (``sddc`` / ``vcfa`` / ``fleet`` / ``vrops`` /
``hetzner``) after #1814 realigned the connectors — the data half of the
breaking realignment. Mirrors
:mod:`tests.test_migration_0046_reconcile_vrli_target_product`.

Test matrix
-----------

* **Live long-token target → reconciled.** For each of the five
  mappings, a non-soft-deleted row carries the short token (and a bumped
  ``updated_at``) after ``upgrade head``.
* **Per-product scoping.** Five live long-token rows seeded together each
  land on their own short token — no mapping bleeds into another.
* **Soft-deleted long-token target → preserved.** A row with
  ``deleted_at`` set is a tombstone and is left under the stale spelling.
* **Unrelated product → untouched.** A ``product='vmware'`` row (and a
  ``product='vrli'`` row, which ``0046`` already owns) is not rewritten.
* **Idempotency.** Re-running ``upgrade()`` against an already-reconciled
  DB (stamp back + re-upgrade) changes nothing.
* **Downgrade inverse.** ``downgrade`` rewrites live short-token rows
  back to their long spelling (the best-effort image-revert inverse).

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

#: Deliberately old seed timestamp — lets assertions tell "migration
#: bumped updated_at" apart from "row untouched" without sleeping.
_SEED_TS: Final[str] = "2026-01-01T00:00:00+00:00"

#: A tenant id every seeded row shares (targets are per-tenant; the
#: rename is tenant-agnostic).
_TENANT: Final[UUID] = UUID("11111111-1111-1111-1111-111111111111")

#: The five long → short renames the migration performs (kept in lockstep
#: with ``_PRODUCT_RENAMES`` in the migration module).
_RENAMES: Final[dict[str, str]] = {
    "sddc-manager": "sddc",
    "vcf-automation": "vcfa",
    "vcf-fleet": "fleet",
    "vcf-operations": "vrops",
    "hetzner-robot": "hetzner",
}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Same harness as :mod:`tests.test_migration_0046_reconcile_vrli_target_product`:
    sync fixture (``alembic.command`` calls ``asyncio.run`` internally),
    per-test SQLite file under ``tmp_path``, settings + engine caches
    reset on both sides so the alembic env reads *this* ``DATABASE_URL``.
    """
    db_path = tmp_path / "migration_0047.db"
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
                    "host": "appliance.example.test",
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


@pytest.mark.parametrize(("long_product", "short_product"), sorted(_RENAMES.items()))
def test_live_long_token_target_reconciled_to_short(
    alembic_cfg: tuple[Config, str],
    long_product: str,
    short_product: str,
) -> None:
    """A live long-token target carries the short token after upgrade.

    ``updated_at`` is bumped so operator tooling driven off the column
    sees the change.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0046")

    row_id = _insert_target(sync_url, name=f"{short_product}-prod", product=long_product)

    command.upgrade(cfg, "head")

    product, updated_at = _read_row(sync_url, row_id)
    assert product == short_product, (
        f"live {long_product} target must be reconciled to {short_product}"
    )
    assert updated_at != _SEED_TS, "updated_at must be bumped by the rewrite"


def test_all_five_renames_are_per_product_scoped(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Five long-token rows seeded together each land on their own short token.

    Proves the per-product scoping: no mapping bleeds into another.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0046")

    ids = {
        long_product: _insert_target(sync_url, name=f"{long_product}-target", product=long_product)
        for long_product in _RENAMES
    }

    command.upgrade(cfg, "head")

    for long_product, row_id in ids.items():
        product, _updated_at = _read_row(sync_url, row_id)
        assert product == _RENAMES[long_product], (
            f"{long_product} must reconcile to {_RENAMES[long_product]}, got {product!r}"
        )


def test_soft_deleted_long_token_target_preserved(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A soft-deleted long-token tombstone is left under the stale spelling."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0046")

    row_id = _insert_target(
        sync_url,
        name="sddc-deleted",
        product="sddc-manager",
        deleted_at="2026-02-01T00:00:00+00:00",
    )

    command.upgrade(cfg, "head")

    product, updated_at = _read_row(sync_url, row_id)
    assert (product, updated_at) == ("sddc-manager", _SEED_TS), (
        "soft-deleted target must keep its product and updated_at"
    )


@pytest.mark.parametrize("unrelated", ["vmware", "vrli", "k8s"])
def test_unrelated_product_untouched(
    alembic_cfg: tuple[Config, str],
    unrelated: str,
) -> None:
    """An unrelated product is not rewritten — the rename is family-scoped.

    ``vrli`` in particular is owned by migration ``0046``, not this one;
    a ``vrli`` row must survive ``0047`` unchanged.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0046")

    row_id = _insert_target(sync_url, name=f"{unrelated}-prod", product=unrelated)

    command.upgrade(cfg, "head")

    product, updated_at = _read_row(sync_url, row_id)
    assert (product, updated_at) == (unrelated, _SEED_TS), (
        f"unrelated product {unrelated!r} must be untouched"
    )


def test_upgrade_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Re-running the upgrade against an already-reconciled DB changes nothing.

    Stamp back to the down-revision and upgrade again — the replay shape
    the sibling 0046 / 0038 tests use. The second pass finds no long-token
    rows, so the already-short row keeps its post-first-migration
    ``updated_at`` (no second bump).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0046")

    row_id = _insert_target(sync_url, name="vcfa-prod", product="vcf-automation")

    command.upgrade(cfg, "head")
    _product_after_first, updated_after_first = _read_row(sync_url, row_id)

    # Stamp back and replay upgrade() against the reconciled DB.
    command.stamp(cfg, "0046")
    command.upgrade(cfg, "head")

    product_after_second, updated_after_second = _read_row(sync_url, row_id)
    assert product_after_second == "vcfa"
    assert updated_after_second == updated_after_first, (
        "idempotent re-run must not bump updated_at a second time"
    )


@pytest.mark.parametrize(("long_product", "short_product"), sorted(_RENAMES.items()))
def test_downgrade_restores_long_token(
    alembic_cfg: tuple[Config, str],
    long_product: str,
    short_product: str,
) -> None:
    """``downgrade`` rewrites live short-token targets back to the long spelling.

    The best-effort image-revert inverse: the long spelling was
    dispatchable on the pre-#1814 image, so restoring it keeps the target
    resolvable against the older code.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    row_id = _insert_target(sync_url, name=f"{short_product}-prod", product=short_product)

    command.downgrade(cfg, "0046")

    product, updated_at = _read_row(sync_url, row_id)
    assert product == long_product, "downgrade must restore the historical spelling"
    assert updated_at != _SEED_TS, "downgrade bumps updated_at on the rewritten row"
