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

Dispatch-fidelity of each path is proven live-mocked in
:mod:`tests.test_connectors_vra8_typed_reads` / :mod:`tests.test_connectors_vra8_auth`.
"""

from __future__ import annotations

from meho_backplane.connectors.vra8 import _paths as _paths_module
from meho_backplane.connectors.vra8 import connector as _connector

#: HTTP method per hand-coded path constant. The two login endpoints are POSTed
#: (``._auth.vra8_login``); every read path (the ``/iaas/api/about`` fingerprint
#: probe + the five migration-inventory reads) is GET. A new ``*_PATH`` constant not
#: listed here fails the sweep loudly so its method is mapped consciously.
_METHOD_BY_CONSTANT = {
    "_CSP_LOGIN_PATH": "POST",
    "_IAAS_LOGIN_PATH": "POST",
    "_ABOUT_PATH": "GET",
    "_PROJECTS_PATH": "GET",
    "_DEPLOYMENTS_PATH": "GET",
    "_DEPLOYMENT_DETAIL_PATH": "GET",
    "_BLUEPRINTS_PATH": "GET",
    "_CATALOG_ITEMS_PATH": "GET",
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
    }


def test_probe_constant_links_to_about_path() -> None:
    """The fingerprint probe reads the same ``/iaas/api/about`` the manifest pins.

    The connector's fingerprint dispatches :data:`vra8._paths._ABOUT_PATH`; pin the
    value and the probe-method string's reference to it so a change to one is forced
    through the other (the vcd probe⇄versions link shape).
    """
    assert _paths_module._ABOUT_PATH == "/iaas/api/about"
    assert "/iaas/api/about" in _connector._VRA8_PROBE_METHOD
