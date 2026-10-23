# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The eight HCX reads: manager, site pairings, service mesh, jobs, alerts, inventory.

Each function is the body of a thin shim method on
:class:`~meho_backplane.connectors.hcx.connector.HcxConnector`; the op
metadata lives in :mod:`meho_backplane.connectors.hcx.read_ops`.

Every call goes through the connector's own login (the
``x-hm-authorization`` token). A ``401`` is raised as
:class:`httpx.HTTPStatusError`, so the dispatcher drops the token with
``invalidate_session`` and runs the read once more.

A list read returns ``{"items": [...]}``: one flat row per vendor object,
taken out of the vendor envelope (``items`` or ``data.items``). MEHO then
stores a long list behind a result handle. Rows keep the vendor fields;
any field whose name says password, secret, token, private key or licence
key is replaced with :data:`REDACTED` before the row leaves the connector.

The paths come from the vendor reference "HCX Workload Migration APIs"
(Broadcom developer portal, no version named) and, for
``POST /hybridity/api/service/inventory/cloud/list``, from VMware's own
Terraform provider for HCX. No HCX API file is pinned yet; the reconcile
lane checks every path in :data:`VENDOR_CALLS` once the files are pinned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.hcx.connector import (
    _APPLIANCES_PATH,
    _APPLIANCES_QUERY_BODY,
    parse_manager_version,
)
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.operations._rfc6570 import has_dot_segment

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = [
    "REDACTED",
    "VENDOR_CALLS",
    "hcx_about_impl",
    "hcx_alert_list_impl",
    "hcx_appliance_list_impl",
    "hcx_inventory_list_impl",
    "hcx_job_get_impl",
    "hcx_service_mesh_diagnose_impl",
    "hcx_service_mesh_list_impl",
    "hcx_site_pairing_list_impl",
    "manager_role",
    "redact_secrets",
    "rows_of",
]

_CLOUD_LIST_PATH: Final = "/hybridity/api/service/inventory/cloud/list"
_CLOUD_CONFIGS_PATH: Final = "/hybridity/api/cloudConfigs"
_SERVICE_MESH_PATH: Final = "/hybridity/api/interconnect/serviceMesh"
_INTERCONNECT_APPLIANCES_PATH: Final = "/hybridity/api/interconnect/appliances"
_DIAGNOSE_PATH: Final = "/hybridity/api/interconnect/serviceMesh/{serviceMeshId}/diagnose"
_JOB_PATH: Final = "/hybridity/api/jobs/{jobid}"
_ALERTS_PATH: Final = "/hybridity/api/alerts"
_RESOURCE_CONTAINER_LIST_PATH: Final = "/hybridity/api/service/inventory/resourcecontainer/list"

#: Every vendor call the eight reads make, as ``(method, path template)``.
#: The reconcile lane checks each one against the pinned HCX API files.
VENDOR_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("POST", _APPLIANCES_PATH),
        ("POST", _CLOUD_LIST_PATH),
        ("GET", _CLOUD_CONFIGS_PATH),
        ("GET", _SERVICE_MESH_PATH),
        ("GET", _INTERCONNECT_APPLIANCES_PATH),
        ("POST", _DIAGNOSE_PATH),
        ("GET", _JOB_PATH),
        ("POST", _ALERTS_PATH),
        ("POST", _RESOURCE_CONTAINER_LIST_PATH),
    }
)

#: Written in place of a secret value; the key stays, so the shape is stable.
REDACTED: Final = "***REDACTED***"

#: Key names (lower case, without ``_`` and ``-``) whose value is secret.
_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "clientsecret",
        "token",
        "accesstoken",
        "refreshtoken",
        "sessiontoken",
        "xhmauthorization",
        "authorization",
        "privatekey",
        "licensekey",
        "licencekey",
        "license",
        "licence",
    }
)

#: The alert filter fields a caller may set, with the vendor field name.
_ALERT_FILTER_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("severity", "severity"),
    ("state", "state"),
    ("module", "module"),
    ("category", "category"),
    ("entity_id", "entityId"),
)

#: ``site`` value of ``hcx.inventory.list`` -> the vendor ``filter.cloud`` flags.
_SITE_FILTERS: Final[dict[str, dict[str, bool]]] = {
    "local": {"local": True, "remote": False},
    "remote": {"local": False, "remote": True},
}


#: A key that ends with one of these (``vcPassword``, ``adminPassword``) is secret too.
_SECRET_SUFFIXES: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "secret",
    "token",
    "privatekey",
    "licensekey",
    "licencekey",
)


