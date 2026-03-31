# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware Operations - Combined from Category Files

This module imports and combines all operation definitions from category files.

Categories:
- compute: VM lifecycle, host operations, cluster operations (91 ops)
- storage: Datastore, disk operations (28 ops)
- networking: Network, DVS, port groups (17 ops)
- monitoring: Performance, alarms, events (18 ops including PerformanceManager)
- inventory: Folders, templates, tags, content library (15 ops)
- system: vCenter info, licensing, tasks (10 ops)
- vsan: vSAN health, disk groups, capacity, resync, policies (6 ops) [Phase 96]
- capacity: Cluster capacity, overcommitment, datastore util, host load (4 ops) [Phase 96]
- nsx: Segments, firewall, gateways, transport nodes, search (12 ops) [Phase 96]
- sddc: Workload domains, hosts, clusters, certificates, updates (8 ops) [Phase 96]

Total: ~209 operations
"""

from .capacity import CAPACITY_OPERATIONS
from .compute import COMPUTE_OPERATIONS
from .inventory import INVENTORY_OPERATIONS
from .monitoring import MONITORING_OPERATIONS
from .networking import NETWORKING_OPERATIONS
from .nsx import NSX_OPERATIONS
from .sddc import SDDC_OPERATIONS
from .storage import STORAGE_OPERATIONS
from .system import SYSTEM_OPERATIONS
from .vsan import VSAN_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
# Format: YYYY.MM.DD.revision (e.g., "2024.12.06.1")
VMWARE_OPERATIONS_VERSION = (
    "2026.03.27.1"  # Phase 96: vSAN, NSX, SDDC, Capacity operations added
)

# Combined list of all VMware operations
VMWARE_OPERATIONS = (
    COMPUTE_OPERATIONS
    + STORAGE_OPERATIONS
    + NETWORKING_OPERATIONS
    + MONITORING_OPERATIONS
    + INVENTORY_OPERATIONS
    + SYSTEM_OPERATIONS
    + VSAN_OPERATIONS
    + CAPACITY_OPERATIONS
    + NSX_OPERATIONS
    + SDDC_OPERATIONS
)

__all__ = [
    "CAPACITY_OPERATIONS",
    "COMPUTE_OPERATIONS",
    "INVENTORY_OPERATIONS",
    "MONITORING_OPERATIONS",
    "NETWORKING_OPERATIONS",
    "NSX_OPERATIONS",
    "SDDC_OPERATIONS",
    "STORAGE_OPERATIONS",
    "SYSTEM_OPERATIONS",
    "VMWARE_OPERATIONS",
    "VMWARE_OPERATIONS_VERSION",
    "VSAN_OPERATIONS",
]
