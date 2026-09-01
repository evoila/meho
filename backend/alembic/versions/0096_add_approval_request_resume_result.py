# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request.resume_result`` — the approved dispatch's result.

Revision ID: 0096
Revises: 0095
Create Date: 2026-09-01

Task #3209. A paired, out-of-process consumer that parks a **non-idempotent**
governed op for approval (the concrete case: VCF
``installer.sddc.bringup.start``) cannot resume it in place after a human
approves, because it has no way to retrieve the result the backplane produced
when it re-dispatched the approved op. Two surface facts made the result
unretrievable: :class:`ApprovalRequestView` carries no execution result, and
``GET /api/v1/audit/by-work-ref`` reduces params to a hash and exposes no raw
response body. So the consumer could only *re-submit*, which for a
non-idempotent op starts a second bring-up.

This migration adds the durable landing spot for that result:

* ``resume_result`` -- JSON (JSONB on PG), nullable. The **reduced /
  handle-shaped** :class:`~meho_backplane.connectors.schemas.OperationResult`
  envelope (``OperationResult.model_dump(mode="json")``) of the approved
  re-dispatch, captured on the exactly-one-resumer winning path
  (:func:`~meho_backplane.operations.approval_queue.resume_dispatch_after_approval`).
  It is the *same* reduced envelope the approver already sees inline — for a
  set-shaped response the payload rides a JSONFlux ``handle`` (v0.1-spec §4),
  never a raw body — so this is not a new raw-payload surface. NULL until the
  winning resumer captures it: a pending / rejected / expired row, an agent-run
  request resumed in-process (which does not route through the shared operator
  resume path), and every pre-0096 row all keep NULL. Internal to the
  principal-scoped ``GET /api/v1/approvals/{id}/result`` read surface only;
  like ``params`` it is never projected onto the default read view or a
  broadcast frame.

Soft-column discipline mirrors ``0036`` / ``0040`` / ``0053`` / ``0055``:
nullable, no server default (Python-side ``None`` / capture-set), reversible,
no indexes (the column is only ever read/written off a row already loaded by
primary key).

Migration-chain note (single linear head)
-----------------------------------------

Numbered ``0096`` with ``down_revision = "0095"`` — the head on
``origin/main`` after the satellite write-path chain landed
(``0092 -> 0094 -> 0095``; the ``0093`` gap is a renumbered sibling, see
``0095``). Additive-only: one nullable column on an existing table, no ALTER of
an existing column.

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
revision: str = "0096"
down_revision: str | None = "0095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``resume_result`` column to ``approval_request``."""
    op.add_column(
        "approval_request",
        sa.Column(
            "resume_result",
            sa.JSON(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the ``resume_result`` column added in :func:`upgrade`."""
    op.drop_column("approval_request", "resume_result")
