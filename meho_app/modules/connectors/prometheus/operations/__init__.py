# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus Operations - Combined from Category Files

This module imports and combines all operation definitions from category files.

Categories:
- infrastructure: CPU, memory, disk, network (8 operations)
- service: RED metrics (1 operation)
- discovery: Targets, metrics, alerts, rules, PromQL escape hatch (5 operations)

Total: 14 operations
"""

from .discovery import DISCOVERY_OPERATIONS
from .infrastructure import INFRASTRUCTURE_OPERATIONS
from .service import SERVICE_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
# Format: YYYY.MM.DD.revision
PROMETHEUS_OPERATIONS_VERSION = "2026.03.04.1"

# Combined list of all Prometheus operations
PROMETHEUS_OPERATIONS = INFRASTRUCTURE_OPERATIONS + SERVICE_OPERATIONS + DISCOVERY_OPERATIONS

__all__ = [
    "DISCOVERY_OPERATIONS",
    "INFRASTRUCTURE_OPERATIONS",
    "PROMETHEUS_OPERATIONS",
    "PROMETHEUS_OPERATIONS_VERSION",
    "SERVICE_OPERATIONS",
]
