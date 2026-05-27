# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add a GIN index on ``documents.metadata`` for JSONB containment.

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-27

Substrate-side migration for Task #1177 (G4.4-T1) under Initiative
#1178: light the deferred ``metadata_filters`` parameter on
:func:`~meho_backplane.retrieval.retriever.retrieve` and keep its
``documents.metadata @> :filters_jsonb`` containment lookups
index-backed as corpora grow.

Why a default ``jsonb_ops`` GIN opclass
---------------------------------------

PG ships two GIN opclasses for JSONB columns:

* ``jsonb_ops`` (default) — indexes every key and every leaf value
  in the document. Supports ``@>`` (containment), ``?`` (key
  existence), ``?&`` / ``?|`` (multi-key existence), and the
  ``@?`` / ``@@`` path-query operators. Larger on-disk footprint
  than ``jsonb_path_ops``, but every retrieval surface the
  substrate may grow into stays index-backed without re-indexing.
* ``jsonb_path_ops`` — indexes path-and-value tokens only.
  Supports ``@>`` exclusively, with a smaller index and faster
  build / probe. Forecloses the v0.2.next surfaces the substrate
  has on its roadmap (key-existence filters for "any row with
  ``expires_at``", for example, which the memory recall path
  already wants per #1179 once it lights up the push-down).

``metadata_filters`` lands today as ``@>``-only, but the broader
substrate doctrine (per the Initiative body) is that the JSONB
column is the shared free-form metadata surface every read path
may grow predicates against. Pinning the index to the operator
class that supports the widest set of operators keeps the
forward-compat door open at the cost of a few percent on the
index size — a trade the v0.2 corpus scale (single-tenant,
hundreds to low thousands of documents) is not in tension with.

When a future profiling pass surfaces a measured ``@>``-only
workload whose latency is dominated by index-probe cost, swapping
to ``jsonb_path_ops`` is an additive migration; downgrading
``jsonb_path_ops`` → ``jsonb_ops`` to recover the lost operator
classes is the painful direction. Default-opclass-now keeps the
substrate's options open.

The Task body and the parent Initiative explicitly call for the
default ``jsonb_ops`` opclass; this migration honours that
contract directly rather than introspecting opclass tradeoffs at
build time.

PG-only and CONCURRENTLY-not
----------------------------

The ``vector`` extension and the ``USING gin`` index syntax are
PostgreSQL-only; the migration short-circuits on SQLite (the
dev/test driver) so existing tests against the SQLite engine
remain green. The substrate's behavioural contract against SQLite
was already undefined for the BM25 / cosine operators (see
``retriever.py`` module docstring on the SQLite path); adding a
PG-only index does not introduce a new asymmetry — the dev/test
driver has no ``@>`` cost to worry about because aiosqlite never
sees this SQL outside an integration-test run that uses
testcontainer-pg.

Index creation does **not** use ``CREATE INDEX CONCURRENTLY``.
Alembic does not support concurrent index creation cleanly inside
a single transaction (every migration runs in a transaction by
default; ``CONCURRENTLY`` requires running outside one). Building
this index against a fresh table is fast even at low-thousands
corpus scale; if a future operator hits a hot-DB rebuild scenario
they can drop + re-create the index manually with
``CONCURRENTLY``. Documenting that here so a downstream operator
encountering migration latency on a production rebuild can read
back the rationale.

Reversibility
-------------

``downgrade()`` drops the index with ``IF EXISTS`` so the rollback
is idempotent against an environment that already lost the index
out-of-band. No data is touched; the substrate falls back to
seq-scan on ``@>`` containment, which is acceptable at v0.2
corpus scale and undoes the additive contract of the upgrade
cleanly.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Name of the index. Held as a constant so :func:`upgrade` and
#: :func:`downgrade` agree without a string-literal mismatch the
#: reversibility CI guard would otherwise have to lint. The
#: ``documents_metadata_gin_idx`` shape matches the existing
#: ``documents_body_fts_idx`` / ``documents_embedding_idx`` /
#: ``documents_body_hash_idx`` naming convention from migration
#: ``0003`` (``<table>_<column>_<index-kind>_idx``).
_INDEX_NAME: str = "documents_metadata_gin_idx"


def upgrade() -> None:
    """Create the GIN index on ``documents.metadata`` (PG only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # ``jsonb_ops`` is the default opclass for the JSONB GIN index;
    # spelled out here to make the index definition self-documenting
    # against a future operator who runs ``\\d documents`` and wants
    # to know which opclass the index was built against. The
    # ``IF NOT EXISTS`` guard makes the migration idempotent against
    # an environment that already built this index out-of-band
    # (test harness, manual hot-rebuild). Alembic does not surface
    # ``if_not_exists`` on :func:`op.create_index` for the
    # expression-shaped CREATE INDEX we need here, so the raw
    # ``op.execute`` mirrors the pattern migration ``0003`` used for
    # the BM25 GIN + IVFFlat indexes.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX_NAME} ON documents USING GIN (metadata jsonb_ops)"
    )


def downgrade() -> None:
    """Drop the GIN index (PG only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # ``IF EXISTS`` so a downgrade against an environment that lost
    # the index out-of-band still runs cleanly.
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
