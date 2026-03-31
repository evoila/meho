"""fix_agent_plan_step_fk

Ensure agent_plan_step references agent_plan_id column.

Revision ID: d0c745df8df7
Revises: cce9dbd8b0ad
Create Date: 2025-11-19 21:30:00.000000
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "d0c745df8df7"
down_revision = "cce9dbd8b0ad"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename workflow_id -> agent_plan_id if necessary
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'agent_plan_step'
                  AND column_name = 'workflow_id'
            ) AND NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'agent_plan_step'
                  AND column_name = 'agent_plan_id'
            ) THEN
                ALTER TABLE agent_plan_step RENAME COLUMN workflow_id TO agent_plan_id;
            END IF;
        END
        $$;
        """
    )

    # Reset foreign key to the new column name
    op.execute("ALTER TABLE agent_plan_step DROP CONSTRAINT IF EXISTS workflow_step_workflow_id_fkey;")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM information_schema.table_constraints
                WHERE constraint_name = 'agent_plan_step_agent_plan_id_fkey'
            ) THEN
                ALTER TABLE agent_plan_step
                ADD CONSTRAINT agent_plan_step_agent_plan_id_fkey
                FOREIGN KEY (agent_plan_id) REFERENCES agent_plan(id);
            END IF;
        END
        $$;
        """
    )

    # Recreate index to target agent_plan_id
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_step_agent_plan_id;")
    op.create_index("ix_agent_plan_step_agent_plan_id", "agent_plan_step", ["agent_plan_id"])


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_plan_step_agent_plan_id;")
    op.create_index("ix_agent_plan_step_agent_plan_id", "agent_plan_step", ["workflow_id"])

    op.execute("ALTER TABLE agent_plan_step DROP CONSTRAINT IF EXISTS agent_plan_step_agent_plan_id_fkey;")
    op.execute(
        """
        ALTER TABLE agent_plan_step
        ADD CONSTRAINT workflow_step_workflow_id_fkey
        FOREIGN KEY (workflow_id) REFERENCES agent_plan(id);
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'agent_plan_step'
                  AND column_name = 'agent_plan_id'
            ) AND NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'agent_plan_step'
                  AND column_name = 'workflow_id'
            ) THEN
                ALTER TABLE agent_plan_step RENAME COLUMN agent_plan_id TO workflow_id;
            END IF;
        END
        $$;
        """
    )

