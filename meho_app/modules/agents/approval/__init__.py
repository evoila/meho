# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Approval System for MEHO Agent

TASK-76: Human-in-the-loop approval flow for risky API operations.
Phase 5: Three-tier trust classification (READ, WRITE, DESTRUCTIVE).

This module provides:
- ApprovalRequired exception for tool-level interception
- ApprovalStore repository for persistence
- DangerLevel utilities for automatic classification (legacy)
- TrustTier and classify_operation for three-tier trust classification (Phase 5)
"""

from meho_app.modules.agents.approval.danger_level import (
    DangerLevel,
    assign_danger_level,
    get_impact_message,
    should_require_approval,
)
from meho_app.modules.agents.approval.exceptions import ApprovalRequired
from meho_app.modules.agents.approval.repository import ApprovalStore
from meho_app.modules.agents.approval.trust_classifier import (
    classify_operation,
)
from meho_app.modules.agents.approval.trust_classifier import (
    requires_approval as trust_requires_approval,
)
from meho_app.modules.agents.models import TrustTier

__all__ = [
    # Exception
    "ApprovalRequired",
    # Repository
    "ApprovalStore",
    "DangerLevel",
    # Phase 5: Trust classification
    "TrustTier",
    # Danger level utilities (legacy)
    "assign_danger_level",
    "classify_operation",
    "get_impact_message",
    "should_require_approval",
    "trust_requires_approval",
]
