# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Seed the global ``vmware`` doc-collection manifest (description / when_to_use).

Revision ID: 0048
Revises: 0047
Create Date: 2026-06-19

Initiative #1912 (G2.x corpus grounded-answer pipeline), Task #1920. This
is a **data migration** -- no schema changes -- that fills the
agent-facing manifest prose on the *global* ``vmware`` ``doc_collections``
row so the corpus-aware query-expansion step (#1916,
:func:`~meho_backplane.docs_search.expansion.expand_docs_query`) has
something to read.

Why this migration exists
-------------------------

``description`` / ``when_to_use`` are **nullable** on ``doc_collections``
(migration ``0037``) and **optional at create** (#1739,
``docs_collections/service.py``). #1916's
:func:`~meho_backplane.docs_search.expansion._render_manifest_for_prompt`
injects the collection's ``vendor`` / ``products`` / ``description`` /
``when_to_use`` into the expansion prompt, but **omits empty optional
fields** -- so a ``vmware`` row registered without prose contributes only
``collection`` / ``vendor`` / ``products`` lines, and the model expands on
a thin manifest. This migration writes the hand-authored
``description`` + ``when_to_use`` (and a canonical ``vendor`` / ``products``
when they are still unset) so the expansion is grounded in the corpus's own
domain terms (acronyms, product synonyms).

