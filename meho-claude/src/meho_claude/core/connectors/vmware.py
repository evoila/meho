"""VMware connector implementing BaseConnector via pyvmomi PropertyCollector.

Registered as "vmware" in the connector registry. Uses synchronous pyvmomi
wrapped with asyncio.to_thread() for async compatibility. Supports all 5
VMware object types (VMs, Hosts, Clusters, Datastores, Networks) plus
write operations (power on/off, snapshots, vMotion).
"""

from __future__ import annotations

import asyncio
import ssl
from datetime import datetime
from typing import Any

from pyVim.connect import Disconnect, SmartConnect
from pyVim.task import WaitForTask
from pyVmomi import vim, vmodl

from meho_claude.core.connectors.base import BaseConnector
from meho_claude.core.connectors.models import ConnectorConfig, Operation
from meho_claude.core.connectors.registry import register_connector

# PropertyCollector property sets per object type
_VM_PROPERTIES = [
    "name", "config.instanceUuid", "config.guestFullName",
    "config.hardware.numCPU", "config.hardware.memoryMB",
    "summary.runtime.powerState", "summary.runtime.host",
    "summary.runtime.connectionState", "guest.ipAddress",
    "guest.hostName", "guest.toolsStatus", "resourcePool",
    "network", "datastore",
]

_HOST_PROPERTIES = [
    "name", "summary.hardware.cpuModel", "summary.hardware.numCpuCores",
    "summary.hardware.memorySize", "summary.runtime.connectionState",
    "summary.runtime.powerState", "parent",
]

_CLUSTER_PROPERTIES = [
    "name", "summary.numHosts", "summary.numEffectiveHosts",
    "summary.totalCpu", "summary.totalMemory",
]

_DATASTORE_PROPERTIES = [
    "name", "summary.type", "summary.capacity",
    "summary.freeSpace", "summary.accessible",
]

_NETWORK_PROPERTIES = ["name", "summary.accessible"]

# Operation definitions (connector_name substituted at discover time)
_VMWARE_OPERATIONS = [
    # READ operations
    {
        "operation_id": "list-vms",
        "display_name": "List VMs",
        "description": "List all virtual machines with hardware/runtime properties",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["vmware", "vms"],
    },
    {
        "operation_id": "get-vm",
        "display_name": "Get VM",
        "description": "Get a specific virtual machine by name",
        "trust_tier": "READ",
        "input_schema": {"name": {"type": "string", "required": True}},
        "tags": ["vmware", "vms"],
    },
    {
        "operation_id": "list-hosts",
        "display_name": "List Hosts",
        "description": "List all ESXi hosts with hardware/runtime properties",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["vmware", "hosts"],
    },
    {
        "operation_id": "list-clusters",
        "display_name": "List Clusters",
        "description": "List all compute clusters with resource summary",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["vmware", "clusters"],
    },
    {
        "operation_id": "list-datastores",
        "display_name": "List Datastores",
        "description": "List all datastores with capacity and usage",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["vmware", "datastores"],
    },
    {
        "operation_id": "list-networks",
        "display_name": "List Networks",
        "description": "List all networks with accessibility status",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["vmware", "networks"],
    },
    # WRITE operations
    {
        "operation_id": "power-on-vm",
        "display_name": "Power On VM",
        "description": "Power on a virtual machine",
        "trust_tier": "WRITE",
        "input_schema": {"name": {"type": "string", "required": True}},
        "tags": ["vmware", "vms", "power"],
    },
    {
        "operation_id": "power-off-vm",
        "display_name": "Power Off VM",
        "description": "Power off a virtual machine",
        "trust_tier": "WRITE",
        "input_schema": {"name": {"type": "string", "required": True}},
        "tags": ["vmware", "vms", "power"],
    },
    {
        "operation_id": "create-snapshot",
        "display_name": "Create Snapshot",
        "description": "Create a snapshot of a virtual machine",
        "trust_tier": "WRITE",
        "input_schema": {
            "name": {"type": "string", "required": True},
            "snapshot_name": {"type": "string", "required": True},
            "description": {"type": "string", "required": False},
        },
        "tags": ["vmware", "vms", "snapshots"],
    },
    {
        "operation_id": "revert-snapshot",
        "display_name": "Revert Snapshot",
        "description": "Revert a virtual machine to a named snapshot",
        "trust_tier": "WRITE",
        "input_schema": {
            "name": {"type": "string", "required": True},
            "snapshot_name": {"type": "string", "required": True},
        },
        "tags": ["vmware", "vms", "snapshots"],
    },
    {
        "operation_id": "vmotion-vm",
        "display_name": "vMotion VM",
        "description": "Migrate a virtual machine to a target host",
        "trust_tier": "WRITE",
        "input_schema": {
            "name": {"type": "string", "required": True},
            "target_host": {"type": "string", "required": True},
        },
        "tags": ["vmware", "vms", "migration"],
    },
    # DESTRUCTIVE operations
    {
        "operation_id": "delete-snapshot",
        "display_name": "Delete Snapshot",
        "description": "Delete a named snapshot from a virtual machine",
        "trust_tier": "DESTRUCTIVE",
        "input_schema": {
            "name": {"type": "string", "required": True},
            "snapshot_name": {"type": "string", "required": True},
        },
        "tags": ["vmware", "vms", "snapshots", "destructive"],
    },
]

