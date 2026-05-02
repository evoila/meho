# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Add doc_version + knowledge_document_family + family_id + uniqueness indexes.

Bundles the document-versioning schema atomically so doc_version columns
land alongside the family table and uniqueness indexes that depend on
them. A partial deploy would otherwise leave the DB inconsistent --
columns present but no uniqueness enforcement, or vice versa.

Bundled changes:

* ``knowledge_chunk.doc_version`` and ``ingestion_jobs.doc_version``
  columns + partial index ``ix_knowledge_chunk_doc_version``.
* ``knowledge_document_family`` table groups ingestion jobs that
  represent different versions of the same logical document.
* ``knowledge_chunk.family_id`` and ``ingestion_jobs.family_id`` foreign
  keys + partial indexes.
* Uniqueness indexes on ``(family_id, doc_version)`` and
  ``(family_id, file_hash)`` (excluding deleted jobs) plus
  ``(tenant, scope, connector, name)`` on the family table using
  ``NULLS NOT DISTINCT`` (PostgreSQL 15+).

Revision ID: 0009_doc_family
Revises: 0008_jobs_summary
Create Date: 2026-04-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_doc_family"
down_revision = "0008_jobs_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_chunk",
        sa.Column("doc_version", sa.String(100), nullable=True),
    )
    op.create_index(
        "ix_knowledge_chunk_doc_version",
        "knowledge_chunk",
        ["tenant_id", "doc_version"],
        postgresql_where="doc_version IS NOT NULL",
    )

    op.add_column(
        "ingestion_jobs",
        sa.Column("doc_version", sa.String(100), nullable=True),
    )

    op.create_table(
        "knowledge_document_family",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("scope_type", sa.String(20), nullable=False),
        sa.Column(
            "connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connector.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("connector_type_scope", sa.String(100), nullable=True),
        sa.Column("knowledge_type", sa.String(50), nullable=False, server_default="documentation"),
        sa.Column("tags", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_by_user_id", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    op.create_index(
        "ix_document_family_tenant",
        "knowledge_document_family",
        ["tenant_id"],
    )
    op.create_index(
        "ix_document_family_scope",
        "knowledge_document_family",
        ["tenant_id", "scope_type", "connector_type_scope", "connector_id"],
    )
    # NULLS NOT DISTINCT requires PostgreSQL 15+. docker-compose.yml pins
    # pgvector/pgvector:pg15 so this is safe in the supported deployment.
    op.execute(
        """
        CREATE UNIQUE INDEX ux_document_family_scope_name
        ON knowledge_document_family (
            tenant_id, scope_type, connector_id, connector_type_scope, name
        )
        NULLS NOT DISTINCT
        """
    )

    op.add_column(
        "ingestion_jobs",
        sa.Column(
            "family_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_document_family.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_ingestion_jobs_family",
        "ingestion_jobs",
        ["family_id"],
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_ingestion_jobs_family_version
        ON ingestion_jobs (family_id, doc_version)
        WHERE family_id IS NOT NULL
          AND doc_version IS NOT NULL
          AND status <> 'deleted'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_ingestion_jobs_family_hash
        ON ingestion_jobs (family_id, file_hash)
        WHERE family_id IS NOT NULL
          AND file_hash IS NOT NULL
          AND status <> 'deleted'
        """
    )

    op.add_column(
        "knowledge_chunk",
        sa.Column(
            "family_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_document_family.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_knowledge_chunk_family",
        "knowledge_chunk",
        ["tenant_id", "family_id"],
        postgresql_where="family_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunk_family", table_name="knowledge_chunk")
    op.drop_column("knowledge_chunk", "family_id")

    op.execute("DROP INDEX IF EXISTS ux_ingestion_jobs_family_hash")
    op.execute("DROP INDEX IF EXISTS ux_ingestion_jobs_family_version")
    op.drop_index("ix_ingestion_jobs_family", table_name="ingestion_jobs")
    op.drop_column("ingestion_jobs", "family_id")

    op.execute("DROP INDEX IF EXISTS ux_document_family_scope_name")
    op.drop_index("ix_document_family_scope", table_name="knowledge_document_family")
    op.drop_index("ix_document_family_tenant", table_name="knowledge_document_family")
    op.drop_table("knowledge_document_family")

    op.drop_column("ingestion_jobs", "doc_version")
    op.drop_index("ix_knowledge_chunk_doc_version", table_name="knowledge_chunk")
    op.drop_column("knowledge_chunk", "doc_version")
