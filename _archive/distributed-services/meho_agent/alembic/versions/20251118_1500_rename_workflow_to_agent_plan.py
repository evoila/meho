"""rename_workflow_to_agent_plan

Revision ID: b1c2d3e4f5g6
Revises: a1b2c3d4e5f6
Create Date: 2025-11-18 15:00:00.000000

Clarifies terminology:
- workflow → agent_plan (ephemeral execution plans)
- workflow_step → agent_plan_step (execution steps)

This rename reflects that these are ephemeral plans created by the agent
for each chat interaction, NOT persistent reusable workflows.

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b1c2d3e4f5g6'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if we need to do the migration
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()
    
    # Rename workflow_step → agent_plan_step first (child table)
    if 'workflow_step' in existing_tables and 'agent_plan_step' not in existing_tables:
        op.rename_table('workflow_step', 'agent_plan_step')
        
        # Rename indexes
        op.execute('ALTER INDEX IF EXISTS ix_workflow_step_workflow_id RENAME TO ix_agent_plan_step_agent_plan_id')
    
    # Rename workflow → agent_plan (parent table)
    if 'workflow' in existing_tables and 'agent_plan' not in existing_tables:
        op.rename_table('workflow', 'agent_plan')
        
        # Rename indexes
        op.execute('ALTER INDEX IF EXISTS ix_workflow_tenant_id RENAME TO ix_agent_plan_tenant_id')
        op.execute('ALTER INDEX IF EXISTS ix_workflow_user_id RENAME TO ix_agent_plan_user_id')
    
    # Update foreign key column name in agent_plan_step
    if 'agent_plan_step' in inspector.get_table_names():
        # Rename the column
        op.alter_column('agent_plan_step', 'workflow_id', new_column_name='agent_plan_id')
    
    # Add new fields to agent_plan
    op.execute("""
        ALTER TABLE agent_plan 
        ADD COLUMN IF NOT EXISTS session_id UUID REFERENCES chat_session(id),
        ADD COLUMN IF NOT EXISTS requires_approval BOOLEAN DEFAULT TRUE,
        ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP
    """)
    
    # Update foreign key in chat_message
    if 'chat_message' in existing_tables:
        # Add new column
        op.execute('ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS agent_plan_id UUID')
        
        # Copy data from workflow_id to agent_plan_id
        op.execute('UPDATE chat_message SET agent_plan_id = workflow_id WHERE workflow_id IS NOT NULL')
        
        # Drop old column (keep for now for backward compatibility)
        # op.execute('ALTER TABLE chat_message DROP COLUMN IF EXISTS workflow_id')


def downgrade() -> None:
    # Remove new columns
    op.execute('ALTER TABLE agent_plan DROP COLUMN IF EXISTS session_id')
    op.execute('ALTER TABLE agent_plan DROP COLUMN IF EXISTS requires_approval')
    op.execute('ALTER TABLE agent_plan DROP COLUMN IF EXISTS approved_at')
    op.execute('ALTER TABLE chat_message DROP COLUMN IF EXISTS agent_plan_id')
    
    # Rename back: agent_plan_step → workflow_step
    op.alter_column('agent_plan_step', 'agent_plan_id', new_column_name='workflow_id')
    op.rename_table('agent_plan_step', 'workflow_step')
    op.execute('ALTER INDEX IF EXISTS ix_agent_plan_step_agent_plan_id RENAME TO ix_workflow_step_workflow_id')
    
    # Rename back: agent_plan → workflow
    op.rename_table('agent_plan', 'workflow')
    op.execute('ALTER INDEX IF EXISTS ix_agent_plan_tenant_id RENAME TO ix_workflow_tenant_id')
    op.execute('ALTER INDEX IF EXISTS ix_agent_plan_user_id RENAME TO ix_workflow_user_id')

