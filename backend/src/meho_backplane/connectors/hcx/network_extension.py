# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The HCX network extension operations: list, extend, unextend, and Mobility Optimized Networking.

Each function is the body of a thin shim method on
:class:`~meho_backplane.connectors.hcx.connector.HcxConnector`; the op
metadata lives in :mod:`meho_backplane.connectors.hcx.network_extension_ops`.

Vendor jobs. ``hcx.network_extension.extend`` and
``hcx.network_extension.unextend`` start a vendor job: the Manager answers
with ``{"id": <job id>}`` at once and does the work afterwards. The answer
says ``accepted`` with the job id and names ``hcx.job.get`` as the status
read; it never says the work is done. Before a start, the operation reads
the extension list (``GET /hybridity/api/l2Extensions``). Extend starts
nothing when the network already has an extension. Unextend starts nothing
when the extension is gone, or when its state says that work still runs on
it. This check helps; it is not a lock.

Mobility Optimized Networking (``hcx.network_extension.mon.set``) has two
steps: turn the feature on or off
(``PUT /hybridity/api/networkExtensions/{extensionId}``) and set the policy
routes (``PUT /hybridity/api/l2Extensions/policyRoutes``). A step that
fails stops the run. The answer lists the steps that ran, the step that
failed with the vendor's error and the steps that did not run. Nothing is
undone.

A ``401`` is raised as :class:`httpx.HTTPStatusError`, so the dispatcher
logs in again and runs the call once more. A ``401`` means the Manager did
nothing, so the second run is safe.

The paths come from Broadcom's "HCX Workload Migration APIs" reference and
from VMware's own Terraform provider and PowerCLI module for HCX, which use
the same ``l2Extensions`` calls for create, delete and list. The reference
marks the ``l2Extensions`` create and delete as deprecated; they are still
served, and they are the calls that return the job id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.connectors.hcx.typed_reads import _segment, redact_secrets, rows_of

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = [
    "BUSY_STATE_WORDS",
    "STATUS_READ",
    "VENDOR_CALLS",
    "extension_row",
    "hcx_network_extension_extend_impl",
    "hcx_network_extension_list_impl",
    "hcx_network_extension_mon_set_impl",
    "hcx_network_extension_unextend_impl",
    "is_busy",
]

_L2_EXTENSIONS_PATH: Final = "/hybridity/api/l2Extensions"
_L2_EXTENSION_PATH: Final = "/hybridity/api/l2Extensions/{extensionId}"
_NETWORK_EXTENSION_PATH: Final = "/hybridity/api/networkExtensions/{extensionId}"
_POLICY_ROUTES_PATH: Final = "/hybridity/api/l2Extensions/policyRoutes"

#: Every vendor call these operations make, as ``(method, path template)``.
#: The reconcile lane checks each one against the pinned HCX API files.
VENDOR_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", _L2_EXTENSIONS_PATH),
        ("POST", _L2_EXTENSIONS_PATH),
        ("DELETE", _L2_EXTENSION_PATH),
        ("PUT", _NETWORK_EXTENSION_PATH),
        ("PUT", _POLICY_ROUTES_PATH),
    }
)

#: The status read that follows a started job.
STATUS_READ: Final = "hcx.job.get"

#: A vendor state that holds one of these words means work still runs on
#: the extension. The vendor publishes no fixed list of states.
BUSY_STATE_WORDS: Final[tuple[str, ...]] = (
    "PROGRESS",
    "RUNNING",
    "PENDING",
    "QUEUED",
    "EXTENDING",
    "DELETING",
    "TEARING",
    "UNEXTEND",
    "UNSTRETCH",
)

#: Longest vendor error text an answer carries.
_ERROR_TEXT_MAX: Final = 2000

_MON_STEP: Final = "mobility_optimized_networking"
_ROUTES_STEP: Final = "policy_routes"


def _nested(row: Any, *keys: str) -> Any:
    value = row
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def is_busy(state: Any) -> bool:
    """True when the vendor *state* says that work still runs on the extension."""
    if not isinstance(state, str):
        return False
    upper = state.upper()
    return any(word in upper for word in BUSY_STATE_WORDS)


