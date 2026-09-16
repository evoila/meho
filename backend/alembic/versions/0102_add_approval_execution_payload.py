"""Add encrypted one-time credential approval payloads.

Revision ID: 0102
Revises: 0101

Credential-write approval rows retain only a random opaque handle. Their
execution input is encrypted in a separate, tenant-bound table and removed
when a request is rejected, expires, or is claimed for execution. Historical
rows remain readable as-is: this migration never attempts to backfill or
reinterpret plaintext parked before the custody boundary existed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = depends_on = None


def upgrade():
    op.add_column("approval_request", sa.Column("execution_handle", sa.Uuid(), nullable=True))
    # SQLite cannot ALTER TABLE ADD CONSTRAINT. A unique index has identical
    # NULL semantics for this nullable opaque handle and works on both DBs.
    op.create_index(
        "uq_approval_request_execution_handle",
        "approval_request",
        ["execution_handle"],
        unique=True,
    )
    op.create_table(
        "approval_execution_payload",
        sa.Column("handle", sa.Uuid(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Uuid(),
            sa.ForeignKey("approval_request.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
    )


def downgrade():
    op.drop_table("approval_execution_payload")
    op.drop_index("uq_approval_request_execution_handle", table_name="approval_request")
    op.drop_column("approval_request", "execution_handle")
