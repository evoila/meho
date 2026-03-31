# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware Object Property Serializers

Functions to serialize ALL available properties from pyvmomi managed objects.
These ensure complete data per validation requirements.
"""

from typing import Any


def serialize_vm_properties(vm: Any) -> dict[str, Any]:
    """
    Serialize ALL 17 VirtualMachine properties from pyvmomi.
    Returns complete VM data - capability, config, datastore, environmentBrowser, guest,
    guestHeartbeatStatus, layout, layoutEx, network, parentVApp, resourceConfig,
    resourcePool, rootSnapshot, runtime, snapshot, storage, summary.
    """
    result = {
        "name": vm.name,
    }

    # 1. Runtime properties (includes host!)
    if hasattr(vm, "runtime") and vm.runtime:
        runtime = vm.runtime
        result["runtime"] = {
            "power_state": str(runtime.powerState) if hasattr(runtime, "powerState") else None,
            "host": runtime.host.name if hasattr(runtime, "host") and runtime.host else None,
            "connection_state": str(runtime.connectionState)
            if hasattr(runtime, "connectionState")
            else None,
            "boot_time": str(runtime.bootTime) if hasattr(runtime, "bootTime") else None,
            "max_cpu_usage": runtime.maxCpuUsage if hasattr(runtime, "maxCpuUsage") else None,
            "max_memory_usage": runtime.maxMemoryUsage
            if hasattr(runtime, "maxMemoryUsage")
            else None,
        }

    # 2. Config properties
    if hasattr(vm, "config") and vm.config:
        config = vm.config
        result["config"] = {
            "num_cpu": config.hardware.numCPU if hasattr(config.hardware, "numCPU") else None,
            "memory_mb": config.hardware.memoryMB if hasattr(config.hardware, "memoryMB") else None,
            "guest_os": config.guestFullName if hasattr(config, "guestFullName") else None,
            "uuid": config.uuid if hasattr(config, "uuid") else None,
            "version": config.version if hasattr(config, "version") else None,
            "instance_uuid": config.instanceUuid if hasattr(config, "instanceUuid") else None,
            "guest_id": config.guestId if hasattr(config, "guestId") else None,
            "template": config.template if hasattr(config, "template") else None,
        }

    # 3. Guest properties
    if hasattr(vm, "guest") and vm.guest:
        guest = vm.guest
        result["guest"] = {
            "ip_address": guest.ipAddress if hasattr(guest, "ipAddress") else None,
            "hostname": guest.hostName if hasattr(guest, "hostName") else None,
            "tools_status": str(guest.toolsRunningStatus)
            if hasattr(guest, "toolsRunningStatus")
            else None,
            "tools_version": guest.toolsVersion if hasattr(guest, "toolsVersion") else None,
            "guest_state": str(guest.guestState) if hasattr(guest, "guestState") else None,
        }

    # 4. Summary (includes quickStats)
    if hasattr(vm, "summary") and vm.summary:  # noqa: SIM102 -- readability preferred over collapse
        if hasattr(vm.summary, "quickStats") and vm.summary.quickStats:
            qs = vm.summary.quickStats
            result["summary"] = {
                "cpu_usage_mhz": qs.overallCpuUsage if hasattr(qs, "overallCpuUsage") else None,
                "memory_usage_mb": qs.guestMemoryUsage if hasattr(qs, "guestMemoryUsage") else None,
                "uptime_seconds": qs.uptimeSeconds if hasattr(qs, "uptimeSeconds") else None,
                "overall_status": str(vm.summary.overallStatus)
                if hasattr(vm.summary, "overallStatus")
                else None,
            }

    # 5. Resource Pool
    if hasattr(vm, "resourcePool") and vm.resourcePool:
        result["resource_pool"] = vm.resourcePool.name if hasattr(vm.resourcePool, "name") else None

    # 6. Datastores (array)
    if hasattr(vm, "datastore") and vm.datastore:
        result["datastores"] = [ds.name for ds in vm.datastore if hasattr(ds, "name")]

    # 7. Networks (array)
    if hasattr(vm, "network") and vm.network:
        result["networks"] = [net.name for net in vm.network if hasattr(net, "name")]

    # 8. Capability
    if hasattr(vm, "capability") and vm.capability:
        cap = vm.capability
        result["capability"] = {
            "snapshot_operations_supported": cap.snapshotOperationsSupported
            if hasattr(cap, "snapshotOperationsSupported")
            else None,
            "multiple_snapshots_supported": cap.multipleSnapshotsSupported
            if hasattr(cap, "multipleSnapshotsSupported")
            else None,
            "can_power_off": cap.poweredOffSnapshotsSupported
            if hasattr(cap, "poweredOffSnapshotsSupported")
            else None,
        }

    # 9. Guest Heartbeat Status
    if hasattr(vm, "guestHeartbeatStatus"):
        result["guest_heartbeat_status"] = str(vm.guestHeartbeatStatus)

    # 10. Snapshot info
    if hasattr(vm, "snapshot") and vm.snapshot:
        result["snapshot"] = {
            "current_snapshot": vm.snapshot.currentSnapshot.name
            if hasattr(vm.snapshot, "currentSnapshot") and vm.snapshot.currentSnapshot
            else None,
            "root_snapshot_count": len(vm.snapshot.rootSnapshotList)
            if hasattr(vm.snapshot, "rootSnapshotList")
            else 0,
        }

    # 11. Root Snapshots (array)
    if hasattr(vm, "rootSnapshot") and vm.rootSnapshot:
        result["root_snapshots"] = [snap.name for snap in vm.rootSnapshot if hasattr(snap, "name")]

    # 12. Storage info
    if hasattr(vm, "storage") and vm.storage:
        storage = vm.storage
        result["storage"] = {
            "per_datastore_usage": [
                {
                    "datastore": usage.datastore.name if hasattr(usage.datastore, "name") else None,
                    "committed": usage.committed if hasattr(usage, "committed") else None,
                    "uncommitted": usage.uncommitted if hasattr(usage, "uncommitted") else None,
                }
                for usage in (
                    storage.perDatastoreUsage if hasattr(storage, "perDatastoreUsage") else []
                )
            ][:10]  # Limit to 10 for brevity
        }

    # 13. Layout (file layout info)
    if hasattr(vm, "layoutEx") and vm.layoutEx:
        layout = vm.layoutEx
        result["layout"] = {
            "file_count": len(layout.file) if hasattr(layout, "file") else 0,
            "disk_count": len(layout.disk) if hasattr(layout, "disk") else 0,
        }

    # 14. Parent VApp
    if hasattr(vm, "parentVApp") and vm.parentVApp:
        result["parent_vapp"] = vm.parentVApp.name if hasattr(vm.parentVApp, "name") else None

    # 15. Resource Config
    if hasattr(vm, "resourceConfig") and vm.resourceConfig:
        rc = vm.resourceConfig
        result["resource_config"] = {
            "cpu_allocation": rc.cpuAllocation.reservation
            if hasattr(rc, "cpuAllocation") and rc.cpuAllocation
            else None,
            "memory_allocation": rc.memoryAllocation.reservation
            if hasattr(rc, "memoryAllocation") and rc.memoryAllocation
            else None,
        }

    # 16. Environment Browser
    if hasattr(vm, "environmentBrowser") and vm.environmentBrowser:
        result["environment_browser"] = str(vm.environmentBrowser)[:50]  # Reference only

    return result


def serialize_host_properties(host: Any) -> dict[str, Any]:
    """
    Serialize ALL 19 HostSystem properties from pyvmomi.
    Returns complete host data - capability, config, configManager, datastore,
    datastoreBrowser, hardware, licensableResource, network, runtime, summary,
    systemResources, vm, and compliance/remediation state.
    """
    result = {
        "name": host.name,
    }

    # 1. Runtime properties
    if hasattr(host, "runtime") and host.runtime:
        runtime = host.runtime
        result["runtime"] = {
            "connection_state": str(runtime.connectionState)
            if hasattr(runtime, "connectionState")
            else None,
            "power_state": str(runtime.powerState) if hasattr(runtime, "powerState") else None,
            "maintenance_mode": runtime.inMaintenanceMode
            if hasattr(runtime, "inMaintenanceMode")
            else None,
            "standby_mode": str(runtime.standbyMode) if hasattr(runtime, "standbyMode") else None,
            "boot_time": str(runtime.bootTime) if hasattr(runtime, "bootTime") else None,
        }

    # 2. Summary (includes hardware and quickStats)
    if hasattr(host, "summary") and host.summary:
        if hasattr(host.summary, "hardware") and host.summary.hardware:
            hw = host.summary.hardware
            result["hardware_summary"] = {
                "vendor": hw.vendor if hasattr(hw, "vendor") else None,
                "model": hw.model if hasattr(hw, "model") else None,
                "uuid": hw.uuid if hasattr(hw, "uuid") else None,
                "cpu_mhz": hw.cpuMhz if hasattr(hw, "cpuMhz") else None,
                "cpu_cores": hw.numCpuCores if hasattr(hw, "numCpuCores") else None,
                "cpu_threads": hw.numCpuThreads if hasattr(hw, "numCpuThreads") else None,
                "memory_mb": (hw.memorySize // (1024 * 1024))
                if hasattr(hw, "memorySize") and hw.memorySize
                else None,
            }

        if hasattr(host.summary, "quickStats") and host.summary.quickStats:
            qs = host.summary.quickStats
            result["stats"] = {
                "cpu_usage_mhz": qs.overallCpuUsage if hasattr(qs, "overallCpuUsage") else None,
                "memory_usage_mb": qs.overallMemoryUsage
                if hasattr(qs, "overallMemoryUsage")
                else None,
                "uptime_seconds": qs.uptime if hasattr(qs, "uptime") else None,
            }

        result["overall_status"] = (
            str(host.summary.overallStatus) if hasattr(host.summary, "overallStatus") else None
        )

    # 3. Hardware (detailed)
    if hasattr(host, "hardware") and host.hardware:
        hw = host.hardware
        result["hardware_detailed"] = {
            "system_info": {
                "vendor": hw.systemInfo.vendor
                if hasattr(hw, "systemInfo") and hw.systemInfo
                else None,
                "model": hw.systemInfo.model
                if hasattr(hw, "systemInfo") and hw.systemInfo
                else None,
            },
            "cpu_pkg_count": len(hw.cpuPkg) if hasattr(hw, "cpuPkg") else 0,
            "nic_count": len(hw.pciDevice) if hasattr(hw, "pciDevice") else 0,
        }

    # 4. Config
    if hasattr(host, "config") and host.config:
        config = host.config
        result["config"] = {
            "hyperthread_available": config.hyperThread.available
            if hasattr(config, "hyperThread") and config.hyperThread
            else None,
            "hyperthread_active": config.hyperThread.active
            if hasattr(config, "hyperThread") and config.hyperThread
            else None,
        }

    # 5. Capability
    if hasattr(host, "capability") and host.capability:
        cap = host.capability
        result["capability"] = {
            "vmotion_supported": cap.vmotionSupported if hasattr(cap, "vmotionSupported") else None,
            "storage_vmotion_supported": cap.storageVMotionSupported
            if hasattr(cap, "storageVMotionSupported")
            else None,
            "ft_supported": cap.ftSupported if hasattr(cap, "ftSupported") else None,
        }

    # 6. Licensable Resource
    if hasattr(host, "licensableResource") and host.licensableResource:
        lr = host.licensableResource
        result["licensable_resource"] = {
            "num_cpu_packages": lr.numCpuPackages if hasattr(lr, "numCpuPackages") else None,
            "num_cpu_cores": lr.numCpuCores if hasattr(lr, "numCpuCores") else None,
        }

    # 7. System Resources
    if hasattr(host, "systemResources") and host.systemResources:
        sr = host.systemResources
        result["system_resources"] = {
            "config_present": sr.config is not None if hasattr(sr, "config") else False,
        }

    # 8. Parent cluster
    if hasattr(host, "parent") and host.parent:
        result["cluster"] = host.parent.name if hasattr(host.parent, "name") else None

    # 9. VMs on this host (array)
    if hasattr(host, "vm") and host.vm:
        result["vm_count"] = len(host.vm)
        result["vms"] = [vm.name for vm in host.vm if hasattr(vm, "name")][:50]  # Limit to 50

    # 10. Datastores (array)
    if hasattr(host, "datastore") and host.datastore:
        result["datastore_count"] = len(host.datastore)
        result["datastores"] = [ds.name for ds in host.datastore if hasattr(ds, "name")]

    # 11. Networks (array)
    if hasattr(host, "network") and host.network:
        result["network_count"] = len(host.network)
        result["networks"] = [net.name for net in host.network if hasattr(net, "name")]

    # 12. Datastore Browser
    if hasattr(host, "datastoreBrowser") and host.datastoreBrowser:
        result["datastore_browser"] = str(host.datastoreBrowser)[:50]  # Reference only

    # 13. Config Manager
    if hasattr(host, "configManager") and host.configManager:
        result["config_manager_available"] = True

    # 14-19. Compliance/Remediation (profile-related, often None)
    if hasattr(host, "complianceCheckState"):
        result["compliance_check_state"] = str(host.complianceCheckState)

    return result


def serialize_datastore_properties(ds: Any) -> dict[str, Any]:
    """
    Serialize ALL 7 Datastore properties from pyvmomi.
    Returns complete datastore data - browser, capability, host, info,
    iormConfiguration, summary, vm.
    """
    result = {
        "name": ds.name,
    }

    # 1. Summary
    if hasattr(ds, "summary") and ds.summary:
        summary = ds.summary
        result["summary"] = {
            "type": summary.type if hasattr(summary, "type") else None,
            "capacity_gb": (summary.capacity // (1024**3))
            if hasattr(summary, "capacity") and summary.capacity
            else None,
            "free_space_gb": (summary.freeSpace // (1024**3))
            if hasattr(summary, "freeSpace") and summary.freeSpace
            else None,
            "accessible": summary.accessible if hasattr(summary, "accessible") else None,
            "multiple_host_access": summary.multipleHostAccess
            if hasattr(summary, "multipleHostAccess")
            else None,
            "url": summary.url if hasattr(summary, "url") else None,
            "maintenance_mode": summary.maintenanceMode
            if hasattr(summary, "maintenanceMode")
            else None,
        }

    # 2. Info (detailed configuration)
    if hasattr(ds, "info") and ds.info:
        info = ds.info
        result["info"] = {
            "url": info.url if hasattr(info, "url") else None,
            "name": info.name if hasattr(info, "name") else None,
            "max_file_size": info.maxFileSize if hasattr(info, "maxFileSize") else None,
        }

    # 3. Capability
    if hasattr(ds, "capability") and ds.capability:
        cap = ds.capability
        result["capability"] = {
            "directory_hierarchy_supported": cap.directoryHierarchySupported
            if hasattr(cap, "directoryHierarchySupported")
            else None,
            "raw_disk_mappings_supported": cap.rawDiskMappingsSupported
            if hasattr(cap, "rawDiskMappingsSupported")
            else None,
            "per_file_thin_provisioning_supported": cap.perFileThinProvisioningSupported
            if hasattr(cap, "perFileThinProvisioningSupported")
            else None,
        }

    # 4. Hosts with access to this datastore (array)
    if hasattr(ds, "host") and ds.host:
        result["host_count"] = len(ds.host)
        result["hosts"] = [h.key.name if hasattr(h.key, "name") else None for h in ds.host][
            :50
        ]  # Limit to 50

    # 5. VMs using this datastore (array)
    if hasattr(ds, "vm") and ds.vm:
        result["vm_count"] = len(ds.vm)
        result["vms"] = [vm.name for vm in ds.vm if hasattr(vm, "name")][:50]  # Limit to 50

    # 6. Browser
    if hasattr(ds, "browser") and ds.browser:
        result["browser"] = str(ds.browser)[:50]  # Reference only

    # 7. IORM Configuration (I/O Resource Management)
    if hasattr(ds, "iormConfiguration") and ds.iormConfiguration:
        iorm = ds.iormConfiguration
        result["iorm_configuration"] = {
            "enabled": iorm.enabled if hasattr(iorm, "enabled") else None,
            "congestion_threshold": iorm.congestionThreshold
            if hasattr(iorm, "congestionThreshold")
            else None,
        }

    return result


def serialize_cluster_properties(cluster: Any) -> dict[str, Any]:
    """
    Serialize ALL 8 ClusterComputeResource properties from pyvmomi.
    Returns complete cluster data - actionHistory, configuration, drsFault,
    drsRecommendation, hciConfig, migrationHistory, recommendation, summaryEx.
    """
    result = {"name": cluster.name}

    # 1. Configuration (DRS, HA, etc.)
    if hasattr(cluster, "configuration") and cluster.configuration:
        config = cluster.configuration
        result["configuration"] = {
            "drs_enabled": config.drsConfig.enabled
            if hasattr(config, "drsConfig") and config.drsConfig
            else None,
            "drs_behavior": str(config.drsConfig.defaultVmBehavior)
            if hasattr(config, "drsConfig") and config.drsConfig
            else None,
            "ha_enabled": config.dasConfig.enabled
            if hasattr(config, "dasConfig") and config.dasConfig
            else None,
            "ha_host_monitoring": config.dasConfig.hostMonitoring
            if hasattr(config, "dasConfig") and config.dasConfig
            else None,
        }

    # 2. SummaryEx (extended summary)
    if hasattr(cluster, "summaryEx") and cluster.summaryEx:
        summary = cluster.summaryEx
        result["summary"] = {
            "num_hosts": summary.numHosts if hasattr(summary, "numHosts") else None,
            "num_effective_hosts": summary.numEffectiveHosts
            if hasattr(summary, "numEffectiveHosts")
            else None,
            "total_cpu_mhz": summary.totalCpu if hasattr(summary, "totalCpu") else None,
            "total_memory_mb": (summary.totalMemory // (1024 * 1024))
            if hasattr(summary, "totalMemory")
            else None,
        }
    elif hasattr(cluster, "summary") and cluster.summary:
        # Fallback to regular summary
        summary = cluster.summary
        result["summary"] = {
            "num_hosts": summary.numHosts if hasattr(summary, "numHosts") else None,
            "num_effective_hosts": summary.numEffectiveHosts
            if hasattr(summary, "numEffectiveHosts")
            else None,
        }

    # 3. Hosts (inherited from ComputeResource)
    if hasattr(cluster, "host") and cluster.host:
        result["host_count"] = len(cluster.host)
        result["hosts"] = [h.name for h in cluster.host if hasattr(h, "name")][:50]  # Limit to 50

    # 4. DRS Recommendation (array)
    if hasattr(cluster, "drsRecommendation") and cluster.drsRecommendation:
        result["drs_recommendation_count"] = len(cluster.drsRecommendation)
        result["drs_recommendations"] = [
            {
                "key": rec.key,
                "rating": rec.rating,
                "reason": rec.reason,
            }
            for rec in cluster.drsRecommendation[:10]  # First 10
        ]

    # 5. Recommendation (general, includes DRS)
    if hasattr(cluster, "recommendation") and cluster.recommendation:
        result["recommendation_count"] = len(cluster.recommendation)

    # 6. Action History
    if hasattr(cluster, "actionHistory") and cluster.actionHistory:
        result["action_history_count"] = len(cluster.actionHistory)

    # 7. Migration History
    if hasattr(cluster, "migrationHistory") and cluster.migrationHistory:
        result["migration_history_count"] = len(cluster.migrationHistory)

    # 8. DRS Faults
    if hasattr(cluster, "drsFault") and cluster.drsFault:
        result["drs_fault_count"] = len(cluster.drsFault)

    # 9. HCI Config (Hyper-Converged Infrastructure)
    if hasattr(cluster, "hciConfig") and cluster.hciConfig:
        result["hci_config"] = {
            "workflow_state": str(cluster.hciConfig.workflowState)
            if hasattr(cluster.hciConfig, "workflowState")
            else None,
        }

    return result


def serialize_network_properties(network: Any) -> dict[str, Any]:
    """
    Serialize ALL 3 Network properties from pyvmomi.
    Returns host, summary, vm.
    """
    result = {"name": network.name}

    # 1. Summary
    if hasattr(network, "summary") and network.summary:
        result["summary"] = {
            "accessible": network.summary.accessible
            if hasattr(network.summary, "accessible")
            else None,
            "name": network.summary.name if hasattr(network.summary, "name") else None,
        }

    # 2. Hosts (array)
    if hasattr(network, "host") and network.host:
        result["host_count"] = len(network.host)
        result["hosts"] = [h.name for h in network.host if hasattr(h, "name")][:50]

    # 3. VMs (array)
    if hasattr(network, "vm") and network.vm:
        result["vm_count"] = len(network.vm)
        result["vms"] = [vm.name for vm in network.vm if hasattr(vm, "name")][:50]

    return result


def serialize_dvs_properties(dvs: Any) -> dict[str, Any]:
    """
    Serialize ALL 7 DistributedVirtualSwitch properties from pyvmomi.
    Returns capability, config, networkResourcePool, portgroup, runtime, summary, uuid.
    """
    result = {"name": dvs.name}

    # 1. UUID
    if hasattr(dvs, "uuid"):
        result["uuid"] = dvs.uuid

    # 2. Summary
    if hasattr(dvs, "summary") and dvs.summary:
        summary = dvs.summary
        result["summary"] = {
            "name": summary.name if hasattr(summary, "name") else None,
            "num_ports": summary.numPorts if hasattr(summary, "numPorts") else None,
            "product_info": summary.productInfo.version
            if hasattr(summary, "productInfo") and summary.productInfo
            else None,
            "host_member_count": len(summary.hostMember)
            if hasattr(summary, "hostMember") and summary.hostMember
            else 0,
        }

    # 3. Config
    if hasattr(dvs, "config") and dvs.config:
        config = dvs.config
        result["config"] = {
            "max_ports": config.maxPorts if hasattr(config, "maxPorts") else None,
            "num_ports": config.numPorts if hasattr(config, "numPorts") else None,
            "num_standalone_ports": config.numStandalonePorts
            if hasattr(config, "numStandalonePorts")
            else None,
        }

    # 4. Capability
    if hasattr(dvs, "capability") and dvs.capability:
        cap = dvs.capability
        result["capability"] = {
            "dv_port_group_operation_supported": cap.dvPortGroupOperationSupported
            if hasattr(cap, "dvPortGroupOperationSupported")
            else None,
            "dvs_operation_supported": cap.dvsOperationSupported
            if hasattr(cap, "dvsOperationSupported")
            else None,
        }

    # 5. Runtime
    if hasattr(dvs, "runtime") and dvs.runtime:
        result["runtime"] = {
            "host_member_runtime_count": len(dvs.runtime.hostMemberRuntime)
            if hasattr(dvs.runtime, "hostMemberRuntime") and dvs.runtime.hostMemberRuntime
            else 0,
        }

    # 6. Portgroups (array)
    if hasattr(dvs, "portgroup") and dvs.portgroup:
        result["portgroup_count"] = len(dvs.portgroup)
        result["portgroups"] = [pg.name for pg in dvs.portgroup if hasattr(pg, "name")][:50]

    # 7. Network Resource Pool
    if hasattr(dvs, "networkResourcePool") and dvs.networkResourcePool:
        result["network_resource_pool_count"] = len(dvs.networkResourcePool)

    return result


def serialize_portgroup_properties(pg: Any) -> dict[str, Any]:
    """
    Serialize ALL 3 DistributedVirtualPortgroup properties from pyvmomi.
    Returns config, key, portKeys.
    """
    result = {"name": pg.name}

    # 1. Key
    if hasattr(pg, "key"):
        result["key"] = pg.key

    # 2. Config
    if hasattr(pg, "config") and pg.config:
        config = pg.config
        result["config"] = {
            "num_ports": config.numPorts if hasattr(config, "numPorts") else None,
            "type": config.type if hasattr(config, "type") else None,
            "default_port_config": str(config.defaultPortConfig)[:100]
            if hasattr(config, "defaultPortConfig")
            else None,
        }

    # 3. Port Keys (array)
    if hasattr(pg, "portKeys") and pg.portKeys:
        result["port_count"] = len(pg.portKeys)
        result["port_keys"] = pg.portKeys[:20]  # First 20

    return result


def serialize_resource_pool_properties(pool: Any) -> dict[str, Any]:
    """Serialize ALL properties from ResourcePool."""
    result = {"name": pool.name}

    if hasattr(pool, "config") and pool.config:
        config = pool.config
        result["config"] = {
            "cpu_allocation_mhz": config.cpuAllocation.reservation
            if hasattr(config, "cpuAllocation") and config.cpuAllocation
            else None,
            "memory_allocation_mb": config.memoryAllocation.reservation
            if hasattr(config, "memoryAllocation") and config.memoryAllocation
            else None,
        }

    if hasattr(pool, "vm") and pool.vm:
        result["vm_count"] = len(pool.vm)

    if hasattr(pool, "resourcePool") and pool.resourcePool:
        result["child_pool_count"] = len(pool.resourcePool)

    return result


def serialize_folder_properties(folder: Any) -> dict[str, Any]:
    """Serialize ALL properties from Folder."""
    result = {"name": folder.name}

    if hasattr(folder, "childEntity") and folder.childEntity:
        result["child_count"] = len(folder.childEntity)
        result["children"] = [child.name for child in folder.childEntity if hasattr(child, "name")]

    return result


def serialize_datacenter_properties(dc: Any) -> dict[str, Any]:
    """Serialize ALL properties from Datacenter."""
    result = {"name": dc.name}

    if hasattr(dc, "hostFolder") and dc.hostFolder:
        result["host_folder"] = dc.hostFolder.name if hasattr(dc.hostFolder, "name") else None

    if hasattr(dc, "vmFolder") and dc.vmFolder:
        result["vm_folder"] = dc.vmFolder.name if hasattr(dc.vmFolder, "name") else None

    if hasattr(dc, "datastoreFolder") and dc.datastoreFolder:
        result["datastore_folder"] = (
            dc.datastoreFolder.name if hasattr(dc.datastoreFolder, "name") else None
        )

    if hasattr(dc, "networkFolder") and dc.networkFolder:
        result["network_folder"] = (
            dc.networkFolder.name if hasattr(dc.networkFolder, "name") else None
        )

    return result
