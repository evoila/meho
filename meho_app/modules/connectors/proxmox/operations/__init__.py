# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox Operations - Combined from Category Files

This module imports and combines all operation definitions from category files.

Categories:
- compute: VM and container lifecycle operations
- nodes: Node (host) operations
- storage: Storage pool operations

Total: ~40 operations
"""

from .compute import COMPUTE_OPERATIONS
from .nodes import NODE_OPERATIONS
from .storage import STORAGE_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
# Format: YYYY.MM.DD.revision
PROXMOX_OPERATIONS_VERSION = (
    "2026.03.04.1"  # Force re-sync: knowledge chunks purged by connector_scoped_knowledge migration
)

# Combined list of all Proxmox operations
PROXMOX_OPERATIONS = COMPUTE_OPERATIONS + NODE_OPERATIONS + STORAGE_OPERATIONS

__all__ = [
    "COMPUTE_OPERATIONS",
    "NODE_OPERATIONS",
    "PROXMOX_OPERATIONS",
    "PROXMOX_OPERATIONS_VERSION",
    "STORAGE_OPERATIONS",
]
