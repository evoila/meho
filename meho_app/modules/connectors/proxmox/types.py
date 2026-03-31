# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox Type Definitions (TASK-100)

These help the agent understand what entities exist in Proxmox
and can be discovered via search_types.
"""

from meho_app.modules.connectors.base import TypeDefinition

PROXMOX_TYPES = [
    TypeDefinition(
        type_name="Node",
        description="A Proxmox VE host server that runs VMs and containers. Equivalent to ESXi host in VMware.",
        category="nodes",
        properties=[
            {"name": "name", "type": "string", "description": "Node hostname"},
            {"name": "status", "type": "string", "description": "Node status: online, offline"},
            {
                "name": "cpu_usage_percent",
                "type": "float",
                "description": "Current CPU usage percentage",
            },
            {"name": "memory_used_mb", "type": "integer", "description": "Memory used in MB"},
            {"name": "memory_total_mb", "type": "integer", "description": "Total memory in MB"},
            {"name": "disk_used_gb", "type": "float", "description": "Local disk space used in GB"},
            {
                "name": "disk_total_gb",
                "type": "float",
                "description": "Total local disk space in GB",
            },
            {"name": "uptime", "type": "string", "description": "Node uptime"},
            {"name": "pve_version", "type": "string", "description": "Proxmox VE version"},
        ],
    ),
    TypeDefinition(
        type_name="VM",
        description="A QEMU/KVM virtual machine running on Proxmox. Can be powered on/off, snapshotted, migrated.",
        category="compute",
        properties=[
            {"name": "vmid", "type": "integer", "description": "Unique VM ID"},
            {"name": "name", "type": "string", "description": "VM display name"},
            {"name": "node", "type": "string", "description": "Node hosting the VM"},
            {
                "name": "status",
                "type": "string",
                "description": "Power state: running, stopped, paused, suspended",
            },
            {"name": "cpu_count", "type": "integer", "description": "Number of virtual CPUs"},
            {
                "name": "cpu_usage_percent",
                "type": "float",
                "description": "Current CPU usage percentage",
            },
            {"name": "memory_mb", "type": "integer", "description": "Total memory in MB"},
            {"name": "memory_used_mb", "type": "integer", "description": "Memory used in MB"},
            {"name": "disk_size_gb", "type": "float", "description": "Total disk size in GB"},
            {"name": "uptime", "type": "string", "description": "VM uptime"},
            {"name": "template", "type": "boolean", "description": "Whether this is a template"},
            {"name": "tags", "type": "array", "description": "VM tags for organization"},
        ],
    ),
    TypeDefinition(
        type_name="Container",
        description="An LXC container running on Proxmox. Lightweight alternative to VMs with shared kernel.",
        category="compute",
        properties=[
            {"name": "vmid", "type": "integer", "description": "Unique container ID"},
            {"name": "name", "type": "string", "description": "Container hostname"},
            {"name": "node", "type": "string", "description": "Node hosting the container"},
            {"name": "status", "type": "string", "description": "State: running, stopped"},
            {"name": "cpu_count", "type": "integer", "description": "CPU cores allocated"},
            {
                "name": "cpu_usage_percent",
                "type": "float",
                "description": "Current CPU usage percentage",
            },
            {"name": "memory_mb", "type": "integer", "description": "Total memory in MB"},
            {"name": "memory_used_mb", "type": "integer", "description": "Memory used in MB"},
            {"name": "swap_mb", "type": "integer", "description": "Swap space in MB"},
            {"name": "disk_size_gb", "type": "float", "description": "Root filesystem size in GB"},
            {"name": "template", "type": "boolean", "description": "Whether this is a template"},
            {
                "name": "unprivileged",
                "type": "boolean",
                "description": "Unprivileged container (more secure)",
            },
        ],
    ),
    TypeDefinition(
        type_name="Storage",
        description="A storage pool in Proxmox for VM disks, ISOs, backups, and templates.",
        category="storage",
        properties=[
            {"name": "storage", "type": "string", "description": "Storage pool name"},
            {
                "name": "type",
                "type": "string",
                "description": "Storage type: dir, lvm, lvmthin, nfs, cifs, zfspool, etc.",
            },
            {
                "name": "content",
                "type": "array",
                "description": "Content types: images, rootdir, iso, vztmpl, backup",
            },
            {"name": "total_gb", "type": "float", "description": "Total capacity in GB"},
            {"name": "used_gb", "type": "float", "description": "Used space in GB"},
            {"name": "available_gb", "type": "float", "description": "Available space in GB"},
            {"name": "usage_percent", "type": "float", "description": "Usage percentage"},
            {
                "name": "shared",
                "type": "boolean",
                "description": "Shared storage (accessible from all nodes)",
            },
            {
                "name": "active",
                "type": "boolean",
                "description": "Storage is active and accessible",
            },
        ],
    ),
    TypeDefinition(
        type_name="Snapshot",
        description="A point-in-time capture of a VM or container state.",
        category="compute",
        properties=[
            {"name": "name", "type": "string", "description": "Snapshot name"},
            {"name": "description", "type": "string", "description": "Snapshot description"},
            {
                "name": "snaptime",
                "type": "integer",
                "description": "Snapshot timestamp (Unix epoch)",
            },
            {"name": "parent", "type": "string", "description": "Parent snapshot name"},
            {"name": "vmstate", "type": "boolean", "description": "Includes RAM state (VM only)"},
        ],
    ),
    TypeDefinition(
        type_name="Cluster",
        description="A Proxmox VE cluster consisting of multiple nodes with shared configuration.",
        category="nodes",
        properties=[
            {"name": "cluster_name", "type": "string", "description": "Cluster name"},
            {"name": "quorate", "type": "boolean", "description": "Cluster has quorum"},
            {"name": "node_count", "type": "integer", "description": "Number of nodes in cluster"},
            {"name": "online_count", "type": "integer", "description": "Number of online nodes"},
            {"name": "version", "type": "integer", "description": "Cluster configuration version"},
        ],
    ),
    TypeDefinition(
        type_name="Task",
        description="An asynchronous operation in Proxmox (e.g., VM start, migration, backup).",
        category="system",
        properties=[
            {"name": "upid", "type": "string", "description": "Unique process ID"},
            {
                "name": "type",
                "type": "string",
                "description": "Task type (qmstart, qmstop, vzdump, etc.)",
            },
            {"name": "status", "type": "string", "description": "Task status"},
            {"name": "node", "type": "string", "description": "Node running the task"},
            {"name": "user", "type": "string", "description": "User who started the task"},
            {"name": "starttime", "type": "integer", "description": "Start timestamp"},
            {"name": "exitstatus", "type": "string", "description": "Exit status (OK, ERROR)"},
        ],
    ),
]
