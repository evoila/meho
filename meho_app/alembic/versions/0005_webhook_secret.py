# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Add webhook_secret column to connector table.

Stores an optional HMAC-SHA256 secret for verifying inbound webhook
signatures. When set, the ingestion webhook handler requires a valid
``X-Webhook-Signature`` header. When NULL (default), webhooks are
accepted without signature verification for backward compatibility.

Revision ID: 0005_webhook_secret
Revises: 0004_webhook_to_event
Create Date: 2026-03-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_webhook_secret"
down_revision = "0004_webhook_to_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("connector", sa.Column("webhook_secret", sa.String(256), nullable=True))


def downgrade() -> None:
    op.drop_column("connector", "webhook_secret")