def extension_row(row: dict[str, Any]) -> dict[str, Any]:
    """A vendor extension row with the main fields added flat."""
    flat = dict(row)
    flat["stretch_id"] = row.get("stretchId")
    flat["state"] = _nested(row, "operationStatus", "state")
    flat["job_id"] = _nested(row, "operationStatus", "jobId")
    flat["source_network_id"] = _nested(row, "sourceNetwork", "networkId")
    flat["source_network_name"] = _nested(row, "sourceNetwork", "networkName")
    flat["source_network_type"] = _nested(row, "sourceNetwork", "networkType")
    flat["destination_network_name"] = _nested(row, "destinationNetwork", "networkName")
    flat["destination_gateway_id"] = _nested(row, "destinationNetwork", "gatewayId")
    flat["destination_endpoint_id"] = _nested(row, "destination", "endpointId")
    flat["source_appliance_id"] = _nested(row, "sourceAppliance", "applianceId")
    flat["mobility_optimized_networking"] = _nested(row, "features", "mobilityOptimizedNetworking")
    flat["egress_optimization"] = _nested(row, "features", "egressOptimization")
    return flat


async def _extensions(
    connector: HcxConnector, operator: Operator, target: HcxTargetLike
) -> list[dict[str, Any]]:
    payload = await connector._get_json(target, _L2_EXTENSIONS_PATH, operator=operator)
    return rows_of(payload)


def _find(rows: list[dict[str, Any]], stretch_id: str) -> dict[str, Any] | None:
    return next((row for row in rows if row.get("stretchId") == stretch_id), None)


async def hcx_network_extension_list_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.network_extension.list`` -- ``GET /hybridity/api/l2Extensions``, rows."""
    del params
    rows = await _extensions(connector, operator, target)
    return {"items": [extension_row(row) for row in redact_secrets(rows)]}


def _job_id(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return None
    return payload.get("id") or payload.get("jobId")


def _extend_body(params: dict[str, Any]) -> dict[str, Any]:
    source = params["source_network"]
    destination = params["destination"]
    body: dict[str, Any] = {
        "gateway": params["gateway"],
        "netmask": params["netmask"],
        "dns": list(params.get("dns") or []),
        "destination": {
            "endpointId": destination["endpoint_id"],
            "endpointName": destination.get("endpoint_name"),
            "endpointType": destination.get("endpoint_type"),
            "resourceId": destination["resource_id"],
            "resourceName": destination.get("resource_name"),
            "resourceType": destination.get("resource_type"),
        },
        "destinationNetwork": {"gatewayId": params["destination_gateway_id"]},
        "features": {
            "egressOptimization": bool(params.get("egress_optimization", False)),
            "mobilityOptimizedNetworking": bool(params.get("mobility_optimized_networking", False)),
        },
        "sourceAppliance": {"applianceId": params["source_appliance_id"]},
        "sourceNetwork": {
            "networkId": source["network_id"],
            "networkName": source.get("network_name"),
            "networkType": source["network_type"],
        },
    }
    if params.get("vc_guid"):
        body["vcGuid"] = params["vc_guid"]
    return body


async def hcx_network_extension_extend_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.network_extension.extend`` -- ``POST /hybridity/api/l2Extensions``, a vendor job.

    Starts nothing when the source network already has an extension (in
    any state); the answer then names that extension, its state and its
    job id when the vendor gives one.
    """
    network_id = params["source_network"]["network_id"]
    for row in await _extensions(connector, operator, target):
        if _nested(row, "sourceNetwork", "networkId") == network_id:
            return {
                "status": "already_extended",
                "job_id": _nested(row, "operationStatus", "jobId"),
                "status_read": STATUS_READ,
                "network_id": network_id,
                "stretch_id": row.get("stretchId"),
                "state": _nested(row, "operationStatus", "state"),
            }
    payload = await connector._post_json(
        target, _L2_EXTENSIONS_PATH, operator=operator, json=_extend_body(params)
    )
    return {
        "status": "accepted",
        "job_id": _job_id(payload),
        "status_read": STATUS_READ,
        "network_id": network_id,
        "stretch_id": None,
        "state": None,
    }


