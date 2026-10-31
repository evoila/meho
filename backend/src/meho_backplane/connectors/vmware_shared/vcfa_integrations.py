# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The integration list of the two Automation connectors, and the flat rows.

``<ns>.integration.list`` (``<ns>`` is ``vcfa.tenant`` or ``vcfa.vra8``)
reads the integrations of the organization, the Git content sources with
their last sync, one integration, one Git sync request or one IaaS request
tracker. The add or change is in :mod:`.vcfa_integration_set`, the remove in
:mod:`.vcfa_integration_delete`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import httpx

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors.vmware_shared.vcfa_integration_calls import (
    CONTENT_SOURCES_PATH,
    INTEGRATION_PATH,
    INTEGRATIONS_PATH,
    LIST_TOP,
    REQUEST_TRACKER_PATH,
    SYNC_HISTORY_PATH,
    SYNC_REQUEST_PATH,
    Calls,
    IaasNotServedError,
    content_rows,
    now_iso,
    path_with,
    tracker_row,
)

__all__ = [
    "GIT_INTEGRATION_TYPES",
    "GIT_SOURCE_TYPES",
    "INTEGRATION_TYPES",
    "SYNC_RUNNING_STATES",
    "git_source_rows",
    "integration_list",
    "integration_row",
    "list_integrations",
    "read_job",
    "sync_history",
]

#: The Git integration types MEHO adds (the vendor's names).
GIT_INTEGRATION_TYPES: Final[tuple[str, ...]] = (
    "com.github.saas",
    "com.gitlab.saas",
    "org.bitbucket",
)
#: Every integration type ``integration.set`` adds.
INTEGRATION_TYPES: Final[tuple[str, ...]] = ("vro", "ipam", *GIT_INTEGRATION_TYPES)
#: The content source types of a Git repository.
GIT_SOURCE_TYPES: Final[frozenset[str]] = frozenset(
    {"com.github", "com.github.enterprise", "com.gitlab", "org.bitbucket"}
)
_SYNC_END_STATES: Final[frozenset[str]] = frozenset({"COMPLETED", "FAILED", "SKIPPED"})
#: The states of a Git sync that has not ended yet.
SYNC_RUNNING_STATES: Final[frozenset[str]] = frozenset({"REQUESTED", "STARTED", "PROCESSING"})
_UNREACHABLE_STATUS: Final[frozenset[int]] = frozenset({502, 503, 504})

_IAAS_404_TEXT: Final[str] = (
    "this organization answers 404 on /iaas/api/integrations. On VCF Automation 9 the "
    "integration ops work only in a 'VM Apps' organization; an 'All Apps' organization has "
    "no IaaS integrations. Use a target that logs in to a VM Apps organization."
)


# ---------------------------------------------------------------------------
# Flat rows
# ---------------------------------------------------------------------------


def _shown(values: Any) -> dict[str, Any]:
    """Vendor properties without a certificate and without any secret-named entry."""
    if not isinstance(values, Mapping):
        return {}
    return {
        str(key): value
        for key, value in values.items()
        if key != "certificate" and not is_secret_param_name(str(key))
    }


def integration_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The flat view of one vendor integration. No password, token or private key."""
    props = row.get("integrationProperties")
    custom = row.get("customProperties")
    state = row.get("status")
    if state is None and isinstance(custom, Mapping):
        state = custom.get("status")
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "integration_type": row.get("integrationType"),
        "description": row.get("description"),
        "state": state,
        "has_certificate": bool(
            (isinstance(props, Mapping) and props.get("certificate")) or row.get("certificateInfo")
        ),
        "integration_properties": _shown(props),
        "custom_properties": _shown(custom),
        "created_at": row.get("createdAt"),
        "updated_at": row.get("updatedAt"),
    }


def _source_row(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("config")
    config: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "type_id": row.get("typeId"),
        "project_id": row.get("projectId"),
        "sync_enabled": row.get("syncEnabled"),
        "integration_id": config.get("integrationId"),
        "repository": config.get("repository"),
        "branch": config.get("branch"),
        "path": config.get("path"),
        "content_type": config.get("contentType"),
        "last_sync": None,
    }


async def list_integrations(calls: Calls) -> list[dict[str, Any]]:
    """The vendor integration rows. A 404 means the organization has no IaaS API."""
    try:
        page = await calls.iaas("GET", INTEGRATIONS_PATH, **{"$top": LIST_TOP})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise IaasNotServedError(_IAAS_404_TEXT) from None
        raise
    return content_rows(page)


async def sync_history(calls: Calls, source_ids: list[str]) -> list[dict[str, Any]]:
    """The vendor's sync history rows of *source_ids*, newest first."""
    if not source_ids:
        return []
    page = await calls.json("GET", SYNC_HISTORY_PATH, params={"sourceIds": source_ids})
    rows = content_rows(page)
    return sorted(rows, key=lambda r: str(r.get("timestamp") or ""), reverse=True)


