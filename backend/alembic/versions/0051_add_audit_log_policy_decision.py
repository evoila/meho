# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``audit_log.policy_decision`` — stamp the policy-gate verdict on the row.

Task #130 under Initiative #128 (policy-gate hardening), Goal #87. The
synchronous policy gate
(:func:`meho_backplane.operations._validate.policy_gate`) computes a
:class:`~meho_backplane.db.models.PermissionVerdict`
(``auto-execute`` / ``needs-approval`` / ``deny``) on every governed
``call_operation``, and the dispatcher branches on it — but the verdict was
never persisted, so a consumer had to reconstruct it by joining
``method``+``path`` patterns and parsing ``payload``. This migration adds the
first-class column so the moat-pillar promise ("the verdict is stamped onto
the operation's audit row") holds and a consumer can
``WHERE policy_decision = '<verdict>'`` directly.

What this migration adds
------------------------

* ``audit_log.policy_decision text NULL`` — the gate verdict, populated by
  both audit writers (the dispatch-row writer and the approval-queue writer)
  off the dispatch-scoped ``policy_decision_var`` contextvar. No index — it is
  a low-cardinality equality filter normally combined with a time/principal
  predicate that ``audit_log_occurred_at_idx`` / ``audit_log_operator_sub_idx``
  already cover.
* ``ck_audit_log_policy_decision`` CHECK — the value is the closed
  ``PermissionVerdict`` set or ``NULL``, DB-enforced. Mirrors
  ``ck_agent_permission_verdict``; the enum is intentionally closed (a fourth
  verdict is a coordinated code + migration change).

Why nullable (no ``server_default``, like ``0050``'s ``tls_server_name``)
-------------------------------------------------------------------------

"No gate ran for this row" is a first-class state, not a default value:
pre-#130 rows, pre-gate usage-error rows (unknown op / invalid params /
connector-resolution failures), and system-internal writers carry ``NULL``.
A plain nullable ``ADD COLUMN`` is safe on a populated table — PostgreSQL and
SQLite both add it as ``NULL`` for every existing row with no rewrite.

Why ``batch_alter_table`` for the CHECK
---------------------------------------

Same portable cookbook ``0025`` used to add a CHECK to an existing table: on
PostgreSQL Alembic issues plain ``ALTER TABLE ADD COLUMN`` + ``ADD
CONSTRAINT`` (no copy); on SQLite it falls back to the table-recreate path
(the only way SQLite adds a CHECK to an existing table). One batch block adds
the column and the constraint together, so SQLite recreates once.

Reversibility contract
----------------------

``downgrade()`` drops the constraint + column inside the same batch block.
The reversible-additive discipline established by ``0006``+ applies: new
requirement = new migration head, never rewrite historical migrations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The closed PermissionVerdict vocabulary, mirrored in the CHECK. ``NULL`` is
# permitted (no gate ran / pre-#130 row); a SQL CHECK passes on NULL, and the
# explicit ``IS NULL`` arm keeps the intent legible + matches the model's
# ``ck_audit_log_policy_decision``.
_POLICY_DECISION_CHECK: str = (
    "policy_decision IN ('auto-execute', 'needs-approval', 'deny') OR policy_decision IS NULL"
)


def upgrade() -> None:
    """Add the nullable, CHECK-constrained ``policy_decision`` column."""
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.add_column(
            sa.Column(
                "policy_decision",
                sa.Text(),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_audit_log_policy_decision",
            _POLICY_DECISION_CHECK,
        )


def downgrade() -> None:
    """Drop the ``policy_decision`` column + its CHECK added in :func:`upgrade`."""
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.drop_constraint(
            "ck_audit_log_policy_decision",
            type_="check",
        )
        batch_op.drop_column("policy_decision")
