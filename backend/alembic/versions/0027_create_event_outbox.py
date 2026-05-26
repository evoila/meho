# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``event_outbox`` table for durable event-subscription triggers.

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-26

This migration is the storage substrate of Initiative #804 (G11.3
Scheduler P2), Task #824 (T3). It adds the ``event_outbox`` table --
one row per MEHO-internal event that a subscribed agent trigger may
fire on (an agent run reaching a terminal state; future kinds: audit
predicates, connector alerts).

Why a transactional outbox (not raw ``LISTEN/NOTIFY``)
------------------------------------------------------

Plain PostgreSQL ``LISTEN/NOTIFY`` is **not durable**: a notification
sent while no listener is connected is lost forever (per the PG manual
§47.11). For an event-driven agent trigger that must survive process
restarts, that loss is unacceptable -- a connector alert at 02:00 with
no listener attached must still fire the on-call escalation agent
once the listener reconnects.

The durable, replica-safe answer is the **transactional outbox
pattern**: producers insert an ``event_outbox`` row in the same DB
transaction that writes the event-producing state change (an
``agent_run`` row transitioning to ``succeeded``). A separate drain
loop scans the outbox via ``SELECT ... FOR UPDATE SKIP LOCKED``,
claims unprocessed rows, dispatches them, and marks them processed.
The drain loop sits on a 5-10s cadence so the outbox is always making
forward progress even with no NOTIFY.

``LISTEN/NOTIFY`` is added on top as a **latency hint** only: the
producer's same-transaction commit triggers an asynchronous NOTIFY
that wakes the drain loop's sleep early, dropping per-event latency
from "next 10s tick" to "sub-second". A dropped notification is
benign -- the next polled tick picks the row up anyway.

Schema
------

* ``event_id`` -- ``BIGSERIAL`` primary key. A monotonic sequence (not
  UUID) so the drain's "scan unprocessed events" query has a natural
  ordering key without timestamp ties; the drain queries
  ``WHERE processed_at IS NULL ORDER BY event_id`` for fairness.
  ``BIGSERIAL`` on PG; the ORM-side ``Integer`` mapping with
  ``autoincrement=True`` keeps the SQLite test path on a plain
  rowid-derived integer (no sequence object needed).

* ``tenant_id`` -- UUID NOT NULL with a real ``REFERENCES tenant(id)``
  FK. Same discipline as :class:`AgentRun.tenant_id` (0017) and
  :class:`ScheduledTrigger.tenant_id` (0020): every event lives inside
  a tenant; an orphan event surfaces as ``IntegrityError`` at insert.
  No ``ondelete`` -- a tenant with live events must clear them first.

* ``event_kind`` -- Text NOT NULL. The discriminator the matcher will
  use once the subscription-junction lands (T5 #826). Free-text rather
  than a closed enum because new event kinds (audit predicates,
  connector alerts) are added per-Initiative without coordinated DB
  migrations; the matching policy lives in the subscriber rather than
  a DB-level constraint. v0.2 values shipped: ``agent_run.completed``.

* ``payload`` -- portable JSON -> JSONB NOT NULL. The event-specific
  payload the subscriber's filter matches against. NOT NULL with a
  default of ``'{}'`` so a payload-less event remains insertable
  without ambiguity at the SQL layer.

* ``claimed_at`` / ``claimed_by`` -- ``timestamptz`` + Text, both
  nullable. Stamped by the drain loop on a successful
  ``SELECT FOR UPDATE SKIP LOCKED`` + ``UPDATE`` claim. ``claimed_by``
  records a process / replica identifier so an operator can observe
  which replica is handling a stuck claim. Both NULL on an
  unprocessed event.

* ``processed_at`` -- ``timestamptz`` nullable. Stamped by the drain
  loop after the event has been dispatched (or marked no-op in v0.2
  when no subscriber matches). NULL means "not yet processed"; the
  partial index keys on this column.

* ``created_at`` -- ``timestamptz`` NOT NULL DEFAULT ``now()``. The
  insert-time wall-clock; observable from the admin surface (T5) for
  diagnostics.

Indexes
-------

* ``event_outbox_tenant_unprocessed_idx`` -- b-tree on
  ``(tenant_id, processed_at NULLS FIRST, event_id)``. Drives the
  drain loop's tenant-scoped "what's unprocessed next" query (T3
  future scaling; in v0.2 the drain is global, but the index keeps a
  tenant filter cheap once the matcher layers it on).

