# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write-op metadata + curated blurbs for the argocd connector (G3.12-T4 #1405).

Split out of :mod:`~meho_backplane.connectors.argocd.ops_write` so the
handler module stays under the code-quality file-size budget (mirrors the
keycloak ``ops_write`` / ``ops_write_schemas`` split #1406). Carries the
``ARGOCD_WRITE_OPS`` registration table, the per-group ``when_to_use``
blurbs (``ARGOCD_WHEN_TO_USE_WRITE_BY_GROUP``), and the reusable JSON-schema
fragments. The handlers live in ``ops_write``; the connector imports
``ARGOCD_WRITE_OPS`` + ``ARGOCD_WHEN_TO_USE_WRITE_BY_GROUP`` (re-exported
from ``ops_write``) for its ``register_operations`` walk.

Every op registers ``requires_approval=True`` — no write reaches the
``argocd-server`` REST surface until a human approves the parked request
through the queue (G11.7-T1 #1401 routes a ``USER``-principal
``requires_approval`` dispatch to ``needs-approval`` rather than
hard-denying it). The agent/MCP path additionally floors every one of these
to ``needs-approval`` regardless of safety level.

Endpoint + field facts are pinned to the ``argoproj/argo-cd``
``server/application/application.proto`` + ``server/project/project.proto``
gRPC-gateway mappings (mirrored in ``assets/swagger.json``):

* ``POST /api/v1/applications/{name}/sync`` — ``ApplicationSyncRequest``.
* ``POST /api/v1/applications/{name}/rollback`` — ``ApplicationRollbackRequest``
  (``id`` is the int64 history id of a prior deployed revision).
* ``PUT  /api/v1/applications/{name}/spec`` — ``ApplicationSpec`` body.
* ``GET  /api/v1/applications/{name}?refresh=hard`` — the hard-refresh
  (a GET that forces an immediate reconcile; under ``selfHeal`` it can
  trigger an auto-sync — flagged in the op's ``llm_instructions``).
* ``DELETE /api/v1/applications/{name}?cascade=…`` — cascade delete.
* ``POST /api/v1/projects`` — ``ProjectCreateRequest`` (``{project, upsert}``).
* ``PUT  /api/v1/projects/{name}`` — ``ProjectUpdateRequest`` (``{project}``).
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.argocd.ops import ArgoCdOp

__all__ = ["ARGOCD_WHEN_TO_USE_WRITE_BY_GROUP", "ARGOCD_WRITE_OPS"]


# ---------------------------------------------------------------------------
# Curated when_to_use blurbs (one per write op group)
# ---------------------------------------------------------------------------

_WHEN_TO_USE_APP_WRITE = (
    "Use to drive an ArgoCD Application's GitOps lifecycle: kick a sync "
    "(argocd.app.sync) and wait for it to reach a terminal phase, roll an "
    "app back to a prior deployed revision (argocd.app.rollback), change an "
    "app's spec / target-revision (argocd.app.set), force an immediate "
    "reconcile (argocd.app.refresh), or remove an app and its managed "
    "resources (argocd.app.delete). Every one of these is approval-gated — a "
    "dispatch parks for a human to approve before it touches the cluster. The "
    "right group when the operator wants to CHANGE cluster state, not just "
    "inspect it (use the read group argocd.app.* for inspection)."
)

_WHEN_TO_USE_APPPROJECT_WRITE = (
    "Use to define or amend the multi-tenant guardrails an AppProject "
    "enforces: create a new AppProject (argocd.appproject.create) or update "
    "an existing one's allow-lists / roles (argocd.appproject.update). Both "
    "are approval-gated — an AppProject is the tenancy/authorization boundary "
    "for every Application bound to it, so a change here widens or narrows "
    "what those apps may deploy and where. The right group when the operator "
    "is provisioning a tenant or adjusting what a project permits."
)

#: Curated ``when_to_use`` blurb per write op group. The group keys carry a
#: ``-write`` suffix so they never collide with the read-op group keys in
#: :data:`~meho_backplane.connectors.argocd.ops.ARGOCD_WHEN_TO_USE_BY_GROUP`
#: when the connector merges both maps in ``register_operations``.
ARGOCD_WHEN_TO_USE_WRITE_BY_GROUP: dict[str, str] = {
    "argocd-apps-write": _WHEN_TO_USE_APP_WRITE,
    "argocd-projects-write": _WHEN_TO_USE_APPPROJECT_WRITE,
}


# ---------------------------------------------------------------------------
# Reusable JSON-schema fragments
# ---------------------------------------------------------------------------

_APP_NAME_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "ArgoCD Application name (metadata.name), e.g. 'guestbook'.",
}

_PROJECT_QUERY_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "Optional AppProject name to scope the operation to. When set, "
        "ArgoCD returns 404 if the app is not in this project (an "
        "authorization scoping hint)."
    ),
}

