# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Compute Engine Operation Definitions (TASK-102)

Operations for managing Compute Engine instances, disks, and snapshots.
"""

from meho_app.modules.connectors.base import OperationDefinition

ZONE_OF_THE_INSTANCE = "Zone of the instance"

COMPUTE_OPERATIONS = [
    # Instance Operations
    OperationDefinition(
        operation_id="list_instances",
        name="List Compute Engine Instances",
        description="List all VM instances in the project. Returns instance details including name, zone, machine type, status, IP addresses, and labels. Can filter by zone.",
        category="compute",
        parameters=[
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": "Zone to list instances from (default: all zones)",
            },
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": "Filter expression (e.g., 'status=RUNNING')",
            },
        ],
        example="list_instances(zone='us-central1-a')",
        response_entity_type="Instance",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_instance",
        name="Get Instance Details",
        description="Get detailed information about a specific Compute Engine instance including configuration, network interfaces, disks, and current status.",
        category="compute",
        parameters=[
            {
                "name": "instance_name",
                "type": "string",
                "required": True,
                "description": "Name of the instance",
            },
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": "Zone of the instance (uses default if not specified)",
            },
        ],
        example="get_instance(instance_name='my-vm', zone='us-central1-a')",
        response_entity_type="Instance",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="start_instance",
        name="Start Instance",
        description="Start a stopped Compute Engine instance. The instance must be in STOPPED or TERMINATED status.",
        category="compute",
        parameters=[
            {
                "name": "instance_name",
                "type": "string",
                "required": True,
                "description": "Name of the instance to start",
            },
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": ZONE_OF_THE_INSTANCE,
            },
        ],
        example="start_instance(instance_name='my-vm', zone='us-central1-a')",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="stop_instance",
        name="Stop Instance",
        description="Stop a running Compute Engine instance. This is a graceful shutdown that preserves the instance's persistent disks.",
        category="compute",
        parameters=[
            {
                "name": "instance_name",
                "type": "string",
                "required": True,
                "description": "Name of the instance to stop",
            },
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": ZONE_OF_THE_INSTANCE,
            },
        ],
        example="stop_instance(instance_name='my-vm', zone='us-central1-a')",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="reset_instance",
        name="Reset Instance",
        description="Hard reset a Compute Engine instance. This is equivalent to pressing the reset button on a physical machine - the instance restarts immediately without graceful shutdown.",
        category="compute",
        parameters=[
            {
                "name": "instance_name",
                "type": "string",
                "required": True,
                "description": "Name of the instance to reset",
            },
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": ZONE_OF_THE_INSTANCE,
            },
        ],
        example="reset_instance(instance_name='my-vm', zone='us-central1-a')",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="get_instance_serial_port_output",
        name="Get Instance Serial Port Output",
        description="Get the serial port output (console log) from a Compute Engine instance. Useful for debugging boot issues.",
        category="compute",
        parameters=[
            {
                "name": "instance_name",
                "type": "string",
                "required": True,
                "description": "Name of the instance",
            },
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": ZONE_OF_THE_INSTANCE,
            },
            {
                "name": "port",
                "type": "integer",
                "required": False,
                "description": "Serial port number (1-4, default: 1)",
            },
        ],
        example="get_instance_serial_port_output(instance_name='my-vm')",
        # Logs don't have entity type
    ),
    # Disk Operations
    OperationDefinition(
        operation_id="list_disks",
        name="List Persistent Disks",
        description="List all persistent disks in the project. Returns disk name, size, type, status, and attached instances.",
        category="storage",
        parameters=[
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": "Zone to list disks from (default: all zones)",
            },
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": "Filter expression",
            },
        ],
        example="list_disks(zone='us-central1-a')",
        response_entity_type="Disk",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_disk",
        name="Get Disk Details",
        description="Get detailed information about a specific persistent disk including size, type, source image/snapshot, and users.",
        category="storage",
        parameters=[
            {
                "name": "disk_name",
                "type": "string",
                "required": True,
                "description": "Name of the disk",
            },
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": "Zone of the disk",
            },
        ],
        example="get_disk(disk_name='my-disk', zone='us-central1-a')",
        response_entity_type="Disk",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # Snapshot Operations
    OperationDefinition(
        operation_id="list_snapshots",
        name="List Disk Snapshots",
        description="List all disk snapshots in the project. Returns snapshot name, source disk, size, and status.",
        category="storage",
        parameters=[
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": "Filter expression",
            },
        ],
        example="list_snapshots()",
        response_entity_type="Snapshot",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_snapshot",
        name="Get Snapshot Details",
        description="Get detailed information about a specific disk snapshot including source disk, storage size, and creation time.",
        category="storage",
        parameters=[
            {
                "name": "snapshot_name",
                "type": "string",
                "required": True,
                "description": "Name of the snapshot",
            },
        ],
        example="get_snapshot(snapshot_name='my-snapshot')",
        response_entity_type="Snapshot",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="create_snapshot",
        name="Create Disk Snapshot",
        description="Create a snapshot of a persistent disk. Snapshots are incremental and can be used for backup or to create new disks.",
        category="storage",
        parameters=[
            {
                "name": "disk_name",
                "type": "string",
                "required": True,
                "description": "Name of the disk to snapshot",
            },
            {
                "name": "snapshot_name",
                "type": "string",
                "required": True,
                "description": "Name for the new snapshot",
            },
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": "Zone of the disk",
            },
            {
                "name": "description",
                "type": "string",
                "required": False,
                "description": "Description for the snapshot",
            },
        ],
        example="create_snapshot(disk_name='my-disk', snapshot_name='my-snapshot-2024')",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="delete_snapshot",
        name="Delete Snapshot",
        description="Delete a disk snapshot. This action is irreversible.",
        category="storage",
        parameters=[
            {
                "name": "snapshot_name",
                "type": "string",
                "required": True,
                "description": "Name of the snapshot to delete",
            },
        ],
        example="delete_snapshot(snapshot_name='old-snapshot')",
        # Action operations return status
    ),
    # Zone Operations
    OperationDefinition(
        operation_id="list_zones",
        name="List Available Zones",
        description="List all available zones in the project. Returns zone name, region, and status.",
        category="compute",
        parameters=[],
        example="list_zones()",
        response_entity_type="Zone",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="list_machine_types",
        name="List Machine Types",
        description="List available machine types in a zone. Returns vCPU count, memory, and other specifications.",
        category="compute",
        parameters=[
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": "Zone to list machine types for",
            },
        ],
        example="list_machine_types(zone='us-central1-a')",
        response_entity_type="MachineType",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
]
