# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SQLAlchemy models for the Audit module.

AuditEvent records who did what, when, and the result for compliance
and security audit trails.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from meho_app.database import Base


class AuditEvent(Base):  # type: ignore[misc,valid-type]
    """
    Audit event record.

    Captures write/destructive operations and authentication events
    with who, when, what, and result for SOC 2 compliance evidence.

    Table: audit_event
    """

    __tablename__ = "audit_event"

    # Primary key
    id = sa.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Multi-tenancy & user attribution
    tenant_id = sa.Column(sa.String, nullable=False, index=True)
    user_id = sa.Column(sa.String, nullable=False, index=True)
    user_email = sa.Column(sa.String, nullable=True)

    # Event classification
    event_type = sa.Column(
        sa.String(50), nullable=False
    )  # e.g. 'connector.create', 'auth.login', 'knowledge.delete'
    action = sa.Column(
        sa.String(50), nullable=False
    )  # e.g. 'create', 'update', 'delete', 'login', 'logout', 'approve', 'deny'
    resource_type = sa.Column(
        sa.String(50), nullable=False
    )  # e.g. 'connector', 'knowledge_doc', 'config', 'session'

    # Resource identification
    resource_id = sa.Column(sa.String, nullable=True)
    resource_name = sa.Column(sa.String, nullable=True)

    # Extra context (changes, metadata)
    details = sa.Column(JSONB, nullable=True)

    # Outcome
    result = sa.Column(
        sa.String(20), nullable=False, server_default="success"
    )  # 'success', 'failure', 'error'

    # Request metadata
    ip_address = sa.Column(sa.String(45), nullable=True)  # IPv4 or IPv6
    user_agent = sa.Column(sa.String, nullable=True)

    # Timestamp
    created_at = sa.Column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        sa.Index("ix_audit_event_tenant_created", "tenant_id", "created_at"),
        sa.Index("ix_audit_event_user_created", "user_id", "created_at"),
        sa.Index("ix_audit_event_type", "event_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditEvent(id={self.id}, event_type={self.event_type!r}, "
            f"action={self.action!r}, user_id={self.user_id!r})>"
        )
