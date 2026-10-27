# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure network operations.

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.network.NetworkOps`. The operation
table and the guidance for the AI are in
:mod:`meho_backplane.connectors.azure.network`, the rows are built in
:mod:`meho_backplane.connectors.azure.network_rows`.

Every list is read with
:func:`~meho_backplane.connectors.azure._paging.list_all_pages`, so the
rows sit under ``value`` and a long list goes behind a link to the stored
result.

A resource group or a name goes into a URL path only after a check against
Microsoft's naming rules, and percent-encoded. A ``/``, a ``?`` or a name
such as ``..`` can therefore never move the call to another path or another
subscription.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._paging import PagedRows, list_all_pages
from meho_backplane.connectors.azure._paths import (
    EXPRESS_ROUTE_CIRCUITS_PATH,
    LOAD_BALANCERS_PATH,
    NETWORK_API_VERSION,
    NETWORK_SECURITY_GROUP_PATH,
    NETWORK_SECURITY_GROUPS_PATH,
    PRIVATE_DNS_API_VERSION,
    PRIVATE_DNS_ZONES_PATH,
    PUBLIC_IP_ADDRESSES_PATH,
    ROUTE_TABLES_PATH,
    VIRTUAL_NETWORK_GATEWAYS_PATH,
    VIRTUAL_NETWORK_PATH,
    VIRTUAL_NETWORKS_PATH,
)
from meho_backplane.connectors.azure.network_rows import (
    Row,
    express_route_row,
    gateway_row,
    load_balancer_row,
    nsg_detail,
    nsg_row,
    private_zone_row,
    public_ip_row,
    route_table_row,
    vnet_detail,
    vnet_row,
)
from meho_backplane.connectors.azure.network_schemas import (
    RESOURCE_GROUP_CHARS,
    RESOURCE_GROUP_MAX_LENGTH,
    RESOURCE_NAME_CHARS,
    SECURITY_GROUP_NAME_MAX_LENGTH,
    VIRTUAL_NETWORK_NAME_MAX_LENGTH,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "network_expressroute_list_impl",
    "network_gateway_list_impl",
    "network_lb_list_impl",
    "network_nsg_list_impl",
    "network_nsg_show_impl",
    "network_privatedns_list_impl",
    "network_publicip_list_impl",
    "network_routetable_list_impl",
    "network_vnet_list_impl",
    "network_vnet_show_impl",
]

_RESOURCE_GROUP: Final[re.Pattern[str]] = re.compile(RESOURCE_GROUP_CHARS)
_RESOURCE_NAME: Final[re.Pattern[str]] = re.compile(RESOURCE_NAME_CHARS)


# ----------------------------------------------------------------------
# Calls
# ----------------------------------------------------------------------


def _checked(params: Mapping[str, Any], key: str, pattern: re.Pattern[str], max_length: int) -> str:
    """Return ``params[key]`` checked and percent-encoded for a URL path.

    The parameter schema already checks the same rule; this check keeps a
    direct call safe too. Raises ``ValueError`` without repeating the value.
    """
    value = params.get(key)
    if not isinstance(value, str) or len(value) > max_length or not pattern.fullmatch(value):
        raise ValueError(
            f"{key} is not a valid Azure name. Pass the name exactly as Azure shows it."
        )
    return quote(value, safe="")


def _shaped(paged: PagedRows, row: Callable[[Mapping[str, Any]], Row]) -> dict[str, Any]:
    """The answer of a list: one row per object, and how the paging went."""
    result = paged.as_dict()
    result["value"] = [row(item) for item in paged.rows if isinstance(item, dict)]
    return result


async def _list(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    path: str,
    api_version: str,
    row: Callable[[Mapping[str, Any]], Row],
) -> dict[str, Any]:
    paged = await list_all_pages(
        connector, target, path, operator=operator, api_version=api_version
    )
    return _shaped(paged, row)


def _subscription_path(target: AzureTargetLike, template: str) -> str:
    return template.format(subscription_id=resolve_target_config(target).subscription_id)


async def _get_one(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: Mapping[str, Any],
    template: str,
    name_max_length: int,
) -> dict[str, Any]:
    """GET one resource by ``resource_group`` and ``name`` (checked, encoded)."""
    path = template.format(
        subscription_id=resolve_target_config(target).subscription_id,
        resource_group=_checked(
            params, "resource_group", _RESOURCE_GROUP, RESOURCE_GROUP_MAX_LENGTH
        ),
        name=_checked(params, "name", _RESOURCE_NAME, name_max_length),
    )
    return await connector.arm_get(target, path, operator=operator, api_version=NETWORK_API_VERSION)


async def network_vnet_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.vnet.list``: every virtual network of the subscription."""
    del params  # the operation takes no parameters
    path = _subscription_path(target, VIRTUAL_NETWORKS_PATH)
    return await _list(connector, operator, target, path, NETWORK_API_VERSION, vnet_row)


async def network_vnet_show_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.vnet.show``: one virtual network with its subnets and peerings."""
    payload = await _get_one(
        connector, operator, target, params, VIRTUAL_NETWORK_PATH, VIRTUAL_NETWORK_NAME_MAX_LENGTH
    )
    return vnet_detail(payload)


async def network_nsg_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.nsg.list``: every network security group of the subscription."""
    del params
    path = _subscription_path(target, NETWORK_SECURITY_GROUPS_PATH)
    return await _list(connector, operator, target, path, NETWORK_API_VERSION, nsg_row)


async def network_nsg_show_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.nsg.show``: one network security group with every rule."""
    payload = await _get_one(
        connector,
        operator,
        target,
        params,
        NETWORK_SECURITY_GROUP_PATH,
        SECURITY_GROUP_NAME_MAX_LENGTH,
    )
    return nsg_detail(payload)


async def network_lb_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.lb.list``: every load balancer of the subscription."""
    del params
    path = _subscription_path(target, LOAD_BALANCERS_PATH)
    return await _list(connector, operator, target, path, NETWORK_API_VERSION, load_balancer_row)


async def network_publicip_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.publicip.list``: every public IP address of the subscription."""
    del params
    path = _subscription_path(target, PUBLIC_IP_ADDRESSES_PATH)
    return await _list(connector, operator, target, path, NETWORK_API_VERSION, public_ip_row)


async def network_gateway_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.gateway.list``: the VPN and ExpressRoute gateways of one resource group."""
    path = VIRTUAL_NETWORK_GATEWAYS_PATH.format(
        subscription_id=resolve_target_config(target).subscription_id,
        resource_group=_checked(
            params, "resource_group", _RESOURCE_GROUP, RESOURCE_GROUP_MAX_LENGTH
        ),
    )
    return await _list(connector, operator, target, path, NETWORK_API_VERSION, gateway_row)


async def network_expressroute_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.expressroute.list``: every ExpressRoute circuit, without its keys."""
    del params
    path = _subscription_path(target, EXPRESS_ROUTE_CIRCUITS_PATH)
    return await _list(connector, operator, target, path, NETWORK_API_VERSION, express_route_row)


async def network_routetable_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.routetable.list``: every route table with its routes."""
    del params
    path = _subscription_path(target, ROUTE_TABLES_PATH)
    return await _list(connector, operator, target, path, NETWORK_API_VERSION, route_table_row)


async def network_privatedns_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.privatedns.list``: every private DNS zone of the subscription."""
    del params
    path = _subscription_path(target, PRIVATE_DNS_ZONES_PATH)
    return await _list(connector, operator, target, path, PRIVATE_DNS_API_VERSION, private_zone_row)
