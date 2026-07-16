# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``gateway_command`` queue table for Initiative #2415 (#2498).

Revision ID: 0059
Revises: 0058
Create Date: 2026-07-15

This migration is the transport substrate of Task #2498 (outbound
long-poll command plane) under Initiative #2415 (Remote execution
gateway). It creates the ``gateway_command`` table — the durable queue
that holds centrally-enqueued operations per satellite runner. The two
runner-facing routes (blocking ``GET /api/v1/gateway/{runner}/next`` and
``POST /api/v1/gateway/{runner}/result``) claim from and report onto rows
of this table; central code enqueues via
:func:`meho_backplane.gateway.queue.enqueue_command`.

Serialized order (from the initiative's set-consistency review): this is
the **second** migration in the chain (#2502 -> #2498 -> #2499 -> #2500
-> #2501). It extends the then-current single head ``0058`` (#2502's
``runner_principal`` table). Per the house Alembic rule, if a sibling
migration lands on main first, renumber-before-merge — the number is not
load-bearing, only ``down_revision`` extending the current head is.

Lifecycle
---------

A row walks a closed four-state lifecycle, mirroring the
``approval_request`` durable-queue discipline (#817):

* ``pending``   — enqueued centrally, awaiting a runner claim.
* ``delivered`` — claimed by the runner's long-poll (``pending`` flips to
  ``delivered`` under ``SELECT ... FOR UPDATE SKIP LOCKED`` on PG / a
  conditional ``UPDATE`` on the SQLite test path); ``delivered_at`` stamped.
* ``succeeded`` / ``failed`` — terminal, set by ``POST .../result``;
  ``result`` / ``error`` and ``completed_at`` stamped.

A row that is claimed but never reported stays ``delivered`` — lost, not
redelivered (the v1 at-most-once failure mode; #2500 bounds claimability
via its own ``expires_at``, out of scope here).

Columns
-------

* ``id`` — UUID primary key. PG production gets ``gen_random_uuid()`` via
  this migration; the ORM ``default=uuid.uuid4`` covers SQLite and the
  enqueue path.

* ``tenant_id`` — UUID NOT NULL, ``REFERENCES tenant(id)``. A real FK
  because ``gateway_command`` is a brand-new clean-slate table. ``NO
  ACTION`` on delete: removing a tenant that still has queued commands is
  blocked at the DB layer (same discipline as ``approval_request`` 0023
  and ``runner_principal`` 0058).

* ``runner_id`` — Text NOT NULL. The runner principal **name** (the wire
  identity: #2498's ``{runner}`` path segment, ``MEHO_RUNNER_ID`` on the
  runner, ``RunnerResultBatch.runner_id`` on the wire). Named ``runner_id``
  to match that wire field; the per-tenant unique ``runner_principal``
  ``name`` is what the gateway guard binds the token's ``runner_id`` UUID
  claim to, so filtering the queue by name is correctly scoped.

* ``op_id`` — Text NOT NULL. The operation id the runner executes.

* ``params`` — portable JSON NOT NULL DEFAULT ``{}`` (JSONB on PG). The
  validated op params.

* ``target_descriptor`` — portable JSON **nullable** (JSONB on PG). The
  centrally-resolved target descriptor a connector handler duck-reads
  (``connectors/resolver.py`` is DB-bound; the runner has no local target
  table). Nullable — not NOT NULL as the task sketch first drafted —
  because targetless synthetic ops (``net.*``) carry no descriptor, which
  the wire model already encodes as
  ``RunnerWorkItem.target_descriptor: ResolvedTargetDescriptor | None``
  (#2497). A NOT NULL column would force ``{}`` for those, and ``{}`` is
  not a valid ``ResolvedTargetDescriptor`` (its ``name`` / ``product`` are
  required) — so NULL is the wire-compatible encoding of "targetless".

* ``status`` — Closed enum, portable ``IN (...)`` CHECK, DEFAULT ``pending``.

* ``result`` — portable JSON nullable (JSONB on PG). The runner's success
  payload; NULL until reported.

* ``error`` — Text nullable. The runner's failure summary; NULL until a
  failure is reported.

* ``enqueued_by_sub`` — Text NOT NULL. The ``sub`` of the principal whose
  central dispatch enqueued the command (audit provenance; #2500's minting
  path is the production caller).

* ``enqueued_at`` — ``timestamptz`` NOT NULL. PG ``now()`` server default;
  ORM ``default`` for SQLite. Drives the FIFO claim order.

* ``delivered_at`` / ``completed_at`` — ``timestamptz`` nullable. Stamped
  on the ``pending -> delivered`` claim and the ``delivered -> terminal``
  report respectively.

Index
-----

* ``gateway_command_claim_idx`` — composite b-tree on
  ``(tenant_id, runner_id, status, enqueued_at)``. Serves the hot claim
  query (oldest ``pending`` row for a runner in a tenant) and the
  tenant/runner-scoped result lookup.

Dialect portability
-------------------

Mirrors ``0023`` / ``0056``: ``gen_random_uuid()`` / ``now()`` server
defaults on PG with the ORM covering SQLite; portable
``JSON().with_variant(JSONB(), "postgresql")`` JSON columns; a portable
``IN (...)`` CHECK.

Reversibility contract
----------------------

``upgrade()`` creates the table then its index; ``downgrade()`` drops the
index then the table in inverse order. Purely additive on the way up (no
destructive DDL), so the migration-compat CI guard passes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``gateway_command.status`` vocabulary — kept in lock-step with
#: :class:`meho_backplane.db.models.GatewayCommandStatus`. Duplicated here
#: as a literal tuple (not imported) so the migration's recorded DDL is a
#: frozen snapshot independent of any later edit to the model enum — the
#: same self-contained discipline migration ``0023`` follows. The drift
#: guard in :mod:`tests.migrations.test_migration_0059_create_gateway_command`
#: asserts the model enum and this recorded vocabulary agree.
_GATEWAY_COMMAND_STATUSES: tuple[str, ...] = (
    "pending",
    "delivered",
    "succeeded",
    "failed",
)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Create the ``gateway_command`` table and its claim index."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Portable JSONB -> JSON variant; PG gets binary JSONB, SQLite text JSON.
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    op.create_table(
        "gateway_command",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # Real FK -- clean-slate substrate, no ondelete (tenant deletion
        # must clear queued commands first; NO ACTION blocks the cascade).
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        # The runner principal NAME (wire identity), not the UUID row id.
        sa.Column("runner_id", sa.Text(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column(
            "params",
            json_type,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        # Nullable: targetless synthetic ops (net.*) carry no descriptor;
        # NULL is the wire-compatible encoding of "targetless" (see docstring).
        sa.Column("target_descriptor", json_type, nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("result", json_type, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("enqueued_by_sub", sa.Text(), nullable=False),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        # Closed enum -- portable IN(...) CHECK. Drift guard in test_migration.
        sa.CheckConstraint(
            _check_in("status", _GATEWAY_COMMAND_STATUSES),
            name="ck_gateway_command_status",
        ),
    )

    op.create_index(
        "gateway_command_claim_idx",
        "gateway_command",
        ["tenant_id", "runner_id", "status", "enqueued_at"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the claim index then the ``gateway_command`` table."""
    op.drop_index("gateway_command_claim_idx", table_name="gateway_command")
    op.drop_table("gateway_command")
