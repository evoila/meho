"""
VMware vSphere Connector (TASK-97)

Implements the BaseConnector interface using pyvmomi for
native VMware vSphere API access.
"""

from meho_openapi.connectors.vmware.connector import VMwareConnector
from meho_openapi.connectors.vmware.operations import (
    VMWARE_OPERATIONS,
    VMWARE_OPERATIONS_VERSION,
)
from meho_openapi.connectors.vmware.types import VMWARE_TYPES
from meho_openapi.connectors.vmware.sync import (
    sync_all_vmware_connectors,
    sync_vmware_operations_if_needed,
)

__all__ = [
    "VMwareConnector",
    "VMWARE_OPERATIONS",
    "VMWARE_OPERATIONS_VERSION",
    "VMWARE_TYPES",
    "sync_all_vmware_connectors",
    "sync_vmware_operations_if_needed",
]

