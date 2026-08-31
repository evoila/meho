# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-tenant flight-recorder agent-read gate (#3216, F5).

Revision ID: 0087
Revises: 0086
Create Date: 2026-08-31

Task #3216 under Initiative #3207. Ships the per-tenant gate for the
**agent** read surface of the dispatch flight recorder (decision of record:
``docs/decisions/dispatch-flight-recorder.md``, F5 -- the operator override
that makes traces agent-readable through the narrow-waist result-handle
idiom). The operator plane keeps full trace access independent of this gate;
this column governs only whether an *agent* may page a tenant's traces.

What this migration adds
------------------------

``tenant.flight_recorder_agent_readable`` -- a **tri-state** Boolean nullable
column, mirroring the ``targets.flight_recorder_capture`` override shape
(migration ``0085``):

* ``NULL`` (default) -- **inherit** the per-tenant capture default
  (``tenant.flight_recorder_enabled``). This is what makes the gate "follow
  the F1 default (lab-on)": a lab-class tenant that flipped capture ON gets
  agent-readable traces by default, with no second flag to set.
* ``True`` -- force agent-readable ON regardless of the capture default.
* ``False`` -- force agent-readable OFF while the operator plane keeps full
  access. This is the independent gate-off the F5 override requires: an
  operator can withhold traces from agents without turning capture off (which
  would also blind the operator plane).

Additive-only, per the migration-compatibility house rule -- one ``ADD
COLUMN`` on ``tenant``, no ALTER of an existing column and no data rewrite.
The column is **nullable** (NULL = inherit), so the add-column needs no
backfill and is safe on a populated table.

Reversibility contract
----------------------

``downgrade()`` drops the single added column. SQLite ALTER-TABLE
drop-column is supported since 3.35.0 (we run 3.45+), so Alembic batch-mode
is not required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0087"
down_revision: str | None = "0086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tri-state agent-read gate (F5): NULL inherit / True on / False off.
    # Nullable, so no backfill -- existing tenants keep NULL (= inherit the
    # capture default), preserving the F1 lab-on posture with no flag change.
    op.add_column(
        "tenant",
        sa.Column("flight_recorder_agent_readable", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant", "flight_recorder_agent_readable")
