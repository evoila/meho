# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Connector Module.

Provides ArgoConnector class with 10 operations for ArgoCD REST API:
- Applications: list_applications, get_application
- Resources: get_resource_tree, get_managed_resources, get_application_events
- History: get_sync_history, get_revision_metadata
- Diff: get_server_diff
- Sync: sync_application, rollback_application

Bearer token authentication with configurable SSL verification.
"""

from meho_app.modules.connectors.argocd.connector import ArgoConnector
from meho_app.modules.connectors.argocd.operations import (
    ARGOCD_OPERATIONS,
    ARGOCD_OPERATIONS_VERSION,
    DESTRUCTIVE_OPERATIONS,
    WRITE_OPERATIONS,
)

# Empty type list -- ArgoCD entities are handled via topology schema
ARGOCD_TYPES: list = []

__all__ = [
    "ARGOCD_OPERATIONS",
    "ARGOCD_OPERATIONS_VERSION",
    "ARGOCD_TYPES",
    "DESTRUCTIVE_OPERATIONS",
    "WRITE_OPERATIONS",
    "ArgoConnector",
]
