# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add trusted claim columns to ``agent_announcement`` (#3736).

Revision ID: 0108
Revises: 0107
Create Date: 2026-10-16

Only a trusted agent may now publish an announcement, and each new
announcement is one step (start / update / completion) of a claim. This
adds six nullable columns:

* ``claim_id`` -- the logical claim UUID shared by a claim's steps.
* ``owner_sub`` -- the trusted agent identity that owns the claim.
* ``actor_sub`` -- the acting agent of a delegated hosted run.
* ``admission`` -- how MEHO checked the identity.
* ``idempotency_key`` -- the caller's retry key for one step.
* ``stream_entry_id`` -- the Valkey stream entry id once the step reached
  the feed. NULL means the row was stored but the stream write failed; a
  retry with the same key finishes the delivery.

And three partial unique indexes so the database, not only the
application, enforces the rules: one retry key per owner, one start per
claim, one completion per claim.

There is no backfill. Existing rows keep NULL in every new column. A
row with no ``claim_id`` / ``owner_sub`` is history only and can never
become an active claim, which is the intended meaning for rows written
before trusted admission. Only new rows get a ``stream_entry_id``; old
rows have none, and nothing retries them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0108"
down_revision: str | None = "0107"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_announcement"
_TEXT_COLUMNS: tuple[str, ...] = (
    "owner_sub",
    "actor_sub",
    "admission",
    "idempotency_key",
    "stream_entry_id",
)
_IDEMPOTENCY_WHERE = "idempotency_key IS NOT NULL"
_START_WHERE = "phase = 'start' AND claim_id IS NOT NULL"
_COMPLETION_WHERE = "phase = 'completion' AND claim_id IS NOT NULL"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("claim_id", sa.Uuid(), nullable=True))
    for name in _TEXT_COLUMNS:
        op.add_column(_TABLE, sa.Column(name, sa.Text(), nullable=True))
    op.create_index(
        "agent_announcement_idempotency_uq",
        _TABLE,
        ["tenant_id", "owner_sub", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text(_IDEMPOTENCY_WHERE),
        sqlite_where=sa.text(_IDEMPOTENCY_WHERE),
    )
    op.create_index(
        "agent_announcement_claim_start_uq",
        _TABLE,
        ["tenant_id", "claim_id"],
        unique=True,
        postgresql_where=sa.text(_START_WHERE),
        sqlite_where=sa.text(_START_WHERE),
    )
    op.create_index(
        "agent_announcement_claim_completion_uq",
        _TABLE,
        ["tenant_id", "claim_id"],
        unique=True,
        postgresql_where=sa.text(_COMPLETION_WHERE),
        sqlite_where=sa.text(_COMPLETION_WHERE),
    )


def downgrade() -> None:
    op.drop_index("agent_announcement_claim_completion_uq", table_name=_TABLE)
    op.drop_index("agent_announcement_claim_start_uq", table_name=_TABLE)
    op.drop_index("agent_announcement_idempotency_uq", table_name=_TABLE)
    for name in reversed(_TEXT_COLUMNS):
        op.drop_column(_TABLE, name)
    op.drop_column(_TABLE, "claim_id")
