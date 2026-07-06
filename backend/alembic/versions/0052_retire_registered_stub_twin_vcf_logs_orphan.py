# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retire the vcf-logs orphan ``0049`` missed (0-row registered stub twin).

Revision ID: 0052
Revises: 0051
Create Date: 2026-07-06

Initiative #2065 (G0.29 v0.19.0 closed-loop dogfood hardening), Task #2068.
The forward complement of migration ``0049`` (#2001 / PR #2015): the
same long-product ``vcf-logs`` ingest orphan, retired for the case
``0049``'s live-twin ``EXISTS`` probe could not reach.

Why this migration exists
-------------------------

Migration ``0049`` retires a long-product orphan
(``product='vcf-logs'``, ``impl_id='vrli-rest-…'``) only when a
short-product twin already exists **on the same per-op / per-group
natural key** -- its guard is a correlated ``EXISTS`` on
``endpoint_descriptor`` / ``operation_group`` requiring the short twin
to carry a matching ``op_id`` / ``group_key`` at the **same version**.

On the RDC v0.19.0 upgrade (#2068 finding) the orphan survived anyway.
The canonical short representation (``vrli``) is registered via the
**class-side v2 connector registry**, not by an ingest -- so it carries
**zero** ``endpoint_descriptor`` / ``operation_group`` rows, and it sits
at a **divergent version** (``9.0`` vs the orphan's ``9.0.2``).
``0049``'s per-op ``EXISTS`` probe therefore matches nothing and retires
nothing, leaving the 136-op orphan in place through every upgrade. That
orphan is exactly the undeletable review-shadow dead weight ``0049`` and
#1910 set out to clear: non-dispatchable (dispatch keys on the short
``parse_connector_id``-derived product) and unreachable by
``meho.connector.delete`` (which resolves a divergent-product row to
``ConnectorNotFoundError``, #1910).

Fix shape -- key on the orphan's OWN attributes, not a DB twin
--------------------------------------------------------------

The critical correction over ``0049`` (and over this task's own earlier
draft, which prescribed a looser connector-grain DB twin probe): the
retire predicate must **NOT depend on any DB-visible short twin**. A
class-registry short connector has no ``endpoint_descriptor`` /
``operation_group`` rows, so *any* ``EXISTS`` probe against those tables
-- ``0049``'s per-op correlation or a connector-grain one -- returns
false for a 0-row stub and retires nothing, re-encoding the exact miss.

Instead the predicate keys entirely on the orphan row itself:

* ``tenant_id IS NULL`` -- built-in / global rows only. Tenant-scoped
  rows are operator-owned (#1699 NULL/UUID-namespace contract) and are
  **never** deleted. Same boundary ``0049`` / ``0038`` drew.
* ``product = '<long>'`` -- the row still carries the pre-#1677 registry
  spelling (one of the six ``_PRODUCT_SPLITS`` long products).
* ``impl_id = '<short>' OR impl_id LIKE '<short>-%'`` --
  ``parse_connector_id``'s first-hyphen-segment rule
  (``operations/_lookup.py:57``) in portable SQL. This scopes the
  DELETE to rows the dispatcher *would* resolve under the short
  spelling, so a ``product='vcf-logs'`` row whose ``impl_id`` derives
  some *other*, non-split product is left alone -- impl_id-family
  scoping, identical to ``0049``.

There is deliberately **no** ``EXISTS`` clause.

Why retiring without a DB twin is safe
--------------------------------------

The short product for each split in ``_PRODUCT_SPLITS`` is a **registered
connector class** in the v2 registry -- the live, dispatchable
representation of that connector, present regardless of whether it has
ingested any operation rows yet. The dispatch / query surface
(:func:`~meho_backplane.operations._lookup.connector_exists`,
``count_known_ops``, ``list_operation_groups``) keys on the short
``parse_connector_id``-derived product, so a long-product orphan whose
``impl_id`` derives a known-split short product is **always**
non-dispatchable and is **never** the sole *live* representation of that
connector -- the registered short class is. Retiring it therefore loses
no reachable data. This class-registry invariant is what ``0049``'s
per-op DB probe tried and failed to approximate; keying on the orphan's
own ``impl_id`` states it directly.

Relationship to ``0049``'s twin-less guard
------------------------------------------

``0049`` deliberately preserved a *twin-less* long row on the theory it
might be the only representation of an operation (``0038`` would rewrite
it on the write side). That caution is correct only for a long product
**outside** ``_PRODUCT_SPLITS``. For a long product **inside** the split
map, the short target is a registered connector class by construction,
so "no DB twin rows" means "not yet ingested under the short spelling",
**not** "no live representation". This migration retires exactly that
in-split subset; anything outside the split map is untouched (there is
no such row in the built-in set, but the impl_id/product scoping makes
the boundary explicit).

Idempotency
-----------

Self-idempotent: once the orphan rows are gone the predicate matches
nothing, so a re-run or a stamp-back replay is a no-op. No
``updated_at`` to bump on a DELETE. ``endpoint_descriptor`` and
``operation_group`` are retired under the same predicate, so a
connector's descriptors and groups go together.

Reversibility contract
----------------------

``downgrade()`` is a documented no-op, same rationale as ``0049`` /
``0038``: the retired rows were never dispatchable and carried no
operator value; the registered short class served every dispatch /
review probe. Recreating them would re-introduce the undeletable
``vcf-logs`` shadow. No DDL runs in ``upgrade()``, so there is nothing to
undo at the schema layer; production rollback is image-revert under the
additive-only forward-compat contract (``docs/codebase/migrations.md``).

Fix shape -- Alembic data migration
-----------------------------------

Same self-contained shape as ``0011`` / ``0038`` / ``0046`` / ``0047``
/ ``0049``: lightweight :func:`sa.table` / :func:`sa.column` shims
mirror only the columns the migration touches, no ORM imports (replay
safety), each statement executes via ``op.get_bind()``. No UUID bind
params are written (DELETE by text predicates only), so the
``docs/codebase/migrations.md`` UUID-binding rule does not apply.

Cross-references
----------------

* Task #2068 / Initiative #2065 -- this cleanup.
* Migration ``0049`` / #2001 / PR #2015 -- the retire whose per-op
  ``EXISTS`` twin probe this completes for the 0-row registered stub.
* Issue #1910 -- the original dogfood finding (stale ``vcf-logs`` row +
  undeletability).
* Migrations ``0038`` / #1701, ``0047`` / #1814 -- the backfill and
  ``targets.product`` reconciliation this operation-row cleanup follows.
* :func:`~meho_backplane.operations._lookup.parse_connector_id`
  (``operations/_lookup.py:57``) -- the product-derivation rule the
  impl_id predicate mirrors.
* :mod:`tests.test_migration_0052_retire_registered_stub_twin_vcf_logs_orphan`
  -- the behavioural contract, incl. the 0-row registered-stub-twin case
  a naive DB-``EXISTS``-twin implementation MUST fail.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Every known long->short product split, as ``(long_product,
#: short_product)``. Snapshot of ``_PRODUCT_SPLITS`` in migration
#: ``0049`` (:mod:`...0049...` at ``:192``), which in turn snapshots
#: ``0038``'s mapping. The impl_id predicate is *derived* from the short
#: product (:func:`~meho_backplane.operations._lookup.parse_connector_id`
#: derives the product from the first hyphen-segment of ``impl_id``).
_PRODUCT_SPLITS: tuple[tuple[str, str], ...] = (
    ("hetzner-robot", "hetzner"),
    ("sddc-manager", "sddc"),
    ("vcf-automation", "vcfa"),
    ("vcf-fleet", "fleet"),
    ("vcf-logs", "vrli"),
    ("vcf-operations", "vrops"),
)


def _retire_table(*, table_name: str) -> None:
    """Issue one guarded DELETE per product split against *table_name*.

    Unlike ``0049``'s ``_retire_table``, there is no ``op_discriminator``
    and no correlated twin subquery: the predicate keys solely on the
    orphan row's own ``tenant_id`` / ``product`` / ``impl_id``. Retiring
    is safe because the short product for each split is a registered
    connector class (the live representation), so a ``tenant_id IS NULL``
    long orphan whose ``impl_id`` derives a known-split short product is
    never the sole live copy -- see the module docstring.

    One statement per split keeps the SQL log attributable (same
    auditability call as ``0038`` / ``0049``); the built-in row volume is
    small.
    """
    shim = sa.table(
        table_name,
        sa.column("tenant_id", sa.Uuid()),
        sa.column("product", sa.Text()),
        sa.column("impl_id", sa.Text()),
    )
    bind = op.get_bind()

    for long_product, short_product in _PRODUCT_SPLITS:
        stmt = sa.delete(shim).where(
            shim.c.tenant_id.is_(None),
            shim.c.product == long_product,
            # First-hyphen-segment rule from ``parse_connector_id``: the
            # dispatcher resolves this row under ``short_product`` exactly
            # when ``impl_id`` is the short product itself or starts with
            # ``"<short>-"``. Scopes the DELETE to rows genuinely tied to
            # a known split; a foreign-impl_id row is left alone.
            sa.or_(
                shim.c.impl_id == short_product,
                shim.c.impl_id.like(f"{short_product}-%"),
            ),
        )
        bind.execute(stmt)


def upgrade() -> None:
    """Retire the in-split long-product orphan rows, twin or no twin.

    Both row surfaces the dispatch/query layer reads --
    ``endpoint_descriptor`` and ``operation_group`` -- are retired under
    the same predicate so a connector's orphaned descriptors and groups
    are removed together, leaving the registered short connector class as
    the sole live representation.
    """
    _retire_table(table_name="endpoint_descriptor")
    _retire_table(table_name="operation_group")


def downgrade() -> None:
    """No-op by design.

    The retired rows were never dispatchable (that is the defect this
    migration fixes) and carried no operator value -- the registered
    short connector class served every dispatch / review probe.
    Recreating them would re-introduce the undeletable ``vcf-logs``
    shadow. No DDL runs in ``upgrade()``, so there is nothing to undo at
    the schema layer. Same documented-no-op shape as ``0049`` / ``0038``.
    """
    # Intentionally empty -- see docstring. The function stays defined so
    # ``alembic downgrade -1`` resolves the symbol cleanly.