#: The poll-timeout knob shared by the two long-running, operationState-polling
#: ops (sync / rollback). Bounded so a stuck operation cannot block the
#: dispatch indefinitely; on timeout the handler returns the last-observed
#: phase with ``timed_out=True`` rather than raising.
_POLL_TIMEOUT_PROPERTY: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": 1800,
    "description": (
        "Seconds to poll status.operationState for a terminal phase "
        "(Succeeded / Failed / Error) before giving up (default 300). On "
        "timeout the op returns the last-observed phase with timed_out=true."
    ),
}

_WRITE_CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


ARGOCD_WRITE_OPS: tuple[ArgoCdOp, ...] = (
    # -- argocd.app.sync ---------------------------------------------------
    ArgoCdOp(
        op_id="argocd.app.sync",
        handler_attr="app_sync",
        summary="Sync an ArgoCD Application and wait for a terminal phase (approval-gated).",
        description=(
            "POSTs /api/v1/applications/{name}/sync to reconcile an "
            "Application to its desired Git state, then polls "
            "status.operationState.phase until it reaches a terminal phase "
            "(Succeeded / Failed / Error) or the poll timeout elapses. "
            "Returns the final phase + message and the SyncResult revision. "
            "A bad sync reconciles the whole app at once, so this is "
            "safety_level=dangerous, requires_approval=True. Optional "
            "revision / prune / dry_run shape the SyncOperation."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": _APP_NAME_PROPERTY,
                "project": _PROJECT_QUERY_PROPERTY,
                "revision": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Git revision to sync to (default: the app's target revision).",
                },
                "prune": {
                    "type": "boolean",
                    "description": "Delete resources no longer defined in Git (default false).",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Render + validate without applying (default false).",
                },
                "poll_timeout_seconds": _POLL_TIMEOUT_PROPERTY,
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="argocd-apps-write",
        tags=("write", "argocd", "gitops", "sync"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_APP_WRITE,
            "parameter_hints": {
                "name": "The Application's metadata.name.",
                "revision": "Omit to sync to the app's configured targetRevision.",
                "prune": "true also deletes cluster resources dropped from Git — extra-dangerous.",
            },
            "output_shape": (
                "{name, phase, message, synced_revision, timed_out}. phase is "
                "the terminal operationState phase (Succeeded / Failed / "
                "Error); timed_out=true means the poll gave up before a "
                "terminal phase and phase is the last observed value."
            ),
        },
    ),
    # -- argocd.app.rollback -----------------------------------------------
    ArgoCdOp(
        op_id="argocd.app.rollback",
        handler_attr="app_rollback",
        summary="Roll an ArgoCD Application back to a prior deployed revision (approval-gated).",
        description=(
            "POSTs /api/v1/applications/{name}/rollback with the int64 "
            "history id of a previously-deployed revision (from "
            "status.history[].id), then polls "
            "status.operationState.phase to a terminal phase (Succeeded / "
            "Failed / Error) or the poll timeout. Returns the final phase + "
            "message. safety_level=dangerous, requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": _APP_NAME_PROPERTY,
                "project": _PROJECT_QUERY_PROPERTY,
                "id": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "The deployment history id to roll back to "
                        "(status.history[].id from argocd.app.get)."
                    ),
                },
                "prune": {
                    "type": "boolean",
                    "description": (
                        "Delete resources no longer defined at that revision (default false)."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Render + validate without applying (default false).",
                },
                "poll_timeout_seconds": _POLL_TIMEOUT_PROPERTY,
            },
            "required": ["name", "id"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="argocd-apps-write",
        tags=("write", "argocd", "gitops", "rollback"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_APP_WRITE,
            "parameter_hints": {
                "name": "The Application's metadata.name.",
                "id": "A history id from status.history[] — NOT a Git SHA.",
            },
            "output_shape": (
                "{name, rollback_id, phase, message, synced_revision, "
                "timed_out}. phase is the terminal operationState phase."
            ),
        },
    ),
    # -- argocd.app.set ----------------------------------------------------
    ArgoCdOp(
        op_id="argocd.app.set",
        handler_attr="app_set",
        summary="Update an ArgoCD Application's spec / target revision (approval-gated).",
        description=(
            "PUTs /api/v1/applications/{name}/spec with a new ApplicationSpec "
            "(target revision, source path/params, sync policy). Snapshots "
            "the spec before and after the change into the result's "
            "proposed_effect block so the reviewer/auditor sees exactly what "
            "shifted. Under an automated selfHeal sync policy this is a "
            "deferred whole-app reconcile, so it is safety_level=dangerous, "
            "requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": _APP_NAME_PROPERTY,
                "project": _PROJECT_QUERY_PROPERTY,
                "spec": {
                    "type": "object",
                    "description": (
                        "The full ApplicationSpec to set (replaces the "
                        "current spec). Read the current spec via "
                        "argocd.app.get first and edit it."
                    ),
                    "additionalProperties": True,
                },
                "validate": {
                    "type": "boolean",
                    "description": "Server-side validate the new spec (default true).",
                },
            },
            "required": ["name", "spec"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="argocd-apps-write",
        tags=("write", "argocd", "gitops"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_APP_WRITE,
            "parameter_hints": {
                "spec": "The full ApplicationSpec — get the current one first and edit it.",
            },
            "output_shape": (
                "{name, updated, proposed_effect: {before_spec, after_spec}}. "
                "Compare before_spec vs after_spec to see what changed."
            ),
        },
    ),
    # -- argocd.app.refresh ------------------------------------------------
    ArgoCdOp(
        op_id="argocd.app.refresh",
        handler_attr="app_refresh",
        summary="Force an immediate reconcile of an ArgoCD Application (approval-gated).",
        description=(
            "GETs /api/v1/applications/{name}?refresh=hard to force ArgoCD to "
            "re-compare the app against Git immediately (a hard refresh also "
            "re-renders manifests, bypassing the manifest cache). This does "
            "not itself mutate the cluster, but under an automated selfHeal "
            "sync policy the freshly-detected drift can trigger an immediate "
            "auto-sync — so it is safety_level=caution, requires_approval=True. "
            "Returns the refreshed sync + health status."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": _APP_NAME_PROPERTY,
                "project": _PROJECT_QUERY_PROPERTY,
                "hard": {
                    "type": "boolean",
                    "description": (
                        "true (default) for a hard refresh (re-render "
                        "manifests, bypass cache); false for a normal refresh."
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="argocd-apps-write",
        tags=("write", "argocd", "gitops", "refresh"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_APP_WRITE,
            "parameter_hints": {
                "hard": (
                    "true is the governed replacement for the hand-run "
                    "'kubectl annotate application … argocd.argoproj.io/"
                    "refresh=hard'."
                ),
            },
            "output_shape": (
                "{name, refresh, sync_status, health_status}. CAUTION: under "
                "selfHeal, a refresh that reveals drift can trigger an "
                "immediate auto-sync."
            ),
        },
    ),
    # -- argocd.app.delete -------------------------------------------------
    ArgoCdOp(
        op_id="argocd.app.delete",
        handler_attr="app_delete",
        summary="Delete an ArgoCD Application with cascade (approval-gated).",
        description=(
            "DELETEs /api/v1/applications/{name}. With cascade=true (the "
            "default) ArgoCD also deletes every Kubernetes resource the app "
            "manages. Before deleting, the handler snapshots the app's "
            "resource tree into the result's proposed_effect.cascade_resources "
            "list so the reviewer/auditor sees exactly which cluster objects "
            "the cascade will remove. safety_level=dangerous, "
            "requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": _APP_NAME_PROPERTY,
                "project": _PROJECT_QUERY_PROPERTY,
                "cascade": {
                    "type": "boolean",
                    "description": (
                        "Also delete the app's managed cluster resources "
                        "(default true). false leaves them orphaned."
                    ),
                },
                "propagation_policy": {
                    "type": "string",
                    "enum": ["foreground", "background", "orphan"],
                    "description": "Kubernetes deletion propagation policy (default foreground).",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="argocd-apps-write",
        tags=("write", "argocd", "gitops", "delete"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_APP_WRITE,
            "parameter_hints": {
                "cascade": "true (default) removes managed cluster resources too.",
            },
            "output_shape": (
                "{name, deleted, cascade, proposed_effect: "
                "{cascade_resources: [{group, kind, namespace, name}, ...]}}. "
                "The cascade_resources list is the set of cluster objects the "
                "delete will (cascade=true) remove."
            ),
        },
    ),
    # -- argocd.appproject.create ------------------------------------------
    ArgoCdOp(
        op_id="argocd.appproject.create",
        handler_attr="appproject_create",
        summary="Create an ArgoCD AppProject (approval-gated).",
        description=(
            "POSTs /api/v1/projects with a ProjectCreateRequest "
            "({project: AppProject, upsert}). An AppProject is the "
            "tenancy/authorization boundary for every Application bound to "
            "it — its sourceRepos / destinations / resource allow-lists gate "
            "what those apps may deploy and where. safety_level=dangerous, "
            "requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "project": {
                    "type": "object",
                    "description": "The AppProject object ({metadata, spec}).",
                    "additionalProperties": True,
                },
                "upsert": {
                    "type": "boolean",
                    "description": "Update the project if it already exists (default false).",
                },
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="argocd-projects-write",
        tags=("write", "argocd", "gitops", "appproject"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_APPPROJECT_WRITE,
            "parameter_hints": {
                "project": "AppProject object; at minimum {metadata: {name}, spec: {...}}.",
                "upsert": "true makes the create idempotent (update-if-exists).",
            },
            "output_shape": "{name, created}.",
        },
    ),
    # -- argocd.appproject.update ------------------------------------------
    ArgoCdOp(
        op_id="argocd.appproject.update",
        handler_attr="appproject_update",
        summary="Update an ArgoCD AppProject (approval-gated).",
        description=(
            "PUTs /api/v1/projects/{name} with a ProjectUpdateRequest "
            "({project: AppProject}). Snapshots the project spec before and "
            "after the change into the result's proposed_effect block so the "
            "reviewer/auditor sees how the tenancy/authorization boundary "
            "shifted. safety_level=dangerous, requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "project": {
                    "type": "object",
                    "description": (
                        "The full AppProject object to set (replaces the "
                        "current one). Its metadata.name selects the project; "
                        "read the current project via argocd.appproject.list "
                        "first and edit it."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="argocd-projects-write",
        tags=("write", "argocd", "gitops", "appproject"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_APPPROJECT_WRITE,
            "parameter_hints": {
                "project": "Full AppProject object; metadata.name selects which project.",
            },
            "output_shape": ("{name, updated, proposed_effect: {before_spec, after_spec}}."),
        },
    ),
)
