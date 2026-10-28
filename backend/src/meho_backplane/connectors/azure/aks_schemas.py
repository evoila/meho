# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the Azure Kubernetes Service (AKS) reads.

The operation table in :mod:`meho_backplane.connectors.azure.aks` uses them.
Every list answer has the same frame: the rows under ``value``, their number
in ``count``, and the paging fields of
:meth:`~meho_backplane.connectors.azure._paging.PagedRows.as_dict`. The
version list reads one answer without pages, so it has no paging fields.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure._model import TEXT_OR_NULL
from meho_backplane.connectors.azure.aks_rows import (
    CLUSTER_NAME_PATTERN,
    LOCATION_PATTERN,
    RESOURCE_GROUP_MAX,
    RESOURCE_GROUP_PATTERN,
)

__all__ = [
    "CLUSTER_LIST_RESPONSE",
    "CLUSTER_SHOW_PARAMS",
    "CLUSTER_SHOW_RESPONSE",
    "NODE_POOL_LIST_PARAMS",
    "NODE_POOL_LIST_RESPONSE",
    "VERSION_LIST_PARAMS",
    "VERSION_LIST_RESPONSE",
]

_INT_OR_NULL: Final[list[str]] = ["integer", "null"]

_EXAMPLE_RESOURCE_GROUP: Final[str] = "rg-example"
_EXAMPLE_CLUSTER: Final[str] = "aks-example-01"


def _text(description: str) -> dict[str, Any]:
    return {"type": TEXT_OR_NULL, "description": description}


def _number(description: str) -> dict[str, Any]:
    return {"type": _INT_OR_NULL, "description": description}


def _flag(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def _texts(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------

_RESOURCE_GROUP: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": RESOURCE_GROUP_PATTERN,
    "maxLength": RESOURCE_GROUP_MAX,
    "description": "The resource group of the cluster, as azure.aks.cluster.list shows it.",
}


def _cluster(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "pattern": CLUSTER_NAME_PATTERN,
        "maxLength": 63,
        "description": description,
    }


CLUSTER_SHOW_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_group": _RESOURCE_GROUP,
        "name": _cluster("The name of the cluster, as azure.aks.cluster.list shows it."),
    },
    "required": ["resource_group", "name"],
    "additionalProperties": False,
    "examples": [{"resource_group": _EXAMPLE_RESOURCE_GROUP, "name": _EXAMPLE_CLUSTER}],
}

NODE_POOL_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_group": _RESOURCE_GROUP,
        "cluster": _cluster("The name of the cluster, as azure.aks.cluster.list shows it."),
    },
    "required": ["resource_group", "cluster"],
    "additionalProperties": False,
    "examples": [{"resource_group": _EXAMPLE_RESOURCE_GROUP, "cluster": _EXAMPLE_CLUSTER}],
}

VERSION_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "location": {
            "type": "string",
            "pattern": LOCATION_PATTERN,
            "description": (
                "The region name as Azure uses it in requests, for example westeurope "
                "(not the display name West Europe). A cluster's location field is one."
            ),
        },
    },
    "required": ["location"],
    "additionalProperties": False,
    "examples": [{"location": "westeurope"}],
}

# ----------------------------------------------------------------------
# Answers
# ----------------------------------------------------------------------

#: The paging fields every paged list answer carries next to ``value``.
_LIST_FIELDS: Final[dict[str, Any]] = {
    "count": {"type": "integer", "description": "How many rows value holds."},
    "truncated": _flag("True when MEHO stopped reading pages at one of its limits."),
    "omission": _text("What was left out and how to narrow the request, when truncated."),
    "omission_kind": _text("Which limit stopped the list: row_limit, page_limit or time_limit."),
    "pages_read": {"type": "integer", "description": "How many pages MEHO read from Azure."},
}

_CLUSTER_FIELDS: Final[dict[str, Any]] = {
    "name": _text("The cluster name. Text from Azure."),
    "id": _text("The Azure resource ID of the cluster."),
    "resource_group": _text("The resource group of the cluster."),
    "location": _text("The region of the cluster, for example westeurope."),
    "kubernetes_version": _text(
        "The Kubernetes version asked for; it may be only major.minor, for example 1.30."
    ),
    "current_kubernetes_version": _text("The Kubernetes version that runs (major.minor.patch)."),
    "provisioning_state": _text("Succeeded, Failed, Updating, Upgrading and so on."),
    "power_state": _text("Running or Stopped."),
    "sku_tier": _text("Free, Standard or Premium (the pricing tier and its uptime promise)."),
    "support_plan": _text("KubernetesOfficial or AKSLongTermSupport; null means Azure gave none."),
    "node_resource_group": _text("The resource group that holds the cluster's VMs and disks."),
    "network_plugin": _text("azure, kubenet or none."),
    "network_plugin_mode": _text("overlay for Azure CNI Overlay, else null."),
    "network_policy": _text("azure, calico, cilium or none; null when not set."),
    "network_dataplane": _text("azure or cilium."),
    "outbound_type": _text("How traffic leaves the cluster, for example loadBalancer."),
    "private_cluster": _flag("True when the API server has only a private address."),
    "identity_type": _text(
        "SystemAssigned, UserAssigned, ServicePrincipal (a login with a secret), or null."
    ),
    "auto_upgrade_channel": _text("rapid, stable, patch, node-image or none."),
    "node_os_upgrade_channel": _text("None, Unmanaged, SecurityPatch or NodeImage."),
    "node_pools": _texts("The names of the node pools."),
    "node_count": _number("The sum of the node counts of the pools."),
    "addons": _texts("The add-ons and managed features that are on, sorted."),
}

