"""
Approval System for MEHO Agent

TASK-76: Human-in-the-loop approval flow for risky API operations.

This module provides:
- ApprovalRequired exception for tool-level interception
- ApprovalStore repository for persistence
- DangerLevel utilities for automatic classification
"""

from meho_agent.approval.exceptions import ApprovalRequired
from meho_agent.approval.repository import ApprovalStore
from meho_agent.approval.danger_level import (
    assign_danger_level,
    should_require_approval,
    get_impact_message,
    DangerLevel,
)

__all__ = [
    # Exception
    "ApprovalRequired",
    # Repository
    "ApprovalStore",
    # Danger level utilities
    "assign_danger_level",
    "should_require_approval",
    "get_impact_message",
    "DangerLevel",
]

