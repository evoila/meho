"""create_workflow_templates

Revision ID: c1d2e3f4g5h6
Revises: b1c2d3e4f5g6
Create Date: 2025-11-18 15:10:00.000000

Creates tables for REAL workflows (reusable automation templates).

This is separate from agent_plan which are ephemeral execution plans.
Workflows are persistent, reusable automation saved to a library.

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'c1d2e3f4g5h6'
down_revision = 'b1c2d3e4f5g6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if tables exist
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()
    
    # Create workflow_template table
    if 'workflow_template' not in existing_tables:
        op.create_table('workflow_template',
            sa.Column('id', sa.UUID(), nullable=False),
            sa.Column('tenant_id', sa.String(), nullable=False),
            sa.Column('created_by', sa.String(), nullable=False),
            sa.Column('name', sa.String(), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('category', sa.String(), nullable=True),
            sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column('plan_template', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column('parameters', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column('is_public', sa.Boolean(), nullable=False, server_default='false'),
            sa.Column('shared_with_groups', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column('execution_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_executed_at', sa.TIMESTAMP(), nullable=True),
            sa.Column('last_executed_by', sa.String(), nullable=True),
            sa.Column('created_at', sa.TIMESTAMP(), nullable=False),
            sa.Column('updated_at', sa.TIMESTAMP(), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_workflow_template_tenant_id'), 'workflow_template', ['tenant_id'], unique=False)
        op.create_index(op.f('ix_workflow_template_name'), 'workflow_template', ['name'], unique=False)
        op.create_index(op.f('ix_workflow_template_category'), 'workflow_template', ['category'], unique=False)
    
    # Create workflow_execution table
    if 'workflow_execution' not in existing_tables:
        op.create_table('workflow_execution',
            sa.Column('id', sa.UUID(), nullable=False),
            sa.Column('template_id', sa.UUID(), nullable=False),
            sa.Column('session_id', sa.UUID(), nullable=True),
            sa.Column('tenant_id', sa.String(), nullable=False),
            sa.Column('user_id', sa.String(), nullable=False),
            sa.Column('parameters', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column('plan_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column('status', sa.Enum('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', name='executionstatus'), nullable=False),
            sa.Column('result_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('created_at', sa.TIMESTAMP(), nullable=False),
            sa.Column('completed_at', sa.TIMESTAMP(), nullable=True),
            sa.ForeignKeyConstraint(['template_id'], ['workflow_template.id'], ),
            sa.ForeignKeyConstraint(['session_id'], ['chat_session.id'], ),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_workflow_execution_template_id'), 'workflow_execution', ['template_id'], unique=False)
        op.create_index(op.f('ix_workflow_execution_session_id'), 'workflow_execution', ['session_id'], unique=False)
        op.create_index(op.f('ix_workflow_execution_tenant_id'), 'workflow_execution', ['tenant_id'], unique=False)


def downgrade() -> None:
    # Drop workflow_execution table
    op.drop_index(op.f('ix_workflow_execution_tenant_id'), table_name='workflow_execution')
    op.drop_index(op.f('ix_workflow_execution_session_id'), table_name='workflow_execution')
    op.drop_index(op.f('ix_workflow_execution_template_id'), table_name='workflow_execution')
    op.drop_table('workflow_execution')
    op.execute('DROP TYPE IF EXISTS executionstatus')
    
    # Drop workflow_template table
    op.drop_index(op.f('ix_workflow_template_category'), table_name='workflow_template')
    op.drop_index(op.f('ix_workflow_template_name'), table_name='workflow_template')
    op.drop_index(op.f('ix_workflow_template_tenant_id'), table_name='workflow_template')
    op.drop_table('workflow_template')

