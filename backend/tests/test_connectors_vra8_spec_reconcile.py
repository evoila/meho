# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vra8 (vRealize Automation 8.x) hand-coded-surface reconcile lane (#3058) — #2980 harness.

Pins the exact ``METHOD:/path`` surface the ``vra8`` connector hand-codes, swept
from the connector's **live** module constants (the #2944 introspect-live-constants
pattern — never a hardcoded mirror that can drift). Parse-only, no DB / embeddings /
containers, so it lives in the required unit sweep per the lane-placement rule in
``docs/decisions/spec-reconcile-guards-standard.md``.

**Evidenced exclusion — manifest pin, no served-op-id compare.** A committable
vendor spec *does* exist for the vRA 8 read surface — ``vmware/vra-sdk-go``
(Apache-2.0) ships Swagger 2.0 documents for the IaaS (``vra-iaas.json``), Service
Broker (``vra-catalog-deployment.json``), and Blueprint (``vra-blueprint.json``)
services — but:

* MEHO's ingest parser accepts OpenAPI 3.0/3.1 only (#2090), so the Swagger 2.0
  surface is **not operator-ingestable** — hence the typed read core (not generic).
* The CSP identity endpoint (``POST /csp/gateway/am/api/login``) is **not** part of
  vra-sdk-go at all (CSP identity is outside the SDK), so a served-op-id compare
  could never cover the login surface.

