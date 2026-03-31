# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Alertmanager Operations - Combined from Category Files

This module imports and combines all operation definitions from category files.

Categories:
- alerts: list_alerts, get_firing_alerts, get_alert_detail (3 operations)
- silences: list_silences, create_silence, silence_alert, expire_silence (4 operations)
- status: get_cluster_status, list_receivers (2 operations)

Total: 9 operations
"""

from .alerts import ALERT_OPERATIONS
from .silences import SILENCE_OPERATIONS, WRITE_OPERATIONS
from .status import STATUS_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
# Format: YYYY.MM.DD.revision
ALERTMANAGER_OPERATIONS_VERSION = "2026.03.05.1"

# Combined list of all Alertmanager operations
ALERTMANAGER_OPERATIONS = ALERT_OPERATIONS + SILENCE_OPERATIONS + STATUS_OPERATIONS

__all__ = [
    "ALERTMANAGER_OPERATIONS",
    "ALERTMANAGER_OPERATIONS_VERSION",
    "ALERT_OPERATIONS",
    "SILENCE_OPERATIONS",
    "STATUS_OPERATIONS",
    "WRITE_OPERATIONS",
]