async def hcx_network_extension_unextend_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.network_extension.unextend`` -- ``DELETE /hybridity/api/l2Extensions/{extensionId}``.

    A vendor job. ``connect_to_local_router`` goes out as the
    ``connectToLocalRouter`` query flag. Starts nothing when the extension
    is not in the list (``not_found``) or when its state says that work
    still runs on it (``already_running``).
    """
    stretch_id = params["stretch_id"]
    path = _L2_EXTENSION_PATH.format(extensionId=_segment("extension", stretch_id))
    row = _find(await _extensions(connector, operator, target), stretch_id)
    if row is None or is_busy(_nested(row, "operationStatus", "state")):
        return {
            "status": "not_found" if row is None else "already_running",
            "job_id": None if row is None else _nested(row, "operationStatus", "jobId"),
            "status_read": STATUS_READ,
            "stretch_id": stretch_id,
            "state": None if row is None else _nested(row, "operationStatus", "state"),
        }
    connect = bool(params.get("connect_to_local_router", False))
    payload = await connector._post_json(
        target,
        path,
        operator=operator,
        verb="DELETE",
        params={"connectToLocalRouter": "true" if connect else "false"},
    )
    return {
        "status": "accepted",
        "job_id": _job_id(payload),
        "status_read": STATUS_READ,
        "stretch_id": stretch_id,
        "state": _nested(row, "operationStatus", "state"),
    }


def _vendor_error(exc: httpx.HTTPStatusError) -> str:
    """The vendor's error text of *exc*: the ``errors`` messages, else the body text."""
    response = exc.response
    try:
        body = response.json()
    except ValueError:
        body = None
    messages = _messages(body)
    text = "; ".join(messages) if messages else response.text
    return f"HTTP {response.status_code}: {text}"[:_ERROR_TEXT_MAX]


def _messages(body: Any) -> list[str]:
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return []
    return [
        str(item.get("message"))
        for item in errors
        if isinstance(item, dict) and item.get("message")
    ]


def _warnings(body: Any) -> list[str]:
    warnings = body.get("warnings") if isinstance(body, dict) else None
    if not isinstance(warnings, list):
        return []
    return [
        str(item.get("message"))
        for item in warnings
        if isinstance(item, dict) and item.get("message")
    ]


def _mon_body(row: dict[str, Any], enabled: bool) -> dict[str, Any]:
    body = {key: value for key, value in row.items() if key != "operationStatus"}
    features = dict(row.get("features") or {})
    features["mobilityOptimizedNetworking"] = enabled
    body["features"] = features
    return body


def _mon_steps(
    params: dict[str, Any], row: dict[str, Any], path: str
) -> list[tuple[str, str, dict[str, Any]]]:
    """The changes to make, in order, as ``(step name, path, body)``."""
    steps: list[tuple[str, str, dict[str, Any]]] = []
    if "mobility_optimized_networking" in params:
        enabled = bool(params["mobility_optimized_networking"])
        steps.append((_MON_STEP, path, _mon_body(row, enabled)))
    routes = params.get("policy_routes")
    if routes is not None:
        endpoint_id = routes.get("endpoint_id") or _nested(row, "destination", "endpointId")
        body = {
            "endpointId": endpoint_id,
            "allow": list(routes.get("allow") or []),
            "deny": list(routes.get("deny") or []),
        }
        steps.append((_ROUTES_STEP, _POLICY_ROUTES_PATH, body))
    return steps


async def hcx_network_extension_mon_set_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.network_extension.mon.set`` -- turn MON on or off, then set the policy routes.

    Reads the extension first (its row is the body of the MON change and
    names the remote site of the policy routes). Then it runs each asked
    step in order and stops at the first that fails. A failed step is an
    HTTP error other than ``401``, or an answer whose ``errors`` list is
    not empty.
    """
    stretch_id = params["stretch_id"]
    path = _NETWORK_EXTENSION_PATH.format(extensionId=_segment("extension", stretch_id))
    row = _find(await _extensions(connector, operator, target), stretch_id)
    if row is None:
        raise ValueError(
            f"no network extension with stretch id {stretch_id!r}; "
            "call hcx.network_extension.list for the ids"
        )
    steps = _mon_steps(params, row, path)
    ran: list[str] = []
    failed: dict[str, Any] | None = None
    warnings: list[str] = []
    for name, step_path, body in steps:
        try:
            answer = await connector._post_json(
                target, step_path, operator=operator, verb="PUT", json=body
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.UNAUTHORIZED:
                raise
            failed = {"step": name, "error": _vendor_error(exc)}
            break
        errors = _messages(answer)
        if errors:
            failed = {"step": name, "error": "; ".join(errors)[:_ERROR_TEXT_MAX]}
            break
        warnings.extend(_warnings(answer))
        ran.append(name)
    # The steps run in order and stop at the first failure.
    not_run = [name for name, _, _ in steps[len(ran) + (failed is not None) :]]
    status = "accepted" if failed is None else "partial" if ran else "failed"
    result: dict[str, Any] = redact_secrets(
        {
            "status": status,
            "stretch_id": stretch_id,
            "steps_ran": ran,
            "step_failed": failed,
            "steps_not_run": not_run,
            "warnings": warnings,
        }
    )
    return result
