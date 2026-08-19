# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vcd (VMware Cloud Director) hand-coded-surface reconcile lane (#3057) — #2980 harness.

Pins the exact ``METHOD:/path`` surface the ``vcd`` connector hand-codes, swept
from the connector's **live** module constants (the #2944 introspect-live-
constants pattern — never a hardcoded mirror that can drift). Parse-only, no DB
/ embeddings / containers, so it lives in the required unit sweep per the
lane-placement rule in ``docs/decisions/spec-reconcile-guards-standard.md``.

**Evidenced exclusion — no served-op-id compare.** Unlike the ``fleet-lcm`` lane
(which reconciles against a pinned Apache-2.0 OpenAPI on the shelf), vCD is a
*typed* connector precisely because Broadcom publishes **no committable spec**
for its REST surface: the classic ``/api/*`` query service is bundled into the
UI JS at build time, and the appliance serves no ``/cloudapi/1.0.0/openapi*``
(the same evidence recorded for the ``vcf-automation`` provider plane, which
speaks the same vCloud-Director-derived surface). So there is no artifact to
reconcile against; this lane is the **manifest pin** half of the standard —
runs unconditionally, proves the enumeration on every sweep, and a new
hand-coded path / renamed constant / method change must update the manifest
consciously. Dispatch-fidelity of each path is proven live-mocked in
:mod:`tests.test_connectors_vcd_typed_reads` / :mod:`tests.test_connectors_vcd_auth`.

The full evidence + activation trigger (if Broadcom ever ships a vCD OpenAPI)
live in the ``vcd`` entry of the standard doc's Evidenced-exclusions section.
"""

from __future__ import annotations

from meho_backplane.connectors.vcd import _paths as _paths_module
from meho_backplane.connectors.vcd import connector as _connector

#: HTTP method per hand-coded path constant. The provider session-create
#: endpoint is POSTed (``._auth.vcd_provider_login``); every other constant —
#: the ``/api/versions`` fingerprint probe and the two read paths — is GET.
#: A new ``*_PATH`` constant not listed here fails the sweep loudly so its
#: method is mapped consciously.
_METHOD_BY_CONSTANT = {
    "_SESSIONS_PROVIDER_PATH": "POST",
    "_VERSIONS_PATH": "GET",
    "_ORGS_PATH": "GET",
    "_QUERY_PATH": "GET",
}


def _path_constants() -> dict[str, str]:
    """The live ``_*_PATH`` string constants of the ``vcd._paths`` module."""
    return {
        name: value
        for name, value in vars(_paths_module).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the vcd connector dispatches.

    Swept from the live ``_paths`` constants (never a hardcoded mirror); the
    method for each is taken from :data:`_METHOD_BY_CONSTANT`. A constant with
    no method mapping fails loudly so the reconcile keeps covering it.
    """
    declared: set[str] = set()
    for name, path in _path_constants().items():
        method = _METHOD_BY_CONSTANT.get(name)
        if method is None:
            raise AssertionError(
                f"vcd._paths.{name} has no entry in _METHOD_BY_CONSTANT — map the "
                "new constant's HTTP method there consciously so the reconcile "
                "keeps covering it."
            )
        declared.add(f"{method}:{path}")
    return declared


def test_path_constant_names_are_pinned() -> None:
    """Guard: the introspection finds exactly the hand-coded path-constant set.

    Pinning the exact ``_*_PATH`` constant-name set (the #2944 guard shape) means
    a renamed or dropped constant — or a new one that skips the ``_PATH``
    convention — can't silently shrink the reconciled surface to a vacuous pass.
    """
    assert sorted(_path_constants()) == [
        "_ORGS_PATH",
        "_QUERY_PATH",
        "_SESSIONS_PROVIDER_PATH",
        "_VERSIONS_PATH",
    ]


def test_vcd_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept ``METHOD:/path`` manifest so drift can't silently change the surface.

    Runs unconditionally (no shelf / spec needed), so the enumeration is proven
    on every sweep. Because vCD publishes no committable spec (evidenced
    exclusion — see the module docstring), this manifest pin is the whole
    reconcile: a new hand-coded path, a renamed constant, or a method change must
    update it consciously.
    """
    assert _declared_op_ids() == {
        "GET:/api/query",
        "GET:/api/versions",
        "GET:/cloudapi/1.0.0/orgs",
        "POST:/cloudapi/1.0.0/sessions/provider",
    }


def test_probe_constant_links_to_versions_path() -> None:
    """The fingerprint probe reads the same ``/api/versions`` the manifest pins.

    The connector's fingerprint dispatches :data:`vcd._paths._VERSIONS_PATH`; pin
    the value and the probe-method string's reference to it so a change to one is
    forced through the other (the fleet-lcm health⇄probe link shape).
    """
    assert _paths_module._VERSIONS_PATH == "/api/versions"
    assert "/api/versions" in _connector._VCD_PROBE_METHOD
