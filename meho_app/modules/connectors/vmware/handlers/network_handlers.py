# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Network Operation Handlers

Mixin class containing 12 network operation handlers.
"""

from typing import Any


class NetworkHandlerMixin:
    """Mixin for network operation handlers."""

    # These will be provided by VMwareConnector (base class)
    _content: Any

    # Helper methods (will be provided by VMwareConnector) - stubs for type checking
    def _find_network(self, name: str) -> Any | None:
        return None

    def _find_dvs(self, name: str) -> Any | None:
        return None

    def _find_vm(self, name: str) -> Any | None:
        return None

    # Serializer methods (will be provided by VMwareConnector) - stubs for type checking
    def _serialize_network_properties(self, network: Any) -> dict[str, Any]:
        return {}

    def _serialize_dvs_properties(self, dvs: Any) -> dict[str, Any]:
        return {}

    def _serialize_portgroup_properties(self, pg: Any) -> dict[str, Any]:
        return {}

    async def _list_networks(self, params: dict[str, Any]) -> list[dict]:
        """
        List all networks with complete property data.

        OPTIMIZED: Uses PropertyCollector to fetch all network properties in ONE API call.
        """
        from pyVmomi import vim, vmodl

        network_properties = [
            "name",
            "summary.accessible",
            "summary.name",
            "host",
            "vm",
        ]

        container_view = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.Network], True
        )

        try:
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities", path="view", skip=False, type=vim.view.ContainerView
            )

            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=container_view, skip=True, selectSet=[traversal_spec]
            )

            property_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=vim.Network, pathSet=network_properties, all=False
            )

            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec], propSet=[property_spec]
            )

            results = self._content.propertyCollector.RetrieveContents([filter_spec])

            return [self._format_network_from_properties(obj) for obj in results]

        finally:
            container_view.Destroy()

    def _format_network_from_properties(self, obj: Any) -> dict[str, Any]:
        """Format network data from PropertyCollector results."""
        props = {prop.name: prop.val for prop in obj.propSet}

        result: dict[str, Any] = {"name": props.get("name", "")}

        # Summary
        summary_data: dict[str, Any] = {}
        if "summary.accessible" in props:
            summary_data["accessible"] = props["summary.accessible"]
        if "summary.name" in props:
            summary_data["name"] = props["summary.name"]
        if summary_data:
            result["summary"] = summary_data

        # Hosts
        if props.get("host"):
            result["host_count"] = len(props["host"])
            result["hosts"] = [h.name for h in props["host"] if hasattr(h, "name")][:50]

        # VMs
        if props.get("vm"):
            result["vm_count"] = len(props["vm"])
            result["vms"] = [vm.name for vm in props["vm"] if hasattr(vm, "name")][:50]

        return result

    async def _list_distributed_switches(self, params: dict[str, Any]) -> list[dict]:
        """
        List all distributed switches with complete DVS property data.

        OPTIMIZED: Uses PropertyCollector to fetch all DVS properties in ONE API call.
        """
        from pyVmomi import vim, vmodl

        dvs_properties = [
            "name",
            "uuid",
            "summary.name",
            "summary.numPorts",
            "summary.productInfo",
            "summary.hostMember",
            "config.maxPorts",
            "config.numPorts",
            "config.numStandalonePorts",
            "capability.dvPortGroupOperationSupported",
            "capability.dvsOperationSupported",
            "runtime.hostMemberRuntime",
            "portgroup",
            "networkResourcePool",
        ]

        container_view = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.DistributedVirtualSwitch], True
        )

        try:
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities", path="view", skip=False, type=vim.view.ContainerView
            )

            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=container_view, skip=True, selectSet=[traversal_spec]
            )

            property_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=vim.DistributedVirtualSwitch, pathSet=dvs_properties, all=False
            )

            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec], propSet=[property_spec]
            )

            results = self._content.propertyCollector.RetrieveContents([filter_spec])

            return [self._format_dvs_from_properties(obj) for obj in results]

        finally:
            container_view.Destroy()

    def _format_dvs_from_properties(self, obj: Any) -> dict[str, Any]:
        """Format DVS data from PropertyCollector results."""
        props = {prop.name: prop.val for prop in obj.propSet}

        result: dict[str, Any] = {"name": props.get("name", "")}

        if "uuid" in props:
            result["uuid"] = props["uuid"]

        # Summary
        summary_data: dict[str, Any] = {}
        if "summary.name" in props:
            summary_data["name"] = props["summary.name"]
        if "summary.numPorts" in props:
            summary_data["num_ports"] = props["summary.numPorts"]
        if props.get("summary.productInfo"):
            summary_data["product_info"] = props["summary.productInfo"].version
        if props.get("summary.hostMember"):
            summary_data["host_member_count"] = len(props["summary.hostMember"])
        if summary_data:
            result["summary"] = summary_data

        # Config
        config_data: dict[str, Any] = {}
        if "config.maxPorts" in props:
            config_data["max_ports"] = props["config.maxPorts"]
        if "config.numPorts" in props:
            config_data["num_ports"] = props["config.numPorts"]
        if "config.numStandalonePorts" in props:
            config_data["num_standalone_ports"] = props["config.numStandalonePorts"]
        if config_data:
            result["config"] = config_data

        # Capability
        capability_data: dict[str, Any] = {}
        if "capability.dvPortGroupOperationSupported" in props:
            capability_data["dv_port_group_operation_supported"] = props[
                "capability.dvPortGroupOperationSupported"
            ]
        if "capability.dvsOperationSupported" in props:
            capability_data["dvs_operation_supported"] = props["capability.dvsOperationSupported"]
        if capability_data:
            result["capability"] = capability_data

        # Runtime
        if props.get("runtime.hostMemberRuntime"):
            result["runtime"] = {
                "host_member_runtime_count": len(props["runtime.hostMemberRuntime"])
            }

        # Portgroups
        if props.get("portgroup"):
            result["portgroup_count"] = len(props["portgroup"])
            result["portgroups"] = [pg.name for pg in props["portgroup"] if hasattr(pg, "name")][
                :50
            ]

        # Network Resource Pool
        if props.get("networkResourcePool"):
            result["network_resource_pool_count"] = len(props["networkResourcePool"])

        return result

    async def _get_distributed_switch(self, params: dict[str, Any]) -> dict:
        """Get distributed switch details with complete DVS property data."""

        dvs_name = params.get("dvs_name")
        if not dvs_name:
            raise ValueError("dvs_name is required")

        dvs = self._find_dvs(dvs_name)
        if not dvs:
            raise ValueError(f"Distributed switch not found: {dvs_name}")

        return self._serialize_dvs_properties(dvs)

    async def _list_port_groups(self, params: dict[str, Any]) -> list[dict]:
        """
        List all port groups with complete property data.

        OPTIMIZED: Uses PropertyCollector to fetch all portgroup properties in ONE API call.
        """
        from pyVmomi import vim, vmodl

        # Portgroup properties (for DistributedVirtualPortgroup)
        pg_properties = [
            "name",
            "key",
            "config.numPorts",
            "config.type",
            "config.defaultPortConfig",
            "portKeys",
        ]

        container_view = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.dvs.DistributedVirtualPortgroup], True
        )

        try:
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities", path="view", skip=False, type=vim.view.ContainerView
            )

            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=container_view, skip=True, selectSet=[traversal_spec]
            )

            property_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=vim.dvs.DistributedVirtualPortgroup, pathSet=pg_properties, all=False
            )

            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec], propSet=[property_spec]
            )

            results = self._content.propertyCollector.RetrieveContents([filter_spec])

            return [self._format_portgroup_from_properties(obj) for obj in results]

        finally:
            container_view.Destroy()

    def _format_portgroup_from_properties(self, obj: Any) -> dict[str, Any]:
        """Format portgroup data from PropertyCollector results."""
        props = {prop.name: prop.val for prop in obj.propSet}

        result: dict[str, Any] = {"name": props.get("name", "")}

        if "key" in props:
            result["key"] = props["key"]

        # Config
        config_data: dict[str, Any] = {}
        if "config.numPorts" in props:
            config_data["num_ports"] = props["config.numPorts"]
        if "config.type" in props:
            config_data["type"] = props["config.type"]
        if "config.defaultPortConfig" in props:
            config_data["default_port_config"] = str(props["config.defaultPortConfig"])[:100]
        if config_data:
            result["config"] = config_data

        # Port Keys
        if props.get("portKeys"):
            result["port_count"] = len(props["portKeys"])
            result["port_keys"] = props["portKeys"][:20]

        return result

    async def _get_port_group(self, params: dict[str, Any]) -> dict:
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
                    if "DistributedVirtualPortgroup" in type(net).__name__:
                        return self._serialize_portgroup_properties(net)
                    else:
                        return self._serialize_network_properties(net)
            raise ValueError(f"Port group not found: {pg_name}")
        finally:
            container.Destroy()

    async def _create_dvs_portgroup(self, params: dict[str, Any]) -> dict:
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
        return {
            "message": f"Port group '{portgroup_name}' creation initiated",
            "task_id": str(task._moId),
        }

    async def _destroy_dvs_portgroup(self, params: dict[str, Any]) -> dict:
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
                    return {
                        "message": f"Port group '{portgroup_name}' destruction initiated",
                        "task_id": str(task._moId),
                    }
            raise ValueError(f"Port group not found: {portgroup_name}")
        finally:
            container.Destroy()

    async def _query_used_vlans(self, params: dict[str, Any]) -> list[int]:
        """Query VLANs in use on DVS."""
        dvs_name = params.get("dvs_name")
        if not dvs_name:
            raise ValueError("dvs_name is required")

        dvs = self._find_dvs(dvs_name)
        if not dvs:
            raise ValueError(f"DVS not found: {dvs_name}")

        vlans = dvs.QueryUsedVlanIdInDvs()
        return list(vlans) if vlans else []

    async def _refresh_dvs_port_state(self, params: dict[str, Any]) -> dict:
        """Refresh DVS port state."""
        dvs_name = params.get("dvs_name")
        if not dvs_name:
            raise ValueError("dvs_name is required")

        dvs = self._find_dvs(dvs_name)
        if not dvs:
            raise ValueError(f"DVS not found: {dvs_name}")

        dvs.RefreshDVPortState(portKeys=None)
        return {"message": f"DVS port state refreshed for {dvs_name}"}

    async def _add_network_adapter(self, params: dict[str, Any]) -> dict:
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

    async def _remove_network_adapter(self, params: dict[str, Any]) -> dict:
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
            if isinstance(device, vim.vm.device.VirtualEthernetCard):  # noqa: SIM102 -- readability preferred over collapse
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

    async def _change_network(self, params: dict[str, Any]) -> dict:
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
            if isinstance(device, vim.vm.device.VirtualEthernetCard):  # noqa: SIM102 -- readability preferred over collapse
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
