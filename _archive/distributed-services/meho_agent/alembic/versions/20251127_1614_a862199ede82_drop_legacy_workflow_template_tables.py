"""drop_legacy_workflow_template_tables

Revision ID: a862199ede82
Revises: 3ed197ed1589
Create Date: 2025-11-27 16:14:10.306694

Removes legacy WorkflowTemplate implementation (Phase 1):
- Drops workflow_execution table (executions of templates)
- Drops workflow_template table (Phase 1 templates)

WorkflowDefinition (Phase 2 - execution-only) is the correct approach.
This migration is part of the architecture simplification.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'a862199ede82'
down_revision = '3ed197ed1589'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop legacy workflow_template and workflow_execution tables."""
    # Drop workflow_execution first (has FK to workflow_template)
    op.execute('DROP TABLE IF EXISTS workflow_execution CASCADE')
    
    # Drop workflow_template
    op.execute('DROP TABLE IF EXISTS workflow_template CASCADE')
    
    # Drop ExecutionStatus enum if it exists (was only used by workflow_execution)
    op.execute('DROP TYPE IF EXISTS executionstatus CASCADE')


def downgrade() -> None:
    """Downgrade not supported - WorkflowTemplate architecture eliminated.
    
    If you need to restore these tables, you must:
    1. Restore WorkflowTemplateModel and WorkflowExecutionModel to models.py
    2. Restore routes_workflow_templates.py
    3. Run the previous migration
    
    This is not recommended - WorkflowDefinition is the correct approach.
    """
    raise RuntimeError(
        "Cannot downgrade: WorkflowTemplate architecture has been eliminated. "
        "Use WorkflowDefinition (Phase 2) instead."
    )

