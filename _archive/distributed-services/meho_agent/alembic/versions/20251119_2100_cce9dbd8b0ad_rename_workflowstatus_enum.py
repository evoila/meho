"""rename_workflowstatus_enum

Ensure the database enum backing agent_plan.status matches the
PlanStatus name used in SQLAlchemy models (planstatus).

Revision ID: cce9dbd8b0ad
Revises: b1c2d3e4f5g6
Create Date: 2025-11-19 21:00:00.000000
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "cce9dbd8b0ad"
down_revision = "c1d2e3f4g5h6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename old workflowstatus enum if that's what exists
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'workflowstatus')
               AND NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'planstatus') THEN
                ALTER TYPE workflowstatus RENAME TO planstatus;
            ELSIF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'planstatus') THEN
                CREATE TYPE planstatus AS ENUM (
                    'PLANNING',
                    'WAITING_APPROVAL',
                    'RUNNING',
                    'COMPLETED',
                    'FAILED',
                    'CANCELLED'
                );
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    # Restore original workflowstatus enum name to match earlier migrations
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'planstatus')
               AND NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'workflowstatus') THEN
                ALTER TYPE planstatus RENAME TO workflowstatus;
            END IF;
        END
        $$;
        """
    )

