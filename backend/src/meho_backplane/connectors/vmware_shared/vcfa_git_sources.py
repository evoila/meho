# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``<ns>.git_repo.set``: add a Git repository to a project and sync it, or remove it.

Add (``action: add``) is a vendor job. The steps:

1. **find** — look for a content source with the same name in the project.
   The same settings: MEHO reuses it (safe to run twice). Other settings:
   MEHO stops and changes nothing.
2. **create** — ``POST /content/api/sources`` with the Git integration, the
   repository, the branch, the folder and the content type.
3. **check_running** — read the sync history of the source. When a sync of
   it still runs, MEHO returns that sync's id and starts nothing. This is a
   help, not a lock: two callers can both check before either starts.
4. **sync** — ``POST /content/api/sourcecontrol/sync-requests``. The vendor
   accepts the sync and runs it after the call returns.

The answer says ``accepted`` with the vendor's sync id and the status read
(``<ns>.integration.list`` with ``sync_request_id``). It never says the sync
is done. A failure part-way returns the steps that ran, the failed step with
the vendor's error and the steps that did not run; MEHO undoes nothing.

Remove (``action: remove``) deletes one content source by id.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

import httpx

from meho_backplane.connectors.vmware_shared.vcfa_integration_calls import (
    CONTENT_SOURCE_PATH,
    CONTENT_SOURCES_PATH,
    INTEGRATION_PATH,
    SYNC_REQUESTS_PATH,
    Calls,
    Steps,
    path_with,
    vendor_text,
)
from meho_backplane.connectors.vmware_shared.vcfa_integrations import (
    SYNC_RUNNING_STATES,
    git_source_rows,
    sync_history,
)

__all__ = ["CONTENT_TYPES", "git_repo_set"]

#: What a Git content source imports (the vendor's names).
CONTENT_TYPES: Final[tuple[str, ...]] = ("BLUEPRINT", "ABX_SCRIPTS", "TERRAFORM_CONFIGURATION")

#: Git integration type -> content source type.
_SOURCE_TYPE: Final[dict[str, str]] = {
    "com.github.saas": "com.github",
    "com.gitlab.saas": "com.gitlab",
    "org.bitbucket": "org.bitbucket",
}

_REPOSITORY: Final = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+")
_BRANCH: Final = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*")
_FOLDER: Final = re.compile(r"[A-Za-z0-9_. /-]*")


def _check_text(value: Any, pattern: re.Pattern[str], what: str) -> str:
    text = value if isinstance(value, str) else ""
    parts = text.split("/")
    if not pattern.fullmatch(text) or ".." in parts or "." in parts or text.endswith("/"):
        raise ValueError(f"{what} {value!r} is not a valid {what}; nothing was sent")
    return text


def _config(params: Mapping[str, Any]) -> dict[str, Any]:
    folder = params.get("path") or ""
    if folder:
        _check_text(folder, _FOLDER, "path")
    return {
        "integrationId": params["integration_id"],
        "repository": _check_text(params.get("repository"), _REPOSITORY, "repository"),
        "branch": _check_text(params.get("branch"), _BRANCH, "branch"),
        "path": folder,
        "contentType": params["content_type"],
    }


def _same(source: Mapping[str, Any], config: Mapping[str, Any]) -> bool:
    return all(
        (source.get(key) or "") == (config[name] or "")
        for key, name in (
            ("integration_id", "integrationId"),
            ("repository", "repository"),
            ("branch", "branch"),
            ("path", "path"),
            ("content_type", "contentType"),
        )
    )


def _answer(
    status: str,
    guidance: str,
    steps: Steps,
    source: Mapping[str, Any] | None = None,
    job: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "source": dict(source) if source is not None else None,
        "job": dict(job) if job is not None else None,
        "steps": steps.rows(),
        "guidance": guidance,
    }


async def _source_type(calls: Calls, integration_id: Any) -> str:
    try:
        row = await calls.iaas("GET", path_with(INTEGRATION_PATH, integration_id, "integration_id"))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ValueError(
                f"no integration has the id {integration_id!r}; nothing was sent"
            ) from None
        raise
    kind = str(row.get("integrationType") or "") if isinstance(row, Mapping) else ""
    if kind not in _SOURCE_TYPE:
        raise ValueError(
            f"integration {integration_id!r} has the type {kind!r}, not a Git type "
            f"({', '.join(_SOURCE_TYPE)}); nothing was sent"
        )
    return _SOURCE_TYPE[kind]


