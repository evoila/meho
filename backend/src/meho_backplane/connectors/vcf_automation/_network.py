# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers for a tenant project's own network (VCF Automation tenant plane).

Four reads (vSphere networks, computes, regions, cloud zones) and two
changes (the address settings and tags of one vSphere network, a network
profile). Metadata lives in :mod:`.network_ops`.

Every ``/iaas/api`` call here sends ``apiVersion`` (:data:`._paths.TENANT_IAAS_API_VERSION`).
The changes return the same envelope as the provisioning writes::

    {"status": "updated" | "created" | "unchanged" | "invalid_request", <resource>, "guidance"}

An upstream 4xx/5xx raises :exc:`httpx.HTTPStatusError`; the dispatcher maps it
to ``connector_error`` with ``http_status`` and ``upstream_message``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vcf_automation._lookups import find_by_odata_name, quote_segment
from meho_backplane.connectors.vcf_automation._paths import (
    TENANT_FABRIC_COMPUTES_PATH,
    TENANT_FABRIC_NETWORK_PATH,
    TENANT_FABRIC_NETWORKS_PATH,
    TENANT_IAAS_API_VERSION,
    TENANT_NETWORK_PROFILES_PATH,
    TENANT_REGIONS_PATH,
    TENANT_ZONES_PATH,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "fabric_network_patch_body",
    "network_profile_create_body",
    "tenant_fabric_compute_list",
    "tenant_fabric_network_list",
    "tenant_fabric_network_update",
    "tenant_network_profile_create",
    "tenant_region_list",
    "tenant_zone_list",
]

_API_VERSION: Final[dict[str, str]] = {"apiVersion": TENANT_IAAS_API_VERSION}
_LIST_KEYS: Final = ("$filter", "$top", "$skip")
#: Op param -> IaaS field for the plain fields of a network change.
_NETWORK_FIELDS: Final[dict[str, str]] = {
    "cidr": "cidr",
    "default_gateway": "defaultGateway",
    "dns_server_addresses": "dnsServerAddresses",
    "domain": "domain",
}


async def _list(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    path: str,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    query = {key: params[key] for key in _LIST_KEYS if params.get(key) is not None}
    return await connector._request_json(
        target, "GET", path, operator=operator, params={**_API_VERSION, **query}
    )


async def tenant_fabric_network_list(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.tenant.fabric_network.list`` — ``GET /iaas/api/fabric-networks-vsphere``."""
    return await _list(connector, operator, target, TENANT_FABRIC_NETWORKS_PATH, params)


async def tenant_fabric_compute_list(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.tenant.fabric_compute.list`` — ``GET /iaas/api/fabric-computes``."""
    return await _list(connector, operator, target, TENANT_FABRIC_COMPUTES_PATH, params)


async def tenant_region_list(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.tenant.region.list`` — ``GET /iaas/api/regions``."""
    return await _list(connector, operator, target, TENANT_REGIONS_PATH, params)


async def tenant_zone_list(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.tenant.zone.list`` — ``GET /iaas/api/zones``."""
    return await _list(connector, operator, target, TENANT_ZONES_PATH, params)


# ---------------------------------------------------------------------------
# vSphere network change
# ---------------------------------------------------------------------------


def _tag(item: Mapping[str, Any]) -> dict[str, str]:
    return {"key": str(item["key"]), "value": str(item.get("value") or "")}


def fabric_network_patch_body(
    current: Mapping[str, Any], params: Mapping[str, Any]
) -> dict[str, Any]:
    """The ``PATCH`` body: only the fields whose value would change.

    ``add_tags`` is added to the tags the network has now. The ``PATCH``
    sends the whole tag list, so the existing tags are sent back and no tag
    is lost.
    """
    body: dict[str, Any] = {
        wire: params[param]
        for param, wire in _NETWORK_FIELDS.items()
        if param in params and params[param] != current.get(wire)
    }
    raw = current.get("tags")
    tags = (
        [_tag(t) for t in raw if isinstance(t, dict) and "key" in t]
        if isinstance(raw, list)
        else []
    )
    added = False
    for item in params.get("add_tags") or []:
        tag = _tag(item)
        if tag not in tags:
            tags.append(tag)
            added = True
    if added:
        body["tags"] = tags
    return body


def _network_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in ("id", "name", "cidr", "defaultGateway", "dnsServerAddresses", "domain", "tags")
    }


async def tenant_fabric_network_update(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.tenant.fabric_network.update`` — read, then ``PATCH`` only what differs."""
    if not any(key in params for key in (*_NETWORK_FIELDS, "add_tags")):
        return {
            "status": "invalid_request",
            "fabric_network": None,
            "changed": [],
            "guidance": (
                "give at least one of cidr, default_gateway, dns_server_addresses, "
                "domain or add_tags"
            ),
        }
    path = TENANT_FABRIC_NETWORK_PATH.format(id=quote_segment(params["id"]))
    current = await connector._request_json(
        target, "GET", path, operator=operator, params=_API_VERSION
    )
    body = fabric_network_patch_body(current, params)
    if not body:
        return {
            "status": "unchanged",
            "fabric_network": _network_view(current),
            "changed": [],
            "guidance": "the network already has these values; nothing was written",
        }
    updated = await connector._post_json(
        target, path, operator=operator, verb="PATCH", params=_API_VERSION, json=body
    )
    return {
        "status": "updated",
        "fabric_network": _network_view(updated or {**current, **body}),
        "changed": sorted(body),
        "guidance": None,
    }


# ---------------------------------------------------------------------------
# Network profile create
# ---------------------------------------------------------------------------


def network_profile_create_body(params: Mapping[str, Any]) -> dict[str, Any]:
    """The ``POST /iaas/api/network-profiles`` body (``NetworkProfileSpecification``)."""
    body: dict[str, Any] = {"name": params["name"], "regionId": params["region_id"]}
    if params.get("description"):
        body["description"] = params["description"]
    if params.get("fabric_network_ids"):
        body["fabricNetworkIds"] = list(params["fabric_network_ids"])
    if params.get("tags"):
        body["tags"] = [_tag(t) for t in params["tags"]]
    return body


def _profile_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in ("id", "name", "externalRegionId")}


async def tenant_network_profile_create(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.tenant.network_profile.create`` — ``POST /iaas/api/network-profiles``.

    Safe to run twice: a profile with the same name answers ``unchanged``
    and nothing is written.
    """
    existing = await find_by_odata_name(
        connector,
        target,
        operator,
        TENANT_NETWORK_PROFILES_PATH,
        params["name"],
        params=_API_VERSION,
    )
    if existing is not None:
        return {
            "status": "unchanged",
            "network_profile": _profile_view(existing),
            "guidance": (
                f"a network profile named {existing.get('name')!r} already exists; "
                "nothing was written"
            ),
        }
    created = await connector._post_json(
        target,
        TENANT_NETWORK_PROFILES_PATH,
        operator=operator,
        params=_API_VERSION,
        json=network_profile_create_body(params),
    )
    return {"status": "created", "network_profile": _profile_view(created), "guidance": None}
