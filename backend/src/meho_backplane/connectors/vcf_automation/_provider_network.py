# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The provider networking read and the helpers the networking changes share.

``vcfa.provider.network.list`` reads one kind of networking object at a time
and answers flat rows. It is also the status read of the networking changes:
VCF Automation answers a create, change or delete with HTTP 202 and an empty
body, and then works on its own. The object's ``status`` field shows how far
it got (``PENDING``, ``CONFIGURING``, ``REALIZED``, ``REALIZATION_FAILED``);
each row maps it to a job state (queued, running, succeeded, failed).

With the ``job_id`` a change returned, the read also reads the vendor job
(:mod:`._provider_job`): its state, progress and the vendor's error text.

When the appliance does not answer (a connection error, a time-out, or HTTP
502, 503 or 504, for example during its own upgrade), the read answers
``state: not_reachable`` with the job id it was given and the time. It never
says ``failed`` then. MEHO keeps no job state between calls.

Field names follow the ``go-vcloud-director`` v3 types the vendor's own
Terraform provider uses for VCF Automation 9 (``types/v56/tm.go``:
``TmIpSpace``, ``TmProviderGateway``, ``TmIpSpaceAssociation``,
``TmRegionalNetworkingSetting``, ``TmEdgeCluster``, ``TmTier0Gateway``,
``TmOrgNetworkingSettings``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_automation._lookups import find_org, fiql_safe, list_all
from meho_backplane.connectors.vcf_automation._provider_job import (
    job_id_problem,
    not_reachable,
    read_job,
    with_job,
)
from meho_backplane.connectors.vcf_automation._provider_paths import (
    PROVIDER_EDGE_CLUSTERS_PATH,
    PROVIDER_GATEWAY_PATH,
    PROVIDER_GATEWAYS_PATH,
    PROVIDER_IP_SPACE_ASSOCIATION_PATH,
    PROVIDER_IP_SPACE_ASSOCIATIONS_PATH,
    PROVIDER_IP_SPACE_PATH,
    PROVIDER_IP_SPACES_PATH,
    PROVIDER_ORG_NETWORKING_PATH,
    PROVIDER_ORG_PATH,
    PROVIDER_REGIONAL_NETWORKING_PATH,
    PROVIDER_REGIONAL_NETWORKING_SETTING_PATH,
    PROVIDER_TIER0_ROUTERS_PATH,
    bad_id_reason,
    provider_path,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "KINDS",
    "RUNNING_STATUSES",
    "STATUS_READ_OP",
    "VcfaProviderNetworkError",
    "drift_reason",
    "job_id_of",
    "job_state",
    "list_kind",
    "now_iso",
    "provider_network_list",
    "read_one",
    "resolve_org",
    "send",
    "state_hash",
    "status_read",
]

#: The read that follows every networking job.
STATUS_READ_OP: Final = "vcfa.provider.network.list"
#: Vendor ``status`` values of an object VCF Automation still works on.
RUNNING_STATUSES: Final = frozenset({"PENDING", "CONFIGURING"})
_JOB_STATES: Final = {
    "PENDING": "queued",
    "CONFIGURING": "running",
    "REALIZED": "succeeded",
    "REALIZATION_FAILED": "failed",
    "UNKNOWN": "unknown",
}
_ORG_URN_PREFIX: Final = "urn:vcloud:org:"
#: Fields that change without anyone changing the object; a state hash skips them.
_UNBOUND_FIELDS: Final = frozenset(
    {
        "status",
        "catalogCount",
        "diskCount",
        "orgVdcCount",
        "runningVMCount",
        "userCount",
        "vappCount",
        "directlyManagedOrgCount",
        "nodeCount",
        "orgCount",
        "vpcCount",
        "avgCpuUsagePercentage",
        "avgMemoryUsagePercentage",
        "healthStatus",
    }
)


class VcfaProviderNetworkError(ValueError):
    """A networking op cannot go on (a bad id, an unknown org, a refused preview)."""


def now_iso() -> str:
    """The current UTC time, ISO 8601 with a ``Z``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def job_state(status: object) -> str | None:
    """The job state for a vendor ``status`` value, or ``None`` when there is none."""
    if not isinstance(status, str) or not status:
        return None
    return _JOB_STATES.get(status.upper(), "unknown")


def _ref(row: Mapping[str, Any], key: str) -> tuple[Any, Any]:
    ref = row.get(key)
    if not isinstance(ref, Mapping):
        return None, None
    return ref.get("id"), ref.get("name")


def _status_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"status": row.get("status"), "job_state": job_state(row.get("status"))}


def _ip_space_row(row: Mapping[str, Any]) -> dict[str, Any]:
    region_id, region_name = _ref(row, "regionRef")
    raw_quota = row.get("defaultQuota")
    quota: Mapping[str, Any] = raw_quota if isinstance(raw_quota, Mapping) else {}
    blocks = row.get("internalScopeCidrBlocks")
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        **_status_fields(row),
        "description": row.get("description"),
        "region_id": region_id,
        "region_name": region_name,
        "cidrs": [b.get("cidr") for b in blocks if isinstance(b, Mapping)]
        if isinstance(blocks, list)
        else [],
        "max_cidr_count": quota.get("maxCidrCount"),
        "max_ip_count": quota.get("maxIpCount"),
        "max_subnet_size": quota.get("maxSubnetSize"),
    }


def _gateway_row(row: Mapping[str, Any]) -> dict[str, Any]:
    region_id, region_name = _ref(row, "regionRef")
    tier0_id, tier0_name = _ref(row, "backingRef")
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        **_status_fields(row),
        "description": row.get("description"),
        "region_id": region_id,
        "region_name": region_name,
        "backing_type": row.get("backingType"),
        "tier0_id": tier0_id,
        "tier0_name": tier0_name,
    }


def _association_row(row: Mapping[str, Any]) -> dict[str, Any]:
    ip_space_id, ip_space_name = _ref(row, "ipSpaceRef")
    gateway_id, gateway_name = _ref(row, "providerGatewayRef")
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        **_status_fields(row),
        "ip_space_id": ip_space_id,
        "ip_space_name": ip_space_name,
        "provider_gateway_id": gateway_id,
        "provider_gateway_name": gateway_name,
    }


def _regional_row(row: Mapping[str, Any]) -> dict[str, Any]:
    org_id, org_name = _ref(row, "orgRef")
    region_id, region_name = _ref(row, "regionRef")
    gateway_id, gateway_name = _ref(row, "providerGatewayRef")
    edge_id, edge_name = _ref(row, "serviceEdgeClusterRef")
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        **_status_fields(row),
        "org_id": org_id,
        "org_name": org_name,
        "region_id": region_id,
        "region_name": region_name,
        "provider_gateway_id": gateway_id,
        "provider_gateway_name": gateway_name,
        "edge_cluster_id": edge_id,
        "edge_cluster_name": edge_name,
    }


def _edge_cluster_row(row: Mapping[str, Any]) -> dict[str, Any]:
    region_id, region_name = _ref(row, "regionRef")
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        **_status_fields(row),
        "region_id": region_id,
        "region_name": region_name,
        "health_status": row.get("healthStatus"),
        "node_count": row.get("nodeCount"),
        "org_count": row.get("orgCount"),
        "vpc_count": row.get("vpcCount"),
    }


def _tier0_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("displayName"),
        "description": row.get("description"),
        "parent_tier0_id": row.get("parentTier0Id") or None,
        "already_imported": row.get("alreadyImported"),
    }


@dataclass(frozen=True)
class _Kind:
    """One kind of networking object: where it lives and how a row looks."""

    collection: str
    item: str | None
    item_param: str | None
    row: Callable[[Mapping[str, Any]], dict[str, Any]]
    #: FIQL field per filter param (``region_id`` → ``regionRef.id``).
    filters: Mapping[str, str]


KINDS: Final[dict[str, _Kind]] = {
    "ip_spaces": _Kind(
        PROVIDER_IP_SPACES_PATH,
        PROVIDER_IP_SPACE_PATH,
        "ipSpaceId",
        _ip_space_row,
        {"region_id": "regionRef.id", "name": "name"},
    ),
    "provider_gateways": _Kind(
        PROVIDER_GATEWAYS_PATH,
        PROVIDER_GATEWAY_PATH,
        "gatewayId",
        _gateway_row,
        {"region_id": "regionRef.id", "name": "name"},
    ),
    "ip_space_associations": _Kind(
        PROVIDER_IP_SPACE_ASSOCIATIONS_PATH,
        PROVIDER_IP_SPACE_ASSOCIATION_PATH,
        "ipSpaceAssociationId",
        _association_row,
        {"provider_gateway_id": "providerGatewayRef.id", "ip_space_id": "ipSpaceRef.id"},
    ),
    "regional_networking": _Kind(
        PROVIDER_REGIONAL_NETWORKING_PATH,
        PROVIDER_REGIONAL_NETWORKING_SETTING_PATH,
        "settingId",
        _regional_row,
        {
            "region_id": "regionRef.id",
            "org_id": "orgRef.id",
            "provider_gateway_id": "providerGatewayRef.id",
            "name": "name",
        },
    ),
    "edge_clusters": _Kind(
        PROVIDER_EDGE_CLUSTERS_PATH,
        None,
        None,
        _edge_cluster_row,
        {"region_id": "regionRef.id", "name": "name"},
    ),
}


def state_hash(obj: Mapping[str, Any] | None) -> str:
    """A short hash of an object's state, to bind an approval to what the approver saw.

    ``absent`` for no object. The hash covers the object as read, so any
    change in between (a rename, a new range) shows. Fields that move on
    their own (the realization ``status``, usage counts) are left out.
    """
    if obj is None:
        return "absent"
    kept = {key: value for key, value in obj.items() if key not in _UNBOUND_FIELDS}
    canonical = json.dumps(kept, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def job_id_of(resp: httpx.Response) -> str | None:
    """The vendor task id from a 202 ``Location`` header, or ``None``."""
    location = resp.headers.get("Location") or resp.headers.get("location")
    if not location:
        return None
    return location.rstrip("/").rsplit("/", 1)[-1] or None


async def send(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    method: str,
    path: str,
    *,
    body: Mapping[str, Any] | None = None,
) -> httpx.Response:
    """Send one change and return the raw answer (the ``Location`` header has the task)."""
    return await connector._send_with_retry(target, method, path, operator=operator, json=body)


async def read_one(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    kind: str,
    obj_id: str,
) -> dict[str, Any] | None:
    """One object of *kind* by id, or ``None`` when the appliance has none (HTTP 404)."""
    spec = KINDS[kind]
    if spec.item is None or spec.item_param is None:
        rows = await list_all(
            connector,
            target,
            operator,
            provider_path(target, spec.collection),
            params={"filter": f"id=={obj_id}"},
        )
        return next((row for row in rows if row.get("id") == obj_id), None)
    path = provider_path(target, spec.item, **{spec.item_param: obj_id})
    try:
        return await connector._request_json(target, "GET", path, operator=operator)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


def _fiql(spec: _Kind, params: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """``(filter, problem)``: the FIQL filter for the given filter params."""
    clauses: list[str] = []
    for param, fiql_field in spec.filters.items():
        value = params.get(param)
        if value is None:
            continue
        if not isinstance(value, str) or not value or not fiql_safe(value):
            return None, f"{param} {value!r} cannot be used in a filter"
        clauses.append(f"{fiql_field}=={value}")
    return (";".join(clauses) or None), None


async def list_kind(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    kind: str,
    filters: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Every raw object of *kind* that matches *filters* (exact match on ``name``)."""
    spec = KINDS[kind]
    fiql, problem = _fiql(spec, filters)
    if problem is not None:
        raise VcfaProviderNetworkError(problem)
    rows = await list_all(
        connector,
        target,
        operator,
        provider_path(target, spec.collection),
        params={"filter": fiql} if fiql else None,
    )
    name = filters.get("name")
    return [row for row in rows if name is None or row.get("name") == name]


async def resolve_org(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    org: str,
) -> dict[str, Any]:
    """The organization *org* names (its id or its name), read by id.

    Raises :class:`VcfaProviderNetworkError` for a bad id or an unknown name.
    """
    if org.startswith(_ORG_URN_PREFIX):
        problem = bad_id_reason(org, "organization")
        if problem is not None:
            raise VcfaProviderNetworkError(problem)
        path = provider_path(target, PROVIDER_ORG_PATH, orgUrn=org)
        try:
            return await connector._request_json(target, "GET", path, operator=operator)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise VcfaProviderNetworkError(
                    f"no organization with id {org!r}; list them with vcfa.provider.org.list"
                ) from exc
            raise
    found = await find_org(connector, target, operator, org)
    if found is None or not isinstance(found.get("id"), str):
        raise VcfaProviderNetworkError(
            f"no organization named {org!r}; list them with vcfa.provider.org.list"
        )
    return await resolve_org(connector, target, operator, found["id"])


def status_read(kind: str, **filters: Any) -> dict[str, Any]:
    """The status read a job's answer names: the op and its params."""
    return {"op_id": STATUS_READ_OP, "params": {"kind": kind, **filters}}


async def _tier0_rows(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    params: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """The free Tier-0 gateways of a region or an NSX manager.

    The reference says the list must be read with a ``regionId`` or a
    ``networkProviderId`` filter; without one it is empty. MEHO always sends it.
    """
    region_id, nsx_id = params.get("region_id"), params.get("nsx_manager_id")
    key, value = ("regionId", region_id) if region_id else ("networkProviderId", nsx_id)
    if not isinstance(value, str) or not value or not fiql_safe(value):
        raise VcfaProviderNetworkError(
            "the Tier-0 list needs region_id or nsx_manager_id; without one the appliance "
            "answers an empty list"
        )
    rows = await list_all(
        connector,
        target,
        operator,
        PROVIDER_TIER0_ROUTERS_PATH,
        params={"filter": f"{key}=={value}"},
    )
    return [_tier0_row(row) for row in rows]


async def _org_networking_rows(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    params: Mapping[str, Any],
) -> list[dict[str, Any]]:
    org_value = params.get("org")
    if not isinstance(org_value, str) or not org_value:
        raise VcfaProviderNetworkError("kind org_networking needs 'org' (its name or id)")
    org = await resolve_org(connector, target, operator, org_value)
    path = provider_path(target, PROVIDER_ORG_NETWORKING_PATH, orgUrn=str(org["id"]))
    settings = await connector._request_json(target, "GET", path, operator=operator)
    return [
        {
            "org_id": org.get("id"),
            "org_name": org.get("name"),
            "is_enabled": org.get("isEnabled"),
            "networking_enabled": settings.get("networkingTenancyEnabled"),
            "org_name_for_logs": settings.get("orgNameForLogs") or None,
        }
    ]


async def _object_rows(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    kind: str,
    params: Mapping[str, Any],
) -> list[dict[str, Any]]:
    spec = KINDS[kind]
    obj_id = params.get("id")
    if obj_id is not None:
        problem = bad_id_reason(obj_id, kind)
        if problem is not None:
            raise VcfaProviderNetworkError(problem)
        found = await read_one(connector, target, operator, kind, obj_id)
        return [] if found is None else [spec.row(found)]
    return [spec.row(row) for row in await list_kind(connector, target, operator, kind, params)]


def _single_state(rows: list[dict[str, Any]], one_object: bool) -> tuple[Any, Any]:
    """``(state, vendor_status)`` of a read that names one object."""
    if not one_object:
        return None, None
    if not rows:
        return "not_found", None
    if len(rows) > 1:
        return "ambiguous", None
    return rows[0].get("job_state"), rows[0].get("status")


async def provider_network_list(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.network.list`` — one kind of networking object as flat rows."""
    kind = params["kind"]
    job_id = params.get("job_id")
    base: dict[str, Any] = {
        "kind": kind,
        "reachable": True,
        "checked_at": now_iso(),
        "job_id": job_id,
        "state": None,
        "vendor_status": None,
        "job_state": None,
        "progress": None,
        "error": None,
        "count": 0,
        "items": [],
        "job": None,
        "guidance": None,
    }
    try:
        if job_id is not None and (problem := job_id_problem(job_id)) is not None:
            raise VcfaProviderNetworkError(problem)
        if kind == "tier0_gateways":
            rows = await _tier0_rows(connector, target, operator, params)
        elif kind == "org_networking":
            rows = await _org_networking_rows(connector, target, operator, params)
        else:
            rows = await _object_rows(connector, target, operator, kind, params)
    except VcfaProviderNetworkError as exc:
        return {**base, "state": "invalid_request", "guidance": str(exc)}
    except (httpx.HTTPError, RuntimeError) as exc:
        return not_reachable(base, exc)
    try:
        job = None if job_id is None else await read_job(connector, target, operator, job_id)
    except (httpx.HTTPError, RuntimeError) as exc:
        # Keep the object rows already read; only the job read got no answer.
        return not_reachable({**base, "count": len(rows), "items": rows}, exc)
    one_object = kind in KINDS and (params.get("id") is not None or params.get("name") is not None)
    state, vendor_status = _single_state(rows, one_object)
    guidance = None
    if state == "failed":
        guidance = (
            "VCF Automation could not realize the object. The object holds no error text; "
            "pass the job_id of the change to get the vendor's error text."
        )
    answer = {**base, "state": state, "vendor_status": vendor_status, "count": len(rows)}
    return with_job({**answer, "items": rows, "guidance": guidance}, job)


def drift_reason(op_id: str, current: Mapping[str, Any] | None) -> str | None:
    """Why an approved run must stop: the object changed since the approver saw it.

    ``None`` on an ordinary call, when the approval card holds no state hash,
    and when the object still has the state the approver saw.
    """
    from meho_backplane.operations._preview import approved_preview

    seen = approved_preview(op_id)
    if not seen:
        return None
    seen_hash = seen.get("state_hash")
    if not isinstance(seen_hash, str) or seen_hash == state_hash(current):
        return None
    return (
        "the object changed after the approver saw it; nothing was written. "
        "Preview the call again and ask for a new approval"
    )
