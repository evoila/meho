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
undone. A step that ran is never lost: when the connection drops during a
later step, the answer still names it, and the failed step says
``outcome: unknown``, because the Manager may have taken the request
before the connection broke. A ``401`` at a step means the Manager did
nothing with it: the operation logs in again and sends that step once
more; the steps before it are not repeated. When the new login fails (the
Manager rejects it, answers it with a fault, or does not answer), or the
step gets a second ``401``, the error is raised when no step ran yet, so
the dispatcher reports the login failure; after a step ran, it is the
failed step of the answer, with the login error as its text.

Every vendor text in an answer (an error body, an ``errors`` or
``warnings`` message) goes through the secret filter first: a secret-named
field of a JSON error body is replaced before the body becomes text, and
the text itself goes through the default redaction policy.

For the other operations a ``401`` is raised as
:class:`httpx.HTTPStatusError`, so the dispatcher logs in again and runs
the call once more. A ``401`` means the Manager did nothing, so the second
run is safe.

The paths come from Broadcom's "HCX Workload Migration APIs" reference and
from VMware's own Terraform provider and PowerCLI module for HCX, which use
the same ``l2Extensions`` calls for create, delete and list. The reference
marks the ``l2Extensions`` create and delete as deprecated; they are still
served, and they are the calls that return the job id.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import SsrfBlockedError
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.connectors.hcx.typed_reads import (
    _is_tls_verify_failure,
    _segment,
    redact_secrets,
    rows_of,
)
from meho_backplane.redaction import get_default_policy, redact
from meho_backplane.redaction.secret_fields import scrub_secret_fields, scrub_secret_fields_in_text

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

#: Written in place of a vendor text when the secret filter itself fails.
_TEXT_WITHHELD: Final = "<text from HCX withheld: the secret filter failed>"

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


def _clean_text(text: str) -> str:
    """*text* with every secret filtered out, cut to :data:`_ERROR_TEXT_MAX`.

    The value of each quoted secret-named pair is replaced first (the
    default policy does not see ``"password": "..."`` behind a quote), then
    the default redaction policy runs. The filter runs before the cut, so a
    secret never leaves a fragment behind. When the filter itself fails,
    the text is withheld.
    """
    try:
        cleaned = redact(scrub_secret_fields_in_text(text), get_default_policy()).redacted
    except Exception:  # fail-closed: a vendor text never bypasses the filter
        return _TEXT_WITHHELD
    if not isinstance(cleaned, str):
        return _TEXT_WITHHELD
    return cleaned[:_ERROR_TEXT_MAX]


def _vendor_error(exc: httpx.HTTPStatusError) -> str:
    """The vendor's error text of *exc*: the ``errors`` messages, else the body.

    A JSON body with no ``errors`` message has its secret-named fields
    replaced before it becomes text, so a password the Manager echoes in
    an error body never reaches the answer.
    """
    response = exc.response
    try:
        body = response.json()
    except ValueError:
        body = None
    messages = _messages(body)
    if messages:
        text = "; ".join(messages)
    elif body is not None:
        text = json.dumps(scrub_secret_fields(body), default=str)
    else:
        text = response.text
    return _clean_text(f"HTTP {response.status_code}: {text}")


def _texts(body: Any, key: str) -> list[str]:
    """The cleaned ``message`` of each item under *key* of *body*."""
    items = body.get(key) if isinstance(body, dict) else None
    if not isinstance(items, list):
        return []
    return [
        _clean_text(str(item.get("message")))
        for item in items
        if isinstance(item, dict) and item.get("message")
    ]


def _messages(body: Any) -> list[str]:
    return _texts(body, "errors")


def _warnings(body: Any) -> list[str]:
    return _texts(body, "warnings")


def _never_sent(exc: httpx.TransportError) -> bool:
    """True when MEHO refused the request before it left, or the Manager's certificate failed."""
    return isinstance(exc, SsrfBlockedError) or _is_tls_verify_failure(exc)


class _LoginError(Exception):
    """The new login after a ``401`` failed; ``reason`` is the cleaned error text."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.reason = _clean_text(
            f"HTTP 401: the step was not taken, and the new login failed: "
            f"{type(cause).__name__}: {cause}"
        )


async def _put_step(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    path: str,
    body: dict[str, Any],
    *,
    raise_login_failure: bool,
) -> dict[str, Any]:
    """``PUT`` one step. On a ``401``, log in again and send the same step once more.

    A ``401`` means the Manager did nothing with the request, so one more
    send of this step is safe, and the steps before it stay as they are. A
    second ``401`` is raised to the caller. The new login runs on its own,
    before the second send: when it fails, the error is raised as it is
    with *raise_login_failure* (no step ran yet, the dispatcher reports
    it), else as :class:`_LoginError`, so the caller keeps the steps that
    ran. Any error counts: a rejected login, a Manager fault, a lost
    connection, a credential that cannot be read.
    """
    try:
        return await connector._post_json(target, path, operator=operator, verb="PUT", json=body)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != httpx.codes.UNAUTHORIZED:
            raise
        await connector.invalidate_session(target, rejected=exc.request)
    try:
        await connector.auth_headers(target, operator)
    except Exception as exc:
        if raise_login_failure:
            raise
        raise _LoginError(exc) from exc
    return await connector._post_json(target, path, operator=operator, verb="PUT", json=body)


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
    HTTP error (``outcome: rejected``), an answer whose ``errors`` list is
    not empty (``rejected``), or a lost connection (``unknown``: the
    Manager may have taken the step). A ``401`` makes a new login and one
    more send of that step only. A lost connection at the first step is
    raised when the request never left MEHO (a blocked destination, an
    untrusted certificate); a failed new login, or a ``401`` that survives
    it, is raised when no step ran yet, and is the failed step
    (``rejected``, the Manager did nothing with it) after a step ran.
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
            answer = await _put_step(
                connector, operator, target, step_path, body, raise_login_failure=not ran
            )
        except _LoginError as exc:
            failed = {"step": name, "error": exc.reason, "outcome": "rejected"}
            break
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.UNAUTHORIZED and not ran:
                raise
            failed = {"step": name, "error": _vendor_error(exc), "outcome": "rejected"}
            break
        except httpx.TransportError as exc:
            if not ran and _never_sent(exc):
                raise
            error = _clean_text(f"{type(exc).__name__}: {exc}")
            failed = {"step": name, "error": error, "outcome": "unknown"}
            break
        errors = _messages(answer)
        if errors:
            error = "; ".join(errors)[:_ERROR_TEXT_MAX]
            failed = {"step": name, "error": error, "outcome": "rejected"}
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
