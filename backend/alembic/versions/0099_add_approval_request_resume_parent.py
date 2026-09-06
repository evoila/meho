# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request.resume_parent`` — parent composite for a sub-op park.

Revision ID: 0099
Revises: 0098
Create Date: 2026-09-06

Task #3351. A composite handler that parks a governed direct-session sub-op
(via :func:`~meho_backplane.operations.composite.enforce_subop_policy`) writes
an :class:`ApprovalRequest` whose ``op_id`` + ``params`` are the *sub-op*
governance key + identity-only gate params — never a dispatchable descriptor
call. On approval the shared resume path
(:func:`~meho_backplane.operations.approval_queue.resume_dispatch_after_approval`)
re-dispatched that stored key generically, which cannot execute: a VI-JSON vim
child (``POST:/…/CreateVM_Task``) fails schema validation / path substitution
because the gate params omit the path var + assembled body, and a REST
``?action=`` power child (``POST:/vcenter/vm/{vm}/power?action=start``) has no
ingested descriptor and returns ``unknown_op``. Either way the approve produced
a red resume row that executed nothing while burning the exactly-one-resumer
claim.

This migration adds the durable landing spot that lets the resume re-enter the
**parent composite** instead of the raw sub-op key:

* ``resume_parent`` -- JSON (JSONB on PG), nullable. ``{"op_id": <composite
  op_id>, "params": <composite params>}`` captured at park time from the
  composite-dispatch context
  (:data:`~meho_backplane.operations.composite.composite_dispatch_var`). The
  resume path re-dispatches this composite ``_approved=True`` with the approved
  sub-op pre-cleared, so the whole governed step reproduces through the normal
  dispatch path. NULL for every non-composite (direct-op) park and on pre-0099
  rows — those keep the unchanged generic re-dispatch. Internal resume input
  only, like ``params``; never projected onto a read view or a broadcast frame.

Soft-column discipline mirrors ``0036`` / ``0053`` / ``0055`` / ``0096``:
nullable, no server default (Python-side ``None`` / capture-set), reversible,
no indexes (only ever read/written off a row already loaded by primary key).

Migration-chain note (single linear head)
-----------------------------------------

Numbered ``0099`` with ``down_revision = "0098"`` — the head on ``origin/main``.
Additive-only: one nullable column on an existing table, no ALTER of an existing
column.

Reversibility contract
----------------------

``downgrade()`` drops the column. SQLite's ALTER TABLE drop-column has been
supported since 3.35.0 (we're on 3.45+); Alembic's batch-mode fallback is not
required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0099"
down_revision: str | None = "0098"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``resume_parent`` column to ``approval_request``."""
    op.add_column(
        "approval_request",
        sa.Column(
            "resume_parent",
            sa.JSON(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the ``resume_parent`` column added in :func:`upgrade`."""
    op.drop_column("approval_request", "resume_parent")
