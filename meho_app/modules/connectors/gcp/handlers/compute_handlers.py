# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Compute Engine Handlers (TASK-102)

Handlers for Compute Engine operations (instances, disks, snapshots).
"""

import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.gcp.serializers import (
    serialize_disk,
    serialize_instance,
    serialize_snapshot,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.gcp.connector import GCPConnector

logger = get_logger(__name__)


class ComputeHandlerMixin:
    """Mixin providing Compute Engine operation handlers."""

    # Type hints for IDE support
    if TYPE_CHECKING:
        _instances_client: Any
        _disks_client: Any
        _snapshots_client: Any
        _credentials: Any
        project_id: str
        default_zone: str

    # =========================================================================
    # INSTANCE OPERATIONS
    # =========================================================================

    async def _handle_list_instances(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List Compute Engine instances."""
        from google.cloud import compute_v1

        zone = params.get("zone")
        filter_str = params.get("filter")

        instances = []

        if zone:
            # List instances in specific zone
            request = compute_v1.ListInstancesRequest(
                project=self.project_id,
                zone=zone,
                filter=filter_str,
            )
            response = await asyncio.to_thread(
                lambda: list(self._instances_client.list(request=request))
            )
            instances.extend(response)
        else:
            # List instances in all zones (aggregated list)
            request = compute_v1.AggregatedListInstancesRequest(
                project=self.project_id,
                filter=filter_str,
            )
            response = await asyncio.to_thread(
                lambda: self._instances_client.aggregated_list(request=request)
            )
            for _zone_name, instances_scoped_list in response:
                if instances_scoped_list.instances:
                    instances.extend(instances_scoped_list.instances)

        return [serialize_instance(i) for i in instances]

    async def _handle_get_instance(self: "GCPConnector", params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]
        """Get instance details."""
        from google.cloud import compute_v1

        instance_name = params["instance_name"]
        zone = params.get("zone", self.default_zone)

        request = compute_v1.GetInstanceRequest(
            project=self.project_id,
            zone=zone,
            instance=instance_name,
        )
        instance = await asyncio.to_thread(lambda: self._instances_client.get(request=request))

        return serialize_instance(instance)

    async def _handle_start_instance(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Start an instance."""
        from google.cloud import compute_v1

        instance_name = params["instance_name"]
        zone = params.get("zone", self.default_zone)

        request = compute_v1.StartInstanceRequest(
            project=self.project_id,
            zone=zone,
            instance=instance_name,
        )
        operation = await asyncio.to_thread(lambda: self._instances_client.start(request=request))

        return {
            "operation_id": operation.name,
            "status": "STARTING",
            "message": f"Starting instance {instance_name}",
        }

    async def _handle_stop_instance(self: "GCPConnector", params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]
        """Stop an instance."""
        from google.cloud import compute_v1

        instance_name = params["instance_name"]
        zone = params.get("zone", self.default_zone)

        request = compute_v1.StopInstanceRequest(
            project=self.project_id,
            zone=zone,
            instance=instance_name,
        )
        operation = await asyncio.to_thread(lambda: self._instances_client.stop(request=request))

        return {
            "operation_id": operation.name,
            "status": "STOPPING",
            "message": f"Stopping instance {instance_name}",
        }

    async def _handle_reset_instance(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Reset (hard reboot) an instance."""
        from google.cloud import compute_v1

        instance_name = params["instance_name"]
        zone = params.get("zone", self.default_zone)

        request = compute_v1.ResetInstanceRequest(
            project=self.project_id,
            zone=zone,
            instance=instance_name,
        )
        operation = await asyncio.to_thread(lambda: self._instances_client.reset(request=request))

        return {
            "operation_id": operation.name,
            "status": "RESETTING",
            "message": f"Resetting instance {instance_name}",
        }

    async def _handle_get_instance_serial_port_output(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get serial port output from an instance."""
        from google.cloud import compute_v1

        instance_name = params["instance_name"]
        zone = params.get("zone", self.default_zone)
        port = params.get("port", 1)

        request = compute_v1.GetSerialPortOutputInstanceRequest(
            project=self.project_id,
            zone=zone,
            instance=instance_name,
            port=port,
        )
        output = await asyncio.to_thread(
            lambda: self._instances_client.get_serial_port_output(request=request)
        )

        return {
            "instance": instance_name,
            "port": port,
            "contents": output.contents,
            "next": output.next_,
        }

    # =========================================================================
    # DISK OPERATIONS
    # =========================================================================

    async def _handle_list_disks(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List persistent disks."""
        from google.cloud import compute_v1

        zone = params.get("zone")
        filter_str = params.get("filter")

        disks = []

        if zone:
            request = compute_v1.ListDisksRequest(
                project=self.project_id,
                zone=zone,
                filter=filter_str,
            )
            response = await asyncio.to_thread(
                lambda: list(self._disks_client.list(request=request))
            )
            disks.extend(response)
        else:
            request = compute_v1.AggregatedListDisksRequest(
                project=self.project_id,
                filter=filter_str,
            )
            response = await asyncio.to_thread(
                lambda: self._disks_client.aggregated_list(request=request)
            )
            for _zone_name, disks_scoped_list in response:
                if disks_scoped_list.disks:
                    disks.extend(disks_scoped_list.disks)

        return [serialize_disk(d) for d in disks]

    async def _handle_get_disk(self: "GCPConnector", params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]
        """Get disk details."""
        from google.cloud import compute_v1

        disk_name = params["disk_name"]
        zone = params.get("zone", self.default_zone)

        request = compute_v1.GetDiskRequest(
            project=self.project_id,
            zone=zone,
            disk=disk_name,
        )
        disk = await asyncio.to_thread(lambda: self._disks_client.get(request=request))

        return serialize_disk(disk)

    # =========================================================================
    # SNAPSHOT OPERATIONS
    # =========================================================================

    async def _handle_list_snapshots(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List disk snapshots."""
        from google.cloud import compute_v1

        filter_str = params.get("filter")

        request = compute_v1.ListSnapshotsRequest(
            project=self.project_id,
            filter=filter_str,
        )
        snapshots = await asyncio.to_thread(
            lambda: list(self._snapshots_client.list(request=request))
        )

        return [serialize_snapshot(s) for s in snapshots]

    async def _handle_get_snapshot(self: "GCPConnector", params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]
        """Get snapshot details."""
        from google.cloud import compute_v1

        snapshot_name = params["snapshot_name"]

        request = compute_v1.GetSnapshotRequest(
            project=self.project_id,
            snapshot=snapshot_name,
        )
        snapshot = await asyncio.to_thread(lambda: self._snapshots_client.get(request=request))

        return serialize_snapshot(snapshot)

    async def _handle_create_snapshot(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a disk snapshot."""
        from google.cloud import compute_v1

        disk_name = params["disk_name"]
        snapshot_name = params["snapshot_name"]
        zone = params.get("zone", self.default_zone)
        description = params.get("description", "")

        snapshot = compute_v1.Snapshot(
            name=snapshot_name,
            description=description,
        )

        request = compute_v1.CreateSnapshotDiskRequest(
            project=self.project_id,
            zone=zone,
            disk=disk_name,
            snapshot_resource=snapshot,
        )
        operation = await asyncio.to_thread(
            lambda: self._disks_client.create_snapshot(request=request)
        )

        return {
            "operation_id": operation.name,
            "snapshot_name": snapshot_name,
            "status": "CREATING",
            "message": f"Creating snapshot {snapshot_name} from disk {disk_name}",
        }

    async def _handle_delete_snapshot(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Delete a snapshot."""
        from google.cloud import compute_v1

        snapshot_name = params["snapshot_name"]

        request = compute_v1.DeleteSnapshotRequest(
            project=self.project_id,
            snapshot=snapshot_name,
        )
        operation = await asyncio.to_thread(lambda: self._snapshots_client.delete(request=request))

        return {
            "operation_id": operation.name,
            "status": "DELETING",
            "message": f"Deleting snapshot {snapshot_name}",
        }

    # =========================================================================
    # ZONE/MACHINE TYPE OPERATIONS
    # =========================================================================

    async def _handle_list_zones(  # type: ignore[misc]
        self: "GCPConnector", _params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List available zones."""
        from google.cloud import compute_v1

        zones_client = compute_v1.ZonesClient(credentials=self._credentials)

        request = compute_v1.ListZonesRequest(project=self.project_id)
        zones = await asyncio.to_thread(lambda: list(zones_client.list(request=request)))

        return [
            {
                "name": z.name,
                "description": z.description,
                "region": z.region.split("/")[-1] if z.region else None,
                "status": z.status,
            }
            for z in zones
        ]

    async def _handle_list_machine_types(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List available machine types in a zone."""
        from google.cloud import compute_v1

        zone = params.get("zone", self.default_zone)

        machine_types_client = compute_v1.MachineTypesClient(credentials=self._credentials)

        request = compute_v1.ListMachineTypesRequest(
            project=self.project_id,
            zone=zone,
        )
        machine_types = await asyncio.to_thread(
            lambda: list(machine_types_client.list(request=request))
        )

        return [
            {
                "name": mt.name,
                "description": mt.description,
                "guest_cpus": mt.guest_cpus,
                "memory_mb": mt.memory_mb,
                "is_shared_cpu": mt.is_shared_cpu,
            }
            for mt in machine_types
        ]
