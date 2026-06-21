# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retire the stale long-product ingest orphan rows ``0038`` skipped.

Revision ID: 0049
Revises: 0048
Create Date: 2026-06-21

Initiative #1998 (G0.28 closed-loop dogfood hardening), Task #2001.
The operation-row cleanup the long↔short product realignment
(#1810 / #1814) deferred, and the destructive complement migration
``0038`` deliberately left undone.

Why this migration exists
-------------------------

The endpoint-descriptor product-split backfill (migration ``0038``,
#1701) reconciled built-in ``tenant_id IS NULL`` rows from the long
registry spelling (``vcf-logs`` etc.) to the short, dispatch-canonical
spelling (``vrli`` etc.) -- **except** where a short-spelling twin
already existed on the same global natural key. ``0038``'s correlated
``NOT EXISTS`` guard *skips* those long rows rather than rewriting them
(which would collide with the partial unique indexes from migration
``0005``). A short twin pre-exists exactly when an operator re-ingested
a VCF-family connector after upgrading -- the re-ingest auto-registers
the short-product rows, and the post-#1677 register-time reconciliation
writes new ingests under the short spelling too. The long row is then
left behind, untouched.

Those skipped rows are dead weight:

* **Non-dispatchable.** Every dispatch / query probe
  (:func:`~meho_backplane.operations._lookup.connector_exists`,
  ``count_known_ops``, ``list_operation_groups``) keys on the short,
  ``parse_connector_id``-derived product. A row physically stored under
  ``product='vcf-logs'`` for ``impl_id='vrli-rest'`` is invisible to
  all of them -- the short twin is the live row.
* **Undeletable.** ``meho.connector.delete``
  (:meth:`~meho_backplane.operations.ingest.service.IngestService.delete_connector`)
  resolves its target via ``parse_connector_id``, which derives
  ``product='vrli'`` from a ``vrli-rest-9.0.2`` connector_id. There is
  no ``product`` selector on the REST route or the MCP tool, so the
  long ``vcf-logs`` row can never be the DELETE target -- the only
  outcome is ``ConnectorNotFoundError`` (#1910, the v0.16.x dogfood
  finding). ``0038``'s own docstring delegated this cleanup to "the
  DELETE surface (#1700)", which provably cannot reach a
  divergent-product row.
* **A review/enable_reads shadow.** Because the long row shares
  ``(version, impl_id)`` with the live short twin, it surfaces as a
  redundant ``vcf-logs`` candidate when an operator reviews / enables
  reads on ``vrli-rest-9.0.2``.

Migration ``0047`` (#1814) already reconciled the ``targets.product``
side of this realignment; it neither did nor can retire these
operation-row orphans (it rewrites ``targets``, not
``operation_group`` / ``endpoint_descriptor``). This migration is the
operation-row complement: it DELETEs the orphans the resolver can't
reach.

Fix shape -- Alembic data migration
-----------------------------------

Same self-contained shape as migrations ``0011`` / ``0038`` / ``0046``
/ ``0047``: lightweight :func:`sa.table` / :func:`sa.column` shims
mirror only the columns the migration touches, no ORM imports
(importing the live models would pin the migration to one moment in the
schema's history and break replay), and each statement executes
synchronously via ``op.get_bind()``. No UUID bind params are written
(this migration only DELETEs by text predicates), so the
``docs/codebase/migrations.md`` UUID-binding rule does not apply.

Mapping table
-------------

The six long->short product splits, snapshotted from ``_PRODUCT_SPLITS``
in migration ``0038`` (which mirrors the connector registrations PR
#1677 reconciled). ``vcf-logs`` -> ``vrli`` is the one the #1910 finding
hit; the others are retired under the identical rule for completeness so
no VCF-family connector carries an undeletable long-product shadow:

==================  ================
registry product    dispatch product
==================  ================
``hetzner-robot``   ``hetzner``
``sddc-manager``    ``sddc``
``vcf-automation``  ``vcfa``
``vcf-fleet``       ``fleet``
``vcf-logs``        ``vrli``
``vcf-operations``  ``vrops``
==================  ================

Row-narrowing predicate
-----------------------

A row is deleted only when **all** of the following hold -- the exact
inverse of ``0038``'s collision-skip:

* ``tenant_id IS NULL`` -- built-in / global rows only. Tenant-scoped
  rows are operator-owned (the #1699 NULL/UUID-namespace contract);
  operators re-ingest under their tenant to update those. Same boundary
  ``0038`` drew on the write side. **Never delete a tenant row.**
* ``product = '<long>'`` -- the row still carries the pre-#1677 registry
  spelling. (After ``0038`` ran, a long row only survives *because* it
  was skipped -- i.e. a short twin exists; this DELETE finishes that
  cleanup.)
* ``impl_id = '<short>' OR impl_id LIKE '<short>-%'`` --
  ``parse_connector_id``'s first-hyphen-segment rule expressed in
  portable SQL. It scopes the DELETE to rows the dispatcher *would*
  resolve under the short spelling, so a hypothetical
  ``product='vcf-logs'`` row whose ``impl_id`` derives some *other*
  product is left alone rather than wrongly retired.
* ``EXISTS (<short twin>)`` -- the live-twin guard. A long row is
  retired only when a short-product twin already exists on the same
  global natural key, so the connector's operations remain represented
  by the live (dispatchable) short rows. This is the same correlated
  natural-key probe ``0038`` uses, with the polarity flipped from
  ``NOT EXISTS`` (skip the long row) to ``EXISTS`` (retire it).
  Crucially, a long row **without** a short twin is NOT deleted: it is
  still the only representation of that operation, and ``0038`` would
  have already rewritten it to the short spelling -- so a surviving
  twin-less long row is an edge case (e.g. ``0038`` not yet applied)
  where deleting would lose data. Leaving it for ``0038``'s rewrite is
  the safe choice.

Idempotency
-----------

The DELETE is self-idempotent: once the orphan rows are gone, the
predicate matches nothing, so a re-run (or the stamp-back replay the
test suite exercises) is a no-op. No ``updated_at`` to bump on a
delete.

``group_id`` linkage needs no touch-up: the deleted ``operation_group``
orphans and their ``endpoint_descriptor`` orphans are retired under the
same predicate, so a connector's descriptors and groups are removed
together. The live short twins (a separate ``group_id`` lineage created
by the re-ingest) are untouched.

Reversibility contract
----------------------

``downgrade()`` is a documented no-op, same rationale as ``0038``: the
deleted rows were never dispatchable (that is the defect) and carried no
operator value -- the live short twins served every dispatch / review
probe. Recreating them would re-introduce the undeletable shadow.
Production rollback is image-revert plus the additive-only forward-compat
contract (``docs/codebase/migrations.md``): a reverted image reads the
live short rows exactly as it did before, because the short spelling is
what the dispatch surface keyed on in every release. No DDL runs in
``upgrade()``, so there is nothing to undo at the schema layer either.

Cross-references
----------------

* Task #2001 / Initiative #1998 -- this cleanup.
* Issue #1910 -- the dogfood finding (stale ``vcf-logs`` row +
  undeletability) this retires.
* Migration ``0038`` / #1701 -- the backfill whose collision-skip left
  these orphans; this migration is its destructive complement.
* Migration ``0047`` / #1814 -- the ``targets.product`` reconciliation
  this completes on the operation-row side.
* :func:`~meho_backplane.operations._lookup.parse_connector_id` -- the
  product-derivation rule that makes a divergent-product row unreachable
  for DELETE.
* :mod:`tests.test_migration_0049_retire_stale_vcf_logs_orphan_rows` --
  the behavioural contract: retire-with-twin, tenant boundary,
  twin-less long row preserved, foreign impl_id preserved, unmapped
  product preserved, idempotency, post-migration dispatch parity.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Every known long->short product split, as ``(long_product,
#: short_product)``. Snapshot of ``_PRODUCT_SPLITS`` in migration
#: ``0038`` (minus the per-split impl_id column: the impl_id predicate
#: is *derived* from the short product below, mirroring how
#: :func:`~meho_backplane.operations._lookup.parse_connector_id`
#: derives the product from the first hyphen-segment of ``impl_id``).
_PRODUCT_SPLITS: tuple[tuple[str, str], ...] = (
    ("hetzner-robot", "hetzner"),
    ("sddc-manager", "sddc"),
    ("vcf-automation", "vcfa"),
    ("vcf-fleet", "fleet"),
    ("vcf-logs", "vrli"),
    ("vcf-operations", "vrops"),
)


def _retire_table(*, table_name: str, op_discriminator: str) -> None:
    """Issue one guarded DELETE per product split against *table_name*.

    ``op_discriminator`` names the per-operation natural-key column that
    completes the global unique index alongside ``(product, version,
    impl_id)`` -- ``op_id`` on ``endpoint_descriptor``, ``group_key`` on
    ``operation_group``. It participates only in the live-twin
    correlation; the retire predicate itself is per-connector.

    One statement per split (12 total across both tables) rather than a
    single bulk DELETE -- same auditability call as ``0038``: the row
    volume is small and per-split statements keep the SQL log
    attributable.
    """
    shim = sa.table(
        table_name,
        sa.column("tenant_id", sa.Uuid()),
        sa.column("product", sa.Text()),
        sa.column("version", sa.Text()),
        sa.column("impl_id", sa.Text()),
        sa.column(op_discriminator, sa.Text()),
    )
    twin = shim.alias("twin")
    bind = op.get_bind()

    for long_product, short_product in _PRODUCT_SPLITS:
        # Correlated existence probe for a live row already persisted
        # under the short spelling on the same global natural key. The
        # outer DELETE target correlates automatically; only ``twin`` is
        # selected from. EXISTS (not NOT EXISTS as in 0038): we retire
        # the long orphan only when the short twin is there to represent
        # the connector.
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
        stmt = sa.delete(shim).where(
            shim.c.tenant_id.is_(None),
            shim.c.product == long_product,
            # First-hyphen-segment rule from ``parse_connector_id``: the
            # dispatcher resolves this row under ``short_product`` exactly
            # when ``impl_id`` is the short product itself or starts with
            # ``"<short>-"``.
            sa.or_(
                shim.c.impl_id == short_product,
                shim.c.impl_id.like(f"{short_product}-%"),
            ),
            short_twin_exists,
        )
        bind.execute(stmt)


def upgrade() -> None:
    """Retire the long-product orphan rows whose live short twin exists.

    Both row surfaces the dispatch/query layer reads --
    ``endpoint_descriptor`` and ``operation_group`` -- are retired under
    the same predicate so a connector's orphaned operations and groups
    are removed together, leaving the live short twins as the sole
    representation.
    """
    _retire_table(table_name="endpoint_descriptor", op_discriminator="op_id")
    _retire_table(table_name="operation_group", op_discriminator="group_key")


def downgrade() -> None:
    """No-op by design.

    The retired rows were never dispatchable (that is the defect this
    migration fixes) and carried no operator value -- the live short
    twins served every dispatch / review probe. Recreating them would
    re-introduce the undeletable ``vcf-logs`` shadow. Production rollback
    is image-revert plus the additive-only forward-compat contract
    (``docs/codebase/migrations.md``); a reverted image reads the live
    short rows exactly as it did before. No DDL runs in ``upgrade()``, so
    there is nothing to undo at the schema layer either. Same
    documented-no-op shape as migration ``0038``.
    """
    # Intentionally empty -- see docstring. The function stays defined so
    # ``alembic downgrade -1`` resolves the symbol cleanly.
