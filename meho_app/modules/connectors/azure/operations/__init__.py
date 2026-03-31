# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Operation Definitions (Phase 92).

Aggregates all operation definitions from category-specific modules.
"""

from meho_app.modules.connectors.azure.operations.aks import AKS_OPERATIONS
from meho_app.modules.connectors.azure.operations.compute import COMPUTE_OPERATIONS
from meho_app.modules.connectors.azure.operations.monitor import MONITOR_OPERATIONS
from meho_app.modules.connectors.azure.operations.network import NETWORK_OPERATIONS
from meho_app.modules.connectors.azure.operations.storage import STORAGE_OPERATIONS
from meho_app.modules.connectors.azure.operations.web import WEB_OPERATIONS

# Version for tracking operation updates
AZURE_OPERATIONS_VERSION = "2026.03.27.1"

# Aggregate all operations
AZURE_OPERATIONS = (
    COMPUTE_OPERATIONS
    + MONITOR_OPERATIONS
    + AKS_OPERATIONS
    + NETWORK_OPERATIONS
    + STORAGE_OPERATIONS
    + WEB_OPERATIONS
)

__all__ = [
    "AKS_OPERATIONS",
    "AZURE_OPERATIONS",
    "AZURE_OPERATIONS_VERSION",
    "COMPUTE_OPERATIONS",
    "MONITOR_OPERATIONS",
    "NETWORK_OPERATIONS",
    "STORAGE_OPERATIONS",
    "WEB_OPERATIONS",
]
