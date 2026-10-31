# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reconcile lane of the Automation integration ops (both connectors).

The five integration ops are written once (``connectors/vmware_shared``) and
registered on ``vcfa-rest-9.0`` (VCF Automation 9, tenant side) and on
``vcfa-vra8-8.0`` (Aria Automation 8). Every vendor path they send is a
``*_PATH`` constant of :mod:`~meho_backplane.connectors.vmware_shared.vcfa_integration_calls`,
so this one lane covers the VCF Automation and the vRA 8 sides.

The lane checks each ``METHOD:/path`` against the vendor's Swagger 2.0 files
of ``vmware/vra-sdk-go`` v0.6.5 on the spec shelf: ``vra-iaas.json`` (the
integrations, request trackers, external IP ranges, network profiles) and
``vra-content.json`` (content sources and Git syncs). Without the shelf
(``MEHO_CONSUMER_DOCS_ROOT`` unset) the shelf test skips; CI sets it up.

Exclusion: the IPAM provider calls (``/provisioning/ipam/api/providers`` and
``.../packages/import``) are in no published API file: not in vra-sdk-go and
not in the VCF Automation 9.0, 9.1 or 9.1.1 references. They come from the
vendor's own web app. The lane pins them, and fails when a pinned file starts
to list them, so they move into the checked set.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from meho_backplane.connectors.vcf_automation._routing import plane_for_path
from meho_backplane.connectors.vmware_shared import vcfa_integration_calls as _calls
from meho_backplane.connectors.vmware_shared.vcfa_integration_ops import INTEGRATION_OPS
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

if TYPE_CHECKING:
    from pathlib import Path

_SPEC_DIR = "vcf-automation-9.0"
_SPEC_FILES = (
    "swagger-vra-sdk-go-v0.6.5/vra-iaas.json",
    "swagger-vra-sdk-go-v0.6.5/vra-content.json",
)
_SWAGGER2_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch"})

#: HTTP method(s) per path constant. A new constant missing here fails the
#: sweep, so its method is mapped on purpose.
_METHODS: dict[str, frozenset[str]] = {
    "INTEGRATIONS_PATH": frozenset({"GET", "POST"}),
    "INTEGRATION_PATH": frozenset({"GET", "PATCH", "DELETE"}),
    "REQUEST_TRACKER_PATH": frozenset({"GET"}),
    "EXTERNAL_IP_RANGES_PATH": frozenset({"GET"}),
    "NETWORK_PROFILES_PATH": frozenset({"GET"}),
    "CONTENT_SOURCES_PATH": frozenset({"GET", "POST"}),
    "CONTENT_SOURCE_PATH": frozenset({"GET", "DELETE"}),
    "SYNC_REQUESTS_PATH": frozenset({"POST"}),
    "SYNC_REQUEST_PATH": frozenset({"GET"}),
    "SYNC_HISTORY_PATH": frozenset({"GET"}),
    "IPAM_PROVIDERS_PATH": frozenset({"GET"}),
    "IPAM_PACKAGE_IMPORT_PATH": frozenset({"POST"}),
}

#: The vendor calls of the Task's table plus the reads the ops need for their
#: checks (sync history, IP ranges, network profiles).
_EXPECTED: frozenset[str] = frozenset(
    {
        "GET:/iaas/api/integrations",
        "POST:/iaas/api/integrations",
        "GET:/iaas/api/integrations/{id}",
        "PATCH:/iaas/api/integrations/{id}",
        "DELETE:/iaas/api/integrations/{id}",
        "GET:/iaas/api/request-tracker/{id}",
        "GET:/iaas/api/external-network-ip-ranges",
        "GET:/iaas/api/network-profiles",
        "GET:/content/api/sources",
        "POST:/content/api/sources",
        "GET:/content/api/sources/{id}",
        "DELETE:/content/api/sources/{id}",
        "POST:/content/api/sourcecontrol/sync-requests",
        "GET:/content/api/sourcecontrol/sync-requests/{id}",
        "GET:/content/api/sourcecontrol/sync-history",
        "GET:/provisioning/ipam/api/providers",
        "POST:/provisioning/ipam/api/providers/packages/import",
    }
)

#: In no published API file (see the module text).
_EXCLUDED: frozenset[str] = frozenset(
    {
        "GET:/provisioning/ipam/api/providers",
        "POST:/provisioning/ipam/api/providers/packages/import",
    }
)


def _path_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(_calls).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared() -> set[str]:
    declared: set[str] = set()
    for name, path in _path_constants().items():
        if name not in _METHODS:
            raise AssertionError(f"{name} has no entry in _METHODS; map its HTTP method(s)")
        declared.update(f"{method}:{path}" for method in _METHODS[name])
    return declared


def _served(spec_path: Path) -> set[str]:
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    if document.get("swagger") != "2.0":
        pytest.fail(f"{spec_path.name}: expected a Swagger 2.0 document")
    base = str(document.get("basePath") or "").rstrip("/")
    served = {
        f"{method.upper()}:{base}{path}"
        for path, item in document.get("paths", {}).items()
        for method in item
        if method.lower() in _SWAGGER2_METHODS
    }
    if not served:
        pytest.fail(f"{spec_path.name}: no operations found; the file layout moved")
    return served


def test_path_constants_are_pinned() -> None:
    assert sorted(_path_constants()) == sorted(_METHODS)
    assert _declared() == _EXPECTED


def test_every_op_path_is_a_swept_constant() -> None:
    swept = set(_path_constants().values())
    assert {spec.path for spec in INTEGRATION_OPS} <= swept


def test_every_path_rides_the_tenant_login() -> None:
    assert {plane_for_path(op_id.partition(":")[2]) for op_id in _declared()} == {"tenant"}


def test_paths_are_served_by_the_pinned_vra_files() -> None:
    served: set[str] = set()
    for spec_file in _SPEC_FILES:
        served |= _served(require_shelf_spec(_SPEC_DIR, spec_file))
    assert_op_ids_served(
        _declared() - _EXCLUDED,
        served,
        spec_label=f"{_SPEC_DIR} vra-iaas + vra-content",
    )
    assert not _EXCLUDED & served, (
        "a pinned file now lists an IPAM provider call; remove it from _EXCLUDED"
    )
