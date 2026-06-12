# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Backfill pre-v0.14.0 product-split orphans to the dispatch-canonical spelling.

Revision ID: 0038
Revises: 0037
Create Date: 2026-06-12

Initiative #1691 (G0.25 v0.14.0 cycle-10 dogfood hardening), Task
#1701 (CI-3). PR #1677 (#1647) made ``register_ingested_operations``
reconcile the operator-supplied *registry* product to the
dispatch-canonical spelling at write time
(:func:`~meho_backplane.operations.ingest.register_ingested._reconciled_row_product`
-> :func:`~meho_backplane.operations._lookup.dispatch_product`), so
new ingests of the VCF-family / SDDC / Hetzner-Robot connectors land
under the product the dispatcher derives from the connector_id
(``vcf-logs`` -> ``vrli``, etc.). That fix is forward-only: rows
ingested on v0.13.0 and earlier still carry the long registry
spelling, and every dispatch probe (``connector_exists``,
``search_operations``, ``list_operation_groups``) keys on the short,
parser-derived product -- the pre-existing rows are invisible, the
catalog reports ``registered, 0 ops``, and the operations are
non-dispatchable (claude-rdc-hetzner-dc v0.14.0 cycle-10 finding
CI-3). This migration is the one-shot data backfill that reconciles
those orphaned rows.

Fix shape -- Alembic data migration
-----------------------------------

