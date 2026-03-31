"""
VMware Operation Definitions - Split by Category

These are registered in the generic connector_operation table
so the agent can discover them via search_operations.
"""

from meho_openapi.connectors.base import OperationDefinition


# STORAGE OPERATIONS

STORAGE_OPERATIONS = [
    OperationDefinition(
        operation_id="list_datastores",
        name="List Datastores",
        description="Get all datastores with capacity and free space.",
        category="storage",
        parameters=[],
        example="list_datastores()",
    ),
    OperationDefinition(
        operation_id="get_datastore",
        name="Get Datastore Details",
        description="Get detailed info about a datastore including connected hosts.",
        category="storage",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of datastore"}
        ],
        example="get_datastore(datastore_name='datastore1')",
    ),
    OperationDefinition(
        operation_id="get_vm_disks",
        name="Get VM Disks",
        description="List all virtual disks attached to a VM with size and datastore info.",
        category="storage",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"}
        ],
        example="get_vm_disks(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="relocate_vm",
        name="Relocate VM (Storage vMotion)",
        description="Relocate VM storage to different datastore. pyvmomi: RelocateVM_Task(spec, priority)",
        category="storage",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "target_datastore", "type": "string", "required": False, "description": "Target datastore name"},
            {"name": "target_host", "type": "string", "required": False, "description": "Target ESXi host name"},
            {"name": "priority", "type": "string", "required": False, "description": "Migration priority: default, high, low (default: default)"},
        ],
        example="relocate_vm(vm_name='web-01', target_datastore='ssd-datastore-01')",
    ),
    OperationDefinition(
        operation_id="get_host_datastores",
        name="Get Host Datastores",
        description="List all datastores accessible from a specific ESXi host.",
        category="storage",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"}
        ],
        example="get_host_datastores(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="browse_datastore",
        name="Browse Datastore",
        description="Browse files and folders in a datastore. Optionally filter by path.",
        category="storage",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of datastore"},
            {"name": "path", "type": "string", "required": False, "description": "Path within datastore (default: root)"},
        ],
        example="browse_datastore(datastore_name='datastore1', path='/vm-templates')",
    ),
    OperationDefinition(
        operation_id="get_datastore_vms",
        name="Get VMs on Datastore",
        description="List all VMs stored on a specific datastore.",
        category="storage",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of datastore"}
        ],
        example="get_datastore_vms(datastore_name='datastore1')",
    ),
    OperationDefinition(
        operation_id="consolidate_disks",
        name="Consolidate VM Disks",
        description="Consolidate VM disk files after snapshot operations. pyvmomi: ConsolidateVMDisks_Task()",
        category="storage",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="consolidate_disks(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="defragment_all_disks",
        name="Defragment All VM Disks",
        description="Defragment all virtual disks attached to a VM. pyvmomi: DefragmentAllDisks()",
        category="storage",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="defragment_all_disks(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="refresh_storage_info",
        name="Refresh VM Storage Info",
        description="Refresh storage information for a VM. pyvmomi: RefreshStorageInfo()",
        category="storage",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="refresh_storage_info(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="query_changed_disk_areas",
        name="Query Changed Disk Areas (CBT)",
        description="Get changed disk blocks since a snapshot for incremental backup. pyvmomi: QueryChangedDiskAreas()",
        category="storage",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "snapshot_name", "type": "string", "required": True, "description": "Snapshot to compare against"},
            {"name": "device_key", "type": "integer", "required": True, "description": "Disk device key"},
            {"name": "start_offset", "type": "integer", "required": True, "description": "Starting byte offset"},
            {"name": "change_id", "type": "string", "required": True, "description": "Change ID from previous query"},
        ],
        example="query_changed_disk_areas(vm_name='web-01', snapshot_name='backup-snap', device_key=2000, start_offset=0, change_id='*')",
    ),
    OperationDefinition(
        operation_id="refresh_datastore",
        name="Refresh Datastore",
        description="Refresh datastore storage information. pyvmomi: RefreshDatastore()",
        category="storage",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of datastore"},
        ],
        example="refresh_datastore(datastore_name='datastore1')",
    ),
    OperationDefinition(
        operation_id="enter_datastore_maintenance_mode",
        name="Enter Datastore Maintenance Mode",
        description="Put datastore into maintenance mode for storage operations. pyvmomi: DatastoreEnterMaintenanceMode()",
        category="storage",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of datastore"},
        ],
        example="enter_datastore_maintenance_mode(datastore_name='datastore1')",
    ),
    OperationDefinition(
        operation_id="exit_datastore_maintenance_mode",
        name="Exit Datastore Maintenance Mode",
        description="Take datastore out of maintenance mode. pyvmomi: DatastoreExitMaintenanceMode_Task()",
        category="storage",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of datastore"},
        ],
        example="exit_datastore_maintenance_mode(datastore_name='datastore1')",
    ),
    OperationDefinition(
        operation_id="rename_datastore",
        name="Rename Datastore",
        description="Rename a datastore in vCenter inventory. pyvmomi: RenameDatastore(newName)",
        category="storage",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Current datastore name"},
            {"name": "new_name", "type": "string", "required": True, "description": "New datastore name"},
        ],
        example="rename_datastore(datastore_name='datastore1', new_name='ssd-primary')",
    ),
    OperationDefinition(
        operation_id="attach_disk",
        name="Attach Virtual Disk",
        description="Attach an existing virtual disk to a VM. pyvmomi: AttachDisk_Task(diskId, datastore, controllerKey, unitNumber)",
        category="storage",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "disk_path", "type": "string", "required": True, "description": "Datastore path to VMDK file"},
            {"name": "controller_key", "type": "integer", "required": False, "description": "Controller device key"},
            {"name": "unit_number", "type": "integer", "required": False, "description": "Unit number on controller"},
        ],
        example="attach_disk(vm_name='web-01', disk_path='[datastore1] disks/data.vmdk')",
    ),
    OperationDefinition(
        operation_id="detach_disk",
        name="Detach Virtual Disk",
        description="Detach a virtual disk from a VM without deleting the VMDK. pyvmomi: DetachDisk_Task(diskId)",
        category="storage",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "disk_label", "type": "string", "required": True, "description": "Label of disk to detach (e.g., 'Hard disk 2')"},
        ],
        example="detach_disk(vm_name='web-01', disk_label='Hard disk 2')",
    ),
    OperationDefinition(
        operation_id="add_disk",
        name="Add New Virtual Disk",
        description="Add a new virtual disk to a VM. Creates a new VMDK file.",
        category="storage",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "size_gb", "type": "integer", "required": True, "description": "Disk size in GB"},
            {"name": "thin_provisioned", "type": "boolean", "required": False, "description": "Use thin provisioning (default: True)"},
            {"name": "datastore_name", "type": "string", "required": False, "description": "Target datastore (default: VM's datastore)"},
        ],
        example="add_disk(vm_name='web-01', size_gb=100, thin_provisioned=True)",
    ),
    OperationDefinition(
        operation_id="extend_disk",
        name="Extend Virtual Disk",
        description="Extend the size of an existing virtual disk.",
        category="storage",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "disk_label", "type": "string", "required": True, "description": "Label of disk to extend (e.g., 'Hard disk 1')"},
            {"name": "new_size_gb", "type": "integer", "required": True, "description": "New disk size in GB (must be larger than current)"},
        ],
        example="extend_disk(vm_name='web-01', disk_label='Hard disk 1', new_size_gb=200)",
    ),
    OperationDefinition(
        operation_id="scan_host_storage",
        name="Rescan Host Storage",
        description="Rescan storage adapters on a host to discover new LUNs/datastores.",
        category="storage",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="scan_host_storage(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="destroy_datastore",
        name="Destroy Datastore",
        description="Remove a datastore from vCenter. pyvmomi: DestroyDatastore()",
        category="storage",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of datastore to destroy"},
        ],
        example="destroy_datastore(datastore_name='old-datastore')",
    ),
    OperationDefinition(
        operation_id="refresh_datastore_storage_info",
        name="Refresh Datastore Storage Info",
        description="Refresh storage information for datastore. pyvmomi: RefreshDatastoreStorageInfo()",
        category="storage",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of datastore"},
        ],
        example="refresh_datastore_storage_info(datastore_name='datastore1')",
    ),
    OperationDefinition(
        operation_id="create_vmfs_datastore",
        name="Create VMFS Datastore",
        description="Create a new VMFS datastore on a host.",
        category="storage",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name for new datastore"},
            {"name": "device_path", "type": "string", "required": True, "description": "Device path (e.g., 'naa.xxx')"},
        ],
        example="create_vmfs_datastore(host_name='esxi-01', datastore_name='new-vmfs', device_path='naa.xxx')",
    ),
    OperationDefinition(
        operation_id="create_nfs_datastore",
        name="Create NFS Datastore",
        description="Create a new NFS datastore on a host.",
        category="storage",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name for new datastore"},
            {"name": "remote_host", "type": "string", "required": True, "description": "NFS server hostname/IP"},
            {"name": "remote_path", "type": "string", "required": True, "description": "NFS export path"},
            {"name": "access_mode", "type": "string", "required": False, "description": "Access mode: readWrite, readOnly (default: readWrite)"},
        ],
        example="create_nfs_datastore(host_name='esxi-01', datastore_name='nfs-share', remote_host='nas.example.com', remote_path='/exports/vmware')",
    ),
    OperationDefinition(
        operation_id="remove_datastore",
        name="Remove Datastore from Host",
        description="Unmount/remove a datastore from a specific host.",
        category="storage",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of datastore to remove"},
        ],
        example="remove_datastore(host_name='esxi-01.example.com', datastore_name='old-nfs')",
    ),
    OperationDefinition(
        operation_id="expand_vmfs_datastore",
        name="Expand VMFS Datastore",
        description="Expand a VMFS datastore to use additional space on its LUN.",
        category="storage",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of VMFS datastore"},
        ],
        example="expand_vmfs_datastore(datastore_name='datastore1')",
    ),
    OperationDefinition(
        operation_id="get_storage_pods",
        name="List Storage DRS Pods",
        description="List all Storage DRS pods (datastore clusters).",
        category="storage",
        parameters=[],
        example="get_storage_pods()",
    ),
    OperationDefinition(
        operation_id="get_storage_pod",
        name="Get Storage Pod Details",
        description="Get details of a Storage DRS pod including member datastores.",
        category="storage",
        parameters=[
            {"name": "pod_name", "type": "string", "required": True, "description": "Name of storage pod"},
        ],
        example="get_storage_pod(pod_name='StoragePod-01')",
    ),
]