async def git_source_rows(
    calls: Calls, *, integration_id: str | None = None
) -> list[dict[str, Any]]:
    """The Git content sources, each with its last sync (or ``None``)."""
    query = {"integrationId": integration_id} if integration_id else None
    sources = [
        _source_row(row)
        for row in content_rows(await calls.json("GET", CONTENT_SOURCES_PATH, params=query))
        if row.get("typeId") in GIT_SOURCE_TYPES
        and (integration_id is None or _source_row(row)["integration_id"] == integration_id)
    ]
    try:
        history = await sync_history(calls, [str(row["id"]) for row in sources if row["id"]])
    except httpx.HTTPStatusError:
        history = []
    for source in sources:
        last = next((h for h in history if h.get("sourceId") == source["id"]), None)
        if last is not None:
            source["last_sync"] = {
                "request_id": last.get("requestId"),
                "status": last.get("status"),
                "at": last.get("timestamp"),
            }
    return sources


# ---------------------------------------------------------------------------
# integration.list
# ---------------------------------------------------------------------------


def _job_view(kind: str, job_id: str, read: Mapping[str, Any]) -> dict[str, Any]:
    if kind == "git_sync":
        state = read.get("status")
        return {
            "kind": kind,
            "job_id": read.get("requestId") or job_id,
            "state": state,
            "progress": None,
            "message": read.get("message"),
            "source_id": read.get("sourceId"),
            "project_id": read.get("projectId"),
            "done": state in _SYNC_END_STATES,
            "checked_at": now_iso(),
        }
    row = tracker_row(read)
    return {
        "kind": kind,
        "job_id": row["id"] or job_id,
        "state": row["status"],
        "progress": row["progress"],
        "message": row["message"],
        "resources": row["resources"],
        "done": row["status"] in {"FINISHED", "FAILED"},
        "checked_at": now_iso(),
    }


async def read_job(calls: Calls, kind: str, job_id: str) -> dict[str, Any]:
    """Read one vendor job: a Git sync request or an IaaS request tracker.

    A system that does not answer gives ``state: not_reachable`` with the job
    id and the time, never ``failed``. MEHO keeps no job state between calls.
    """
    if kind == "git_sync":
        path = path_with(SYNC_REQUEST_PATH, job_id, "sync_request_id")
    else:
        path = path_with(REQUEST_TRACKER_PATH, job_id, "request_tracker_id")
    try:
        read = await (calls.json("GET", path) if kind == "git_sync" else calls.iaas("GET", path))
    except httpx.TransportError as exc:
        return _unreachable(kind, job_id, type(exc).__name__)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in _UNREACHABLE_STATUS:
            return _unreachable(kind, job_id, f"HTTP {exc.response.status_code}")
        if exc.response.status_code == 404:
            return {
                "kind": kind,
                "job_id": job_id,
                "state": "not_found",
                "progress": None,
                "message": "the vendor knows no job with this id",
                "done": True,
                "checked_at": now_iso(),
            }
        raise
    return _job_view(kind, job_id, read if isinstance(read, Mapping) else {})


def _unreachable(kind: str, job_id: str, why: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "job_id": job_id,
        "state": "not_reachable",
        "progress": None,
        "message": (
            f"the system did not answer ({why}). This does not mean the job failed. "
            "Read again later with the same id."
        ),
        "done": False,
        "checked_at": now_iso(),
    }


async def integration_list(calls: Calls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<ns>.integration.list``."""
    picked = [key for key in ("id", "sync_request_id", "request_tracker_id") if params.get(key)]
    if len(picked) > 1:
        raise ValueError(
            "give at most one of id, sync_request_id and request_tracker_id; nothing was read"
        )
    if params.get("sync_request_id"):
        job = await read_job(calls, "git_sync", str(params["sync_request_id"]))
        return {"view": "sync_request", "rows": [], "total": 0, "job": job}
    if params.get("request_tracker_id"):
        job = await read_job(calls, "request_tracker", str(params["request_tracker_id"]))
        return {"view": "request_tracker", "rows": [], "total": 0, "job": job}
    if params.get("id"):
        row = await calls.iaas("GET", path_with(INTEGRATION_PATH, params["id"], "id"))
        return {"view": "integration", "rows": [integration_row(row)], "total": 1, "job": None}
    if params.get("view") == "git_sources":
        rows = await git_source_rows(calls)
        return {"view": "git_sources", "rows": rows, "total": len(rows), "job": None}
    wanted = params.get("integration_type")
    rows = [
        integration_row(row)
        for row in await list_integrations(calls)
        if not wanted or row.get("integrationType") == wanted
    ]
    return {"view": "integrations", "rows": rows, "total": len(rows), "job": None}
