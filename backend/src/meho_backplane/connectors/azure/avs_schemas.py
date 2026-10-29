# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the Azure VMware Solution reads.

The operation table is in :mod:`meho_backplane.connectors.azure.avs`. The
schemas tell the AI which fields it may send and what comes back. The name
rules here are also the checks the operation bodies run before a name goes
into a URL path (:mod:`.avs_reads`).
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure._model import TEXT_OR_NULL

__all__ = [
    "AVS_NAME_CHARS",
    "AVS_NAME_MAX_LENGTH",
    "CLUSTER_LIST_PARAMS",
    "CLUSTER_LIST_RESPONSE",
    "HOST_LIST_PARAMS",
    "HOST_LIST_RESPONSE",
    "LOCATION_CHARS",
    "LOCATION_MAX_LENGTH",
    "PRIVATE_CLOUD_LIST_RESPONSE",
    "QUOTA_SHOW_PARAMS",
    "QUOTA_SHOW_RESPONSE",
    "RESOURCE_GROUP_CHARS",
    "RESOURCE_GROUP_MAX_LENGTH",
]

#: A resource group name: letters, digits, ``_``, ``-``, ``.``, ``(`` and
#: ``)``, 1 to 90 characters, not ending with ``.`` (Microsoft's naming
#: rules). Not ending with ``.`` also rules out ``.`` and ``..``, which could
#: move the URL path.
RESOURCE_GROUP_CHARS: Final[str] = r"[-\w.()]{0,89}[-\w()]"
RESOURCE_GROUP_MAX_LENGTH: Final[int] = 90

#: A private cloud or cluster name: 1 to 80 letters, digits, ``_``, ``-`` and
#: ``.`` (the characters Azure's API description allows), and not only dots,
#: so ``.`` and ``..`` can never move the URL path.
AVS_NAME_CHARS: Final[str] = r"(?!\.+$)[-A-Za-z0-9_.]{1,80}"
AVS_NAME_MAX_LENGTH: Final[int] = 80

#: A region name as Azure uses it in requests: letters and digits only, for
#: example ``westeurope``.
LOCATION_CHARS: Final[str] = r"[A-Za-z0-9]{1,64}"
LOCATION_MAX_LENGTH: Final[int] = 64


def _anchored(chars: str) -> str:
    """The JSON Schema pattern of *chars*: the whole value, nothing more.

    ``(?!\\n)``: the validator uses Python's ``re``, where ``$`` also matches
    before a final newline, so without it ``"name\\n"`` would pass.
    """
    return f"^{chars}$(?!\\n)"


def _text(description: str) -> dict[str, Any]:
    return {"type": TEXT_OR_NULL, "description": description}


def _texts(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def _int(description: str) -> dict[str, Any]:
    return {"type": ["integer", "null"], "description": description}


def _row(fields: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": fields, "required": ["name"]}


def _paged_list(row: dict[str, Any], what: str, **context: dict[str, Any]) -> dict[str, Any]:
    """The answer of a list: rows under ``value``, the paging fields and *context*."""
    return {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": row,
                "description": (
                    f"One row per {what}. More than 50 rows (or more than 4 KB) come "
                    "back as a link to the stored result; read it with result_query."
                ),
            },
            **context,
            "truncated": {
                "type": "boolean",
                "description": "True when MEHO stopped reading pages at one of its limits.",
            },
            "omission": _text("What was left out and how to narrow the request, when truncated."),
            "omission_kind": _text(
                "Which limit stopped the list: row_limit, page_limit or time_limit."
            ),
            "pages_read": {"type": "integer", "description": "How many pages MEHO read."},
        },
        "required": ["value", "truncated", "pages_read"],
    }


# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------

_RESOURCE_GROUP_PARAM: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": RESOURCE_GROUP_MAX_LENGTH,
    "pattern": _anchored(RESOURCE_GROUP_CHARS),
    "description": (
        "The resource group of the private cloud, as azure.avs.privatecloud.list shows "
        "it (resource_group)."
    ),
}

_PRIVATE_CLOUD_PARAM: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": AVS_NAME_MAX_LENGTH,
    "pattern": _anchored(AVS_NAME_CHARS),
    "description": "The name of the private cloud, as azure.avs.privatecloud.list shows it.",
}

_CLUSTER_PARAM: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": AVS_NAME_MAX_LENGTH,
    "pattern": _anchored(AVS_NAME_CHARS),
    "description": "The name of the cluster, as azure.avs.cluster.list shows it.",
}

_EXAMPLE_GROUP: Final[str] = "rg-avs-example"
_EXAMPLE_CLOUD: Final[str] = "avs-cloud-example"

CLUSTER_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_group": _RESOURCE_GROUP_PARAM,
        "private_cloud": _PRIVATE_CLOUD_PARAM,
    },
    "required": ["resource_group", "private_cloud"],
    "additionalProperties": False,
    "examples": [{"resource_group": _EXAMPLE_GROUP, "private_cloud": _EXAMPLE_CLOUD}],
}

