# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Resource Graph operations of the Azure ``inventory`` family.

``azure.graph.query`` runs the caller's read-only Resource Graph query (KQL).
``azure.inventory.summary`` and the fixed lists of VMs, AKS node pools and
network resources run MEHO's own queries, written here. All of them run over
a checked set of subscriptions with
:func:`~meho_backplane.connectors.azure._graph_set.read_over_set`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._graph_set import read_over_set
from meho_backplane.connectors.azure.session import AzureTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "AKS_LIST_OP_ID",
    "AKS_QUERY",
    "GRAPH_QUERY_OP_ID",
    "NETWORK_LIST_OP_ID",
    "NETWORK_QUERY",
    "SUMMARY_OP_ID",
    "SUMMARY_QUERY",
    "VM_LIST_OP_ID",
    "VM_QUERY",
    "graph_query_impl",
    "inventory_aks_list_impl",
    "inventory_network_list_impl",
    "inventory_summary_impl",
    "inventory_vm_list_impl",
]

GRAPH_QUERY_OP_ID: Final[str] = "azure.graph.query"
SUMMARY_OP_ID: Final[str] = "azure.inventory.summary"
VM_LIST_OP_ID: Final[str] = "azure.inventory.vm.list"
AKS_LIST_OP_ID: Final[str] = "azure.inventory.aks.list"
NETWORK_LIST_OP_ID: Final[str] = "azure.inventory.network.list"

SUMMARY_QUERY: Final[str] = "Resources | summarize count() by subscriptionId, type, location"

VM_QUERY: Final[str] = (
    "Resources\n"
    "| where type =~ 'microsoft.compute/virtualmachines'\n"
    "| project id, name, subscriptionId, resourceGroup, location,\n"
    "    size = tostring(properties.hardwareProfile.vmSize),\n"
    "    os = tostring(properties.storageProfile.osDisk.osType),\n"
    "    power = tostring(properties.extended.instanceView.powerState.code)"
)

AKS_QUERY: Final[str] = (
    "Resources\n"
    "| where type =~ 'microsoft.containerservice/managedclusters'\n"
    "| mv-expand pool = properties.agentPoolProfiles\n"
    "| project id, cluster = name, subscriptionId, resourceGroup, location,\n"
    "    k8s = tostring(properties.kubernetesVersion), pool = tostring(pool.name),\n"
    "    vmSize = tostring(pool.vmSize), nodes = toint(pool['count']),\n"
    "    mode = tostring(pool.mode)"
)

NETWORK_QUERY: Final[str] = (
    "Resources\n"
    "| where type in~ ('microsoft.network/virtualnetworks',\n"
    "    'microsoft.network/networksecuritygroups', 'microsoft.network/publicipaddresses',\n"
    "    'microsoft.network/loadbalancers')\n"
    "| project id, name, type, subscriptionId, resourceGroup, location,\n"
    "    addressPrefixes = properties.addressSpace.addressPrefixes,\n"
    "    ipAddress = tostring(properties.ipAddress)"
)


# ----------------------------------------------------------------------
# The operations
# ----------------------------------------------------------------------


async def graph_query_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.graph.query``: the caller's read-only Resource Graph query over the set."""
    query = params.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a Resource Graph query (KQL).")
    page_size = params.get("page_size")
    return await read_over_set(
        connector,
        operator,
        target,
        params.get("subscriptions") or [],
        op_id=GRAPH_QUERY_OP_ID,
        query=query,
        page_size=page_size if isinstance(page_size, int) else None,
    )


async def inventory_summary_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.inventory.summary``: resources counted by subscription, type and location."""
    result = await read_over_set(
        connector,
        operator,
        target,
        params.get("subscriptions") or [],
        op_id=SUMMARY_OP_ID,
        query=SUMMARY_QUERY,
    )
    result["resource_total"] = _resource_total(result["value"])
    return result


def _resource_total(rows: list[Any]) -> int:
    """The sum of the ``count_`` column over *rows*."""
    counts = (row.get("count_") for row in rows if isinstance(row, dict))
    return sum(count for count in counts if isinstance(count, int) and not isinstance(count, bool))


async def inventory_vm_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.inventory.vm.list``: the virtual machines of the set, one row each."""
    names = params.get("subscriptions") or []
    return await read_over_set(
        connector, operator, target, names, op_id=VM_LIST_OP_ID, query=VM_QUERY
    )


async def inventory_aks_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.inventory.aks.list``: the AKS clusters of the set, one row per node pool."""
    names = params.get("subscriptions") or []
    return await read_over_set(
        connector, operator, target, names, op_id=AKS_LIST_OP_ID, query=AKS_QUERY
    )


async def inventory_network_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.inventory.network.list``: networks, security groups, public IPs, load balancers."""
    names = params.get("subscriptions") or []
    return await read_over_set(
        connector, operator, target, names, op_id=NETWORK_LIST_OP_ID, query=NETWORK_QUERY
    )


# ----------------------------------------------------------------------
# The read over the set
# ----------------------------------------------------------------------
