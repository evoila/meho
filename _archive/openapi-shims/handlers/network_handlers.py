"""
Network Operation Handlers

Mixin class containing 12 network operation handlers.
"""

from typing import List, Dict, Any, Optional


class NetworkHandlerMixin:
    """Mixin for network operation handlers."""
    
    # These will be provided by VMwareConnector (base class)
    _content: Any
    
    # Helper methods (will be provided by VMwareConnector) - stubs for type checking
    def _find_network(self, name: str) -> Optional[Any]: return None
    def _find_dvs(self, name: str) -> Optional[Any]: return None
    def _find_vm(self, name: str) -> Optional[Any]: return None
    
    # Serializer methods (will be provided by VMwareConnector) - stubs for type checking
    def _serialize_network_properties(self, network: Any) -> Dict[str, Any]: return {}
    def _serialize_dvs_properties(self, dvs: Any) -> Dict[str, Any]: return {}
    def _serialize_portgroup_properties(self, pg: Any) -> Dict[str, Any]: return {}
    
    async def _list_networks(self, params: Dict[str, Any]) -> List[Dict]:
        """List all networks with complete property data."""
        from pyVmomi import vim
        
        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.Network], True
        )
        try:
            return [self._serialize_network_properties(n) for n in container.view]
        finally:
            container.Destroy()
    

    async def _list_distributed_switches(self, params: Dict[str, Any]) -> List[Dict]:
        """List all distributed switches with complete DVS property data."""
        from pyVmomi import vim
        
        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.DistributedVirtualSwitch], True
        )
        try:
            return [self._serialize_dvs_properties(dvs) for dvs in container.view]
        finally:
            container.Destroy()
    

    async def _get_distributed_switch(self, params: Dict[str, Any]) -> Dict:
        """Get distributed switch details with complete DVS property data."""
        from pyVmomi import vim
        
        dvs_name = params.get("dvs_name")
        if not dvs_name:
            raise ValueError("dvs_name is required")
        
        dvs = self._find_dvs(dvs_name)
        if not dvs:
            raise ValueError(f"Distributed switch not found: {dvs_name}")
        
        return self._serialize_dvs_properties(dvs)
    

    async def _list_port_groups(self, params: Dict[str, Any]) -> List[Dict]:
        """List all port groups with complete property data."""
        from pyVmomi import vim
        
        port_groups = []
        
        # Scan all networks (includes standard and distributed port groups)
        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.Network], True
        )
        try:
            for net in container.view:
                # Use appropriate serializer based on type
                if 'DistributedVirtualPortgroup' in type(net).__name__:
                    port_groups.append(self._serialize_portgroup_properties(net))
                else:
                    port_groups.append(self._serialize_network_properties(net))
        finally:
            container.Destroy()
        
        return port_groups
    

    async def _get_port_group(self, params: Dict[str, Any]) -> Dict:
        """Get port group details with complete property data."""
        pg_name = params.get("portgroup_name")
        if not pg_name:
            raise ValueError("portgroup_name is required")
        
        from pyVmomi import vim
        
        # Check both standard networks and distributed portgroups
        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.Network], True
        )
        try:
            for net in container.view:
                if net.name == pg_name:
                    # Use appropriate serializer based on type
                    if 'DistributedVirtualPortgroup' in type(net).__name__:
                        return self._serialize_portgroup_properties(net)
                    else:
                        return self._serialize_network_properties(net)
            raise ValueError(f"Port group not found: {pg_name}")
        finally:
            container.Destroy()
    

    async def _create_dvs_portgroup(self, params: Dict[str, Any]) -> Dict:
        """Create DVS port group."""
        from pyVmomi import vim
        
        dvs_name = params.get("dvs_name")
        if not dvs_name:
            raise ValueError("dvs_name is required")
        
        portgroup_name = params.get("portgroup_name")
        if not portgroup_name:
            raise ValueError("portgroup_name is required")
        
        dvs = self._find_dvs(dvs_name)
        if not dvs:
            raise ValueError(f"DVS not found: {dvs_name}")
        
        spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec()
        spec.name = portgroup_name
        spec.type = "earlyBinding"
        spec.numPorts = params.get("num_ports", 8)
        
        vlan_id = params.get("vlan_id")
        if vlan_id:
            spec.defaultPortConfig = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy()
            spec.defaultPortConfig.vlan = vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec()
            spec.defaultPortConfig.vlan.vlanId = vlan_id
        
        task = dvs.CreateDVPortgroup_Task(spec=spec)
        return {"message": f"Port group '{portgroup_name}' creation initiated", "task_id": str(task._moId)}
    

    async def _destroy_dvs_portgroup(self, params: Dict[str, Any]) -> Dict:
        """Destroy DVS port group."""
        from pyVmomi import vim
        
        portgroup_name = params.get("portgroup_name")
        if not portgroup_name:
            raise ValueError("portgroup_name is required")
        
        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.dvs.DistributedVirtualPortgroup], True
        )
        try:
            for pg in container.view:
                if pg.name == portgroup_name:
                    task = pg.Destroy_Task()
                    return {"message": f"Port group '{portgroup_name}' destruction initiated", "task_id": str(task._moId)}
            raise ValueError(f"Port group not found: {portgroup_name}")
        finally:
            container.Destroy()
    

    async def _query_used_vlans(self, params: Dict[str, Any]) -> List[int]:
        """Query VLANs in use on DVS."""
        dvs_name = params.get("dvs_name")
        if not dvs_name:
            raise ValueError("dvs_name is required")
        
        dvs = self._find_dvs(dvs_name)
        if not dvs:
            raise ValueError(f"DVS not found: {dvs_name}")
        
        vlans = dvs.QueryUsedVlanIdInDvs()
        return list(vlans) if vlans else []
    

    async def _refresh_dvs_port_state(self, params: Dict[str, Any]) -> Dict:
        """Refresh DVS port state."""
        dvs_name = params.get("dvs_name")
        if not dvs_name:
            raise ValueError("dvs_name is required")
        
        dvs = self._find_dvs(dvs_name)
        if not dvs:
            raise ValueError(f"DVS not found: {dvs_name}")
        
        dvs.RefreshDVPortState(portKeys=None)
        return {"message": f"DVS port state refreshed for {dvs_name}"}
    

    async def _add_network_adapter(self, params: Dict[str, Any]) -> Dict:
        """Add network adapter to VM."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        network_name = params.get("network_name")
        
        if not vm_name or not network_name:
            raise ValueError("vm_name and network_name are required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        network = self._find_network(network_name)
        if not network:
            raise ValueError(f"Network not found: {network_name}")
        
        adapter_type = params.get("adapter_type", "vmxnet3")
        
        nic_spec = vim.vm.device.VirtualDeviceSpec()
        nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        
        if adapter_type == "vmxnet3":
            nic_spec.device = vim.vm.device.VirtualVmxnet3()
        elif adapter_type == "e1000e":
            nic_spec.device = vim.vm.device.VirtualE1000e()
        else:
            nic_spec.device = vim.vm.device.VirtualE1000()
        
        nic_spec.device.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
        nic_spec.device.backing.network = network
        nic_spec.device.backing.deviceName = network_name
        nic_spec.device.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
        nic_spec.device.connectable.startConnected = True
        nic_spec.device.connectable.connected = True
        
        config_spec = vim.vm.ConfigSpec()
        config_spec.deviceChange = [nic_spec]
        
        task = vm.ReconfigVM_Task(spec=config_spec)
        return {"message": f"Added network adapter to {vm_name}", "task_id": str(task._moId)}
    

    async def _remove_network_adapter(self, params: Dict[str, Any]) -> Dict:
        """Remove network adapter from VM."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        adapter_label = params.get("adapter_label")
        
        if not vm_name or not adapter_label:
            raise ValueError("vm_name and adapter_label are required")
        
        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        nic = None
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualEthernetCard):
                if device.deviceInfo.label == adapter_label:
                    nic = device
                    break
        
        if not nic:
            raise ValueError(f"Network adapter not found: {adapter_label}")
        
        nic_spec = vim.vm.device.VirtualDeviceSpec()
        nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.remove
        nic_spec.device = nic
        
        config_spec = vim.vm.ConfigSpec()
        config_spec.deviceChange = [nic_spec]
        
        task = vm.ReconfigVM_Task(spec=config_spec)
        return {"message": f"Removed {adapter_label} from {vm_name}", "task_id": str(task._moId)}
    

    async def _change_network(self, params: Dict[str, Any]) -> Dict:
        """Change VM network adapter's connected network."""
        from pyVmomi import vim
        
        vm_name = params.get("vm_name")
        adapter_label = params.get("adapter_label")
        network_name = params.get("network_name")
        
        if not all([vm_name, adapter_label, network_name]):
            raise ValueError("vm_name, adapter_label, and network_name are required")
        
        vm = self._find_vm(str(vm_name))
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")
        
        network = self._find_network(str(network_name))
        if not network:
            raise ValueError(f"Network not found: {network_name}")
        
        nic = None
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualEthernetCard):
                if device.deviceInfo.label == adapter_label:
                    nic = device
                    break
        
        if not nic:
            raise ValueError(f"Network adapter not found: {adapter_label}")
        
        nic_spec = vim.vm.device.VirtualDeviceSpec()
        nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
        nic_spec.device = nic
        nic_spec.device.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
        nic_spec.device.backing.network = network
        nic_spec.device.backing.deviceName = network_name
        
        config_spec = vim.vm.ConfigSpec()
        config_spec.deviceChange = [nic_spec]
        
        task = vm.ReconfigVM_Task(spec=config_spec)
        return {"message": f"Changed {adapter_label} to {network_name}", "task_id": str(task._moId)}
    

