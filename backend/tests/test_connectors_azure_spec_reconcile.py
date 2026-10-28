# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec path check of the Azure connector.

Every path the Azure connector calls is a ``*_PATH`` constant in
:mod:`meho_backplane.connectors.azure._paths`, next to its API version. This
check fails when one of them is not in Azure's own API description files,
with the same API version.

The files are pinned on the spec shelf in ``azure-arm-2026.10/``: Swagger 2.0
files from ``Azure/azure-rest-api-specs`` (MIT license), one commit, with
their upstream paths kept. The shelf's ``MANIFEST.md`` names the commit and
every file. When the shelf is not set up (``MEHO_CONSUMER_DOCS_ROOT`` unset or
the folder missing) the check skips, like every spec check
(``tests/_spec_shelf.py``).

How paths are compared:

* Swagger 2.0 is not read by MEHO's spec ingest, so this check reads the
  files itself: the keys of ``paths`` and of ``x-ms-paths``. Azure uses
  ``x-ms-paths`` for paths with a fixed query part (for example
  ``/{containerName}?restype=container&comp=list``); the query part is cut
  off, because MEHO sends query values as parameters.
* Parameter names are ignored. Azure names the same parameter differently
  across files (``{subscriptionId}``, ``{resourceGroupName}``, ``{vmName}``
  or ``{name}``), so every ``{...}`` segment compares as ``{}``.
* Case is ignored. Azure Resource Manager treats paths as case-insensitive,
  and the files mix ``resourceGroups`` and ``resourcegroups``.
* The API version is part of the compare. Each spec file serves its
  ``info.version``; a path with a wrong API version fails.

A new ``*_PATH`` constant needs one line in :data:`_DECLARED`; the guard test
below fails until it has one. A red run is the check finding a real problem;
see docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from meho_backplane.connectors.azure import _paths
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_SPEC_DIR = "azure-arm-2026.10"

#: One file the shelf folder must hold. The check reads every ``*.json``
#: file in the folder; this one only proves the folder is there.
_ANCHOR_FILE = (
    "specification/resources/resource-manager/Microsoft.Resources/"
    "subscriptions/stable/2022-12-01/subscriptions.json"
)

#: The HTTP verbs of a Swagger 2.0 path item. Other keys (``parameters``,
#: ``x-ms-...``) are not operations.
_SWAGGER2_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch"})

#: Per path constant: the HTTP method(s) the connector sends and the name of
#: the API version constant it sends with them. A path string carries no
#: method, so it is written here; the guard test keeps this table and the
#: constants in step. Each family adds its lines under its own comment, so two
#: families never add a line at the same place.
_DECLARED: dict[str, tuple[tuple[str, ...], str]] = {
    # account and shared paths
    "SUBSCRIPTION_PATH": (("GET",), "SUBSCRIPTIONS_API_VERSION"),
    "LOCATIONS_PATH": (("GET",), "SUBSCRIPTIONS_API_VERSION"),
    "RESOURCE_GRAPH_PATH": (("POST",), "RESOURCE_GRAPH_API_VERSION"),
    # aks (Task #5087)
    "AKS_CLUSTERS_PATH": (("GET",), "AKS_API_VERSION"),
    "AKS_CLUSTER_PATH": (("GET",), "AKS_API_VERSION"),
    "AKS_UPGRADE_PROFILE_PATH": (("GET",), "AKS_API_VERSION"),
    "AKS_NODE_POOLS_PATH": (("GET",), "AKS_API_VERSION"),
    "AKS_KUBERNETES_VERSIONS_PATH": (("GET",), "AKS_API_VERSION"),
    # arc (Task #5084)
    "ARC_MACHINES_PATH": (("GET",), "ARC_API_VERSION"),
    "ARC_MACHINE_PATH": (("GET",), "ARC_API_VERSION"),
    "ARC_ASSESS_PATCHES_PATH": (("POST",), "ARC_API_VERSION"),
    "VM_ASSESS_PATCHES_PATH": (("POST",), "VM_ASSESS_API_VERSION"),
    # automation (Task #5092)
    # avs (Task #5091)
    # compute (Task #5083)
    # cost (Task #5088)
    "COST_QUERY_PATH": (("POST",), "COST_MANAGEMENT_API_VERSION"),
    "COST_FORECAST_PATH": (("POST",), "COST_MANAGEMENT_API_VERSION"),
    "BUDGETS_PATH": (("GET",), "CONSUMPTION_API_VERSION"),
    "ADVISOR_RECOMMENDATIONS_PATH": (("GET",), "ADVISOR_API_VERSION"),
    # inventory (Task #5082)
    # migrate (Task #5093)
    # monitor (Task #5090)
    # network (Task #5085)
    # security (Task #5089)
    # storage (Task #5086)
}

_PARAMETER = re.compile(r"\{[^}]*\}")


def _normalise(path: str) -> str:
    """Cut the query part, blank every parameter name, and lower-case the path."""
    return _PARAMETER.sub("{}", path.partition("?")[0]).lower()


def _key(method: str, path: str, api_version: str) -> str:
    return f"{method.upper()}:{_normalise(path)}@{api_version}"