def _is_secret_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    name = key.lower().replace("_", "").replace("-", "")
    return name in _SECRET_KEYS or name.endswith(_SECRET_SUFFIXES)


def redact_secrets(value: Any) -> Any:
    """Return *value* with every secret-named field replaced, at any depth. No mutation."""
    if isinstance(value, dict):
        return _redact_object(value)
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def _redact_object(value: dict[str, Any]) -> dict[str, Any]:
    """:func:`redact_secrets` for one object."""
    return {
        key: REDACTED if _is_secret_key(key) else redact_secrets(item)
        for key, item in value.items()
    }


def _segment(kind: str, value: Any) -> str:
    """Return *value* as one URL path segment, or raise ``ValueError``.

    An id must stay one path segment. A ``.`` or ``..`` id is refused, and
    every reserved character (``/``, ``?``, ``#``, ...) is percent-encoded,
    so the request always goes to the path the op names.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"the {kind} id must be a non-empty string, got {value!r}")
    if value == "." or has_dot_segment(value):
        raise ValueError(f"the {kind} id {value!r} must not be '.' or contain a '..' segment")
    return quote(value, safe="")


def rows_of(payload: Any) -> list[dict[str, Any]]:
    """Return the object rows of a vendor answer: a bare list, ``items`` or ``data.items``."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for container in (payload, payload.get("data")):
        if isinstance(container, dict) and isinstance(container.get("items"), list):
            return [row for row in container["items"] if isinstance(row, dict)]
    return []


def _nested(row: dict[str, Any], *keys: str) -> Any:
    """Return ``row[k1][k2]...`` or ``None`` when a step is missing or not a dict."""
    value: Any = row
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def manager_role(system_type: Any) -> str:
    """Map the vendor ``systemType`` to ``cloud``, ``connector`` or ``unknown``."""
    if not isinstance(system_type, str):
        return "unknown"
    lowered = system_type.lower()
    if "cloud" in lowered:
        return "cloud"
    if "connector" in lowered:
        return "connector"
    return "unknown"


async def hcx_about_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.about`` -- the Manager version and role, and the local site.

    ``POST /hybridity/api/hcspAppliances`` (the local appliance row:
    ``version``, ``systemType``, ``systemName``) and
    ``POST /hybridity/api/service/inventory/cloud/list`` with the local
    filter (the local site's ``endpointId``, ``name``, ``url``,
    ``endpointType``).
    """
    del params
    appliances = await connector._post_json(
        target, _APPLIANCES_PATH, operator=operator, json=_APPLIANCES_QUERY_BODY
    )
    clouds = await connector._post_json(
        target,
        _CLOUD_LIST_PATH,
        operator=operator,
        json={"filter": {"local": True, "remote": False}},
    )
    version = parse_manager_version(appliances)
    appliance_rows = [row for row in rows_of(appliances) if row.get("version")]
    if not appliance_rows and isinstance(appliances, dict) and appliances.get("version"):
        appliance_rows = [appliances]
    appliance = appliance_rows[0] if appliance_rows else {}
    local = rows_of(clouds)
    site = local[0] if local else {}
    return _redact_object(
        {
            "version": version.version,
            "build": version.build,
            "system_type": appliance.get("systemType"),
            "system_name": appliance.get("systemName"),
            "role": manager_role(appliance.get("systemType")),
            "local_endpoint_id": site.get("endpointId"),
            "local_endpoint_name": site.get("name"),
            "local_endpoint_type": site.get("endpointType"),
            "local_url": site.get("url"),
        }
    )


async def hcx_site_pairing_list_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.site_pairing.list`` -- ``GET /hybridity/api/cloudConfigs``, one row per pairing."""
    del params
    payload = await connector._get_json(target, _CLOUD_CONFIGS_PATH, operator=operator)
    return {"items": redact_secrets(rows_of(payload))}


def _service_mesh_row(row: dict[str, Any]) -> dict[str, Any]:
    """Add the flat fields of the last task to a service mesh row."""
    flat = dict(row)
    flat["last_task_id"] = _nested(row, "lastTaskDetails", "interconnectTaskId")
    flat["last_task_type"] = _nested(row, "lastTaskDetails", "type")
    flat["last_task_status"] = _nested(row, "lastTaskDetails", "status")
    return flat


