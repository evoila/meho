"""
Add VMware connector support (TASK-97)

- Add connector_type column to connector table
- Create connector_operation table for typed connector operations
- Create connector_type table for typed connector entity types

Revision ID: 8c3f2b9a1d5e
Revises: c3a9ea52c75d
Create Date: 2025-12-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'add_vmware_connector_97'
down_revision = 'add_soap_tables_96'  # Chain from 20251204_add_soap_tables.py
branch_labels = None
depends_on = None


def upgrade():
    # Add connector_type column to connector table
    op.add_column(
        'connector',
        sa.Column('connector_type', sa.String(), nullable=False, server_default='rest')
    )
    
    # Create connector_operation table
    op.create_table(
        'connector_operation',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('connector_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('operation_id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(), nullable=True),
        sa.Column('parameters', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('example', sa.String(), nullable=True),
        sa.Column('search_content', sa.Text(), nullable=True),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('safety_level', sa.String(), nullable=False, server_default='safe'),
        sa.Column('requires_approval', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['connector_id'], ['connector.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create indexes for connector_operation
    op.create_index('ix_conn_op_connector', 'connector_operation', ['connector_id'])
    op.create_index('ix_conn_op_connector_operation', 'connector_operation', ['connector_id', 'operation_id'])
    op.create_index('ix_conn_op_tenant', 'connector_operation', ['tenant_id'])
    op.create_index('ix_conn_op_category', 'connector_operation', ['connector_id', 'category'])
    
    # Create connector_type table
    op.create_table(
        'connector_type',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('connector_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('type_name', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(), nullable=True),
        sa.Column('properties', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('search_content', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['connector_id'], ['connector.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create indexes for connector_type
    op.create_index('ix_conn_type_connector', 'connector_type', ['connector_id'])
    op.create_index('ix_conn_type_connector_name', 'connector_type', ['connector_id', 'type_name'])
    op.create_index('ix_conn_type_tenant', 'connector_type', ['tenant_id'])


def downgrade():
    # Drop connector_type table
    op.drop_index('ix_conn_type_tenant', table_name='connector_type')
    op.drop_index('ix_conn_type_connector_name', table_name='connector_type')
    op.drop_index('ix_conn_type_connector', table_name='connector_type')
    op.drop_table('connector_type')
    
    # Drop connector_operation table
    op.drop_index('ix_conn_op_category', table_name='connector_operation')
    op.drop_index('ix_conn_op_tenant', table_name='connector_operation')
    op.drop_index('ix_conn_op_connector_operation', table_name='connector_operation')
    op.drop_index('ix_conn_op_connector', table_name='connector_operation')
    op.drop_table('connector_operation')
    
    # Drop connector_type column
    op.drop_column('connector', 'connector_type')

