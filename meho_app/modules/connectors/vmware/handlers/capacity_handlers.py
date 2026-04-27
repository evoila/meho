# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Capacity Planning Operation Handlers

Mixin class containing 4 capacity planning aggregation operation handlers.
"""

from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class CapacityHandlerMixin:
    """Capacity planning aggregation operations for VMware connector."""

    # These will be provided by VMwareConnector (base class)
    _content: Any

    # Helper methods (will be provided by VMwareConnector) - stubs for type checking
    def _find_cluster(self, _name: str) -> Any | None:
        return None

    async def _get_cluster_capacity(self, params: dict[str, Any]) -> dict:
        """Get cluster-level capacity summary: CPU, memory, hosts, VMs."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        try:
            summary = cluster.summary

            total_cpu_mhz = getattr(summary, "totalCpu", 0) or 0
            effective_cpu_mhz = getattr(summary, "effectiveCpu", 0) or 0
            total_memory_bytes = getattr(summary, "totalMemory", 0) or 0
            effective_memory_bytes = getattr(summary, "effectiveMemory", 0) or 0
            num_hosts = getattr(summary, "numHosts", 0) or 0
            num_effective_hosts = getattr(summary, "numEffectiveHosts", 0) or 0
            num_cpu_cores = getattr(summary, "numCpuCores", 0) or 0

            # Count VMs via resource pool
            num_vms = 0
            try:
                resource_pool = getattr(cluster, "resourcePool", None)
                if resource_pool:
                    vm_list = getattr(resource_pool, "vm", None)
                    if vm_list:
                        num_vms = len(vm_list)
            except Exception as e:
                logger.warning(f"Could not count VMs in cluster {cluster_name}: {e}")

            return {
                "cluster_name": cluster_name,
                "total_cpu_mhz": total_cpu_mhz,
                "effective_cpu_mhz": effective_cpu_mhz,
                "total_memory_gb": round(total_memory_bytes / 1073741824, 2),
                "effective_memory_gb": round(effective_memory_bytes / 1073741824, 2)
                if effective_memory_bytes > 1073741824
                else round(effective_memory_bytes / 1024, 2),
                "num_hosts": num_hosts,
                "num_effective_hosts": num_effective_hosts,
                "num_cpu_cores": num_cpu_cores,
                "num_vms": num_vms,
            }
        except Exception as e:
            logger.warning(f"Error reading cluster capacity for {cluster_name}: {e}")
            raise ValueError(f"Failed to read cluster capacity: {e}") from e

    async def _get_cluster_overcommitment(
        self, params: dict[str, Any]
    ) -> dict:  # NOSONAR (cognitive complexity)
        """Get CPU and memory overcommitment ratios for a cluster."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        try:
            summary = cluster.summary
            num_cpu_cores = getattr(summary, "numCpuCores", 0) or 0
            total_memory_bytes = getattr(summary, "totalMemory", 0) or 0

            # Gather VM resource allocations
            total_vcpus = 0
            total_vm_memory_mb = 0
            num_vms = 0

            # Get VMs from resource pool or iterate hosts
            vms: list[Any] = []
            try:
                resource_pool = getattr(cluster, "resourcePool", None)
                if resource_pool:
                    vm_list = getattr(resource_pool, "vm", None)
                    if vm_list:
                        vms = list(vm_list)
            except Exception:
                pass

            # Fallback: iterate host VMs if resource pool didn't work
            if not vms:
                for host in cluster.host or []:
                    try:
                        host_vms = getattr(host, "vm", None) or []
                        vms.extend(host_vms)
                    except Exception:
                        continue

            for vm in vms:
                try:
                    config = getattr(vm, "config", None)
                    if not config:
                        continue
                    hardware = getattr(config, "hardware", None)
                    if not hardware:
                        continue
                    vcpus = getattr(hardware, "numCPU", 0) or 0
                    mem_mb = getattr(hardware, "memoryMB", 0) or 0
                    total_vcpus += vcpus
                    total_vm_memory_mb += mem_mb
                    num_vms += 1
                except Exception as e:
                    logger.warning(f"Could not read VM config in cluster {cluster_name}: {e}")

            # Calculate ratios
            vcpu_pcpu_ratio = round(total_vcpus / num_cpu_cores, 2) if num_cpu_cores > 0 else 0.0
            total_physical_memory_mb = total_memory_bytes / 1048576
            memory_overcommit_ratio = (
                round(total_vm_memory_mb / total_physical_memory_mb, 2)
                if total_physical_memory_mb > 0
                else 0.0
            )

            return {
                "cluster_name": cluster_name,
                "total_vcpus": total_vcpus,
                "total_physical_cores": num_cpu_cores,
                "vcpu_pcpu_ratio": vcpu_pcpu_ratio,
                "total_vm_memory_gb": round(total_vm_memory_mb / 1024, 2),
                "total_physical_memory_gb": round(total_physical_memory_mb / 1024, 2),
                "memory_overcommit_ratio": memory_overcommit_ratio,
                "num_vms": num_vms,
            }
        except Exception as e:
            logger.warning(f"Error calculating overcommitment for {cluster_name}: {e}")
            raise ValueError(f"Failed to calculate overcommitment: {e}") from e

    async def _get_datastore_utilization(self, _params: dict[str, Any]) -> list[dict]:
        """Get utilization summary for all datastores."""
        from pyVmomi import vim

        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.Datastore], True
        )
        try:
            results = []
            for ds in container.view:
                try:
                    summary = ds.summary
                    capacity = getattr(summary, "capacity", 0) or 0
                    free_space = getattr(summary, "freeSpace", 0) or 0
                    uncommitted = getattr(summary, "uncommitted", None)

                    if capacity == 0:
                        continue

                    provisioned = capacity - free_space + (uncommitted or 0)
                    utilization_pct = round(((capacity - free_space) / capacity) * 100, 1)
                    thin_savings_gb = (
                        round(uncommitted / 1073741824, 2)
                        if uncommitted and uncommitted > 0
                        else None
                    )

                    results.append(
                        {
                            "name": ds.name,
                            "type": getattr(summary, "type", "unknown"),
                            "capacity_gb": round(capacity / 1073741824, 2),
                            "free_space_gb": round(free_space / 1073741824, 2),
                            "provisioned_gb": round(provisioned / 1073741824, 2),
                            "utilization_percent": utilization_pct,
                            "thin_provisioning_savings_gb": thin_savings_gb,
                        }
                    )
                except Exception as e:
                    logger.warning(f"Error reading datastore {getattr(ds, 'name', '?')}: {e}")
                    continue

            return results
        finally:
            container.Destroy()

    async def _get_host_load_distribution(
        self, params: dict[str, Any]
    ) -> dict:  # NOSONAR (cognitive complexity)
        """Get CPU and memory utilization per host in a cluster, with load imbalance metrics."""
        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        hosts_data: list[dict[str, Any]] = []
        cpu_utilizations: list[float] = []
        memory_utilizations: list[float] = []

        for host in cluster.host or []:
            host_name = getattr(host, "name", "unknown")
            try:
                quick_stats = getattr(getattr(host, "summary", None), "quickStats", None)
                hardware = getattr(getattr(host, "summary", None), "hardware", None)

                if not quick_stats or not hardware:
                    hosts_data.append({"host_name": host_name, "status": "stats_unavailable"})
                    continue

                cpu_usage_mhz = getattr(quick_stats, "overallCpuUsage", 0) or 0
                memory_usage_mb = getattr(quick_stats, "overallMemoryUsage", 0) or 0

                cpu_mhz = getattr(hardware, "cpuMhz", 0) or 0
                num_cores = getattr(hardware, "numCpuCores", 0) or 0
                memory_size = getattr(hardware, "memorySize", 0) or 0

                cpu_capacity_mhz = cpu_mhz * num_cores
                memory_capacity_mb = memory_size / 1048576

                cpu_util_pct = (
                    round((cpu_usage_mhz / cpu_capacity_mhz) * 100, 1)
                    if cpu_capacity_mhz > 0
                    else 0.0
                )
                memory_util_pct = (
                    round((memory_usage_mb / memory_capacity_mb) * 100, 1)
                    if memory_capacity_mb > 0
                    else 0.0
                )

                # Count VMs on this host
                num_vms = len(getattr(host, "vm", None) or [])

                cpu_utilizations.append(cpu_util_pct)
                memory_utilizations.append(memory_util_pct)

                hosts_data.append(
                    {
                        "host_name": host_name,
                        "cpu_usage_mhz": cpu_usage_mhz,
                        "cpu_capacity_mhz": cpu_capacity_mhz,
                        "cpu_utilization_percent": cpu_util_pct,
                        "memory_usage_mb": memory_usage_mb,
                        "memory_capacity_mb": round(memory_capacity_mb, 0),
                        "memory_utilization_percent": memory_util_pct,
                        "num_vms": num_vms,
                    }
                )
            except Exception as e:
                logger.warning(f"Error reading host load for {host_name}: {e}")
                hosts_data.append({"host_name": host_name, "status": "error", "error": str(e)})

        # Calculate load imbalance
        load_imbalance_cpu = (
            round(max(cpu_utilizations) - min(cpu_utilizations), 1)
            if len(cpu_utilizations) >= 2
            else 0.0
        )
        load_imbalance_memory = (
            round(max(memory_utilizations) - min(memory_utilizations), 1)
            if len(memory_utilizations) >= 2
            else 0.0
        )

        return {
            "cluster_name": cluster_name,
            "hosts": hosts_data,
            "load_imbalance_cpu": load_imbalance_cpu,
            "load_imbalance_memory": load_imbalance_memory,
        }
