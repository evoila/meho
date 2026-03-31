"""Add workflow_definition table for Phase 2

Revision ID: 20251122_2200
Revises: d0c745df8df7
Create Date: 2025-11-22 22:00:00.000000

This migration adds the workflow_definition table for Phase 2 implementation.
Workflow definitions are execution-only saved automation templates.

Different from agent_plan (ephemeral chat executions) and workflow_template (Phase 1).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '20251122_2200'
down_revision = 'd0c745df8df7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add workflow_definition table"""
    op.create_table(
        'workflow_definition',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('created_by', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(), nullable=True),
        sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('steps', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('parameters', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('is_public', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Add indexes for efficient queries
    op.create_index('ix_workflow_definition_tenant_id', 'workflow_definition', ['tenant_id'])
    op.create_index('ix_workflow_definition_created_by', 'workflow_definition', ['created_by'])
    op.create_index('ix_workflow_definition_category', 'workflow_definition', ['category'])
    op.create_index('ix_workflow_definition_is_public', 'workflow_definition', ['is_public'])
    
    # Add workflow_execution table for tracking execution instances
    op.create_table(
        'workflow_definition_execution',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('workflow_definition_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('session_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('parameters', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('results', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('now()')),
        sa.Column('completed_at', sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['workflow_definition_id'], ['workflow_definition.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['session_id'], ['chat_session.id'], ondelete='SET NULL')
    )
    
    # Add indexes for execution queries
    op.create_index('ix_workflow_definition_execution_workflow_definition_id', 'workflow_definition_execution', ['workflow_definition_id'])
    op.create_index('ix_workflow_definition_execution_tenant_id', 'workflow_definition_execution', ['tenant_id'])
    op.create_index('ix_workflow_definition_execution_user_id', 'workflow_definition_execution', ['user_id'])
    op.create_index('ix_workflow_definition_execution_status', 'workflow_definition_execution', ['status'])


def downgrade() -> None:
    """Remove workflow_definition table"""
    op.drop_index('ix_workflow_definition_execution_status', table_name='workflow_definition_execution')
    op.drop_index('ix_workflow_definition_execution_user_id', table_name='workflow_definition_execution')
    op.drop_index('ix_workflow_definition_execution_tenant_id', table_name='workflow_definition_execution')
    op.drop_index('ix_workflow_definition_execution_workflow_definition_id', table_name='workflow_definition_execution')
    op.drop_table('workflow_definition_execution')
    
    op.drop_index('ix_workflow_definition_is_public', table_name='workflow_definition')
    op.drop_index('ix_workflow_definition_category', table_name='workflow_definition')
    op.drop_index('ix_workflow_definition_created_by', table_name='workflow_definition')
    op.drop_index('ix_workflow_definition_tenant_id', table_name='workflow_definition')
    op.drop_table('workflow_definition')

