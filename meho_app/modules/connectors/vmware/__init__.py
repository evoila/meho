# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware vSphere Connector.

Implements the BaseConnector interface using pyvmomi for
native VMware vSphere API access.
"""

from meho_app.modules.connectors.vmware.connector import VMwareConnector
from meho_app.modules.connectors.vmware.operations import (
    VMWARE_OPERATIONS,
    VMWARE_OPERATIONS_VERSION,
)
from meho_app.modules.connectors.vmware.sync import (
    sync_all_vmware_connectors,
    sync_vmware_operations_if_needed,
)
from meho_app.modules.connectors.vmware.types import VMWARE_TYPES

__all__ = [
    "VMWARE_OPERATIONS",
    "VMWARE_OPERATIONS_VERSION",
    "VMWARE_TYPES",
    "VMwareConnector",
    "sync_all_vmware_connectors",
    "sync_vmware_operations_if_needed",
]
