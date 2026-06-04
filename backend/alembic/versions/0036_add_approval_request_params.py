# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``params`` to ``approval_request`` for direct-op approve re-dispatch.

Revision ID: 0036
Revises: 0035
Create Date: 2026-06-04

This migration is the schema substrate of Task #1503 (G0.20-T3) under
Initiative #1500 (the v0.10.1 closed-loop dogfood hardening). It extends
the ``approval_request`` table — created by migration
``0023_create_approval_request`` (G11.2-T4, #817) — with a nullable
``params`` JSON column.

Why this column
---------------

A parked **direct** operator op (an operator calling a
``requires_approval`` op directly, not via an agent run) that is then
approved through ``/decide`` or the MCP/CLI by-id approve surface was
never executed: those surfaces hold only the request id, not the
original params, so they could record the decision but not re-dispatch.
The only execute-after-approve path was REST ``/approve`` carrying the
params in-band. Storing the params on the row turns it into a complete
re-dispatch primitive (it already holds ``connector_id`` / ``op_id`` /
``target_id``), so any approval surface can drive the post-approval
re-dispatch with the stored params.

The in-process agent-run resume path is unaffected: it keeps the params
in memory and re-dispatches from there (the live-wait case), so it never
reads this column.

What this migration adds
------------------------

* ``params`` — nullable JSON (PG ``JSONB`` via the ORM ``with_variant``;
  generic ``sa.JSON`` here so the recorded DDL stays dialect-portable,
  same discipline as ``0030``'s redaction columns). ``NULL`` = a
  pre-0036 row with no stored params (those rows predate the feature and
  can only be resumed via REST ``/approve`` + params, exactly as
  before). A row written on or after 0036 always carries the params.

Dialect-portability decisions
-----------------------------

Mirrors the discipline 0001-0035 established:

* ``params`` — nullable JSON, no server default (NULL is the sensible
  initial value for every existing row). Generic ``sa.JSON`` mirrors
  ``0030`` (``audit_log.raw_payload`` / ``redaction_manifest``): PG gets
  the binary ``JSONB`` the ORM pins via ``with_variant``; SQLite gets
  text JSON.

Reversibility contract
----------------------

``downgrade()`` drops the column, reversing ``upgrade()``. The CI guard
inspects only ``upgrade()``; destructive ops in ``downgrade()`` are
allowed by design.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approval_request",
        sa.Column(
            "params",
            sa.JSON(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("approval_request", "params")
