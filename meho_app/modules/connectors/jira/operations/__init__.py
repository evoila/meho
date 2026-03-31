# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Jira Operations - Combined from Category Files.

This module imports and combines all operation definitions from category files.

Categories:
- search: search_issues, get_recent_changes, search_by_jql (3 operations)
- issues: get_issue, create_issue, add_comment, transition_issue (4 operations)
- projects: list_projects (1 operation)

Total: 8 operations
"""

from .issues import ISSUE_OPERATIONS
from .projects import PROJECT_OPERATIONS
from .search import SEARCH_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
JIRA_OPERATIONS_VERSION = "2026.03.07.1"

# Combined list of all Jira operations
JIRA_OPERATIONS = SEARCH_OPERATIONS + ISSUE_OPERATIONS + PROJECT_OPERATIONS

# Operation IDs that require WRITE trust (used during sync registration)
# search_by_jql is WRITE because arbitrary JQL is an escape hatch
WRITE_OPERATIONS = {"create_issue", "add_comment", "transition_issue", "search_by_jql"}

__all__ = [
    "ISSUE_OPERATIONS",
    "JIRA_OPERATIONS",
    "JIRA_OPERATIONS_VERSION",
    "PROJECT_OPERATIONS",
    "SEARCH_OPERATIONS",
    "WRITE_OPERATIONS",
]