def _path_constants() -> dict[str, str]:
    """The ``*_PATH`` constants of the Azure ``_paths`` module, by name."""
    return {
        name: value
        for name, value in vars(_paths).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every ``METHOD:/path@api-version`` the Azure connector sends."""
    constants = _path_constants()
    return {
        _key(method, constants[name], getattr(_paths, version_name))
        for name, (methods, version_name) in _DECLARED.items()
        for method in methods
    }


def _served_op_ids(spec_dir: Path) -> set[str]:
    """Every ``METHOD:/path@api-version`` the pinned Swagger 2.0 files describe.

    A file that is not Swagger 2.0, has no ``info.version`` or has no paths
    fails the check, so a changed shelf layout cannot make the check pass
    without comparing anything.
    """
    served: set[str] = set()
    files = sorted(spec_dir.rglob("*.json"))
    if not files:
        raise AssertionError(f"{_SPEC_DIR}: no spec file found under {spec_dir}")
    for spec_file in files:
        document = json.loads(spec_file.read_text(encoding="utf-8-sig"))
        label = spec_file.relative_to(spec_dir)
        if not isinstance(document, dict) or str(document.get("swagger")) != "2.0":
            raise AssertionError(f"{_SPEC_DIR}/{label}: not a Swagger 2.0 file")
        version = (document.get("info") or {}).get("version")
        if not isinstance(version, str) or not version:
            raise AssertionError(f"{_SPEC_DIR}/{label}: no info.version")
        items = {**(document.get("paths") or {}), **(document.get("x-ms-paths") or {})}
        if not items:
            raise AssertionError(f"{_SPEC_DIR}/{label}: no paths")
        served.update(
            _key(method, path, version)
            for path, item in items.items()
            for method in item
            if method.lower() in _SWAGGER2_METHODS
        )
    return served


def test_every_path_constant_has_a_method_and_an_api_version() -> None:
    """A new ``*_PATH`` constant must get a line in :data:`_DECLARED`."""
    assert sorted(_path_constants()) == sorted(_DECLARED)
    for _methods, version_name in _DECLARED.values():
        assert isinstance(getattr(_paths, version_name, None), str), version_name


def test_the_checked_paths_are_pinned() -> None:
    """The list of checked paths, so a change to it is always on purpose.

    The list has one block per family, like the ``_DECLARED`` table. A line
    that starts with ``"# "`` starts a block: it names the family and holds no
    path. Each family adds its paths under its own line, so two families never
    add a line at the same place, even when their paths come from the same spec
    file. Inside each block the paths are sorted.
    """
    pinned = [
        "# account and shared paths",
        "GET:/subscriptions/{}/locations@2022-12-01",
        "GET:/subscriptions/{}@2022-12-01",
        "POST:/providers/microsoft.resourcegraph/resources@2024-04-01",
        "# aks (Task #5087)",
        "GET:/subscriptions/{}/providers/microsoft.containerservice/locations/{}/kubernetesversions@2026-08-01",
        "GET:/subscriptions/{}/providers/microsoft.containerservice/managedclusters@2026-08-01",
        "GET:/subscriptions/{}/resourcegroups/{}/providers/microsoft.containerservice/managedclusters/{}/agentpools@2026-08-01",
        "GET:/subscriptions/{}/resourcegroups/{}/providers/microsoft.containerservice/managedclusters/{}/upgradeprofiles/default@2026-08-01",
        "GET:/subscriptions/{}/resourcegroups/{}/providers/microsoft.containerservice/managedclusters/{}@2026-08-01",
        "# arc (Task #5084)",
        "GET:/subscriptions/{}/providers/microsoft.hybridcompute/machines@2026-09-01",
        "GET:/subscriptions/{}/resourcegroups/{}/providers/microsoft.hybridcompute/machines/{}@2026-09-01",
        "POST:/subscriptions/{}/resourcegroups/{}/providers/microsoft.compute/virtualmachines/{}/assesspatches@2026-04-01",
        "POST:/subscriptions/{}/resourcegroups/{}/providers/microsoft.hybridcompute/machines/{}/assesspatches@2026-09-01",
        "# automation (Task #5092)",
        "# avs (Task #5091)",
        "# compute (Task #5083)",
        "# cost (Task #5088)",
        "GET:/subscriptions/{}/providers/microsoft.advisor/recommendations@2026-10-01",
        "GET:/{}/providers/microsoft.consumption/budgets@2026-06-01",
        "POST:/{}/providers/microsoft.costmanagement/forecast@2026-06-01",
        "POST:/{}/providers/microsoft.costmanagement/query@2026-06-01",
        "# inventory (Task #5082)",
        "# migrate (Task #5093)",
        "# monitor (Task #5090)",
        "# network (Task #5085)",
        "# security (Task #5089)",
        "# storage (Task #5086)",
    ]
    blocks: list[tuple[str, list[str]]] = []
    for line in pinned:
        if line.startswith("# "):
            blocks.append((line, []))
        else:
            assert blocks, f"{line!r} is above the first block line"
            blocks[-1][1].append(line)
    names = [name for name, _ in blocks]
    assert len(names) == len(set(names)), f"a block line is there twice: {names}"
    for name, block in blocks:
        assert ":/" not in name, f"{name!r} starts a block, so it must not hold a path"
        assert block == sorted(block), f"the paths under {name!r} are not sorted"
    checked = [path for _, block in blocks for path in block]
    assert sorted(checked) == sorted(_declared_op_ids())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/subscriptions/{subscriptionId}/resourcegroups", "/subscriptions/{}/resourcegroups"),
        ("/subscriptions/{subscription_id}/resourceGroups", "/subscriptions/{}/resourcegroups"),
        ("/{containerName}?restype=container&comp=list", "/{}"),
    ],
)
def test_paths_compare_without_parameter_names_case_or_query(raw: str, expected: str) -> None:
    assert _normalise(raw) == expected


def test_every_path_is_in_the_pinned_azure_spec_files() -> None:
    """Every Azure path, with its API version, is in the pinned spec files."""
    anchor = require_shelf_spec(_SPEC_DIR, _ANCHOR_FILE)
    spec_dir = anchor.parents[len(Path(_ANCHOR_FILE).parts) - 1]
    served = _served_op_ids(spec_dir)
    assert_op_ids_served(_declared_op_ids(), served, spec_label=f"{_SPEC_DIR} (Azure specs)")
