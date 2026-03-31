# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Audit trail module for MEHO.

Provides compliance-ready audit logging for write/destructive operations,
authentication events, and security warnings.

Exports:
    AuditEvent: SQLAlchemy model for audit events
    AuditService: Service layer for logging, querying, and purging audit events
"""

from meho_app.modules.audit.models import AuditEvent
from meho_app.modules.audit.service import AuditService

__all__ = ["AuditEvent", "AuditService"]
