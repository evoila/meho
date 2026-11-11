# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure public DNS reads (#5368).

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.dns.DnsOps`. Every function only
reads. :func:`find_record_set` is also used by the writes, to show and keep
the values a record set had before a change.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._errors import AzureNotFoundError
from meho_backplane.connectors.azure._paging import PagedRows, list_all_pages
from meho_backplane.connectors.azure._paths import (
    DNS_API_VERSION,
    DNS_RECORD_SETS_PATH,
    DNS_ZONES_PATH,
)
from meho_backplane.connectors.azure.dns_parse import checked_group_and_zone, record_set_path
from meho_backplane.connectors.azure.dns_rows import record_set_detail, record_set_row, zone_row
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config
from meho_backplane.connectors.base import ConnectorUpstreamError

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "dns_record_set_list_impl",
    "dns_record_set_show_impl",
    "dns_zone_list_impl",
    "find_record_set",
]


def _shaped(paged: PagedRows, row: Callable[[Mapping[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    result = paged.as_dict()
    result["value"] = [row(item) for item in paged.rows if isinstance(item, dict)]
    return result


async def dns_zone_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.dns.zone.list``: every public DNS zone of the subscription."""
    del params
    path = DNS_ZONES_PATH.format(subscription_id=resolve_target_config(target).subscription_id)
    paged = await list_all_pages(
        connector, target, path, operator=operator, api_version=DNS_API_VERSION
    )
    return _shaped(paged, zone_row)


async def dns_record_set_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.dns.recordset.list``: every record set of one zone, of every type."""
    group, zone = checked_group_and_zone(params)
    path = DNS_RECORD_SETS_PATH.format(
        subscription_id=resolve_target_config(target).subscription_id,
        resource_group=quote(group, safe=""),
        zone=quote(zone, safe=""),
    )
    paged = await list_all_pages(
        connector, target, path, operator=operator, api_version=DNS_API_VERSION
    )
    result = _shaped(paged, record_set_row)
    result["zone"] = zone
    return result


async def find_record_set(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, path: str
) -> dict[str, Any] | None:
    """Read one record set. ``None`` when Azure has none (HTTP 404); an error otherwise.

    An answer that is not a record set (an empty body) is an error too: it
    must never read as "no such record set".
    """
    try:
        item = await connector.arm_get(target, path, operator=operator, api_version=DNS_API_VERSION)
    except AzureNotFoundError:
        return None
    if not isinstance(item.get("name"), str) or not isinstance(item.get("properties"), dict):
        raise ConnectorUpstreamError(
            f"Azure target {target.name!r}: Azure answered the record set read with "
            "something that is not a record set. MEHO changed nothing.",
            http_status=200,
            upstream_message=None,
        )
    return item


async def dns_record_set_show_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.dns.recordset.show``: one record set with its records and etag."""
    path, _group, zone, _type, _name = record_set_path(
        resolve_target_config(target).subscription_id, params
    )
    item = await find_record_set(connector, operator, target, path)
    if item is None:
        raise AzureNotFoundError(
            [path],
            f"Azure target {target.name!r}: zone {zone!r} has no such record set. "
            "Check the names with azure.network.dns.recordset.list.",
        )
    return {**record_set_detail(item), "zone": zone}
