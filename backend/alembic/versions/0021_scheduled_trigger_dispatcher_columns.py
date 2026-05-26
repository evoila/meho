# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Extend ``scheduled_trigger`` with the dispatcher columns + ``fired`` status.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-25

Initiative #804 (G11.3 Scheduler), Task #823 (T2 -- cron + one-off
dispatcher loop). Migration ``0020`` (T1, PR #1064) settled the storage
substrate but left three columns the dispatcher loop owns to a follow-up
migration: ``identity_sub`` (the identity the scheduler impersonates),
``inputs`` (the JSON payload forwarded to the agent run), and ``timezone``
(the IANA zone cron expressions evaluate in). It also locked the
``status`` enum to ``active|paused|cancelled``; the one-off finalisation
path needs a fourth terminal value -- ``fired`` -- so a single-shot
trigger that has already dispatched is distinguishable from one an
operator cancelled.

What this migration adds
------------------------

* ``timezone`` -- ``TEXT NOT NULL`` with server default ``'UTC'``. The
  default backfills the rows shipped by 0020 (which by definition do
  not yet exist outside dev/test); fresh inserts from T2 onward set the
  column explicitly.
* ``identity_sub`` -- ``TEXT NOT NULL`` with server default
  ``'__scheduler__'``. The sentinel keeps the NOT NULL constraint
  satisfiable for 0020 rows; production triggers (T5 admin surface,
  T2 test seams) populate the column explicitly.
* ``inputs`` -- nullable ``JSONB`` on PostgreSQL, generic ``JSON`` on
  SQLite -- the same portable variant
  :attr:`AgentDefinition.toolset` / :attr:`Document.doc_metadata`
  use. ``none_as_null=True`` keeps SQL NULL distinct from the JSON
  literal ``'null'`` (load-bearing on the ORM side; see model docstring).
* The ``ck_scheduled_trigger_status`` ``CHECK`` constraint is dropped
  and re-created with the widened vocabulary
  ``('active', 'paused', 'cancelled', 'fired')``. The drop+create
  cadence is the only portable way to widen a ``CHECK IN (...)``
  predicate across PG and SQLite -- neither dialect supports
  ``ALTER CONSTRAINT ... CHECK``.

Why split T1 and T2's columns across two migrations
---------------------------------------------------

T1 (#1064) shipped the storage shape that survives without a
dispatcher -- the admin surface can already write rows that satisfy the
discriminated-union CHECK, even if nothing reads them yet. T2 (#1065)
adds the columns the dispatcher itself requires; bundling them into
0020 would have coupled the T1 PR to the T2 design choices (per-trigger
TZ vs. global UTC; JSON inputs vs. positional args; impersonation sub
sourcing). Keeping the migrations sequential follows the same
discipline the agent-runtime substrate used -- ``0016`` shipped
``agent_definition``, ``0017`` tightened the ``agent_run`` FK + status
enum once the dispatcher's contract was fixed.

Reversibility contract
----------------------

``downgrade()`` removes the three columns and restores the original
``ck_scheduled_trigger_status`` body (drop the widened CHECK, recreate
the 0020 CHECK). The drop order is the inverse of the upgrade so the
two halves are symmetric.

Note on SQLite + ``CHECK`` constraints
--------------------------------------

SQLite's ``ALTER TABLE DROP CONSTRAINT`` was added in 3.39 (2022). The
hosted CI runners and contributor machines run SQLite >= 3.40 (Python
3.13 ships 3.45+); local validation happens via ``pytest`` on each
PR. Should an environment fall behind, the ``batch_alter_table``
context manager in this migration falls back to the table-recreate
cookbook automatically, the same way 0017 widens the
``ck_agent_run_status`` predicate. PostgreSQL gets the plain
``DROP CONSTRAINT`` / ``ADD CONSTRAINT`` path.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Widened ``status`` vocabulary -- the 0020 set plus ``'fired'``.
#: Kept in lock-step with
#: :class:`meho_backplane.db.models.ScheduledTriggerStatus`; the drift
#: guard in :mod:`tests.test_db_scheduled_trigger` enforces equality.
_SCHEDULED_TRIGGER_STATUSES_V2: tuple[str, ...] = (
    "active",
    "paused",
    "cancelled",
    "fired",
)

#: Original ``status`` vocabulary frozen in 0020 -- restored on
#: downgrade. The literal tuple is independent of any model edit so the
#: reversal target is the schema 0020 actually shipped.
_SCHEDULED_TRIGGER_STATUSES_V1: tuple[str, ...] = (
    "active",
    "paused",
    "cancelled",
)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Add the three columns + widen the status CHECK to include ``fired``."""
    inputs_type = sa.JSON(none_as_null=True).with_variant(
        postgresql.JSONB(none_as_null=True), "postgresql"
    )

    # Columns first. server_default keeps the NOT NULL backfill safe
    # against any 0020-era row -- the dev/test fixtures replay
    # migrations on a clean DB, but the discipline matches what a
    # production upgrade would need.
    op.add_column(
        "scheduled_trigger",
        sa.Column(
            "timezone",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'UTC'"),
        ),
    )
    op.add_column(
        "scheduled_trigger",
        sa.Column(
            "identity_sub",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'__scheduler__'"),
        ),
    )
    op.add_column(
        "scheduled_trigger",
        sa.Column(
            "inputs",
            inputs_type,
            nullable=True,
        ),
    )

    # Widen the status CHECK. ``batch_alter_table`` is the portable
    # cookbook for "drop + add a CHECK" -- on PG it issues plain
    # ALTER TABLE DROP/ADD CONSTRAINT, on SQLite it falls back to the
    # table-recreate path when needed. The same shape migration 0017
    # used to widen ``ck_agent_run_status``.
    with op.batch_alter_table("scheduled_trigger") as batch_op:
        batch_op.drop_constraint(
            "ck_scheduled_trigger_status",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_scheduled_trigger_status",
            _check_in("status", _SCHEDULED_TRIGGER_STATUSES_V2),
        )


def downgrade() -> None:
    """Restore the 0020 status CHECK and drop the three columns.

    Refuses to land if rows with ``status='fired'`` exist -- they would
    be orphaned by the narrowed v1 CHECK and either fail the
    ``ADD CONSTRAINT`` validation (PG) or trip the table-recreate
    integrity check (SQLite batch_alter_table). The refusal raises a
    :class:`RuntimeError` with operator-actionable instructions rather
    than silently corrupting; the same shape migration 0010
    (``ck_graph_edge_kind`` narrowing) follows. Failing fast before
    touching any DDL is intentional -- a refused downgrade leaves the
    schema unchanged at 0021 rather than half-applied.
    """
    # Block before touching DDL: count one-off rows that the v1 CHECK
    # would orphan. The narrow IN-predicate keeps the count cheap even
    # on a large table; the operator response is a deliberate cancel /
    # re-classify pass before retrying the downgrade.
    scheduled_trigger = sa.table(
        "scheduled_trigger",
        sa.column("status", sa.Text()),
    )
    blocking_stmt = (
        sa.select(sa.func.count().label("n"))
        .select_from(scheduled_trigger)
        .where(scheduled_trigger.c.status == "fired")
    )
    bind = op.get_bind()
    blocking_count: int = bind.execute(blocking_stmt).scalar_one()
    if blocking_count:
        raise RuntimeError(
            f"Cannot downgrade migration 0021: scheduled_trigger contains "
            f"{blocking_count} row(s) with status='fired' that would be "
            "orphaned by the narrowed v1 CHECK constraint. Cancel or "
            "re-classify them ("
            "UPDATE scheduled_trigger SET status='cancelled' "
            "WHERE status='fired'"
            ") before running `alembic downgrade`."
        )

    with op.batch_alter_table("scheduled_trigger") as batch_op:
        batch_op.drop_constraint(
            "ck_scheduled_trigger_status",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_scheduled_trigger_status",
            _check_in("status", _SCHEDULED_TRIGGER_STATUSES_V1),
        )

    op.drop_column("scheduled_trigger", "inputs")
    op.drop_column("scheduled_trigger", "identity_sub")
    op.drop_column("scheduled_trigger", "timezone")
