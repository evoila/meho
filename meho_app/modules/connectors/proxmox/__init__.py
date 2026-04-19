# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox VE Connector Module (TASK-100)

Native Proxmox connector using proxmoxer SDK.
Implements BaseConnector for transparent agent integration.
"""

from meho_app.modules.connectors.proxmox.connector import ProxmoxConnector
from meho_app.modules.connectors.proxmox.operations import (
    PROXMOX_OPERATIONS,
    PROXMOX_OPERATIONS_VERSION,
)
from meho_app.modules.connectors.proxmox.types import PROXMOX_TYPES

__all__ = [
    "PROXMOX_OPERATIONS",
    "PROXMOX_OPERATIONS_VERSION",
    "PROXMOX_TYPES",
    "ProxmoxConnector",
]
