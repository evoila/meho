# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create ``runner_write_allowlist`` — the per-runner remote-write capability set.

Revision ID: 0095
Revises: 0094
Create Date: 2026-09-01

Task #3190 under Initiative #2901 (satellite write path), design
``docs/research/2901-satellite-write-path.md`` §3 mechanism 2, decision
``docs/decisions/satellite-write-path.md``. The per-runner-principal
**capability allowlist**: each row grants one ``(op_pattern, target_scope)``
remote-write capability to one runner principal, and the set of rows for a
runner **is** the definition of its write blast radius (threats T1/T8). A
``remote-write`` (``caution``) op mints to a runner only when its op-class +
target is on this allowlist (checked at the central mint, ANDed with the
approval binding of #3189) **and** the runner's own provisioning-config
mirror agrees at the edge — defence in depth, exactly like the safe wall.

What this migration adds
------------------------

* ``runner_write_allowlist`` table:
  ``id`` (uuid PK), ``tenant_id`` (FK ``tenant.id``),
  ``runner_principal_id`` (FK ``runner_principal.id`` — the capability is hung
  off the runner principal), ``op_pattern`` (glob over ``op_id``),
  ``target_scope`` (``*`` or a concrete ``str(target.id)`` cap),
  ``created_by_sub`` (the granting human — the issuance binding), and
  ``created_at``.
* ``runner_write_allowlist_lookup_idx`` on ``(tenant_id,
  runner_principal_id)`` — the dominant mint-time read ("every capability for
  runner R in tenant T").
* ``uq_runner_write_allowlist_capability`` unique on ``(runner_principal_id,
  op_pattern, target_scope)`` — a re-grant of the same capability is
  idempotent, not a duplicate.

Not-at-birth (T7)
-----------------

Enrollment (``runner_principal`` register) writes **no** rows here: a write
capability requires a separate human step
(``RunnerWriteAllowlistService.grant`` over the operator-gated route), so a
runner cannot self-widen its allowlist (decision recommendation 2).

Migration-chain note (single linear head)
-----------------------------------------

Numbered ``0095`` with ``down_revision = "0094"`` — the head on ``origin/main``
after sibling #3193 (effect audit + alarm) landed ``0094_satellite_effect_audit``
mid-flight (``0093`` was originally this task's number against the old head
``0092``; it was renumbered to ``0095`` and re-parented onto ``0094`` when #3193
merged first, so ``0093`` is skipped and the chain stays a single linear head
``0092 -> 0094 -> 0095``). Additive-only: a fresh table, no ALTER of an existing
object.

Reversibility contract
----------------------

``downgrade()`` drops the indexes then the table. Fresh table, no data
dependency, so the downgrade is clean.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0095"
down_revision: str | None = "0094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``runner_write_allowlist`` table + its two indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "runner_write_allowlist",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column(
            "runner_principal_id",
            sa.Uuid(),
            sa.ForeignKey("runner_principal.id"),
            nullable=False,
        ),
        sa.Column("op_pattern", sa.Text(), nullable=False),
        sa.Column("target_scope", sa.Text(), nullable=False),
        sa.Column("created_by_sub", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
    )
    # Dominant mint-time read: every capability for a runner in a tenant.
    op.create_index(
        "runner_write_allowlist_lookup_idx",
        "runner_write_allowlist",
        ["tenant_id", "runner_principal_id"],
        postgresql_using="btree",
    )
    # At most one row per fully-scoped capability (idempotent re-grant).
    op.create_index(
        "uq_runner_write_allowlist_capability",
        "runner_write_allowlist",
        ["runner_principal_id", "op_pattern", "target_scope"],
        unique=True,
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the indexes and the table created in :func:`upgrade`."""
    op.drop_index(
        "uq_runner_write_allowlist_capability",
        table_name="runner_write_allowlist",
    )
    op.drop_index(
        "runner_write_allowlist_lookup_idx",
        table_name="runner_write_allowlist",
    )
    op.drop_table("runner_write_allowlist")
