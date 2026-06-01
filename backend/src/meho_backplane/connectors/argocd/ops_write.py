# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval-gated write ops for :class:`ArgoCdConnector` (G3.12-T4 #1405).

G3.12-T1/T2 (#1390/#1391) shipped the connector + the six curated read ops.
This module adds the GitOps **write** surface — the mutating ops that retire
the consumer's hand-run ``kubectl annotate application …
argocd.argoproj.io/refresh=hard`` and ad-hoc ``argocd app sync / rollback /
delete``:

======================================  ===========  ==============================================
op_id                                   safety       argocd-server REST API
======================================  ===========  ==============================================
``argocd.app.sync``                     dangerous    ``POST .../applications/{name}/sync``
``argocd.app.rollback``                 dangerous    ``POST .../applications/{name}/rollback``
``argocd.app.set``                      dangerous    ``PUT  .../applications/{name}/spec``
``argocd.app.refresh``                  caution      ``GET  .../applications/{name}?refresh=hard``
``argocd.app.delete``                   dangerous    ``DELETE .../applications/{name}``
``argocd.appproject.create``            dangerous    ``POST .../projects``
``argocd.appproject.update``            dangerous    ``PUT  .../projects/{name}``
======================================  ===========  ==============================================

Approval routing (load-bearing)
===============================

Every op registers ``requires_approval=True``. The dispatcher's policy gate
(:func:`~meho_backplane.operations._validate.policy_gate`) routes a
``USER``-principal dispatch of a ``requires_approval`` op to the human
approve-queue (``needs-approval``) — G11.7-T1 (#1401) — rather than
hard-denying it, and floors an agent dispatch to ``needs-approval``
regardless of safety level. The handler functions below therefore run only
on the ``_approved=True`` resume path, after a human has approved the parked
request through the queue. There is no bypass and no hard-deny: the
``requires_approval=True`` descriptor flag is the only thing the connector
controls, and it is what engages the queue.

operationState polling (sync / rollback)
=========================================

``app.sync`` and ``app.rollback`` POST a SyncOperation / rollback that
ArgoCD runs **asynchronously**: the POST returns immediately with the
Application object, and the result lands later under
``status.operationState.phase``. The handlers mirror
``vmware.composite.vm.clone``'s task-poll: after the POST they poll
``GET .../applications/{name}`` until ``operationState.phase`` reaches a
terminal value (``Succeeded`` / ``Failed`` / ``Error``) or a bounded timeout
elapses, then return the final phase + message. The terminal set matches
gitops-engine's ``OperationPhase.Completed()`` (Succeeded / Failed / Error).

proposed_effect snapshots (set / delete / appproject.update)
============================================================

``app.set`` / ``appproject.update`` snapshot the spec **before** and
**after** the change; ``app.delete`` snapshots the managed resource tree
(the cascade list) before the delete. Each lands under a ``proposed_effect``
key in the returned result so the reviewer/auditor sees exactly what shifted
or what the cascade will remove. (The substrate computes the durable
``ApprovalRequest.proposed_effect`` at park-time from params; this in-result
block is the post-approval execution evidence the op author can compute —
it needs a live read against the target, which only happens once approved.)

References
----------

* Task: https://github.com/evoila/meho/issues/1405
* Parent initiative: https://github.com/evoila/meho/issues/1387
* Human approve-queue: G11.7-T1 #1401.
* argocd-server gRPC-gateway mappings: ``server/application/application.proto``
  + ``server/project/project.proto`` (``argoproj/argo-cd``).
* OperationPhase terminal set: ``gitops-engine/pkg/sync/common`` —
  ``OperationPhase.Completed()`` = {Succeeded, Failed, Error}.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.argocd.connector import ArgoCdConnector
    from meho_backplane.connectors.argocd.session import ArgoCdTargetLike

__all__ = [
    "ARGOCD_WHEN_TO_USE_WRITE_BY_GROUP",
    "ARGOCD_WRITE_OPS",
    "TERMINAL_OPERATION_PHASES",
    "argocd_app_delete",
    "argocd_app_refresh",
    "argocd_app_rollback",
    "argocd_app_set",
    "argocd_app_sync",
    "argocd_appproject_create",
    "argocd_appproject_update",
]

#: The terminal ``status.operationState.phase`` values an async sync /
#: rollback settles into. Matches gitops-engine's
#: ``OperationPhase.Completed()`` (Succeeded / Failed / Error); ``Running``
#: and ``Terminating`` are the non-terminal phases the poll waits through.
TERMINAL_OPERATION_PHASES: frozenset[str] = frozenset({"Succeeded", "Failed", "Error"})

#: Default seconds to poll operationState for a terminal phase before giving
#: up. Capped by the op's ``poll_timeout_seconds`` schema (max 1800).
_DEFAULT_POLL_TIMEOUT = 300

#: Seconds between operationState polls.
_POLL_INTERVAL = 2.0


def _quote_name(name: Any) -> str:
    """URL-encode an Application / project name for a path segment."""
    return quote(str(name), safe="")


def _project_query(params: dict[str, Any]) -> dict[str, Any]:
    """Build the optional ``project`` scoping query from *params*."""
    project = params.get("project")
    return {"project": project} if project else {}


def _operation_state(app: dict[str, Any]) -> dict[str, Any]:
    """Pull ``status.operationState`` out of an Application, or ``{}``."""
    status = app.get("status")
    if not isinstance(status, dict):
        return {}
    op_state = status.get("operationState")
    return op_state if isinstance(op_state, dict) else {}


def _synced_revision(op_state: dict[str, Any]) -> str | None:
    """Pull the synced revision out of an operationState's SyncResult."""
    sync_result = op_state.get("syncResult")
    if isinstance(sync_result, dict):
        revision = sync_result.get("revision")
        if isinstance(revision, str):
            return revision
    return None


async def _poll_operation_state(
    self: ArgoCdConnector,
    operator: Operator,
    target: ArgoCdTargetLike,
    *,
    name: str,
    query: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    """Poll ``GET .../applications/{name}`` until operationState is terminal.

    Returns ``{phase, message, synced_revision, timed_out}`` where *phase* is
    the terminal ``operationState.phase`` (Succeeded / Failed / Error), or —
    on timeout — the last-observed phase with ``timed_out=True``. Mirrors
    ``vmware.composite.vm.clone``'s ``_poll_clone_task`` bound-deadline loop.
    """
    encoded = _quote_name(name)
    path = f"/api/v1/applications/{encoded}"
    deadline = time.monotonic() + timeout_seconds
    last_phase: str | None = None
    last_message: str | None = None
    last_revision: str | None = None
    while time.monotonic() < deadline:
        app = await self._get_json(target, path, operator=operator, params=query or None)
        op_state = _operation_state(app)
        phase = op_state.get("phase")
        last_phase = phase if isinstance(phase, str) else last_phase
        message = op_state.get("message")
        last_message = message if isinstance(message, str) else last_message
        last_revision = _synced_revision(op_state) or last_revision
        if isinstance(phase, str) and phase in TERMINAL_OPERATION_PHASES:
            return {
                "phase": phase,
                "message": last_message,
                "synced_revision": last_revision,
                "timed_out": False,
            }
        await asyncio.sleep(_POLL_INTERVAL)
    return {
        "phase": last_phase,
        "message": last_message,
        "synced_revision": last_revision,
        "timed_out": True,
    }


# ---------------------------------------------------------------------------
# Application writes
# ---------------------------------------------------------------------------


async def argocd_app_sync(
    self: ArgoCdConnector,
    operator: Operator,
    target: ArgoCdTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``argocd.app.sync`` — POST sync, poll operationState to terminal.

    POSTs ``/api/v1/applications/{name}/sync`` with an ApplicationSyncRequest,
    then polls ``status.operationState.phase`` to a terminal phase. Returns
    the final phase + message + synced revision.
    """
    name = str(params["name"])
    encoded = _quote_name(name)
    body: dict[str, Any] = {"name": name}
    if params.get("revision"):
        body["revision"] = params["revision"]
    if params.get("prune") is not None:
        body["prune"] = bool(params["prune"])
    if params.get("dry_run") is not None:
        body["dryRun"] = bool(params["dry_run"])
    if params.get("project"):
        body["project"] = params["project"]
    await self._write_json(
        target, "POST", f"/api/v1/applications/{encoded}/sync", operator=operator, json=body
    )
    timeout = int(params.get("poll_timeout_seconds") or _DEFAULT_POLL_TIMEOUT)
    outcome = await _poll_operation_state(
        self,
        operator,
        target,
        name=name,
        query=_project_query(params),
        timeout_seconds=timeout,
    )
    return {"name": name, **outcome}


async def argocd_app_rollback(
    self: ArgoCdConnector,
    operator: Operator,
    target: ArgoCdTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``argocd.app.rollback`` — POST rollback, poll operationState to terminal.

    POSTs ``/api/v1/applications/{name}/rollback`` with the int64 history
    ``id`` of a prior deployed revision, then polls operationState to a
    terminal phase. Returns the final phase + message.
    """
    name = str(params["name"])
    encoded = _quote_name(name)
    rollback_id = int(params["id"])
    body: dict[str, Any] = {"name": name, "id": rollback_id}
    if params.get("prune") is not None:
        body["prune"] = bool(params["prune"])
    if params.get("dry_run") is not None:
        body["dryRun"] = bool(params["dry_run"])
    if params.get("project"):
        body["project"] = params["project"]
    await self._write_json(
        target, "POST", f"/api/v1/applications/{encoded}/rollback", operator=operator, json=body
    )
    timeout = int(params.get("poll_timeout_seconds") or _DEFAULT_POLL_TIMEOUT)
    outcome = await _poll_operation_state(
        self,
        operator,
        target,
        name=name,
        query=_project_query(params),
        timeout_seconds=timeout,
    )
    return {"name": name, "rollback_id": rollback_id, **outcome}


async def argocd_app_set(
    self: ArgoCdConnector,
    operator: Operator,
    target: ArgoCdTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``argocd.app.set`` — snapshot spec, PUT new spec, snapshot after.

    Reads the current ApplicationSpec, PUTs the new one to
    ``/api/v1/applications/{name}/spec``, and returns both snapshots under
    ``proposed_effect`` so the reviewer sees what changed.
    """
    name = str(params["name"])
    encoded = _quote_name(name)
    query = _project_query(params)
    before = await self._get_json(
        target, f"/api/v1/applications/{encoded}", operator=operator, params=query or None
    )
    before_spec = before.get("spec") if isinstance(before, dict) else None
    put_query: dict[str, Any] = dict(query)
    if params.get("validate") is not None:
        put_query["validate"] = bool(params["validate"])
    after_spec = await self._write_json(
        target,
        "PUT",
        f"/api/v1/applications/{encoded}/spec",
        operator=operator,
        json=dict(params["spec"]),
        params=put_query or None,
    )
    return {
        "name": name,
        "updated": True,
        "proposed_effect": {"before_spec": before_spec, "after_spec": after_spec},
    }


async def argocd_app_refresh(
    self: ArgoCdConnector,
    operator: Operator,
    target: ArgoCdTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``argocd.app.refresh`` — GET ``?refresh=hard`` to force a reconcile.

    Forces ArgoCD to re-compare the app against Git immediately. Under an
    automated selfHeal policy this can trigger an immediate auto-sync (noted
    in the op's llm_instructions). Returns the refreshed sync + health status.
    """
    name = str(params["name"])
    encoded = _quote_name(name)
    hard = params.get("hard")
    refresh = "normal" if hard is False else "hard"
    query = _project_query(params)
    query["refresh"] = refresh
    app = await self._get_json(
        target, f"/api/v1/applications/{encoded}", operator=operator, params=query
    )
    raw_status = app.get("status") if isinstance(app, dict) else None
    status: dict[str, Any] = raw_status if isinstance(raw_status, dict) else {}
    raw_sync = status.get("sync")
    sync: dict[str, Any] = raw_sync if isinstance(raw_sync, dict) else {}
    raw_health = status.get("health")
    health: dict[str, Any] = raw_health if isinstance(raw_health, dict) else {}
    return {
        "name": name,
        "refresh": refresh,
        "sync_status": sync.get("status"),
        "health_status": health.get("status"),
    }


def _cascade_resources(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """Reduce a resource-tree's nodes to a compact cascade list."""
    nodes = tree.get("nodes") if isinstance(tree, dict) else None
    if not isinstance(nodes, list):
        return []
    out: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        out.append(
            {
                "group": node.get("group"),
                "kind": node.get("kind"),
                "namespace": node.get("namespace"),
                "name": node.get("name"),
            }
        )
    return out


async def argocd_app_delete(
    self: ArgoCdConnector,
    operator: Operator,
    target: ArgoCdTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``argocd.app.delete`` — snapshot the cascade list, then DELETE.

    With ``cascade=true`` (default) ArgoCD deletes the app's managed cluster
    resources too. The handler first reads the resource tree to capture the
    cascade list into ``proposed_effect.cascade_resources``, then DELETEs the
    app.
    """
    name = str(params["name"])
    encoded = _quote_name(name)
    cascade = params.get("cascade")
    cascade = True if cascade is None else bool(cascade)
    query = _project_query(params)

    cascade_resources: list[dict[str, Any]] = []
    if cascade:
        tree = await self._get_json(
            target,
            f"/api/v1/applications/{encoded}/resource-tree",
            operator=operator,
            params=query or None,
        )
        cascade_resources = _cascade_resources(tree)

    delete_query: dict[str, Any] = dict(query)
    delete_query["cascade"] = "true" if cascade else "false"
    propagation = params.get("propagation_policy")
    if propagation:
        delete_query["propagationPolicy"] = propagation
    await self._write_json(
        target,
        "DELETE",
        f"/api/v1/applications/{encoded}",
        operator=operator,
        params=delete_query,
    )
    return {
        "name": name,
        "deleted": True,
        "cascade": cascade,
        "proposed_effect": {"cascade_resources": cascade_resources},
    }


# ---------------------------------------------------------------------------
# AppProject writes
# ---------------------------------------------------------------------------


def _project_name(project: dict[str, Any]) -> str:
    """Pull metadata.name out of an AppProject object."""
    metadata = project.get("metadata") if isinstance(project, dict) else None
    if isinstance(metadata, dict):
        name = metadata.get("name")
        if isinstance(name, str) and name:
            return name
    raise ValueError("argocd appproject write requires project.metadata.name")


async def argocd_appproject_create(
    self: ArgoCdConnector,
    operator: Operator,
    target: ArgoCdTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``argocd.appproject.create`` — POST a ProjectCreateRequest.

    POSTs ``/api/v1/projects`` with ``{project: AppProject, upsert}``. An
    AppProject is the tenancy/authorization boundary for its Applications.
    """
    project = dict(params["project"])
    name = _project_name(project)
    body: dict[str, Any] = {"project": project, "upsert": bool(params.get("upsert", False))}
    await self._write_json(target, "POST", "/api/v1/projects", operator=operator, json=body)
    return {"name": name, "created": True}


async def argocd_appproject_update(
    self: ArgoCdConnector,
    operator: Operator,
    target: ArgoCdTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``argocd.appproject.update`` — snapshot, PUT a ProjectUpdateRequest.

    Snapshots the project's spec before/after the PUT to
    ``/api/v1/projects/{name}`` into ``proposed_effect`` so the reviewer sees
    how the tenancy boundary shifted.
    """
    project = dict(params["project"])
    name = _project_name(project)
    encoded = _quote_name(name)

    before_spec: Any = None
    projects = await self._get_json(target, "/api/v1/projects", operator=operator)
    items = projects.get("items") if isinstance(projects, dict) else None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and _project_metadata_name(item) == name:
                before_spec = item.get("spec")
                break

    body: dict[str, Any] = {"project": project}
    updated = await self._write_json(
        target, "PUT", f"/api/v1/projects/{encoded}", operator=operator, json=body
    )
    after_spec = updated.get("spec") if isinstance(updated, dict) else None
    return {
        "name": name,
        "updated": True,
        "proposed_effect": {"before_spec": before_spec, "after_spec": after_spec},
    }


def _project_metadata_name(item: dict[str, Any]) -> str | None:
    """Pull metadata.name from an AppProject list item, or None."""
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        name = metadata.get("name")
        if isinstance(name, str):
            return name
    return None


# The op-metadata table + curated blurbs + JSON-schema fragments live in a
# sibling module so this handler module stays under the file-size budget.
# Re-exported here so the connector's ``register_operations`` walk keeps
# importing both from ``ops_write``.
from meho_backplane.connectors.argocd.ops_write_schemas import (  # noqa: E402
    ARGOCD_WHEN_TO_USE_WRITE_BY_GROUP,
    ARGOCD_WRITE_OPS,
)