Same self-contained shape as migration ``0011`` (the v0.3.2
``when_to_use`` backfill precedent): lightweight :func:`sa.table` /
:func:`sa.column` shims mirror only the columns the migration
touches, no ORM imports (importing the live models would pin the
migration to one moment in the schema's history and break replay),
statements execute synchronously via ``op.get_bind()``.

Mapping table
-------------

The six long<->short splits, snapshotted from the fixture
``_VCF_PRODUCT_SPLITS`` in ``tests/test_operations_register_ingested.py``
(which mirrors the connector registrations PR #1677 reconciled):

==================  ================  ================
registry product    impl_id           dispatch product
==================  ================  ================
``vcf-logs``        ``vrli-rest``     ``vrli``
``vcf-automation``  ``vcfa-rest``     ``vcfa``
``vcf-fleet``       ``fleet-rest``    ``fleet``
``vcf-operations``  ``vrops-rest``    ``vrops``
``sddc-manager``    ``sddc-rest``     ``sddc``
``hetzner-robot``   ``hetzner-rest``  ``hetzner``
==================  ================  ================

The values are inlined rather than imported because migrations must
stay self-contained; the runtime mapping source of truth remains
:func:`~meho_backplane.operations._lookup.dispatch_product`.

Row-narrowing predicate
-----------------------

A row is rewritten only when **all** of the following hold:

* ``tenant_id IS NULL`` -- built-in / global rows only. Tenant-scoped
  rows are operator-owned (the #1699 contract documents the NULL/UUID
  namespaces as deliberately distinct shadow copies); operators
  re-ingest under their tenant to update those. Same boundary
  migration ``0011`` drew.
* ``product = '<long>'`` -- the row still carries the pre-#1677
  registry spelling. This is also what makes the UPDATE idempotent:
  after the rewrite the row carries the short spelling and no longer
  matches, so a re-run (or the stamp-back replay the test suite
  exercises) is a no-op.
* ``impl_id = '<short>' OR impl_id LIKE '<short>-%'`` -- the
  dispatcher derives the product from the first hyphen-segment of
  ``impl_id`` (:func:`~meho_backplane.operations._lookup.parse_connector_id`);
  this clause is that rule expressed in portable SQL. It scopes the
  rewrite to rows the dispatcher would actually resolve under the
  short spelling -- a hypothetical ``product='vcf-logs'`` row whose
  ``impl_id`` derives some *other* product is left alone rather than
  corrupted into a triple the dispatcher still couldn't reach.
* ``NOT EXISTS (<short twin>)`` -- collision guard, see below.

Collision guard
---------------

The partial unique indexes from migration ``0005``
(``endpoint_descriptor_global_idx`` on ``(product, version, impl_id,
op_id)`` and ``operation_group_global_idx`` on ``(product, version,
impl_id, group_key)``, both ``WHERE tenant_id IS NULL``) mean the
product rewrite can collide: an operator who upgraded to v0.14.0 and
*re-ingested* a connector already has fresh rows under the short
spelling, and rewriting the stale long-spelling row onto the same
natural key would raise ``IntegrityError`` and fail the helm
pre-upgrade migration Job mid-deploy. Each UPDATE therefore carries a
correlated ``NOT EXISTS`` self-join that skips long rows whose short
twin already exists. Skipped rows stay exactly as they were --
invisible to dispatch, which is the pre-migration status quo (the
catalog's round-trip integrity gate already drops them from
listings); the re-ingested short rows are the live ones. Deleting the
stale twins is deliberately out of scope for a backfill -- destructive
cleanup stays with the operator (see Task #1700's DELETE surface).

``group_id`` linkage needs no touch-up: ``endpoint_descriptor`` rows
reference their group by UUID FK, which the product rewrite does not
change, and both tables are rewritten under the same predicate so a
connector's descriptors and groups move together.

Reversibility contract
----------------------

``downgrade()`` is a documented no-op, same rationale as migration
``0011``: the long spellings carry no operator value (they were never
dispatchable -- that is the bug), production rollback is image-revert
plus forward-compatible schema (the additive-only contract in
``docs/codebase/migrations.md``), and restoring them would
re-orphan the rows. No DDL runs in ``upgrade()``, so there is nothing
to undo at the schema layer either.

Cross-references
----------------

* Task #1701 / Initiative #1691 (G0.25) -- this backfill.
* Issue #1647 / PR #1677 -- the forward-only register-time
  reconciliation this migration completes.
* :func:`~meho_backplane.operations._lookup.dispatch_product` -- the
  runtime mapping rule this migration mirrors in SQL.
* Migration ``0011`` -- the self-contained data-backfill precedent
  (shims, idempotent predicate, documented no-op downgrade).
* :mod:`tests.test_migration_0038_backfill_product_splits` -- the
  behavioural contract: rewrite, tenant boundary, collision skip,
  idempotency, and post-migration dispatch-probe resolution.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Every known long->short product split, as ``(registry_product,
#: dispatch_product)``. Snapshot of ``_VCF_PRODUCT_SPLITS`` in
#: ``tests/test_operations_register_ingested.py`` (minus the per-split
#: impl_id column: the impl_id predicate is *derived* from the short
#: product below, mirroring how
#: :func:`~meho_backplane.operations._lookup.dispatch_product` derives
#: the product from the first hyphen-segment of ``impl_id`` rather
#: than from a lookup table).
_PRODUCT_SPLITS: tuple[tuple[str, str], ...] = (
    ("hetzner-robot", "hetzner"),
    ("sddc-manager", "sddc"),
    ("vcf-automation", "vcfa"),
    ("vcf-fleet", "fleet"),
    ("vcf-logs", "vrli"),
    ("vcf-operations", "vrops"),
)


def _backfill_table(
    *,
    table_name: str,
    op_discriminator: str,
    now: datetime,
) -> None:
    """Issue one guarded UPDATE per product split against *table_name*.

    ``op_discriminator`` names the per-operation natural-key column
    that completes the global unique index alongside ``(product,
    version, impl_id)`` -- ``op_id`` on ``endpoint_descriptor``,
    ``group_key`` on ``operation_group``. It participates only in the
    collision guard's twin correlation; the rewrite predicate itself
    is per-connector, not per-operation.

    One statement per split (12 total across both tables) rather than
    a single CASE-expression bulk UPDATE -- same auditability call as
    migration ``0011``: the row volume is small (hundreds of rows per
    affected connector at most) and per-split statements keep the SQL
    log attributable.
    """
    shim = sa.table(
        table_name,
        sa.column("tenant_id", sa.Uuid()),
        sa.column("product", sa.Text()),
        sa.column("version", sa.Text()),
        sa.column("impl_id", sa.Text()),
        sa.column(op_discriminator, sa.Text()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    twin = shim.alias("twin")
    bind = op.get_bind()

    for long_product, short_product in _PRODUCT_SPLITS:
        # Correlated existence probe for a row already persisted under
        # the short spelling on the same global natural key (a
        # post-upgrade re-ingest). The outer UPDATE target correlates
        # automatically; only ``twin`` is selected from.
        short_twin_exists = (
            sa.select(sa.literal(1))
            .select_from(twin)
            .where(
                twin.c.tenant_id.is_(None),
                twin.c.product == short_product,
                twin.c.version == shim.c.version,
                twin.c.impl_id == shim.c.impl_id,
                twin.c[op_discriminator] == shim.c[op_discriminator],
            )
            .exists()
        )
        stmt = (
            sa.update(shim)
            .where(
                shim.c.tenant_id.is_(None),
                shim.c.product == long_product,
                # First-hyphen-segment rule from ``parse_connector_id``:
                # the dispatcher resolves this row under ``short_product``
                # exactly when ``impl_id`` is the short product itself or
                # starts with ``"<short>-"``.
                sa.or_(
                    shim.c.impl_id == short_product,
                    shim.c.impl_id.like(f"{short_product}-%"),
                ),
                ~short_twin_exists,
            )
            .values(product=short_product, updated_at=now)
        )
        bind.execute(stmt)


def upgrade() -> None:
    """Reconcile orphaned long-product rows to the dispatch-canonical spelling.

    Both row surfaces the dispatch/query layer reads --
    ``endpoint_descriptor`` and ``operation_group`` -- are rewritten
    under the same predicate so a connector's operations and groups
    stay aligned. ``updated_at`` is bumped (single ``now`` per run,
    same discipline as migration ``0011``) so operator tooling driven
    off the column sees the change.
    """
    now = datetime.now(UTC)
    _backfill_table(
        table_name="endpoint_descriptor",
        op_discriminator="op_id",
        now=now,
    )
    _backfill_table(
        table_name="operation_group",
        op_discriminator="group_key",
        now=now,
    )


def downgrade() -> None:
    """No-op by design.

    Restoring the long registry spellings would re-orphan the rows --
    the long form was never dispatchable (that is the defect this
    migration fixes), so the pre-upgrade state carries no operator
    value to recover. Production rollback is image-revert plus the
    additive-only forward-compat contract (``docs/codebase/migrations.md``);
    a v0.13.x image reading short-spelling rows simply serves them,
    because the short spelling is what the dispatch surface keyed on
    in every release. No DDL runs in ``upgrade()``, so there is
    nothing to undo at the column / index / constraint layer. Same
    documented-no-op shape as migration ``0011``.
    """
    # Intentionally empty -- see docstring. The function stays defined
    # so ``alembic downgrade -1`` resolves the symbol cleanly.
