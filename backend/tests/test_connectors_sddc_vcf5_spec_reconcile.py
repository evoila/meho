# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""sddc-vcf5 (VCF 5.x SDDC Manager) hand-coded-surface reconcile lane (#3059) — #2980 harness.

Pins the exact ``METHOD:/path`` surface the ``sddc-vcf5`` connector hand-codes, swept
from the connector's **live** module constants (the #2944 introspect-live-constants
pattern — never a hardcoded mirror that can drift). Parse-only, no DB / embeddings /
containers, so it lives in the required unit sweep.

**Evidenced exclusion — manifest pin, no served-op-id compare.** SDDC Manager 5.x
publishes only a **Swagger 2.0** API definition (appliance-served, non-conformant:
missing ``info.version``); OpenAPI 3.x is a VCF-9.0-net-new deliverable
(``vmware/vcf-api-specs`` is tagged 9.0/9.1 only — no 5.x). MEHO's ingest parser
accepts OpenAPI 3.0/3.1 only (#2090), so the 5.x surface is not operator-ingestable
(hence the typed read core). So this lane is the **manifest pin** half of the
standard — it runs unconditionally, proves the enumeration on every sweep, and a new
hand-coded path / renamed constant / method change must update the manifest
consciously.

**Strengthening (available, deferred):** SDDC Manager's ``/v1/*`` path surface is
**stable since VCF 4.0**, so these 5.x paths are the same shapes the modern
``sddc_manager`` (9.x) connector reads and reconciles against the vendored OA3
``vmware/vcf-api-specs`` ``sddc-manager-openapi.json`` (9.1). A served-set check could
therefore arm these 5.x *paths* against that 9.x OA3 (path existence is
version-stable even though response schemas drift) — deferred in favour of the
tested always-on manifest pin. Full evidence + activation trigger live in the
``sddc-vcf5`` entry of ``docs/decisions/spec-reconcile-guards-standard.md``.

Dispatch-fidelity of each path is proven live-mocked in
:mod:`tests.test_connectors_sddc_vcf5_typed_reads` /
:mod:`tests.test_connectors_sddc_vcf5_auth`.
"""

from __future__ import annotations

from meho_backplane.connectors.sddc_vcf5 import connector as _connector
from meho_backplane.connectors.sddc_vcf5 import typed_reads as _typed_reads_module

#: HTTP method per hand-coded path constant. ``_TOKENS_PATH`` is POSTed (the token
#: session mint); every other constant — the ``/v1/sddc-managers`` fingerprint probe
#: and the six migration-inventory reads — is GET. A new ``*_PATH`` constant not
#: listed here fails the sweep loudly so its method is mapped consciously.
_METHOD_BY_CONSTANT = {
    "_TOKENS_PATH": "POST",
    "_DOMAINS_PATH": "GET",
    "_CLUSTERS_PATH": "GET",
    "_HOSTS_PATH": "GET",
    "_VCENTERS_PATH": "GET",
    "_NSXT_CLUSTERS_PATH": "GET",
    "_TASKS_PATH": "GET",
    "_SDDC_MANAGERS_PATH": "GET",
}


def _path_constants() -> dict[str, str]:
    """The live ``_*_PATH`` string constants of the ``sddc_vcf5.typed_reads`` module."""
    return {
        name: value
        for name, value in vars(_typed_reads_module).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the sddc-vcf5 connector dispatches.

    Swept from the live ``typed_reads`` constants (never a hardcoded mirror); the
    method for each is taken from :data:`_METHOD_BY_CONSTANT`. A constant with no
    method mapping fails loudly so the reconcile keeps covering it.
    """
    declared: set[str] = set()
    for name, path in _path_constants().items():
        method = _METHOD_BY_CONSTANT.get(name)
        if method is None:
            raise AssertionError(
                f"sddc_vcf5.typed_reads.{name} has no entry in _METHOD_BY_CONSTANT — map "
                "the new constant's HTTP method there consciously so the reconcile keeps "
                "covering it."
            )
        declared.add(f"{method}:{path}")
    return declared


def test_path_constant_names_are_pinned() -> None:
    """Guard: the introspection finds exactly the hand-coded path-constant set."""
    assert sorted(_path_constants()) == [
        "_CLUSTERS_PATH",
        "_DOMAINS_PATH",
        "_HOSTS_PATH",
        "_NSXT_CLUSTERS_PATH",
        "_SDDC_MANAGERS_PATH",
        "_TASKS_PATH",
        "_TOKENS_PATH",
        "_VCENTERS_PATH",
    ]


def test_sddc_vcf5_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept ``METHOD:/path`` manifest so drift can't silently change the surface.

    Runs unconditionally (no shelf / spec needed). Because 5.x publishes only Swagger
    2.0 (OA3-only ingest can't consume it — evidenced exclusion, see the module
    docstring), this manifest pin is the whole reconcile: a new hand-coded path, a
    renamed constant, or a method change must update it consciously.
    """
    assert _declared_op_ids() == {
        "POST:/v1/tokens",
        "GET:/v1/domains",
        "GET:/v1/clusters",
        "GET:/v1/hosts",
        "GET:/v1/vcenters",
        "GET:/v1/nsxt-clusters",
        "GET:/v1/tasks",
        "GET:/v1/sddc-managers",
    }


def test_probe_constant_links_to_sddc_managers_path() -> None:
    """The fingerprint probe reads the same ``/v1/sddc-managers`` the manifest pins."""
    assert _typed_reads_module._SDDC_MANAGERS_PATH == "/v1/sddc-managers"
    assert "/v1/sddc-managers" in _connector._SDDC_VCF5_PROBE_METHOD
