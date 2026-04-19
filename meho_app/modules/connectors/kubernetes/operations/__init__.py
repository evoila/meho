# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes Operations - Combined from Category Files

This module imports and combines all operation definitions from category files.

Categories:
- core: Pods, Nodes, Namespaces, ConfigMaps, Secrets
- workloads: Deployments, StatefulSets, DaemonSets, Jobs, CronJobs
- networking: Services, Ingresses, Endpoints
- storage: PVCs, PVs, StorageClasses
- events: Events and describe operations

Total: 40+ operations
"""

from .core import CORE_OPERATIONS
from .events import EVENTS_OPERATIONS
from .networking import NETWORKING_OPERATIONS
from .storage import STORAGE_OPERATIONS
from .workloads import WORKLOADS_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
# Format: YYYY.MM.DD.revision (e.g., "2026.01.06.1")
KUBERNETES_OPERATIONS_VERSION = (
    "2026.03.04.1"  # Force re-sync: knowledge chunks purged by connector_scoped_knowledge migration
)

# Combined list of all Kubernetes operations
KUBERNETES_OPERATIONS = (
    CORE_OPERATIONS
    + WORKLOADS_OPERATIONS
    + NETWORKING_OPERATIONS
    + STORAGE_OPERATIONS
    + EVENTS_OPERATIONS
)

__all__ = [
    "CORE_OPERATIONS",
    "EVENTS_OPERATIONS",
    "KUBERNETES_OPERATIONS",
    "KUBERNETES_OPERATIONS_VERSION",
    "NETWORKING_OPERATIONS",
    "STORAGE_OPERATIONS",
    "WORKLOADS_OPERATIONS",
]
