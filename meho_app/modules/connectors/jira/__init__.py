# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Jira Connector Module.

Provides JiraConnector class with 8 operations for Jira Cloud REST API v3:
- Search: search_issues, get_recent_changes, search_by_jql
- Issues: get_issue, create_issue, add_comment, transition_issue
- Projects: list_projects

Agent reads/writes markdown; ADF conversion is invisible.
Custom field names are human-readable, never customfield_XXXXX.
"""

from meho_app.modules.connectors.jira.connector import JiraConnector
from meho_app.modules.connectors.jira.operations import (
    JIRA_OPERATIONS,
    JIRA_OPERATIONS_VERSION,
    WRITE_OPERATIONS,
)

# Empty type list -- Jira issues are not topology entities
JIRA_TYPES: list = []

__all__ = [
    "JIRA_OPERATIONS",
    "JIRA_OPERATIONS_VERSION",
    "JIRA_TYPES",
    "WRITE_OPERATIONS",
    "JiraConnector",
]
