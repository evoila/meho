# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""argocd real-spec reconcile lane (#2987) — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the argocd connector
dispatches is served by the pinned ``argocd-3.3`` spec on the shelf —
the ``argocd-server`` Swagger 2.0 document (``argoproj/argo-cd``
``assets/swagger.json`` at tag ``v3.3.9``, **Apache-2.0**; provenance
in the shelf's ``MANIFEST.md``). Parse-only set compare — no DB, no
embeddings, no containers — so it lives in the required unit sweep per
the lane-placement rule in
``docs/decisions/spec-reconcile-guards-standard.md``. Uniform skip when
the shelf is unconfigured (``tests/_spec_shelf.py`` contract).

The pinned artifact is Swagger 2.0, which the ingest parser rejects by
decision (#2090), so the lane supplies its own served-set extraction
instead of routing through
:func:`tests._spec_shelf.openapi_served_op_ids` — the standard's
non-OpenAPI-artifact clause, following the vcf-automation lane's
sanctioned Swagger 2.0 extraction shape. No OpenAPI 3 conversion is
pinned: the typed ops exist precisely because ingest rejects the
document, so a converted artifact would be a test-only fabrication with
no dispatch-fidelity value.

The declared set is introspected from the connector's live constants
(the #2944 pattern — never a hardcoded mirror):
:mod:`meho_backplane.connectors.argocd.routes` declares every dispatched
endpoint exactly once as a ``"METHOD:/path"`` ``*_ROUTE`` constant, and
every handler derives both its verb and its request path from the same
constant (``route_method`` / ``route_path``) — the seven curated reads
and the fingerprint/probe ``GET /api/version`` in ``connector.py``, and
the seven approval-gated writes plus their read-only snapshot helpers in
``ops_write.py``. A path edit in the connector therefore flows into the
reconcile automatically, and a handler regressing to an inline literal
is exactly the drift the pinned-manifest guard below surfaces in review.

First reconcile of the enumerated routes against the real spec surfaced
three findings, all template-segment renames (the vendor's
gRPC-gateway parameter names differ per route family; the runtime
request path — placeholder filled with the URL-encoded resource name —
is unchanged): ``app.diff``'s ``managed-resources`` and
``app.resource_tree``'s ``resource-tree`` use ``{applicationName}``
(not ``{name}``), and the ``appproject.update`` PUT uses
``{project.metadata.name}``. All three were adopted into the route
table in the same PR per the #2970 protocol (the hetzner-robot
``{vswitch-id}`` precedent). Protocol:
docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from meho_backplane.connectors.argocd import routes as _routes_module
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

if TYPE_CHECKING:
    from pathlib import Path

_SPEC_DIR = "argocd-3.3"
_SPEC_FILE = "argocd-swagger.json"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"

#: The Swagger 2.0 path-item keys that are HTTP operations (its fixed
#: verb vocabulary); everything else on a path item (``parameters``,
#: vendor extensions) is not a served operation.
_SWAGGER2_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch"})


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the argocd connector dispatches.

    Sweeps the live ``*_ROUTE`` constants of the connector's route table
    (:mod:`~meho_backplane.connectors.argocd.routes`) — each value is
    already the ``METHOD:/path`` op_id shape the harness compares, and
    is the same string dispatch derives its verb and request path from.
    """
    return {
        value
        for name, value in vars(_routes_module).items()
        if name.endswith("_ROUTE") and isinstance(value, str)
    }


def test_route_constant_names_are_pinned() -> None:
    """Guard: the introspection finds every hand-coded route constant.

    Pinning the exact constant-name set (the #2944 guard shape) means a
    renamed or dropped ``*_ROUTE`` constant — or a new one that skips
    the ``_ROUTE`` suffix convention — can't silently shrink the
    reconciled set to a vacuous pass.
    """
    assert sorted(
        name
        for name, value in vars(_routes_module).items()
        if name.endswith("_ROUTE") and isinstance(value, str)
    ) == [
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
    ]


def test_argocd_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept op_id set so drift can't silently shrink the reconcile.

    Runs unconditionally (no shelf needed), so the enumeration is proven
    on every sweep. A new hand-coded path, a renamed constant, or a
    method change must update this manifest consciously — and the lane's
    declared set moves with it. Template segments carry the vendor's own
    parameter names (reconciled against the pinned spec below).
    """
    assert _declared_op_ids() == {
        "DELETE:/api/v1/applications/{name}",
        "GET:/api/v1/applications",
        "GET:/api/v1/applications/{applicationName}/managed-resources",
        "GET:/api/v1/applications/{applicationName}/resource-tree",
        "GET:/api/v1/applications/{name}",
        "GET:/api/v1/clusters",
        "GET:/api/v1/projects",
        "GET:/api/v1/repositories",
        "GET:/api/version",
        "POST:/api/v1/applications/{name}/rollback",
        "POST:/api/v1/applications/{name}/sync",
        "POST:/api/v1/projects",
        "PUT:/api/v1/applications/{name}/spec",
        "PUT:/api/v1/projects/{project.metadata.name}",
    }


def _swagger2_served_op_ids(spec_path: Path) -> set[str]:
    """Extract the served ``METHOD:/path`` set from a Swagger 2.0 document.

    The pinned spec is Swagger 2.0, which the ingest parser rejects by
    decision (#2090) — so this lane extracts directly instead of routing
    through :func:`tests._spec_shelf.openapi_served_op_ids` (the
    standard's non-OpenAPI-artifact clause; same shape as the
    vcf-automation lane). Served op_ids are the ``basePath``-folded path
    keys crossed with their operation-verb keys; the argocd document
    declares no ``basePath``, so its paths fold unchanged. Format drift
    on the shelf (a re-pin that is no longer Swagger 2.0, or an empty
    path map) fails loudly rather than regressing the lane to a vacuous
    compare.
    """
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    if document.get("swagger") != "2.0":
        pytest.fail(
            f"{_SPEC_LABEL}: expected a Swagger 2.0 document "
            f"(got swagger={document.get('swagger')!r} / "
            f"openapi={document.get('openapi')!r}) — the shelf artifact "
            "format moved; update this lane's extraction."
        )
    base = str(document.get("basePath") or "").rstrip("/")
    served = {
        f"{method.upper()}:{base}{path}"
        for path, item in document.get("paths", {}).items()
        for method in item
        if method.lower() in _SWAGGER2_METHODS
    }
    if not served:
        pytest.fail(
            f"{_SPEC_LABEL}: extracted zero served operations — the "
            "shelf artifact layout moved; update this lane's extraction."
        )
    return served


def test_declared_op_ids_are_served_by_the_pinned_spec() -> None:
    """Every hand-coded argocd op_id is served by the pinned argocd-3.3 spec.

    A red run here is the guard surfacing a finding; triage per
    docs/decisions/spec-reconcile-guards-standard.md.
    """
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = _swagger2_served_op_ids(spec_path)
    assert_op_ids_served(_declared_op_ids(), served, spec_label=_SPEC_LABEL)
