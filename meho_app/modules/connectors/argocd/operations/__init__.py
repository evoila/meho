# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Operations - Combined from Category Files.

This module imports and combines all operation definitions from category files.

Categories:
- applications: list_applications, get_application (2 operations)
- resources: get_resource_tree, get_managed_resources, get_application_events (3 operations)
- history: get_sync_history, get_revision_metadata (2 operations)
- diff: get_server_diff (1 operation)
- sync: sync_application, rollback_application (2 operations)

Total: 10 operations (8 READ + 1 WRITE + 1 DESTRUCTIVE)
"""

from .applications import APPLICATION_OPERATIONS
from .diff import DIFF_OPERATIONS
from .history import HISTORY_OPERATIONS
from .resources import RESOURCE_OPERATIONS
from .sync_ops import SYNC_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
ARGOCD_OPERATIONS_VERSION = "2026.03.09.1"

# Combined tuple of all ArgoCD operations
ARGOCD_OPERATIONS = tuple(
    APPLICATION_OPERATIONS
    + RESOURCE_OPERATIONS
    + HISTORY_OPERATIONS
    + DIFF_OPERATIONS
    + SYNC_OPERATIONS
)

# Operation IDs that require WRITE trust (used during sync registration)
WRITE_OPERATIONS = {"sync_application"}

# Operation IDs that require DESTRUCTIVE trust
DESTRUCTIVE_OPERATIONS = {"rollback_application"}

__all__ = [
    "APPLICATION_OPERATIONS",
    "ARGOCD_OPERATIONS",
    "ARGOCD_OPERATIONS_VERSION",
    "DESTRUCTIVE_OPERATIONS",
    "DIFF_OPERATIONS",
    "HISTORY_OPERATIONS",
    "RESOURCE_OPERATIONS",
    "SYNC_OPERATIONS",
    "WRITE_OPERATIONS",
]
