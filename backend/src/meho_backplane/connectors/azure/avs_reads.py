# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure VMware Solution reads.

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.avs.AvsOps`. The operation table and
the guidance for the AI are in :mod:`meho_backplane.connectors.azure.avs`,
the schemas in :mod:`meho_backplane.connectors.azure.avs_schemas`.

Every function here only reads. The three lists use GET through
:func:`~meho_backplane.connectors.azure._paging.list_all_pages`, so every
``nextLink`` is checked before the token goes to it and a long list goes
behind a link to the stored result. The quota check is a POST that only
reads, so it is sent as a repeatable call, like a GET.

Each row is built from a fixed list of fields. Nothing else from Azure's
answer is passed on, so a password or a license key could never reach a
result even if Azure sent one. MEHO never calls the private cloud's
``listAdminCredentials`` action (the vCenter and NSX passwords) or the
actions that return license keys.

A resource group or a name goes into a URL path only after a check against
the rules in :mod:`.avs_schemas`. A ``/``, a ``?`` or a name such as ``..`` is
refused, so it can never move the call to another path or another
subscription. The checked name is then percent-encoded: a special character
such as ``(`` is written as a ``%`` code (``%28``), so it stays part of the
name.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paging import list_all_pages
from meho_backplane.connectors.azure._paths import (
    AVS_API_VERSION,
    AVS_CLUSTERS_PATH,
    AVS_HOSTS_PATH,
    AVS_PRIVATE_CLOUDS_PATH,
    AVS_QUOTA_PATH,
)
from meho_backplane.connectors.azure.avs_schemas import (
    AVS_NAME_CHARS,
    AVS_NAME_MAX_LENGTH,
    LOCATION_CHARS,
    LOCATION_MAX_LENGTH,
    RESOURCE_GROUP_CHARS,
    RESOURCE_GROUP_MAX_LENGTH,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "avs_cluster_list_impl",
    "avs_host_list_impl",
    "avs_privatecloud_list_impl",
    "avs_quota_show_impl",
    "cluster_row",
    "host_row",
    "private_cloud_row",
    "quota_answer",
]

Row = dict[str, Any]

_RESOURCE_GROUP: Final[re.Pattern[str]] = re.compile(RESOURCE_GROUP_CHARS)
_AVS_NAME: Final[re.Pattern[str]] = re.compile(AVS_NAME_CHARS)
_LOCATION: Final[re.Pattern[str]] = re.compile(LOCATION_CHARS)

#: The path segment before the resource group name in an Azure resource ID.
_RESOURCE_GROUPS_SEGMENT: Final[str] = "resourcegroups"


# ----------------------------------------------------------------------
# Rows
# ----------------------------------------------------------------------


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _object(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _texts(value: object) -> list[str]:
    """The non-empty texts in *value* when it is a list, else an empty list."""
    items = value if isinstance(value, list) else []
    return [item for item in items if isinstance(item, str) and item]


def _resource_group(resource_id: str | None) -> str | None:
    """The resource group named in an Azure resource ID, or ``None``."""
    parts = (resource_id or "").split("/")
    for index, part in enumerate(parts[:-1]):
        if part.lower() == _RESOURCE_GROUPS_SEGMENT:
            return parts[index + 1] or None
    return None


def private_cloud_row(item: Mapping[str, Any]) -> Row:
    """One row of ``azure.avs.privatecloud.list`` from one Azure ``PrivateCloud``."""
    props = _object(item.get("properties"))
    management = _object(props.get("managementCluster"))
    availability = _object(props.get("availability"))
    resource_id = _text(item.get("id"))
    return {
        "name": _text(item.get("name")),
        "resource_group": _resource_group(resource_id),
        "location": _text(item.get("location")),
        "id": resource_id,
        "sku": _text(_object(item.get("sku")).get("name")),
        "provisioning_state": _text(props.get("provisioningState")),
        "management_cluster_size": _int(management.get("clusterSize")),
        "management_cluster_hosts": _texts(management.get("hosts")),
        "network_block": _text(props.get("networkBlock")),
        "extended_network_blocks": _texts(props.get("extendedNetworkBlocks")),
        "internet": _text(props.get("internet")),
        "availability_strategy": _text(availability.get("strategy")),
        "availability_zone": _int(availability.get("zone")),
        "secondary_zone": _int(availability.get("secondaryZone")),
        "identity_type": _text(_object(item.get("identity")).get("type")),
        "vcf_license_kind": _text(_object(props.get("vcfLicense")).get("kind")),
    }


def cluster_row(item: Mapping[str, Any]) -> Row:
    """One row of ``azure.avs.cluster.list`` from one Azure ``Cluster``."""
    props = _object(item.get("properties"))
    return {
        "name": _text(item.get("name")),
        "id": _text(item.get("id")),
        "sku": _text(_object(item.get("sku")).get("name")),
        "cluster_size": _int(props.get("clusterSize")),
        "hosts": _texts(props.get("hosts")),
        "provisioning_state": _text(props.get("provisioningState")),
    }


def host_row(item: Mapping[str, Any]) -> Row:
    """One row of ``azure.avs.host.list`` from one Azure ``Host``."""
    props = _object(item.get("properties"))
    return {
        "name": _text(item.get("name")),
        "id": _text(item.get("id")),
        "display_name": _text(props.get("displayName")),
        "kind": _text(props.get("kind")),
        "provisioning_state": _text(props.get("provisioningState")),
        "fqdn": _text(props.get("fqdn")),
        "mo_ref_id": _text(props.get("moRefId")),
        "maintenance": _text(props.get("maintenance")),
        "fault_domain": _text(props.get("faultDomain")),
        "sku": _text(_object(item.get("sku")).get("name")),
        "zones": _texts(item.get("zones")),
    }


def quota_answer(answer: Mapping[str, Any], *, location: str) -> dict[str, Any]:
    """The answer of ``azure.avs.quota.show`` from one Azure ``Quota``.

    ``hostsRemaining`` maps each host type (SKU) to a host count. An entry
    whose count is not a whole number is left out.
    """
    remaining = _object(answer.get("hostsRemaining"))
    return {
        "location": location,
        "quota_enabled": _text(answer.get("quotaEnabled")),
        "hosts_remaining": {
            sku: count
            for sku, count in remaining.items()
            if isinstance(sku, str) and sku and _int(count) is not None
        },
    }


# ----------------------------------------------------------------------
# Calls
# ----------------------------------------------------------------------


def _checked(params: Mapping[str, Any], key: str, pattern: re.Pattern[str], max_length: int) -> str:
    """Return ``params[key]`` checked, then percent-encoded (special characters as ``%`` codes).

    The parameter schema already checks the same rule; this check keeps a
    direct call safe too. Raises ``ValueError`` without repeating the value.
    """
    value = params.get(key)
    if not isinstance(value, str) or len(value) > max_length or not pattern.fullmatch(value):
        raise ValueError(
            f"{key} is not a valid Azure name. Pass the name exactly as Azure shows it."
        )
    return quote(value, safe="")


def _resource_group_param(params: Mapping[str, Any]) -> str:
    return _checked(params, "resource_group", _RESOURCE_GROUP, RESOURCE_GROUP_MAX_LENGTH)


def _name_param(params: Mapping[str, Any], key: str) -> str:
    return _checked(params, key, _AVS_NAME, AVS_NAME_MAX_LENGTH)


async def _list(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    path: str,
    row: Callable[[Mapping[str, Any]], Row],
    **context: Any,
) -> dict[str, Any]:
    """Read every page of *path* and return one row per object, plus *context*.

    *context* holds plain values (the names the caller asked for). They stay
    next to the rows when a long list goes behind a link to the stored result.
    """
    paged = await list_all_pages(
        connector, target, path, operator=operator, api_version=AVS_API_VERSION
    )
    result = paged.as_dict()
    result["value"] = [row(item) for item in paged.rows if isinstance(item, dict)]
    return {**result, **context}


async def avs_privatecloud_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.avs.privatecloud.list``: every private cloud of the subscription."""
    del params  # the operation takes no parameters
    path = AVS_PRIVATE_CLOUDS_PATH.format(
        subscription_id=resolve_target_config(target).subscription_id
    )
    return await _list(connector, operator, target, path, private_cloud_row)


async def avs_cluster_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.avs.cluster.list``: the clusters of one private cloud, without the management one."""
    path = AVS_CLUSTERS_PATH.format(
        subscription_id=resolve_target_config(target).subscription_id,
        resource_group=_resource_group_param(params),
        private_cloud=_name_param(params, "private_cloud"),
    )
    return await _list(
        connector,
        operator,
        target,
        path,
        cluster_row,
        resource_group=params["resource_group"],
        private_cloud=params["private_cloud"],
    )


async def avs_host_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.avs.host.list``: the hosts of one cluster of a private cloud."""
    path = AVS_HOSTS_PATH.format(
        subscription_id=resolve_target_config(target).subscription_id,
        resource_group=_resource_group_param(params),
        private_cloud=_name_param(params, "private_cloud"),
        cluster=_name_param(params, "cluster"),
    )
    return await _list(
        connector,
        operator,
        target,
        path,
        host_row,
        resource_group=params["resource_group"],
        private_cloud=params["private_cloud"],
        cluster=params["cluster"],
    )


async def avs_quota_show_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.avs.quota.show``: the host quota of the subscription in one region.

    ``checkQuotaAvailability`` is a POST that only reads, so it is sent as a
    repeatable call (tried again after a 5xx, like a GET). It has no body.
    """
    location = _checked(params, "location", _LOCATION, LOCATION_MAX_LENGTH)
    response = await send(
        connector,
        target,
        "POST",
        AVS_QUOTA_PATH.format(
            subscription_id=resolve_target_config(target).subscription_id, location=location
        ),
        operator=operator,
        params={"api-version": AVS_API_VERSION},
        repeatable=True,
    )
    return quota_answer(json_payload_or_empty(response), location=params["location"])
