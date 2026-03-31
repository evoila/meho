"""add_event_templates_table

Revision ID: c83443418ca1
Revises: 
Create Date: 2025-11-16 12:49:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'c83443418ca1'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Create event_templates table for generic webhook processing.
    
    Event templates define how to process webhook events:
    - text_template: Jinja2 template for text extraction
    - tag_rules: JSON array of Jinja2 expressions for tags
    - issue_detection_rule: Optional Jinja2 boolean expression
    """
    op.create_table(
        'event_templates',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('connector_id', sa.String(length=255), nullable=False),
        sa.Column('event_type', sa.String(length=255), nullable=False),
        sa.Column('text_template', sa.Text(), nullable=False),
        sa.Column('tag_rules', postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column('issue_detection_rule', sa.Text(), nullable=True),
        sa.Column('tenant_id', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create indexes for efficient lookups
    op.create_index(
        'idx_event_templates_connector_event',
        'event_templates',
        ['connector_id', 'event_type'],
        unique=True
    )
    op.create_index(
        'idx_event_templates_tenant',
        'event_templates',
        ['tenant_id'],
        unique=False
    )
    op.create_index(
        'idx_event_templates_connector',
        'event_templates',
        ['connector_id'],
        unique=False
    )


def downgrade() -> None:
    """Drop event_templates table and indexes"""
    op.drop_index('idx_event_templates_connector', table_name='event_templates')
    op.drop_index('idx_event_templates_tenant', table_name='event_templates')
    op.drop_index('idx_event_templates_connector_event', table_name='event_templates')
    op.drop_table('event_templates')
