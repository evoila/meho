# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Operations - Combined from Category Files.

This module imports and combines all operation definitions from category files.

Categories:
- repositories: list_repositories (1 operation)
- commits: list_commits, compare_refs (2 operations)
- pull_requests: list_pull_requests, get_pull_request (2 operations)
- actions: list_workflow_runs, get_workflow_run, list_workflow_jobs, get_workflow_logs, rerun_failed_jobs (5 operations)
- deployments: list_deployments (1 operation)
- checks: get_commit_status (1 operation)

Total: 12 operations (11 READ + 1 WRITE)
"""

from .actions import ACTION_OPERATIONS
from .commits import COMMIT_OPERATIONS
from .deployments import DEPLOYMENT_OPERATIONS
from .pulls import PULL_OPERATIONS
from .repos import REPO_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
GITHUB_OPERATIONS_VERSION = "2026.03.09.1"

# Combined tuple of all GitHub operations
GITHUB_OPERATIONS = tuple(
    REPO_OPERATIONS
    + COMMIT_OPERATIONS
    + PULL_OPERATIONS
    + ACTION_OPERATIONS
    + DEPLOYMENT_OPERATIONS
)

# Operation IDs that require WRITE trust (used during sync registration)
WRITE_OPERATIONS = {"rerun_failed_jobs"}

# No DESTRUCTIVE operations for GitHub connector
# (rerun_failed_jobs is WRITE, not destructive -- it only re-runs, doesn't delete)

__all__ = [
    "ACTION_OPERATIONS",
    "COMMIT_OPERATIONS",
    "DEPLOYMENT_OPERATIONS",
    "GITHUB_OPERATIONS",
    "GITHUB_OPERATIONS_VERSION",
    "PULL_OPERATIONS",
    "REPO_OPERATIONS",
    "WRITE_OPERATIONS",
]
