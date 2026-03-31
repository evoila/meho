# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Storage Operation Handlers

Mixin class containing 12 storage operation handlers.
"""

import asyncio
from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class StorageHandlerMixin:
    """Mixin for storage operation handlers."""

    # These will be provided by VMwareConnector (base class)
    _content: Any

    # Helper methods (will be provided by VMwareConnector) - stubs for type checking
    def _find_datastore(self, name: str) -> Any | None:
        return None

    def _find_vm(self, name: str) -> Any | None:
        return None

    def _find_host(self, name: str) -> Any | None:
        return None

    # Serializer methods (will be provided by VMwareConnector) - stubs for type checking
    def _serialize_datastore_properties(self, ds: Any) -> dict[str, Any]:
        return {}

    def _serialize_vm_properties(self, vm: Any) -> dict[str, Any]:
        return {}

    async def _list_datastores(self, params: dict[str, Any]) -> list[dict]:
        """
        List all datastores with complete property data.

        OPTIMIZED: Uses PropertyCollector to fetch all datastore properties in ONE API call.
        """
        from pyVmomi import vim, vmodl

        datastore_properties = [
            "name",
            # Summary
            "summary.type",
            "summary.capacity",
            "summary.freeSpace",
            "summary.accessible",
            "summary.multipleHostAccess",
            "summary.url",
            "summary.maintenanceMode",
            # Info
            "info.url",
            "info.name",
            "info.maxFileSize",
            # Capability
            "capability.directoryHierarchySupported",
            "capability.rawDiskMappingsSupported",
            "capability.perFileThinProvisioningSupported",
            # Hosts and VMs
            "host",
            "vm",
        ]

        container_view = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.Datastore], True
        )

        try:
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities", path="view", skip=False, type=vim.view.ContainerView
            )

            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=container_view, skip=True, selectSet=[traversal_spec]
            )

            property_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=vim.Datastore, pathSet=datastore_properties, all=False
            )

            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec], propSet=[property_spec]
            )

            results = self._content.propertyCollector.RetrieveContents([filter_spec])

            return [self._format_datastore_from_properties(obj) for obj in results]

        finally:
            container_view.Destroy()

    def _format_datastore_from_properties(self, obj: Any) -> dict[str, Any]:
        """Format datastore data from PropertyCollector results."""
        props = {prop.name: prop.val for prop in obj.propSet}

        result: dict[str, Any] = {"name": props.get("name", "")}

        # Summary
        summary_data: dict[str, Any] = {}
        if "summary.type" in props:
            summary_data["type"] = props["summary.type"]
        if props.get("summary.capacity"):
            summary_data["capacity_gb"] = props["summary.capacity"] // (1024**3)
        if props.get("summary.freeSpace"):
            summary_data["free_space_gb"] = props["summary.freeSpace"] // (1024**3)
        if "summary.accessible" in props:
            summary_data["accessible"] = props["summary.accessible"]
        if "summary.multipleHostAccess" in props:
            summary_data["multiple_host_access"] = props["summary.multipleHostAccess"]
        if "summary.url" in props:
            summary_data["url"] = props["summary.url"]
        if "summary.maintenanceMode" in props:
            summary_data["maintenance_mode"] = props["summary.maintenanceMode"]
        if summary_data:
            result["summary"] = summary_data

        # Info
        info_data: dict[str, Any] = {}
        if "info.url" in props:
            info_data["url"] = props["info.url"]
        if "info.name" in props:
            info_data["name"] = props["info.name"]
        if "info.maxFileSize" in props:
            info_data["max_file_size"] = props["info.maxFileSize"]
        if info_data:
            result["info"] = info_data

        # Capability
        capability_data: dict[str, Any] = {}
        if "capability.directoryHierarchySupported" in props:
            capability_data["directory_hierarchy_supported"] = props[
                "capability.directoryHierarchySupported"
            ]
        if "capability.rawDiskMappingsSupported" in props:
            capability_data["raw_disk_mappings_supported"] = props[
                "capability.rawDiskMappingsSupported"
            ]
        if "capability.perFileThinProvisioningSupported" in props:
            capability_data["per_file_thin_provisioning_supported"] = props[
                "capability.perFileThinProvisioningSupported"
            ]
        if capability_data:
            result["capability"] = capability_data

        # Hosts
        if props.get("host"):
            result["host_count"] = len(props["host"])
            result["hosts"] = [
                h.key.name if hasattr(h.key, "name") else None for h in props["host"]
            ][:50]

        # VMs
        if props.get("vm"):
            result["vm_count"] = len(props["vm"])
            result["vms"] = [vm.name for vm in props["vm"] if hasattr(vm, "name")][:50]

        return result

    async def _get_datastore(self, params: dict[str, Any]) -> dict:
        """Get datastore details with complete property data."""
        ds_name = params.get("datastore_name") or params.get("name")
        if not ds_name:
            raise ValueError("datastore_name is required")

        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")

        return self._serialize_datastore_properties(ds)

    async def _browse_datastore(self, params: dict[str, Any]) -> list[dict]:
        """Browse datastore files."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")

        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")

        path = params.get("path", "")
        browser = ds.browser

        # Build search path
        search_path = f"[{ds_name}]"
        if path:
            search_path = f"[{ds_name}] {path.lstrip('/')}"

        from pyVmomi import vim

        search_spec = vim.host.DatastoreBrowser.SearchSpec()
        search_spec.matchPattern = ["*"]

        task = browser.SearchDatastore_Task(datastorePath=search_path, searchSpec=search_spec)

        # Poll task completion with timeout
        timeout_seconds = 120
        elapsed = 0
        while elapsed < timeout_seconds:
            state = task.info.state
            if state == "success":
                break
            if state == "error":
                error_msg = getattr(task.info.error, "localizedMessage", str(task.info.error))
                raise ValueError(f"Datastore browse failed: {error_msg}")
            await asyncio.sleep(1)
            elapsed += 1

        if elapsed >= timeout_seconds:
            raise TimeoutError(
                f"Datastore browse timed out after {timeout_seconds}s for {search_path}"
            )

        # Extract results from completed task
        results = task.info.result
        files = []
        if results and hasattr(results, "file"):
            for file_info in results.file or []:
                files.append(
                    {
                        "path": getattr(file_info, "path", ""),
                        "file_size": getattr(file_info, "fileSize", None),
                        "modification_time": str(getattr(file_info, "modification", ""))
                        if getattr(file_info, "modification", None)
                        else None,
                    }
                )
        return files

    async def _refresh_datastore(self, params: dict[str, Any]) -> dict:
        """Refresh datastore."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")

        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")

        ds.RefreshDatastore()
        return {"message": f"Datastore {ds_name} refreshed"}

    async def _rename_datastore(self, params: dict[str, Any]) -> dict:
        """Rename datastore."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")

        new_name = params.get("new_name")
        if not new_name:
            raise ValueError("new_name is required")

        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")

        ds.RenameDatastore(newName=new_name)
        return {"message": f"Datastore renamed from {ds_name} to {new_name}"}

    async def _destroy_datastore(self, params: dict[str, Any]) -> dict:
        """Destroy datastore."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")

        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")

        ds.DestroyDatastore()
        return {"message": f"Datastore {ds_name} destroyed"}

    async def _refresh_datastore_storage_info(self, params: dict[str, Any]) -> dict:
        """Refresh datastore storage info."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")

        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")

        ds.RefreshDatastoreStorageInfo()
        return {"message": f"Storage info refreshed for {ds_name}"}

    async def _create_nfs_datastore(self, params: dict[str, Any]) -> dict:
        """Create NFS datastore."""
        from pyVmomi import vim

        host_name = params.get("host_name")
        ds_name = params.get("datastore_name")
        remote_host = params.get("remote_host")
        remote_path = params.get("remote_path")

        if not all([host_name, ds_name, remote_host, remote_path]):
            raise ValueError("host_name, datastore_name, remote_host, and remote_path are required")

        host = self._find_host(str(host_name))
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        spec = vim.host.NasVolume.Specification()
        spec.remoteHost = str(remote_host)
        spec.remotePath = str(remote_path)
        spec.localPath = str(ds_name)
        spec.accessMode = str(params.get("access_mode", "readWrite"))

        ds = host.configManager.datastoreSystem.CreateNasDatastore(spec=spec)
        return {"message": f"NFS datastore {ds_name} created", "name": ds.name}

    async def _remove_datastore(self, params: dict[str, Any]) -> dict:
        """Remove datastore from host."""
        host_name = params.get("host_name")
        ds_name = params.get("datastore_name")

        if not host_name or not ds_name:
            raise ValueError("host_name and datastore_name are required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")

        host.configManager.datastoreSystem.RemoveDatastore(datastore=ds)
        return {"message": f"Datastore {ds_name} removed from {host_name}"}

    async def _get_datastore_performance(self, params: dict[str, Any]) -> dict:
        """
        Get datastore capacity and status information.

        NOTE: IOPS/latency metrics are NOT available directly from datastore objects.
        Datastores are not valid performance providers in vSphere's PerformanceManager API.

        To get datastore IOPS/latency, use get_detailed_host_performance with metrics=["datastore"]
        on a host that accesses the datastore.
        """
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")

        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")

        return {
            "datastore_name": ds_name,
            "type": ds.summary.type,  # VMFS, NFS, vsan, etc.
            "capacity_gb": ds.summary.capacity // (1024**3),
            "free_space_gb": ds.summary.freeSpace // (1024**3),
            "uncommitted_gb": (ds.summary.uncommitted // (1024**3))
            if ds.summary.uncommitted
            else 0,
            "accessible": ds.summary.accessible,
            "maintenance_mode": ds.summary.maintenanceMode,
        }

    async def _get_storage_pods(self, params: dict[str, Any]) -> list[dict]:
        """List storage pods (datastore clusters)."""
        from pyVmomi import vim

        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.StoragePod], True
        )
        try:
            return [
                {
                    "name": pod.name,
                    "capacity_gb": pod.summary.capacity // (1024**3) if pod.summary else 0,
                    "free_space_gb": pod.summary.freeSpace // (1024**3) if pod.summary else 0,
                    "num_datastores": len(pod.childEntity) if pod.childEntity else 0,
                }
                for pod in container.view
            ]
        finally:
            container.Destroy()

    async def _get_storage_pod(self, params: dict[str, Any]) -> dict:
        """Get storage pod details."""
        from pyVmomi import vim

        pod_name = params.get("pod_name")
        if not pod_name:
            raise ValueError("pod_name is required")

        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.StoragePod], True
        )
        try:
            for pod in container.view:
                if pod.name == pod_name:
                    return {
                        "name": pod.name,
                        "capacity_gb": pod.summary.capacity // (1024**3) if pod.summary else 0,
                        "free_space_gb": pod.summary.freeSpace // (1024**3) if pod.summary else 0,
                        "datastores": [ds.name for ds in pod.childEntity]
                        if pod.childEntity
                        else [],
                    }
            raise ValueError(f"Storage pod not found: {pod_name}")
        finally:
            container.Destroy()