async def hcx_service_mesh_list_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.service_mesh.list`` -- ``GET /hybridity/api/interconnect/serviceMesh``."""
    del params
    payload = await connector._get_json(target, _SERVICE_MESH_PATH, operator=operator)
    return {"items": [_service_mesh_row(row) for row in redact_secrets(rows_of(payload))]}


def _version_text(version: Any) -> str | None:
    """Join the vendor ``{major, minor, patch}`` object into ``"4.11.0"``."""
    if not isinstance(version, dict):
        return None
    parts = [version.get(key) for key in ("major", "minor", "patch")]
    if any(part is None for part in parts):
        return None
    return ".".join(str(part) for part in parts)


def _appliance_row(row: dict[str, Any]) -> dict[str, Any]:
    """Add flat state, tunnel and version fields to an interconnect appliance row."""
    flat = dict(row)
    summary = _nested(row, "status", "summary")
    summary = summary if isinstance(summary, dict) else {}
    flat["state"] = _nested(row, "applianceStatus", "state")
    flat["overall_status"] = summary.get("overallStatus")
    flat["transport_status"] = summary.get("overallTransportStatus")
    flat["tunnel_status"] = summary.get("overallEncryptionTunnelStatus")
    flat["tunnels_active"] = _nested(summary, "encryptionTunnelSummary", "totalActiveTunnelCount")
    flat["tunnels_total"] = _nested(summary, "encryptionTunnelSummary", "totalTunnelCount")
    flat["version"] = _version_text(row.get("applianceVersion"))
    flat["build"] = _nested(row, "applianceVersion", "build")
    flat["update_available"] = _nested(row, "updateInfo", "updateAvailable")
    return flat


async def hcx_appliance_list_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.appliance.list`` -- ``GET /hybridity/api/interconnect/appliances``."""
    del params
    payload = await connector._get_json(target, _INTERCONNECT_APPLIANCES_PATH, operator=operator)
    return {"items": [_appliance_row(row) for row in redact_secrets(rows_of(payload))]}


async def hcx_service_mesh_diagnose_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.service_mesh.diagnose`` -- start the diagnostics of one service mesh.

    ``POST /hybridity/api/interconnect/serviceMesh/{serviceMeshId}/diagnose``
    answers ``202`` with ``data.interconnectTaskId``. The run changes no
    configuration; its progress shows as the mesh's last task in
    ``hcx.service_mesh.list``.
    """
    mesh_id = params["service_mesh_id"]
    path = _DIAGNOSE_PATH.format(serviceMeshId=_segment("service mesh", mesh_id))
    payload = await connector._post_json(target, path, operator=operator, json={})
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    body = payload if isinstance(payload, dict) else {}
    return _redact_object(
        {
            "service_mesh_id": data.get("serviceMeshId") or mesh_id,
            "interconnect_task_id": data.get("interconnectTaskId"),
            "warnings": body.get("warnings") or [],
            "errors": body.get("errors") or [],
        }
    )


async def hcx_job_get_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.job.get`` -- ``GET /hybridity/api/jobs/{jobid}``, the vendor job object."""
    path = _JOB_PATH.format(jobid=_segment("job", params["job_id"]))
    payload = await connector._get_json(target, path, operator=operator)
    return _redact_object(payload) if isinstance(payload, dict) else {"job": payload}


async def hcx_alert_list_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.alert.list`` -- ``POST /hybridity/api/alerts?action=query`` with an optional filter."""
    alert_filter: dict[str, Any] = {}
    for param, vendor_field in _ALERT_FILTER_FIELDS:
        value = params.get(param)
        if value:
            alert_filter[vendor_field] = value
    body: dict[str, Any] = {"filter": alert_filter} if alert_filter else {}
    payload = await connector._post_json(
        target, _ALERTS_PATH, operator=operator, params={"action": "query"}, json=body
    )
    return {"items": redact_secrets(rows_of(payload))}


async def hcx_inventory_list_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.inventory.list`` -- the resource containers of the paired sites.

    ``POST /hybridity/api/service/inventory/resourcecontainer/list``. With
    ``site`` set, the body is ``{"filter": {"cloud": {"local", "remote"}}}``;
    without it the body is empty and the Manager lists every site.
    """
    site = params.get("site")
    body: dict[str, Any] = {"filter": {"cloud": _SITE_FILTERS[site]}} if site else {}
    payload = await connector._post_json(
        target, _RESOURCE_CONTAINER_LIST_PATH, operator=operator, json=body
    )
    return {"items": redact_secrets(rows_of(payload))}
