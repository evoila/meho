# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SQLAlchemy models for the Email Connector module.

EmailDeliveryLogModel tracks every email sent via any provider.
Follows the WebhookEventModel pattern from meho_app/modules/connectors/models.py.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import TIMESTAMP, Column, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from meho_app.database import Base


class EmailDeliveryLogModel(Base):  # type: ignore[misc,valid-type]
    """
    Delivery log for every email sent via the Email connector.

    Records the outcome (sent / accepted / failed), provider info,
    and links to the connector that sent it. Does NOT store the full
    email body (anti-pattern per research).
    """

    __tablename__ = "email_delivery_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(
        UUID(as_uuid=True),
        ForeignKey("connector.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id = Column(String, nullable=False, index=True)

    # Email details
    from_email = Column(String, nullable=False)
    to_emails = Column(JSONB, nullable=False)  # List of recipient addresses
    subject = Column(String(500), nullable=False)

    # Provider info
    provider_type = Column(String(20), nullable=False)  # smtp, sendgrid, mailgun, ses, generic_http
    provider_message_id = Column(String, nullable=True)  # Provider-assigned ID if available

    # Status
    status = Column(String(20), nullable=False)  # sent, accepted, failed
    error_message = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    # Relationships
    connector = relationship("ConnectorModel")

    __table_args__ = (
        Index("ix_email_log_connector", "connector_id"),
        Index("ix_email_log_tenant", "tenant_id"),
        Index("ix_email_log_created", "created_at"),
    )