Substrate stays dumb (#1177): the prose is **hand-authored**, not
auto-summarised from a chunk sample at ingest (explicitly out of scope per
#1920). This migration only puts existing-shape data in front of the model.

Fix shape -- fill-only UPDATE on the existing global row
--------------------------------------------------------

The same self-contained data-migration shape as ``0046`` (the vRLI
target-product reconcile) and ``0011`` (the ``when_to_use`` backfill): a
lightweight :func:`sa.table` / :func:`sa.column` shim mirrors only the
columns this migration touches, **no ORM import** (importing the live
models would pin the migration to one moment in the schema's history and
break replay), and the statement executes synchronously via
``op.get_bind()``.

The write is an **UPDATE, never an INSERT**, narrowed to the **global**
(``tenant_id IS NULL``) ``vmware`` row, and it is **fill-only** -- each
field is written only where it is currently empty. The rationale for each
of those three properties:

* **UPDATE not INSERT.** ``backend`` is NOT NULL with no default (migration
  ``0037``): every row must bind to exactly one ``{type, ref}`` routing
  record, and that record is **deploy-specific** (the corpus endpoint /
  RAG corpus path), not something a migration can author. The global
  ``vmware`` row is provisioned **out-of-band** by the operator DB seed
  (``docs/cross-repo/meho-docs-addon.md`` -- the create API only ever
  scopes a row to the caller's tenant, so the shared row is never created
  through it). This migration therefore *enriches* that operator-seeded
  row rather than inventing one. On a deploy where the ``vmware`` row does
  not exist yet, the UPDATE matches **zero rows** -- a clean no-op, which
  is the correct behaviour (a corpus that is not registered has no manifest
  to fill).

* **Global scope only.** ``tenant_id IS NULL`` is the shared/global row
  every tenant sees via the resolver's global fallback. A tenant-curated
  ``vmware`` row is the operator's own content and is deliberately left
  untouched -- this seed owns only the shared corpus's manifest.

* **Fill-only (never clobber).** ``description`` / ``when_to_use`` /
  ``products`` are written only where the row's current value is empty
  (NULL / ``''`` for the prose, an empty array for ``products``), so an
  operator who already authored a manifest keeps it -- the
  operator-content-wins discipline migrations ``0018`` / ``0028`` follow
  for seeded conventions. ``vendor`` is NOT NULL (always carries a value),
  so the only canonicalisation is upgrading the bare token ``vmware`` (a
  common seed shorthand) to the catalogue display string
  ``VMware by Broadcom``; an operator-chosen vendor string is left as-is.

Idempotency
-----------

Re-running ``upgrade()`` is a no-op for an already-seeded row: the
fill-only predicates (``description IS NULL OR description = ''`` etc.) no
longer match once the prose is present, so no row is rewritten and
``updated_at`` is not bumped a second time. This matters for the
upgrade -> downgrade -> upgrade replay the test suite exercises and the
testcontainers PG replay cycle.

Reversibility contract
----------------------

``downgrade()`` clears the prose **only where it still equals the exact
text this migration authored** -- an operator edit made after the seed ran
survives the rollback (the narrow-reversal discipline migrations ``0011`` /
``0018`` follow: rewind only what this migration wrote). ``products`` and
the ``vendor`` canonicalisation are intentionally **not** reverted: the
empty-array / bare-token states they replaced carry no information an image
revert needs, and re-deriving "was this array operator-authored or
seed-authored?" on downgrade is ambiguous, so the safer inverse leaves the
richer values in place.

Cross-references
----------------

* Task #1920 / Initiative #1912 -- this manifest seed.
* :func:`~meho_backplane.docs_search.expansion.expand_docs_query` (#1916)
  -- the corpus-aware expansion step that reads the manifest fields.
* Migration ``0037`` -- created ``doc_collections`` (the nullable
  ``description`` / ``when_to_use`` columns this migration fills).
* Migration ``0046`` / ``0011`` -- the self-contained fill/reconcile
  data-migration precedents this file mirrors.
* ``docs/cross-repo/meho-docs-addon.md`` -- documents the global row as an
  out-of-band operator seed and the canonical ``vmware`` field values.
* :mod:`tests.test_migration_0048_seed_vmware_collection_manifest` -- the
  behavioural contract: fill-empty, never-clobber, no-row no-op,
  idempotency, narrow downgrade, and the manifest-reaches-the-prompt check.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The collection this seed targets. The global (``tenant_id IS NULL``) row
#: only -- the shared corpus every tenant sees.
_COLLECTION_KEY: Final[str] = "vmware"

#: Bare seed-shorthand vendor token upgraded to the catalogue display
#: string. An operator-chosen vendor string is left untouched.
_VENDOR_PLACEHOLDER: Final[str] = "vmware"
_VENDOR_CANONICAL: Final[str] = "VMware by Broadcom"

#: The hand-authored manifest prose (#1920). ``description`` says what the
#: corpus contains; ``when_to_use`` is the agent-facing "pick this
#: collection when..." blurb #1916's expansion prompt and
#: ``list_doc_collections`` read verbatim.
_DESCRIPTION: Final[str] = (
    "VMware vSphere, VCF, and NSX product documentation, Broadcom KB "
    "articles, and curated community posts covering vSphere, vCenter, "
    "ESXi, NSX, vSAN, and the Aria/vRealize suite."
)
_WHEN_TO_USE: Final[str] = (
    "VMware / Broadcom infrastructure questions -- vSphere, vCenter, "
    "ESXi, VCF, NSX, vSAN, and Aria/vRealize (vROps, vRLI)."
)

#: The products the corpus covers, written only when the row's ``products``
#: is still empty. Tokens align with the connector-registry product
#: vocabulary (``vsphere`` / ``nsx`` / ``vrops`` / ``vrli``) so the
#: catalogue's ``--vendor`` / products view reads consistently.
_PRODUCTS: Final[list[str]] = ["vsphere", "vcf", "nsx", "vsan", "vrops", "vrli"]


def _doc_collections_shim() -> sa.Table:
    """Return a minimal :func:`sa.table` shim for the columns we touch.

    ``products`` reuses the dialect-portable column type migration ``0037``
    declared -- native ``TEXT[]`` on PostgreSQL, JSON-text array on SQLite
    -- so passing a Python ``list[str]`` value binds correctly on both
    engines (SQLAlchemy's bind processor JSON-encodes for SQLite and
    array-encodes for PG).
    """
    products_type = sa.JSON().with_variant(postgresql.ARRAY(sa.Text()), "postgresql")
    return sa.table(
        "doc_collections",
        sa.column("collection_key", sa.Text()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("vendor", sa.Text()),
        sa.column("products", products_type),
        sa.column("description", sa.Text()),
        sa.column("when_to_use", sa.Text()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def upgrade() -> None:
    """Fill the global ``vmware`` row's manifest where it is still empty.

    Four scoped UPDATEs, each on the global ``vmware`` row and each
    fill-only so an operator-authored value is never overwritten:

    1. ``description`` -- where NULL / ``''``.
    2. ``when_to_use`` -- where NULL / ``''``.
    3. ``products`` -- where the array is empty (NULL / ``[]``).
    4. ``vendor`` -- only the bare ``vmware`` placeholder -> the display
       string.

    Each write bumps ``updated_at`` (single ``now`` per run, the discipline
    migrations ``0011`` / ``0046`` follow) so operator tooling driven off
    the column sees the change. A deploy without a global ``vmware`` row
    matches zero rows on every statement -- a clean no-op.
    """
    table = _doc_collections_shim()
    bind = op.get_bind()
    now = datetime.now(UTC)
    global_vmware = sa.and_(
        table.c.collection_key == _COLLECTION_KEY,
        table.c.tenant_id.is_(None),
    )

    bind.execute(
        sa.update(table)
        .where(
            global_vmware,
            sa.or_(table.c.description.is_(None), table.c.description == ""),
        )
        .values(description=_DESCRIPTION, updated_at=now),
    )
    bind.execute(
        sa.update(table)
        .where(
            global_vmware,
            sa.or_(table.c.when_to_use.is_(None), table.c.when_to_use == ""),
        )
        .values(when_to_use=_WHEN_TO_USE, updated_at=now),
    )
    # ``products`` empty-array check: an empty TEXT[] on PG and an empty
    # JSON array on SQLite both round-trip through the ORM as ``[]``; a
    # never-set column is NULL. Narrowing on "no products listed" leaves an
    # operator-authored list intact.
    empty_products = sa.literal([], type_=table.c.products.type)
    bind.execute(
        sa.update(table)
        .where(
            global_vmware,
            sa.or_(table.c.products.is_(None), table.c.products == empty_products),
        )
        .values(products=_PRODUCTS, updated_at=now),
    )
    bind.execute(
        sa.update(table)
        .where(global_vmware, table.c.vendor == _VENDOR_PLACEHOLDER)
        .values(vendor=_VENDOR_CANONICAL, updated_at=now),
    )


def downgrade() -> None:
    """Clear only the prose this migration authored; keep operator edits.

    The narrow-reversal inverse: ``description`` / ``when_to_use`` are reset
    to NULL **only where they still hold the exact seeded text**, so an
    operator who rewrote the manifest after the seed ran keeps their
    content. ``products`` and the ``vendor`` canonicalisation are not
    reverted -- the empty-array / bare-token states they replaced carry no
    information an image revert needs (see the module docstring's
    "Reversibility contract").
    """
    table = _doc_collections_shim()
    bind = op.get_bind()
    now = datetime.now(UTC)
    global_vmware = sa.and_(
        table.c.collection_key == _COLLECTION_KEY,
        table.c.tenant_id.is_(None),
    )

    bind.execute(
        sa.update(table)
        .where(global_vmware, table.c.description == _DESCRIPTION)
        .values(description=None, updated_at=now),
    )
    bind.execute(
        sa.update(table)
        .where(global_vmware, table.c.when_to_use == _WHEN_TO_USE)
        .values(when_to_use=None, updated_at=now),
    )