CLUSTER_LIST_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "value": {
            "type": "array",
            "items": {"type": "object", "properties": _CLUSTER_FIELDS},
            "description": (
                "One row per AKS cluster. More than 50 rows come back as a link to the "
                "stored result."
            ),
        },
        **_LIST_FIELDS,
    },
    "required": ["value", "count", "truncated", "pages_read"],
}

_VERSION_CHOICES: Final[dict[str, Any]] = {
    "kubernetes_version": _text("The Kubernetes version that runs now."),
    "upgrades": _texts("The released versions it can upgrade to, oldest first."),
    "preview_upgrades": _texts("The preview versions it can upgrade to, oldest first."),
}

CLUSTER_SHOW_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **_CLUSTER_FIELDS,
        "fqdn": _text("The host name of the cluster's API server."),
        "private_fqdn": _text("The private host name of the API server of a private cluster."),
        "pod_cidrs": _texts("The address ranges of the pods."),
        "service_cidrs": _texts("The address ranges of the Kubernetes services."),
        "upgrades": {
            "type": "object",
            "description": "The Kubernetes versions the cluster and its node pools can move to.",
            "properties": {
                "control_plane": {
                    "type": "object",
                    "description": "The upgrade options of the control plane.",
                    "properties": _VERSION_CHOICES,
                },
                "node_pools": {
                    "type": "array",
                    "description": "The upgrade options of each node pool.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": _text("The node pool name."),
                            "os_type": _text("Linux or Windows."),
                            **_VERSION_CHOICES,
                        },
                    },
                },
            },
        },
    },
    "required": ["name", "upgrades"],
}

_NODE_POOL_FIELDS: Final[dict[str, Any]] = {
    "name": _text("The node pool name."),
    "mode": _text("System (runs the cluster's own pods), User or Gateway."),
    "vm_size": _text("The VM size of the nodes, for example Standard_D4s_v5."),
    "count": _number("The number of nodes now."),
    "autoscaling": _flag("True when the cluster autoscaler changes the node count."),
    "min_count": _number("The fewest nodes the autoscaler keeps."),
    "max_count": _number("The most nodes the autoscaler adds."),
    "os_type": _text("Linux or Windows."),
    "os_sku": _text("Ubuntu, AzureLinux, Windows2019, Windows2022 and so on."),
    "orchestrator_version": _text("The Kubernetes version asked for."),
    "current_orchestrator_version": _text("The Kubernetes version that runs."),
    "node_image_version": _text("The version of the node image."),
    "power_state": _text("Running or Stopped."),
    "provisioning_state": _text("Succeeded, Failed, Upgrading and so on."),
    "availability_zones": _texts("The availability zones of the nodes, for example 1, 2, 3."),
    "max_pods": _number("The most pods one node runs."),
    "priority": _text("Regular, or Spot for VMs Azure may take back."),
    "pool_type": _text("VirtualMachineScaleSets, VirtualMachines or AvailabilitySet."),
}

NODE_POOL_LIST_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "value": {
            "type": "array",
            "items": {"type": "object", "properties": _NODE_POOL_FIELDS},
            "description": (
                "One row per node pool. More than 50 rows come back as a link to the stored result."
            ),
        },
        **_LIST_FIELDS,
        "cluster": _text("The cluster the node pools belong to."),
        "resource_group": _text("The resource group of the cluster."),
    },
    "required": ["value", "count", "truncated", "pages_read", "cluster", "resource_group"],
}

VERSION_LIST_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "value": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "version": _text("The Kubernetes minor version, for example 1.30."),
                    "is_default": _flag("True for the version a new cluster gets by default."),
                    "is_preview": _flag("True when the version is in preview."),
                    "support_plans": _texts(
                        "KubernetesOfficial, and AKSLongTermSupport when long-term support "
                        "is offered."
                    ),
                    "patch_versions": _texts("The patch versions offered, oldest first."),
                },
            },
            "description": "One row per Kubernetes minor version, oldest first.",
        },
        "count": {"type": "integer", "description": "How many rows value holds."},
        "location": _text("The region the versions are for."),
        "default_version": _text("The minor version a new cluster gets by default."),
    },
    "required": ["value", "count", "location"],
}
