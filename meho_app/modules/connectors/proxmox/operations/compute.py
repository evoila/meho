# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox Compute Operations - VMs and Containers

Defines operations for QEMU VMs and LXC containers.
"""

from meho_app.modules.connectors.base import OperationDefinition

CONTAINER_ID = "Container ID"
NODE_NAME = "Node name"
VM_ID = "VM ID"

COMPUTE_OPERATIONS = [
    # =========================================================================
    # VM (QEMU) OPERATIONS
    # =========================================================================
    OperationDefinition(
        operation_id="list_vms",
        name="List Virtual Machines",
        description="Get all QEMU VMs across all nodes in the Proxmox cluster. Returns name, status, CPU/memory usage, disk size, uptime, and node location for each VM.",
        category="compute",
        parameters=[
            {
                "name": "node",
                "type": "string",
                "required": False,
                "description": "Filter to specific node (optional)",
            },
        ],
        example="list_vms()",
        response_entity_type="VirtualMachine",
        response_identifier_field="vmid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_vm",
        name="Get VM Details",
        description="Get detailed information about a specific VM including configuration, resource usage, and status.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {
                "name": "node",
                "type": "string",
                "required": False,
                "description": "Node name (auto-detected if not provided)",
            },
        ],
        example="get_vm(vmid=100)",
        response_entity_type="VirtualMachine",
        response_identifier_field="vmid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_vm_status",
        name="Get VM Status",
        description="Get current status and resource metrics for a VM.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="get_vm_status(vmid=100)",
        response_entity_type="VirtualMachine",
        response_identifier_field="vmid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="start_vm",
        name="Start VM",
        description="Power on a virtual machine.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="start_vm(vmid=100)",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="stop_vm",
        name="Stop VM",
        description="Stop a virtual machine (hard power off).",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="stop_vm(vmid=100)",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="shutdown_vm",
        name="Shutdown VM",
        description="Gracefully shutdown a virtual machine via ACPI. Requires QEMU Guest Agent for best results.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
            {
                "name": "timeout",
                "type": "integer",
                "required": False,
                "description": "Timeout in seconds (default: 60)",
            },
        ],
        example="shutdown_vm(vmid=100)",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="restart_vm",
        name="Restart VM",
        description="Reboot a virtual machine. Uses ACPI for graceful reboot if possible.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="restart_vm(vmid=100)",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="reset_vm",
        name="Reset VM",
        description="Hard reset a virtual machine (like pressing reset button).",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="reset_vm(vmid=100)",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="suspend_vm",
        name="Suspend VM",
        description="Suspend a virtual machine to disk (hibernate).",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="suspend_vm(vmid=100)",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="resume_vm",
        name="Resume VM",
        description="Resume a suspended virtual machine.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="resume_vm(vmid=100)",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="get_vm_config",
        name="Get VM Configuration",
        description="Get the full configuration of a virtual machine including CPU, memory, disks, and network settings.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="get_vm_config(vmid=100)",
        response_entity_type="VirtualMachine",
        response_identifier_field="vmid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="clone_vm",
        name="Clone VM",
        description="Create a clone of a virtual machine.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": "Source VM ID"},
            {
                "name": "newid",
                "type": "integer",
                "required": True,
                "description": "New VM ID for the clone",
            },
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
            {
                "name": "name",
                "type": "string",
                "required": False,
                "description": "Name for the new VM",
            },
            {
                "name": "full",
                "type": "boolean",
                "required": False,
                "description": "Full clone (not linked, default: True)",
            },
        ],
        example="clone_vm(vmid=100, newid=101, name='web-server-clone')",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="migrate_vm",
        name="Migrate VM",
        description="Migrate a virtual machine to another node.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {"name": "node", "type": "string", "required": True, "description": "Source node name"},
            {
                "name": "target",
                "type": "string",
                "required": True,
                "description": "Target node name",
            },
            {
                "name": "online",
                "type": "boolean",
                "required": False,
                "description": "Live migration (default: True)",
            },
        ],
        example="migrate_vm(vmid=100, node='pve1', target='pve2')",
        # Action operations return status
    ),
    # VM SNAPSHOT OPERATIONS
    OperationDefinition(
        operation_id="list_vm_snapshots",
        name="List VM Snapshots",
        description="Get all snapshots for a virtual machine.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="list_vm_snapshots(vmid=100)",
        response_entity_type="Snapshot",
        response_identifier_field="name",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="create_vm_snapshot",
        name="Create VM Snapshot",
        description="Create a snapshot of a virtual machine.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {
                "name": "snapname",
                "type": "string",
                "required": True,
                "description": "Snapshot name",
            },
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
            {
                "name": "description",
                "type": "string",
                "required": False,
                "description": "Snapshot description",
            },
            {
                "name": "vmstate",
                "type": "boolean",
                "required": False,
                "description": "Include RAM state (default: False)",
            },
        ],
        example="create_vm_snapshot(vmid=100, snapname='before-upgrade')",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="delete_vm_snapshot",
        name="Delete VM Snapshot",
        description="Delete a snapshot from a virtual machine.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {
                "name": "snapname",
                "type": "string",
                "required": True,
                "description": "Snapshot name to delete",
            },
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="delete_vm_snapshot(vmid=100, snapname='old-snapshot')",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="rollback_vm_snapshot",
        name="Rollback VM Snapshot",
        description="Rollback a virtual machine to a previous snapshot.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": VM_ID},
            {
                "name": "snapname",
                "type": "string",
                "required": True,
                "description": "Snapshot name to rollback to",
            },
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="rollback_vm_snapshot(vmid=100, snapname='before-upgrade')",
        # Action operations return status
    ),
    # =========================================================================
    # CONTAINER (LXC) OPERATIONS
    # =========================================================================
    OperationDefinition(
        operation_id="list_containers",
        name="List Containers",
        description="Get all LXC containers across all nodes in the Proxmox cluster. Returns name, status, CPU/memory usage, and node location for each container.",
        category="compute",
        parameters=[
            {
                "name": "node",
                "type": "string",
                "required": False,
                "description": "Filter to specific node (optional)",
            },
        ],
        example="list_containers()",
        response_entity_type="Container",
        response_identifier_field="vmid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_container",
        name="Get Container Details",
        description="Get detailed information about a specific LXC container including configuration and resource usage.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": CONTAINER_ID},
            {
                "name": "node",
                "type": "string",
                "required": False,
                "description": "Node name (auto-detected if not provided)",
            },
        ],
        example="get_container(vmid=200)",
        response_entity_type="Container",
        response_identifier_field="vmid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_container_status",
        name="Get Container Status",
        description="Get current status and resource metrics for a container.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": CONTAINER_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="get_container_status(vmid=200)",
        response_entity_type="Container",
        response_identifier_field="vmid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="start_container",
        name="Start Container",
        description="Start an LXC container.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": CONTAINER_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="start_container(vmid=200)",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="stop_container",
        name="Stop Container",
        description="Stop an LXC container (immediate stop).",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": CONTAINER_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="stop_container(vmid=200)",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="shutdown_container",
        name="Shutdown Container",
        description="Gracefully shutdown an LXC container.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": CONTAINER_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
            {
                "name": "timeout",
                "type": "integer",
                "required": False,
                "description": "Timeout in seconds (default: 60)",
            },
        ],
        example="shutdown_container(vmid=200)",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="restart_container",
        name="Restart Container",
        description="Restart an LXC container.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": CONTAINER_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="restart_container(vmid=200)",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="get_container_config",
        name="Get Container Configuration",
        description="Get the full configuration of an LXC container.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": CONTAINER_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="get_container_config(vmid=200)",
        response_entity_type="Container",
        response_identifier_field="vmid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="clone_container",
        name="Clone Container",
        description="Create a clone of an LXC container.",
        category="compute",
        parameters=[
            {
                "name": "vmid",
                "type": "integer",
                "required": True,
                "description": "Source container ID",
            },
            {
                "name": "newid",
                "type": "integer",
                "required": True,
                "description": "New container ID for the clone",
            },
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
            {
                "name": "hostname",
                "type": "string",
                "required": False,
                "description": "Hostname for the new container",
            },
            {
                "name": "full",
                "type": "boolean",
                "required": False,
                "description": "Full clone (not linked, default: True)",
            },
        ],
        example="clone_container(vmid=200, newid=201, hostname='app-clone')",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="migrate_container",
        name="Migrate Container",
        description="Migrate an LXC container to another node.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": CONTAINER_ID},
            {"name": "node", "type": "string", "required": True, "description": "Source node name"},
            {
                "name": "target",
                "type": "string",
                "required": True,
                "description": "Target node name",
            },
            {
                "name": "online",
                "type": "boolean",
                "required": False,
                "description": "Online migration (default: True)",
            },
        ],
        example="migrate_container(vmid=200, node='pve1', target='pve2')",
        # Action operations return status
    ),
    # CONTAINER SNAPSHOT OPERATIONS
    OperationDefinition(
        operation_id="list_container_snapshots",
        name="List Container Snapshots",
        description="Get all snapshots for an LXC container.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": CONTAINER_ID},
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="list_container_snapshots(vmid=200)",
        response_entity_type="Snapshot",
        response_identifier_field="name",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="create_container_snapshot",
        name="Create Container Snapshot",
        description="Create a snapshot of an LXC container.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": CONTAINER_ID},
            {
                "name": "snapname",
                "type": "string",
                "required": True,
                "description": "Snapshot name",
            },
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
            {
                "name": "description",
                "type": "string",
                "required": False,
                "description": "Snapshot description",
            },
        ],
        example="create_container_snapshot(vmid=200, snapname='before-update')",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="delete_container_snapshot",
        name="Delete Container Snapshot",
        description="Delete a snapshot from an LXC container.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": CONTAINER_ID},
            {
                "name": "snapname",
                "type": "string",
                "required": True,
                "description": "Snapshot name to delete",
            },
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="delete_container_snapshot(vmid=200, snapname='old-snapshot')",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="rollback_container_snapshot",
        name="Rollback Container Snapshot",
        description="Rollback an LXC container to a previous snapshot.",
        category="compute",
        parameters=[
            {"name": "vmid", "type": "integer", "required": True, "description": CONTAINER_ID},
            {
                "name": "snapname",
                "type": "string",
                "required": True,
                "description": "Snapshot name to rollback to",
            },
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="rollback_container_snapshot(vmid=200, snapname='before-update')",
        # Action operations return status
    ),
]
