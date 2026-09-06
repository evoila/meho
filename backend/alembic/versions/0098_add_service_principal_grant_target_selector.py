# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add a target *selector* to ``service_principal_grant`` (#3349).

Two nullable columns — ``target_product`` and ``target_name_pattern`` — let
a standing grant carry a target *selector* in place of a concrete
``target_id``. A selector grant matches any dispatch whose target
fingerprint satisfies it (``product`` exact + ``name`` ``fnmatch`` glob), so
an operator can authorise an op on targets that do not yet exist — a
blueprint that registers its own appliances mid-run — without a per-target
grant. The wildcard is requested explicitly here, never implied by a NULL
``target_id`` (which stays targetless-only).

Additive columns are safe on a populated table (both nullable, no
backfill: existing rows read back NULL = "no selector, pure targetless or
targeted as before").

The ``uq_service_principal_grant_targetless`` partial unique index is
rebuilt with a narrower predicate so it covers only *pure* targetless
grants (``target_product`` and ``target_name_pattern`` both NULL). Without
the narrowing a selector grant — which also keys ``target_id IS NULL`` —
would collide with a pure targetless grant on the same
``(tenant, principal_sub, op_id, connector_id)`` key. Every pre-#3349 row
has both selector columns NULL, so the narrowed predicate covers the
existing targetless rows byte-identically. Selector-grant uniqueness is
enforced in the CRUD layer, not by a partial index (the nullable selector
columns make a portable NULL-safe unique index awkward across
Postgres / SQLite).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0098"
down_revision: str | None = "0097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "service_principal_grant",
        sa.Column("target_product", sa.Text(), nullable=True),
    )
    op.add_column(
        "service_principal_grant",
        sa.Column("target_name_pattern", sa.Text(), nullable=True),
    )

    # Rebuild the targetless partial unique index with the narrowed
    # predicate so it excludes selector grants (which also key
    # ``target_id IS NULL``).
    op.drop_index(
        "uq_service_principal_grant_targetless",
        table_name="service_principal_grant",
    )
    op.create_index(
        "uq_service_principal_grant_targetless",
        "service_principal_grant",
        ["tenant_id", "principal_sub", "op_id", "connector_id"],
        unique=True,
        postgresql_where=sa.text(
            "target_id IS NULL AND target_product IS NULL "
            "AND target_name_pattern IS NULL AND revoked_at IS NULL"
        ),
        sqlite_where=sa.text(
            "target_id IS NULL AND target_product IS NULL "
            "AND target_name_pattern IS NULL AND revoked_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_service_principal_grant_targetless",
        table_name="service_principal_grant",
    )
    op.create_index(
        "uq_service_principal_grant_targetless",
        "service_principal_grant",
        ["tenant_id", "principal_sub", "op_id", "connector_id"],
        unique=True,
        postgresql_where=sa.text("target_id IS NULL AND revoked_at IS NULL"),
        sqlite_where=sa.text("target_id IS NULL AND revoked_at IS NULL"),
    )
    op.drop_column("service_principal_grant", "target_name_pattern")
    op.drop_column("service_principal_grant", "target_product")
