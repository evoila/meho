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
#: constants in step.
_DECLARED: dict[str, tuple[tuple[str, ...], str]] = {
    "SUBSCRIPTION_PATH": (("GET",), "SUBSCRIPTIONS_API_VERSION"),
    "LOCATIONS_PATH": (("GET",), "SUBSCRIPTIONS_API_VERSION"),
    "RESOURCE_GRAPH_PATH": (("POST",), "RESOURCE_GRAPH_API_VERSION"),
    "VIRTUAL_MACHINES_PATH": (("GET",), "COMPUTE_API_VERSION"),
    "RESOURCE_GROUP_VIRTUAL_MACHINES_PATH": (("GET",), "COMPUTE_API_VERSION"),
    "VIRTUAL_MACHINE_PATH": (("GET",), "COMPUTE_API_VERSION"),
    "IMAGES_PATH": (("GET",), "COMPUTE_API_VERSION"),
    "RESOURCE_GROUP_IMAGES_PATH": (("GET",), "COMPUTE_API_VERSION"),
    "SCALE_SETS_PATH": (("GET",), "COMPUTE_API_VERSION"),
    "RESOURCE_GROUP_SCALE_SETS_PATH": (("GET",), "COMPUTE_API_VERSION"),
    "DISKS_PATH": (("GET",), "DISKS_API_VERSION"),
    "RESOURCE_GROUP_DISKS_PATH": (("GET",), "DISKS_API_VERSION"),
    "COMPUTE_SKUS_PATH": (("GET",), "COMPUTE_SKUS_API_VERSION"),
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
    """The list of checked paths, so a change to it is always on purpose."""
    assert sorted(_declared_op_ids()) == [
        "GET:/subscriptions/{}/locations@2022-12-01",
        "GET:/subscriptions/{}/providers/microsoft.compute/disks@2026-03-02",
        "GET:/subscriptions/{}/providers/microsoft.compute/images@2026-04-01",
        "GET:/subscriptions/{}/providers/microsoft.compute/skus@2021-07-01",
        "GET:/subscriptions/{}/providers/microsoft.compute/virtualmachines@2026-04-01",
        "GET:/subscriptions/{}/providers/microsoft.compute/virtualmachinescalesets@2026-04-01",
        "GET:/subscriptions/{}/resourcegroups/{}/providers/microsoft.compute/disks@2026-03-02",
        "GET:/subscriptions/{}/resourcegroups/{}/providers/microsoft.compute/images@2026-04-01",
        "GET:/subscriptions/{}/resourcegroups/{}/providers/microsoft.compute/virtualmachines/{}@2026-04-01",
        "GET:/subscriptions/{}/resourcegroups/{}/providers/microsoft.compute/virtualmachines@2026-04-01",
        "GET:/subscriptions/{}/resourcegroups/{}/providers/microsoft.compute/virtualmachinescalesets@2026-04-01",
        "GET:/subscriptions/{}@2022-12-01",
        "POST:/providers/microsoft.resourcegraph/resources@2024-04-01",
    ]


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
