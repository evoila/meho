# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Widen ``graph_edge.kind`` to the closed v0.2 ten-kind vocabulary.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-18

Initiative #364 (G9.2 Curated cross-system edges + annotation flow),
Task #593 (T1). Migration ``0007`` shipped the ``ck_graph_edge_kind``
CHECK constraint over the four auto-discoverable kinds G9.1 (#363)
emits from connector probes:

* ``runs-on``, ``mounts``, ``routes-through``, ``belongs-to``

G9.2 widens the closed vocabulary to ten kinds by adding the six
operator-curated cross-system kinds that auto-discovery cannot infer
(decision #6 in :file:`docs/planning/v0.2-decisions.md`):

* ``authenticates-via`` -- principal -> identity-provider node.
* ``depends-on`` -- cross-system functional dependency.
* ``replicates-to`` -- operator-asserted replication.
* ``backed-up-by`` -- operator-asserted backup.
* ``routes-via`` -- operator-asserted network path via an intermediary.
* ``policy-binds`` -- RBAC / policy attachment crossing connectors.

Why the closed enum (and the migration shape) matter
----------------------------------------------------

The v0.2.next policy engine parses ``graph_edge.kind`` to drive rules
like "destructive ops on resources with > 0 ``depends-on`` dependents
require approval". A free-form ``kind`` string fragments that grammar
across tenants. The closed enum + portable ``CHECK kind IN (...)`` is
the same shape ``ck_targets_auth_model`` (migration ``0004``) and
``ck_graph_edge_source`` / ``ck_graph_edge_kind`` (migration ``0007``)
already use -- PostgreSQL ``ENUM`` types would force an
``ALTER TYPE ADD VALUE`` ceremony SQLite cannot mirror, breaking the
dev/test path. Closed-with-migration-widening is the chassis pattern.

The Python type-level mirror :class:`GraphEdgeKind`
(:mod:`meho_backplane.db.models`) is derived from the same vocabulary
so the enum and the CHECK constraint cannot drift; the drift guard in
:mod:`tests.test_topology_schema` asserts the equality at unit-test
time.

Reversibility contract
----------------------

``upgrade()`` drops the existing ``ck_graph_edge_kind`` and recreates
it over the wider ten-kind tuple. Backward compatibility is automatic:
every pre-migration row's ``kind`` is in the four-kind subset, which
is itself a subset of the new ten-kind set, so no row violates the
widened constraint and no backfill / scrub is needed.

``downgrade()`` narrows the constraint back to the four-kind subset.
Narrowing a CHECK is **not** backward-compatible by itself -- any row
written between the upgrade and the downgrade with one of the six
curated-only kinds would silently violate the narrowed constraint on
new writes, but PostgreSQL's ``ALTER TABLE ... ADD CONSTRAINT`` does
validate the *existing* rows: dropping and re-adding the constraint
fails loudly on PG if any curated-only-kind row exists, and SQLite
applies the rebuild-table semantics so a row that violates the new
CHECK aborts the migration.

That last sentence is the contract, but we do better than relying on
the dialect's row-validation pass: ``downgrade()`` explicitly counts
rows whose ``kind`` is in the six-removed set and raises
:class:`RuntimeError` with the row count and the affected kinds before
attempting the DDL. Two reasons:

1. **Clear operator-facing error.** A failing CHECK at the DDL layer
   surfaces as a generic ``IntegrityError`` /
   ``CheckViolation`` on PG, and as an opaque ``Could not run`` on
   SQLite. The pre-check turns "the migration crashed somewhere
   inside ``ALTER TABLE``" into "you have N rows that would be
   orphaned; remove them first or stay on revision 0010". Operators
   following the runbook get a row count and the exact list of
   blocking ``kind`` values -- enough to write the targeted
   ``DELETE FROM graph_edge WHERE kind IN (...)`` (or, more
   conservatively, ``meho topology unannotate``) without touching
   compliant rows.
2. **Dialect-portable.** SQLite's CHECK-validation behaviour under
   constraint redefinition is sensitive to ``PRAGMA legacy_alter_table``;
   PG validates eagerly on ``ADD CONSTRAINT``. Explicit pre-check is
   portable, predictable, and lets the test suite assert the failure
   shape on the same dialect production uses (SQLite for tests, PG for
   prod).

The pre-check is **on the curated-only subset**, not on
``source='curated'`` -- the row-narrowing predicate is "this kind is
not in the post-downgrade vocabulary", regardless of how it was
written. An ``auto``-source ``depends-on`` row (e.g. a connector that
later starts inferring dependencies in v0.2.next) would also block
downgrade; that is the correct semantic, since the row's ``kind`` is
the violation.

Notes
-----

The migration is **self-contained** by design: a migration must never
import the ORM-layer ``_ck_in`` / ``GraphEdgeKind`` from
:mod:`meho_backplane.db.models`, because Alembic must run successfully
against any historical revision's model graph. The local
``_check_in`` helper mirrors the one in ``0007_create_topology_graph``
and the kind tuples are inlined verbatim.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The v0.2 ten-kind vocabulary -- the four auto-discoverable kinds
#: from migration ``0007`` plus the six curated-only kinds added by
#: this migration. Mirrors :class:`GraphEdgeKind` in
#: :mod:`meho_backplane.db.models`; the ORM-layer twin is derived from
#: the enum so the two cannot drift. Inlined here (rather than imported)
#: because migrations must be self-contained.
_EDGE_KINDS_V0_2: tuple[str, ...] = (
    # G9.1 auto-discoverable subset (unchanged from migration 0007).
    "runs-on",
    "mounts",
    "routes-through",
    "belongs-to",
    # G9.2 curated-only additions (Initiative #364).
    "authenticates-via",
    "depends-on",
    "replicates-to",
    "backed-up-by",
    "routes-via",
    "policy-binds",
)

#: The pre-G9.2 auto-discoverable subset -- what ``downgrade()`` narrows
#: the constraint back to.
_EDGE_KINDS_V0_1: tuple[str, ...] = (
    "runs-on",
    "mounts",
    "routes-through",
    "belongs-to",
)

#: The six kinds added by this migration -- the set that
#: ``downgrade()`` must refuse to orphan.
_CURATED_ONLY_KINDS: tuple[str, ...] = (
    "authenticates-via",
    "depends-on",
    "replicates-to",
    "backed-up-by",
    "routes-via",
    "policy-binds",
)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a ``column IN ('a', 'b', ...)`` clause for a CHECK constraint.

    Mirrors the helper in ``0007_create_topology_graph`` -- migrations
    must be self-contained, so the helper is inlined rather than
    imported from the ORM layer.
    """
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Widen ``ck_graph_edge_kind`` from four auto-discoverable kinds to the ten-kind v0.2 set.

    Drop the existing constraint and recreate it over the wider tuple.
    Every pre-migration row's ``kind`` is in the four-kind subset,
    which is a strict subset of the new ten-kind set, so no row
    violates the widened constraint -- backward compatibility is
    automatic.

    Wrapped in :func:`op.batch_alter_table` for SQLite portability:
    SQLite has no ``ALTER TABLE ... DROP CONSTRAINT`` / ``ADD CHECK``
    DDL (sqlite.org/lang_altertable.html), so Alembic batch mode
    rebuilds the table (copy rows -> new schema -> rename) under the
    SQLite dialect. PostgreSQL accepts the equivalent ``ALTER TABLE``
    statements natively; the same code path drives both.
    """
    with op.batch_alter_table("graph_edge") as batch_op:
        batch_op.drop_constraint("ck_graph_edge_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_graph_edge_kind",
            _check_in("kind", _EDGE_KINDS_V0_2),
        )


def downgrade() -> None:
    """Narrow ``ck_graph_edge_kind`` back to the four-kind auto-discoverable subset.

    Refuses (with :class:`RuntimeError`) if any row exists whose
    ``kind`` is in the six curated-only kinds added by this migration
    -- narrowing the constraint while those rows exist would orphan
    them on next write and / or surface as an opaque DDL failure
    halfway through ``ALTER TABLE``. The pre-check turns that into a
    clear "you have N rows with the following kinds; remove them
    first" message before any DDL runs.

    The check uses the curated-only subset (``kind NOT IN (...)`` of
    the post-downgrade vocabulary), not ``source = 'curated'`` -- the
    row-narrowing predicate is "this kind is not in the post-downgrade
    vocabulary", regardless of how the row was written.
    """
    bind = op.get_bind()
    blocking_rows = bind.execute(
        sa.text(
            "SELECT kind, COUNT(*) AS n FROM graph_edge "
            f"WHERE kind IN ({', '.join(f"'{k}'" for k in _CURATED_ONLY_KINDS)}) "
            "GROUP BY kind"
        )
    ).all()

    if blocking_rows:
        total = sum(row.n for row in blocking_rows)
        details = ", ".join(f"{row.kind}={row.n}" for row in blocking_rows)
        raise RuntimeError(
            "Cannot downgrade migration 0010: graph_edge contains "
            f"{total} row(s) with curated-only kind(s) ({details}) "
            "that would be orphaned by the narrowed CHECK constraint. "
            "Remove or re-classify them before running `alembic downgrade`."
        )

    with op.batch_alter_table("graph_edge") as batch_op:
        batch_op.drop_constraint("ck_graph_edge_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_graph_edge_kind",
            _check_in("kind", _EDGE_KINDS_V0_1),
        )