HOST_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_group": _RESOURCE_GROUP_PARAM,
        "private_cloud": _PRIVATE_CLOUD_PARAM,
        "cluster": _CLUSTER_PARAM,
    },
    "required": ["resource_group", "private_cloud", "cluster"],
    "additionalProperties": False,
    "examples": [
        {"resource_group": _EXAMPLE_GROUP, "private_cloud": _EXAMPLE_CLOUD, "cluster": "Cluster-2"}
    ],
}

QUOTA_SHOW_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "location": {
            "type": "string",
            "minLength": 1,
            "maxLength": LOCATION_MAX_LENGTH,
            "pattern": _anchored(LOCATION_CHARS),
            "description": (
                "The Azure region, as Azure writes it in requests: letters and digits "
                "only, for example westeurope. azure.location.list shows the names."
            ),
        },
    },
    "required": ["location"],
    "additionalProperties": False,
    "examples": [{"location": "westeurope"}],
}


# ----------------------------------------------------------------------
# Responses
# ----------------------------------------------------------------------

_PRIVATE_CLOUD_ROW: Final[dict[str, Any]] = _row(
    {
        "name": _text("The name of the private cloud."),
        "resource_group": _text("The resource group, read from the Azure resource ID."),
        "location": _text("The Azure region, for example westeurope."),
        "id": _text("The Azure resource ID of the private cloud."),
        "sku": _text("The host type (SKU) of the management cluster, for example AV36P."),
        "provisioning_state": _text(
            "Succeeded, Failed, Building, Pending, Updating, Deleting or Canceled."
        ),
        "management_cluster_size": _int("How many hosts the management cluster has."),
        "management_cluster_hosts": _texts("The names of the hosts of the management cluster."),
        "network_block": _text(
            "The address block of the private cloud (CIDR, /22 or larger). It must not "
            "overlap the customer's other networks."
        ),
        "extended_network_blocks": _texts("More address blocks of the private cloud, if any."),
        "internet": _text("Enabled or Disabled: internet access through Azure."),
        "availability_strategy": _text(
            "SingleZone, or DualZone for a stretched private cloud over two zones."
        ),
        "availability_zone": _int("The availability zone of the private cloud."),
        "secondary_zone": _int("The second availability zone of a stretched private cloud."),
        "identity_type": _text("The managed identity type: SystemAssigned or None."),
        "vcf_license_kind": _text(
            "The kind of VMware Cloud Foundation license the customer brought, for "
            "example vcf5 (VCF 5.0); null when none is attached."
        ),
    }
)

_CLUSTER_ROW: Final[dict[str, Any]] = _row(
    {
        "name": _text("The name of the cluster, for example Cluster-2."),
        "id": _text("The Azure resource ID of the cluster."),
        "sku": _text("The host type (SKU) of the cluster, for example AV36P."),
        "cluster_size": _int("How many hosts the cluster has."),
        "hosts": _texts("The names of the hosts of the cluster."),
        "provisioning_state": _text("Succeeded, Failed, Updating, Deleting or Canceled."),
    }
)

_HOST_ROW: Final[dict[str, Any]] = _row(
    {
        "name": _text("The name of the host in Azure."),
        "id": _text("The Azure resource ID of the host."),
        "display_name": _text("The name of the host in vCenter."),
        "kind": _text("General or Specialized."),
        "provisioning_state": _text("Succeeded, Failed or Canceled."),
        "fqdn": _text("The fully qualified domain name of the host."),
        "mo_ref_id": _text("The ID of the host in vCenter, for example host-209."),
        "maintenance": _text("Replacement or Upgrade while the host is in maintenance, else null."),
        "fault_domain": _text("The fault domain of the host."),
        "sku": _text("The host type (SKU), for example AV64."),
        "zones": _texts("The availability zones of the host."),
    }
)

_RESOURCE_GROUP_FIELD: Final[dict[str, Any]] = _text("The resource group that was asked for.")
_PRIVATE_CLOUD_FIELD: Final[dict[str, Any]] = _text("The private cloud that was asked for.")

PRIVATE_CLOUD_LIST_RESPONSE: Final[dict[str, Any]] = _paged_list(
    _PRIVATE_CLOUD_ROW, "private cloud"
)

CLUSTER_LIST_RESPONSE: Final[dict[str, Any]] = _paged_list(
    _CLUSTER_ROW,
    "cluster (the management cluster is not in this list)",
    resource_group=_RESOURCE_GROUP_FIELD,
    private_cloud=_PRIVATE_CLOUD_FIELD,
)

HOST_LIST_RESPONSE: Final[dict[str, Any]] = _paged_list(
    _HOST_ROW,
    "host",
    resource_group=_RESOURCE_GROUP_FIELD,
    private_cloud=_PRIVATE_CLOUD_FIELD,
    cluster=_text("The cluster that was asked for."),
)

QUOTA_SHOW_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "location": {"type": "string", "description": "The region that was asked for."},
        "quota_enabled": _text(
            "Enabled when the subscription has host quota for Azure VMware Solution, else Disabled."
        ),
        "hosts_remaining": {
            "type": "object",
            "additionalProperties": {"type": "integer"},
            "description": (
                "How many more hosts of each host type (SKU) the subscription may "
                'deploy in the region, for example {"AV36P": 3}.'
            ),
        },
    },
    "required": ["location", "quota_enabled", "hosts_remaining"],
}
