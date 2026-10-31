# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The bodies of ``hcx.profile.list`` and ``hcx.profile.set``.

Compute profiles live under ``/hybridity/api/interconnect/computeProfiles``.
A create or a change answers ``202`` with
``{"data": {"interconnectTaskId", "computeProfileId"}, "warnings", "errors"}``;
the caller follows the task with ``hcx.task.get``.

Network profiles live under ``/hybridity/api/networks``. ``GET`` answers a
bare list. A create or a change answers
``{"data": {"jobId", "objectId"}, "error", "errorMessage"}``; the caller
follows the job with ``hcx.job.get``.

A change reads the profile first, replaces the fields the caller set and
sends the vendor fields of the object back, because the vendor ``PUT``
needs the whole object.

The paths come from the vendor reference "HCX Workload Migration APIs"
(Broadcom developer portal) and the bodies from VMware's Terraform
provider for HCX. The op metadata lives in
:mod:`meho_backplane.connectors.hcx.profile_ops`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.connectors.hcx.typed_reads import (
    _envelope_notes,
    _nested,
    _redact_object,
    _segment,
    redact_secrets,
    rows_of,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = ["VENDOR_CALLS", "hcx_profile_list_impl", "hcx_profile_set_impl"]

_COMPUTE_PROFILES_PATH: Final = "/hybridity/api/interconnect/computeProfiles"
_COMPUTE_PROFILE_PATH: Final = "/hybridity/api/interconnect/computeProfiles/{computeProfileId}"
_NETWORK_PROFILES_PATH: Final = "/hybridity/api/networks"
_NETWORK_PROFILE_PATH: Final = "/hybridity/api/networks/{networkId}"

#: Every vendor call of this module, as ``(method, path template)``.
#: The reconcile lane checks each one against the pinned HCX API files.
VENDOR_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", _COMPUTE_PROFILES_PATH),
        ("POST", _COMPUTE_PROFILES_PATH),
        ("GET", _COMPUTE_PROFILE_PATH),
        ("PUT", _COMPUTE_PROFILE_PATH),
        ("GET", _NETWORK_PROFILES_PATH),
        ("POST", _NETWORK_PROFILES_PATH),
        ("GET", _NETWORK_PROFILE_PATH),
        ("PUT", _NETWORK_PROFILE_PATH),
    }
)

#: The fields of a vendor compute profile that a ``PUT`` sends back.
_COMPUTE_BODY_FIELDS: Final[tuple[str, ...]] = (
    "computeProfileId",
    "name",
    "compute",
    "services",
    "deploymentContainer",
    "networks",
    "switches",
    "storage",
    "location",
    "locationName",
    "deploymentComputeClusterId",
    "deploymentComputeClusterName",
)

#: The fields of a vendor network profile that a ``PUT`` sends back.
_NETWORK_BODY_FIELDS: Final[tuple[str, ...]] = (
    "objectId",
    "name",
    "description",
    "enterprise",
    "organization",
    "backings",
    "mtu",
    "tags",
    "ipScopes",
    "l3TenantManaged",
    "ownedBySystem",
    "allowTrunkInterfaces",
    "scope",
    "scopeNames",
)

#: Fields a new profile must have, by kind.
_COMPUTE_CREATE_FIELDS: Final = ("name", "services", "clusters", "deployment", "networks")
_NETWORK_CREATE_FIELDS: Final = ("name", "backing", "prefix_length", "ip_ranges")

#: ``(parameter name, vendor name)`` of a vCenter object reference.
_REF_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("id", "id"),
    ("name", "name"),
    ("type", "type"),
    ("cmp_id", "cmpId"),
    ("cmp_name", "cmpName"),
    ("cmp_type", "cmpType"),
)

_SWITCH_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("id", "id"),
    ("name", "name"),
    ("type", "type"),
    ("cmp_id", "cmpId"),
    ("max_mtu", "maxMtu"),
)

#: ``(parameter name, vendor name)`` of the subnet fields of a network profile.
_SCOPE_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("prefix_length", "prefixLength"),
    ("gateway", "gateway"),
    ("primary_dns", "primaryDns"),
    ("secondary_dns", "secondaryDns"),
    ("dns_suffix", "dnsSuffix"),
)

_TASK_READ: Final = "hcx.task.get"
_JOB_READ: Final = "hcx.job.get"


