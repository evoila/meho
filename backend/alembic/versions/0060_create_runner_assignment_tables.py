# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``runner_assignments`` + ``runner_check_results`` tables (#2499).

Revision ID: 0059
Revises: 0058
Create Date: 2026-07-15

Initiative #2415 (Remote execution gateway), Task #2499 — the central-side
ingest + versioned assignment API for the push-only satellite runner. Two
gateway-owned tables:

* ``runner_assignments`` — one operator-authored document per
  ``(tenant_id, runner_name)``. ``PUT /api/v1/checks/assignment/{runner}``
  replaces the row wholesale; the runner-facing ``GET`` materialises the
  authored ``items`` (JSONB) into wire ``RunnerWorkItem`` objects at
  request time, resolving the live target descriptor + the op's
  ``handler_ref`` / ``safety_level`` so target-row drift is picked up on
  the next poll rather than frozen at authoring time.

* ``runner_check_results`` — one row per accepted result in a runner's
  ``POST /api/v1/checks/results`` batch. ``received_at`` is stamped by the
  central clock at ingest (never accepted from the client), because the
  dead-man's switch (#2501) flips workloads stale on the central clock.
  ``(tenant_id, runner_name, result_uid)`` is unique, making re-posts from
  the runner's on-disk retry spool (#2497) idempotent.

This migration is the **third** in the initiative's serialized chain
(#2502 -> #2498 -> #2499 -> #2500 -> #2501); it extends the then-current
single head ``0058`` (``runner_principal``). Per the house Alembic rule,
if a sibling migration lands on main first, renumber-before-merge — never
fork the linear chain.

Schema notes
------------

* ``tenant_id`` is a real ``REFERENCES tenant(id)`` FK on both tables —
  brand-new clean-slate tables, mould parity with ``runner_principal``
  (``0058``). Both tables must therefore appear in the integration +
  acceptance per-test TRUNCATE lists (``test_truncate_list_drift`` guards
  this).
* ``runner_name`` is a **soft** FK to ``runner_principal.name`` (no DB FK),
  the same soft-reference discipline the gateway set uses so #2499/#2501
  reference a runner by name without coupling to the principal lifecycle.
* ``runner_check_results.status`` is bounded by a portable CHECK to the
  tri-state ``ok`` / ``refused`` / ``error`` — the ``RunnerResult.status``
  vocabulary the runner posts (a bare ``ok``/``error`` CHECK would reject
  the ``refused`` rows the runner legitimately reports).
* ``items`` / ``result_payload`` are portable ``JSON`` -> ``JSONB``
  variants (mould ``0020``).

Dialect portability
-------------------

Mirrors ``0058`` / ``0020``: ``gen_random_uuid()`` / ``now()`` server
defaults on PG with the ORM ``default`` covering SQLite; ``JSON`` ->
``JSONB`` variant for the document columns.

Reversibility contract
----------------------

``upgrade()`` creates each table then its indexes; ``downgrade()`` drops
the indexes then the tables in inverse order. Explicit index drops keep
the inverse symmetric across both dialects (SQLite does not always cascade
indexes on ``drop_table``). Purely additive on the way up.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_variant() -> sa.types.TypeEngine[object]:
    """Portable ``JSON`` -> ``JSONB`` column type (mould ``0020``)."""
    return sa.JSON(none_as_null=True).with_variant(
        postgresql.JSONB(none_as_null=True), "postgresql"
    )


def upgrade() -> None:
    """Create the two gateway tables and their indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "runner_assignments",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # Real FK -- clean-slate table, mould parity with runner_principal.
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        # Soft-FK to runner_principal.name (no DB FK).
        sa.Column("runner_name", sa.Text(), nullable=False),
        sa.Column("items", _json_variant(), nullable=False),
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
    )
    op.create_index(
        "runner_assignments_tenant_runner_idx",
        "runner_assignments",
        ["tenant_id", "runner_name"],
        unique=True,
        postgresql_using="btree",
    )

    op.create_table(
        "runner_check_results",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("runner_name", sa.Text(), nullable=False),
        sa.Column("result_uid", sa.Text(), nullable=False),
        sa.Column("check_ref", sa.Text(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result_payload", _json_variant(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        # Central-stamped at ingest -- NOT accepted from the client.
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'refused', 'error')",
            name="ck_runner_check_results_status",
        ),
    )
    # Ingest idempotency: a re-posted spool batch collides here.
    op.create_index(
        "runner_check_results_uid_idx",
        "runner_check_results",
        ["tenant_id", "runner_name", "result_uid"],
        unique=True,
        postgresql_using="btree",
    )
    # #2501 staleness reads: latest result per (runner, check).
    op.create_index(
        "runner_check_results_staleness_idx",
        "runner_check_results",
        ["tenant_id", "runner_name", "check_ref", "received_at"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the indexes then the two tables (reverse order)."""
    op.drop_index(
        "runner_check_results_staleness_idx",
        table_name="runner_check_results",
    )
    op.drop_index(
        "runner_check_results_uid_idx",
        table_name="runner_check_results",
    )
    op.drop_table("runner_check_results")
    op.drop_index(
        "runner_assignments_tenant_runner_idx",
        table_name="runner_assignments",
    )
    op.drop_table("runner_assignments")
