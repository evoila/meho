"""
VMware Connector Helper Methods

Utility functions for finding VMware objects by name.
"""

from typing import Optional, Any, List


def find_vm(content: Any, name: str) -> Optional[Any]:
    """Find VM by name."""
    from pyVmomi import vim
    
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        for vm in container.view:
            if vm.name == name:
                return vm
        return None
    finally:
        container.Destroy()


def find_cluster(content: Any, name: str) -> Optional[Any]:
    """Find cluster by name."""
    from pyVmomi import vim
    
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.ClusterComputeResource], True
    )
    try:
        for c in container.view:
            if c.name == name:
                return c
        return None
    finally:
        container.Destroy()


def find_host(content: Any, name: str) -> Optional[Any]:
    """Find host by name."""
    from pyVmomi import vim
    
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.HostSystem], True
    )
    try:
        for h in container.view:
            if h.name == name:
                return h
        return None
    finally:
        container.Destroy()


def find_datastore(content: Any, name: str) -> Optional[Any]:
    """Find datastore by name."""
    from pyVmomi import vim
    
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.Datastore], True
    )
    try:
        for ds in container.view:
            if ds.name == name:
                return ds
        return None
    finally:
        container.Destroy()


def find_folder(content: Any, name: str) -> Optional[Any]:
    """Find folder by name."""
    from pyVmomi import vim
    
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.Folder], True
    )
    try:
        for folder in container.view:
            if folder.name == name:
                return folder
        return None
    finally:
        container.Destroy()


def find_snapshot(vm: Any, snapshot_name: str) -> Optional[Any]:
    """Find snapshot by name within a VM's snapshot tree."""
    if not vm.snapshot:
        return None
    return search_snapshot_tree(vm.snapshot.rootSnapshotList, snapshot_name)


def search_snapshot_tree(snapshot_list: List, name: str) -> Optional[Any]:
    """Recursively search snapshot tree for a snapshot by name."""
    for snap in snapshot_list or []:
        if snap.name == name:
            return snap.snapshot
        if snap.childSnapshotList:
            found = search_snapshot_tree(snap.childSnapshotList, name)
            if found:
                return found
    return None


def find_resource_pool(content: Any, name: str) -> Optional[Any]:
    """Find resource pool by name."""
    from pyVmomi import vim
    
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.ResourcePool], True
    )
    try:
        for rp in container.view:
            if rp.name == name:
                return rp
        return None
    finally:
        container.Destroy()


def find_dvs(content: Any, name: str) -> Optional[Any]:
    """Find distributed virtual switch by name."""
    from pyVmomi import vim
    
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.DistributedVirtualSwitch], True
    )
    try:
        for dvs in container.view:
            if dvs.name == name:
                return dvs
        return None
    finally:
        container.Destroy()


def find_network(content: Any, name: str) -> Optional[Any]:
    """Find network by name."""
    from pyVmomi import vim
    
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.Network], True
    )
    try:
        for net in container.view:
            if net.name == name:
                return net
        return None
    finally:
        container.Destroy()


def make_guest_auth(username: str, password: str) -> Any:
    """Create guest authentication object."""
    from pyVmomi import vim
    creds = vim.vm.guest.NamePasswordAuthentication()
    creds.username = username
    creds.password = password
    return creds


def collect_snapshots(snapshot_list: List, results: List[dict], parent: Optional[str] = None) -> None:
    """Recursively collect snapshot info."""
    for snap in snapshot_list or []:
        results.append({
            "name": snap.name,
            "description": snap.description,
            "created": str(snap.createTime) if snap.createTime else None,
            "state": str(snap.state) if snap.state else None,
            "quiesced": snap.quiesced,
            "parent": parent,
        })
        if snap.childSnapshotList:
            collect_snapshots(snap.childSnapshotList, results, snap.name)

