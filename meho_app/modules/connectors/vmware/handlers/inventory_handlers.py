# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Inventory Operation Handlers

Mixin class containing 16 inventory operation handlers.
"""

from typing import Any


class InventoryHandlerMixin:
    """Mixin for inventory operation handlers."""

    # These will be provided by VMwareConnector (base class)
    _content: Any

    # Helper methods (will be provided by VMwareConnector) - stubs for type checking
    def _find_folder(self, name: str) -> Any | None:
        return None

    def _find_resource_pool(self, name: str) -> Any | None:
        return None

    def _find_vm(self, name: str) -> Any | None:
        return None

    def _find_host(self, name: str) -> Any | None:
        return None

    def _find_datastore(self, name: str) -> Any | None:
        return None

    def _find_cluster(self, name: str) -> Any | None:
        return None

    # Serializer methods (will be provided by VMwareConnector) - stubs for type checking
    def _serialize_datacenter_properties(self, dc: Any) -> dict[str, Any]:
        return {}

    def _serialize_resource_pool_properties(self, pool: Any) -> dict[str, Any]:
        return {}

    def _serialize_folder_properties(self, folder: Any) -> dict[str, Any]:
        return {}

    async def _list_datacenters(self, params: dict[str, Any]) -> list[dict]:
        """
        List all datacenters with complete property data.

        OPTIMIZED: Uses PropertyCollector to fetch all datacenter properties in ONE API call.
        """
        from pyVmomi import vim, vmodl

        dc_properties = [
            "name",
            "hostFolder",
            "vmFolder",
            "datastoreFolder",
            "networkFolder",
        ]

        container_view = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.Datacenter], True
        )

        try:
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities", path="view", skip=False, type=vim.view.ContainerView
            )

            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=container_view, skip=True, selectSet=[traversal_spec]
            )

            property_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=vim.Datacenter, pathSet=dc_properties, all=False
            )

            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec], propSet=[property_spec]
            )

            results = self._content.propertyCollector.RetrieveContents([filter_spec])

            return [self._format_datacenter_from_properties(obj) for obj in results]

        finally:
            container_view.Destroy()

    def _format_datacenter_from_properties(self, obj: Any) -> dict[str, Any]:
        """Format datacenter data from PropertyCollector results."""
        props = {prop.name: prop.val for prop in obj.propSet}

        result: dict[str, Any] = {"name": props.get("name", "")}

        if props.get("hostFolder"):
            result["host_folder"] = (
                props["hostFolder"].name if hasattr(props["hostFolder"], "name") else None
            )

        if props.get("vmFolder"):
            result["vm_folder"] = (
                props["vmFolder"].name if hasattr(props["vmFolder"], "name") else None
            )

        if props.get("datastoreFolder"):
            result["datastore_folder"] = (
                props["datastoreFolder"].name if hasattr(props["datastoreFolder"], "name") else None
            )

        if props.get("networkFolder"):
            result["network_folder"] = (
                props["networkFolder"].name if hasattr(props["networkFolder"], "name") else None
            )

        return result

    async def _list_resource_pools(self, params: dict[str, Any]) -> list[dict]:
        """
        List all resource pools with complete property data.

        OPTIMIZED: Uses PropertyCollector to fetch all resource pool properties in ONE API call.
        """
        from pyVmomi import vim, vmodl

        pool_properties = [
            "name",
            "config.cpuAllocation",
            "config.memoryAllocation",
            "vm",
            "resourcePool",
        ]

        container_view = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.ResourcePool], True
        )

        try:
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities", path="view", skip=False, type=vim.view.ContainerView
            )

            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=container_view, skip=True, selectSet=[traversal_spec]
            )

            property_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=vim.ResourcePool, pathSet=pool_properties, all=False
            )

            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec], propSet=[property_spec]
            )

            results = self._content.propertyCollector.RetrieveContents([filter_spec])

            return [self._format_resource_pool_from_properties(obj) for obj in results]

        finally:
            container_view.Destroy()

    def _format_resource_pool_from_properties(self, obj: Any) -> dict[str, Any]:
        """Format resource pool data from PropertyCollector results."""
        props = {prop.name: prop.val for prop in obj.propSet}

        result: dict[str, Any] = {"name": props.get("name", "")}

        # Config
        config_data: dict[str, Any] = {}
        if props.get("config.cpuAllocation"):
            config_data["cpu_allocation_mhz"] = props["config.cpuAllocation"].reservation
        if props.get("config.memoryAllocation"):
            config_data["memory_allocation_mb"] = props["config.memoryAllocation"].reservation
        if config_data:
            result["config"] = config_data

        # VMs
        if props.get("vm"):
            result["vm_count"] = len(props["vm"])

        # Child pools
        if props.get("resourcePool"):
            result["child_pool_count"] = len(props["resourcePool"])

        return result

    async def _list_folders(self, params: dict[str, Any]) -> list[dict]:
        """
        List all VM folders with complete property data.

        OPTIMIZED: Uses PropertyCollector to fetch all folder properties in ONE API call.
        """
        from pyVmomi import vim, vmodl

        folder_properties = [
            "name",
            "childEntity",
        ]

        container_view = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.Folder], True
        )

        try:
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities", path="view", skip=False, type=vim.view.ContainerView
            )

            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=container_view, skip=True, selectSet=[traversal_spec]
            )

            property_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=vim.Folder, pathSet=folder_properties, all=False
            )

            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec], propSet=[property_spec]
            )

            results = self._content.propertyCollector.RetrieveContents([filter_spec])

            return [self._format_folder_from_properties(obj) for obj in results]

        finally:
            container_view.Destroy()

    def _format_folder_from_properties(self, obj: Any) -> dict[str, Any]:
        """Format folder data from PropertyCollector results."""
        props = {prop.name: prop.val for prop in obj.propSet}

        result: dict[str, Any] = {"name": props.get("name", "")}

        if props.get("childEntity"):
            result["child_count"] = len(props["childEntity"])
            result["children"] = [
                child.name for child in props["childEntity"] if hasattr(child, "name")
            ]

        return result

    async def _create_folder(self, params: dict[str, Any]) -> dict:
        """Create folder."""
        parent_folder_name = params.get("parent_folder")
        if not parent_folder_name:
            raise ValueError("parent_folder is required")

        folder_name = params.get("folder_name")
        if not folder_name:
            raise ValueError("folder_name is required")

        parent = self._find_folder(parent_folder_name)
        if not parent:
            raise ValueError(f"Parent folder not found: {parent_folder_name}")

        new_folder = parent.CreateFolder(name=folder_name)
        return {"message": f"Folder '{folder_name}' created", "name": new_folder.name}

    async def _rename_folder(self, params: dict[str, Any]) -> dict:
        """Rename folder."""
        folder_name = params.get("folder_name")
        if not folder_name:
            raise ValueError("folder_name is required")

        new_name = params.get("new_name")
        if not new_name:
            raise ValueError("new_name is required")

        folder = self._find_folder(folder_name)
        if not folder:
            raise ValueError(f"Folder not found: {folder_name}")

        task = folder.Rename_Task(newName=new_name)
        return {"message": f"Folder renamed to {new_name}", "task_id": str(task._moId)}

    async def _destroy_folder(self, params: dict[str, Any]) -> dict:
        """Destroy folder."""
        folder_name = params.get("folder_name")
        if not folder_name:
            raise ValueError("folder_name is required")

        folder = self._find_folder(folder_name)
        if not folder:
            raise ValueError(f"Folder not found: {folder_name}")

        task = folder.Destroy_Task()
        return {
            "message": f"Folder '{folder_name}' destruction initiated",
            "task_id": str(task._moId),
        }

    async def _move_into_folder(self, params: dict[str, Any]) -> dict:
        """Move entity into folder."""
        target_folder_name = params.get("target_folder")
        if not target_folder_name:
            raise ValueError("target_folder is required")

        entity_name = params.get("entity_name")
        if not entity_name:
            raise ValueError("entity_name is required")

        entity_type = params.get("entity_type", "vm")

        folder = self._find_folder(target_folder_name)
        if not folder:
            raise ValueError(f"Folder not found: {target_folder_name}")

        entity = None
        if entity_type == "vm":
            entity = self._find_vm(entity_name)
        elif entity_type == "folder":
            entity = self._find_folder(entity_name)

        if not entity:
            raise ValueError(f"{entity_type} not found: {entity_name}")

        task = folder.MoveIntoFolder_Task(list=[entity])
        return {
            "message": f"Moving {entity_name} into {target_folder_name}",
            "task_id": str(task._moId),
        }

    async def _create_resource_pool(self, params: dict[str, Any]) -> dict:
        """Create resource pool."""
        from pyVmomi import vim

        parent_pool_name = params.get("parent_pool")
        if not parent_pool_name:
            raise ValueError("parent_pool is required")

        pool_name = params.get("pool_name")
        if not pool_name:
            raise ValueError("pool_name is required")

        parent = self._find_resource_pool(parent_pool_name)
        if not parent:
            raise ValueError(f"Parent pool not found: {parent_pool_name}")

        cpu_alloc = vim.ResourceAllocationInfo()
        cpu_alloc.reservation = params.get("cpu_reservation", 0)
        cpu_alloc.limit = -1  # type: ignore[assignment]
        cpu_alloc.shares = vim.SharesInfo(level="normal")  # type: ignore[assignment]

        mem_alloc = vim.ResourceAllocationInfo()
        mem_alloc.reservation = params.get("memory_reservation_mb", 0)
        mem_alloc.limit = -1  # type: ignore[assignment]
        mem_alloc.shares = vim.SharesInfo(level="normal")  # type: ignore[assignment]

        spec = vim.ResourceConfigSpec()
        spec.cpuAllocation = cpu_alloc
        spec.memoryAllocation = mem_alloc

        new_pool = parent.CreateResourcePool(name=pool_name, spec=spec)
        return {"message": f"Resource pool '{pool_name}' created", "name": new_pool.name}

    async def _destroy_resource_pool(self, params: dict[str, Any]) -> dict:
        """Destroy resource pool."""
        pool_name = params.get("pool_name")
        if not pool_name:
            raise ValueError("pool_name is required")

        pool = self._find_resource_pool(pool_name)
        if not pool:
            raise ValueError(f"Resource pool not found: {pool_name}")

        task = pool.Destroy_Task()
        return {
            "message": f"Resource pool '{pool_name}' destruction initiated",
            "task_id": str(task._moId),
        }

    async def _update_resource_pool(self, params: dict[str, Any]) -> dict:
        """Update resource pool configuration."""
        from pyVmomi import vim

        pool_name = params.get("pool_name")
        if not pool_name:
            raise ValueError("pool_name is required")

        pool = self._find_resource_pool(pool_name)
        if not pool:
            raise ValueError(f"Resource pool not found: {pool_name}")

        spec = vim.ResourceConfigSpec()
        spec.cpuAllocation = vim.ResourceAllocationInfo()
        spec.cpuAllocation.reservation = params.get(
            "cpu_reservation", pool.config.cpuAllocation.reservation
        )
        spec.cpuAllocation.limit = params.get("cpu_limit", pool.config.cpuAllocation.limit)
        spec.cpuAllocation.shares = pool.config.cpuAllocation.shares

        spec.memoryAllocation = vim.ResourceAllocationInfo()
        spec.memoryAllocation.reservation = params.get(
            "memory_reservation_mb", pool.config.memoryAllocation.reservation
        )
        spec.memoryAllocation.limit = params.get(
            "memory_limit_mb", pool.config.memoryAllocation.limit
        )
        spec.memoryAllocation.shares = pool.config.memoryAllocation.shares

        pool.UpdateConfig(name=pool_name, config=spec)
        return {"message": f"Resource pool '{pool_name}' updated"}

    async def _get_content_library_items(self, params: dict[str, Any]) -> list[dict]:
        """Get content library items."""
        return [
            {
                "message": "Content Library access requires vSphere REST API",
                "note": "Use /rest/com/vmware/content/library/item endpoint",
            }
        ]

    async def _deploy_library_item(self, params: dict[str, Any]) -> dict:
        """Deploy VM from content library."""
        return {
            "message": "Content Library deployment requires vSphere REST API",
            "note": "Use /rest/vcenter/vm-template/library-items endpoint",
        }

    async def _list_tags(self, params: dict[str, Any]) -> list[dict]:
        """List all tags."""
        # Tags require vSphere REST API (com.vmware.cis.tagging)
        return [
            {
                "message": "Tag operations require vSphere REST API",
                "note": "Use /rest/com/vmware/cis/tagging/tag endpoint",
            }
        ]

    async def _list_tag_categories(self, params: dict[str, Any]) -> list[dict]:
        """List tag categories."""
        return [
            {
                "message": "Tag operations require vSphere REST API",
                "note": "Use /rest/com/vmware/cis/tagging/category endpoint",
            }
        ]

    async def _search_inventory(self, params: dict[str, Any]) -> list[dict]:
        """Search vCenter inventory."""
        from pyVmomi import vim

        name_pattern = params.get("name_pattern")
        if not name_pattern:
            raise ValueError("name_pattern is required")

        entity_type = params.get("entity_type", "vm")

        type_map = {
            "vm": vim.VirtualMachine,
            "host": vim.HostSystem,
            "datastore": vim.Datastore,
            "network": vim.Network,
            "cluster": vim.ClusterComputeResource,
        }

        managed_type = type_map.get(entity_type, vim.VirtualMachine)

        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [managed_type], True
        )
        try:
            import fnmatch

            pattern = str(name_pattern)
            results = []
            for obj in container.view:
                if fnmatch.fnmatch(obj.name, pattern):
                    results.append(
                        {
                            "name": obj.name,
                            "type": type(obj).__name__,
                        }
                    )
            return results
        finally:
            container.Destroy()

    async def _get_inventory_path(self, params: dict[str, Any]) -> dict:
        """Get inventory path of entity."""
        entity_name = params.get("entity_name")
        entity_type = params.get("entity_type")

        if not entity_name or not entity_type:
            raise ValueError("entity_name and entity_type are required")

        # Find entity based on type
        entity = None
        if entity_type == "vm":
            entity = self._find_vm(str(entity_name))
        elif entity_type == "host":
            entity = self._find_host(str(entity_name))
        elif entity_type == "datastore":
            entity = self._find_datastore(str(entity_name))
        elif entity_type == "folder":
            entity = self._find_folder(str(entity_name))
        elif entity_type == "cluster":
            entity = self._find_cluster(str(entity_name))

        if not entity:
            raise ValueError(f"{entity_type} not found: {entity_name}")

        # Build path
        path_parts = [entity.name]
        parent = entity.parent
        while parent:
            path_parts.insert(0, parent.name)
            parent = getattr(parent, "parent", None)

        return {
            "entity_name": entity_name,
            "entity_type": entity_type,
            "path": "/" + "/".join(path_parts),
        }

    async def _list_content_libraries(self, params: dict[str, Any]) -> list[dict]:
        """List content libraries."""
        # Note: Content Library requires separate REST API (com.vmware.content)
        # vSphere API doesn't directly expose content library through vim
        return [
            {
                "message": "Content Library access requires vSphere REST API",
                "note": "Use /rest/com/vmware/content/library endpoint",
            }
        ]
