# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The infrastructure paths, for the vRA 8 and VCF Automation reconcile lanes.

Both connectors send the calls of
:mod:`meho_backplane.connectors.vcf_automation._infra_paths` (cloud accounts,
cloud zones, profiles, projects). Each lane adds :func:`infra_op_ids` to its
declared set, so each lane checks them against the pinned ``vra-iaas.json``.
"""

from __future__ import annotations

import json
from typing import Final

import pytest

from meho_backplane.connectors.vcf_automation import _infra_paths
from tests._spec_shelf import require_shelf_spec

#: The shelf file that holds the IaaS API.
IAAS_SPEC_DIR: Final[str] = "vcf-automation-9.0"
IAAS_SPEC_FILE: Final[str] = "swagger-vra-sdk-go-v0.6.5/vra-iaas.json"

#: HTTP method(s) per path constant. A new constant missing here fails the
#: sweep, so its method is mapped on purpose.
INFRA_PATH_METHODS: Final[dict[str, frozenset[str]]] = {
    "CLOUD_ACCOUNTS_PATH": frozenset({"GET"}),
    "CLOUD_ACCOUNT_PATH": frozenset({"GET", "DELETE"}),
    "CLOUD_ACCOUNTS_VSPHERE_PATH": frozenset({"POST"}),
    "CLOUD_ACCOUNTS_NSXT_PATH": frozenset({"POST"}),
    "ZONES_PATH": frozenset({"GET", "POST"}),
    "ZONE_PATH": frozenset({"GET", "PATCH"}),
    "FLAVOR_PROFILES_PATH": frozenset({"GET", "POST"}),
    "FLAVOR_PROFILE_PATH": frozenset({"GET", "PATCH"}),
    "NETWORK_PROFILES_PATH": frozenset({"GET"}),
    "NETWORK_IP_RANGES_PATH": frozenset({"GET"}),
    "STORAGE_PROFILES_PATH": frozenset({"GET"}),
    "PROJECTS_PATH": frozenset({"GET", "POST"}),
    "PROJECT_PATH": frozenset({"GET", "PATCH"}),
}

#: The pinned manifest of the infrastructure calls.
EXPECTED_INFRA_OP_IDS: Final[frozenset[str]] = frozenset(
    {
        "GET:/iaas/api/cloud-accounts",
        "GET:/iaas/api/cloud-accounts/{id}",
        "DELETE:/iaas/api/cloud-accounts/{id}",
        "POST:/iaas/api/cloud-accounts-vsphere",
        "POST:/iaas/api/cloud-accounts-nsx-t",
        "GET:/iaas/api/zones",
        "POST:/iaas/api/zones",
        "GET:/iaas/api/zones/{id}",
        "PATCH:/iaas/api/zones/{id}",
        "GET:/iaas/api/flavor-profiles",
        "POST:/iaas/api/flavor-profiles",
        "GET:/iaas/api/flavor-profiles/{id}",
        "PATCH:/iaas/api/flavor-profiles/{id}",
        "GET:/iaas/api/network-profiles",
        "GET:/iaas/api/network-ip-ranges",
        "GET:/iaas/api/storage-profiles",
        "GET:/iaas/api/projects",
        "POST:/iaas/api/projects",
        "GET:/iaas/api/projects/{id}",
        "PATCH:/iaas/api/projects/{id}",
    }
)


def infra_path_constants() -> dict[str, str]:
    """The live ``*_PATH`` constants of the shared module."""
    return {
        name: value
        for name, value in vars(_infra_paths).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def infra_op_ids() -> set[str]:
    """Every ``METHOD:/path`` the shared infrastructure handlers send."""
    declared: set[str] = set()
    for name, path in infra_path_constants().items():
        methods = INFRA_PATH_METHODS.get(name)
        if methods is None:
            raise AssertionError(
                f"_infra_paths.{name} has no entry in INFRA_PATH_METHODS; map its HTTP "
                "method there so the reconcile lanes keep covering it."
            )
        declared.update(f"{method}:{path}" for method in methods)
    return declared


def served_iaas_op_ids() -> set[str]:
    """The ``METHOD:/path`` set of the pinned ``vra-iaas.json`` (Swagger 2.0).

    Skips when the spec shelf is not set up (``tests/_spec_shelf.py``).
    """
    document = json.loads(require_shelf_spec(IAAS_SPEC_DIR, IAAS_SPEC_FILE).read_text("utf-8"))
    if document.get("swagger") != "2.0":
        pytest.fail(f"{IAAS_SPEC_FILE}: expected a Swagger 2.0 document")
    base = str(document.get("basePath") or "").rstrip("/")
    verbs = {"get", "put", "post", "delete", "patch"}
    served = {
        f"{method.upper()}:{base}{path}"
        for path, item in document.get("paths", {}).items()
        for method in item
        if method.lower() in verbs
    }
    if not served:
        pytest.fail(f"{IAAS_SPEC_FILE}: read zero operations; the file layout moved.")
    return served
