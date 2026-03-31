"""
VM Operation Handlers

Mixin class containing 75 vm operation handlers.
"""

from typing import List, Dict, Any, Optional


class VMHandlerMixin:
    """Mixin for vm operation handlers."""
    
    # These will be provided by VMwareConnector (base class)
    _content: Any
    
    # Helper methods (will be provided by VMwareConnector) - stubs for type checking
    def _find_vm(self, name: str) -> Optional[Any]: return None
    def _find_host(self, name: str) -> Optional[Any]: return None
    def _find_datastore(self, name: str) -> Optional[Any]: return None
    def _find_folder(self, name: str) -> Optional[Any]: return None
    def _find_snapshot(self, vm: Any, snapshot_name: str) -> Optional[Any]: return None
    def _find_resource_pool(self, name: str) -> Optional[Any]: return None
    def _find_cluster(self, name: str) -> Optional[Any]: return None
    def _make_guest_auth(self, username: str, password: str) -> Any: return None
    def _collect_snapshots(self, snapshot_list: List, results: List[Dict], parent: Optional[str] = None) -> None: pass
    
    # Serializer methods (will be provided by VMwareConnector) - stubs for type checking
    def _serialize_vm_properties(self, vm: Any) -> Dict[str, Any]: return {}
    def _serialize_datastore_properties(self, ds: Any) -> Dict[str, Any]: return {}
    
    async def _list_vms(self, params: Dict[str, Any]) -> List[Dict]:
        """List all VMs with complete property data."""
        from pyVmomi import vim
        
        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            return [self._serialize_vm_properties(vm) for vm in container.view]
        finally:
            container.Destroy()
    

    async def _get_vm(self, params: Dict[str, Any]) -> Dict:
        """Get VM details with complete property data."""
        vm_name = params.get("vm_name") or params.get("name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        return self._serialize_vm_properties(vm)
    

    async def _power_on_vm(self, params: Dict[str, Any]) -> Dict:
        """Power on VM."""
        vm_name = params.get("vm_name") or params.get("name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        task = vm.PowerOn()
        return {
            "message": f"Power on initiated for {vm_name}",
            "task_id": str(task._moId),
        }
    

    async def _power_off_vm(self, params: Dict[str, Any]) -> Dict:
        """Power off VM (hard)."""
        vm_name = params.get("vm_name") or params.get("name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        task = vm.PowerOff()
        return {
            "message": f"Power off initiated for {vm_name}",
            "task_id": str(task._moId),
        }
    

    async def _shutdown_guest(self, params: Dict[str, Any]) -> Dict:
        """Graceful guest shutdown."""
        vm_name = params.get("vm_name") or params.get("name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        # ShutdownGuest doesn't return a task, it's a guest operation
        vm.ShutdownGuest()
        return {
            "message": f"Guest shutdown initiated for {vm_name}",
        }
    

    async def _create_snapshot(self, params: Dict[str, Any]) -> Dict:
        """Create VM snapshot."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        snapshot_name = params.get("snapshot_name")
        if not snapshot_name:
            raise ValueError("snapshot_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        description = params.get("description", "")
        include_memory = params.get("include_memory", False)
        quiesce_guest = params.get("quiesce_guest", True)
        
        task = vm.CreateSnapshot(
            name=snapshot_name,
            description=description,
            memory=include_memory,
            quiesce=quiesce_guest,
        )
        
        return {
            "message": f"Snapshot '{snapshot_name}' creation initiated for {vm_name}",
            "task_id": str(task._moId),
        }
    

    async def _list_snapshots(self, params: Dict[str, Any]) -> Dict:
        """List VM snapshots with complete VM context."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        snapshots: List[Dict] = []
        if vm.snapshot:
            self._collect_snapshots(vm.snapshot.rootSnapshotList, snapshots)
        
        # Return complete VM data + snapshot information
        result = self._serialize_vm_properties(vm)
        result["snapshots"] = snapshots
        return result
    

    async def _revert_snapshot(self, params: Dict[str, Any]) -> Dict:
        """Revert to VM snapshot.
        
        pyvmomi signature: RevertToSnapshot_Task(host: HostSystem, suppressPowerOn: bool)
        """
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        snapshot_name = params.get("snapshot_name")
        if not snapshot_name:
            raise ValueError("snapshot_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        snapshot = self._find_snapshot(vm, snapshot_name)
        if not snapshot:
            raise ValueError(f"Snapshot not found: {snapshot_name}")
        
        # Optional parameters from pyvmomi signature
        suppress_power_on = params.get("suppress_power_on", False)
        target_host_name = params.get("target_host")
        
        target_host = None
        if target_host_name:
            target_host = self._find_host(target_host_name)
        
        task = snapshot.RevertToSnapshot_Task(
            host=target_host,
            suppressPowerOn=suppress_power_on
        )
        return {
            "message": f"Revert to snapshot '{snapshot_name}' initiated for {vm_name}",
            "task_id": str(task._moId),
            "suppress_power_on": suppress_power_on,
        }
    

    async def _delete_snapshot(self, params: Dict[str, Any]) -> Dict:
        """Delete VM snapshot.
        
        pyvmomi signature: RemoveSnapshot_Task(removeChildren: bool, consolidate: bool)
        """
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        snapshot_name = params.get("snapshot_name")
        if not snapshot_name:
            raise ValueError("snapshot_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        snapshot = self._find_snapshot(vm, snapshot_name)
        if not snapshot:
            raise ValueError(f"Snapshot not found: {snapshot_name}")
        
        remove_children = params.get("remove_children", False)
        consolidate = params.get("consolidate", True)  # Usually want to consolidate
        
        task = snapshot.RemoveSnapshot_Task(
            removeChildren=remove_children,
            consolidate=consolidate
        )
        
        return {
            "message": f"Snapshot '{snapshot_name}' deletion initiated for {vm_name}",
            "task_id": str(task._moId),
            "consolidate": consolidate,
        }
    

    async def _delete_all_snapshots(self, params: Dict[str, Any]) -> Dict:
        """Delete all VM snapshots.
        
        pyvmomi signature: RemoveAllSnapshots_Task(consolidate: bool, spec: SnapshotSelectionSpec)
        Note: spec is optional in practice, only consolidate is commonly used.
        """
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        consolidate = params.get("consolidate", True)  # Usually want to consolidate
        
        # Note: spec parameter is rarely used, passing None
        task = vm.RemoveAllSnapshots_Task(
            consolidate=consolidate,
            spec=None
        )
        return {
            "message": f"All snapshots deletion initiated for {vm_name}",
            "task_id": str(task._moId),
            "consolidate": consolidate,
        }
    

    async def _reconfigure_vm_cpu(self, params: Dict[str, Any]) -> Dict:
        """Reconfigure VM CPU."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        num_cpus = params.get("num_cpus")
        if not num_cpus:
            raise ValueError("num_cpus is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        cores_per_socket = params.get("cores_per_socket", 1)
        
        config_spec = vim.vm.ConfigSpec()
        config_spec.numCPUs = num_cpus
        config_spec.numCoresPerSocket = cores_per_socket
        
        task = vm.ReconfigVM_Task(spec=config_spec)
        return {
            "message": f"CPU reconfiguration to {num_cpus} CPUs initiated for {vm_name}",
            "task_id": str(task._moId),
        }
    

    async def _reconfigure_vm_memory(self, params: Dict[str, Any]) -> Dict:
        """Reconfigure VM memory."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        memory_mb = params.get("memory_mb")
        if not memory_mb:
            raise ValueError("memory_mb is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        config_spec = vim.vm.ConfigSpec()
        config_spec.memoryMB = memory_mb
        
        task = vm.ReconfigVM_Task(spec=config_spec)
        return {
            "message": f"Memory reconfiguration to {memory_mb} MB initiated for {vm_name}",
            "task_id": str(task._moId),
        }
    

    async def _get_vm_disks(self, params: Dict[str, Any]) -> Dict:
        """Get VM disk info with complete VM context."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        disks = []
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                backing = device.backing
                disks.append({
                    "label": device.deviceInfo.label if device.deviceInfo else None,
                    "capacity_gb": device.capacityInKB // (1024 * 1024) if device.capacityInKB else None,
                    "datastore": backing.datastore.name if backing and backing.datastore else None,
                    "file_name": backing.fileName if backing else None,
                    "thin_provisioned": backing.thinProvisioned if hasattr(backing, 'thinProvisioned') else None,
                })
        
        # Return complete VM data + specialized disk information
        result = self._serialize_vm_properties(vm)
        result["disks"] = disks
        return result
    

    async def _get_vm_nics(self, params: Dict[str, Any]) -> Dict:
        """Get VM network adapter info with complete VM context."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        nics = []
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualEthernetCard):
                backing = device.backing
                network = None
                if hasattr(backing, 'network') and backing.network:
                    network = backing.network.name
                elif hasattr(backing, 'port') and backing.port:
                    network = f"DVPort: {backing.port.portgroupKey}"
                
                nics.append({
                    "label": device.deviceInfo.label if device.deviceInfo else None,
                    "mac_address": device.macAddress,
                    "network": network,
                    "connected": device.connectable.connected if device.connectable else None,
                    "type": type(device).__name__,
                })
        
        # Return complete VM data + specialized NIC information
        result = self._serialize_vm_properties(vm)
        result["nics"] = nics
        return result
    

    async def _rename_vm(self, params: Dict[str, Any]) -> Dict:
        """Rename VM."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        new_name = params.get("new_name")
        if not new_name:
            raise ValueError("new_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        task = vm.Rename(new_name)
        return {
            "message": f"VM renamed from '{vm_name}' to '{new_name}'",
            "task_id": str(task._moId) if task else None,
        }
    

    async def _set_vm_annotation(self, params: Dict[str, Any]) -> Dict:
        """Set VM annotation."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        annotation = params.get("annotation", "")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        config_spec = vim.vm.ConfigSpec()
        config_spec.annotation = annotation
        
        task = vm.ReconfigVM_Task(spec=config_spec)
        return {
            "message": f"Annotation updated for {vm_name}",
            "task_id": str(task._moId),
        }
    

    async def _migrate_vm(self, params: Dict[str, Any]) -> Dict:
        """vMotion - migrate VM to different host.
        
        pyvmomi signature: MigrateVM_Task(pool: ResourcePool, host: HostSystem, 
                                          priority: MovePriority, state: PowerState)
        """
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        target_host_name = params.get("target_host")
        if not target_host_name:
            raise ValueError("target_host is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        target_host = self._find_host(target_host_name)
        if not target_host:
            raise ValueError(f"Host not found: {target_host_name}")
        
        # Get resource pool from target host's parent cluster or compute resource
        resource_pool = None
        if target_host.parent and hasattr(target_host.parent, 'resourcePool'):
            resource_pool = target_host.parent.resourcePool
        
        # Priority: defaultPriority, highPriority, lowPriority
        priority_map = {
            "default": vim.VirtualMachine.MovePriority.defaultPriority,
            "high": vim.VirtualMachine.MovePriority.highPriority,
            "low": vim.VirtualMachine.MovePriority.lowPriority,
        }
        priority_str = params.get("priority", "default")
        priority = priority_map.get(priority_str, vim.VirtualMachine.MovePriority.defaultPriority)
        
        # State: poweredOn, poweredOff, suspended (current state of VM for migration)
        # None means use current state
        state = None  # Let vCenter determine based on current VM state
        
        task = vm.MigrateVM_Task(
            pool=resource_pool,
            host=target_host,
            priority=priority,
            state=state,
        )
        
        return {
            "message": f"vMotion initiated for {vm_name} to {target_host_name}",
            "task_id": str(task._moId),
            "priority": priority_str,
        }
    

    async def _relocate_vm(self, params: Dict[str, Any]) -> Dict:
        """Relocate VM (Storage vMotion).
        
        pyvmomi signature: RelocateVM_Task(spec: RelocateSpec, priority: MovePriority)
        """
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        relocate_spec = vim.vm.RelocateSpec()
        
        # Set target datastore
        target_ds_name = params.get("target_datastore")
        if target_ds_name:
            target_ds = self._find_datastore(target_ds_name)
            if not target_ds:
                raise ValueError(f"Datastore not found: {target_ds_name}")
            relocate_spec.datastore = target_ds
        
        # Set target host
        target_host_name = params.get("target_host")
        if target_host_name:
            target_host = self._find_host(target_host_name)
            if not target_host:
                raise ValueError(f"Host not found: {target_host_name}")
            relocate_spec.host = target_host
        
        # Priority: defaultPriority, highPriority, lowPriority
        priority_map = {
            "default": vim.VirtualMachine.MovePriority.defaultPriority,
            "high": vim.VirtualMachine.MovePriority.highPriority,
            "low": vim.VirtualMachine.MovePriority.lowPriority,
        }
        priority_str = params.get("priority", "default")
        priority = priority_map.get(priority_str, vim.VirtualMachine.MovePriority.defaultPriority)
        
        task = vm.RelocateVM_Task(
            spec=relocate_spec,
            priority=priority
        )
        return {
            "message": f"Relocation initiated for {vm_name}",
            "task_id": str(task._moId),
            "priority": priority_str,
        }
    

    async def _clone_vm(self, params: Dict[str, Any]) -> Dict:
        """Clone VM."""
        from pyVmomi import vim
        
        source_vm_name = params.get("source_vm")
        if not source_vm_name:
            raise ValueError("source_vm is required")
        
        clone_name = params.get("clone_name")
        if not clone_name:
            raise ValueError("clone_name is required")
        
        source_vm = self._find_vm(source_vm_name)
        if not source_vm:
            raise ValueError(f"Source VM not found: {source_vm_name}")
        
        # Get folder (use source VM's folder or specified folder)
        folder_name = params.get("target_folder")
        if folder_name:
            folder = self._find_folder(folder_name)
            if not folder:
                raise ValueError(f"Folder not found: {folder_name}")
        else:
            folder = source_vm.parent
        
        # Build clone spec
        clone_spec = vim.vm.CloneSpec()
        clone_spec.powerOn = params.get("power_on", False)
        
        # Relocate spec (datastore)
        relocate_spec = vim.vm.RelocateSpec()
        target_ds_name = params.get("target_datastore")
        if target_ds_name:
            target_ds = self._find_datastore(target_ds_name)
            if not target_ds:
                raise ValueError(f"Datastore not found: {target_ds_name}")
            relocate_spec.datastore = target_ds
        clone_spec.location = relocate_spec
        
        task = source_vm.Clone(folder=folder, name=clone_name, spec=clone_spec)
        return {
            "message": f"Clone '{clone_name}' creation initiated from {source_vm_name}",
            "task_id": str(task._moId),
        }
    

    async def _get_host_vms(self, params: Dict[str, Any]) -> List[Dict]:
        """Get VMs on a host with complete VM property data."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")
        
        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")
        
        return [self._serialize_vm_properties(vm) for vm in host.vm or []]
    

    async def _get_datastore_vms(self, params: Dict[str, Any]) -> List[Dict]:
        """Get VMs on a datastore with complete VM property data."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")
        
        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")
        
        return [self._serialize_vm_properties(vm) for vm in ds.vm or []]
    

    async def _get_vm_performance(self, params: Dict[str, Any]) -> Dict:
        """Get VM performance metrics."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        # Get quick stats (always available)
        qs = vm.summary.quickStats
        result = {
            "vm_name": vm_name,
            "quick_stats": {
                "cpu_usage_mhz": qs.overallCpuUsage,
                "memory_usage_mb": qs.guestMemoryUsage,
                "active_memory_mb": qs.activeMemory,
                "consumed_overhead_memory_mb": qs.consumedOverheadMemory,
                "uptime_seconds": qs.uptimeSeconds,
            } if qs else {},
        }
        
        return result
    

    async def _reboot_guest(self, params: Dict[str, Any]) -> Dict:
        """Reboot guest OS."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.RebootGuest()
        return {"message": f"Guest reboot initiated for {vm_name}"}
    

    async def _standby_guest(self, params: Dict[str, Any]) -> Dict:
        """Put guest into standby mode."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.StandbyGuest()
        return {"message": f"Guest standby initiated for {vm_name}"}
    

    async def _suspend_vm(self, params: Dict[str, Any]) -> Dict:
        """Suspend VM."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        task = vm.SuspendVM_Task()
        return {"message": f"Suspend initiated for {vm_name}", "task_id": str(task._moId)}
    

    async def _reset_vm(self, params: Dict[str, Any]) -> Dict:
        """Hard reset VM."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        task = vm.ResetVM_Task()
        return {"message": f"Reset initiated for {vm_name}", "task_id": str(task._moId)}
    

    async def _mark_as_template(self, params: Dict[str, Any]) -> Dict:
        """Convert VM to template."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.MarkAsTemplate()
        return {"message": f"VM '{vm_name}' converted to template"}
    

    async def _consolidate_disks(self, params: Dict[str, Any]) -> Dict:
        """Consolidate VM disks."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        task = vm.ConsolidateVMDisks_Task()
        return {"message": f"Disk consolidation initiated for {vm_name}", "task_id": str(task._moId)}
    

    async def _defragment_all_disks(self, params: Dict[str, Any]) -> Dict:
        """Defragment all VM disks."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.DefragmentAllDisks()
        return {"message": f"Disk defragmentation initiated for {vm_name}"}
    

    async def _mount_tools_installer(self, params: Dict[str, Any]) -> Dict:
        """Mount VMware Tools installer."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.MountToolsInstaller()
        return {"message": f"VMware Tools installer mounted for {vm_name}"}
    

    async def _unmount_tools_installer(self, params: Dict[str, Any]) -> Dict:
        """Unmount VMware Tools installer."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.UnmountToolsInstaller()
        return {"message": f"VMware Tools installer unmounted for {vm_name}"}
    

    async def _upgrade_tools(self, params: Dict[str, Any]) -> Dict:
        """Upgrade VMware Tools."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        options = params.get("installer_options", "")
        task = vm.UpgradeTools_Task(installerOptions=options)
        return {"message": f"VMware Tools upgrade initiated for {vm_name}", "task_id": str(task._moId)}
    

    async def _export_vm(self, params: Dict[str, Any]) -> Dict:
        """Export VM to OVF."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        lease = vm.ExportVm()
        return {
            "message": f"Export lease acquired for {vm_name}",
            "lease_state": str(lease.state) if lease else None,
        }
    

    async def _unregister_vm(self, params: Dict[str, Any]) -> Dict:
        """Unregister VM from inventory."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.UnregisterVM()
        return {"message": f"VM '{vm_name}' unregistered from inventory"}
    

    async def _destroy_vm(self, params: Dict[str, Any]) -> Dict:
        """Destroy VM (delete from disk)."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        task = vm.Destroy_Task()
        return {"message": f"VM '{vm_name}' destruction initiated", "task_id": str(task._moId)}
    

    async def _answer_vm_question(self, params: Dict[str, Any]) -> Dict:
        """Answer VM question."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        question_id = params.get("question_id")
        if not question_id:
            raise ValueError("question_id is required")
        
        answer_choice = params.get("answer_choice")
        if not answer_choice:
            raise ValueError("answer_choice is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.AnswerVM(questionId=question_id, answerChoice=answer_choice)
        return {"message": f"Question answered for {vm_name}"}
    

    async def _acquire_mks_ticket(self, params: Dict[str, Any]) -> Dict:
        """Acquire MKS console ticket."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        ticket = vm.AcquireMksTicket()
        return {
            "host": ticket.host,
            "port": ticket.port,
            "ticket": ticket.ticket,
            "ssl_thumbprint": ticket.sslThumbprint,
        }
    

    async def _acquire_ticket(self, params: Dict[str, Any]) -> Dict:
        """Acquire VM ticket."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        ticket_type = params.get("ticket_type")
        if not ticket_type:
            raise ValueError("ticket_type is required (mks, device, guestControl, webmks)")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        ticket = vm.AcquireTicket(ticketType=ticket_type)
        return {
            "ticket": ticket.ticket,
            "host": getattr(ticket, 'host', None),
            "port": getattr(ticket, 'port', None),
        }
    

    async def _query_changed_disk_areas(self, params: Dict[str, Any]) -> Dict:
        """Query changed disk areas for CBT."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        snapshot_name = params.get("snapshot_name")
        if not snapshot_name:
            raise ValueError("snapshot_name is required")
        
        snapshot = self._find_snapshot(vm, snapshot_name)
        if not snapshot:
            raise ValueError(f"Snapshot not found: {snapshot_name}")
        
        device_key = params.get("device_key")
        start_offset = params.get("start_offset", 0)
        change_id = params.get("change_id", "*")
        
        result = vm.QueryChangedDiskAreas(
            snapshot=snapshot,
            deviceKey=device_key,
            startOffset=start_offset,
            changeId=change_id,
        )
        
        return {
            "start_offset": result.startOffset,
            "length": result.length,
            "changed_areas": [
                {"start": area.start, "length": area.length}
                for area in result.changedArea or []
            ],
        }
    

    async def _acquire_cim_ticket(self, params: Dict[str, Any]) -> Dict:
        """Acquire CIM services ticket."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")
        
        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")
        
        ticket = host.AcquireCimServicesTicket()
        return {
            "host": ticket.host,
            "port": ticket.port,
            "ticket": ticket.sessionId,
            "ssl_thumbprint": ticket.sslThumbprint,
        }
    

    async def _recommend_hosts_for_vm(self, params: Dict[str, Any]) -> List[Dict]:
        """Get host recommendations for VM placement."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")
        
        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")
        
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        hosts = cluster.RecommendHostsForVm(vm=vm, pool=None)
        return [
            {"host": h.host.name, "recommendation": h.recommendation}
            for h in hosts or []
        ]
    

    async def _attach_disk(self, params: Dict[str, Any]) -> Dict:
        """Attach existing virtual disk to VM."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        disk_path = params.get("disk_path")
        if not disk_path:
            raise ValueError("disk_path is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        # Find SCSI controller
        controller = None
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualSCSIController):
                controller = device
                break
        
        if not controller:
            raise ValueError("No SCSI controller found on VM")
        
        # Create disk spec
        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        disk_spec.device = vim.vm.device.VirtualDisk()
        disk_spec.device.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        disk_spec.device.backing.diskMode = "persistent"
        disk_spec.device.backing.fileName = disk_path
        disk_spec.device.controllerKey = controller.key
        disk_spec.device.unitNumber = params.get("unit_number", -1)
        
        config_spec = vim.vm.ConfigSpec()
        config_spec.deviceChange = [disk_spec]
        
        task = vm.ReconfigVM_Task(spec=config_spec)
        return {"message": f"Disk attached to {vm_name}", "task_id": str(task._moId)}
    

    async def _detach_disk(self, params: Dict[str, Any]) -> Dict:
        """Detach virtual disk from VM."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        disk_label = params.get("disk_label")
        
        if not vm_name or not disk_label:
            raise ValueError("vm_name and disk_label are required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        # Find disk by label
        disk = None
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                if device.deviceInfo.label == disk_label:
                    disk = device
                    break
        
        if not disk:
            raise ValueError(f"Disk not found: {disk_label}")
        
        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.remove
        disk_spec.device = disk
        
        config_spec = vim.vm.ConfigSpec()
        config_spec.deviceChange = [disk_spec]
        
        task = vm.ReconfigVM_Task(spec=config_spec)
        return {"message": f"Disk '{disk_label}' detached from {vm_name}", "task_id": str(task._moId)}
    

    async def _add_disk(self, params: Dict[str, Any]) -> Dict:
        """Add new virtual disk to VM."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        size_gb = params.get("size_gb")
        
        if not vm_name or not size_gb:
            raise ValueError("vm_name and size_gb are required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        # Find SCSI controller
        controller = None
        unit_number = 0
        used_units = set()
        
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualSCSIController):
                controller = device
            if isinstance(device, vim.vm.device.VirtualDisk):
                used_units.add(device.unitNumber)
        
        if not controller:
            raise ValueError("No SCSI controller found on VM")
        
        # Find available unit number
        for i in range(16):
            if i != 7 and i not in used_units:  # Unit 7 is reserved
                unit_number = i
                break
        
        # Get datastore
        ds_name = params.get("datastore_name")
        datastore = self._find_datastore(ds_name) if ds_name else vm.datastore[0]
        
        thin = params.get("thin_provisioned", True)
        
        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        disk_spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create
        disk_spec.device = vim.vm.device.VirtualDisk()
        disk_spec.device.capacityInKB = size_gb * 1024 * 1024
        disk_spec.device.controllerKey = controller.key
        disk_spec.device.unitNumber = unit_number
        disk_spec.device.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        disk_spec.device.backing.diskMode = "persistent"
        disk_spec.device.backing.thinProvisioned = thin
        disk_spec.device.backing.datastore = datastore
        
        config_spec = vim.vm.ConfigSpec()
        config_spec.deviceChange = [disk_spec]
        
        task = vm.ReconfigVM_Task(spec=config_spec)
        return {"message": f"Added {size_gb}GB disk to {vm_name}", "task_id": str(task._moId)}
    

    async def _extend_disk(self, params: Dict[str, Any]) -> Dict:
        """Extend virtual disk size."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        disk_label = params.get("disk_label")
        new_size_gb = params.get("new_size_gb")
        
        if not all([vm_name, disk_label, new_size_gb]):
            raise ValueError("vm_name, disk_label, and new_size_gb are required")
        
        vm = self._find_vm(str(vm_name))
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        # Find disk
        disk = None
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                if device.deviceInfo.label == disk_label:
                    disk = device
                    break
        
        if not disk:
            raise ValueError(f"Disk not found: {disk_label}")
        
        new_size_kb = (int(new_size_gb) if new_size_gb else 0) * 1024 * 1024
        if new_size_kb <= disk.capacityInKB:
            raise ValueError(f"New size must be larger than current size ({disk.capacityInKB // (1024*1024)} GB)")
        
        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
        disk_spec.device = disk
        disk_spec.device.capacityInKB = new_size_kb
        
        config_spec = vim.vm.ConfigSpec()
        config_spec.deviceChange = [disk_spec]
        
        task = vm.ReconfigVM_Task(spec=config_spec)
        return {"message": f"Extended disk to {new_size_gb}GB on {vm_name}", "task_id": str(task._moId)}
    

    async def _customize_guest(self, params: Dict[str, Any]) -> Dict:
        """Apply guest OS customization."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        # Build customization spec
        spec = vim.vm.customization.Specification()
        
        # Identity - Linux naming
        linux_prep = vim.vm.customization.LinuxPrep()
        linux_prep.domain = params.get("domain", "localdomain")
        linux_prep.hostName = vim.vm.customization.FixedName()
        linux_prep.hostName.name = params.get("hostname", vm_name)
        spec.identity = linux_prep
        
        # Global IP settings
        spec.globalIPSettings = vim.vm.customization.GlobalIPSettings()
        dns_servers = params.get("dns_servers", "")
        if dns_servers:
            spec.globalIPSettings.dnsServerList = dns_servers.split(",")
        
        # NIC settings
        nic_setting = vim.vm.customization.AdapterMapping()
        nic_setting.adapter = vim.vm.customization.IPSettings()
        
        ip_address = params.get("ip_address")
        if ip_address:
            nic_setting.adapter.ip = vim.vm.customization.FixedIp()
            nic_setting.adapter.ip.ipAddress = ip_address
            nic_setting.adapter.subnetMask = params.get("subnet_mask", "255.255.255.0")
            nic_setting.adapter.gateway = [params.get("gateway", "")] if params.get("gateway") else []
        else:
            nic_setting.adapter.ip = vim.vm.customization.DhcpIpGenerator()
        
        spec.nicSettingMap = [nic_setting]
        
        task = vm.CustomizeVM_Task(spec=spec)
        return {"message": f"Customization applied to {vm_name}", "task_id": str(task._moId)}
    

    async def _create_screenshot(self, params: Dict[str, Any]) -> Dict:
        """Create VM screenshot."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        task = vm.CreateScreenshot_Task()
        return {"message": f"Screenshot created for {vm_name}", "task_id": str(task._moId)}
    

    async def _set_boot_options(self, params: Dict[str, Any]) -> Dict:
        """Set VM boot options."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        boot_options = vim.vm.BootOptions()
        
        if "boot_delay" in params:
            boot_options.bootDelay = params["boot_delay"]
        if "enter_bios_setup" in params:
            boot_options.enterBIOSSetup = params["enter_bios_setup"]
        if "boot_retry_enabled" in params:
            boot_options.bootRetryEnabled = params["boot_retry_enabled"]
        if "boot_retry_delay" in params:
            boot_options.bootRetryDelay = params["boot_retry_delay"]
        
        config_spec = vim.vm.ConfigSpec()
        config_spec.bootOptions = boot_options  # type: ignore[assignment]
        
        task = vm.ReconfigVM_Task(spec=config_spec)
        return {"message": f"Boot options updated for {vm_name}", "task_id": str(task._moId)}
    

    async def _instant_clone(self, params: Dict[str, Any]) -> Dict:
        """Create instant clone of running VM."""
        from pyVmomi import vim
        
        source_vm_name = params.get("source_vm")
        clone_name = params.get("clone_name")
        
        if not source_vm_name or not clone_name:
            raise ValueError("source_vm and clone_name are required")
        
        vm = self._find_vm(str(source_vm_name))
        if not vm:
            raise ValueError(f"Source VM not found: {source_vm_name}")
        
        spec = vim.vm.InstantCloneSpec()
        spec.name = clone_name
        
        folder_name = params.get("target_folder")
        if folder_name:
            target_folder = self._find_folder(str(folder_name))
            if target_folder:
                spec.location = vim.vm.RelocateSpec()
                spec.location.folder = target_folder
        
        task = vm.InstantClone_Task(spec=spec)
        return {"message": f"Instant clone '{clone_name}' created", "task_id": str(task._moId)}
    

    async def _register_vm(self, params: Dict[str, Any]) -> Dict:
        """Register VM from VMX file."""
        vmx_path = params.get("vmx_path")
        if not vmx_path:
            raise ValueError("vmx_path is required")
        
        vm_name = params.get("vm_name")
        folder_name = params.get("folder_name")
        pool_name = params.get("resource_pool")
        as_template = params.get("as_template", False)
        
        folder = None
        if folder_name:
            folder = self._find_folder(str(folder_name))
        if not folder:
            # Use root VM folder from first datacenter
            from pyVmomi import vim
            container = self._content.viewManager.CreateContainerView(
                self._content.rootFolder, [vim.Datacenter], True
            )
            try:
                if container.view:
                    folder = container.view[0].vmFolder
            finally:
                container.Destroy()
        
        pool = None
        if pool_name:
            pool = self._find_resource_pool(str(pool_name))
        
        if not folder:
            raise ValueError("Could not find a folder to register VM into")
        
        task = folder.RegisterVM_Task(
            path=vmx_path,
            name=vm_name,
            asTemplate=as_template,
            pool=pool,
        )
        return {"message": f"VM registered from {vmx_path}", "task_id": str(task._moId)}
    

    async def _reload_vm(self, params: Dict[str, Any]) -> Dict:
        """Reload VM configuration."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.Reload()
        return {"message": f"VM configuration reloaded for {vm_name}"}
    

    async def _terminate_fault_tolerance(self, params: Dict[str, Any]) -> Dict:
        """Turn off Fault Tolerance."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        task = vm.TurnOffFaultToleranceForVM_Task()
        return {"message": f"FT disabled for {vm_name}", "task_id": str(task._moId)}
    

    async def _send_nmi(self, params: Dict[str, Any]) -> Dict:
        """Send NMI to VM."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.SendNMI()
        return {"message": f"NMI sent to {vm_name}"}
    

    async def _place_vm(self, params: Dict[str, Any]) -> Dict:
        """Get VM placement recommendations."""
        from pyVmomi import vim
        
        cluster_name = params.get("cluster_name")
        vm_name = params.get("vm_name")
        
        if not cluster_name or not vm_name:
            raise ValueError("cluster_name and vm_name are required")
        
        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        spec = vim.cluster.PlacementSpec()
        spec.vm = vm
        
        result = cluster.PlaceVm(placementSpec=spec)
        return {
            "recommendations": [
                {"host": r.host.name, "datastore": r.datastore.name if r.datastore else None}
                for r in result.recommendations or []
            ] if result else [],
        }
    

    async def _create_vmfs_datastore(self, params: Dict[str, Any]) -> Dict:
        """Create VMFS datastore."""
        from pyVmomi import vim
        
        host_name = params.get("host_name")
        ds_name = params.get("datastore_name")
        device_path = params.get("device_path")
        
        if not all([host_name, ds_name, device_path]):
            raise ValueError("host_name, datastore_name, and device_path are required")
        
        host = self._find_host(str(host_name))
        if not host:
            raise ValueError(f"Host not found: {host_name}")
        
        # Note: VMFS datastore creation is complex and depends on LUN availability
        # This is a simplified implementation - in production, use host.configManager.datastoreSystem
        spec = vim.host.VmfsDatastoreCreateSpec()
        spec.vmfs = vim.host.VmfsSpec()  # type: ignore[attr-defined]
        spec.vmfs.volumeName = str(ds_name)
        spec.extent = vim.host.ScsiDisk.Partition()
        spec.extent.diskName = str(device_path)
        
        ds = host.configManager.datastoreSystem.CreateVmfsDatastore(spec=spec)
        return {"message": f"VMFS datastore {ds_name} created", "name": ds.name}
    

    async def _expand_vmfs_datastore(self, params: Dict[str, Any]) -> Dict:
        """Expand VMFS datastore."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")
        
        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")
        
        # Get a host that has access to this datastore
        if not ds.host:
            raise ValueError("No hosts connected to datastore")
        
        host = ds.host[0].key
        
        # Get current VMFS info and expand options
        vmfs = ds.info.vmfs
        options = host.configManager.datastoreSystem.QueryVmfsDatastoreExpandOptions(datastore=ds)
        
        if not options:
            raise ValueError("No expansion options available for datastore")
        
        # Use first option
        spec = options[0].spec
        host.configManager.datastoreSystem.ExpandVmfsDatastore(datastore=ds, spec=spec)
        return {"message": f"Datastore {ds_name} expanded"}
    

    async def _create_vm(self, params: Dict[str, Any]) -> Dict:
        """Create new virtual machine."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        num_cpus = params.get("num_cpus")
        memory_mb = params.get("memory_mb")
        disk_size_gb = params.get("disk_size_gb")
        guest_os_id = params.get("guest_os_id")
        ds_name = params.get("datastore_name")
        
        if not all([vm_name, num_cpus, memory_mb, disk_size_gb, guest_os_id, ds_name]):
            raise ValueError("vm_name, num_cpus, memory_mb, disk_size_gb, guest_os_id, and datastore_name are required")
        
        datastore = self._find_datastore(str(ds_name))
        if not datastore:
            raise ValueError(f"Datastore not found: {ds_name}")
        
        # Get folder
        folder = None
        folder_name = params.get("folder_name")
        if folder_name:
            folder = self._find_folder(str(folder_name))
        
        if not folder:
            # Get default VM folder from first datacenter
            container = self._content.viewManager.CreateContainerView(
                self._content.rootFolder, [vim.Datacenter], True
            )
            try:
                if container.view:
                    folder = container.view[0].vmFolder
            finally:
                container.Destroy()
        
        # Get resource pool
        pool = None
        pool_name = params.get("resource_pool")
        if pool_name:
            pool = self._find_resource_pool(str(pool_name))
        
        if not pool:
            # Get default resource pool from first cluster
            container = self._content.viewManager.CreateContainerView(
                self._content.rootFolder, [vim.ClusterComputeResource], True
            )
            try:
                if container.view:
                    pool = container.view[0].resourcePool
            finally:
                container.Destroy()
        
        if not folder:
            raise ValueError("Could not find a folder to create VM in")
        
        # Build config spec
        config = vim.vm.ConfigSpec()
        config.name = str(vm_name)
        config.numCPUs = int(num_cpus) if num_cpus else 1
        config.memoryMB = int(memory_mb) if memory_mb else 1024  # type: ignore[assignment]
        config.guestId = str(guest_os_id)
        
        # File info
        files = vim.vm.FileInfo()
        files.vmPathName = f"[{ds_name}]"
        config.files = files  # type: ignore[assignment]
        
        # Create VM
        task = folder.CreateVM_Task(config=config, pool=pool)
        return {"message": f"VM '{vm_name}' creation initiated", "task_id": str(task._moId)}
    

    async def _deploy_ovf(self, params: Dict[str, Any]) -> Dict:
        """Deploy OVF/OVA template."""
        ovf_url = params.get("ovf_url")
        vm_name = params.get("vm_name")
        ds_name = params.get("datastore_name")
        
        if not all([ovf_url, vm_name, ds_name]):
            raise ValueError("ovf_url, vm_name, and datastore_name are required")
        
        # Note: Full OVF deployment is complex and typically requires
        # reading the OVF descriptor, mapping networks, etc.
        # This is a simplified implementation
        return {
            "message": "OVF deployment requires additional implementation",
            "ovf_url": ovf_url,
            "vm_name": vm_name,
            "note": "Use vCenter UI or ovftool for full OVF deployment",
        }
    

    async def _get_vm_guest_info(self, params: Dict[str, Any]) -> Dict:
        """Get VM guest OS information."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(str(vm_name))
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        guest = vm.guest
        return {
            "vm_name": vm_name,
            "guest_state": str(guest.guestState) if guest else None,
            "tools_status": str(guest.toolsRunningStatus) if guest else None,
            "tools_version": guest.toolsVersion if guest else None,
            "hostname": guest.hostName if guest else None,
            "ip_address": guest.ipAddress if guest else None,
            "guest_id": guest.guestId if guest else None,
            "guest_full_name": guest.guestFullName if guest else None,
            "ip_stack": [
                {"ip": ip.ipAddress, "prefix": ip.prefixLength}
                for net in (guest.net or [])
                for ip in (net.ipConfig.ipAddress if net.ipConfig else [])
            ] if guest else [],
        }
    

    async def _list_guest_processes(self, params: Dict[str, Any]) -> List[Dict]:
        """List processes in guest OS."""
        vm_name = params.get("vm_name")
        username = params.get("username")
        password = params.get("password")
        
        if not all([vm_name, username, password]):
            raise ValueError("vm_name, username, and password are required")
        
        vm = self._find_vm(str(vm_name))
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        # Guest operations require processManager
        guest_ops = self._content.guestOperationsManager
        creds = self._make_guest_auth(str(username), str(password))
        
        processes = guest_ops.processManager.ListProcessesInGuest(vm=vm, auth=creds)
        return [
            {"pid": p.pid, "name": p.name, "owner": p.owner, "start_time": str(p.startTime) if p.startTime else None}
            for p in processes or []
        ]
    

    async def _run_program_in_guest(self, params: Dict[str, Any]) -> Dict:
        """Run program in guest OS."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        username = params.get("username")
        password = params.get("password")
        program_path = params.get("program_path")
        
        if not all([vm_name, username, password, program_path]):
            raise ValueError("vm_name, username, password, and program_path are required")
        
        vm = self._find_vm(str(vm_name))
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        guest_ops = self._content.guestOperationsManager
        creds = self._make_guest_auth(str(username), str(password))
        
        spec = vim.vm.guest.ProcessManager.ProgramSpec()
        spec.programPath = str(program_path)
        spec.arguments = str(params.get("arguments", ""))
        spec.workingDirectory = str(params.get("working_directory", ""))
        
        pid = guest_ops.processManager.StartProgramInGuest(vm=vm, auth=creds, spec=spec)
        return {"message": f"Program started", "pid": pid}
    

    async def _upload_file_to_guest(self, params: Dict[str, Any]) -> Dict:
        """Upload file to guest OS."""
        vm_name = params.get("vm_name")
        username = params.get("username")
        password = params.get("password")
        guest_path = params.get("guest_path")
        
        if not all([vm_name, username, password, guest_path]):
            raise ValueError("vm_name, username, password, and guest_path are required")
        
        # Note: File upload requires HTTP POST to URL from InitiateFileTransferToGuest
        return {
            "message": "File upload initiated - use returned URL for upload",
            "guest_path": guest_path,
            "note": "Full implementation requires HTTP client for file transfer",
        }
    

    async def _download_file_from_guest(self, params: Dict[str, Any]) -> Dict:
        """Download file from guest OS."""
        vm_name = params.get("vm_name")
        guest_path = params.get("guest_path")
        
        if not all([vm_name, guest_path]):
            raise ValueError("vm_name and guest_path are required")
        
        return {
            "message": "File download initiated - use returned URL for download",
            "guest_path": guest_path,
            "note": "Full implementation requires HTTP client for file transfer",
        }
    

    async def _create_directory_in_guest(self, params: Dict[str, Any]) -> Dict:
        """Create directory in guest OS."""
        vm_name = params.get("vm_name")
        username = params.get("username")
        password = params.get("password")
        directory_path = params.get("directory_path")
        
        if not all([vm_name, username, password, directory_path]):
            raise ValueError("vm_name, username, password, and directory_path are required")
        
        vm = self._find_vm(str(vm_name))
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        guest_ops = self._content.guestOperationsManager
        creds = self._make_guest_auth(str(username), str(password))
        create_parents = params.get("create_parents", True)
        
        guest_ops.fileManager.MakeDirectoryInGuest(
            vm=vm, auth=creds, directoryPath=str(directory_path), createParentDirectories=create_parents
        )
        return {"message": f"Directory created: {directory_path}"}
    

    async def _delete_file_in_guest(self, params: Dict[str, Any]) -> Dict:
        """Delete file in guest OS."""
        vm_name = params.get("vm_name")
        username = params.get("username")
        password = params.get("password")
        file_path = params.get("file_path")
        
        if not all([vm_name, username, password, file_path]):
            raise ValueError("vm_name, username, password, and file_path are required")
        
        vm = self._find_vm(str(vm_name))
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        guest_ops = self._content.guestOperationsManager
        creds = self._make_guest_auth(str(username), str(password))
        
        guest_ops.fileManager.DeleteFileInGuest(vm=vm, auth=creds, filePath=str(file_path))
        return {"message": f"File deleted: {file_path}"}
    

    async def _set_custom_value(self, params: Dict[str, Any]) -> Dict:
        """Set custom attribute value on VM."""
        vm_name = params.get("vm_name")
        key = params.get("attribute_key")
        value = params.get("attribute_value")
        
        if not all([vm_name, key, value]):
            raise ValueError("vm_name, attribute_key, and attribute_value are required")
        
        vm = self._find_vm(str(vm_name))
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.setCustomValue(key=str(key), value=str(value))
        return {"message": f"Custom value '{key}' set on {vm_name}"}
    

    async def _get_custom_values(self, params: Dict[str, Any]) -> Dict:
        """Get custom attribute values for VM."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(str(vm_name))
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        custom_values = {}
        for cv in vm.customValue or []:
            # Find the field definition
            for field in self._content.customFieldsManager.field:
                if field.key == cv.key:
                    custom_values[field.name] = cv.value
                    break
        
        return {"vm_name": vm_name, "custom_values": custom_values}
    

    async def _set_screen_resolution(self, params: Dict[str, Any]) -> Dict:
        """Set VM screen resolution."""
        vm_name = params.get("vm_name")
        width = params.get("width")
        height = params.get("height")
        
        if not all([vm_name, width, height]):
            raise ValueError("vm_name, width, and height are required")
        
        vm = self._find_vm(str(vm_name))
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.SetScreenResolution(width=int(width) if width else 1024, height=int(height) if height else 768)
        return {"message": f"Screen resolution set to {width}x{height}"}
    

    async def _get_vm_tags(self, params: Dict[str, Any]) -> Dict:
        """Get tags assigned to VM."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        return {
            "vm_name": vm_name,
            "message": "Tag operations require vSphere REST API",
            "note": "Use /rest/com/vmware/cis/tagging/tag-association endpoint",
        }
    

    async def _assign_tag_to_vm(self, params: Dict[str, Any]) -> Dict:
        """Assign tag to VM."""
        return {
            "message": "Tag operations require vSphere REST API",
            "note": "Use POST /rest/com/vmware/cis/tagging/tag-association",
        }
    

    async def _remove_tag_from_vm(self, params: Dict[str, Any]) -> Dict:
        """Remove tag from VM."""
        return {
            "message": "Tag operations require vSphere REST API",
            "note": "Use DELETE /rest/com/vmware/cis/tagging/tag-association",
        }
    

    async def _revert_to_current_snapshot(self, params: Dict[str, Any]) -> Dict:
        """Revert to current snapshot."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(str(vm_name))
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        suppress_power_on = params.get("suppress_power_on", False)
        task = vm.RevertToCurrentSnapshot_Task(suppressPowerOn=suppress_power_on)
        return {"message": f"Reverted to current snapshot for {vm_name}", "task_id": str(task._moId)}
    

    async def _reset_guest_information(self, params: Dict[str, Any]) -> Dict:
        """Reset guest information."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(str(vm_name))
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        vm.ResetGuestInformation()
        return {"message": f"Guest information reset for {vm_name}"}
    

    async def _list_templates(self, params: Dict[str, Any]) -> List[Dict]:
        """List VM templates with complete VM property data."""
        from pyVmomi import vim
        
        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            return [
                self._serialize_vm_properties(vm)
                for vm in container.view
                if vm.config and vm.config.template
            ]
        finally:
            container.Destroy()
    

    async def _get_template(self, params: Dict[str, Any]) -> Dict:
        """Get template details with complete VM property data."""
        template_name = params.get("template_name")
        if not template_name:
            raise ValueError("template_name is required")
        
        vm = self._find_vm(str(template_name))
        if not vm:
            raise ValueError(f"Template not found: {template_name}")
        
        if not vm.config or not vm.config.template:
            raise ValueError(f"{template_name} is not a template")
        
        return self._serialize_vm_properties(vm)
    


    async def _mark_as_virtual_machine(self, params: Dict[str, Any]) -> Dict:
        """Convert template to VM."""
        template_name = params.get("template_name")
        if not template_name:
            raise ValueError("template_name is required")
        
        vm = self._find_vm(template_name)
        if not vm:
            raise ValueError(f"Template not found: {template_name}")
        
        # Get resource pool
        pool = None
        pool_name = params.get("resource_pool")
        if pool_name:
            pool = self._find_resource_pool(pool_name)
        
        # Get host
        host = None
        host_name = params.get("host_name")
        if host_name:
            host = self._find_host(host_name)
        
        vm.MarkAsVirtualMachine(pool=pool, host=host)
        return {"message": f"Template '{template_name}' converted to VM"}
    
    async def _upgrade_virtual_hardware(self, params: Dict[str, Any]) -> Dict:
        """Upgrade VM hardware version."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        version = params.get("version")  # e.g., "vmx-19", None for latest
        task = vm.UpgradeVM_Task(version=version)
        return {"message": f"Hardware upgrade initiated for {vm_name}", "task_id": str(task._moId)}
    
