# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Curated read ops exposed by :class:`ArgoCdConnector` (G3.12-T2 #1391).

G3.12-T1 (#1390) shipped the connector skeleton with zero operations. This
module adds the **curated read core** — the bounded set of GitOps-visibility
ops that let an RDC operator see what ArgoCD sees without reaching for the
``argocd`` CLI or raw ``kubectl`` against the ``argocd`` namespace:

* ``argocd.app.list`` -- ``GET /api/v1/applications`` (optional ``projects``
  / ``selector`` filters); apps with sync/health status.
* ``argocd.app.get`` -- ``GET /api/v1/applications/{name}``; one app's full
  spec + status.
* ``argocd.app.diff`` -- ``GET /api/v1/applications/{name}/managed-resources``;
  the per-resource desired-vs-live drift an operator otherwise gets from
  ``argocd app diff <app>`` (each item carries ``liveState`` / ``targetState``
  / ``normalizedLiveState`` / ``predictedLiveState`` / ``diff``).
* ``argocd.app.resource_tree`` -- ``GET /api/v1/applications/{name}/resource-tree``;
  the reconciled resource tree with per-node sync/health.
* ``argocd.appproject.list`` -- ``GET /api/v1/projects``; AppProjects + their
  allow-lists.
* ``argocd.repo.list`` -- ``GET /api/v1/repositories``; configured repos +
  connection state.

All six are ``safety_level="safe"`` + ``requires_approval=False`` and carry a
``read-only`` tag — this Task registers no write/mutating op (``app.sync`` /
``rollback`` / ``set`` are a deferred, approval-gated follow-up).

The dataclass + tuple shape mirrors the bind9 (#367) and Kubernetes
connectors so the registration walk in
:func:`~meho_backplane.connectors.argocd.register_argocd_typed_operations`
reads identically to those siblings. The handler methods live on
:class:`~meho_backplane.connectors.argocd.connector.ArgoCdConnector` (each a
thin ``_get_json`` call) so the descriptor's ``handler_ref`` round-trips
through the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.import_handler` walk
against a ``module.ClassName.method`` dotted path — the same shape Harbor's
``robot_create`` / ``robot_delete`` handlers use.

Endpoint + field facts are pinned to the ArgoCD ``argocd-server`` Swagger
spec (``argoproj/argo-cd`` ``assets/swagger.json``): the application-collection
endpoints wrap results under ``items``; ``managed-resources`` returns
``{"items": [ResourceDiff, ...]}`` where each ``ResourceDiff`` carries the
``liveState`` / ``targetState`` pair that drives the CLI diff. The
``managed-resources`` and ``resource-tree`` paths use ``{applicationName}`` as
their path placeholder; ``app.get`` uses ``{name}`` — both are populated from
the handler's ``name`` param.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["ARGOCD_OPS", "ARGOCD_WHEN_TO_USE_BY_GROUP", "ArgoCdOp"]


@dataclass(frozen=True)
class ArgoCdOp:
    """Metadata for one argocd op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the registrar can splat the dataclass into the helper without
    per-op boilerplate. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.argocd.connector.ArgoCdConnector` that
    exposes the async handler; the registrar resolves the bound method against
    the class at registration time so the dispatcher's
    :func:`~meho_backplane.operations._handler_resolve.import_handler` walk can
    recover the callable from the persisted ``module.ClassName.method`` path.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` blurbs per group. ``register_typed_operation``
#: requires a non-empty string whenever ``group_key`` is set (G0.9-T4a #731);
#: the registrar looks each op's ``group_key`` up here. Three groups: the
#: application read surface, the project allow-list surface, and the
#: repository inventory surface.
ARGOCD_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "argocd-apps": (
        "Use to inspect ArgoCD Applications and their GitOps reconciliation "
        "state: list every app with its sync (Synced/OutOfSync) and health "
        "(Healthy/Degraded/Progressing) status (argocd.app.list), read one "
        "app's full spec + status (argocd.app.get), see the per-resource "
        "desired-vs-live drift an operator gets from 'argocd app diff' "
        "(argocd.app.diff), or walk the reconciled resource tree with "
        "per-node health (argocd.app.resource_tree). The right group when the "
        "question is 'is this app in sync?', 'what changed?', or 'which child "
        "resources are unhealthy?'. Read-only — no sync/rollback here."
    ),
    "argocd-projects": (
        "Use to list ArgoCD AppProjects and their allow-lists "
        "(argocd.appproject.list): which source repos, destination "
        "clusters/namespaces, and resource kinds each project permits. The "
        "right group when confirming whether an Application's source or "
        "destination is permitted by its project before (or while diagnosing) "
        "a reconciliation, or when auditing multi-tenant guardrails."
    ),
    "argocd-repos": (
        "Use to list the Git/Helm repositories configured in ArgoCD and their "
        "connection state (argocd.repo.list): URL, type, and whether ArgoCD "
        "can currently reach and authenticate to each. The right group when "
        "diagnosing a 'ComparisonError'/'repository not accessible' app "
        "condition or confirming a repo is registered before pointing an "
        "Application at it."
    ),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

#: The application-name path param shared by app.get / app.diff /
#: app.resource_tree. Required, non-empty.
_APP_NAME_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "ArgoCD Application name (metadata.name), e.g. 'guestbook'.",
}

#: The optional ``project`` scoping query param ArgoCD accepts on the
#: per-application endpoints. ArgoCD returns 404 (not 403) for an app the
#: caller can't see when ``project`` is supplied, so it doubles as an
#: authorization-scoping hint.
_PROJECT_QUERY_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "Optional AppProject name to scope the lookup to. When set, ArgoCD "
        "returns 404 if the app is not in this project (an authorization "
        "scoping hint)."
    ),
}


# ---------------------------------------------------------------------------
# argocd.app.list
# ---------------------------------------------------------------------------

_APP_LIST = ArgoCdOp(
    op_id="argocd.app.list",
    handler_attr="app_list",
    summary="List ArgoCD Applications with their sync and health status.",
    description=(
        "Lists ArgoCD Applications via GET /api/v1/applications, optionally "
        "filtered by one or more projects and/or a Kubernetes label selector. "
        "Each item carries the app's spec (source repo/path/target revision, "
        "destination cluster/namespace) plus its status — most usefully "
        "status.sync.status (Synced / OutOfSync) and status.health.status "
        "(Healthy / Degraded / Progressing / Missing). Use as the entry point "
        "for any 'which apps are out of sync / unhealthy?' question. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "projects": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "Optional list of AppProject names to filter by. Omit to "
                    "list applications across all projects the credential can see."
                ),
            },
            "selector": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Optional Kubernetes label selector "
                    "(e.g. 'team=payments,env=prod') filtering the returned apps."
                ),
            },
        },
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "items": {"type": ["array", "null"]},
            "metadata": {"type": "object"},
        },
        "additionalProperties": True,
    },
    group_key="argocd-apps",
    tags=("read-only", "argocd", "gitops"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call first when the operator asks about the fleet of ArgoCD apps "
            "or wants to find which apps are OutOfSync or unhealthy. Narrow "
            "with 'projects' and/or 'selector' when the operator named a "
            "project or label."
        ),
        "parameter_hints": {
            "projects": "List of AppProject names; omit for all projects.",
            "selector": "Kubernetes label selector string; omit for no label filter.",
        },
        "output_shape": (
            "{items: [Application, ...], metadata: {...}}. Read each item's "
            "status.sync.status and status.health.status for the at-a-glance "
            "state; metadata.name identifies the app for follow-up ops."
        ),
    },
)


# ---------------------------------------------------------------------------
# argocd.app.get
# ---------------------------------------------------------------------------

_APP_GET = ArgoCdOp(
    op_id="argocd.app.get",
    handler_attr="app_get",
    summary="Get one ArgoCD Application's full spec and status by name.",
    description=(
        "Returns a single ArgoCD Application via "
        "GET /api/v1/applications/{name} — the full object: spec (source, "
        "destination, sync policy) and status (sync state, health, "
        "operationState, the conditions list, and the resources summary). "
        "Use after argocd.app.list to drill into one app, or directly when "
        "the operator names an app. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": _APP_NAME_PROPERTY,
            "project": _PROJECT_QUERY_PROPERTY,
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "metadata": {"type": "object"},
            "spec": {"type": "object"},
            "status": {"type": "object"},
        },
        "additionalProperties": True,
    },
    group_key="argocd-apps",
    tags=("read-only", "argocd", "gitops"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator names a specific app and wants its full "
            "detail (sync policy, conditions, operationState) beyond the "
            "summary argocd.app.list returns."
        ),
        "parameter_hints": {
            "name": "The Application's metadata.name (from argocd.app.list).",
            "project": "Optional AppProject to scope the lookup; omit unless known.",
        },
        "output_shape": (
            "A single Application object: {metadata, spec, status}. "
            "status.conditions surfaces ComparisonError / SyncError detail."
        ),
    },
)


# ---------------------------------------------------------------------------
# argocd.app.diff
# ---------------------------------------------------------------------------

_APP_DIFF = ArgoCdOp(
    op_id="argocd.app.diff",
    handler_attr="app_diff",
    summary="Return the desired-vs-live drift for an ArgoCD Application.",
    description=(
        "Returns the per-resource desired-vs-live drift for an ArgoCD "
        "Application via GET /api/v1/applications/{name}/managed-resources — "
        "the same managed-resources delta the 'argocd app diff <app>' CLI "
        "renders. Each item is one managed resource carrying its group / kind "
        "/ namespace / name plus liveState (the object currently in the "
        "cluster), targetState (the desired object rendered from Git via "
        "Helm/Kustomize), normalizedLiveState and predictedLiveState (the "
        "normalized pair the controller actually compares), and a 'modified' "
        "flag. Use to answer 'what exactly is out of sync?' for an OutOfSync "
        "app. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": _APP_NAME_PROPERTY,
            "project": _PROJECT_QUERY_PROPERTY,
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "items": {"type": ["array", "null"]},
        },
        "additionalProperties": True,
    },
    group_key="argocd-apps",
    tags=("read-only", "argocd", "gitops", "diff"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when an app is OutOfSync and the operator wants the concrete "
            "delta — which managed resources differ and how their live state "
            "diverges from the desired Git-rendered state. This is the "
            "API-level equivalent of 'argocd app diff'."
        ),
        "parameter_hints": {
            "name": "The Application's metadata.name.",
            "project": "Optional AppProject to scope the lookup; omit unless known.",
        },
        "output_shape": (
            "{items: [ResourceDiff, ...]}. Per item compare liveState vs "
            "targetState (or the normalized*/predicted* pair); 'modified'=true "
            "marks a drifted resource. liveState empty + targetState set means "
            "a resource missing from the cluster."
        ),
    },
)


# ---------------------------------------------------------------------------
# argocd.app.resource_tree
# ---------------------------------------------------------------------------

_APP_RESOURCE_TREE = ArgoCdOp(
    op_id="argocd.app.resource_tree",
    handler_attr="app_resource_tree",
    summary="Return an ArgoCD Application's reconciled resource tree.",
    description=(
        "Returns the reconciled resource tree for an ArgoCD Application via "
        "GET /api/v1/applications/{name}/resource-tree: the nodes (each "
        "managed/child Kubernetes object with its group/kind/namespace/name, "
        "parentRefs, health, and sync status), orphanedNodes (objects in the "
        "destination namespace not managed by this app), hosts, and "
        "shardsCount. Use to see the full hierarchy of what an app manages and "
        "where in the tree a health problem sits. safety_level=safe, "
        "read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": _APP_NAME_PROPERTY,
            "project": _PROJECT_QUERY_PROPERTY,
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "nodes": {"type": ["array", "null"]},
            "orphanedNodes": {"type": ["array", "null"]},
            "hosts": {"type": ["array", "null"]},
            "shardsCount": {"type": ["integer", "string", "null"]},
        },
        "additionalProperties": True,
    },
    group_key="argocd-apps",
    tags=("read-only", "argocd", "gitops"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator wants the child-resource hierarchy of an "
            "app and per-node health/sync — e.g. to find which Deployment / "
            "Pod under an app is Degraded, or to spot orphaned resources."
        ),
        "parameter_hints": {
            "name": "The Application's metadata.name.",
            "project": "Optional AppProject to scope the lookup; omit unless known.",
        },
        "output_shape": (
            "{nodes: [ResourceNode, ...], orphanedNodes: [...], hosts: [...], "
            "shardsCount: int}. Each node carries health.status and (where "
            "applicable) parentRefs linking it into the tree."
        ),
    },
)


# ---------------------------------------------------------------------------
# argocd.appproject.list
# ---------------------------------------------------------------------------

_APPPROJECT_LIST = ArgoCdOp(
    op_id="argocd.appproject.list",
    handler_attr="appproject_list",
    summary="List ArgoCD AppProjects and their allow-lists.",
    description=(
        "Lists ArgoCD AppProjects via GET /api/v1/projects. Each item carries "
        "the project's spec: sourceRepos (permitted Git repo URL globs), "
        "destinations (permitted cluster server + namespace pairs), and the "
        "cluster/namespace resource allow- and deny-lists that gate what "
        "Applications in the project may deploy. Use to audit multi-tenant "
        "guardrails or to confirm an app's source/destination is permitted by "
        "its project. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "items": {"type": ["array", "null"]},
            "metadata": {"type": "object"},
        },
        "additionalProperties": True,
    },
    group_key="argocd-projects",
    tags=("read-only", "argocd", "gitops"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator asks which projects exist or what a "
            "project permits, or to check whether an Application's source repo "
            "/ destination is allowed by its AppProject."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{items: [AppProject, ...], metadata: {...}}. Read each item's "
            "spec.sourceRepos and spec.destinations for the allow-lists."
        ),
    },
)


# ---------------------------------------------------------------------------
# argocd.repo.list
# ---------------------------------------------------------------------------

_REPO_LIST = ArgoCdOp(
    op_id="argocd.repo.list",
    handler_attr="repo_list",
    summary="List configured ArgoCD repositories and their connection state.",
    description=(
        "Lists the Git/Helm repositories configured in ArgoCD via "
        "GET /api/v1/repositories. Each item carries the repo URL, type "
        "(git/helm), name, and a connectionState (status Successful / Failed "
        "plus a message and the attemptedAt timestamp) reflecting whether "
        "ArgoCD can currently reach and authenticate to the repo. Use to "
        "diagnose a 'repository not accessible' / ComparisonError app "
        "condition or to confirm a repo is registered. safety_level=safe, "
        "read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "items": {"type": ["array", "null"]},
            "metadata": {"type": "object"},
        },
        "additionalProperties": True,
    },
    group_key="argocd-repos",
    tags=("read-only", "argocd", "gitops"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator asks which repos ArgoCD knows about, or to "
            "check repo reachability/auth when an app reports a "
            "ComparisonError or 'repository not accessible' condition."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{items: [Repository, ...], metadata: {...}}. Read each item's "
            "connectionState.status (Successful / Failed) and .message."
        ),
    },
)


#: The ops :class:`ArgoCdConnector` registers at lifespan startup — the full
#: G3.12-T2 read core. Ordered apps → projects → repos to match the
#: operator's typical drill path.
ARGOCD_OPS: tuple[ArgoCdOp, ...] = (
    _APP_LIST,
    _APP_GET,
    _APP_DIFF,
    _APP_RESOURCE_TREE,
    _APPPROJECT_LIST,
    _REPO_LIST,
)
