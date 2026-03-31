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

Total: 179 operations
"""

from .compute import COMPUTE_OPERATIONS
from .storage import STORAGE_OPERATIONS
from .networking import NETWORKING_OPERATIONS
from .monitoring import MONITORING_OPERATIONS
from .inventory import INVENTORY_OPERATIONS
from .system import SYSTEM_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
# Format: YYYY.MM.DD.revision (e.g., "2024.12.06.1")
VMWARE_OPERATIONS_VERSION = "2024.12.06.3"  # Added key_metrics_summary to response format

# Combined list of all VMware operations
VMWARE_OPERATIONS = (
    COMPUTE_OPERATIONS +
    STORAGE_OPERATIONS +
    NETWORKING_OPERATIONS +
    MONITORING_OPERATIONS +
    INVENTORY_OPERATIONS +
    SYSTEM_OPERATIONS
)

__all__ = [
    'VMWARE_OPERATIONS',
    'VMWARE_OPERATIONS_VERSION',
    'COMPUTE_OPERATIONS',
    'STORAGE_OPERATIONS',
    'NETWORKING_OPERATIONS',
    'MONITORING_OPERATIONS',
    'INVENTORY_OPERATIONS',
    'SYSTEM_OPERATIONS',
]

