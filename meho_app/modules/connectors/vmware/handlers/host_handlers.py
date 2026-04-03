# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Host Operation Handlers

Mixin class containing 35 host operation handlers.
"""

from typing import Any


class HostHandlerMixin:
    """Mixin for host operation handlers."""

    # These will be provided by VMwareConnector (base class)
    _content: Any

    # Helper methods (will be provided by VMwareConnector) - stubs for type checking
    def _find_host(self, _name: str) -> Any | None:
        return None

    def _find_vm(self, _name: str) -> Any | None:
        return None

    def _find_datastore(self, _name: str) -> Any | None:
        return None

    def _find_cluster(self, _name: str) -> Any | None:
        return None

    # Serializer methods (will be provided by VMwareConnector) - stubs for type checking
    def _serialize_host_properties(self, _host: Any) -> dict[str, Any]:
        return {}

    def _serialize_vm_properties(self, _vm: Any) -> dict[str, Any]:
        return {}

    def _serialize_datastore_properties(self, _ds: Any) -> dict[str, Any]:
        return {}

    def _serialize_network_properties(self, _network: Any) -> dict[str, Any]:
        return {}

    async def _list_hosts(self, _params: dict[str, Any]) -> list[dict]:
        """
        List all ESXi hosts with complete property data.

        OPTIMIZED: Uses PropertyCollector to fetch all host properties in ONE API call.
        """
        from pyVmomi import vim, vmodl

        # Define all properties to fetch upfront
        host_properties = [
            "name",
            # Runtime
            "runtime.connectionState",
            "runtime.powerState",
            "runtime.inMaintenanceMode",
            "runtime.standbyMode",
            "runtime.bootTime",
            # Summary hardware
            "summary.hardware.vendor",
            "summary.hardware.model",
            "summary.hardware.uuid",
            "summary.hardware.cpuMhz",
            "summary.hardware.numCpuCores",
            "summary.hardware.numCpuThreads",
            "summary.hardware.memorySize",
            # Summary quickStats
            "summary.quickStats.overallCpuUsage",
            "summary.quickStats.overallMemoryUsage",
            "summary.quickStats.uptime",
            "summary.overallStatus",
            # Hardware detailed
            "hardware.systemInfo",
            "hardware.cpuPkg",
            # Config
            "config.hyperThread",
            # Capability
            "capability.vmotionSupported",
            "capability.storageVMotionSupported",
            "capability.ftSupported",
            # Licensable resource
            "licensableResource.numCpuPackages",
            "licensableResource.numCpuCores",
            # Parent cluster
            "parent",
            # VMs and resources
            "vm",
            "datastore",
            "network",
        ]

        container_view = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.HostSystem], True
        )

        try:
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities", path="view", skip=False, type=vim.view.ContainerView
            )

            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=container_view, skip=True, selectSet=[traversal_spec]
            )

            property_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=vim.HostSystem, pathSet=host_properties, all=False
            )

            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec], propSet=[property_spec]
            )

            results = self._content.propertyCollector.RetrieveContents([filter_spec])

            return [self._format_host_from_properties(obj) for obj in results]

        finally:
            container_view.Destroy()

    def _format_host_from_properties(
        self, obj: Any
    ) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
        """Format host data from PropertyCollector results."""
        props = {prop.name: prop.val for prop in obj.propSet}

        result: dict[str, Any] = {"name": props.get("name", "")}

        # Runtime
        runtime_data: dict[str, Any] = {}
        if "runtime.connectionState" in props:
            runtime_data["connection_state"] = str(props["runtime.connectionState"])
        if "runtime.powerState" in props:
            runtime_data["power_state"] = str(props["runtime.powerState"])
        if "runtime.inMaintenanceMode" in props:
            runtime_data["maintenance_mode"] = props["runtime.inMaintenanceMode"]
        if "runtime.standbyMode" in props:
            runtime_data["standby_mode"] = (
                str(props["runtime.standbyMode"]) if props["runtime.standbyMode"] else None
            )
        if "runtime.bootTime" in props:
            runtime_data["boot_time"] = (
                str(props["runtime.bootTime"]) if props["runtime.bootTime"] else None
            )
        if runtime_data:
            result["runtime"] = runtime_data

        # Hardware summary
        hw_summary: dict[str, Any] = {}
        if "summary.hardware.vendor" in props:
            hw_summary["vendor"] = props["summary.hardware.vendor"]
        if "summary.hardware.model" in props:
            hw_summary["model"] = props["summary.hardware.model"]
        if "summary.hardware.uuid" in props:
            hw_summary["uuid"] = props["summary.hardware.uuid"]
        if "summary.hardware.cpuMhz" in props:
            hw_summary["cpu_mhz"] = props["summary.hardware.cpuMhz"]
        if "summary.hardware.numCpuCores" in props:
            hw_summary["cpu_cores"] = props["summary.hardware.numCpuCores"]
        if "summary.hardware.numCpuThreads" in props:
            hw_summary["cpu_threads"] = props["summary.hardware.numCpuThreads"]
        if props.get("summary.hardware.memorySize"):
            hw_summary["memory_mb"] = props["summary.hardware.memorySize"] // (1024 * 1024)
        if hw_summary:
            result["hardware_summary"] = hw_summary

        # Stats
        stats_data: dict[str, Any] = {}
        if "summary.quickStats.overallCpuUsage" in props:
            stats_data["cpu_usage_mhz"] = props["summary.quickStats.overallCpuUsage"]
        if "summary.quickStats.overallMemoryUsage" in props:
            stats_data["memory_usage_mb"] = props["summary.quickStats.overallMemoryUsage"]
        if "summary.quickStats.uptime" in props:
            stats_data["uptime_seconds"] = props["summary.quickStats.uptime"]
        if stats_data:
            result["stats"] = stats_data

        if "summary.overallStatus" in props:
            result["overall_status"] = str(props["summary.overallStatus"])

        # Hardware detailed
        hw_detailed: dict[str, Any] = {}
        if props.get("hardware.systemInfo"):
            si = props["hardware.systemInfo"]
            hw_detailed["system_info"] = {
                "vendor": si.vendor if hasattr(si, "vendor") else None,
                "model": si.model if hasattr(si, "model") else None,
            }
        if "hardware.cpuPkg" in props:
            hw_detailed["cpu_pkg_count"] = (
                len(props["hardware.cpuPkg"]) if props["hardware.cpuPkg"] else 0
            )
        if hw_detailed:
            result["hardware_detailed"] = hw_detailed

        # Config
        if props.get("config.hyperThread"):
            ht = props["config.hyperThread"]
            result["config"] = {
                "hyperthread_available": ht.available if hasattr(ht, "available") else None,
                "hyperthread_active": ht.active if hasattr(ht, "active") else None,
            }

        # Capability
        capability_data: dict[str, Any] = {}
        if "capability.vmotionSupported" in props:
            capability_data["vmotion_supported"] = props["capability.vmotionSupported"]
        if "capability.storageVMotionSupported" in props:
            capability_data["storage_vmotion_supported"] = props[
                "capability.storageVMotionSupported"
            ]
        if "capability.ftSupported" in props:
            capability_data["ft_supported"] = props["capability.ftSupported"]
        if capability_data:
            result["capability"] = capability_data

        # Licensable resource
        lic_data: dict[str, Any] = {}
        if "licensableResource.numCpuPackages" in props:
            lic_data["num_cpu_packages"] = props["licensableResource.numCpuPackages"]
        if "licensableResource.numCpuCores" in props:
            lic_data["num_cpu_cores"] = props["licensableResource.numCpuCores"]
        if lic_data:
            result["licensable_resource"] = lic_data

        # Parent cluster
        if props.get("parent"):
            result["cluster"] = props["parent"].name if hasattr(props["parent"], "name") else None

        # VMs
        if props.get("vm"):
            result["vm_count"] = len(props["vm"])
            result["vms"] = [vm.name for vm in props["vm"] if hasattr(vm, "name")][:50]

        # Datastores
        if props.get("datastore"):
            result["datastores"] = [ds.name for ds in props["datastore"] if hasattr(ds, "name")]

        # Networks
        if props.get("network"):
            result["networks"] = [net.name for net in props["network"] if hasattr(net, "name")]

        return result

    async def _get_host(self, params: dict[str, Any]) -> dict:
        """Get host details with complete property data."""
        host_name = params.get("host_name") or params.get("name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        return self._serialize_host_properties(host)

    async def _enter_maintenance_mode(self, params: dict[str, Any]) -> dict:
        """Put host into maintenance mode."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        timeout = params.get("timeout", 0)
        evacuate_vms = params.get("evacuate_vms", True)

        task = host.EnterMaintenanceMode(
            timeout=timeout,
            evacuatePoweredOffVms=evacuate_vms,
        )

        return {
            "message": f"Entering maintenance mode for {host_name}",
            "task_id": str(task._moId),
        }

    async def _exit_maintenance_mode(self, params: dict[str, Any]) -> dict:
        """Take host out of maintenance mode."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        timeout = params.get("timeout", 0)
        task = host.ExitMaintenanceMode(timeout=timeout)

        return {
            "message": f"Exiting maintenance mode for {host_name}",
            "task_id": str(task._moId),
        }

    async def _get_host_datastores(self, params: dict[str, Any]) -> list[dict]:
        """Get datastores accessible from a host with complete property data."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        return [self._serialize_datastore_properties(ds) for ds in host.datastore or []]

    async def _get_host_networks(self, params: dict[str, Any]) -> list[dict]:
        """Get networks on a host with complete property data."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        return [self._serialize_network_properties(net) for net in host.network or []]

    async def _get_host_performance(self, params: dict[str, Any]) -> dict:
        """Get host performance metrics."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        qs = host.summary.quickStats
        hw = host.summary.hardware

        return {
            "host_name": host_name,
            "quick_stats": {
                "cpu_usage_mhz": qs.overallCpuUsage if qs else None,
                "memory_usage_mb": qs.overallMemoryUsage if qs else None,
                "distributed_cpu_fairness": qs.distributedCpuFairness if qs else None,
                "distributed_memory_fairness": qs.distributedMemoryFairness if qs else None,
            },
            "capacity": {
                "total_cpu_mhz": hw.cpuMhz * hw.numCpuCores if hw else None,
                "total_memory_mb": hw.memorySize // (1024 * 1024) if hw else None,
            },
        }

    async def _refresh_storage_info(self, params: dict[str, Any]) -> dict:
        """Refresh VM storage info."""
        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")

        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")

        vm.RefreshStorageInfo()
        return {"message": f"Storage info refreshed for {vm_name}"}

    async def _reboot_host(self, params: dict[str, Any]) -> dict:
        """Reboot ESXi host."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        force = params.get("force", False)
        task = host.RebootHost_Task(force=force)
        return {"message": f"Reboot initiated for {host_name}", "task_id": str(task._moId)}

    async def _shutdown_host(self, params: dict[str, Any]) -> dict:
        """Shutdown ESXi host."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        force = params.get("force", False)
        task = host.ShutdownHost_Task(force=force)
        return {"message": f"Shutdown initiated for {host_name}", "task_id": str(task._moId)}

    async def _disconnect_host(self, params: dict[str, Any]) -> dict:
        """Disconnect host from vCenter."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        task = host.DisconnectHost_Task()
        return {"message": f"Disconnect initiated for {host_name}", "task_id": str(task._moId)}

    async def _reconnect_host(self, params: dict[str, Any]) -> dict:
        """Reconnect host to vCenter."""
        from pyVmomi import vim

        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        cnx_spec = None
        username = params.get("username")
        password = params.get("password")
        if username and password:
            cnx_spec = vim.host.ConnectSpec()
            cnx_spec.userName = username
            cnx_spec.password = password

        task = host.ReconnectHost_Task(cnxSpec=cnx_spec)
        return {"message": f"Reconnect initiated for {host_name}", "task_id": str(task._moId)}

    async def _enter_lockdown_mode(self, params: dict[str, Any]) -> dict:
        """Enter lockdown mode."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        host.EnterLockdownMode()
        return {"message": f"Lockdown mode entered for {host_name}"}

    async def _exit_lockdown_mode(self, params: dict[str, Any]) -> dict:
        """Exit lockdown mode."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        host.ExitLockdownMode()
        return {"message": f"Lockdown mode exited for {host_name}"}

    async def _enter_standby_mode(self, params: dict[str, Any]) -> dict:
        """Enter standby mode (power down)."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        timeout = params.get("timeout", 0)
        evacuate_vms = params.get("evacuate_vms", True)

        task = host.PowerDownHostToStandBy_Task(
            timeoutSec=timeout,
            evacuatePoweredOffVms=evacuate_vms,
        )
        return {"message": f"Standby mode initiated for {host_name}", "task_id": str(task._moId)}

    async def _exit_standby_mode(self, params: dict[str, Any]) -> dict:
        """Exit standby mode (power up)."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        timeout = params.get("timeout", 0)
        task = host.PowerUpHostFromStandBy_Task(timeoutSec=timeout)
        return {"message": f"Power up initiated for {host_name}", "task_id": str(task._moId)}

    async def _query_host_connection_info(self, params: dict[str, Any]) -> dict:
        """Query host connection info."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        info = host.QueryHostConnectionInfo()
        return {
            "host": info.host.name if info.host else None,
            "connected": info.connected,
        }

    async def _retrieve_hardware_uptime(self, params: dict[str, Any]) -> dict:
        """Get host hardware uptime."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        uptime = host.RetrieveHardwareUptime()
        return {
            "host_name": host_name,
            "uptime_seconds": uptime,
            "uptime_days": uptime // 86400 if uptime else 0,
        }

    async def _add_host_to_cluster(self, params: dict[str, Any]) -> dict:
        """Add host to cluster."""
        from pyVmomi import vim

        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        host_name = params.get("host_name")
        username = params.get("username")
        password = params.get("password")

        if not all([host_name, username, password]):
            raise ValueError("host_name, username, and password are required")

        spec = vim.host.ConnectSpec()
        spec.hostName = host_name
        spec.userName = username
        spec.password = password
        spec.force = True

        as_connected = params.get("as_connected", True)

        task = cluster.AddHost_Task(spec=spec, asConnected=as_connected)
        return {"message": f"Host {host_name} addition initiated", "task_id": str(task._moId)}

    async def _move_host_into_cluster(self, params: dict[str, Any]) -> dict:
        """Move host into cluster."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        task = cluster.MoveHostInto_Task(host=host, resourcePool=None)
        return {"message": f"Host {host_name} move initiated", "task_id": str(task._moId)}

    async def _enter_datastore_maintenance_mode(self, params: dict[str, Any]) -> dict:
        """Enter datastore maintenance mode."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")

        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")

        result = ds.DatastoreEnterMaintenanceMode()
        return {
            "message": f"Datastore {ds_name} entering maintenance mode",
            "recommendations": len(result.recommendations) if result.recommendations else 0,
        }

    async def _exit_datastore_maintenance_mode(self, params: dict[str, Any]) -> dict:
        """Exit datastore maintenance mode."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")

        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")

        task = ds.DatastoreExitMaintenanceMode_Task()
        return {
            "message": f"Datastore {ds_name} exiting maintenance mode",
            "task_id": str(task._moId),
        }

    async def _query_memory_overhead(self, params: dict[str, Any]) -> dict:
        """Query memory overhead for VM on host."""
        host_name = params.get("host_name")
        vm_name = params.get("vm_name")

        if not host_name or not vm_name:
            raise ValueError("host_name and vm_name are required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")

        overhead = host.QueryMemoryOverheadEx(vmConfigInfo=vm.config)
        return {"vm_name": vm_name, "memory_overhead_bytes": overhead}

    async def _update_host_flags(self, params: dict[str, Any]) -> dict:
        """Update host flags."""
        from pyVmomi import vim

        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        flag_info = vim.host.FlagInfo()
        if "enable_management" in params:
            flag_info.backgroundSnapshotsEnabled = params["enable_management"]

        host.UpdateFlags(flagInfo=flag_info)
        return {"message": f"Flags updated for {host_name}"}

    async def _query_tpm_attestation(self, params: dict[str, Any]) -> dict:
        """Query TPM attestation report."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        report = host.QueryTpmAttestationReport()
        return {
            "host_name": host_name,
            "tpm_present": report is not None,
        }

    async def _scan_host_storage(self, params: dict[str, Any]) -> dict:
        """Rescan host storage."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        host.configManager.storageSystem.RescanAllHba()
        host.configManager.storageSystem.RescanVmfs()
        return {"message": f"Storage rescan completed for {host_name}"}

    async def _refresh_host_services(self, params: dict[str, Any]) -> dict:
        """Refresh host services list."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        host.configManager.serviceSystem.RefreshServices()
        return {"message": f"Services refreshed for {host_name}"}

    async def _get_host_services(self, params: dict[str, Any]) -> list[dict]:
        """Get host services."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        services = host.configManager.serviceSystem.serviceInfo.service
        return [
            {
                "key": s.key,
                "label": s.label,
                "running": s.running,
                "policy": s.policy,
            }
            for s in services or []
        ]

    async def _start_host_service(self, params: dict[str, Any]) -> dict:
        """Start host service."""
        host_name = params.get("host_name")
        service_key = params.get("service_key")

        if not host_name or not service_key:
            raise ValueError("host_name and service_key are required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        host.configManager.serviceSystem.StartService(id=service_key)
        return {"message": f"Service '{service_key}' started on {host_name}"}

    async def _stop_host_service(self, params: dict[str, Any]) -> dict:
        """Stop host service."""
        host_name = params.get("host_name")
        service_key = params.get("service_key")

        if not host_name or not service_key:
            raise ValueError("host_name and service_key are required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        host.configManager.serviceSystem.StopService(id=service_key)
        return {"message": f"Service '{service_key}' stopped on {host_name}"}

    async def _restart_host_service(self, params: dict[str, Any]) -> dict:
        """Restart host service."""
        host_name = params.get("host_name")
        service_key = params.get("service_key")

        if not host_name or not service_key:
            raise ValueError("host_name and service_key are required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        host.configManager.serviceSystem.RestartService(id=service_key)
        return {"message": f"Service '{service_key}' restarted on {host_name}"}

    async def _get_host_firewall_rules(self, params: dict[str, Any]) -> list[dict]:
        """Get host firewall rules."""
        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        rulesets = host.configManager.firewallSystem.firewallInfo.ruleset
        return [
            {
                "key": r.key,
                "label": r.label,
                "enabled": r.enabled,
                "required": r.required,
            }
            for r in rulesets or []
        ]

    async def _enable_firewall_ruleset(self, params: dict[str, Any]) -> dict:
        """Enable firewall ruleset."""
        host_name = params.get("host_name")
        ruleset_key = params.get("ruleset_key")

        if not host_name or not ruleset_key:
            raise ValueError("host_name and ruleset_key are required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        host.configManager.firewallSystem.EnableRuleset(id=ruleset_key)
        return {"message": f"Ruleset '{ruleset_key}' enabled on {host_name}"}

    async def _disable_firewall_ruleset(self, params: dict[str, Any]) -> dict:
        """Disable firewall ruleset."""
        host_name = params.get("host_name")
        ruleset_key = params.get("ruleset_key")

        if not host_name or not ruleset_key:
            raise ValueError("host_name and ruleset_key are required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        host.configManager.firewallSystem.DisableRuleset(id=ruleset_key)
        return {"message": f"Ruleset '{ruleset_key}' disabled on {host_name}"}

    async def _cluster_enter_maintenance_mode(self, params: dict[str, Any]) -> dict:
        """Put cluster into maintenance mode."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        result = cluster.ClusterEnterMaintenanceMode()
        return {
            "message": f"Cluster {cluster_name} entering maintenance mode",
            "recommendations": len(result.recommendations) if result.recommendations else 0,
        }
