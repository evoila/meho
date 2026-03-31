"""Add tenant agent config tables

TASK-77: Externalize Prompts & Models

Creates tables for:
- tenant_agent_config: Admin-defined installation context per tenant
- tenant_agent_config_audit: Audit log for configuration changes

Revision ID: tenant_agent_config
Revises: add_recipe_tables
Create Date: 2025-12-01 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision = 'tenant_agent_config'
down_revision = 'add_recipe_tables'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create tenant agent config tables."""
    
    # Create tenant_agent_config table
    op.create_table(
        'tenant_agent_config',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', sa.String(), nullable=False, unique=True, index=True),
        
        # Admin-defined context - added to system prompt
        sa.Column('installation_context', sa.Text(), nullable=True),
        
        # Optional overrides
        sa.Column('model_override', sa.String(100), nullable=True),
        sa.Column('temperature_override', JSONB, nullable=True),
        
        # Feature flags
        sa.Column('features', JSONB, nullable=False, server_default='{}'),
        
        # Metadata
        sa.Column('updated_by', sa.String(), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('NOW()')),
    )
    
    # Create tenant_agent_config_audit table
    op.create_table(
        'tenant_agent_config_audit',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', sa.String(), nullable=False, index=True),
        
        # What changed
        sa.Column('field_changed', sa.String(100), nullable=False),
        sa.Column('old_value', sa.Text(), nullable=True),
        sa.Column('new_value', sa.Text(), nullable=True),
        
        # Who changed it
        sa.Column('changed_by', sa.String(), nullable=False),
        sa.Column('changed_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('NOW()')),
    )


def downgrade() -> None:
    """Drop tenant agent config tables."""
    op.drop_table('tenant_agent_config_audit')
    op.drop_table('tenant_agent_config')

