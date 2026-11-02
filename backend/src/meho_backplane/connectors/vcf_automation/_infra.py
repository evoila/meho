# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Infrastructure reads and the cloud account changes, for Aria Automation 8 and VCF Automation 9.

Aria Automation 8.18 and the tenant plane of a VCF Automation 9 "VM Apps"
org serve the same IaaS API (``/iaas/api``). So each handler here is written
once and runs on both connectors:
:class:`~meho_backplane.connectors.vcf_automation.connector.VcfAutomationConnector`
(op ids ``vcfa.tenant.*``) and :class:`~meho_backplane.connectors.vra8.connector.Vra8Connector`
(op ids ``vcfa.vra8.*``). A handler needs only the connector's
``_request_json`` (GET) and ``_post_json`` (POST, PATCH, DELETE); each
connector adds its own login. ``prefix`` is the op id prefix of the calling
connector, so the guidance names that connector's ops.

The zone, flavor profile, member and project changes are in
:mod:`._infra_changes`; the metadata is in :mod:`.infra_ops`.

* Every call sends ``apiVersion`` (:data:`._paths.TENANT_IAAS_API_VERSION`).
* A caller id in a URL path is checked (not empty, not ``.`` or ``..``) and
  percent-encoded (:func:`segment`).
* A list comes back as flat rows under ``items``; MEHO stores a long list
  behind a result handle.
* The cloud account password is read from Vault (``password_secret_ref``)
  under the caller's identity, only when the approved call runs. It goes
  only into the create body. A vendor error that repeats it gets a marker
  in its place. A result never holds it.

An upstream 4xx/5xx raises :exc:`httpx.HTTPStatusError`; the dispatcher maps
it to ``connector_error`` with the vendor's message.