def _job(calls: Calls, job_id: Any, state: Any, *, already_running: bool) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "state": state,
        "already_running": already_running,
        "status_read": {
            "op_id": calls.op("integration.list"),
            "params": {"sync_request_id": job_id},
        },
    }


async def _find_or_create(
    calls: Calls, params: Mapping[str, Any], config: dict[str, Any], steps: Steps
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(source, early answer)``; exactly one is set."""
    type_id = await _source_type(calls, params["integration_id"])
    same_name = [
        row
        for row in await git_source_rows(calls)
        if row["name"] == params["name"] and row["project_id"] == params["project_id"]
    ]
    steps.done("find")
    if same_name and not _same(same_name[0], config):
        return None, _answer(
            "exists",
            "The project has a content source with this name and other settings. Nothing was "
            "changed. Remove it first, or use another name.",
            steps,
            same_name[0],
        )
    if same_name:
        return same_name[0], None
    body = {
        "name": params["name"],
        "typeId": type_id,
        "projectId": params["project_id"],
        "syncEnabled": bool(params.get("sync_enabled", True)),
        "config": config,
    }
    if params.get("description"):
        body["description"] = params["description"]
    try:
        created = await calls.json("POST", CONTENT_SOURCES_PATH, json=body)
    except httpx.HTTPStatusError as exc:
        steps.failed("create", vendor_text(exc))
        return None, _answer("failed", "The vendor refused the new content source.", steps)
    steps.done("create")
    return {
        "id": created.get("id") if isinstance(created, Mapping) else None,
        "name": params["name"],
        "type_id": type_id,
        "project_id": params["project_id"],
        "sync_enabled": body["syncEnabled"],
        "integration_id": config["integrationId"],
        "repository": config["repository"],
        "branch": config["branch"],
        "path": config["path"],
        "content_type": config["contentType"],
        "last_sync": None,
    }, None


async def _add(calls: Calls, params: Mapping[str, Any]) -> dict[str, Any]:
    missing = [
        k for k in ("project_id", "name", "integration_id", "content_type") if not params.get(k)
    ]
    if missing:
        raise ValueError(f"action add needs {', '.join(missing)}; nothing was sent")
    config = _config(params)
    steps = Steps("find", "create", "check_running", "sync")
    source, early = await _find_or_create(calls, params, config, steps)
    if early is not None or source is None:
        return early or _answer("failed", "No content source.", steps)
    try:
        history = await sync_history(calls, [str(source["id"])])
        steps.done("check_running")
    except httpx.HTTPError as exc:
        steps.failed("check_running", f"could not read the sync history ({type(exc).__name__})")
        history = []
    running = next((row for row in history if row.get("status") in SYNC_RUNNING_STATES), None)
    if running is not None:
        return _answer(
            "accepted",
            "A sync of this source already runs; MEHO started no second one. Follow it with "
            "the status read until it ends.",
            steps,
            source,
            _job(calls, running.get("requestId"), running.get("status"), already_running=True),
        )
    try:
        sync = await calls.json("POST", SYNC_REQUESTS_PATH, json={"sourceId": source["id"]})
    except httpx.HTTPStatusError as exc:
        steps.failed("sync", vendor_text(exc))
        return _answer(
            "failed",
            "The content source is there, but the vendor refused the sync. Nothing was undone. "
            f"Read the source with {calls.op('integration.list')} view git_sources.",
            steps,
            source,
        )
    steps.done("sync")
    sync = sync if isinstance(sync, Mapping) else {}
    return _answer(
        "accepted",
        "The vendor accepted the sync; it is not done yet. Read its state with the status "
        "read until it says COMPLETED, FAILED or SKIPPED.",
        steps,
        source,
        _job(calls, sync.get("requestId"), sync.get("status"), already_running=False),
    )


async def _remove(calls: Calls, params: Mapping[str, Any]) -> dict[str, Any]:
    path = path_with(CONTENT_SOURCE_PATH, params.get("id"), "id")
    steps = Steps("remove")
    try:
        await calls.json("GET", path)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _answer(
                "unchanged", "No content source has this id. Nothing was removed.", steps
            )
        raise
    try:
        await calls.send("DELETE", path)
    except httpx.HTTPStatusError as exc:
        steps.failed("remove", vendor_text(exc))
        return _answer("failed", "The vendor refused the remove.", steps)
    steps.done("remove")
    return _answer("removed", "The content source is removed.", steps)


async def git_repo_set(calls: Calls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<ns>.git_repo.set``."""
    if params.get("action") == "remove":
        return await _remove(calls, params)
    return await _add(calls, params)
