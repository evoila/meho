# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Cluster Operation Handlers

Mixin class containing 13 cluster operation handlers.
"""

from typing import Any


class ClusterHandlerMixin:
    """Mixin for cluster operation handlers."""

    # These will be provided by VMwareConnector (base class)
    _content: Any

    # Helper methods (will be provided by VMwareConnector) - stubs for type checking
    def _find_cluster(self, name: str) -> Any | None:
        return None

    def _find_vm(self, name: str) -> Any | None:
        return None

    # Serializer methods (will be provided by VMwareConnector) - stubs for type checking
    def _serialize_cluster_properties(self, cluster: Any) -> dict[str, Any]:
        return {}

    async def _list_clusters(self, params: dict[str, Any]) -> list[dict]:
        """
        List all clusters with complete property data.

        OPTIMIZED: Uses PropertyCollector to fetch all cluster properties in ONE API call.
        """
        from pyVmomi import vim, vmodl

        cluster_properties = [
            "name",
            # Configuration
            "configuration.drsConfig.enabled",
            "configuration.drsConfig.defaultVmBehavior",
            "configuration.dasConfig.enabled",
            "configuration.dasConfig.hostMonitoring",
            # Summary
            "summaryEx.numHosts",
            "summaryEx.numEffectiveHosts",
            "summaryEx.totalCpu",
            "summaryEx.totalMemory",
            # Hosts
            "host",
            # DRS
            "drsRecommendation",
            "recommendation",
            "actionHistory",
            "migrationHistory",
            "drsFault",
            # HCI
            "hciConfig",
        ]

        container_view = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.ClusterComputeResource], True
        )

        try:
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities", path="view", skip=False, type=vim.view.ContainerView
            )

            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=container_view, skip=True, selectSet=[traversal_spec]
            )

            property_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=vim.ClusterComputeResource, pathSet=cluster_properties, all=False
            )

            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec], propSet=[property_spec]
            )

            results = self._content.propertyCollector.RetrieveContents([filter_spec])

            return [self._format_cluster_from_properties(obj) for obj in results]

        finally:
            container_view.Destroy()

    def _format_cluster_from_properties(self, obj: Any) -> dict[str, Any]:
        """Format cluster data from PropertyCollector results."""
        props = {prop.name: prop.val for prop in obj.propSet}

        result: dict[str, Any] = {"name": props.get("name", "")}

        # Configuration
        config_data: dict[str, Any] = {}
        if "configuration.drsConfig.enabled" in props:
            config_data["drs_enabled"] = props["configuration.drsConfig.enabled"]
        if "configuration.drsConfig.defaultVmBehavior" in props:
            config_data["drs_behavior"] = str(props["configuration.drsConfig.defaultVmBehavior"])
        if "configuration.dasConfig.enabled" in props:
            config_data["ha_enabled"] = props["configuration.dasConfig.enabled"]
        if "configuration.dasConfig.hostMonitoring" in props:
            config_data["ha_host_monitoring"] = props["configuration.dasConfig.hostMonitoring"]
        if config_data:
            result["configuration"] = config_data

        # Summary
        summary_data: dict[str, Any] = {}
        if "summaryEx.numHosts" in props:
            summary_data["num_hosts"] = props["summaryEx.numHosts"]
        if "summaryEx.numEffectiveHosts" in props:
            summary_data["num_effective_hosts"] = props["summaryEx.numEffectiveHosts"]
        if "summaryEx.totalCpu" in props:
            summary_data["total_cpu_mhz"] = props["summaryEx.totalCpu"]
        if props.get("summaryEx.totalMemory"):
            summary_data["total_memory_mb"] = props["summaryEx.totalMemory"] // (1024 * 1024)
        if summary_data:
            result["summary"] = summary_data

        # Hosts
        if props.get("host"):
            result["host_count"] = len(props["host"])
            result["hosts"] = [h.name for h in props["host"] if hasattr(h, "name")][:50]

        # DRS Recommendations
        if props.get("drsRecommendation"):
            result["drs_recommendation_count"] = len(props["drsRecommendation"])
            result["drs_recommendations"] = [
                {"key": rec.key, "rating": rec.rating, "reason": rec.reason}
                for rec in props["drsRecommendation"][:10]
            ]

        # Recommendation count
        if props.get("recommendation"):
            result["recommendation_count"] = len(props["recommendation"])

        # Action History count
        if props.get("actionHistory"):
            result["action_history_count"] = len(props["actionHistory"])

        # Migration History count
        if props.get("migrationHistory"):
            result["migration_history_count"] = len(props["migrationHistory"])

        # DRS Faults count
        if props.get("drsFault"):
            result["drs_fault_count"] = len(props["drsFault"])

        # HCI Config
        if props.get("hciConfig"):
            hci = props["hciConfig"]
            result["hci_config"] = {
                "workflow_state": str(hci.workflowState) if hasattr(hci, "workflowState") else None,
            }

        return result

    async def _get_cluster(self, params: dict[str, Any]) -> dict:
        """Get cluster details with complete property data."""
        cluster_name = params.get("cluster_name") or params.get("name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        return self._serialize_cluster_properties(cluster)

    async def _get_drs_recommendations(self, params: dict[str, Any]) -> list[dict]:
        """Get DRS recommendations for a cluster."""
        cluster_name = params.get("cluster_name") or params.get("name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        recommendations = cluster.recommendation or []

        return [
            {
                "key": rec.key,
                "rating": rec.rating,
                "reason": rec.reason,
                "reason_text": rec.reasonText,
                "target": rec.target.name if rec.target else None,
            }
            for rec in recommendations
        ]

    async def _apply_drs_recommendation(self, params: dict[str, Any]) -> dict:
        """Apply a DRS recommendation."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        key = params.get("recommendation_key")
        if not key:
            raise ValueError("recommendation_key is required")

        task = cluster.ApplyRecommendation(key)
        return {
            "message": f"Applied DRS recommendation {key}",
            "task_id": str(task._moId) if task else None,
        }

    async def _cancel_drs_recommendation(self, params: dict[str, Any]) -> dict:
        """Cancel DRS recommendation."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        key = params.get("recommendation_key")
        if not key:
            raise ValueError("recommendation_key is required")

        cluster.CancelRecommendation(key=key)
        return {"message": f"DRS recommendation {key} cancelled"}

    async def _refresh_drs_recommendations(self, params: dict[str, Any]) -> dict:
        """Refresh DRS recommendations."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        cluster.RefreshRecommendation()
        return {"message": f"DRS recommendations refreshed for {cluster_name}"}

    async def _get_cluster_resource_usage(self, params: dict[str, Any]) -> dict:
        """Get cluster resource usage."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        usage = cluster.GetResourceUsage()
        return {
            "cpu_used_mhz": usage.cpuUsedMHz,
            "cpu_capacity_mhz": usage.cpuCapacityMHz,
            "memory_used_mb": usage.memUsedMB,
            "memory_capacity_mb": usage.memCapacityMB,
            "storage_used_mb": usage.storageUsedMB,
            "storage_capacity_mb": usage.storageCapacityMB,
        }

    async def _get_cluster_ha_status(self, params: dict[str, Any]) -> dict:
        """Get cluster HA runtime info."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        ha_info = cluster.RetrieveDasAdvancedRuntimeInfo()
        return {
            "das_host_state": [
                {"host": h.name, "state": str(h.configState)}
                for h in getattr(ha_info, "dasHostInfo", {}).get("hostDasState", [])
            ]
            if ha_info
            else [],
        }

    async def _reconfigure_cluster(self, params: dict[str, Any]) -> dict:
        """Reconfigure cluster settings."""
        from pyVmomi import vim

        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        spec = vim.cluster.ConfigSpecEx()

        # DRS configuration
        if "drs_enabled" in params or "drs_automation_level" in params:
            drs_config = vim.cluster.DrsConfigInfo()
            if "drs_enabled" in params:
                drs_config.enabled = params["drs_enabled"]
            if "drs_automation_level" in params:
                level_map = {
                    "manual": vim.cluster.DrsConfigInfo.DrsBehavior.manual,
                    "partiallyAutomated": vim.cluster.DrsConfigInfo.DrsBehavior.partiallyAutomated,
                    "fullyAutomated": vim.cluster.DrsConfigInfo.DrsBehavior.fullyAutomated,
                }
                drs_config.defaultVmBehavior = level_map.get(params["drs_automation_level"])
            spec.drsConfig = drs_config  # type: ignore[assignment]

        # HA configuration
        if "ha_enabled" in params or "ha_host_monitoring" in params:
            das_config = vim.cluster.DasConfigInfo()
            if "ha_enabled" in params:
                das_config.enabled = params["ha_enabled"]
            if "ha_host_monitoring" in params:
                das_config.hostMonitoring = params["ha_host_monitoring"]
            spec.dasConfig = das_config  # type: ignore[assignment]

        task = cluster.ReconfigureComputeResource_Task(spec=spec, modify=True)
        return {"message": f"Cluster {cluster_name} reconfigured", "task_id": str(task._moId)}

    async def _destroy_cluster(self, params: dict[str, Any]) -> dict:
        """Destroy cluster."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        task = cluster.Destroy_Task()
        return {
            "message": f"Cluster {cluster_name} destruction initiated",
            "task_id": str(task._moId),
        }

    async def _rename_cluster(self, params: dict[str, Any]) -> dict:
        """Rename cluster."""
        cluster_name = params.get("cluster_name")
        new_name = params.get("new_name")

        if not cluster_name or not new_name:
            raise ValueError("cluster_name and new_name are required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        task = cluster.Rename_Task(newName=new_name)
        return {"message": f"Cluster renamed to {new_name}", "task_id": str(task._moId)}

    async def _get_evc_mode(self, params: dict[str, Any]) -> dict:
        """Get cluster EVC mode."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        evc_manager = cluster.EvcManager()
        evc_state = evc_manager.evcState if evc_manager else None

        return {
            "cluster_name": cluster_name,
            "evc_mode": evc_state.currentEVCModeKey if evc_state else None,
            "supported_modes": [m.key for m in evc_state.supportedEVCMode]
            if evc_state and evc_state.supportedEVCMode
            else [],
        }

    async def _get_cluster_performance(self, params: dict[str, Any]) -> dict:
        """Get cluster performance metrics."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        summary = cluster.summary
        return {
            "cluster_name": cluster_name,
            "total_cpu_mhz": summary.totalCpu,
            "total_memory_mb": summary.totalMemory // (1024 * 1024),
            "num_hosts": summary.numHosts,
            "num_effective_hosts": summary.numEffectiveHosts,
            "current_balance": summary.currentBalance
            if hasattr(summary, "currentBalance")
            else None,
        }

    async def _find_rules_for_vm(self, params: dict[str, Any]) -> list[dict]:
        """Find DRS rules for a VM."""
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

        rules = cluster.FindRulesForVm(vm=vm)
        return [
            {
                "name": rule.name,
                "enabled": rule.enabled,
                "mandatory": getattr(rule, "mandatory", None),
            }
            for rule in rules or []
        ]
