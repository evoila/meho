"""add_recipe_tables

Revision ID: add_recipe_tables
Revises: a862199ede82
Create Date: 2025-11-30 12:00:00.000000

Adds recipe and recipe_execution tables for persistent recipe storage.
Recipes are reusable Q&A patterns that capture successful interactions.

Session 80: Recipe System Persistence
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


# revision identifiers, used by Alembic.
revision = 'add_recipe_tables'
down_revision = 'a862199ede82'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create recipe table
    op.create_table(
        'recipe',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.String(), nullable=False, index=True),
        
        # Metadata
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('tags', JSONB, nullable=False, server_default='[]'),
        
        # Source information
        sa.Column('connector_id', UUID(as_uuid=True), nullable=False, index=True),
        sa.Column('endpoint_id', UUID(as_uuid=True), nullable=True, index=True),
        
        # The original question
        sa.Column('original_question', sa.Text(), nullable=False),
        
        # Parameters and query template as JSON
        sa.Column('parameters', JSONB, nullable=False, server_default='[]'),
        sa.Column('query_template', JSONB, nullable=False),
        
        # Interpretation
        sa.Column('interpretation_prompt', sa.Text(), nullable=True),
        
        # Timestamps
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()),
        
        # Usage stats
        sa.Column('execution_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_executed_at', sa.TIMESTAMP(), nullable=True),
        
        # Sharing
        sa.Column('is_public', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_by', sa.String(), nullable=True),
    )
    
    # Create recipe name uniqueness constraint per tenant
    op.create_index(
        'ix_recipe_tenant_name',
        'recipe',
        ['tenant_id', 'name'],
        unique=True
    )
    
    # Create recipe_execution status enum
    recipe_execution_status = sa.Enum(
        'pending', 'running', 'completed', 'failed',
        name='recipe_execution_status'
    )
    recipe_execution_status.create(op.get_bind(), checkfirst=True)
    
    # Create recipe_execution table
    op.create_table(
        'recipe_execution',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('recipe_id', UUID(as_uuid=True), sa.ForeignKey('recipe.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('tenant_id', sa.String(), nullable=False, index=True),
        
        # Execution parameters
        sa.Column('parameter_values', JSONB, nullable=False, server_default='{}'),
        
        # Status
        sa.Column('status', recipe_execution_status, nullable=False, server_default='pending'),
        sa.Column('error_message', sa.Text(), nullable=True),
        
        # Results
        sa.Column('result_count', sa.Integer(), nullable=True),
        sa.Column('result_summary', sa.Text(), nullable=True),
        sa.Column('aggregates', JSONB, nullable=False, server_default='{}'),
        
        # Performance
        sa.Column('started_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('completed_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        
        # Triggered by
        sa.Column('triggered_by', sa.String(), nullable=True),
    )


def downgrade() -> None:
    # Drop tables
    op.drop_table('recipe_execution')
    op.drop_table('recipe')
    
    # Drop enum
    recipe_execution_status = sa.Enum(
        'pending', 'running', 'completed', 'failed',
        name='recipe_execution_status'
    )
    recipe_execution_status.drop(op.get_bind(), checkfirst=True)

