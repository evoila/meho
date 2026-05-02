# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Compute handler mixin (Phase 92).

Handlers for Compute Engine operations: VMs, disks, resource groups,
availability sets. Uses native async Azure SDK clients.
"""

from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.azure.helpers import (
    _build_resource_uri,
    _extract_resource_group,
    _safe_tags,
)
from meho_app.modules.connectors.azure.serializers import (
    serialize_azure_disk,
    serialize_azure_resource_group,
    serialize_azure_vm,
    serialize_azure_vm_instance_view,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.azure.connector import AzureConnector

logger = get_logger(__name__)


class ComputeHandlerMixin:
    """Mixin providing Azure Compute operation handlers.

    Covers VMs, managed disks, resource groups, and availability sets.
    All methods use native async Azure SDK calls (no asyncio.to_thread).
    """

    if TYPE_CHECKING:
        _compute_client: Any
        _resource_client: Any
        _subscription_id: str
        _resource_group_filter: str | None

    # =========================================================================
    # VM OPERATIONS
    # =========================================================================

    async def _handle_list_azure_vms(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List Azure Virtual Machines.

        If resource_group is provided, lists VMs in that group.
        Otherwise falls back to resource_group_filter, then lists all.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter
        state_filter = params.get("state_filter")

        results: list[dict[str, Any]] = []

        if resource_group:
            async for vm in self._compute_client.virtual_machines.list(resource_group):
                results.append(serialize_azure_vm(vm))
        else:
            async for vm in self._compute_client.virtual_machines.list_all():
                results.append(serialize_azure_vm(vm))

        # Apply optional state filter (e.g., "running", "deallocated")
        if state_filter:
            state_lower = state_filter.lower()
            results = [
                r for r in results if state_lower in (r.get("provisioning_state", "") or "").lower()
            ]

        return results

    async def _handle_get_azure_vm(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get VM details including power state.

        Fetches both the VM properties and the instance view to merge
        power state information into the response.
        """
        resource_group = params["resource_group"]
        vm_name = params["vm_name"]

        # Get VM properties
        vm = await self._compute_client.virtual_machines.get(
            resource_group_name=resource_group,
            vm_name=vm_name,
        )
        result = serialize_azure_vm(vm)

        # Overlay power state from instance view
        try:
            iv = await self._compute_client.virtual_machines.instance_view(
                resource_group_name=resource_group,
                vm_name=vm_name,
            )
            iv_data = serialize_azure_vm_instance_view(iv)
            result["power_state"] = iv_data.get("power_state")
            result["vm_agent_status"] = iv_data.get("vm_agent_status")
            result["os_name"] = iv_data.get("os_name")
            result["os_version"] = iv_data.get("os_version")
        except Exception as e:
            logger.warning(f"Failed to get instance view for {vm_name}: {e}")
            result["power_state"] = None

        return result

    async def _handle_get_azure_vm_instance_view(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get VM runtime status (instance view).

        Returns power state, VM agent status, boot diagnostics, and
        maintenance state information.
        """
        resource_group = params["resource_group"]
        vm_name = params["vm_name"]

        iv = await self._compute_client.virtual_machines.instance_view(
            resource_group_name=resource_group,
            vm_name=vm_name,
        )
        result = serialize_azure_vm_instance_view(iv)
        result["vm_name"] = vm_name
        result["resource_group"] = resource_group
        return result

    async def _handle_get_azure_vm_metrics(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get common VM metrics (CPU, memory, disk, network).

        Composite operation: builds the resource URI for the VM, then
        delegates to _handle_get_azure_metrics for the actual query.
        """
        resource_group = params["resource_group"]
        vm_name = params["vm_name"]
        timespan = params.get("timespan", "PT1H")
        interval = params.get("interval", "PT5M")

        resource_uri = _build_resource_uri(
            subscription_id=self._subscription_id,
            resource_group=resource_group,
            provider="Microsoft.Compute",
            resource_type="virtualMachines",
            resource_name=vm_name,
        )

        # Common VM metrics
        metric_names = "Percentage CPU,Available Memory Bytes,Disk Read Bytes,Disk Write Bytes,Network In Total,Network Out Total"

        metrics_result = await self._handle_get_azure_metrics(
            {
                "resource_uri": resource_uri,
                "timespan": timespan,
                "interval": interval,
                "metricnames": metric_names,
                "aggregation": "Average",
            }
        )

        return {
            "vm_name": vm_name,
            "resource_group": resource_group,
            "timespan": timespan,
            "interval": interval,
            "metrics": metrics_result,
        }

    # =========================================================================
    # DISK OPERATIONS
    # =========================================================================

    async def _handle_list_azure_disks(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List managed disks.

        If resource_group is provided, lists disks in that group.
        Otherwise falls back to resource_group_filter, then lists all.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter

        results: list[dict[str, Any]] = []

        if resource_group:
            async for disk in self._compute_client.disks.list_by_resource_group(resource_group):
                results.append(serialize_azure_disk(disk))
        else:
            async for disk in self._compute_client.disks.list():
                results.append(serialize_azure_disk(disk))

        return results

    async def _handle_get_azure_disk(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get managed disk details."""
        resource_group = params["resource_group"]
        disk_name = params["disk_name"]

        disk = await self._compute_client.disks.get(
            resource_group_name=resource_group,
            disk_name=disk_name,
        )
        return serialize_azure_disk(disk)

    # =========================================================================
    # AVAILABILITY SET OPERATIONS
    # =========================================================================

    async def _handle_list_azure_availability_sets(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List availability sets in a resource group."""
        resource_group = params.get("resource_group") or self._resource_group_filter

        if not resource_group:
            return [{"error": "resource_group is required for listing availability sets"}]

        results: list[dict[str, Any]] = []
        async for avset in self._compute_client.availability_sets.list(resource_group):
            results.append(
                {
                    "id": avset.id,
                    "name": avset.name,
                    "location": avset.location,
                    "resource_group": _extract_resource_group(avset.id or ""),
                    "platform_fault_domain_count": getattr(
                        avset, "platform_fault_domain_count", None
                    ),
                    "platform_update_domain_count": getattr(
                        avset, "platform_update_domain_count", None
                    ),
                    "sku_name": avset.sku.name if avset.sku else None,
                    "tags": _safe_tags(avset.tags),
                }
            )

        return results

    # =========================================================================
    # RESOURCE GROUP OPERATIONS
    # =========================================================================

    async def _handle_list_azure_resource_groups(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List all resource groups in the subscription."""
        results: list[dict[str, Any]] = []
        async for rg in self._resource_client.resource_groups.list():
            results.append(serialize_azure_resource_group(rg))

        return results
