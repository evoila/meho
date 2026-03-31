"""
Backward compatibility shim for VMware connector.

This connector has been moved to meho_app.modules.connectors.vmware.
This file re-exports it for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.vmware import (
    VMwareConnector,
    VMWARE_OPERATIONS,
    VMWARE_OPERATIONS_VERSION,
    VMWARE_TYPES,
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
