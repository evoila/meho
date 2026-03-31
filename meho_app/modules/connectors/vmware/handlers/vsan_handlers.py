# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
vSAN Operation Handlers

Mixin class containing 6 vSAN health and storage operation handlers.
"""

from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class VsanHandlerMixin:
    """vSAN health and storage operations for VMware connector."""

    # These will be provided by VMwareConnector (base class)
    _content: Any

    # Helper methods (will be provided by VMwareConnector) - stubs for type checking
    def _find_cluster(self, name: str) -> Any | None:
        return None

    def _check_vsan_enabled(
        self, cluster: Any, cluster_name: str
    ) -> dict[str, Any] | None:
        """Check if vSAN is enabled on the cluster. Returns error dict if not enabled, None if OK."""
        vsan_config = getattr(cluster.configurationEx, "vsanConfigInfo", None)
        if not vsan_config or not getattr(vsan_config, "enabled", False):
            return {
                "cluster_name": cluster_name,
                "vsan_enabled": False,
                "message": "vSAN is not configured on this cluster",
            }
        return None

    async def _get_vsan_cluster_health(self, params: dict[str, Any]) -> dict:
        """Get vSAN cluster configuration and health status."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        not_enabled = self._check_vsan_enabled(cluster, cluster_name)
        if not_enabled:
            return not_enabled

        try:
            vsan_config = cluster.configurationEx.vsanConfigInfo

            default_config = getattr(vsan_config, "defaultConfig", None)
            default_policy_uuid = (
                str(default_config.uuid) if default_config and hasattr(default_config, "uuid") else None
            )

            auto_claim = getattr(vsan_config, "autoClaimStorage", None)

            return {
                "cluster_name": cluster_name,
                "vsan_enabled": True,
                "default_policy": default_policy_uuid,
                "auto_claim_storage": auto_claim,
            }
        except Exception as e:
            logger.warning(f"Error reading vSAN cluster health for {cluster_name}: {e}")
            raise ValueError(f"Failed to read vSAN cluster health: {e}") from e

    async def _get_vsan_disk_groups(self, params: dict[str, Any]) -> list[dict] | dict:
        """Get vSAN disk groups per host in the cluster."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        not_enabled = self._check_vsan_enabled(cluster, cluster_name)
        if not_enabled:
            return not_enabled

        disk_groups = []
        for host in cluster.host or []:
            host_name = getattr(host, "name", "unknown")
            try:
                vsan_system = getattr(
                    getattr(host, "configManager", None), "vsanSystem", None
                )
                if not vsan_system:
                    disk_groups.append(
                        {"host": host_name, "message": "vSAN system not available"}
                    )
                    continue

                config = getattr(vsan_system, "config", None)
                storage_info = getattr(config, "storageInfo", None) if config else None
                disk_mappings = (
                    getattr(storage_info, "diskMapping", None) if storage_info else None
                )

                if not disk_mappings:
                    disk_groups.append(
                        {"host": host_name, "disk_groups": [], "message": "No disk mappings found"}
                    )
                    continue

                for mapping in disk_mappings:
                    ssd = getattr(mapping, "ssd", None)
                    non_ssd = getattr(mapping, "nonSsd", None) or []
                    disk_groups.append(
                        {
                            "host": host_name,
                            "ssd": getattr(ssd, "displayName", "unknown") if ssd else "unknown",
                            "capacity_disks": [
                                getattr(d, "displayName", "unknown") for d in non_ssd
                            ],
                        }
                    )
            except Exception as e:
                logger.warning(f"Error reading disk groups for host {host_name}: {e}")
                disk_groups.append({"host": host_name, "error": str(e)})

        return disk_groups

    async def _get_vsan_capacity(self, params: dict[str, Any]) -> dict:
        """Get vSAN datastore capacity information for a cluster."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        not_enabled = self._check_vsan_enabled(cluster, cluster_name)
        if not_enabled:
            return not_enabled

        # Find the vSAN datastore on this cluster
        vsan_ds = None
        for ds in cluster.datastore or []:
            try:
                if getattr(ds.summary, "type", "") == "vsan":
                    vsan_ds = ds
                    break
            except Exception:
                continue

        if not vsan_ds:
            return {
                "cluster_name": cluster_name,
                "vsan_enabled": True,
                "message": "No vSAN datastore found on cluster",
            }

        try:
            summary = vsan_ds.summary
            capacity_bytes = getattr(summary, "capacity", 0) or 0
            free_bytes = getattr(summary, "freeSpace", 0) or 0
            uncommitted_bytes = getattr(summary, "uncommitted", None)

            capacity_gb = round(capacity_bytes / 1073741824, 2)
            free_gb = round(free_bytes / 1073741824, 2)
            uncommitted_gb = (
                round(uncommitted_bytes / 1073741824, 2) if uncommitted_bytes else None
            )
            provisioned_gb = round(
                (capacity_bytes - free_bytes + (uncommitted_bytes or 0)) / 1073741824, 2
            )

            return {
                "cluster_name": cluster_name,
                "datastore_name": vsan_ds.name,
                "capacity_gb": capacity_gb,
                "free_space_gb": free_gb,
                "uncommitted_gb": uncommitted_gb,
                "provisioned_gb": provisioned_gb,
            }
        except Exception as e:
            logger.warning(f"Error reading vSAN capacity for {cluster_name}: {e}")
            raise ValueError(f"Failed to read vSAN capacity: {e}") from e

    async def _get_vsan_resync_status(self, params: dict[str, Any]) -> dict:
        """Get vSAN resync status for a cluster."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        not_enabled = self._check_vsan_enabled(cluster, cluster_name)
        if not_enabled:
            return not_enabled

        # Check if vsanInternalSystem is available (requires vSAN SDK stubs)
        vsan_internal = getattr(self._content, "vsanInternalSystem", None)
        if not vsan_internal:
            return {
                "cluster_name": cluster_name,
                "message": "vSAN resync status requires vSAN SDK stubs. Use get_vsan_cluster_health for basic status.",
            }

        try:
            syncing_objects = vsan_internal.QuerySyncingVsanObjects(cluster._moId)
            return {
                "cluster_name": cluster_name,
                "resyncing_objects": len(syncing_objects) if syncing_objects else 0,
                "details": str(syncing_objects)[:500] if syncing_objects else None,
            }
        except Exception as e:
            logger.warning(f"Error querying vSAN resync for {cluster_name}: {e}")
            return {
                "cluster_name": cluster_name,
                "message": f"Could not query resync status: {e}. Use get_vsan_cluster_health for basic status.",
            }

    async def _get_vsan_storage_policies(self, params: dict[str, Any]) -> dict:
        """Get vSAN storage policies (default policy from cluster config)."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        not_enabled = self._check_vsan_enabled(cluster, cluster_name)
        if not_enabled:
            return not_enabled

        try:
            vsan_config = cluster.configurationEx.vsanConfigInfo
            default_config = getattr(vsan_config, "defaultConfig", None)

            policy_info: dict[str, Any] = {
                "cluster_name": cluster_name,
                "vsan_enabled": True,
            }

            if default_config:
                policy_info["default_policy_uuid"] = (
                    str(default_config.uuid) if hasattr(default_config, "uuid") else None
                )
                # Extract basic policy attributes if available
                if hasattr(default_config, "hostFailuresToTolerate"):
                    policy_info["host_failures_to_tolerate"] = (
                        default_config.hostFailuresToTolerate
                    )
                if hasattr(default_config, "stripeWidth"):
                    policy_info["stripe_width"] = default_config.stripeWidth
                if hasattr(default_config, "forceProvisioning"):
                    policy_info["force_provisioning"] = default_config.forceProvisioning
            else:
                policy_info["message"] = (
                    "Default policy config not available. "
                    "Full PBM policy queries require separate PBM service connection."
                )

            return policy_info
        except Exception as e:
            logger.warning(f"Error reading vSAN policies for {cluster_name}: {e}")
            raise ValueError(f"Failed to read vSAN storage policies: {e}") from e

    async def _get_vsan_objects_health(self, params: dict[str, Any]) -> dict | list:
        """Get vSAN object and disk health status per host."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        not_enabled = self._check_vsan_enabled(cluster, cluster_name)
        if not_enabled:
            return not_enabled

        # Try vsanInternalSystem first
        vsan_internal = getattr(self._content, "vsanInternalSystem", None)
        if vsan_internal:
            try:
                syncing = vsan_internal.QuerySyncingVsanObjects(cluster._moId)
                return {
                    "cluster_name": cluster_name,
                    "source": "vsanInternalSystem",
                    "objects_resyncing": len(syncing) if syncing else 0,
                }
            except Exception as e:
                logger.warning(f"vsanInternalSystem query failed, falling back to per-host: {e}")

        # Fallback: per-host vSAN disk status
        host_health: list[dict[str, Any]] = []
        for host in cluster.host or []:
            host_name = getattr(host, "name", "unknown")
            try:
                vsan_system = getattr(
                    getattr(host, "configManager", None), "vsanSystem", None
                )
                if not vsan_system:
                    host_health.append(
                        {"host": host_name, "status": "vsan_system_unavailable"}
                    )
                    continue

                config = getattr(vsan_system, "config", None)
                storage_info = getattr(config, "storageInfo", None) if config else None
                disk_mappings = (
                    getattr(storage_info, "diskMapping", None) if storage_info else None
                )

                disk_count = 0
                if disk_mappings:
                    for mapping in disk_mappings:
                        ssd = getattr(mapping, "ssd", None)
                        non_ssd = getattr(mapping, "nonSsd", None) or []
                        disk_count += 1 + len(non_ssd)

                host_health.append(
                    {
                        "host": host_name,
                        "status": "ok" if disk_mappings else "no_disk_mappings",
                        "disk_group_count": len(disk_mappings) if disk_mappings else 0,
                        "total_disks": disk_count,
                    }
                )
            except Exception as e:
                logger.warning(f"Error reading vSAN health for host {host_name}: {e}")
                host_health.append({"host": host_name, "status": "error", "error": str(e)})

        return {
            "cluster_name": cluster_name,
            "source": "per_host_disk_status",
            "hosts": host_health,
        }
