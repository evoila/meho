"""
Storage Operation Handlers

Mixin class containing 12 storage operation handlers.
"""

from typing import List, Dict, Any, Optional


class StorageHandlerMixin:
    """Mixin for storage operation handlers."""
    
    # These will be provided by VMwareConnector (base class)
    _content: Any
    
    # Helper methods (will be provided by VMwareConnector) - stubs for type checking
    def _find_datastore(self, name: str) -> Optional[Any]: return None
    def _find_vm(self, name: str) -> Optional[Any]: return None
    def _find_host(self, name: str) -> Optional[Any]: return None
    
    # Serializer methods (will be provided by VMwareConnector) - stubs for type checking
    def _serialize_datastore_properties(self, ds: Any) -> Dict[str, Any]: return {}
    def _serialize_vm_properties(self, vm: Any) -> Dict[str, Any]: return {}
    
    async def _list_datastores(self, params: Dict[str, Any]) -> List[Dict]:
        """List all datastores with complete property data."""
        from pyVmomi import vim
        
        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.Datastore], True
        )
        try:
            return [self._serialize_datastore_properties(ds) for ds in container.view]
        finally:
            container.Destroy()
    

    async def _get_datastore(self, params: Dict[str, Any]) -> Dict:
        """Get datastore details with complete property data."""
        ds_name = params.get("datastore_name") or params.get("name")
        if not ds_name:
            raise ValueError("datastore_name is required")
        
        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")
        
        return self._serialize_datastore_properties(ds)
    

    async def _browse_datastore(self, params: Dict[str, Any]) -> List[Dict]:
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
        
        try:
            task = browser.SearchDatastore_Task(datastorePath=search_path, searchSpec=search_spec)
            # For simplicity, return immediately - in production would wait for task
            return [{
                "path": search_path,
                "message": "Search initiated - use list_tasks to check status",
                "task_id": str(task._moId),
            }]
        except Exception as e:
            return [{"error": str(e)}]
    

    async def _refresh_datastore(self, params: Dict[str, Any]) -> Dict:
        """Refresh datastore."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")
        
        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")
        
        ds.RefreshDatastore()
        return {"message": f"Datastore {ds_name} refreshed"}
    

    async def _rename_datastore(self, params: Dict[str, Any]) -> Dict:
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
    

    async def _destroy_datastore(self, params: Dict[str, Any]) -> Dict:
        """Destroy datastore."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")
        
        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")
        
        ds.DestroyDatastore()
        return {"message": f"Datastore {ds_name} destroyed"}
    

    async def _refresh_datastore_storage_info(self, params: Dict[str, Any]) -> Dict:
        """Refresh datastore storage info."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")
        
        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")
        
        ds.RefreshDatastoreStorageInfo()
        return {"message": f"Storage info refreshed for {ds_name}"}
    

    async def _create_nfs_datastore(self, params: Dict[str, Any]) -> Dict:
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
    

    async def _remove_datastore(self, params: Dict[str, Any]) -> Dict:
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
    

    async def _get_datastore_performance(self, params: Dict[str, Any]) -> Dict:
        """Get datastore performance metrics."""
        ds_name = params.get("datastore_name")
        if not ds_name:
            raise ValueError("datastore_name is required")
        
        ds = self._find_datastore(ds_name)
        if not ds:
            raise ValueError(f"Datastore not found: {ds_name}")
        
        return {
            "datastore_name": ds_name,
            "capacity_gb": ds.summary.capacity // (1024**3),
            "free_space_gb": ds.summary.freeSpace // (1024**3),
            "uncommitted_gb": (ds.summary.uncommitted // (1024**3)) if ds.summary.uncommitted else 0,
            "accessible": ds.summary.accessible,
            "maintenance_mode": ds.summary.maintenanceMode,
        }
    

    async def _get_storage_pods(self, params: Dict[str, Any]) -> List[Dict]:
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
    

    async def _get_storage_pod(self, params: Dict[str, Any]) -> Dict:
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
                        "datastores": [ds.name for ds in pod.childEntity] if pod.childEntity else [],
                    }
            raise ValueError(f"Storage pod not found: {pod_name}")
        finally:
            container.Destroy()
    

