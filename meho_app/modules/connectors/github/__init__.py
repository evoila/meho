# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Connector Module.

Provides GitHubConnector class with 12 operations for GitHub REST API:
- Repositories: list_repositories
- Commits: list_commits, compare_refs
- Pull Requests: list_pull_requests, get_pull_request
- Actions: list_workflow_runs, get_workflow_run, list_workflow_jobs, get_workflow_logs, rerun_failed_jobs
- Deployments: list_deployments
- Checks: get_commit_status

PAT Bearer token authentication with rate limit tracking.
"""

from meho_app.modules.connectors.github.connector import GitHubConnector
from meho_app.modules.connectors.github.operations import (
    GITHUB_OPERATIONS,
    GITHUB_OPERATIONS_VERSION,
    WRITE_OPERATIONS,
)

# Empty type list -- GitHub entities are handled via topology schema
GITHUB_TYPES: list = []

__all__ = [
    "GITHUB_OPERATIONS",
    "GITHUB_OPERATIONS_VERSION",
    "GITHUB_TYPES",
    "WRITE_OPERATIONS",
    "GitHubConnector",
]
