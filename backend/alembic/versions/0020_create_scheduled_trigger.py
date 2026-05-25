# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``scheduled_trigger`` table for the G11.3 scheduler substrate.

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-25

This migration is the schema substrate of Task #822 (G11.3-T1) under
Initiative #804 (the P2 scheduler). It adds the ``scheduled_trigger``
table -- one row per durable trigger that fires a G11.1 agent run. T1
settles the durability-substrate fork (Option A: extend the existing
roll-our-own ``asyncio`` + ``pg_try_advisory_lock`` pattern; see the
PR body for the full A-vs-B rationale) and lands the storage shape the
remaining G11.3 tasks (#823 cron loop, #824 event outbox, #825
in-flight policy, #826 admin surface) build on.

What this migration adds
------------------------

* The ``scheduled_trigger`` table -- one row per durable, tenant-scoped
  agent trigger.
* Two indexes:
  * ``scheduled_trigger_next_fire_at_idx`` -- b-tree on
    ``(status, next_fire_at)`` drives the dispatcher's "what fires
    next" claim query (T2 / T3); a partial index on PG limits it to
    ``status = 'active'`` so the index only carries the rows the
    dispatcher actually scans.
  * ``scheduled_trigger_tenant_idx`` -- b-tree on
    ``(tenant_id, kind)`` drives the admin surface's tenant-scoped
    list (T5).

Discriminated fields by ``kind``
--------------------------------

A single table stores all three trigger shapes because the read-side
dispatcher (T2/T3) scans them with the same "claim the next due row"
query; splitting them into three tables would force three scanners
with three locks. The shape is the discriminated-union pattern: the
``kind`` column picks which type-specific column (``cron_expr`` for
cron, ``event_filter`` for event; none for one_off) carries the
trigger's semantics, with ``next_fire_at`` as the universal schedule
signal the dispatcher claims on. A DB-side ``CHECK`` constraint
(``ck_scheduled_trigger_kind_fields``) enforces the invariant at the
substrate boundary.

* ``kind = 'cron'``   -> ``cron_expr`` populated, ``event_filter``
  NULL. ``next_fire_at`` is materialised by the dispatcher.
* ``kind = 'one_off'`` -> neither ``cron_expr`` nor ``event_filter``
  populated; ``next_fire_at`` is NOT NULL (set at insert as the only
  schedule signal). After firing, ``status`` transitions to ``fired``.
* ``kind = 'event'``  -> ``event_filter`` populated, ``cron_expr``
  NULL.

The vocabulary is closed (``CHECK kind IN (...)``); widening it is a
coordinated DB + model change (new migration, new ``KindCheck``
literals, new enum member) so the constraint and the
:class:`~meho_backplane.db.models.ScheduledTriggerKind` cannot drift.
The same closed-enum discipline ``agent_run.status`` (migration
``0017``) follows.

``status`` and ``in_flight_policy`` are also closed enums with their
own ``CHECK`` bodies (``ck_scheduled_trigger_status`` /
``ck_scheduled_trigger_in_flight_policy``).

Why real FKs to ``tenant`` and ``agent_definition``
---------------------------------------------------

``tenant_id`` -- identical rationale to ``agent_run.tenant_id``
(migration ``0017``) and ``agent_definition.tenant_id`` (``0016``):
clean-slate substrate, no chassis-era rows, enforced at the DB layer
so an orphan trigger for a typo'd / replayed tenant surfaces as
:class:`IntegrityError` at insert.

``agent_definition_id`` -- the FK is enforceable in *this* migration
because the parent table already exists at HEAD (``0016`` shipped
ahead of this work). The sibling :class:`AgentRun` (``0017``) had to
use a soft-FK because ``agent_definition`` was landing in parallel;
that constraint does not apply here, so the FK is tightened. A
trigger cannot point at a deleted definition.

No ``ondelete`` clauses: deleting a tenant or a definition with live
triggers is a major operation that must cancel the triggers first;
the default ``NO ACTION`` blocks the cascade -- the same discipline
``agent_run`` follows.

Dialect-portability decisions
-----------------------------

Mirrors the discipline migrations 0016 / 0017 established:

* ``id`` server default -- PG gets ``gen_random_uuid()``; SQLite
  leaves the column to the ORM ``default=uuid.uuid4``.
* ``created_at`` / ``updated_at`` server defaults -- PG gets
  ``now()``; SQLite leaves it to the ORM ``default=lambda:
  datetime.now(UTC)``. The ORM also declares
  ``onupdate=lambda: datetime.now(UTC)`` on ``updated_at`` so ORM-side
  edits bump the timestamp.
* ``status`` / ``in_flight_policy`` -- portable string literal
  defaults on both dialects.
* ``event_filter`` -- portable :class:`JSON` -> :class:`JSONB` variant
  (the same pattern :attr:`Document.doc_metadata` /
  :attr:`AgentDefinition.toolset` use). Nullable on both dialects.
* ``CHECK (col IN (...))`` constraints compile identically on PG and
  SQLite -- the same portable enforcement migrations ``0007`` /
  ``0017`` use.
* ``scheduled_trigger_next_fire_at_idx`` is a *partial* index on PG
  (``WHERE status = 'active'``); SQLite gets a plain b-tree because
  partial indexes have a divergent ``WHERE`` grammar across older
  SQLite builds and the test path is single-replica so the partial
  win is not load-bearing there.

Reversibility contract
----------------------

``downgrade()`` drops the indexes then the table in inverse order.
The indexes are dropped explicitly so the migration is reversible
cleanly on SQLite (which does not always cascade index drops on
``DROP TABLE``) as well as PostgreSQL -- the same discipline
migrations ``0007`` / ``0012`` / ``0017`` follow.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``scheduled_trigger.kind`` vocabulary -- kept in lock-step
#: with :class:`meho_backplane.db.models.ScheduledTriggerKind`.
#: Duplicated here as a literal tuple (not imported) so the migration's
#: recorded DDL is a frozen snapshot independent of any later edit to
#: the model enum -- the same self-contained discipline migration
#: ``0017`` follows. The drift guard in :mod:`tests.test_db_scheduled_trigger`
#: asserts the model enum and the live ``CHECK`` constraint agree.
_SCHEDULED_TRIGGER_KINDS: tuple[str, ...] = (
    "cron",
    "one_off",
    "event",
)

#: Closed ``scheduled_trigger.status`` vocabulary -- lock-step with
#: :class:`meho_backplane.db.models.ScheduledTriggerStatus`. ``fired``
#: is the terminal state for one-off triggers after the dispatcher
#: fires them (T2 #823); cron / event triggers never enter it
#: (cron re-arms by recomputing ``next_fire_at`` after each fire,
#: event re-arms by remaining ``active`` against the outbox stream).
_SCHEDULED_TRIGGER_STATUSES: tuple[str, ...] = (
    "active",
    "paused",
    "cancelled",
    "fired",
)

#: Closed ``scheduled_trigger.in_flight_policy`` vocabulary -- lock-step
#: with :class:`meho_backplane.db.models.ScheduledTriggerInFlightPolicy`.
#: T4 #825 wires the policy mechanics; this Task only stores the field.
_SCHEDULED_TRIGGER_IN_FLIGHT_POLICIES: tuple[str, ...] = (
    "resume",
    "fail_into_audit",
)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


#: Discriminated-union invariant: exactly one of the two type-specific
#: columns (``cron_expr`` for cron, ``event_filter`` for event) carries
#: the trigger's semantics. One-offs are distinguished by
#: ``kind = 'one_off'`` + both type columns NULL + a NOT NULL
#: ``next_fire_at`` (which cron / event rows may also populate after
#: the dispatcher's first compute-next pass). The
#: ``(kind = '...' AND col IS NOT NULL AND other_col IS NULL)`` form is
#: portable across PG and SQLite -- no dialect-specific syntax.
_KIND_FIELDS_CHECK: str = (
    "("
    "(kind = 'cron' AND cron_expr IS NOT NULL AND event_filter IS NULL) OR "
    "(kind = 'one_off' AND cron_expr IS NULL AND event_filter IS NULL "
    "AND next_fire_at IS NOT NULL) OR "
    "(kind = 'event' AND event_filter IS NOT NULL AND cron_expr IS NULL)"
    ")"
)


def upgrade() -> None:
    """Create the ``scheduled_trigger`` table + its indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Portable JSONB -> JSON variant with ``none_as_null=True`` so a
    # Python ``None`` round-trips through the ORM as SQL NULL rather
    # than the JSON literal ``'null'``. Load-bearing: the discriminated-
    # union ``ck_scheduled_trigger_kind_fields`` CHECK predicates on
    # ``event_filter IS NULL`` for the cron / one_off kinds; without
    # ``none_as_null`` a defaulted ``event_filter`` would store the
    # JSON literal ``'null'`` (non-NULL at SQL layer) and the CHECK
    # would fire. Same kwarg applies to ``inputs``.
    portable_json = sa.JSON(none_as_null=True).with_variant(
        postgresql.JSONB(none_as_null=True), "postgresql"
    )

    op.create_table(
        "scheduled_trigger",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # Real REFERENCES tenant(id) FK -- see module docstring.
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        # Real REFERENCES agent_definition(id) FK -- the parent table is
        # already at HEAD via 0016, so this Task can tighten the FK that
        # 0017 had to leave as soft.
        sa.Column(
            "agent_definition_id",
            sa.Uuid(),
            sa.ForeignKey("agent_definition.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        # Discriminated by ``kind`` -- see _KIND_FIELDS_CHECK below.
        # cron rows populate ``cron_expr``; event rows populate
        # ``event_filter``; one_off rows populate neither (and set
        # ``next_fire_at`` at insert time as the only schedule signal).
        sa.Column("cron_expr", sa.Text(), nullable=True),
        # IANA tz name -- only cron actually reads it but the schema
        # is uniform across kinds. Default ``'UTC'`` so unspecified
        # cron rows fire on UTC wall-clock.
        sa.Column(
            "timezone",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'UTC'"),
        ),
        sa.Column("event_filter", portable_json, nullable=True),
        # Agent run payload/prompt the dispatcher passes into the
        # spawned agent. NULL when the trigger relies entirely on the
        # underlying agent_definition's defaults.
        sa.Column("inputs", portable_json, nullable=True),
        # JWT ``sub`` the spawned run executes as -- dispatch identity.
        # Required so T2/T3 can mint per-fire tokens at run time.
        sa.Column("identity_sub", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        # T4 #825 owns the policy mechanics; this Task only stores it.
        sa.Column(
            "in_flight_policy",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'fail_into_audit'"),
        ),
        # Materialised next-fire timestamp the dispatcher claims on
        # (T2/T3). NULL on a newly-created trigger before the scheduler
        # computes the first fire; populated by T2's "compute next" pass.
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        # Set by the dispatcher (T2/T3) after a successful fire.
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        # JWT ``sub`` of the tenant-admin who created the trigger -- no
        # operator table exists in this chassis; the ``sub`` is the
        # stable identifier (same precedent as
        # :attr:`AgentDefinition.created_by_sub` and
        # :attr:`BroadcastOverride.created_by_sub`).
        sa.Column("created_by_sub", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        # Closed enums -- portable IN(...) CHECKs. Widening any of these
        # is a coordinated migration + model change so the enum and the
        # constraint move in lock-step (drift guards live in
        # tests.test_db_scheduled_trigger).
        sa.CheckConstraint(
            _check_in("kind", _SCHEDULED_TRIGGER_KINDS),
            name="ck_scheduled_trigger_kind",
        ),
        sa.CheckConstraint(
            _check_in("status", _SCHEDULED_TRIGGER_STATUSES),
            name="ck_scheduled_trigger_status",
        ),
        sa.CheckConstraint(
            _check_in("in_flight_policy", _SCHEDULED_TRIGGER_IN_FLIGHT_POLICIES),
            name="ck_scheduled_trigger_in_flight_policy",
        ),
        # Discriminated-union invariant: see _KIND_FIELDS_CHECK module
        # docstring. A malformed row (cron with no expr, one_off with no
        # next_fire_at, event with a populated cron_expr, ...) fails at
        # the substrate boundary rather than at dispatch time.
        sa.CheckConstraint(
            _KIND_FIELDS_CHECK,
            name="ck_scheduled_trigger_kind_fields",
        ),
    )

    # The dispatcher's "what fires next" claim query (T2/T3) scans
    # ``WHERE status = 'active' ORDER BY next_fire_at ASC``. The b-tree
    # on (status, next_fire_at) drives that scan; the partial-index
    # ``WHERE status = 'active'`` on PG further trims the index to only
    # the rows the dispatcher actually claims, but the predicate is
    # PG-only -- SQLite gets a plain b-tree on the same columns.
    if is_postgres:
        op.create_index(
            "scheduled_trigger_next_fire_at_idx",
            "scheduled_trigger",
            ["status", "next_fire_at"],
            postgresql_using="btree",
            postgresql_where=sa.text("status = 'active'"),
        )
    else:
        op.create_index(
            "scheduled_trigger_next_fire_at_idx",
            "scheduled_trigger",
            ["status", "next_fire_at"],
        )

    # Tenant-scoped list (T5 admin surface). The b-tree on
    # (tenant_id, kind) drives "list this tenant's cron triggers"
    # without sequential-scanning the table.
    op.create_index(
        "scheduled_trigger_tenant_idx",
        "scheduled_trigger",
        ["tenant_id", "kind"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the indexes then the ``scheduled_trigger`` table."""
    op.drop_index("scheduled_trigger_tenant_idx", table_name="scheduled_trigger")
    op.drop_index("scheduled_trigger_next_fire_at_idx", table_name="scheduled_trigger")
    op.drop_table("scheduled_trigger")
