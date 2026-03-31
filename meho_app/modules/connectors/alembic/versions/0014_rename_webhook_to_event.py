# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Rename webhook tables to event tables and add response_config.

Renames:
- webhook_registration -> event_registration
- webhook_event -> event_history
- webhook_id column -> event_registration_id (on event_history)
- All related indexes renamed accordingly

Adds:
- response_config JSONB column on event_registration (nullable)

Revision ID: connectors_0014_rename_webhook_to_event
Revises: connectors_0013_remove_delegate_credentials
Create Date: 2026-03-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "connectors_0014_rename_webhook_to_event"
down_revision = "connectors_0013_remove_delegate_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- Rename tables ----
    op.rename_table("webhook_registration", "event_registration")
    op.rename_table("webhook_event", "event_history")

    # ---- Rename column webhook_id -> event_registration_id on event_history ----
    op.alter_column("event_history", "webhook_id", new_column_name="event_registration_id")

    # ---- Drop old indexes on event_registration (formerly webhook_registration) ----
    op.drop_index("ix_webhook_reg_connector", table_name="event_registration")
    op.drop_index("ix_webhook_reg_tenant", table_name="event_registration")
    # created_by_user_id index (added in Phase 74)
    op.drop_index("ix_webhook_reg_created_by", table_name="event_registration")

    # ---- Drop old indexes on event_history (formerly webhook_event) ----
    op.drop_index("ix_webhook_event_webhook", table_name="event_history")
    op.drop_index("ix_webhook_event_created", table_name="event_history")

    # ---- Create new indexes on event_registration ----
    op.create_index("ix_event_reg_connector", "event_registration", ["connector_id"])
    op.create_index("ix_event_reg_tenant", "event_registration", ["tenant_id"])
    op.create_index("ix_event_reg_created_by", "event_registration", ["created_by_user_id"])

    # ---- Create new indexes on event_history ----
    op.create_index("ix_event_history_registration", "event_history", ["event_registration_id"])
    op.create_index("ix_event_history_created", "event_history", ["created_at"])

    # ---- Add response_config column ----
    op.add_column("event_registration", sa.Column("response_config", JSONB, nullable=True))


def downgrade() -> None:
    # ---- Drop response_config column ----
    op.drop_column("event_registration", "response_config")

    # ---- Drop new indexes on event_history ----
    op.drop_index("ix_event_history_created", table_name="event_history")
    op.drop_index("ix_event_history_registration", table_name="event_history")

    # ---- Drop new indexes on event_registration ----
    op.drop_index("ix_event_reg_created_by", table_name="event_registration")
    op.drop_index("ix_event_reg_tenant", table_name="event_registration")
    op.drop_index("ix_event_reg_connector", table_name="event_registration")

    # ---- Restore old indexes on event_history (before rename back) ----
    op.create_index("ix_webhook_event_created", "event_history", ["created_at"])
    op.create_index("ix_webhook_event_webhook", "event_history", ["event_registration_id"])

    # ---- Restore old indexes on event_registration (before rename back) ----
    op.create_index("ix_webhook_reg_created_by", "event_registration", ["created_by_user_id"])
    op.create_index("ix_webhook_reg_tenant", "event_registration", ["tenant_id"])
    op.create_index("ix_webhook_reg_connector", "event_registration", ["connector_id"])

    # ---- Rename column back ----
    op.alter_column("event_history", "event_registration_id", new_column_name="webhook_id")

    # ---- Rename tables back ----
    op.rename_table("event_history", "webhook_event")
    op.rename_table("event_registration", "webhook_registration")
