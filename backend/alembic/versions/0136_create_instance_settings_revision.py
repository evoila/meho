# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create ``instance_settings_revision`` -- the settings-file import history.

Revision ID: 0136
Revises: 0135
Create Date: 2026-11-02

One row per applied ``POST /api/v1/settings/import`` that changed something
(an unchanged file writes no row). Columns:

* ``id`` / ``ts`` -- the revision's identity and wall-clock time.
* ``actor_sub`` -- NOT NULL; the human operator who ran the import (machine
  principals cannot import).
* ``audit_id`` -- soft FK to the import request's ``audit_log`` row. (The
  ``tenant_convention_history`` rows the import writes link by ``request_id``.)
* ``file_sha256`` / ``api_version`` / ``exported_from`` -- what was imported:
  the digest of the file bytes, its ``apiVersion``, and its informational
  ``metadata.exportedFrom`` block (JSON, nullable).
* ``spec_before`` / ``spec_after`` -- the ``spec`` of the touched tenants
  before and after the import (JSON). The diff is worked out on read.
* ``summary`` -- added / changed counts per section (JSON).

Append-only at the datastore
----------------------------

Copies the ``audit_log`` trigger from revision ``0100``: a ``plpgsql``
function that ``RAISE``s plus a ``BEFORE UPDATE OR DELETE ... FOR EACH ROW``
trigger, PostgreSQL only. SQLite (the dev/test lane) cannot run plpgsql, so
the trigger DDL is skipped there and the table is append-only by convention
(the service only ever INSERTs).

Additive-only (``docs/codebase/migrations.md``): one new table, one new
function, one new trigger; nothing existing is altered.

Reversibility contract
----------------------

``downgrade()`` drops the trigger and function (PostgreSQL only), then the
index and the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0136"
down_revision: str | None = "0135"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these names itself. List them in __all__, so the code-quality
# check does not report them as unused globals. Keep a plain list of strings
# with no type annotation: the check reads only that form.
__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "instance_settings_revision",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_sub", sa.Text(), nullable=False),
        sa.Column("audit_id", sa.Uuid(), nullable=False),
        sa.Column("file_sha256", sa.Text(), nullable=False),
        sa.Column("api_version", sa.Text(), nullable=False),
        sa.Column("exported_from", _JSON, nullable=True),
        sa.Column("spec_before", _JSON, nullable=False),
        sa.Column("spec_after", _JSON, nullable=False),
        sa.Column("summary", _JSON, nullable=False),
    )
    op.create_index(
        "instance_settings_revision_ts_idx",
        "instance_settings_revision",
        ["ts"],
    )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION instance_settings_revision_reject_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'instance_settings_revision is append-only: % is not permitted',
                TG_OP;
        END;
        $$;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS instance_settings_revision_append_only "
        "ON instance_settings_revision"
    )
    op.execute(
        """
        CREATE TRIGGER instance_settings_revision_append_only
        BEFORE UPDATE OR DELETE ON instance_settings_revision
        FOR EACH ROW
        EXECUTE FUNCTION instance_settings_revision_reject_mutation();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS instance_settings_revision_append_only "
            "ON instance_settings_revision"
        )
        op.execute("DROP FUNCTION IF EXISTS instance_settings_revision_reject_mutation()")
    op.drop_index("instance_settings_revision_ts_idx", table_name="instance_settings_revision")
    op.drop_table("instance_settings_revision")