This module also registers the preview of the two cloud account deletes
(``vcfa.tenant.cloud_account.delete`` and ``vcfa.vra8.cloud_account.delete``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.connectors.vcf_automation._infra_paths import (
    CLOUD_ACCOUNT_PATH,
    CLOUD_ACCOUNTS_NSXT_PATH,
    CLOUD_ACCOUNTS_PATH,
    CLOUD_ACCOUNTS_VSPHERE_PATH,
    FLAVOR_PROFILES_PATH,
    NETWORK_IP_RANGES_PATH,
    NETWORK_PROFILES_PATH,
    STORAGE_PROFILES_PATH,
    ZONES_PATH,
)
from meho_backplane.connectors.vcf_automation._lookups import (
    find_by_odata_name,
    password_missing_guidance,
    read_password,
)
from meho_backplane.connectors.vcf_automation._paths import TENANT_IAAS_API_VERSION
from meho_backplane.operations._preview import PreviewContext, register_preview_builder
from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER

__all__ = [
    "API_VERSION",
    "cloud_account_create",
    "cloud_account_delete",
    "cloud_account_delete_preview",
    "cloud_account_list",
    "flavor_profile_list",
    "flavor_rows",
    "linked_id",
    "list_rows",
    "network_profile_list",
    "segment",
    "storage_profile_list",
    "tags_of",
    "zone_list",
    "zone_row",
]

API_VERSION: Final[dict[str, str]] = {"apiVersion": TENANT_IAAS_API_VERSION}
#: Page size and page limit of a paged IaaS list (10,000 rows at most).
_PAGE_SIZE: Final[int] = 200
_MAX_PAGES: Final[int] = 50
_FABRIC_NETWORK_HREF: Final = re.compile(r"^/iaas/api/fabric-networks(?:-vsphere)?/([^/?#]+)$")


def segment(value: Any) -> str:
    """Check one caller value for a URL path and percent-encode it.

    Refuses an empty value, ``.`` and ``..``: they would point the request
    at another path. Encodes everything else, also ``/``.
    """
    text = "" if value is None else str(value)
    if not text.strip() or text in {".", ".."}:
        raise ValueError(f"the id {text!r} is not valid; give the id of one object")
    return quote(text, safe="")


def _content(answer: Any) -> list[dict[str, Any]]:
    rows = answer.get("content") if isinstance(answer, Mapping) else None
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


async def list_rows(
    connector: Any,
    operator: Any,
    target: Any,
    path: str,
    params: Mapping[str, Any],
    *,
    paged: bool,
) -> tuple[list[dict[str, Any]], Any]:
    """The rows of an IaaS list and its ``totalElements``.

    With ``$top`` or ``$skip`` in *params*, one page as asked. Else, on a
    *paged* list, every page (at most ``_MAX_PAGES``). A list the vendor
    does not page is read once.
    """
    query: dict[str, Any] = dict(API_VERSION)
    if params.get("$filter"):
        query["$filter"] = params["$filter"]
    asked = {key: params[key] for key in ("$top", "$skip") if params.get(key) is not None}
    if asked or not paged:
        answer = await connector._request_json(
            target, "GET", path, operator=operator, params={**query, **asked}
        )
        return _content(answer), answer.get("totalElements")
    rows: list[dict[str, Any]] = []
    seen: set[Any] = set()
    total: Any = None
    for page in range(_MAX_PAGES):
        answer = await connector._request_json(
            target,
            "GET",
            path,
            operator=operator,
            params={**query, "$top": _PAGE_SIZE, "$skip": page * _PAGE_SIZE},
        )
        content = [
            row for row in _content(answer) if row.get("id") not in seen or not row.get("id")
        ]
        rows.extend(content)
        seen.update(row.get("id") for row in content)
        total = answer.get("totalElements")
        if len(content) < _PAGE_SIZE or (isinstance(total, int) and len(rows) >= total):
            break
    return rows, total


def _answer(rows: list[dict[str, Any]], total: Any) -> dict[str, Any]:
    return {"items": rows, "totalElements": total if isinstance(total, int) else len(rows)}


def _hrefs(row: Mapping[str, Any]) -> list[str]:
    """Every href in the row's ``_links``."""
    links = row.get("_links")
    found: list[str] = []
    for link in links.values() if isinstance(links, Mapping) else ():
        if isinstance(link, Mapping):
            candidates = (link.get("href"), *(link.get("hrefs") or []))
            found.extend(href for href in candidates if isinstance(href, str))
    return found


def linked_id(row: Mapping[str, Any], prefix: str) -> str | None:
    """The id the first ``_links`` href under *prefix* (an IaaS path) points to."""
    for href in _hrefs(row):
        if href.startswith(prefix):
            tail = href[len(prefix) :].split("/", 1)[0].split("?", 1)[0]
            if tail:
                return tail
    return None


def _fabric_network_ids(row: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for href in _hrefs(row):
        match = _FABRIC_NETWORK_HREF.match(href)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def tags_of(value: Any) -> list[dict[str, Any]]:
    """The ``{key, value}`` tags of a vendor tag list."""
    if not isinstance(value, list):
        return []
    return [
        {"key": tag.get("key"), "value": tag.get("value")}
        for tag in value
        if isinstance(tag, Mapping) and tag.get("key")
    ]


def _common(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "description": row.get("description"),
        "externalRegionId": row.get("externalRegionId"),
        "cloudAccountId": row.get("cloudAccountId"),
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _region_row(region: Mapping[str, Any]) -> dict[str, Any]:
    return {key: region.get(key) for key in ("id", "externalRegionId", "name")}


def _cloud_account_row(row: Mapping[str, Any]) -> dict[str, Any]:
    properties = row.get("cloudAccountProperties")
    host = properties.get("hostName") if isinstance(properties, Mapping) else None
    regions = row.get("enabledRegions")
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "type": row.get("cloudAccountType"),
        "description": row.get("description"),
        "hostName": host if isinstance(host, str) else None,
        "healthy": row.get("healthy"),
        "inMaintenanceMode": row.get("inMaintenanceMode"),
        "regions": [_region_row(r) for r in regions if isinstance(r, Mapping)]
        if isinstance(regions, list)
        else [],
        "tags": tags_of(row.get("tags")),
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


async def cloud_account_list(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``GET /iaas/api/cloud-accounts`` as flat rows; only the host name of the properties."""
    del prefix
    rows, total = await list_rows(
        connector, operator, target, CLOUD_ACCOUNTS_PATH, params, paged=True
    )
    return _answer([_cloud_account_row(row) for row in rows], total)


def zone_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One cloud zone as a flat row."""
    return {
        **_common(row),
        "regionId": linked_id(row, "/iaas/api/regions/"),
        "placementPolicy": row.get("placementPolicy"),
        "folder": row.get("folder"),
        "tagsToMatch": tags_of(row.get("tagsToMatch")),
        "tags": tags_of(row.get("tags")),
        "updatedAt": row.get("updatedAt"),
    }


async def zone_list(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``GET /iaas/api/zones`` as flat rows."""
    del prefix
    rows, total = await list_rows(connector, operator, target, ZONES_PATH, params, paged=True)
    return _answer([zone_row(row) for row in rows], total)


def flavor_rows(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The flavors of one flavor profile: ``{key, name, cpuCount, memoryInMB}``."""
    mappings = row.get("flavorMappings")
    mapping = mappings.get("mapping") if isinstance(mappings, Mapping) else None
    if not isinstance(mapping, Mapping):
        return []
    return [
        {
            "key": key,
            "name": flavor.get("name"),
            "cpuCount": flavor.get("cpuCount"),
            "memoryInMB": flavor.get("memoryInMB"),
        }
        for key, flavor in sorted(mapping.items())
        if isinstance(flavor, Mapping)
    ]


async def flavor_profile_list(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``GET /iaas/api/flavor-profiles`` as flat rows, one per region profile."""
    del prefix, params
    rows, total = await list_rows(
        connector, operator, target, FLAVOR_PROFILES_PATH, {}, paged=False
    )
    return _answer(
        [
            {
                **_common(row),
                "regionId": linked_id(row, "/iaas/api/regions/"),
                "flavors": flavor_rows(row),
                "updatedAt": row.get("updatedAt"),
            }
            for row in rows
        ],
        total,
    )


def _ip_range_row(row: Mapping[str, Any]) -> dict[str, Any]:
    networks = _fabric_network_ids(row)
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "startIPAddress": row.get("startIPAddress"),
        "endIPAddress": row.get("endIPAddress"),
        "ipVersion": row.get("ipVersion"),
        "fabricNetworkId": networks[0] if networks else None,
    }


async def network_profile_list(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``GET /iaas/api/network-profiles`` and ``GET /iaas/api/network-ip-ranges``.

    One row per profile, with the ids of its networks and the IP ranges of
    those networks.
    """
    del prefix
    rows, total = await list_rows(
        connector, operator, target, NETWORK_PROFILES_PATH, params, paged=True
    )
    ranges, _ = await list_rows(
        connector, operator, target, NETWORK_IP_RANGES_PATH, {}, paged=False
    )
    range_rows = [_ip_range_row(row) for row in ranges]
    items = []
    for row in rows:
        networks = _fabric_network_ids(row)
        items.append(
            {
                **_common(row),
                "isolationType": row.get("isolationType"),
                "fabricNetworkIds": networks,
                "ipRanges": [r for r in range_rows if r["fabricNetworkId"] in networks],
                "tags": tags_of(row.get("tags")),
            }
        )
    return _answer(items, total)


async def storage_profile_list(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``GET /iaas/api/storage-profiles`` as flat rows."""
    del prefix, params
    rows, total = await list_rows(
        connector, operator, target, STORAGE_PROFILES_PATH, {}, paged=False
    )
    return _answer(
        [
            {
                **_common(row),
                "defaultItem": row.get("defaultItem"),
                "supportsEncryption": row.get("supportsEncryption"),
                "diskProperties": row.get("diskProperties")
                if isinstance(row.get("diskProperties"), Mapping)
                else {},
                "tags": tags_of(row.get("tags")),
            }
            for row in rows
        ],
        total,
    )


# ---------------------------------------------------------------------------
# Add a cloud account
# ---------------------------------------------------------------------------

#: Op param -> body field, for both account types.
_ACCOUNT_FIELDS: Final[dict[str, str]] = {
    "description": "description",
    "dcid": "dcid",
    "associated_cloud_account_ids": "associatedCloudAccountIds",
}
#: Op param -> body field, per account type.
_TYPE_FIELDS: Final[dict[str, dict[str, str]]] = {
    "vsphere": {"create_default_zones": "createDefaultZones"},
    "nsx-t": {"manager_mode": "managerMode", "is_global_manager": "isGlobalManager"},
}
_TYPE_PATHS: Final[dict[str, str]] = {
    "vsphere": CLOUD_ACCOUNTS_VSPHERE_PATH,
    "nsx-t": CLOUD_ACCOUNTS_NSXT_PATH,
}


def cloud_account_body(params: Mapping[str, Any], password: str) -> dict[str, Any]:
    """The ``CloudAccountVsphereSpecification`` or ``CloudAccountNsxTSpecification`` body."""
    kind = params["type"]
    body: dict[str, Any] = {
        "name": params["name"],
        "hostName": params["host_name"],
        "username": params["username"],
        "password": password,
        "acceptSelfSignedCertificate": bool(params.get("accept_self_signed_certificate")),
    }
    for param, wire in {**_ACCOUNT_FIELDS, **_TYPE_FIELDS[kind]}.items():
        if params.get(param) not in (None, "", []):
            body[wire] = params[param]
    if kind == "vsphere":
        body["regions"] = [
            {"externalRegionId": r["external_region_id"], "name": r["name"]}
            for r in params.get("regions") or []
        ]
    if params.get("tags"):
        body["tags"] = [{"key": t["key"], "value": t.get("value") or ""} for t in params["tags"]]
    return body


def _without_secret(exc: httpx.HTTPStatusError, secret: str) -> httpx.HTTPStatusError:
    """The same error with the secret in the vendor text replaced by a marker."""
    text = exc.response.text.replace(secret, SECRET_FIELD_MARKER)
    headers = {k: v for k, v in exc.response.headers.items() if k.lower() == "content-type"}
    response = httpx.Response(
        exc.response.status_code, headers=headers, content=text.encode(), request=exc.request
    )
    return httpx.HTTPStatusError(str(exc), request=exc.request, response=response)


def _tracker_view(tracker: Any) -> dict[str, Any] | None:
    if not isinstance(tracker, Mapping) or not tracker:
        return None
    return {key: tracker.get(key) for key in ("id", "name", "status", "progress", "message")}


def _account_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "type": row.get("cloudAccountType") or row.get("type"),
    }


async def cloud_account_create(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """Add a vCenter or NSX cloud account: ``POST /iaas/api/cloud-accounts-vsphere|-nsx-t``.

    Safe to run twice: an account with the same name answers ``unchanged``
    and nothing is sent. The vendor adds the account in the background and
    answers with a request tracker.
    """
    kind = params["type"]
    if kind not in _TYPE_PATHS:
        raise ValueError(f"type {kind!r} is not 'vsphere' or 'nsx-t'")
    envelope: dict[str, Any] = {"cloud_account": None, "request": None}
    existing = await find_by_odata_name(
        connector, target, operator, CLOUD_ACCOUNTS_PATH, params["name"], params=API_VERSION
    )
    if existing is not None:
        return {
            **envelope,
            "status": "unchanged",
            "cloud_account": _account_view(existing),
            "guidance": (
                f"a cloud account named {existing.get('name')!r} already exists; nothing was sent"
            ),
        }
    if kind == "vsphere" and not params.get("regions"):
        return {
            **envelope,
            "status": "invalid_request",
            "guidance": "a vCenter account needs regions: the datacenters to use",
        }
    password = await read_password(operator, target, params)
    if password is None:
        return {
            **envelope,
            "status": "invalid_request",
            "guidance": password_missing_guidance(params),
        }
    try:
        tracker = await connector._post_json(
            target,
            _TYPE_PATHS[kind],
            operator=operator,
            params=API_VERSION,
            json=cloud_account_body(params, password),
        )
    except httpx.HTTPStatusError as exc:
        raise _without_secret(exc, password) from None
    return {
        "status": "submitted",
        "cloud_account": {"id": None, "name": params["name"], "type": kind},
        "request": _tracker_view(tracker),
        "guidance": (
            "the account is added in the background; read "
            f"{prefix}.cloud_account.list until it shows up, then check that healthy is true"
        ),
    }


# ---------------------------------------------------------------------------
# Remove a cloud account
# ---------------------------------------------------------------------------


async def cloud_account_delete(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``DELETE /iaas/api/cloud-accounts/{id}``. A ``404`` answers ``unchanged``."""
    path = CLOUD_ACCOUNT_PATH.format(id=segment(params["id"]))
    try:
        answer = await connector._post_json(
            target, path, operator=operator, verb="DELETE", params=API_VERSION
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        return {
            "status": "unchanged",
            "request": None,
            "guidance": "no cloud account has this id; nothing was removed",
        }
    tracker = _tracker_view(answer)
    return {
        "status": "submitted" if tracker else "deleted",
        "request": tracker,
        "guidance": (
            f"read {prefix}.cloud_account.list; the account is gone when the remove has ended"
        ),
    }


#: The lists whose rows belong to a cloud account (``cloudAccountId``).
_CHILD_LISTS: Final[tuple[tuple[str, str, bool], ...]] = (
    ("cloud_zone", ZONES_PATH, True),
    ("flavor_profile", FLAVOR_PROFILES_PATH, False),
    ("network_profile", NETWORK_PROFILES_PATH, True),
    ("storage_profile", STORAGE_PROFILES_PATH, False),
)


async def cloud_account_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview of a cloud account remove, with the blast-radius block.

    Reads ``GET /iaas/api/cloud-accounts/{id}``, then the cloud zones and the
    flavor, network and storage profiles that belong to the account. A cloud
    account that does not exist (``404``) raises, so the remove is refused
    before it waits for approval. Without a connector it only names the id.
    """
    account_id = ctx.params.get("id")
    if not isinstance(account_id, str) or not account_id:
        return None
    preview: dict[str, Any] = {"action": "delete_cloud_account", "id": account_id}
    connector: Any = ctx.connector_instance
    if connector is None:
        return preview
    path = CLOUD_ACCOUNT_PATH.format(id=segment(account_id))
    try:
        row = await connector._request_json(
            ctx.target, "GET", path, operator=ctx.operator, params=API_VERSION
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise LookupError(f"no cloud account has the id {account_id!r}") from exc
        raise
    owner = row.get("id") or account_id
    children: list[dict[str, Any]] = []
    for kind, list_path, paged in _CHILD_LISTS:
        rows, _ = await list_rows(connector, ctx.operator, ctx.target, list_path, {}, paged=paged)
        children.extend(
            {"kind": kind, "id": r.get("id"), "name": r.get("name")}
            for r in rows
            if r.get("cloudAccountId") == owner
        )
    return {
        **preview,
        "name": row.get("name"),
        "type": row.get("cloudAccountType"),
        "blast_radius": {
            "object": {"kind": "cloud_account", **_account_view(row), "id": owner},
            "children": children,
            "irreversibility": "permanent",
            "detail": (
                "the cloud zones and profiles listed here belong to the account and stop "
                "working; projects and templates that use them cannot deploy there. Adding "
                "the account again gives new ids, so they must be set up again by hand."
            ),
        },
    }


register_preview_builder("vcfa.tenant.cloud_account.delete", cloud_account_delete_preview)
register_preview_builder("vcfa.vra8.cloud_account.delete", cloud_account_delete_preview)
