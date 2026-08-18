# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``event_source`` registry table (#2880).

Revision ID: 0074
Revises: 0073
Create Date: 2026-08-18

Task #2880 under Initiative #2877. The tenant-scoped registry the inbound
webhook path (#2881, out of scope here) resolves a JWT-less sender
against: the row supplies the tenant attribution ``events.publish()``
needs a real tenant FK for, and its Vault-custodied secret is what the
ingest endpoint verifies. Moulded on ``targets`` (per-tenant, ``name``
unique via a partial index, ``secret_ref`` a nullable Vault path,
``extras`` the escape hatch, ``deleted_at`` soft-delete) with three
deliberate departures documented on the ORM model
(:class:`meho_backplane.db.models.EventSource`):

* ``tenant_id`` carries a **real** ``REFERENCES tenant(id)`` FK (the newer
  ``sensor`` / ``event_outbox`` discipline).
* ``slug`` is unique **globally**, not per-tenant -- it is the sole
  routing key in the JWT-less ingest URL.
* the admin surface writes the auth secret to Vault at ``secret_ref``
  (``targets`` only ever references an externally provisioned path); the
  row still stores only the path.

Portability idioms (from ``0064_create_sensor``): the
``is_postgres`` branch picks PG ``gen_random_uuid()`` / ``now()`` server
defaults vs SQLite ORM-side defaults, and ``extras`` is
``sa.JSON().with_variant(JSONB(), "postgresql")``. Both partial unique
indexes emit their ``WHERE deleted_at IS NULL`` predicate on **both**
dialects via the ``postgresql_where`` / ``sqlite_where`` pair (the
``0072`` / ``0073`` pattern), so the SQLite unit-test path enforces the
exact same partial-unique shape production does -- a soft-deleted
tombstone frees the ``name`` / ``slug`` slot for re-use while a live
duplicate still collides.

Additive-only forward, reversible back
--------------------------------------

``upgrade()`` only creates a table + two indexes -- purely additive, so it
clears ``scripts/ci/check_migration_compat.py`` and an older image reading
the newer schema simply never references the table (the ``helm rollback``
= image-revert + forward-compat-schema contract, ``docs/codebase/migrations.md``
/ #1607, holds). ``downgrade()`` drops the indexes then the table; a plain
``op.drop_table`` needs no ``batch_alter_table`` (SQLite drops whole
tables natively), and it is dev-time symmetry only -- production never
runs ``alembic downgrade``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_SOURCE_KINDS: tuple[str, ...] = (
    "alertmanager",
    "grafana",
    "vcf-operations",
    "harbor",
    "generic-json",
)
_EVENT_SOURCE_AUTH_STRATEGIES: tuple[str, ...] = (
    "hmac-sha256",
    "static-header",
    "basic",
)
_EVENT_SOURCE_STATUSES: tuple[str, ...] = ("active", "paused")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Create the ``event_source`` table + its two partial unique indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    not_null_json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    op.create_table(
        "event_source",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("auth_strategy", sa.Text(), nullable=False),
        sa.Column("secret_ref", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("extras", not_null_json, nullable=False),
        sa.Column("created_by_sub", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_check_in("kind", _EVENT_SOURCE_KINDS), name="ck_event_source_kind"),
        sa.CheckConstraint(
            _check_in("auth_strategy", _EVENT_SOURCE_AUTH_STRATEGIES),
            name="ck_event_source_auth_strategy",
        ),
        sa.CheckConstraint(
            _check_in("status", _EVENT_SOURCE_STATUSES), name="ck_event_source_status"
        ),
    )

    # Partial unique on live rows only: (tenant_id, name). A soft-deleted
    # tombstone frees the name for re-use; a live duplicate still collides.
    op.create_index(
        "event_source_tenant_name_idx",
        "event_source",
        ["tenant_id", "name"],
        unique=True,
        postgresql_using="btree",
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )
    # Global partial unique on slug -- the JWT-less ingest routing key.
    op.create_index(
        "event_source_slug_idx",
        "event_source",
        ["slug"],
        unique=True,
        postgresql_using="btree",
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Drop the indexes then the ``event_source`` table (dev-time symmetry)."""
    op.drop_index("event_source_slug_idx", table_name="event_source")
    op.drop_index("event_source_tenant_name_idx", table_name="event_source")
    op.drop_table("event_source")
