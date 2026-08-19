# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vcf-fleet (vRSLCM) real-spec reconcile lane (#2993) — the #2980 harness.

Asserts every hand-coded ``/lcm/*`` literal the vcf-fleet connector dispatches
against a pinned ``vcf-fleet-9.0`` shelf spec. Parse-only set compare — no DB,
no embeddings, no containers — so it lives in the required unit sweep per the
lane-placement rule in ``docs/decisions/spec-reconcile-guards-standard.md``.
Uniform skip when the shelf is unconfigured (``tests/_spec_shelf.py`` contract).

Outcome (b) — a vendor spec EXISTS but is not yet pinned (#2993), so this lane
ships **dormant**: it skips uniformly until the spec lands on the shelf, then
arms (the standard's "the lane still ships and skips uniformly; it arms itself
the day the spec lands on the shelf"). The declared-set enumeration guard below
runs unconditionally regardless.

Where the spec comes from (the #2993 hunt, evidenced 2026-08-18):

* VCF Fleet 9.0 is a **rebrand of vRealize Suite Lifecycle Manager (vRSLCM)
  8.x / VMware Aria Suite Lifecycle** — same Spring codebase, same
  ``/lcm/<area>/api/...`` surface, same ``Lcm-API-Version: 8.0`` header (shelf
  ``kb/vcf-fleet-9.0-overview.md``). Its **vRSLCM 1.3.0 LCM REST API** — the
  ``/lcm/lcops/api/v2/*`` surface this connector dispatches — is served by the
  appliance's own Swagger UI (``/api/swagger-ui.html``) and documented on the
  Broadcom developer portal (``developer.broadcom.com/xapis/
  vrealize-suite-lifecycle-manager/latest/`` — "vRealize Suite Lifecycle
  Manager API - 1.3.0"; also ``developer.vmware.com/apis/1190``). MEHO's G3.6
  canary ingested exactly this surface (``docs/cross-repo/g36-fleet-canary.md``
  — "the vRSLCM LCM REST OpenAPI surface; vRSLCM 1.3.0"), so a machine-readable
  spec demonstrably exists and covers ``/about`` + ``/environments`` +
  ``/datacenters``. An evidenced *exclusion* would repeat the nsx-9.0
  falsification (an appliance-served spec the exclusion's probe never checked),
  so this is a pin-and-arm (b), not an exclusion (c).
* It is **not** the public ``vmware/vcf-api-specs`` ``fleet-lcm-openapi.yaml``:
  that document is the **new VCF 9 "Fleet LCM Service" API** at
  ``https://vcf.broadcom.com/fleet-lcm`` → ``/v1/*`` (health, config, components,
  tasks, upgrade-plans, release-versions, system), a distinct surface that
  serves **zero** ``/lcm/lcops/*`` / ``/lcm/common/*`` / ``/lcm/locker/*`` path
  (grep-confirmed). Pinning it would red-line all eight literals for the wrong
  reason.

Pinning is the orchestrator's licensed-spec choreography (consumer-shelf PR +
``ci.yml`` sparse-checkout/verify widening for ``docs/vcf-fleet-9.0`` + the
``vendor-spec-ci-provisioning.md`` signoff extension — vendor-served form, like
nsx-9.0, referencing the wave-3 blanket determination). Per the #2993 handoff:

* dir ``vcf-fleet-9.0``; file :data:`_SPEC_FILE`;
* source: fetch the vRSLCM 1.3.0 LCM REST OpenAPI from the operator's own lab
  Fleet / Aria Suite Lifecycle appliance's Swagger backend (behind
  ``/api/swagger-ui.html``) — the nsx-9.0 appliance-served pattern.

Served-set extraction is finalized at pin time against the actual artifact:
this lane uses :func:`~tests._spec_shelf.openapi_served_op_ids` (OpenAPI 3.x);
if the appliance emits Swagger 2.0 (springfox ``/v2/api-docs``), swap to a
Swagger-2.0 extractor per the vcf-automation / argocd precedent — the standard's
non-OpenAPI-artifact clause. First armed run triages any unserved literal per
the red-lane protocol; note six of the eight are the diagnostic endpoints that
return **HTTP 500 across the vRSLCM 8.x family** (they are hand-coded in
``_FLEET_BROKEN_DIAGNOSTIC_ENDPOINTS`` as a known-issue inventory), so whether
the vendor spec documents them is exactly what the first arm decides.

The declared set is introspected from the connector's live constants (the #2944
pattern — never a hardcoded mirror): the three ``_FLEET_*_PATH`` module
constants (probe / about / environments) plus the
``_FLEET_BROKEN_DIAGNOSTIC_ENDPOINTS`` tuple. The connector dispatches
**GET-only** — HTTP Basic on every request, no session-establish POST — so every
literal declares ``GET:``.
"""

from __future__ import annotations

from meho_backplane.connectors.vcf_fleet import connector as _connector
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_DIR = "vcf-fleet-9.0"
#: Handoff filename the shelf pin must land under (or adjust here to match). The
#: vRSLCM 1.3.0 LCM REST OpenAPI, fetched from the appliance Swagger backend.
_SPEC_FILE = "vrslcm-lcm-openapi.json"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"


def _path_constants() -> dict[str, str]:
    """The live ``_*_PATH`` string constants of ``connector``, by constant name."""
    return {
        name: value
        for name, value in vars(_connector).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the vcf-fleet connector dispatches.

    Sweeps the live module constants (never a hardcoded mirror): the
    ``_FLEET_*_PATH`` op/probe constants and the
    ``_FLEET_BROKEN_DIAGNOSTIC_ENDPOINTS`` known-issue tuple. All dispatch as
    ``GET`` — the Fleet connector sends HTTP Basic per request with no
    session-establish POST.
    """
    paths = set(_path_constants().values())
    paths |= set(_connector._FLEET_BROKEN_DIAGNOSTIC_ENDPOINTS)
    return {f"GET:{path}" for path in paths}


def test_hand_coded_path_surface_is_pinned() -> None:
    """Guard: the introspection finds every hand-coded ``/lcm/*`` literal.

    Pinning the exact ``_*_PATH`` constant-name set, the broken-diagnostic
    tuple, and the assembled op_id set (the #2944 guard shape) means a renamed
    or dropped constant — or a new path that skips the convention — fails here
    until the reconcile is updated consciously, so the declared set can never
    silently shrink to a vacuous pass. Runs unconditionally (no shelf needed),
    so the eight-literal enumeration is proven on every sweep even while the
    spec-assert below is dormant.
    """
    assert sorted(_path_constants()) == [
        "_FLEET_ABOUT_PATH",
        "_FLEET_ENVIRONMENTS_PATH",
        "_FLEET_PROBE_PATH",
    ]
    assert _connector._FLEET_BROKEN_DIAGNOSTIC_ENDPOINTS == (
        "/lcm/lcops/api/v2/about",
        "/lcm/lcops/api/v2/health",
        "/lcm/lcops/api/v2/version",
        "/lcm/lcops/api/v2/system-details",
        "/lcm/common/api/about",
        "/lcm/locker/api/v2/about",
    )
    assert _declared_op_ids() == {
        "GET:/lcm/common/api/about",
        "GET:/lcm/lcops/api/v2/about",
        "GET:/lcm/lcops/api/v2/datacenters",
        "GET:/lcm/lcops/api/v2/environments",
        "GET:/lcm/lcops/api/v2/health",
        "GET:/lcm/lcops/api/v2/system-details",
        "GET:/lcm/lcops/api/v2/version",
        "GET:/lcm/locker/api/v2/about",
    }


def test_declared_op_ids_are_served_by_the_pinned_vrslcm_spec() -> None:
    """Every hand-coded op_id is served by the pinned vRSLCM LCM REST spec.

    Dormant until the spec lands on the shelf (``require_shelf_spec`` skips
    uniformly). See the module docstring for the pin handoff and the first-arm
    triage note.
    """
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_op_ids(), served, spec_label=_SPEC_LABEL)
