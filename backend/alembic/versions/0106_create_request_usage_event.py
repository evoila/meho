# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``request_usage_event`` table (#3734).

Revision ID: 0106
Revises: 0104
Create Date: 2026-10-16

Routine REST and MCP activity (lists, searches, resource reads) is usage,
not an accountable action. This table keeps that usage apart from
``audit_log``. Each row holds only opaque ids and bounded labels: tenant,
principal, session, run and request ids, the transport, an enumerated
route or tool, the duration and a bounded outcome code. It never holds a
request body, a query string, tool arguments, a prompt, a token, or a
target name.

What this migration adds
------------------------

* The ``request_usage_event`` table.
* CHECK constraints that pin the closed ``kind``, ``transport`` and
  ``outcome`` values.
* Two indexes:

  * ``request_usage_event_tenant_occurred_idx`` on
    ``(tenant_id, occurred_at)`` for tenant-scoped window reads.
  * ``request_usage_event_occurred_at_idx`` on ``occurred_at`` so the
    retention delete (``occurred_at < cutoff``) is an index range scan.

Revision number
---------------

This is ``0106`` and it follows ``0104``. A sibling change adds ``0105``
at the same time. Whichever of the two merges second must point its
``down_revision`` at the other one, so the chain keeps a single head.

Reversibility
-------------

Purely additive: a new table and its indexes. An older image never reads
the table, so a rollback is safe. ``downgrade()`` drops the indexes and
then the table, which discards the stored usage events.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0106"
down_revision: str | None = "0104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "request_usage_event"

#: Frozen snapshots of the closed value sets in
#: :mod:`meho_backplane.request_usage.schema`. A drift test pins them.
_KINDS: tuple[str, ...] = ("request_finished", "context_selected")
_TRANSPORTS: tuple[str, ...] = ("rest", "mcp")
_OUTCOMES: tuple[str, ...] = (
    "ok",
    "client_error",
    "denied",
    "not_found",
    "rate_limited",
    "server_error",
)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Create ``request_usage_event`` and its two indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("principal_sub", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("transport", sa.Text(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Numeric(12, 2), nullable=True),
        sa.CheckConstraint(_check_in("kind", _KINDS), name="ck_request_usage_event_kind"),
        sa.CheckConstraint(
            _check_in("transport", _TRANSPORTS),
            name="ck_request_usage_event_transport",
        ),
        sa.CheckConstraint(
            f"outcome IS NULL OR {_check_in('outcome', _OUTCOMES)}",
            name="ck_request_usage_event_outcome",
        ),
    )
    op.create_index(
        "request_usage_event_tenant_occurred_idx",
        _TABLE,
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "request_usage_event_occurred_at_idx",
        _TABLE,
        ["occurred_at"],
    )


def downgrade() -> None:
    """Drop the indexes, then the table."""
    op.drop_index("request_usage_event_occurred_at_idx", table_name=_TABLE)
    op.drop_index("request_usage_event_tenant_occurred_idx", table_name=_TABLE)
    op.drop_table(_TABLE)
