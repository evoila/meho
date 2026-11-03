# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The two phpIPAM reads: ``phpipam.subnet.list`` and ``phpipam.subnet.free_addresses``.

Both are ``safe``. A list longer than 50 rows comes back as a link to the
stored result (the dispatcher does that for every list); the agent reads more
rows with ``result_query``.

``phpipam.subnet.free_addresses`` works out the free addresses itself: it
reads the subnet and its used addresses, then walks the subnet from the start
and skips every used address. That is the same rule phpIPAM's own "first
free address" uses, and it needs only two reads however large the subnet is.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.phpipam._common import (
    PhpipamRefusedError,
    Subnet,
    as_int,
    normalize_ip,
    read_subnet,
    rows,
)
from meho_backplane.connectors.phpipam._errors import PhpipamApiError
from meho_backplane.connectors.phpipam._paths import (
    _SUBNET_ADDRESSES_PATH,
    _SUBNET_CIDR_PATH,
    _SUBNET_SLAVES_PATH,
    _SUBNET_USAGE_PATH,
    _SUBNETS_PATH,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.phpipam.connector import PhpipamConnector
    from meho_backplane.connectors.phpipam.session import PhpipamTargetLike

__all__ = ["free_addresses_impl", "subnet_list_impl"]

#: How many subnets get their usage read (one call each). Narrow the list
#: with ``cidr`` to see the usage of others.
USAGE_ROWS_CAP: Final[int] = 50

#: At most this many usage reads run at the same time.
_USAGE_CONCURRENCY: Final[int] = 4

DEFAULT_SUBNET_LIMIT: Final[int] = 100
DEFAULT_FREE_LIMIT: Final[int] = 20
MAX_LIMIT: Final[int] = 1000


def _limit(params: dict[str, Any], default: int) -> int:
    value = params.get("limit", default)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_LIMIT:
        raise PhpipamRefusedError(f"limit must be a whole number from 1 to {MAX_LIMIT}")
    return value


def _subnet_id(params: dict[str, Any]) -> int:
    value = params.get("subnet_id")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PhpipamRefusedError("subnet_id must be the phpIPAM id of a subnet (1 or more)")
    return value


async def _usage(
    conn: PhpipamConnector,
    operator: Operator,
    target: PhpipamTargetLike,
    subnet_id: int,
    gate: asyncio.Semaphore,
) -> dict[str, Any] | None:
    """Read how full one subnet is; ``None`` when phpIPAM answers with an error."""
    async with gate:
        try:
            body = await conn.api_call(
                target, operator, "GET", _SUBNET_USAGE_PATH, {"subnet_id": subnet_id}
            )
        except PhpipamApiError:
            return None
    data = (body or {}).get("data")
    return data if isinstance(data, dict) else None


def _subnet_row(subnet: Subnet, usage: dict[str, Any] | None) -> dict[str, Any]:
    used = as_int((usage or {}).get("used"))
    free = as_int((usage or {}).get("freehosts"))
    max_hosts = as_int((usage or {}).get("maxhosts"))
    used_percent = round(used * 100.0 / max_hosts, 2) if used is not None and max_hosts else None
    return {
        "id": subnet.id,
        "cidr": subnet.cidr,
        "description": subnet.description,
        "section_id": subnet.section_id,
        "is_full": subnet.is_full,
        "used": used,
        "free": free,
        "max_hosts": max_hosts,
        "used_percent": used_percent,
    }


async def subnet_list_impl(
    conn: PhpipamConnector,
    operator: Operator,
    target: PhpipamTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``phpipam.subnet.list``: subnets (folders left out) and how full each is."""
    limit = _limit(params, DEFAULT_SUBNET_LIMIT)
    cidr = params.get("cidr")
    if cidr is not None:
        try:
            network = ipaddress.ip_network(str(cidr), strict=True)
        except ValueError:
            raise PhpipamRefusedError(
                f"cidr must be a network in CIDR form, for example 192.0.2.0/24; got {cidr!r}"
            ) from None
        body = await conn.api_call(
            target,
            operator,
            "GET",
            _SUBNET_CIDR_PATH,
            {"subnet": str(network.network_address), "mask": network.prefixlen},
            not_found_ok=True,
        )
    else:
        body = await conn.api_call(target, operator, "GET", _SUBNETS_PATH)
    subnets = [
        subnet
        for subnet in (Subnet.from_row(row) for row in rows((body or {}).get("data")))
        if subnet is not None and not subnet.is_folder and subnet.network is not None
    ]
    shown = subnets[:limit]
    gate = asyncio.Semaphore(_USAGE_CONCURRENCY)
    with_usage = shown[:USAGE_ROWS_CAP]
    usages = await asyncio.gather(
        *(_usage(conn, operator, target, subnet.id, gate) for subnet in with_usage)
    )
    usage_by_id = {subnet.id: usage for subnet, usage in zip(with_usage, usages, strict=True)}
    return {
        "total": len(subnets),
        "returned": len(shown),
        "truncated": len(subnets) > len(shown),
        "usage_rows": len(with_usage),
        "subnets": [_subnet_row(subnet, usage_by_id.get(subnet.id)) for subnet in shown],
    }


async def _refuse_parent_subnet(
    conn: PhpipamConnector, operator: Operator, target: PhpipamTargetLike, subnet: Subnet
) -> None:
    """Refuse a subnet that holds child subnets: its addresses live in the children."""
    body = await conn.api_call(
        target, operator, "GET", _SUBNET_SLAVES_PATH, {"subnet_id": subnet.id}, not_found_ok=True
    )
    children = [as_int(row.get("id")) for row in rows((body or {}).get("data"))]
    children = [child for child in children if child is not None]
    if children:
        raise PhpipamRefusedError(
            f"phpIPAM subnet {subnet.cidr} (id {subnet.id}) holds child subnets; its addresses "
            f"live in them (ids {children[:10]})",
            remediation="Ask for the free addresses of one child subnet.",
        )


async def used_addresses(
    conn: PhpipamConnector, operator: Operator, target: PhpipamTargetLike, subnet_id: int
) -> list[dict[str, Any]]:
    """The used address rows of one subnet (phpIPAM answers 404 for none)."""
    body = await conn.api_call(
        target,
        operator,
        "GET",
        _SUBNET_ADDRESSES_PATH,
        {"subnet_id": subnet_id},
        not_found_ok=True,
    )
    return rows((body or {}).get("data"))


async def free_addresses_impl(
    conn: PhpipamConnector,
    operator: Operator,
    target: PhpipamTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``phpipam.subnet.free_addresses``: the first free addresses of one subnet."""
    subnet_id = _subnet_id(params)
    limit = _limit(params, DEFAULT_FREE_LIMIT)
    subnet = await read_subnet(conn, operator, target, subnet_id)
    await _refuse_parent_subnet(conn, operator, target, subnet)
    first, last = subnet.usable_range()
    used: set[int] = set()
    for row in await used_addresses(conn, operator, target, subnet_id):
        ip = normalize_ip(row.get("ip"))
        if ip is not None and subnet.contains(ip):
            number = int(ipaddress.ip_address(ip))
            if first <= number <= last:
                used.add(number)
    free_count = 0 if subnet.is_full else (last - first + 1) - len(used)
    found: list[dict[str, str]] = []
    if not subnet.is_full:
        number = first
        while number <= last and len(found) < limit:
            if number not in used:
                found.append({"ip": subnet.address_at(number)})
            number += 1
    return {
        "subnet_id": subnet.id,
        "cidr": subnet.cidr,
        "description": subnet.description,
        "is_full": subnet.is_full,
        "free_count": free_count,
        "returned": len(found),
        "truncated": free_count > len(found),
        "addresses": found,
    }
