# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``addon_capability`` table (#3026).

The capability-advertisement plane on top of the #3025 pairing (Initiative
#2900). One row is one surface a paired add-on advertises — a meta-tool
family, CLI verb family, console panel, or event kind — owned by an
``addon_pairing`` row and declared against that pairing's negotiated
integration-contract version.

``pairing_id`` carries ``ON DELETE CASCADE`` so unpair (which hard-deletes
the pairing row) removes its capability rows in the same operation — no dead
surfaces. The ``kind`` CHECK pins the versioned capability vocabulary at
rest; the API rejects unknown kinds earlier with a 422.

Additive-only (migration-compat contract): a fresh table plus its CHECK and
one unique index, no ALTER of an existing object.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# Merge-order ledger: final slot 0081 in the chain 0079→0080→0081→0082→0083;
# down_revision re-points from "0079" to "0080" (#3201) at merge time (orchestrator ledger).
revision: str = "0081"
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "addon_capability",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "pairing_id",
            sa.Uuid(),
            sa.ForeignKey("addon_pairing.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("display_label", sa.Text(), nullable=True),
        sa.Column("declared_contract_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.CheckConstraint(
            "kind IN ('meta_tool_family', 'cli_verb_family', 'console_panel', 'event_kind')",
            name="ck_addon_capability_kind",
        ),
    )

    # One row per advertised surface per pairing; also the per-pairing lookup.
    op.create_index(
        "addon_capability_pairing_kind_name_idx",
        "addon_capability",
        ["pairing_id", "kind", "name"],
        unique=True,
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index(
        "addon_capability_pairing_kind_name_idx",
        table_name="addon_capability",
    )
    op.drop_table("addon_capability")
