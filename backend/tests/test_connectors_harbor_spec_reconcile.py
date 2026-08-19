# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""harbor real-spec reconcile lane (#2990) — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the harbor connector dispatches
is served by the pinned ``harbor-2.12`` spec on the shelf (``goharbor/harbor``
``api/v2.0/swagger.yaml`` at tag ``v2.12.2``, **Apache-2.0**; provenance in
the shelf's ``MANIFEST.md``, version matching the lab's deployed Harbor
``v2.12.2``). Parse-only set compare — no DB, no embeddings, no containers —
so it lives in the required unit sweep per the lane-placement rule in
``docs/decisions/spec-reconcile-guards-standard.md``. Uniform skip when the
shelf is unconfigured (``tests/_spec_shelf.py`` contract).

The pinned artifact is **Swagger 2.0**, which the ingest parser rejects by
decision (#2090), so the lane supplies its own served-set extraction instead
of routing through :func:`tests._spec_shelf.openapi_served_op_ids` — the
standard's non-OpenAPI-artifact clause, following the ``argocd-3.3`` lane's
sanctioned Swagger 2.0 extraction shape. The document declares
``basePath: /api/v2.0``, so the extractor folds that prefix onto every path
key before comparing against the connector's hand-coded ``/api/v2.0/…`` form
(the #1796 server-base fold). No OpenAPI 3 conversion is pinned: the typed
ops exist precisely because ingest rejects the document, so a converted
artifact would be a test-only fabrication with no dispatch-fidelity value.

The declared set is introspected from the connector's live constants (the
#2944 pattern — never a hardcoded mirror): #2990 hoisted every request path
out of inline f-strings (previously spread across ``connector.py`` /
``typed_reads.py``) into the ``_*_PATH`` template constants of
:mod:`~meho_backplane.connectors.harbor._paths`, which every handler now fills
via ``fill_path`` (or uses directly) — so these constants ARE the dispatched
surface. Template placeholders carry the pinned spec's own path-parameter
names: the project detail / ``/summary`` endpoints take Harbor's name-or-id
segment ``{project_name_or_id}`` while the repository sub-tree uses
``{project_name}`` / ``{repository_name}`` / ``{reference}`` and the
robot-delete uses ``{robot_id}`` — making the reconcile byte-for-byte against
the ``basePath``-folded path keys. The HTTP method cannot be introspected
from a bare path string, so :data:`_METHODS_BY_CONSTANT` declares it per
constant (``_ROBOTS_PATH`` dispatches under two methods — list ``GET`` and
create ``POST``) and a guard test pins the constant-name set; the method
claims come from the dispatch seams: the nine typed reads and the
``project_summary`` / ``quota_list`` / ``artifact_vulnerabilities`` reads are
``GET``, ``robot_create`` is a ``POST``, and ``robot_delete`` is a ``DELETE``.

All 14 reconciled op_ids were served on the first armed run; no path repoints
and no evidenced exclusions were needed (the connector's fingerprint /
``systeminfo`` and probe / ``health`` reads are both modeled by the pinned
spec, unlike keycloak's OIDC-token / serverinfo exclusions). A red run here
is the guard surfacing a finding; triage per
docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from meho_backplane.connectors.harbor import _paths
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

if TYPE_CHECKING:
    from pathlib import Path

_SPEC_DIR = "harbor-2.12"
_SPEC_FILE = "harbor-swagger.yaml"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"

#: The Swagger 2.0 path-item keys that are HTTP operations (its fixed verb
#: vocabulary); everything else on a path item (``parameters``, vendor
#: extensions) is not a served operation.
_SWAGGER2_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch"})

#: HTTP method(s) per hand-coded path constant reconciled against the pinned
#: spec. Declared (not introspected) because a module-level path string
#: carries no method; the guard test below pins this mapping's key set against
#: the live constants so the two can never drift apart silently.
#: ``_ROBOTS_PATH`` serves both a collection read (``GET``) and a create
#: (``POST``).
_METHODS_BY_CONSTANT: dict[str, tuple[str, ...]] = {
    "_SYSTEMINFO_PATH": ("GET",),
    "_HEALTH_PATH": ("GET",),
    "_PROJECTS_PATH": ("GET",),
    "_PROJECT_PATH": ("GET",),
    "_PROJECT_SUMMARY_PATH": ("GET",),
    "_PROJECT_REPOSITORIES_PATH": ("GET",),
    "_REPOSITORY_PATH": ("GET",),
    "_ARTIFACTS_PATH": ("GET",),
    "_ARTIFACT_PATH": ("GET",),
    "_ARTIFACT_VULNERABILITIES_PATH": ("GET",),
    "_ROBOTS_PATH": ("GET", "POST"),
    "_ROBOT_PATH": ("DELETE",),
    "_QUOTAS_PATH": ("GET",),
}


def _path_constants() -> dict[str, str]:
    """The live ``_*_PATH`` constants of ``harbor._paths``, by constant name."""
    return {
        name: value
        for name, value in vars(_paths).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the harbor connector dispatches."""
    constants = _path_constants()
    return {
        f"{method}:{constants[name]}"
        for name, methods in _METHODS_BY_CONSTANT.items()
        for method in methods
    }


def test_path_constants_are_all_discovered_and_method_mapped() -> None:
    """Guard: the introspection finds every path constant, each with a method.

    Pinning the exact constant-name set (the #2944 guard shape) means a
    renamed or dropped ``_*_PATH`` constant — or a new one that skips the
    ``_PATH`` suffix convention or lacks a method mapping — can't silently
    shrink the reconciled set to a vacuous pass.
    """
    assert sorted(_path_constants()) == sorted(_METHODS_BY_CONSTANT)


def test_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept op_id set so drift can't silently shrink the reconcile.

    Runs unconditionally (no shelf needed), so the enumeration wiring is
    proven on every sweep — the nsx-lane manifest precedent. A new hand-coded
    path, a renamed constant, or a changed method must update this manifest
    consciously, and the lane's declared set moves with it. Template segments
    carry the vendor's own parameter names (reconciled against the pinned spec
    in the armed test below).
    """
    assert sorted(_declared_op_ids()) == [
        "DELETE:/api/v2.0/robots/{robot_id}",
        "GET:/api/v2.0/health",
        "GET:/api/v2.0/projects",
        "GET:/api/v2.0/projects/{project_name_or_id}",
        "GET:/api/v2.0/projects/{project_name_or_id}/summary",
        "GET:/api/v2.0/projects/{project_name}/repositories",
        "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}",
        "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts",
        "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts/{reference}",
        "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}"
        "/artifacts/{reference}/additions/vulnerabilities",
        "GET:/api/v2.0/quotas",
        "GET:/api/v2.0/robots",
        "GET:/api/v2.0/systeminfo",
        "POST:/api/v2.0/robots",
    ]


def _swagger2_served_op_ids(spec_path: Path) -> set[str]:
    """Extract the served ``METHOD:/path`` set from the pinned Swagger 2.0 YAML.

    The pinned spec is Swagger 2.0, which the ingest parser rejects by
    decision (#2090) — so this lane extracts directly instead of routing
    through :func:`tests._spec_shelf.openapi_served_op_ids` (the standard's
    non-OpenAPI-artifact clause; same shape as the argocd lane, reading YAML
    rather than JSON). Served op_ids are the ``basePath``-folded path keys
    crossed with their operation-verb keys; the harbor document declares
    ``basePath: /api/v2.0``, so every key folds onto the ``/api/v2.0/…`` form
    the connector hand-codes. Format drift on the shelf (a re-pin that is no
    longer Swagger 2.0, or an empty path map) fails loudly rather than
    regressing the lane to a vacuous compare.
    """
    document = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or str(document.get("swagger")) != "2.0":
        got = document.get("swagger") if isinstance(document, dict) else type(document).__name__
        raise AssertionError(
            f"{_SPEC_LABEL}: expected a Swagger 2.0 document (got swagger={got!r}) — "
            "the shelf artifact format moved; update this lane's extraction."
        )
    base = str(document.get("basePath") or "").rstrip("/")
    served = {
        f"{method.upper()}:{base}{path}"
        for path, item in (document.get("paths") or {}).items()
        for method in item
        if method.lower() in _SWAGGER2_METHODS
    }
    if not served:
        raise AssertionError(
            f"{_SPEC_LABEL}: extracted zero served operations — the shelf "
            "artifact layout moved; update this lane's extraction."
        )
    return served


def test_declared_op_ids_are_served_by_the_pinned_spec() -> None:
    """Every hand-coded harbor op_id is served by the pinned harbor-2.12 spec.

    A red run here is the guard surfacing a finding; triage per
    docs/decisions/spec-reconcile-guards-standard.md.
    """
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = _swagger2_served_op_ids(spec_path)
    assert_op_ids_served(_declared_op_ids(), served, spec_label=_SPEC_LABEL)
