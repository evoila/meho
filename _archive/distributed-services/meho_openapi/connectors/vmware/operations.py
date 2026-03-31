"""
VMware Operation Definitions (TASK-97)

These are registered in the generic connector_operation table
so the agent can discover them via search_operations.
"""

from meho_openapi.connectors.base import OperationDefinition


VMWARE_OPERATIONS = [
    OperationDefinition(
        operation_id="list_virtual_machines",
        name="List Virtual Machines",
        description="Get all VMs in vCenter. Returns name, power state, CPU, memory, IP.",
        category="compute",
        parameters=[],
        example="list_virtual_machines()",
    ),
    OperationDefinition(
        operation_id="get_virtual_machine",
        name="Get Virtual Machine Details",
        description="Get detailed info about a specific VM including CPU, memory, guest OS, IP.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"}
        ],
        example="get_virtual_machine(vm_name='my-vm')",
    ),
    OperationDefinition(
        operation_id="power_on_vm",
        name="Power On VM",
        description="Power on a virtual machine. Returns task ID for tracking.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"}
        ],
        example="power_on_vm(vm_name='my-vm')",
    ),
    OperationDefinition(
        operation_id="power_off_vm",
        name="Power Off VM",
        description="Power off a virtual machine (hard power off). Returns task ID.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"}
        ],
        example="power_off_vm(vm_name='my-vm')",
    ),
    OperationDefinition(
        operation_id="shutdown_guest",
        name="Shutdown Guest OS",
        description="Gracefully shutdown the guest operating system. Requires VMware Tools.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"}
        ],
        example="shutdown_guest(vm_name='my-vm')",
    ),
    OperationDefinition(
        operation_id="list_clusters",
        name="List Clusters",
        description="Get all clusters in vCenter. Returns name, host count, DRS/HA status.",
        category="compute",
        parameters=[],
        example="list_clusters()",
    ),
    OperationDefinition(
        operation_id="get_cluster",
        name="Get Cluster Details",
        description="Get detailed info about a cluster including DRS and HA config.",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"}
        ],
        example="get_cluster(cluster_name='Production')",
    ),
    OperationDefinition(
        operation_id="get_drs_recommendations",
        name="Get DRS Recommendations",
        description="Get pending DRS recommendations for a cluster. DRS suggests VM migrations for load balancing.",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"}
        ],
        example="get_drs_recommendations(cluster_name='Production')",
    ),
    OperationDefinition(
        operation_id="apply_drs_recommendation",
        name="Apply DRS Recommendation",
        description="Apply a DRS recommendation to migrate VMs. Requires recommendation key from get_drs_recommendations.",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
            {"name": "recommendation_key", "type": "string", "required": True, "description": "Recommendation key to apply"},
        ],
        example="apply_drs_recommendation(cluster_name='Production', recommendation_key='rec-1')",
    ),
    OperationDefinition(
        operation_id="list_hosts",
        name="List ESXi Hosts",
        description="Get all ESXi hosts in vCenter. Returns name, connection state, CPU, memory.",
        category="compute",
        parameters=[],
        example="list_hosts()",
    ),
    OperationDefinition(
        operation_id="get_host",
        name="Get Host Details",
        description="Get detailed info about an ESXi host including hardware specs.",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of host"}
        ],
        example="get_host(host_name='esxi-01.example.com')",
    ),
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
        operation_id="list_networks",
        name="List Networks",
        description="Get all networks in vCenter.",
        category="networking",
        parameters=[],
        example="list_networks()",
    ),
    OperationDefinition(
        operation_id="list_datacenters",
        name="List Datacenters",
        description="Get all datacenters in vCenter.",
        category="inventory",
        parameters=[],
        example="list_datacenters()",
    ),
    OperationDefinition(
        operation_id="list_resource_pools",
        name="List Resource Pools",
        description="Get all resource pools in vCenter.",
        category="compute",
        parameters=[],
        example="list_resource_pools()",
    ),
    OperationDefinition(
        operation_id="list_folders",
        name="List Folders",
        description="Get all VM folders in vCenter.",
        category="inventory",
        parameters=[],
        example="list_folders()",
    ),
    OperationDefinition(
        operation_id="get_vcenter_info",
        name="Get vCenter Info",
        description="Get vCenter Server version and build information.",
        category="system",
        parameters=[],
        example="get_vcenter_info()",
    ),
    OperationDefinition(
        operation_id="list_tasks",
        name="List Recent Tasks",
        description="Get recent tasks from vCenter task manager.",
        category="system",
        parameters=[
            {"name": "limit", "type": "integer", "required": False, "description": "Max tasks to return (default 20)"}
        ],
        example="list_tasks(limit=10)",
    ),
    OperationDefinition(
        operation_id="list_alarms",
        name="List Triggered Alarms",
        description="Get currently triggered alarms in vCenter.",
        category="monitoring",
        parameters=[],
        example="list_alarms()",
    ),
    
    # =========================================================================
    # PHASE 1: VM SNAPSHOT OPERATIONS (TASK-99)
    # =========================================================================
    
    OperationDefinition(
        operation_id="create_snapshot",
        name="Create VM Snapshot",
        description="Create a snapshot of a virtual machine. Captures current state including memory (optional).",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "snapshot_name", "type": "string", "required": True, "description": "Name for the snapshot"},
            {"name": "description", "type": "string", "required": False, "description": "Snapshot description"},
            {"name": "include_memory", "type": "boolean", "required": False, "description": "Include VM memory state (default: False)"},
            {"name": "quiesce_guest", "type": "boolean", "required": False, "description": "Quiesce guest filesystem (default: True)"},
        ],
        example="create_snapshot(vm_name='web-01', snapshot_name='pre-upgrade', description='Before patch')",
    ),
    OperationDefinition(
        operation_id="list_snapshots",
        name="List VM Snapshots",
        description="Get all snapshots for a virtual machine with creation time and description.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"}
        ],
        example="list_snapshots(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="revert_snapshot",
        name="Revert to Snapshot",
        description="Revert a virtual machine to a previous snapshot state. pyvmomi: RevertToSnapshot_Task(host, suppressPowerOn)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "snapshot_name", "type": "string", "required": True, "description": "Name of snapshot to revert to"},
            {"name": "target_host", "type": "string", "required": False, "description": "Target host for the reverted VM (optional)"},
            {"name": "suppress_power_on", "type": "boolean", "required": False, "description": "Prevent VM from powering on after revert (default: False)"},
        ],
        example="revert_snapshot(vm_name='web-01', snapshot_name='pre-upgrade')",
    ),
    OperationDefinition(
        operation_id="delete_snapshot",
        name="Delete VM Snapshot",
        description="Delete a snapshot from a virtual machine. pyvmomi: RemoveSnapshot_Task(removeChildren, consolidate)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "snapshot_name", "type": "string", "required": True, "description": "Name of snapshot to delete"},
            {"name": "remove_children", "type": "boolean", "required": False, "description": "Also remove child snapshots (default: False)"},
            {"name": "consolidate", "type": "boolean", "required": False, "description": "Consolidate disks after removal (default: True)"},
        ],
        example="delete_snapshot(vm_name='web-01', snapshot_name='pre-upgrade')",
    ),
    OperationDefinition(
        operation_id="delete_all_snapshots",
        name="Delete All VM Snapshots",
        description="Delete all snapshots from a virtual machine. pyvmomi: RemoveAllSnapshots_Task(consolidate, spec)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "consolidate", "type": "boolean", "required": False, "description": "Consolidate disks after removal (default: True)"},
        ],
        example="delete_all_snapshots(vm_name='web-01')",
    ),
    
    # =========================================================================
    # PHASE 1: VM CONFIGURATION OPERATIONS (TASK-99)
    # =========================================================================
    
    OperationDefinition(
        operation_id="reconfigure_vm_cpu",
        name="Reconfigure VM CPU",
        description="Change the number of CPUs for a virtual machine. VM may need to be powered off.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "num_cpus", "type": "integer", "required": True, "description": "Number of virtual CPUs"},
            {"name": "cores_per_socket", "type": "integer", "required": False, "description": "Cores per socket (default: 1)"},
        ],
        example="reconfigure_vm_cpu(vm_name='web-01', num_cpus=4)",
    ),
    OperationDefinition(
        operation_id="reconfigure_vm_memory",
        name="Reconfigure VM Memory",
        description="Change the memory size for a virtual machine. VM may need to be powered off.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "memory_mb", "type": "integer", "required": True, "description": "Memory size in MB"},
        ],
        example="reconfigure_vm_memory(vm_name='web-01', memory_mb=8192)",
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
        operation_id="get_vm_nics",
        name="Get VM Network Adapters",
        description="List all network adapters attached to a VM with MAC and network info.",
        category="networking",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"}
        ],
        example="get_vm_nics(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="rename_vm",
        name="Rename Virtual Machine",
        description="Rename a virtual machine in vCenter inventory.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Current name of the VM"},
            {"name": "new_name", "type": "string", "required": True, "description": "New name for the VM"},
        ],
        example="rename_vm(vm_name='old-name', new_name='new-name')",
    ),
    OperationDefinition(
        operation_id="set_vm_annotation",
        name="Set VM Annotation",
        description="Set or update the annotation (notes) for a virtual machine.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "annotation", "type": "string", "required": True, "description": "Annotation text"},
        ],
        example="set_vm_annotation(vm_name='web-01', annotation='Production web server')",
    ),
    
    # =========================================================================
    # PHASE 1: MIGRATION OPERATIONS (TASK-99)
    # =========================================================================
    
    OperationDefinition(
        operation_id="migrate_vm",
        name="Migrate VM (vMotion)",
        description="Live migrate a VM to a different host. pyvmomi: MigrateVM_Task(pool, host, priority, state)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "target_host", "type": "string", "required": True, "description": "Target ESXi host name"},
            {"name": "priority", "type": "string", "required": False, "description": "Migration priority: default, high, low (default: default)"},
        ],
        example="migrate_vm(vm_name='web-01', target_host='esxi-02.example.com')",
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
        operation_id="clone_vm",
        name="Clone Virtual Machine",
        description="Create a clone of a virtual machine. Can clone to different datastore/host.",
        category="compute",
        parameters=[
            {"name": "source_vm", "type": "string", "required": True, "description": "Name of source VM"},
            {"name": "clone_name", "type": "string", "required": True, "description": "Name for the clone"},
            {"name": "target_datastore", "type": "string", "required": False, "description": "Target datastore"},
            {"name": "target_folder", "type": "string", "required": False, "description": "Target folder"},
            {"name": "power_on", "type": "boolean", "required": False, "description": "Power on after clone (default: False)"},
        ],
        example="clone_vm(source_vm='template-vm', clone_name='new-web-01')",
    ),
    
    # =========================================================================
    # PHASE 2: HOST OPERATIONS (TASK-99)
    # =========================================================================
    
    OperationDefinition(
        operation_id="enter_maintenance_mode",
        name="Enter Maintenance Mode",
        description="Put an ESXi host into maintenance mode. VMs will be migrated if DRS is enabled.",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "evacuate_vms", "type": "boolean", "required": False, "description": "Evacuate powered-on VMs (default: True)"},
            {"name": "timeout", "type": "integer", "required": False, "description": "Timeout in seconds (default: 0 = no timeout)"},
        ],
        example="enter_maintenance_mode(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="exit_maintenance_mode",
        name="Exit Maintenance Mode",
        description="Take an ESXi host out of maintenance mode.",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "timeout", "type": "integer", "required": False, "description": "Timeout in seconds (default: 0)"},
        ],
        example="exit_maintenance_mode(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="get_host_vms",
        name="Get VMs on Host",
        description="List all VMs running on a specific ESXi host.",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"}
        ],
        example="get_host_vms(host_name='esxi-01.example.com')",
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
        operation_id="get_host_networks",
        name="Get Host Networks",
        description="List all networks available on a specific ESXi host.",
        category="networking",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"}
        ],
        example="get_host_networks(host_name='esxi-01.example.com')",
    ),
    
    # =========================================================================
    # PHASE 2: STORAGE OPERATIONS (TASK-99)
    # =========================================================================
    
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
    
    # =========================================================================
    # PHASE 2: NETWORK OPERATIONS (TASK-99)
    # =========================================================================
    
    OperationDefinition(
        operation_id="list_distributed_switches",
        name="List Distributed Switches",
        description="List all vSphere Distributed Switches in vCenter.",
        category="networking",
        parameters=[],
        example="list_distributed_switches()",
    ),
    OperationDefinition(
        operation_id="get_distributed_switch",
        name="Get Distributed Switch Details",
        description="Get detailed configuration of a distributed switch.",
        category="networking",
        parameters=[
            {"name": "dvs_name", "type": "string", "required": True, "description": "Name of distributed switch"}
        ],
        example="get_distributed_switch(dvs_name='DSwitch-Production')",
    ),
    OperationDefinition(
        operation_id="list_port_groups",
        name="List Port Groups",
        description="List all port groups (standard and distributed).",
        category="networking",
        parameters=[],
        example="list_port_groups()",
    ),
    OperationDefinition(
        operation_id="get_port_group",
        name="Get Port Group Details",
        description="Get detailed configuration of a port group including VLAN.",
        category="networking",
        parameters=[
            {"name": "portgroup_name", "type": "string", "required": True, "description": "Name of port group"}
        ],
        example="get_port_group(portgroup_name='VM-Network')",
    ),
    
    # =========================================================================
    # PHASE 3: PERFORMANCE & EVENTS (TASK-99)
    # =========================================================================
    
    OperationDefinition(
        operation_id="get_vm_performance",
        name="Get VM Performance Metrics",
        description="Get CPU, memory, disk, and network performance metrics for a VM.",
        category="monitoring",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "interval", "type": "string", "required": False, "description": "Interval: realtime, past_day, past_week"},
        ],
        example="get_vm_performance(vm_name='web-01', interval='past_day')",
    ),
    OperationDefinition(
        operation_id="get_host_performance",
        name="Get Host Performance Metrics",
        description="Get CPU, memory, disk, and network performance metrics for a host.",
        category="monitoring",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "interval", "type": "string", "required": False, "description": "Interval: realtime, past_day, past_week"},
        ],
        example="get_host_performance(host_name='esxi-01.example.com', interval='realtime')",
    ),
    OperationDefinition(
        operation_id="get_events",
        name="Get Recent Events",
        description="Get recent events from vCenter event log.",
        category="monitoring",
        parameters=[
            {"name": "limit", "type": "integer", "required": False, "description": "Max events to return (default: 50)"},
            {"name": "entity_type", "type": "string", "required": False, "description": "Filter by entity type: vm, host, cluster"},
        ],
        example="get_events(limit=100, entity_type='vm')",
    ),
    OperationDefinition(
        operation_id="acknowledge_alarm",
        name="Acknowledge Alarm",
        description="Acknowledge a triggered alarm on an entity.",
        category="monitoring",
        parameters=[
            {"name": "entity_name", "type": "string", "required": True, "description": "Name of entity (VM, host, etc.)"},
            {"name": "entity_type", "type": "string", "required": True, "description": "Type: vm, host, cluster, datastore"},
        ],
        example="acknowledge_alarm(entity_name='web-01', entity_type='vm')",
    ),
    
    # =========================================================================
    # ADDITIONAL VM OPERATIONS (from pyvmomi introspection)
    # =========================================================================
    
    OperationDefinition(
        operation_id="reboot_guest",
        name="Reboot Guest OS",
        description="Gracefully reboot the guest operating system. Requires VMware Tools. pyvmomi: RebootGuest()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="reboot_guest(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="standby_guest",
        name="Standby Guest OS",
        description="Put the guest OS into standby/sleep mode. Requires VMware Tools. pyvmomi: StandbyGuest()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="standby_guest(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="suspend_vm",
        name="Suspend Virtual Machine",
        description="Suspend a running VM, saving its state to disk. pyvmomi: SuspendVM_Task()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="suspend_vm(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="reset_vm",
        name="Reset Virtual Machine",
        description="Hard reset a VM (like pressing reset button). pyvmomi: ResetVM_Task()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="reset_vm(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="mark_as_template",
        name="Convert VM to Template",
        description="Convert a virtual machine to a template. VM must be powered off. pyvmomi: MarkAsTemplate()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM to convert"},
        ],
        example="mark_as_template(vm_name='golden-image')",
    ),
    OperationDefinition(
        operation_id="mark_as_virtual_machine",
        name="Convert Template to VM",
        description="Convert a template back to a virtual machine. pyvmomi: MarkAsVirtualMachine(pool, host)",
        category="compute",
        parameters=[
            {"name": "template_name", "type": "string", "required": True, "description": "Name of the template"},
            {"name": "resource_pool", "type": "string", "required": False, "description": "Target resource pool"},
            {"name": "host_name", "type": "string", "required": False, "description": "Target host"},
        ],
        example="mark_as_virtual_machine(template_name='golden-image')",
    ),
    OperationDefinition(
        operation_id="upgrade_virtual_hardware",
        name="Upgrade VM Hardware Version",
        description="Upgrade VM to latest virtual hardware version. VM must be powered off. pyvmomi: UpgradeVM_Task(version)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "version", "type": "string", "required": False, "description": "Target hardware version (e.g., 'vmx-19'). Default: latest"},
        ],
        example="upgrade_virtual_hardware(vm_name='web-01')",
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
        operation_id="mount_tools_installer",
        name="Mount VMware Tools Installer",
        description="Mount the VMware Tools installer CD in the VM. pyvmomi: MountToolsInstaller()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="mount_tools_installer(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="unmount_tools_installer",
        name="Unmount VMware Tools Installer",
        description="Unmount the VMware Tools installer CD from the VM. pyvmomi: UnmountToolsInstaller()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="unmount_tools_installer(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="upgrade_tools",
        name="Upgrade VMware Tools",
        description="Upgrade VMware Tools in the guest OS. pyvmomi: UpgradeTools_Task(installerOptions)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "installer_options", "type": "string", "required": False, "description": "Installer command line options"},
        ],
        example="upgrade_tools(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="export_vm",
        name="Export VM as OVF",
        description="Export a VM to OVF format. Returns an HTTP NFC lease for downloading. pyvmomi: ExportVm()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="export_vm(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="unregister_vm",
        name="Unregister VM from Inventory",
        description="Remove VM from vCenter inventory without deleting files. pyvmomi: UnregisterVM()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="unregister_vm(vm_name='old-vm')",
    ),
    OperationDefinition(
        operation_id="destroy_vm",
        name="Destroy Virtual Machine",
        description="Delete a VM and all its files. DESTRUCTIVE OPERATION. pyvmomi: Destroy_Task()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM to destroy"},
        ],
        example="destroy_vm(vm_name='test-vm')",
    ),
    OperationDefinition(
        operation_id="answer_vm_question",
        name="Answer VM Question",
        description="Answer a question that is blocking VM operations. pyvmomi: AnswerVM(questionId, answerChoice)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "question_id", "type": "string", "required": True, "description": "ID of the question to answer"},
            {"name": "answer_choice", "type": "string", "required": True, "description": "Answer choice (e.g., 'button.uuid.movedTheVM')"},
        ],
        example="answer_vm_question(vm_name='web-01', question_id='0', answer_choice='button.uuid.movedTheVM')",
    ),
    OperationDefinition(
        operation_id="acquire_mks_ticket",
        name="Acquire VM Console Ticket",
        description="Get a ticket for VM console access (MKS/WMKS). pyvmomi: AcquireMksTicket()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="acquire_mks_ticket(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="acquire_ticket",
        name="Acquire VM Ticket",
        description="Get a ticket for VM access (console, VNC, etc). pyvmomi: AcquireTicket(ticketType)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "ticket_type", "type": "string", "required": True, "description": "Ticket type: mks, device, guestControl, webmks"},
        ],
        example="acquire_ticket(vm_name='web-01', ticket_type='webmks')",
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
    
    # =========================================================================
    # ADDITIONAL HOST OPERATIONS (from pyvmomi introspection)
    # =========================================================================
    
    OperationDefinition(
        operation_id="reboot_host",
        name="Reboot ESXi Host",
        description="Reboot an ESXi host. Host should be in maintenance mode. pyvmomi: RebootHost_Task(force)",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "force", "type": "boolean", "required": False, "description": "Force reboot even if VMs running (default: False)"},
        ],
        example="reboot_host(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="shutdown_host",
        name="Shutdown ESXi Host",
        description="Shutdown an ESXi host. Host should be in maintenance mode. pyvmomi: ShutdownHost_Task(force)",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "force", "type": "boolean", "required": False, "description": "Force shutdown even if VMs running (default: False)"},
        ],
        example="shutdown_host(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="disconnect_host",
        name="Disconnect Host from vCenter",
        description="Disconnect an ESXi host from vCenter Server. pyvmomi: DisconnectHost_Task()",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="disconnect_host(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="reconnect_host",
        name="Reconnect Host to vCenter",
        description="Reconnect a disconnected ESXi host to vCenter. pyvmomi: ReconnectHost_Task(cnxSpec, reconnectSpec)",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "username", "type": "string", "required": False, "description": "ESXi root username"},
            {"name": "password", "type": "string", "required": False, "description": "ESXi root password"},
        ],
        example="reconnect_host(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="enter_lockdown_mode",
        name="Enter Lockdown Mode",
        description="Put host into lockdown mode, restricting direct access. pyvmomi: EnterLockdownMode()",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="enter_lockdown_mode(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="exit_lockdown_mode",
        name="Exit Lockdown Mode",
        description="Take host out of lockdown mode, allowing direct access. pyvmomi: ExitLockdownMode()",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="exit_lockdown_mode(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="enter_standby_mode",
        name="Enter Standby Mode (Power Down)",
        description="Put host into standby mode to save power. VMs must be evacuated. pyvmomi: PowerDownHostToStandBy_Task()",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "timeout", "type": "integer", "required": False, "description": "Timeout in seconds"},
            {"name": "evacuate_vms", "type": "boolean", "required": False, "description": "Evacuate powered-off VMs (default: True)"},
        ],
        example="enter_standby_mode(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="exit_standby_mode",
        name="Exit Standby Mode (Power Up)",
        description="Wake up host from standby mode. pyvmomi: PowerUpHostFromStandBy_Task()",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "timeout", "type": "integer", "required": False, "description": "Timeout in seconds"},
        ],
        example="exit_standby_mode(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="query_host_connection_info",
        name="Query Host Connection Info",
        description="Get connection information for a host. pyvmomi: QueryHostConnectionInfo()",
        category="system",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="query_host_connection_info(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="retrieve_hardware_uptime",
        name="Get Host Uptime",
        description="Get hardware uptime for an ESXi host in seconds. pyvmomi: RetrieveHardwareUptime()",
        category="monitoring",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="retrieve_hardware_uptime(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="acquire_cim_ticket",
        name="Acquire CIM Services Ticket",
        description="Get a ticket for CIM (hardware monitoring) access. pyvmomi: AcquireCimServicesTicket()",
        category="monitoring",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="acquire_cim_ticket(host_name='esxi-01.example.com')",
    ),
    
    # =========================================================================
    # CLUSTER OPERATIONS (from pyvmomi introspection)
    # =========================================================================
    
    OperationDefinition(
        operation_id="cancel_drs_recommendation",
        name="Cancel DRS Recommendation",
        description="Cancel/dismiss a DRS recommendation. pyvmomi: CancelRecommendation(key)",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
            {"name": "recommendation_key", "type": "string", "required": True, "description": "Recommendation key to cancel"},
        ],
        example="cancel_drs_recommendation(cluster_name='Production', recommendation_key='rec-1')",
    ),
    OperationDefinition(
        operation_id="refresh_drs_recommendations",
        name="Refresh DRS Recommendations",
        description="Force refresh of DRS recommendations for a cluster. pyvmomi: RefreshRecommendation()",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
        ],
        example="refresh_drs_recommendations(cluster_name='Production')",
    ),
    OperationDefinition(
        operation_id="get_cluster_resource_usage",
        name="Get Cluster Resource Usage",
        description="Get current resource usage summary for a cluster. pyvmomi: GetResourceUsage()",
        category="monitoring",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
        ],
        example="get_cluster_resource_usage(cluster_name='Production')",
    ),
    OperationDefinition(
        operation_id="find_rules_for_vm",
        name="Find DRS Rules for VM",
        description="Find all DRS rules that apply to a specific VM. pyvmomi: FindRulesForVm(vm)",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of VM"},
        ],
        example="find_rules_for_vm(cluster_name='Production', vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="recommend_hosts_for_vm",
        name="Recommend Hosts for VM",
        description="Get host recommendations for placing a VM. pyvmomi: RecommendHostsForVm(vm, pool)",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of VM"},
        ],
        example="recommend_hosts_for_vm(cluster_name='Production', vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="add_host_to_cluster",
        name="Add Host to Cluster",
        description="Add a standalone host to a cluster. pyvmomi: AddHost_Task(spec, asConnected, resourcePool, license)",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
            {"name": "host_name", "type": "string", "required": True, "description": "Hostname or IP of ESXi host"},
            {"name": "username", "type": "string", "required": True, "description": "ESXi root username"},
            {"name": "password", "type": "string", "required": True, "description": "ESXi root password"},
            {"name": "as_connected", "type": "boolean", "required": False, "description": "Connect host after adding (default: True)"},
        ],
        example="add_host_to_cluster(cluster_name='Production', host_name='esxi-new.example.com', username='root', password='...')",
    ),
    OperationDefinition(
        operation_id="move_host_into_cluster",
        name="Move Host into Cluster",
        description="Move an existing host into a cluster. pyvmomi: MoveHostInto_Task(host, resourcePool)",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Target cluster name"},
            {"name": "host_name", "type": "string", "required": True, "description": "Name of host to move"},
        ],
        example="move_host_into_cluster(cluster_name='Production', host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="get_cluster_ha_status",
        name="Get HA Runtime Info",
        description="Get High Availability runtime information for a cluster. pyvmomi: RetrieveDasAdvancedRuntimeInfo()",
        category="monitoring",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
        ],
        example="get_cluster_ha_status(cluster_name='Production')",
    ),
    
    # =========================================================================
    # DATASTORE OPERATIONS (from pyvmomi introspection)
    # =========================================================================
    
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
    
    # =========================================================================
    # FOLDER OPERATIONS (from pyvmomi introspection)
    # =========================================================================
    
    OperationDefinition(
        operation_id="create_folder",
        name="Create Folder",
        description="Create a new folder in vCenter inventory. pyvmomi: CreateFolder(name)",
        category="inventory",
        parameters=[
            {"name": "parent_folder", "type": "string", "required": True, "description": "Parent folder name"},
            {"name": "folder_name", "type": "string", "required": True, "description": "Name for new folder"},
        ],
        example="create_folder(parent_folder='vm', folder_name='Production-VMs')",
    ),
    OperationDefinition(
        operation_id="rename_folder",
        name="Rename Folder",
        description="Rename a folder in vCenter inventory. pyvmomi: Rename_Task(newName)",
        category="inventory",
        parameters=[
            {"name": "folder_name", "type": "string", "required": True, "description": "Current folder name"},
            {"name": "new_name", "type": "string", "required": True, "description": "New folder name"},
        ],
        example="rename_folder(folder_name='old-folder', new_name='new-folder')",
    ),
    OperationDefinition(
        operation_id="destroy_folder",
        name="Delete Folder",
        description="Delete a folder. Folder must be empty. pyvmomi: Destroy_Task()",
        category="inventory",
        parameters=[
            {"name": "folder_name", "type": "string", "required": True, "description": "Name of folder to delete"},
        ],
        example="destroy_folder(folder_name='empty-folder')",
    ),
    OperationDefinition(
        operation_id="move_into_folder",
        name="Move Entity into Folder",
        description="Move a VM or folder into another folder. pyvmomi: MoveIntoFolder_Task(list)",
        category="inventory",
        parameters=[
            {"name": "target_folder", "type": "string", "required": True, "description": "Target folder name"},
            {"name": "entity_name", "type": "string", "required": True, "description": "Name of entity to move"},
            {"name": "entity_type", "type": "string", "required": True, "description": "Type: vm, folder"},
        ],
        example="move_into_folder(target_folder='Production-VMs', entity_name='web-01', entity_type='vm')",
    ),
    
    # =========================================================================
    # RESOURCE POOL OPERATIONS (from pyvmomi introspection)
    # =========================================================================
    
    OperationDefinition(
        operation_id="create_resource_pool",
        name="Create Resource Pool",
        description="Create a new resource pool. pyvmomi: CreateResourcePool(name, spec)",
        category="compute",
        parameters=[
            {"name": "parent_pool", "type": "string", "required": True, "description": "Parent resource pool name"},
            {"name": "pool_name", "type": "string", "required": True, "description": "Name for new resource pool"},
            {"name": "cpu_reservation", "type": "integer", "required": False, "description": "CPU reservation in MHz"},
            {"name": "memory_reservation_mb", "type": "integer", "required": False, "description": "Memory reservation in MB"},
        ],
        example="create_resource_pool(parent_pool='Resources', pool_name='Dev-Pool')",
    ),
    OperationDefinition(
        operation_id="destroy_resource_pool",
        name="Delete Resource Pool",
        description="Delete a resource pool. VMs will be moved to parent. pyvmomi: Destroy_Task()",
        category="compute",
        parameters=[
            {"name": "pool_name", "type": "string", "required": True, "description": "Name of resource pool to delete"},
        ],
        example="destroy_resource_pool(pool_name='old-pool')",
    ),
    OperationDefinition(
        operation_id="update_resource_pool",
        name="Update Resource Pool Config",
        description="Update resource pool CPU and memory configuration. pyvmomi: UpdateConfig(name, spec)",
        category="compute",
        parameters=[
            {"name": "pool_name", "type": "string", "required": True, "description": "Name of resource pool"},
            {"name": "cpu_reservation", "type": "integer", "required": False, "description": "CPU reservation in MHz"},
            {"name": "cpu_limit", "type": "integer", "required": False, "description": "CPU limit in MHz (-1 for unlimited)"},
            {"name": "memory_reservation_mb", "type": "integer", "required": False, "description": "Memory reservation in MB"},
            {"name": "memory_limit_mb", "type": "integer", "required": False, "description": "Memory limit in MB (-1 for unlimited)"},
        ],
        example="update_resource_pool(pool_name='Dev-Pool', cpu_reservation=2000)",
    ),
    
    # =========================================================================
    # DVS OPERATIONS (from pyvmomi introspection)
    # =========================================================================
    
    OperationDefinition(
        operation_id="create_dvs_portgroup",
        name="Create DVS Port Group",
        description="Create a new distributed port group on a DVS. pyvmomi: CreateDVPortgroup_Task(spec)",
        category="networking",
        parameters=[
            {"name": "dvs_name", "type": "string", "required": True, "description": "Name of distributed switch"},
            {"name": "portgroup_name", "type": "string", "required": True, "description": "Name for new port group"},
            {"name": "vlan_id", "type": "integer", "required": False, "description": "VLAN ID (0 for none)"},
            {"name": "num_ports", "type": "integer", "required": False, "description": "Number of ports (default: 8)"},
        ],
        example="create_dvs_portgroup(dvs_name='DSwitch-Prod', portgroup_name='VLAN-100', vlan_id=100)",
    ),
    OperationDefinition(
        operation_id="destroy_dvs_portgroup",
        name="Delete DVS Port Group",
        description="Delete a distributed port group. Must have no connected VMs. pyvmomi: Destroy_Task()",
        category="networking",
        parameters=[
            {"name": "portgroup_name", "type": "string", "required": True, "description": "Name of port group to delete"},
        ],
        example="destroy_dvs_portgroup(portgroup_name='old-portgroup')",
    ),
    OperationDefinition(
        operation_id="query_used_vlans",
        name="Query Used VLANs",
        description="Get list of VLAN IDs in use on a DVS. pyvmomi: QueryUsedVlanIdInDvs()",
        category="networking",
        parameters=[
            {"name": "dvs_name", "type": "string", "required": True, "description": "Name of distributed switch"},
        ],
        example="query_used_vlans(dvs_name='DSwitch-Prod')",
    ),
    OperationDefinition(
        operation_id="refresh_dvs_port_state",
        name="Refresh DVS Port State",
        description="Refresh the state of ports on a DVS. pyvmomi: RefreshDVPortState(portKeys)",
        category="networking",
        parameters=[
            {"name": "dvs_name", "type": "string", "required": True, "description": "Name of distributed switch"},
        ],
        example="refresh_dvs_port_state(dvs_name='DSwitch-Prod')",
    ),
    
    # =========================================================================
    # ADDITIONAL VM OPERATIONS (Batch 2)
    # =========================================================================
    
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
        operation_id="add_network_adapter",
        name="Add Network Adapter",
        description="Add a new network adapter to a VM.",
        category="networking",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "network_name", "type": "string", "required": True, "description": "Name of network to connect to"},
            {"name": "adapter_type", "type": "string", "required": False, "description": "Adapter type: vmxnet3, e1000e, e1000 (default: vmxnet3)"},
        ],
        example="add_network_adapter(vm_name='web-01', network_name='VM Network', adapter_type='vmxnet3')",
    ),
    OperationDefinition(
        operation_id="remove_network_adapter",
        name="Remove Network Adapter",
        description="Remove a network adapter from a VM.",
        category="networking",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "adapter_label", "type": "string", "required": True, "description": "Label of adapter to remove (e.g., 'Network adapter 2')"},
        ],
        example="remove_network_adapter(vm_name='web-01', adapter_label='Network adapter 2')",
    ),
    OperationDefinition(
        operation_id="change_network",
        name="Change VM Network",
        description="Change the network a VM adapter is connected to.",
        category="networking",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "adapter_label", "type": "string", "required": True, "description": "Label of adapter (e.g., 'Network adapter 1')"},
            {"name": "network_name", "type": "string", "required": True, "description": "Name of new network"},
        ],
        example="change_network(vm_name='web-01', adapter_label='Network adapter 1', network_name='Production-VLAN')",
    ),
    OperationDefinition(
        operation_id="customize_guest",
        name="Customize Guest OS",
        description="Apply customization spec to VM (hostname, IP, domain join). pyvmomi: CustomizeVM_Task(spec)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "hostname", "type": "string", "required": False, "description": "New hostname for VM"},
            {"name": "domain", "type": "string", "required": False, "description": "Domain to join"},
            {"name": "ip_address", "type": "string", "required": False, "description": "Static IP address"},
            {"name": "subnet_mask", "type": "string", "required": False, "description": "Subnet mask"},
            {"name": "gateway", "type": "string", "required": False, "description": "Default gateway"},
            {"name": "dns_servers", "type": "string", "required": False, "description": "Comma-separated DNS servers"},
        ],
        example="customize_guest(vm_name='web-01', hostname='web-server-01', ip_address='10.0.0.100')",
    ),
    OperationDefinition(
        operation_id="create_screenshot",
        name="Create VM Screenshot",
        description="Create a screenshot of the VM's display. pyvmomi: CreateScreenshot_Task()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="create_screenshot(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="set_boot_options",
        name="Set VM Boot Options",
        description="Configure VM boot order and options.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "boot_delay", "type": "integer", "required": False, "description": "Boot delay in milliseconds"},
            {"name": "enter_bios_setup", "type": "boolean", "required": False, "description": "Enter BIOS on next boot"},
            {"name": "boot_retry_enabled", "type": "boolean", "required": False, "description": "Enable boot retry"},
            {"name": "boot_retry_delay", "type": "integer", "required": False, "description": "Boot retry delay in milliseconds"},
        ],
        example="set_boot_options(vm_name='web-01', boot_delay=5000, enter_bios_setup=True)",
    ),
    OperationDefinition(
        operation_id="instant_clone",
        name="Instant Clone VM",
        description="Create an instant clone of a running VM. Very fast cloning. pyvmomi: InstantClone_Task(spec)",
        category="compute",
        parameters=[
            {"name": "source_vm", "type": "string", "required": True, "description": "Name of source VM (must be running)"},
            {"name": "clone_name", "type": "string", "required": True, "description": "Name for the instant clone"},
            {"name": "target_folder", "type": "string", "required": False, "description": "Target folder name"},
        ],
        example="instant_clone(source_vm='template-vm', clone_name='instant-clone-01')",
    ),
    OperationDefinition(
        operation_id="register_vm",
        name="Register VM",
        description="Register an existing VM (vmx file) into vCenter inventory. pyvmomi: RegisterVM_Task(path, name, asTemplate, pool, host)",
        category="inventory",
        parameters=[
            {"name": "vmx_path", "type": "string", "required": True, "description": "Datastore path to VMX file"},
            {"name": "vm_name", "type": "string", "required": False, "description": "Name for the VM (default: from VMX)"},
            {"name": "folder_name", "type": "string", "required": False, "description": "Target folder"},
            {"name": "resource_pool", "type": "string", "required": False, "description": "Target resource pool"},
            {"name": "as_template", "type": "boolean", "required": False, "description": "Register as template (default: False)"},
        ],
        example="register_vm(vmx_path='[datastore1] recovered-vm/recovered-vm.vmx')",
    ),
    OperationDefinition(
        operation_id="reload_vm",
        name="Reload VM Configuration",
        description="Reload VM configuration from datastore. pyvmomi: ReloadVirtualMachineFromPath_Task(configurationPath)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="reload_vm(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="terminate_fault_tolerance",
        name="Turn Off Fault Tolerance",
        description="Disable Fault Tolerance for a VM. pyvmomi: TurnOffFaultToleranceForVM_Task()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="terminate_fault_tolerance(vm_name='critical-vm')",
    ),
    OperationDefinition(
        operation_id="send_nmi",
        name="Send NMI to VM",
        description="Send Non-Maskable Interrupt to VM. Used for debugging. pyvmomi: SendNMI()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="send_nmi(vm_name='debug-vm')",
    ),
    
    # =========================================================================
    # ADDITIONAL HOST OPERATIONS (Batch 2)
    # =========================================================================
    
    OperationDefinition(
        operation_id="query_memory_overhead",
        name="Query Memory Overhead",
        description="Query memory overhead for a VM on a host. pyvmomi: QueryMemoryOverheadEx(vmConfigInfo)",
        category="monitoring",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of VM to check overhead for"},
        ],
        example="query_memory_overhead(host_name='esxi-01.example.com', vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="update_host_flags",
        name="Update Host Flags",
        description="Update host flags (lockdown mode settings). pyvmomi: UpdateFlags(flagInfo)",
        category="compute",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "enable_management", "type": "boolean", "required": False, "description": "Enable management services"},
        ],
        example="update_host_flags(host_name='esxi-01.example.com', enable_management=True)",
    ),
    OperationDefinition(
        operation_id="query_tpm_attestation",
        name="Query TPM Attestation",
        description="Query TPM attestation report for host security. pyvmomi: QueryTpmAttestationReport()",
        category="monitoring",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="query_tpm_attestation(host_name='esxi-01.example.com')",
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
        operation_id="refresh_host_services",
        name="Refresh Host Services",
        description="Refresh the list of services running on the host.",
        category="system",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="refresh_host_services(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="get_host_services",
        name="Get Host Services",
        description="Get list of services on ESXi host with their status.",
        category="system",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="get_host_services(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="start_host_service",
        name="Start Host Service",
        description="Start a service on ESXi host (e.g., SSH, NTP).",
        category="system",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "service_key", "type": "string", "required": True, "description": "Service key (e.g., 'TSM-SSH', 'ntpd')"},
        ],
        example="start_host_service(host_name='esxi-01.example.com', service_key='TSM-SSH')",
    ),
    OperationDefinition(
        operation_id="stop_host_service",
        name="Stop Host Service",
        description="Stop a service on ESXi host.",
        category="system",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "service_key", "type": "string", "required": True, "description": "Service key (e.g., 'TSM-SSH', 'ntpd')"},
        ],
        example="stop_host_service(host_name='esxi-01.example.com', service_key='TSM-SSH')",
    ),
    OperationDefinition(
        operation_id="restart_host_service",
        name="Restart Host Service",
        description="Restart a service on ESXi host.",
        category="system",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "service_key", "type": "string", "required": True, "description": "Service key (e.g., 'TSM-SSH', 'ntpd')"},
        ],
        example="restart_host_service(host_name='esxi-01.example.com', service_key='TSM-SSH')",
    ),
    OperationDefinition(
        operation_id="get_host_firewall_rules",
        name="Get Host Firewall Rules",
        description="Get firewall rules configured on ESXi host.",
        category="networking",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="get_host_firewall_rules(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="enable_firewall_ruleset",
        name="Enable Firewall Ruleset",
        description="Enable a firewall ruleset on ESXi host.",
        category="networking",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "ruleset_key", "type": "string", "required": True, "description": "Ruleset key (e.g., 'sshServer', 'nfsClient')"},
        ],
        example="enable_firewall_ruleset(host_name='esxi-01.example.com', ruleset_key='sshServer')",
    ),
    OperationDefinition(
        operation_id="disable_firewall_ruleset",
        name="Disable Firewall Ruleset",
        description="Disable a firewall ruleset on ESXi host.",
        category="networking",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "ruleset_key", "type": "string", "required": True, "description": "Ruleset key (e.g., 'sshServer', 'nfsClient')"},
        ],
        example="disable_firewall_ruleset(host_name='esxi-01.example.com', ruleset_key='sshServer')",
    ),
    
    # =========================================================================
    # ADDITIONAL CLUSTER OPERATIONS (Batch 2)
    # =========================================================================
    
    OperationDefinition(
        operation_id="reconfigure_cluster",
        name="Reconfigure Cluster",
        description="Reconfigure cluster settings (DRS, HA, etc). pyvmomi: ReconfigureCluster_Task(spec, modify)",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
            {"name": "drs_enabled", "type": "boolean", "required": False, "description": "Enable/disable DRS"},
            {"name": "drs_automation_level", "type": "string", "required": False, "description": "DRS level: manual, partiallyAutomated, fullyAutomated"},
            {"name": "ha_enabled", "type": "boolean", "required": False, "description": "Enable/disable HA"},
            {"name": "ha_host_monitoring", "type": "string", "required": False, "description": "Host monitoring: enabled, disabled"},
        ],
        example="reconfigure_cluster(cluster_name='Production', drs_enabled=True, drs_automation_level='fullyAutomated')",
    ),
    OperationDefinition(
        operation_id="cluster_enter_maintenance_mode",
        name="Cluster Enter Maintenance Mode",
        description="Put entire cluster into maintenance mode. pyvmomi: ClusterEnterMaintenanceMode()",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
        ],
        example="cluster_enter_maintenance_mode(cluster_name='Production')",
    ),
    OperationDefinition(
        operation_id="place_vm",
        name="Place VM (Compute Recommendation)",
        description="Get placement recommendations for a VM. pyvmomi: PlaceVm(placementSpec)",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of VM to place"},
        ],
        example="place_vm(cluster_name='Production', vm_name='new-vm')",
    ),
    OperationDefinition(
        operation_id="destroy_cluster",
        name="Destroy Cluster",
        description="Delete a cluster. Hosts must be removed first. pyvmomi: Destroy_Task()",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster to destroy"},
        ],
        example="destroy_cluster(cluster_name='old-cluster')",
    ),
    OperationDefinition(
        operation_id="rename_cluster",
        name="Rename Cluster",
        description="Rename a cluster. pyvmomi: Rename_Task(newName)",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Current cluster name"},
            {"name": "new_name", "type": "string", "required": True, "description": "New cluster name"},
        ],
        example="rename_cluster(cluster_name='old-name', new_name='Production-Cluster')",
    ),
    OperationDefinition(
        operation_id="get_evc_mode",
        name="Get EVC Mode",
        description="Get Enhanced vMotion Compatibility mode for cluster.",
        category="compute",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
        ],
        example="get_evc_mode(cluster_name='Production')",
    ),
    
    # =========================================================================
    # ADDITIONAL DATASTORE OPERATIONS (Batch 2)
    # =========================================================================
    
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
    
    # =========================================================================
    # VM POWER & LIFECYCLE OPERATIONS
    # =========================================================================
    
    OperationDefinition(
        operation_id="create_vm",
        name="Create Virtual Machine",
        description="Create a new virtual machine from scratch.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name for the new VM"},
            {"name": "num_cpus", "type": "integer", "required": True, "description": "Number of vCPUs"},
            {"name": "memory_mb", "type": "integer", "required": True, "description": "Memory in MB"},
            {"name": "disk_size_gb", "type": "integer", "required": True, "description": "Primary disk size in GB"},
            {"name": "guest_os_id", "type": "string", "required": True, "description": "Guest OS ID (e.g., 'ubuntu64Guest', 'windows9Server64Guest')"},
            {"name": "datastore_name", "type": "string", "required": True, "description": "Target datastore"},
            {"name": "folder_name", "type": "string", "required": False, "description": "Target folder"},
            {"name": "resource_pool", "type": "string", "required": False, "description": "Target resource pool"},
            {"name": "network_name", "type": "string", "required": False, "description": "Network to connect to"},
        ],
        example="create_vm(vm_name='new-vm', num_cpus=2, memory_mb=4096, disk_size_gb=40, guest_os_id='ubuntu64Guest', datastore_name='datastore1')",
    ),
    OperationDefinition(
        operation_id="deploy_ovf",
        name="Deploy OVF/OVA Template",
        description="Deploy a VM from an OVF or OVA file.",
        category="compute",
        parameters=[
            {"name": "ovf_url", "type": "string", "required": True, "description": "URL or datastore path to OVF/OVA"},
            {"name": "vm_name", "type": "string", "required": True, "description": "Name for the new VM"},
            {"name": "datastore_name", "type": "string", "required": True, "description": "Target datastore"},
            {"name": "folder_name", "type": "string", "required": False, "description": "Target folder"},
            {"name": "resource_pool", "type": "string", "required": False, "description": "Target resource pool"},
            {"name": "network_mappings", "type": "string", "required": False, "description": "Network mappings (JSON: {\"OVF Network\": \"VM Network\"})"},
        ],
        example="deploy_ovf(ovf_url='https://example.com/template.ova', vm_name='deployed-vm', datastore_name='datastore1')",
    ),
    
    # =========================================================================
    # ADDITIONAL PERFORMANCE & MONITORING
    # =========================================================================
    
    OperationDefinition(
        operation_id="get_cluster_performance",
        name="Get Cluster Performance",
        description="Get aggregated performance metrics for a cluster.",
        category="monitoring",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
            {"name": "interval", "type": "string", "required": False, "description": "Interval: realtime, past_day, past_week"},
        ],
        example="get_cluster_performance(cluster_name='Production', interval='past_day')",
    ),
    OperationDefinition(
        operation_id="get_datastore_performance",
        name="Get Datastore Performance",
        description="Get performance metrics for a datastore (IOPS, latency, throughput).",
        category="monitoring",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of datastore"},
            {"name": "interval", "type": "string", "required": False, "description": "Interval: realtime, past_day, past_week"},
        ],
        example="get_datastore_performance(datastore_name='datastore1', interval='realtime')",
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
    
    # =========================================================================
    # VM GUEST OPERATIONS (Batch 3)
    # =========================================================================
    
    OperationDefinition(
        operation_id="get_vm_guest_info",
        name="Get VM Guest Info",
        description="Get detailed guest OS information including IP, hostname, guest tools.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="get_vm_guest_info(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="list_guest_processes",
        name="List Guest Processes",
        description="List running processes inside guest OS. Requires VMware Tools.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "username", "type": "string", "required": True, "description": "Guest OS username"},
            {"name": "password", "type": "string", "required": True, "description": "Guest OS password"},
        ],
        example="list_guest_processes(vm_name='web-01', username='admin', password='...')",
    ),
    OperationDefinition(
        operation_id="run_program_in_guest",
        name="Run Program in Guest",
        description="Execute a program inside guest OS. Requires VMware Tools.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "username", "type": "string", "required": True, "description": "Guest OS username"},
            {"name": "password", "type": "string", "required": True, "description": "Guest OS password"},
            {"name": "program_path", "type": "string", "required": True, "description": "Full path to program"},
            {"name": "arguments", "type": "string", "required": False, "description": "Program arguments"},
            {"name": "working_directory", "type": "string", "required": False, "description": "Working directory"},
        ],
        example="run_program_in_guest(vm_name='web-01', username='admin', password='...', program_path='/usr/bin/ls', arguments='-la')",
    ),
    OperationDefinition(
        operation_id="upload_file_to_guest",
        name="Upload File to Guest",
        description="Upload a file to the guest OS. Requires VMware Tools.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "username", "type": "string", "required": True, "description": "Guest OS username"},
            {"name": "password", "type": "string", "required": True, "description": "Guest OS password"},
            {"name": "guest_path", "type": "string", "required": True, "description": "Destination path in guest"},
            {"name": "file_content", "type": "string", "required": True, "description": "File content (base64 for binary)"},
            {"name": "overwrite", "type": "boolean", "required": False, "description": "Overwrite if exists (default: True)"},
        ],
        example="upload_file_to_guest(vm_name='web-01', username='admin', password='...', guest_path='/tmp/test.txt', file_content='Hello World')",
    ),
    OperationDefinition(
        operation_id="download_file_from_guest",
        name="Download File from Guest",
        description="Download a file from the guest OS. Requires VMware Tools.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "username", "type": "string", "required": True, "description": "Guest OS username"},
            {"name": "password", "type": "string", "required": True, "description": "Guest OS password"},
            {"name": "guest_path", "type": "string", "required": True, "description": "Path to file in guest"},
        ],
        example="download_file_from_guest(vm_name='web-01', username='admin', password='...', guest_path='/var/log/app.log')",
    ),
    OperationDefinition(
        operation_id="create_directory_in_guest",
        name="Create Directory in Guest",
        description="Create a directory inside guest OS. Requires VMware Tools.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "username", "type": "string", "required": True, "description": "Guest OS username"},
            {"name": "password", "type": "string", "required": True, "description": "Guest OS password"},
            {"name": "directory_path", "type": "string", "required": True, "description": "Directory path to create"},
            {"name": "create_parents", "type": "boolean", "required": False, "description": "Create parent directories (default: True)"},
        ],
        example="create_directory_in_guest(vm_name='web-01', username='admin', password='...', directory_path='/opt/myapp')",
    ),
    OperationDefinition(
        operation_id="delete_file_in_guest",
        name="Delete File in Guest",
        description="Delete a file from guest OS. Requires VMware Tools.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "username", "type": "string", "required": True, "description": "Guest OS username"},
            {"name": "password", "type": "string", "required": True, "description": "Guest OS password"},
            {"name": "file_path", "type": "string", "required": True, "description": "Path to file to delete"},
        ],
        example="delete_file_in_guest(vm_name='web-01', username='admin', password='...', file_path='/tmp/old-file.txt')",
    ),
    
    # =========================================================================
    # VM CUSTOM VALUE OPERATIONS
    # =========================================================================
    
    OperationDefinition(
        operation_id="set_custom_value",
        name="Set Custom Attribute Value",
        description="Set a custom attribute value on a VM. pyvmomi: SetCustomValue(key, value)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "attribute_key", "type": "string", "required": True, "description": "Custom attribute key"},
            {"name": "attribute_value", "type": "string", "required": True, "description": "Custom attribute value"},
        ],
        example="set_custom_value(vm_name='web-01', attribute_key='Environment', attribute_value='Production')",
    ),
    OperationDefinition(
        operation_id="get_custom_values",
        name="Get Custom Attribute Values",
        description="Get all custom attribute values for a VM.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="get_custom_values(vm_name='web-01')",
    ),
    
    # =========================================================================
    # VM SCREEN OPERATIONS
    # =========================================================================
    
    OperationDefinition(
        operation_id="set_screen_resolution",
        name="Set VM Screen Resolution",
        description="Set the screen resolution for a VM. pyvmomi: SetScreenResolution(width, height)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "width", "type": "integer", "required": True, "description": "Screen width in pixels"},
            {"name": "height", "type": "integer", "required": True, "description": "Screen height in pixels"},
        ],
        example="set_screen_resolution(vm_name='vdi-01', width=1920, height=1080)",
    ),
    
    # =========================================================================
    # LICENSING OPERATIONS
    # =========================================================================
    
    OperationDefinition(
        operation_id="get_license_info",
        name="Get License Information",
        description="Get vCenter license information and usage.",
        category="system",
        parameters=[],
        example="get_license_info()",
    ),
    OperationDefinition(
        operation_id="get_licensed_features",
        name="Get Licensed Features",
        description="Get list of licensed features in vCenter.",
        category="system",
        parameters=[],
        example="get_licensed_features()",
    ),
    
    # =========================================================================
    # CONTENT LIBRARY OPERATIONS
    # =========================================================================
    
    OperationDefinition(
        operation_id="list_content_libraries",
        name="List Content Libraries",
        description="List all content libraries in vCenter.",
        category="inventory",
        parameters=[],
        example="list_content_libraries()",
    ),
    OperationDefinition(
        operation_id="get_content_library_items",
        name="Get Content Library Items",
        description="List items in a content library.",
        category="inventory",
        parameters=[
            {"name": "library_name", "type": "string", "required": True, "description": "Name of content library"},
        ],
        example="get_content_library_items(library_name='Templates')",
    ),
    OperationDefinition(
        operation_id="deploy_library_item",
        name="Deploy VM from Content Library",
        description="Deploy a VM from a content library template.",
        category="compute",
        parameters=[
            {"name": "library_name", "type": "string", "required": True, "description": "Name of content library"},
            {"name": "item_name", "type": "string", "required": True, "description": "Name of library item"},
            {"name": "vm_name", "type": "string", "required": True, "description": "Name for new VM"},
            {"name": "datastore_name", "type": "string", "required": True, "description": "Target datastore"},
            {"name": "folder_name", "type": "string", "required": False, "description": "Target folder"},
            {"name": "resource_pool", "type": "string", "required": False, "description": "Target resource pool"},
        ],
        example="deploy_library_item(library_name='Templates', item_name='ubuntu-template', vm_name='new-vm', datastore_name='datastore1')",
    ),
    
    # =========================================================================
    # TAG OPERATIONS
    # =========================================================================
    
    OperationDefinition(
        operation_id="list_tags",
        name="List Tags",
        description="List all tags defined in vCenter.",
        category="inventory",
        parameters=[],
        example="list_tags()",
    ),
    OperationDefinition(
        operation_id="list_tag_categories",
        name="List Tag Categories",
        description="List all tag categories in vCenter.",
        category="inventory",
        parameters=[],
        example="list_tag_categories()",
    ),
    OperationDefinition(
        operation_id="get_vm_tags",
        name="Get VM Tags",
        description="Get all tags assigned to a VM.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="get_vm_tags(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="assign_tag_to_vm",
        name="Assign Tag to VM",
        description="Assign a tag to a VM.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "tag_name", "type": "string", "required": True, "description": "Name of tag to assign"},
        ],
        example="assign_tag_to_vm(vm_name='web-01', tag_name='Production')",
    ),
    OperationDefinition(
        operation_id="remove_tag_from_vm",
        name="Remove Tag from VM",
        description="Remove a tag from a VM.",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "tag_name", "type": "string", "required": True, "description": "Name of tag to remove"},
        ],
        example="remove_tag_from_vm(vm_name='web-01', tag_name='Development')",
    ),
    
    # =========================================================================
    # REVERT/RESET OPERATIONS
    # =========================================================================
    
    OperationDefinition(
        operation_id="revert_to_current_snapshot",
        name="Revert to Current Snapshot",
        description="Revert VM to its most recent snapshot. pyvmomi: RevertToCurrentSnapshot_Task(host, suppressPowerOn)",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "suppress_power_on", "type": "boolean", "required": False, "description": "Prevent power on after revert (default: False)"},
        ],
        example="revert_to_current_snapshot(vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="reset_guest_information",
        name="Reset Guest Information",
        description="Reset guest information to default. pyvmomi: ResetGuestInformation()",
        category="compute",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
        ],
        example="reset_guest_information(vm_name='web-01')",
    ),
    
    # =========================================================================
    # ADDITIONAL INVENTORY OPERATIONS
    # =========================================================================
    
    OperationDefinition(
        operation_id="list_templates",
        name="List VM Templates",
        description="List all VM templates in vCenter.",
        category="inventory",
        parameters=[],
        example="list_templates()",
    ),
    OperationDefinition(
        operation_id="get_template",
        name="Get Template Details",
        description="Get details of a VM template.",
        category="inventory",
        parameters=[
            {"name": "template_name", "type": "string", "required": True, "description": "Name of the template"},
        ],
        example="get_template(template_name='ubuntu-template')",
    ),
    OperationDefinition(
        operation_id="search_inventory",
        name="Search Inventory",
        description="Search vCenter inventory by name pattern.",
        category="inventory",
        parameters=[
            {"name": "name_pattern", "type": "string", "required": True, "description": "Name pattern to search (supports wildcards)"},
            {"name": "entity_type", "type": "string", "required": False, "description": "Entity type: vm, host, datastore, network, cluster"},
        ],
        example="search_inventory(name_pattern='web-*', entity_type='vm')",
    ),
    OperationDefinition(
        operation_id="get_inventory_path",
        name="Get Inventory Path",
        description="Get the full inventory path of an entity.",
        category="inventory",
        parameters=[
            {"name": "entity_name", "type": "string", "required": True, "description": "Name of entity"},
            {"name": "entity_type", "type": "string", "required": True, "description": "Entity type: vm, host, datastore, folder, cluster"},
        ],
        example="get_inventory_path(entity_name='web-01', entity_type='vm')",
    ),
]

