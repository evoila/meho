# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The argocd connector's hand-coded route table — its full REST surface (#2987).

Every ``argocd-server`` endpoint the connector dispatches is declared
here exactly once as a ``"METHOD:/path"`` route constant, and every
handler derives both its HTTP verb (:func:`route_method`) and its
concrete request path (:func:`route_path`) from the same constant. That
single-sourcing is what makes the spec-reconcile lane
(``backend/tests/test_connectors_argocd_spec_reconcile.py``) a live
introspection instead of a hardcoded mirror (the #2944 pattern, per
``docs/decisions/spec-reconcile-guards-standard.md``): the declared set
the lane asserts against the pinned ``argocd-3.3`` shelf spec *is* the
set dispatch executes, so a path edit here flows into the reconcile
automatically — and a new inline path literal in a handler is the drift
the lane's pinned-manifest guard exists to catch in review.

Template segments carry the **vendor's own parameter names** from the
pinned ``argocd-server`` Swagger 2.0 document (``argoproj/argo-cd``
``assets/swagger.json``), because the reconcile compares op_ids
byte-for-byte: the gRPC-gateway names the Application segment ``{name}``
on most application routes but ``{applicationName}`` on
``managed-resources`` / ``resource-tree``, and the project-update
segment is ``{project.metadata.name}`` (the proto field path). The
runtime request is unaffected — :func:`route_path` substitutes whatever
single placeholder the template carries with the URL-encoded resource
name.
"""

from __future__ import annotations

import re
from urllib.parse import quote

__all__ = [
    "APP_DELETE_ROUTE",
    "APP_GET_ROUTE",
    "APP_LIST_ROUTE",
    "APP_MANAGED_RESOURCES_ROUTE",
    "APP_RESOURCE_TREE_ROUTE",
    "APP_ROLLBACK_ROUTE",
    "APP_SPEC_UPDATE_ROUTE",
    "APP_SYNC_ROUTE",
    "CLUSTER_LIST_ROUTE",
    "PROJECT_CREATE_ROUTE",
    "PROJECT_LIST_ROUTE",
    "PROJECT_UPDATE_ROUTE",
    "REPO_LIST_ROUTE",
    "VERSION_ROUTE",
    "route_method",
    "route_path",
]

# --- Unauthenticated fingerprint/probe surface -----------------------------

#: ArgoCD's ``VersionMessage`` endpoint; unauthenticated on argocd-server.
VERSION_ROUTE = "GET:/api/version"

# --- Application reads (G3.12-T2 #1391) ------------------------------------

APP_LIST_ROUTE = "GET:/api/v1/applications"
APP_GET_ROUTE = "GET:/api/v1/applications/{name}"
APP_MANAGED_RESOURCES_ROUTE = "GET:/api/v1/applications/{applicationName}/managed-resources"
APP_RESOURCE_TREE_ROUTE = "GET:/api/v1/applications/{applicationName}/resource-tree"

# --- Application writes (G3.12-T4 #1405) -----------------------------------

APP_SYNC_ROUTE = "POST:/api/v1/applications/{name}/sync"
APP_ROLLBACK_ROUTE = "POST:/api/v1/applications/{name}/rollback"
APP_SPEC_UPDATE_ROUTE = "PUT:/api/v1/applications/{name}/spec"
APP_DELETE_ROUTE = "DELETE:/api/v1/applications/{name}"

# --- AppProject / repository / cluster surfaces ----------------------------

PROJECT_LIST_ROUTE = "GET:/api/v1/projects"
PROJECT_CREATE_ROUTE = "POST:/api/v1/projects"
PROJECT_UPDATE_ROUTE = "PUT:/api/v1/projects/{project.metadata.name}"
REPO_LIST_ROUTE = "GET:/api/v1/repositories"
CLUSTER_LIST_ROUTE = "GET:/api/v1/clusters"

#: One ``{segment}`` placeholder in a route template. Vendor segment
#: names may carry dots (``{project.metadata.name}``), so the match is
#: any non-brace run.
_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")


def route_method(route: str) -> str:
    """The HTTP verb a route constant declares (``"POST:/x"`` → ``"POST"``)."""
    return route.partition(":")[0]


def route_path(route: str, *, name: str | None = None) -> str:
    """The dispatchable URL path of *route*, with its placeholder filled.

    *name* is the resource name (Application ``metadata.name`` /
    AppProject name) substituted — URL-encoded, ``safe=""`` so an
    embedded ``/`` cannot splice path segments — into the template's
    single ``{segment}`` placeholder. Passing *name* against a
    placeholder-less template (or omitting it when the template has
    one) raises :exc:`ValueError`: every call site states exactly the
    shape its route carries, so a route edit that changes the template
    arity fails loudly at dispatch instead of issuing a wrong path.
    """
    template = route.partition(":")[2]
    if name is None:
        if _PLACEHOLDER_RE.search(template):
            raise ValueError(f"route {route!r} has a placeholder but no name was given")
        return template
    filled, substitutions = _PLACEHOLDER_RE.subn(quote(str(name), safe=""), template)
    if substitutions != 1:
        raise ValueError(
            f"route {route!r} has {substitutions} placeholders; route_path(name=...) "
            "fills exactly one"
        )
    return filled