# Map operation_id to (vim type, property set) for list operations
_LIST_OP_MAP: dict[str, tuple[type, list[str]]] = {
    "list-vms": (vim.VirtualMachine, _VM_PROPERTIES),
    "list-hosts": (vim.HostSystem, _HOST_PROPERTIES),
    "list-clusters": (vim.ClusterComputeResource, _CLUSTER_PROPERTIES),
    "list-datastores": (vim.Datastore, _DATASTORE_PROPERTIES),
    "list-networks": (vim.Network, _NETWORK_PROPERTIES),
}


@register_connector("vmware")
class VMwareConnector(BaseConnector):
    """VMware vCenter connector using pyvmomi PropertyCollector.

    Uses synchronous pyvmomi wrapped with asyncio.to_thread() for all
    vCenter operations. Creates a fresh connection per execute() call
    (no cached ServiceInstance).
    """

    def __init__(self, config_obj: ConnectorConfig, credentials: dict | None = None) -> None:
        super().__init__(config_obj, credentials)

    def _connect(self):
        """Create a synchronous vCenter connection.

        Uses SmartConnect with SSL context based on verify_ssl config.
        Reads host from config.base_url and port from config.tags["port"].
        """
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if not self.config.verify_ssl:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return SmartConnect(
            host=self.config.base_url,
            user=self.credentials["username"],
            pwd=self.credentials["password"],
            port=int(self.config.tags.get("port", "443")),
            sslContext=context,
        )

    async def test_connection(self) -> dict[str, Any]:
        """Test connectivity by connecting to vCenter and reading server time.

        Returns:
            Dict with status and server_time on success,
            or status and error message on failure.
        """
        try:
            si = await asyncio.to_thread(self._connect)
            try:
                server_time = await asyncio.to_thread(si.CurrentTime)
                return {
                    "status": "ok",
                    "server_time": server_time.isoformat() if isinstance(server_time, datetime) else str(server_time),
                }
            finally:
                await asyncio.to_thread(Disconnect, si)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def discover_operations(self) -> list[Operation]:
        """Return hardcoded operations for all VMware object types.

        Operations are defined at module level and built with the
        actual connector name at discovery time.
        """
        operations = []
        for op_def in _VMWARE_OPERATIONS:
            operations.append(
                Operation(
                    connector_name=self.config.name,
                    operation_id=op_def["operation_id"],
                    display_name=op_def["display_name"],
                    description=op_def["description"],
                    trust_tier=op_def["trust_tier"],
                    input_schema=op_def["input_schema"],
                    tags=op_def["tags"],
                )
            )
        return operations

    async def execute(self, operation: Operation, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a VMware operation via PropertyCollector or direct API call.

        Creates a fresh vCenter connection per call. Always disconnects in finally.
        """
        op_id = operation.operation_id

        si = await asyncio.to_thread(self._connect)
        try:
            # List operations use PropertyCollector
            if op_id in _LIST_OP_MAP:
                vim_type, path_set = _LIST_OP_MAP[op_id]
                data = await asyncio.to_thread(self._collect_properties, si, vim_type, path_set)
                return {"data": data}

            # get-vm filters list-vms by name
            elif op_id == "get-vm":
                vm_name = params["name"]
                data = await asyncio.to_thread(
                    self._collect_properties, si, vim.VirtualMachine, _VM_PROPERTIES
                )
                for vm in data:
                    if vm.get("name") == vm_name:
                        return {"data": vm}
                raise ValueError(f"VM not found: {vm_name}")

            # Write operations
            elif op_id == "power-on-vm":
                return await self._execute_power_on(si, params)

            elif op_id == "power-off-vm":
                return await self._execute_power_off(si, params)

            elif op_id == "create-snapshot":
                return await self._execute_create_snapshot(si, params)

            elif op_id == "revert-snapshot":
                return await self._execute_revert_snapshot(si, params)

            elif op_id == "delete-snapshot":
                return await self._execute_delete_snapshot(si, params)

            elif op_id == "vmotion-vm":
                return await self._execute_vmotion(si, params)

            else:
                raise ValueError(f"Unknown operation: {op_id}")

        finally:
            await asyncio.to_thread(Disconnect, si)

    def _collect_properties(
        self,
        si: Any,
        obj_type: type,
        path_set: list[str],
    ) -> list[dict[str, Any]]:
        """Collect properties via PropertyCollector with ContainerView.

        Uses the efficient batch property retrieval pattern.
        ALWAYS destroys ContainerView in finally block.
        """
        content = si.content
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [obj_type], True
        )
        try:
            # Build traversal spec
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities",
                path="view",
                skip=False,
                type=type(view),
            )

            # Build object spec
            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=view,
                skip=True,
                selectSet=[traversal_spec],
            )

            # Build property spec
            property_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=obj_type,
                pathSet=path_set,
                all=False,
            )

            # Build filter spec
            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec],
                propSet=[property_spec],
            )

            # Retrieve properties
            results = content.propertyCollector.RetrieveContents([filter_spec])

            # Parse results
            objects = []
            for obj_content in results:
                obj_dict: dict[str, Any] = {"_moref": str(obj_content.obj)}
                if obj_content.propSet:
                    for prop in obj_content.propSet:
                        obj_dict[prop.name] = self._serialize_property(prop.val)
                objects.append(obj_dict)

            return objects

        finally:
            view.Destroy()

    def _serialize_property(self, val: Any) -> Any:
        """Recursively serialize a pyvmomi property value to JSON-safe types.

        Handles: None, str, int, float, bool, datetime, list/tuple,
        ManagedObject, DataObject, and falls back to str().
        """
        if val is None or isinstance(val, (str, int, float, bool)):
            return val

        if isinstance(val, datetime):
            return val.isoformat()

        if isinstance(val, (list, tuple)):
            return [self._serialize_property(item) for item in val]

        # ManagedObject -> MoRef string
        if isinstance(val, vim.ManagedEntity) or (hasattr(val, '_moId') and hasattr(val, '_wsdlName')):
            return str(val)

        # Enum-like objects with .name attribute (e.g., power state enums)
        if hasattr(val, 'name') and isinstance(getattr(val, 'name', None), str) and not hasattr(val, '__dict__'):
            return val.name

        # DataObject -> dict of non-private attributes
        if hasattr(val, '__dict__'):
            result = {}
            for attr_name in dir(val):
                if attr_name.startswith('_'):
                    continue
                try:
                    attr_val = getattr(val, attr_name)
                    if callable(attr_val):
                        continue
                    result[attr_name] = self._serialize_property(attr_val)
                except Exception:
                    continue
            if result:
                return result

        return str(val)

    def _find_vm_by_name(self, si: Any, name: str) -> Any:
        """Find a VM ManagedObject by name using PropertyCollector.

        Returns the vim.VirtualMachine managed object reference.
        Raises ValueError if VM not found.
        """
        content = si.content
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities",
                path="view",
                skip=False,
                type=type(view),
            )
            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=view,
                skip=True,
                selectSet=[traversal_spec],
            )
            property_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=vim.VirtualMachine,
                pathSet=["name"],
                all=False,
            )
            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec],
                propSet=[property_spec],
            )
            results = content.propertyCollector.RetrieveContents([filter_spec])

            for obj_content in results:
                if obj_content.propSet:
                    for prop in obj_content.propSet:
                        if prop.name == "name" and prop.val == name:
                            return obj_content.obj

            raise ValueError(f"VM not found: {name}")

        finally:
            view.Destroy()

    def _find_host_by_name(self, si: Any, name: str) -> Any:
        """Find a HostSystem ManagedObject by name."""
        content = si.content
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        try:
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities",
                path="view",
                skip=False,
                type=type(view),
            )
            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=view,
                skip=True,
                selectSet=[traversal_spec],
            )
            property_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=vim.HostSystem,
                pathSet=["name"],
                all=False,
            )
            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec],
                propSet=[property_spec],
            )
            results = content.propertyCollector.RetrieveContents([filter_spec])

            for obj_content in results:
                if obj_content.propSet:
                    for prop in obj_content.propSet:
                        if prop.name == "name" and prop.val == name:
                            return obj_content.obj

            raise ValueError(f"Host not found: {name}")

        finally:
            view.Destroy()

    def _find_snapshot_by_name(self, vm: Any, snapshot_name: str) -> Any:
        """Find a snapshot by name in a VM's snapshot tree.

        Searches recursively through the snapshot tree.
        Raises ValueError if snapshot not found.
        """
        if not vm.snapshot:
            raise ValueError(f"VM has no snapshots")

        def _search_tree(snapshots):
            for snap in snapshots:
                if snap.name == snapshot_name:
                    return snap.snapshot
                if snap.childSnapshotList:
                    found = _search_tree(snap.childSnapshotList)
                    if found:
                        return found
            return None

        result = _search_tree(vm.snapshot.rootSnapshotList)
        if result is None:
            raise ValueError(f"Snapshot not found: {snapshot_name}")
        return result

    async def _execute_power_on(self, si: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Power on a VM."""
        vm_name = params["name"]
        vm = await asyncio.to_thread(self._find_vm_by_name, si, vm_name)
        task = await asyncio.to_thread(vm.PowerOn)
        await asyncio.to_thread(WaitForTask, task)
        return {"status": "ok", "message": f"VM '{vm_name}' powered on"}

    async def _execute_power_off(self, si: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Power off a VM."""
        vm_name = params["name"]
        vm = await asyncio.to_thread(self._find_vm_by_name, si, vm_name)
        task = await asyncio.to_thread(vm.PowerOff)
        await asyncio.to_thread(WaitForTask, task)
        return {"status": "ok", "message": f"VM '{vm_name}' powered off"}

    async def _execute_create_snapshot(self, si: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Create a VM snapshot."""
        vm_name = params["name"]
        snapshot_name = params["snapshot_name"]
        description = params.get("description", "")
        vm = await asyncio.to_thread(self._find_vm_by_name, si, vm_name)
        task = await asyncio.to_thread(
            vm.CreateSnapshot_Task,
            name=snapshot_name,
            description=description,
            memory=False,
            quiesce=True,
        )
        await asyncio.to_thread(WaitForTask, task)
        return {"status": "ok", "message": f"Snapshot '{snapshot_name}' created for VM '{vm_name}'"}

    async def _execute_revert_snapshot(self, si: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Revert a VM to a named snapshot."""
        vm_name = params["name"]
        snapshot_name = params["snapshot_name"]
        vm = await asyncio.to_thread(self._find_vm_by_name, si, vm_name)
        snap = await asyncio.to_thread(self._find_snapshot_by_name, vm, snapshot_name)
        task = await asyncio.to_thread(snap.RevertToSnapshot_Task)
        await asyncio.to_thread(WaitForTask, task)
        return {"status": "ok", "message": f"VM '{vm_name}' reverted to snapshot '{snapshot_name}'"}

    async def _execute_delete_snapshot(self, si: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Delete a named snapshot from a VM."""
        vm_name = params["name"]
        snapshot_name = params["snapshot_name"]
        vm = await asyncio.to_thread(self._find_vm_by_name, si, vm_name)
        snap = await asyncio.to_thread(self._find_snapshot_by_name, vm, snapshot_name)
        task = await asyncio.to_thread(snap.RemoveSnapshot_Task, removeChildren=False)
        await asyncio.to_thread(WaitForTask, task)
        return {"status": "ok", "message": f"Snapshot '{snapshot_name}' deleted from VM '{vm_name}'"}

    async def _execute_vmotion(self, si: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Migrate a VM to a target host via vMotion."""
        vm_name = params["name"]
        target_host_name = params["target_host"]
        vm = await asyncio.to_thread(self._find_vm_by_name, si, vm_name)
        target_host = await asyncio.to_thread(self._find_host_by_name, si, target_host_name)

        relocate_spec = vim.vm.RelocateSpec(host=target_host)
        task = await asyncio.to_thread(vm.Relocate, spec=relocate_spec)
        await asyncio.to_thread(WaitForTask, task)
        return {"status": "ok", "message": f"VM '{vm_name}' migrated to host '{target_host_name}'"}

    def get_trust_tier(self, operation: Operation) -> str:
        """Determine trust tier, checking config overrides first."""
        override_map = {o.operation_id: o.trust_tier for o in self.config.trust_overrides}
        if operation.operation_id in override_map:
            return override_map[operation.operation_id]
        return operation.trust_tier

    def close(self) -> None:
        """No-op -- vCenter connection created per-execute call."""
        pass
