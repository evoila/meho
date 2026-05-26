# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add lease / heartbeat / in_flight_policy columns to ``agent_run``.

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-26

Initiative #804 (G11.3 Scheduler), Task #825 (T4). T1 (#822, 0020)
landed the ``scheduled_trigger`` table including the
``in_flight_policy`` column on the *trigger*. T4 wires the in-flight
resume-or-fail-into-audit *mechanics*: a per-run lease + heartbeat the
worker holds while it executes, and a reaper that detects expired
leases (the worker died -- pod restart, OOM, network partition) and
applies the policy. The per-run policy is a *snapshot* of the firing
trigger's policy taken at run-start, so a mid-flight definition edit
cannot flip behavior on a run that's already executing.

Columns added to ``agent_run``
------------------------------

* ``lease_owner`` -- Text nullable. The worker process / replica
  identifier holding the lease (e.g. ``"meho-backplane-pod-3:pid-42"``).
  NULL whenever no worker is executing the run (``pending``,
  ``awaiting_approval`` after a release, or any terminal state). The
  lifecycle service keeps this column in lock-step with
  ``lease_expires_at`` -- both set together on claim, both cleared
  together on release.

* ``lease_expires_at`` -- ``timestamptz`` nullable. The wall-clock
  after which the lease is considered abandoned. The healthy worker
  bumps it forward periodically via the lifecycle service's
  ``heartbeat``; as long as heartbeats land the reaper never sees the
  run. The reaper
  (:mod:`meho_backplane.scheduler.reaper`) scans rows with
  ``status='running' AND lease_expires_at < now()`` and applies
  ``in_flight_policy``.

* ``in_flight_policy`` -- Text NOT NULL DEFAULT ``'fail_into_audit'``
  with a portable ``CHECK in_flight_policy IN (...)`` constraint
  enforcing the closed
  :class:`~meho_backplane.db.models.ScheduledTriggerInFlightPolicy`
  vocabulary (``resume`` / ``fail_into_audit``). The default matches
  the consumer doc's explicitly-accepted outcome
  (``agent-runtime-for-ops-spec.md`` §P2): a killed run fails cleanly
  into the audit log and the next trigger tick fires a fresh run.
  ``resume`` is opt-in per agent definition; the scheduler copies the
  policy onto the row at run-start. Existing rows (pre-migration)
  backfill to the default via the server-side DEFAULT so the column
  becomes NOT NULL without a separate backfill UPDATE.

Index added
-----------

* ``agent_run_lease_expires_at_idx`` -- b-tree on
  ``lease_expires_at``. On PG the index is partial
  (``WHERE status = 'running'``) so it stays narrow -- terminal-state
  and ``pending`` rows have ``lease_expires_at IS NULL`` anyway, but
  the partial predicate prunes the index physically and matches the
  reaper's query shape. SQLite ignores ``postgresql_where`` and
  builds a full index; that is fine because SQLite is the dev / test
  path and never sees production-scale data.

Why per-run snapshot, not a join to the trigger
-----------------------------------------------

A run that's already executing must not flip its in-flight behavior
because an operator changed the trigger's policy mid-flight. The
clean way to fix that is to *copy* the policy onto the run row at
run-start (T2 #823 / T3 #824 wire the copy when they fire a run).
Joining back to ``scheduled_trigger.in_flight_policy`` at reap time
would read the *current* value -- which can have changed during the
run, breaking the policy contract. The copy is the substrate that
makes the contract honourable; storing it on the row is the cheapest
way to make every reader see the same answer for the life of the
run.

Dialect portability
-------------------

Mirrors the discipline migrations ``0017`` / ``0020`` follow:

* ``ALTER TABLE ... ADD COLUMN`` -- portable on PG and SQLite (the
  test path); ``server_default=sa.text("'fail_into_audit'")`` on
  ``in_flight_policy`` lets the column flip to NOT NULL without a
  backfill UPDATE on either dialect.
* The ``CHECK (col IN (...))`` constraint compiles identically on PG
  and SQLite -- the same portable enforcement
  :class:`~meho_backplane.db.models.AgentRunStatus` /
  :class:`~meho_backplane.db.models.AgentRunTrigger` use.
* ``op.batch_alter_table`` is required on SQLite (which lacks native
  ``ADD CONSTRAINT``); on PG the batch context is a no-op. Same
  pattern migration ``0024`` (``agent_permission_expires_at``) uses.

Reversibility contract
----------------------

``upgrade()`` adds the three columns + the partial index + the
``CHECK`` constraint; ``downgrade()`` drops them in reverse order
(constraint, index, columns). Explicit drops keep the inverse
symmetric across dialects; the column drop preserves any data the
caller wants to capture in a follow-up migration before reverting.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``agent_run.in_flight_policy`` vocabulary -- kept in lock-step
#: with :class:`meho_backplane.db.models.ScheduledTriggerInFlightPolicy`
#: (the per-run column is a snapshot of the trigger column's
#: vocabulary). Duplicated here as a literal tuple (not imported) so the
#: migration's recorded DDL is a frozen snapshot independent of any
#: later edit to the model enum -- the same self-contained discipline
#: migrations ``0017`` and ``0020`` follow. The drift guard in
#: :mod:`tests.test_db_agent_run` asserts the model enum and the live
#: ``CHECK`` constraint agree.
_AGENT_RUN_IN_FLIGHT_POLICIES: tuple[str, ...] = (
    "resume",
    "fail_into_audit",
)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Add ``lease_owner`` / ``lease_expires_at`` / ``in_flight_policy``."""
    # ``batch_alter_table`` lets SQLite (which lacks ``ALTER TABLE ADD
    # CONSTRAINT``) recreate the table under the hood; PG passes
    # straight through. Same pattern migration ``0024`` follows.
    with op.batch_alter_table("agent_run") as batch:
        batch.add_column(
            sa.Column("lease_owner", sa.Text(), nullable=True),
        )
        batch.add_column(
            sa.Column(
                "lease_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        # ``server_default`` is what lets the column flip to NOT NULL
        # against an existing table without a separate backfill UPDATE:
        # every pre-migration row picks up the default value, and the
        # NOT NULL constraint is satisfied at the same moment the
        # column lands.
        batch.add_column(
            sa.Column(
                "in_flight_policy",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'fail_into_audit'"),
            ),
        )
        batch.create_check_constraint(
            "ck_agent_run_in_flight_policy",
            _check_in("in_flight_policy", _AGENT_RUN_IN_FLIGHT_POLICIES),
        )

    # Partial index on PG (``WHERE status='running'``) -- terminal +
    # ``pending`` rows have ``lease_expires_at IS NULL`` anyway, but
    # the partial predicate prunes the index physically and matches
    # the reaper's query shape. SQLite ignores ``postgresql_where``.
    op.create_index(
        "agent_run_lease_expires_at_idx",
        "agent_run",
        ["lease_expires_at"],
        postgresql_using="btree",
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    """Drop the index, the CHECK constraint, then the columns."""
    op.drop_index("agent_run_lease_expires_at_idx", table_name="agent_run")
    with op.batch_alter_table("agent_run") as batch:
        batch.drop_constraint("ck_agent_run_in_flight_policy", type_="check")
        batch.drop_column("in_flight_policy")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_owner")
