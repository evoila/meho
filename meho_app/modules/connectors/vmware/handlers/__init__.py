# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware Connector Handler Mixins

12 handler mixins covering:
- 8 existing: VM, Host, Cluster, Storage, Network, Inventory, System, Performance
- 4 new (Phase 96): vSAN, Capacity, NSX, SDDC
"""

from .capacity_handlers import CapacityHandlerMixin
from .cluster_handlers import ClusterHandlerMixin
from .host_handlers import HostHandlerMixin
from .inventory_handlers import InventoryHandlerMixin
from .network_handlers import NetworkHandlerMixin
from .nsx_handlers import NsxHandlerMixin
from .performance_handlers import PerformanceHandlerMixin
from .sddc_handlers import SddcHandlerMixin
from .storage_handlers import StorageHandlerMixin
from .system_handlers import SystemHandlerMixin
from .vm_handlers import VMHandlerMixin
from .vsan_handlers import VsanHandlerMixin

__all__ = [
    "CapacityHandlerMixin",
    "ClusterHandlerMixin",
    "HostHandlerMixin",
    "InventoryHandlerMixin",
    "NetworkHandlerMixin",
    "NsxHandlerMixin",
    "PerformanceHandlerMixin",
    "SddcHandlerMixin",
    "StorageHandlerMixin",
    "SystemHandlerMixin",
    "VMHandlerMixin",
    "VsanHandlerMixin",
]