def _vendor(obj: dict[str, Any], fields: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    """Copy the set fields of *obj* under their vendor names."""
    return {vendor: obj[param] for param, vendor in fields if obj.get(param) is not None}


def _compute_row(row: dict[str, Any]) -> dict[str, Any]:
    flat = dict(row)
    flat["profile_type"] = "compute"
    flat["profile_id"] = row.get("computeProfileId")
    flat["last_task_id"] = _nested(row, "lastTaskDetails", "interconnectTaskId")
    flat["last_task_status"] = _nested(row, "lastTaskDetails", "status")
    return flat


def _network_row(row: dict[str, Any]) -> dict[str, Any]:
    flat = dict(row)
    flat["profile_type"] = "network"
    flat["profile_id"] = row.get("objectId")
    flat["last_job_id"] = _nested(row, "jobDetails", "lastJobId")
    return flat


async def hcx_profile_list_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.profile.list`` -- ``GET .../interconnect/computeProfiles`` and ``GET .../networks``."""
    kind = params.get("kind")
    rows: list[dict[str, Any]] = []
    if kind in (None, "compute"):
        payload = await connector._get_json(target, _COMPUTE_PROFILES_PATH, operator=operator)
        rows.extend(_compute_row(row) for row in redact_secrets(rows_of(payload)))
    if kind in (None, "network"):
        payload = await connector._get_json(target, _NETWORK_PROFILES_PATH, operator=operator)
        rows.extend(_network_row(row) for row in redact_secrets(rows_of(payload)))
    return {"items": rows}


def _check_create_fields(spec: dict[str, Any], needed: tuple[str, ...], kind: str) -> None:
    missing = [name for name in needed if spec.get(name) in (None, [], {})]
    if missing:
        raise ValueError(
            f"a new {kind} profile needs {', '.join(missing)}; set them, or set "
            "profile_id to change an existing profile"
        )


def _apply_compute(body: dict[str, Any], spec: dict[str, Any]) -> None:
    """Write the compute profile fields of *spec* into the vendor *body* (in place)."""
    if spec.get("name"):
        body["name"] = spec["name"]
    if spec.get("services"):
        body["services"] = [{"name": name} for name in spec["services"]]
    if spec.get("clusters"):
        body["compute"] = [_vendor(ref, _REF_FIELDS) for ref in spec["clusters"]]
    if spec.get("networks"):
        body["networks"] = [
            {
                "id": net["id"],
                **({"name": net["name"]} if net.get("name") else {}),
                "tags": net["tags"],
            }
            for net in spec["networks"]
        ]
    if spec.get("switches"):
        body["switches"] = [_vendor(switch, _SWITCH_FIELDS) for switch in spec["switches"]]
    if spec.get("deployment"):
        body["deploymentContainer"] = _deployment_container(
            body.get("deploymentContainer"), spec["deployment"]
        )


def _deployment_container(current: Any, deployment: dict[str, Any]) -> dict[str, Any]:
    """The vendor ``deploymentContainer``: *current* with the set fields of *deployment*."""
    container = dict(current) if isinstance(current, dict) else {}
    if deployment.get("clusters"):
        container["compute"] = [_vendor(ref, _REF_FIELDS) for ref in deployment["clusters"]]
    if deployment.get("datastores"):
        container["storage"] = [_vendor(ref, _REF_FIELDS) for ref in deployment["datastores"]]
    if deployment.get("folder"):
        container["folder"] = _vendor(deployment["folder"], _REF_FIELDS)
    container.update(
        _vendor(
            deployment,
            (("cpu_reservation", "cpuReservation"), ("memory_reservation", "memoryReservation")),
        )
    )
    return container


def _apply_network(body: dict[str, Any], spec: dict[str, Any]) -> None:
    """Write the network profile fields of *spec* into the vendor *body* (in place)."""
    for param in ("name", "description", "mtu"):
        if spec.get(param) is not None:
            body[param] = spec[param]
    backing = spec.get("backing")
    if backing:
        body["backings"] = [
            _vendor(
                backing,
                (
                    ("id", "backingId"),
                    ("name", "backingName"),
                    ("type", "type"),
                    ("vcenter_instance_uuid", "vCenterInstanceUuid"),
                ),
            )
        ]
    scope_fields = _vendor(spec, _SCOPE_FIELDS)
    if spec.get("ip_ranges"):
        scope_fields["networkIpRanges"] = [
            {"startAddress": r["start_address"], "endAddress": r["end_address"]}
            for r in spec["ip_ranges"]
        ]
    if scope_fields:
        scopes = list(body.get("ipScopes") or [{}])
        first = scopes[0] if isinstance(scopes[0], dict) else {}
        scopes[0] = {**first, **scope_fields}
        body["ipScopes"] = scopes


async def _read_object(
    connector: HcxConnector, operator: Operator, target: HcxTargetLike, path: str
) -> dict[str, Any]:
    """``GET`` one vendor object; unwrap a ``data`` envelope when there is one."""
    payload = await connector._get_json(target, path, operator=operator)
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and data.get("name") is not None:
        return data
    return payload if isinstance(payload, dict) else {}


def _error_text(notes: dict[str, list[Any]]) -> str:
    return "; ".join(
        str(e.get("message") or e.get("text") or e.get("error"))
        for e in notes["errors"]
        if isinstance(e, dict)
    )


async def _set_compute(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    profile_id: str | None,
    spec: dict[str, Any],
) -> dict[str, Any]:
    if profile_id is None:
        _check_create_fields(spec, _COMPUTE_CREATE_FIELDS, "compute")
        body: dict[str, Any] = {}
        _apply_compute(body, spec)
        payload = await connector._post_json(
            target, _COMPUTE_PROFILES_PATH, operator=operator, json=body
        )
    else:
        path = _COMPUTE_PROFILE_PATH.format(
            computeProfileId=_segment("compute profile", profile_id)
        )
        current = await _read_object(connector, operator, target, path)
        body = {key: current[key] for key in _COMPUTE_BODY_FIELDS if key in current}
        body["computeProfileId"] = profile_id
        _apply_compute(body, spec)
        payload = await connector._post_json(target, path, operator=operator, verb="PUT", json=body)
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    notes = _envelope_notes(payload)
    task_id = data.get("interconnectTaskId")
    if not task_id:
        text = _error_text(notes)
        raise ValueError(
            "the HCX Manager answered without an interconnect task id"
            + (f": {text}" if text else "")
        )
    return {
        "status": "accepted",
        "kind": "compute",
        "action": "create" if profile_id is None else "change",
        "profile_id": data.get("computeProfileId") or profile_id,
        "interconnect_task_id": task_id,
        "job_id": None,
        "status_read": _TASK_READ,
        **notes,
    }


async def _set_network(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    profile_id: str | None,
    spec: dict[str, Any],
) -> dict[str, Any]:
    if profile_id is None:
        _check_create_fields(spec, _NETWORK_CREATE_FIELDS, "network")
        body: dict[str, Any] = {
            "organization": "DEFAULT",
            "l3TenantManaged": False,
            "ownedBySystem": True,
        }
        _apply_network(body, spec)
        payload = await connector._post_json(
            target, _NETWORK_PROFILES_PATH, operator=operator, json=body
        )
    else:
        path = _NETWORK_PROFILE_PATH.format(networkId=_segment("network profile", profile_id))
        current = await _read_object(connector, operator, target, path)
        body = {key: current[key] for key in _NETWORK_BODY_FIELDS if key in current}
        body["objectId"] = profile_id
        _apply_network(body, spec)
        payload = await connector._post_json(target, path, operator=operator, verb="PUT", json=body)
    answer = payload if isinstance(payload, dict) else {}
    data = answer.get("data")
    data = data if isinstance(data, dict) else {}
    if answer.get("error") is True or not data.get("jobId"):
        message = answer.get("errorMessage") or _error_text(_envelope_notes(payload))
        raise ValueError(
            "the HCX Manager did not accept the network profile"
            + (f": {message}" if message else "")
        )
    return {
        "status": "accepted",
        "kind": "network",
        "action": "create" if profile_id is None else "change",
        "profile_id": data.get("objectId") or profile_id,
        "interconnect_task_id": None,
        "job_id": data["jobId"],
        "status_read": _JOB_READ,
        **_envelope_notes(payload),
    }


async def hcx_profile_set_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.profile.set`` -- create (``POST``) or change (``GET`` then ``PUT``) one profile."""
    profile_id = params.get("profile_id")
    if params.get("compute_profile"):
        answer = await _set_compute(
            connector, operator, target, profile_id, params["compute_profile"]
        )
    else:
        answer = await _set_network(
            connector, operator, target, profile_id, params["network_profile"]
        )
    return _redact_object(answer)
