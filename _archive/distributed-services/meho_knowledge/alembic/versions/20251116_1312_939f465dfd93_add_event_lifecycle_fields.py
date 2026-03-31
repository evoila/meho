"""Create or backfill knowledge_chunk table with lifecycle metadata.

This migration handles both:
- Creating the table if it doesn't exist (fresh deployments)
- Backfilling lifecycle fields if the table already exists (manual setups)

Key features:
- Time-based expiration for webhook events
- Prevents knowledge base bloat
- Improved search ranking

Revision ID: 939f465dfd93
Revises:
Create Date: 2025-11-16 13:12:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '939f465dfd93'
down_revision = None
branch_labels = None
depends_on = None

TABLE_NAME = "knowledge_chunk"


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _index_exists(inspector, table_name: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    """
    Ensure the core knowledge_chunk table exists with all lifecycle fields.
    This migration doubles as a repair step for environments that created
    the table manually without Alembic.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, TABLE_NAME):
        op.create_table(
            TABLE_NAME,
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column('tenant_id', sa.String(), nullable=True),
            sa.Column('system_id', sa.String(), nullable=True),
            sa.Column('user_id', sa.String(), nullable=True),
            sa.Column('roles', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
            sa.Column('groups', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
            sa.Column('text', sa.Text(), nullable=False),
            sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
            sa.Column('source_uri', sa.Text(), nullable=True),
            sa.Column('expires_at', sa.TIMESTAMP(), nullable=True),
            sa.Column('knowledge_type', sa.String(50), nullable=False, server_default='documentation'),
            sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('updated_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        )

        # Single-column indexes
        op.create_index('ix_knowledge_chunk_tenant_id', TABLE_NAME, ['tenant_id'])
        op.create_index('ix_knowledge_chunk_system_id', TABLE_NAME, ['system_id'])
        op.create_index('ix_knowledge_chunk_user_id', TABLE_NAME, ['user_id'])

        # Composite indexes used by queries
        op.create_index('ix_knowledge_chunk_tenant_system', TABLE_NAME, ['tenant_id', 'system_id'])
        op.create_index('ix_knowledge_chunk_tenant_user', TABLE_NAME, ['tenant_id', 'user_id'])

        # Lifecycle indexes
        op.create_index('idx_knowledge_expires_at', TABLE_NAME, ['expires_at'])
        op.create_index('idx_knowledge_type', TABLE_NAME, ['knowledge_type'])
        op.create_index('idx_knowledge_type_expires', TABLE_NAME, ['knowledge_type', 'expires_at'])
        return

    # Table exists (likely created outside Alembic); backfill missing columns/indexes.
    columns = {col["name"] for col in inspector.get_columns(TABLE_NAME)}

    if 'expires_at' not in columns:
        op.add_column(TABLE_NAME, sa.Column('expires_at', sa.TIMESTAMP(), nullable=True))
    if 'knowledge_type' not in columns:
        op.add_column(
            TABLE_NAME,
            sa.Column('knowledge_type', sa.String(50), nullable=False, server_default='documentation'),
        )
    if 'priority' not in columns:
        op.add_column(
            TABLE_NAME,
            sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
        )

    # Ensure lifecycle indexes exist
    if not _index_exists(inspector, TABLE_NAME, 'idx_knowledge_expires_at'):
        op.create_index('idx_knowledge_expires_at', TABLE_NAME, ['expires_at'])
    if not _index_exists(inspector, TABLE_NAME, 'idx_knowledge_type'):
        op.create_index('idx_knowledge_type', TABLE_NAME, ['knowledge_type'])
    if not _index_exists(inspector, TABLE_NAME, 'idx_knowledge_type_expires'):
        op.create_index('idx_knowledge_type_expires', TABLE_NAME, ['knowledge_type', 'expires_at'])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, TABLE_NAME):
        return

    for index_name in [
        'idx_knowledge_type_expires',
        'idx_knowledge_type',
        'idx_knowledge_expires_at',
        'ix_knowledge_chunk_tenant_user',
        'ix_knowledge_chunk_tenant_system',
        'ix_knowledge_chunk_user_id',
        'ix_knowledge_chunk_system_id',
        'ix_knowledge_chunk_tenant_id',
    ]:
        if _index_exists(inspector, TABLE_NAME, index_name):
            op.drop_index(index_name, table_name=TABLE_NAME)

    op.drop_table(TABLE_NAME)
