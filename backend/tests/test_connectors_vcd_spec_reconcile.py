# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec path check of the vcd (VMware Cloud Director) connector — #2980 harness.

Every path the connector calls is a ``*_PATH`` constant in
:mod:`meho_backplane.connectors.vcd._paths`. This module reads those **live**
constants (never a copied list that can drift) and checks them in two ways:

* **Manifest pin (always runs).** The exact ``METHOD:/path`` set is pinned, so
  a new path, a renamed constant or a changed method must update the pin on
  purpose. Parse-only: no DB, no embeddings, no containers.
* **CloudAPI check (runs when the spec shelf is set up).** Every
  ``/cloudapi/...`` path is checked against the CloudAPI file a Cloud Director
  10.6 cell serves (``vmware-cloud-director-10.6/cloudapi-39.2.json`` on the
  spec shelf: API 39.2, Swagger 2.0, ``basePath`` ``/cloudapi``). MEHO's spec
  ingest reads OpenAPI 3 only, so this module reads the Swagger 2.0 file itself
  (:func:`_cloudapi_served`). Parameter names are ignored: ``{org_urn}`` and
  ``{orgUrn}`` both compare as ``{}``. Without the shelf the check skips, like
  every spec check (``tests/_spec_shelf.py``); CI sets the shelf up and fails
  when the file is missing.

The older ``/api`` paths (``/api/versions``, ``/api/query``) are in no API file:
the vendor publishes none for ``/api``. They stay in the manifest pin only. The
reference for them is the Cloud Director API reference, version 39.0:
https://developer.broadcom.com/xapis/vmware-cloud-director-api/39.0/
(``GET /versions``, operation ``GET-SupportedVersions``; ``GET /query``,
operation ``GET-ExecuteQuery``).

Dispatch of each path is proven with mocked HTTP in
:mod:`tests.test_connectors_vcd_typed_reads`, :mod:`tests.test_connectors_vcd_org_ops`
and :mod:`tests.test_connectors_vcd_auth`. A red run is the check finding a real
problem; see ``docs/decisions/spec-reconcile-guards-standard.md``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from meho_backplane.connectors.vcd import _paths as _paths_module
from meho_backplane.connectors.vcd import connector as _connector
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_SPEC_DIR = "vmware-cloud-director-10.6"
_SPEC_FILE = "cloudapi-39.2.json"

#: HTTP methods per path constant. The provider session-create endpoint is
#: POSTed (``._auth.vcd_provider_login``); the org collection is listed (GET)
#: and created into (POST); one org is read, changed and deleted. A new
#: ``*_PATH`` constant not listed here fails the sweep, so its methods are
#: mapped on purpose.
_METHODS_BY_CONSTANT: dict[str, tuple[str, ...]] = {
    "_SESSIONS_PROVIDER_PATH": ("POST",),
    "_VERSIONS_PATH": ("GET",),
    "_ORGS_PATH": ("GET", "POST"),
    "_ORG_PATH": ("GET", "PUT", "DELETE"),
    "_QUERY_PATH": ("GET",),
}

#: The HTTP verbs of a Swagger 2.0 path item. Other keys (``parameters``,
#: ``x-vcloud-...``) are not operations.
_SWAGGER2_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch"})

_PARAM = re.compile(r"\{[^}]*\}")


def _normalise(op_id: str) -> str:
    """``METHOD:/path`` with every ``{...}`` path parameter written as ``{}``."""
    return _PARAM.sub("{}", op_id)


def _path_constants() -> dict[str, str]:
    """The live ``_*_PATH`` string constants of the ``vcd._paths`` module."""
    return {
        name: value
        for name, value in vars(_paths_module).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the vcd connector calls.

    Swept from the live ``_paths`` constants; the methods for each come from
    :data:`_METHODS_BY_CONSTANT`. A constant with no entry fails loudly.
    """
    declared: set[str] = set()
    for name, path in _path_constants().items():
        methods = _METHODS_BY_CONSTANT.get(name)
        if methods is None:
            raise AssertionError(
                f"vcd._paths.{name} has no entry in _METHODS_BY_CONSTANT — map the "
                "new constant's HTTP methods there on purpose so the check keeps "
                "covering it."
            )
        declared.update(f"{method}:{path}" for method in methods)
    return declared


def _cloudapi_served(spec_path: Path) -> set[str]:
    """Read a Swagger 2.0 file into normalised ``METHOD:<basePath><path>`` entries."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    base = str(spec.get("basePath") or "").rstrip("/")
    served: set[str] = set()
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method in item:
            if method in _SWAGGER2_METHODS:
                served.add(_normalise(f"{method.upper()}:{base}{path}"))
    return served


def test_path_constant_names_are_pinned() -> None:
    """Guard: the introspection finds exactly the hand-coded path-constant set.

    Pinning the exact ``_*_PATH`` constant-name set (the #2944 guard shape) means
    a renamed or dropped constant — or a new one that skips the ``_PATH``
    convention — can't silently shrink the checked surface to a vacuous pass.
    """
    assert sorted(_path_constants()) == [
        "_ORGS_PATH",
        "_ORG_PATH",
        "_QUERY_PATH",
        "_SESSIONS_PROVIDER_PATH",
        "_VERSIONS_PATH",
    ]


def test_vcd_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept ``METHOD:/path`` manifest so drift can't silently change the surface.

    Runs without the shelf, so the list is proven on every run. A new path, a
    renamed constant or a method change must update it on purpose.
    """
    assert _declared_op_ids() == {
        "GET:/api/query",
        "GET:/api/versions",
        "GET:/cloudapi/1.0.0/orgs",
        "POST:/cloudapi/1.0.0/orgs",
        "GET:/cloudapi/1.0.0/orgs/{org_urn}",
        "PUT:/cloudapi/1.0.0/orgs/{org_urn}",
        "DELETE:/cloudapi/1.0.0/orgs/{org_urn}",
        "POST:/cloudapi/1.0.0/sessions/provider",
    }


def test_cloudapi_paths_are_in_the_cell_served_cloudapi_file() -> None:
    """Every ``/cloudapi`` path the connector calls is in the 10.6 CloudAPI file."""
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    declared = {
        _normalise(op_id)
        for op_id in _declared_op_ids()
        if op_id.split(":", 1)[1].startswith("/cloudapi/")
    }
    assert_op_ids_served(
        declared, _cloudapi_served(spec_path), spec_label=f"{_SPEC_DIR}/{_SPEC_FILE}"
    )


def test_probe_constant_links_to_versions_path() -> None:
    """The fingerprint probe reads the same ``/api/versions`` the manifest pins.

    The connector's fingerprint dispatches :data:`vcd._paths._VERSIONS_PATH`; pin
    the value and the probe-method string's reference to it so a change to one is
    forced through the other (the fleet-lcm health⇄probe link shape).
    """
    assert _paths_module._VERSIONS_PATH == "/api/versions"
    assert "/api/versions" in _connector._VCD_PROBE_METHOD
