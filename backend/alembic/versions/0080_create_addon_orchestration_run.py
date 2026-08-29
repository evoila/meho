# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``addon_orchestration_run`` table (#3028).

The out-of-process parent-linkage anchor for a paired add-on's orchestration
run (Initiative #2900, scope item 4). #2086 gave *in-process* dispatches a
replay subtree by re-binding ``parent_audit_id`` / ``agent_session_id`` before
a resumed dispatch. An external orchestrator (a paired add-on) has no
in-process parent audit row to hang its per-step dispatches off, so this table
holds one **synthesized** anchor per ``(keycloak_client_id, work_ref)``: a
stable ``session_id`` (the replay anchor) plus the ``anchor_audit_id`` of the
orchestration-root ``audit_log`` row written when the run is first opened. Each
subsequent dispatch for that work_ref binds those two values, so its DISPATCH
audit row groups under the one subtree.

Keyed by ``(keycloak_client_id, work_ref)`` — the isolation boundary that makes
linkage acceptable "only from the paired principal for its own work_refs": a
different principal presenting the same work_ref string resolves its own row
(keyed by its own clientId), never another add-on's subtree.

Additive-only (migration-compat contract): a fresh table plus one unique
index, no ALTER of an existing object.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "addon_orchestration_run",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        # The paired principal's OAuth clientId (== addon_pairing.keycloak_client_id).
        sa.Column("keycloak_client_id", sa.Text(), nullable=False),
        # The external change-ticket ref the orchestration groups under.
        sa.Column("work_ref", sa.Text(), nullable=False),
        # Synthesized replay anchor: the agent_session_id every row of the run
        # carries, so /audit/sessions/{id}/replay reconstructs the subtree.
        sa.Column("session_id", sa.Uuid(), nullable=False),
        # The id of the orchestration-root audit_log row (parent_audit_id target).
        sa.Column("anchor_audit_id", sa.Uuid(), nullable=False),
        # The service-account sub that first opened the run (audit provenance).
        sa.Column("opened_by_sub", sa.Text(), nullable=False),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
    )

    # Race-safe resolve-or-open + the per-principal isolation boundary: one
    # anchor per (clientId, work_ref). Globally unique — a Keycloak clientId is
    # global, so it is never namespaced by tenant.
    op.create_index(
        "addon_orchestration_run_client_work_ref_idx",
        "addon_orchestration_run",
        ["keycloak_client_id", "work_ref"],
        unique=True,
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index(
        "addon_orchestration_run_client_work_ref_idx",
        table_name="addon_orchestration_run",
    )
    op.drop_table("addon_orchestration_run")