* ``event_outbox_unprocessed_idx`` -- partial b-tree on ``event_id``
  ``WHERE processed_at IS NULL``. Drives the global drain scan; the
  partial keeps the index small (it carries only the rows the drain
  actually scans) and discards processed rows from the index entirely
  -- which is what keeps the scan cost flat as the table grows. SQLite
  gets a plain b-tree (partial-index WHERE grammar varies across
  SQLite builds and the test path is single-replica).

Subscription matcher deferred
-----------------------------

The subscription-junction (looking up
:class:`ScheduledTrigger` rows with ``kind='event'`` and matching
their ``event_filter`` against ``event_outbox.payload``) is **not in
this migration's scope**. It depends on T5 #826's admin surface to
ship the trigger-creation path. v1 of the drain loop processes events
by stamping ``processed_at`` (no subscriber matched); the matcher is
folded in as a follow-up once T5 lands.

Reversibility contract
----------------------

``downgrade()`` drops the indexes then the table in inverse order.
Mirrors the discipline migrations ``0017`` / ``0020`` follow.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``event_outbox`` table + its indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Portable JSONB -> JSON variant. ``none_as_null`` is NOT set here
    # (unlike ``scheduled_trigger.event_filter``) because the column is
    # NOT NULL with a server default of ``'{}'`` -- a Python ``None``
    # for the payload is a producer bug, not a NULL-storing intent.
    payload_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    # Dialect-portable ``BIGSERIAL`` substitute: ``BigInteger`` on PG
    # compiles to ``BIGSERIAL`` when paired with ``primary_key=True``
    # + ``autoincrement=True``; on SQLite only ``INTEGER PRIMARY KEY``
    # is the rowid alias that auto-increments (``BIGINT PRIMARY KEY``
    # would not), so the ``with_variant`` swap puts plain ``Integer``
    # on the test dialect. Same pattern :class:`GraphNodeHistory`'s
    # ``history_id`` uses.
    event_id_type = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    op.create_table(
        "event_outbox",
        # BIGSERIAL on PG (via with_variant); INTEGER PRIMARY KEY on
        # SQLite (the unit test path) -- the only SQLite shape that
        # rowid-aliases for autoincrement.
        sa.Column(
            "event_id",
            event_id_type,
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        # Real REFERENCES tenant(id) FK -- see module docstring.
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("event_kind", sa.Text(), nullable=False),
        # ``'{}'`` is the empty-JSON-object literal on both dialects --
        # PG stores it as a JSONB ``{}``; SQLite stores it as the text
        # ``{}`` which the JSON adapter round-trips as ``{}``. The same
        # default keeps a payload-less event insertable without the
        # column ambiguity NULL would introduce.
        sa.Column(
            "payload",
            payload_type,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
    )

    # Drain query: ``WHERE tenant_id = :t AND processed_at IS NULL
    # ORDER BY event_id ASC LIMIT N``. The b-tree on
    # ``(tenant_id, processed_at, event_id)`` drives the per-tenant
    # scan once the matcher (T5 follow-up) layers a tenant filter on
    # top; in v0.2 the drain is global but the index stays in place
    # so the future matcher does not require a backfill migration.
    op.create_index(
        "event_outbox_tenant_unprocessed_idx",
        "event_outbox",
        ["tenant_id", "processed_at", "event_id"],
        postgresql_using="btree",
    )

    # Global drain scan: ``WHERE processed_at IS NULL ORDER BY event_id
    # ASC LIMIT N``. The partial index on PG carries only the
    # unprocessed rows so scan cost stays flat as the table grows; on
    # SQLite the partial-index WHERE grammar varies across older
    # builds, so the test path gets a plain b-tree on ``event_id``.
    if is_postgres:
        op.create_index(
            "event_outbox_unprocessed_idx",
            "event_outbox",
            ["event_id"],
            postgresql_using="btree",
            postgresql_where=sa.text("processed_at IS NULL"),
        )
    else:
        op.create_index(
            "event_outbox_unprocessed_idx",
            "event_outbox",
            ["event_id"],
        )


def downgrade() -> None:
    """Drop the indexes then the ``event_outbox`` table."""
    op.drop_index("event_outbox_unprocessed_idx", table_name="event_outbox")
    op.drop_index("event_outbox_tenant_unprocessed_idx", table_name="event_outbox")
    op.drop_table("event_outbox")
