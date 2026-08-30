# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Widen ``endpoint_descriptor.safety_level`` with the ``destructive`` tier.

Revision ID: 0084
Revises: 0079
Create Date: 2026-08-30

Initiative #3183 (governed delete-shaped operations), Task #3196 — the
foundation task. The ratified decision
(:file:`docs/decisions/governed-delete-operations.md`, requirement 4)
adds a fourth, most-restrictive ``safety_level`` value above
``dangerous``:

    safe < caution < dangerous < destructive

``safety_level`` is a closed enum pinned by the DB CHECK constraint
``ck_endpoint_descriptor_safety_level`` (migration ``0005``:
``safety_level IN ('safe', 'caution', 'dangerous')``). Adding the value
is therefore a migration that widens that CHECK, in lockstep with the
Python mirrors (``SafetyLevel`` alias, ``_VALID_SAFETY_LEVELS`` /
``VALID_SAFETY_LEVELS`` frozensets, and the CHECK literal on the ORM
model) updated in the same PR.

Migration-number note (orchestrator-owned)
------------------------------------------
This revision is numbered ``0084`` with ``down_revision = "0079"`` —
``0079`` is the current head on ``origin/main`` at branch time, so the
chain resolves in isolation and the migration suite stays green. The
numbers ``0080``-``0083`` are reserved for queued PRs (#3201 / #3204 /
#3205 / #3206). If those land first the orchestrator re-points
``down_revision`` to the then-current head (``0083``) at merge; if the
queue shifts it re-points to whatever the real head is. Either way the
final linear chain ends at this migration — pinning to ``0079`` keeps
this branch's ``alembic upgrade head`` correct until that re-point.

Why the closed enum (and the migration shape) matter
----------------------------------------------------
The same reasoning as migration ``0010`` (``graph_edge.kind``): a
portable ``CHECK ... IN (...)`` recreated under
:func:`op.batch_alter_table` drives both PostgreSQL (native
``ALTER TABLE`` DDL) and SQLite (table rebuild — SQLite has no
``ALTER TABLE ... DROP CONSTRAINT``). PostgreSQL ``ENUM`` types would
force an ``ALTER TYPE ADD VALUE`` ceremony SQLite cannot mirror,
breaking the dev/test path. Closed-with-migration-widening is the
chassis pattern.

Reversibility contract
----------------------
``upgrade()`` drops the existing constraint and recreates it over the
wider four-value tuple. Backward compatibility is automatic: every
pre-migration row's ``safety_level`` is in the three-value subset,
which is a strict subset of the new set, so no row violates the widened
constraint and no backfill is needed.

``downgrade()`` narrows the constraint back to the three-value subset.
Narrowing a CHECK is not backward-compatible by itself — any row
written with ``safety_level='destructive'`` between the upgrade and the
downgrade would violate the narrowed constraint. Following ``0010``'s
precedent, ``downgrade()`` explicitly counts ``destructive`` rows and
raises :class:`RuntimeError` with the count before attempting the DDL,
turning an opaque mid-``ALTER TABLE`` failure into a clear
operator-facing message.

Migrations must be **self-contained** — the value tuples are inlined
verbatim rather than imported from :mod:`meho_backplane.db.models`, so
Alembic runs against any historical revision's model graph.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0084"
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT: str = "ck_endpoint_descriptor_safety_level"
_TABLE: str = "endpoint_descriptor"

#: The four-value v0.2.next vocabulary — the three pre-#3183 values plus
#: the ``destructive`` tier. Mirrors the ``SafetyLevel`` alias and the
#: CHECK literal on the ORM model; inlined because migrations must be
#: self-contained.
_SAFETY_LEVELS_V4: tuple[str, ...] = ("safe", "caution", "dangerous", "destructive")

#: The pre-#3183 three-value subset — what ``downgrade()`` narrows back to.
_SAFETY_LEVELS_V3: tuple[str, ...] = ("safe", "caution", "dangerous")

#: The value added by this migration — the set ``downgrade()`` must refuse
#: to orphan.
_ADDED_ONLY: tuple[str, ...] = ("destructive",)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a ``column IN ('a', 'b', ...)`` clause for a CHECK constraint.

    Mirrors the helper in ``0010_widen_graph_edge_kind`` — migrations must
    be self-contained, so the helper is inlined rather than imported.
    """
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Widen ``ck_endpoint_descriptor_safety_level`` to include ``destructive``.

    Drop the existing constraint and recreate it over the wider tuple.
    Every pre-migration row's ``safety_level`` is in the three-value
    subset, a strict subset of the new set, so no row violates the widened
    constraint — backward compatibility is automatic.

    Wrapped in :func:`op.batch_alter_table` for SQLite portability.
    """
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT,
            _check_in("safety_level", _SAFETY_LEVELS_V4),
        )


def downgrade() -> None:
    """Narrow ``ck_endpoint_descriptor_safety_level`` back to three values.

    Refuses (with :class:`RuntimeError`) if any row carries the
    ``destructive`` value added by this migration — narrowing the
    constraint while those rows exist would orphan them on next write and
    surface as an opaque DDL failure. The pre-check turns that into a clear
    "you have N destructive-tier ops; re-classify them first" message
    before any DDL runs.
    """
    endpoint_descriptor = sa.table(
        _TABLE,
        sa.column("safety_level", sa.Text()),
    )
    count_col = sa.func.count().label("n")
    blocking_stmt = (
        sa.select(endpoint_descriptor.c.safety_level, count_col)
        .where(endpoint_descriptor.c.safety_level.in_(_ADDED_ONLY))
        .group_by(endpoint_descriptor.c.safety_level)
    )

    bind = op.get_bind()
    blocking_rows = bind.execute(blocking_stmt).all()

    if blocking_rows:
        total = sum(row.n for row in blocking_rows)
        details = ", ".join(f"{row.safety_level}={row.n}" for row in blocking_rows)
        raise RuntimeError(
            "Cannot downgrade migration 0084: endpoint_descriptor contains "
            f"{total} row(s) with the destructive-tier safety_level ({details}) "
            "that would be orphaned by the narrowed CHECK constraint. "
            "Re-classify them (safety_level -> 'dangerous') before running "
            "`alembic downgrade`."
        )

    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT,
            _check_in("safety_level", _SAFETY_LEVELS_V3),
        )