So this lane is the **manifest pin** half of the standard — it runs unconditionally,
proves the enumeration on every sweep, and a new hand-coded path / renamed constant /
method change must update the manifest consciously. **Strengthening (deferred):** the
sibling ``vcf-automation`` tenant lane *does* shelf-arm its ``/iaas/*`` paths against
the Swagger 2.0 ``vra-iaas.json`` (via ``tests._spec_shelf`` + a local Swagger-2.0
served-set extractor); the same could arm this connector's ``/iaas`` + ``/deployment``
+ ``/blueprint`` + ``/catalog`` paths against the three vra-sdk-go swaggers once they
are shelved under a ``vcfa-vra8-8.0/`` shelf dir. That layer skips when the shelf is
unconfigured (the default sweep), so the always-on manifest pin below is the tested
guard. Full evidence + activation trigger live in the ``vcfa-vra8`` entry of the
standard doc's Evidenced-exclusions section.

**Shelf-armed: the shared infrastructure calls.** The cloud account, cloud zone,
profile and project calls this connector shares with VCF Automation
(:mod:`tests._vra_infra_paths`) are pinned here too, and checked against the
pinned ``vra-iaas.json`` of the shelf when it is set up.

Dispatch-fidelity of each path is proven live-mocked in
:mod:`tests.test_connectors_vra8_typed_reads` / :mod:`tests.test_connectors_vra8_auth`.
"""

from __future__ import annotations

from meho_backplane.connectors.vcf_automation import _day2_paths as _day2_paths_module
from meho_backplane.connectors.vcf_automation._paths import (
    TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH,
    TENANT_REQUEST_TRACKER_PATH,
)
from meho_backplane.connectors.vra8 import _paths as _paths_module
from meho_backplane.connectors.vra8 import api_file as _api_file
from meho_backplane.connectors.vra8 import connector as _connector
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec
from tests._vra_infra_paths import (
    EXPECTED_INFRA_OP_IDS,
    IAAS_SPEC_DIR,
    IAAS_SPEC_FILE,
    infra_op_ids,
    served_iaas_op_ids,
)
from tests.test_connectors_vcf_automation_spec_reconcile import (
    _DAY2_PATHS_METHODS,
    _swagger2_served_op_ids,
)

#: The pinned ``vra-sdk-go`` v0.6.5 files on the spec shelf. The vRA 8 day-2 ops
#: use the same deployment and IaaS APIs as VCF Automation 9, so they are checked
#: against the same files as the VCF Automation lane.
_SPEC_DIR = "vcf-automation-9.0"
_DAY2_SPEC_FILES = (
    "swagger-vra-sdk-go-v0.6.5/vra-iaas.json",
    "swagger-vra-sdk-go-v0.6.5/vra-catalog-deployment.json",
)

#: HTTP method per hand-coded path constant. The two login endpoints are POSTed
#: (``._auth.vra8_login``); every read path (the ``/iaas/api/about`` fingerprint
#: probe, the five migration-inventory reads and the Orchestrator ``/vco/api/about``
#: version read) is GET. A new ``*_PATH`` constant not listed here fails the sweep
#: loudly so its method is mapped consciously.
#:
#: ``GET /vco/api/about`` is checked against the vendor's Orchestrator API reference
#: (the 8.x reference on the vendor's site, and the Orchestrator 9.0 reference on the
#: spec shelf): ``{version, build-number, build-date, api-version}``. No 8.18 API
#: file exists, so the row is a manifest pin like the others.
_METHOD_BY_CONSTANT = {
    "_CSP_LOGIN_PATH": "POST",
    "_IAAS_LOGIN_PATH": "POST",
    "_ABOUT_PATH": "GET",
    "_PROJECTS_PATH": "GET",
    "_DEPLOYMENTS_PATH": "GET",
    "_DEPLOYMENT_DETAIL_PATH": "GET",
    "_BLUEPRINTS_PATH": "GET",
    "_CATALOG_ITEMS_PATH": "GET",
    "_VCO_ABOUT_PATH": "GET",
}

#: Documentation paths, not in the API file: the pages and folders that
#: ``vcfa.vra8.api_file.get`` reads with ``GET``. An API file never lists the path
#: that serves it, so no vendor spec can serve as a reconcile authority for them;
#: the vendor's 8.18 API Programming Guide names the two landing pages, the vendor's
#: Orchestrator page names ``/vco/api/docs/``, and the per-service Swagger folder
#: comes from VMware's own ``vra-sdk-go`` build file (not proven on 8.18).
_EXPECTED_DOCUMENTATION_ONLY = {
    "GET:/automation/api-docs",
    "GET:/automation-ui/api-docs",
    "GET:/vco/api/docs",
    "GET:/{service}/api/swagger",
}


def _path_constants() -> dict[str, str]:
    """The live ``_*_PATH`` string constants of the ``vra8._paths`` module."""
    return {
        name: value
        for name, value in vars(_paths_module).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the vra8 connector dispatches.

    Swept from the live ``_paths`` constants (never a hardcoded mirror); the method
    for each is taken from :data:`_METHOD_BY_CONSTANT`. A constant with no method
    mapping fails loudly so the reconcile keeps covering it.
    """
    declared: set[str] = set()
    for name, path in _path_constants().items():
        method = _METHOD_BY_CONSTANT.get(name)
        if method is None:
            raise AssertionError(
                f"vra8._paths.{name} has no entry in _METHOD_BY_CONSTANT — map the "
                "new constant's HTTP method there consciously so the reconcile keeps "
                "covering it."
            )
        declared.add(f"{method}:{path}")
    return declared


def _day2_op_ids() -> set[str]:
    """Every ``METHOD:/path`` the shared day-2 ops send (``vcfa.vra8.*`` shims).

    Swept from the live ``vcf_automation._day2_paths`` constants with the methods
    the VCF Automation lane maps, plus the deployment read and delete and the IaaS
    request tracker the handlers also use.
    """
    declared = {
        f"{method}:{path}"
        for name, path in vars(_day2_paths_module).items()
        if name.endswith("_PATH") and isinstance(path, str)
        for method in _DAY2_PATHS_METHODS[name]
    }
    declared |= {
        f"GET:{TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH}",
        f"DELETE:{TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH}",
        f"GET:{TENANT_REQUEST_TRACKER_PATH}",
    }
    return declared


def test_day2_op_id_manifest_is_pinned() -> None:
    """Pin the day-2 surface the vRA 8 connector shares with VCF Automation 9."""
    assert _day2_op_ids() == {
        "DELETE:/deployment/api/deployments/{deploymentId}",
        "GET:/deployment/api/deployments/{deploymentId}",
        "GET:/deployment/api/deployments/{deploymentId}/actions",
        "GET:/deployment/api/resources/{resourceId}/actions",
        "GET:/iaas/api/block-devices",
        "GET:/iaas/api/machines",
        "GET:/iaas/api/request-tracker/{id}",
        "POST:/deployment/api/deployments/{deploymentId}/requests",
        "POST:/deployment/api/resources/{resourceId}/requests",
        "POST:/iaas/api/block-devices",
        "POST:/iaas/api/machines",
        "POST:/iaas/api/machines/{id}/disks",
    }


def test_day2_paths_are_served_by_the_pinned_vra_files() -> None:
    """Every day-2 call is in the pinned ``vra-sdk-go`` files (skips without the shelf)."""
    served: set[str] = set()
    for spec_file in _DAY2_SPEC_FILES:
        served |= _swagger2_served_op_ids(require_shelf_spec(_SPEC_DIR, spec_file))
    assert_op_ids_served(
        _day2_op_ids(), served, spec_label=f"{_SPEC_DIR} vra-iaas + vra-catalog-deployment"
    )


def test_path_constant_names_are_pinned() -> None:
    """Guard: the introspection finds exactly the hand-coded path-constant set.

    Pinning the exact ``_*_PATH`` constant-name set (the #2944 guard shape) means a
    renamed or dropped constant — or a new one that skips the ``_PATH`` convention —
    can't silently shrink the reconciled surface to a vacuous pass.
    """
    assert sorted(_path_constants()) == [
        "_ABOUT_PATH",
        "_BLUEPRINTS_PATH",
        "_CATALOG_ITEMS_PATH",
        "_CSP_LOGIN_PATH",
        "_DEPLOYMENTS_PATH",
        "_DEPLOYMENT_DETAIL_PATH",
        "_IAAS_LOGIN_PATH",
        "_PROJECTS_PATH",
        "_VCO_ABOUT_PATH",
    ]


def test_vra8_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept ``METHOD:/path`` manifest so drift can't silently change the surface.

    Runs unconditionally (no shelf / spec needed), so the enumeration is proven on
    every sweep. Because the vRA 8 vendor spec is Swagger 2.0 (OA3-only ingest can't
    consume it) and omits the CSP login (evidenced exclusion — see the module
    docstring), this manifest pin is the whole reconcile: a new hand-coded path, a
    renamed constant, or a method change must update it consciously.
    """
    assert _declared_op_ids() == {
        "POST:/csp/gateway/am/api/login",
        "POST:/iaas/api/login",
        "GET:/iaas/api/about",
        "GET:/iaas/api/projects",
        "GET:/deployment/api/deployments",
        "GET:/deployment/api/deployments/{deployment_id}",
        "GET:/blueprint/api/blueprints",
        "GET:/catalog/api/admin/items",
        "GET:/vco/api/about",
    }


def test_api_file_documentation_paths_are_pinned() -> None:
    """Pin the documentation paths ``vcfa.vra8.api_file.get`` reads (all ``GET``).

    These are documentation paths, not in the API file, so they are listed apart
    from the manifest above. Every fixed page the read accepts is also a folder
    (``/`` after it covers every path under it), and the per-service Swagger folder
    is the template the path rule expands.
    """
    documentation = {f"GET:{path}" for path in _api_file.DOCUMENTATION_PATHS}
    documentation.add(f"GET:{_api_file.SERVICE_SWAGGER_FOLDER}")
    assert documentation == _EXPECTED_DOCUMENTATION_ONLY
    # No documentation path doubles as a data path of the read core.
    data_paths = {op_id.partition(":")[2] for op_id in _declared_op_ids()}
    assert not {path.partition(":")[2] for path in documentation} & data_paths
    # The version read of the same op is a data path, pinned in the manifest.
    assert f"GET:{_paths_module._VCO_ABOUT_PATH}" in _declared_op_ids()
    assert f"GET:{_paths_module._ABOUT_PATH}" in _declared_op_ids()
    for path in _api_file.DOCUMENTATION_PATHS:
        assert _api_file.check_api_file_path(path) == path
        assert _api_file.check_api_file_path(path + "/") == path + "/"


def test_probe_constant_links_to_about_path() -> None:
    """The fingerprint probe reads the same ``/iaas/api/about`` the manifest pins.

    The connector's fingerprint dispatches :data:`vra8._paths._ABOUT_PATH`; pin the
    value and the probe-method string's reference to it so a change to one is forced
    through the other (the vcd probe⇄versions link shape).
    """
    assert _paths_module._ABOUT_PATH == "/iaas/api/about"
    assert "/iaas/api/about" in _connector._VRA8_PROBE_METHOD


def test_shared_infra_op_id_manifest_is_pinned() -> None:
    """Pin the infrastructure calls this connector shares with VCF Automation."""
    assert infra_op_ids() == EXPECTED_INFRA_OP_IDS


def test_shared_infra_paths_are_served_by_the_pinned_iaas_spec() -> None:
    """Every shared infrastructure call is in the pinned vRA IaaS API file.

    Skips when the spec shelf is not set up (``tests/_spec_shelf.py``); CI runs it.
    """
    assert_op_ids_served(
        infra_op_ids(), served_iaas_op_ids(), spec_label=f"{IAAS_SPEC_DIR}/{IAAS_SPEC_FILE}"
    )
