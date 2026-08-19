# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The first two-impl resolver test — ``product=fleet`` (fleet-rest + fleet-lcm), #3037.

``product=fleet`` is the codebase's **first real two-implementation case**
(initiative #3033). Two hand-rolled ``HttpConnector`` subclasses register
under the same product with distinct ``impl_id``s and overlapping version
bands, and :func:`~meho_backplane.connectors.resolver.resolve_connector` picks
one per target by fingerprint:

* legacy typed ``fleet-rest``
  (:class:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector`) —
  the vRSLCM ``/lcm/*`` surface, ``supported_version_range=">=8.0,<10.0"``
  (re-banded from ``">=9.0,<10.0"`` in this Task).
* modern generic ``fleet-lcm``
  (:class:`~meho_backplane.connectors.fleet_lcm.connector.FleetLcmConnector`) —
  the VCF 9 Fleet LCM Service ``/v1/*`` surface,
  ``supported_version_range=">=9.0,<10.0"``.

This test constructs both registrations exactly as the two packages'
``__init__`` modules do (the legacy adds the ``("fleet","","")`` wildcard
fallback; the modern impl registers only its versioned triple) and asserts the
resolution matrix against fake targets — mirroring the target-construction and
``resolve_connector`` call shape of :mod:`tests.test_connectors_resolver`.

A note on the operator-preference case (a corrected task assertion)
------------------------------------------------------------------

Tasks #3037 / initiative #3033 framed a third case as *"preferred_impl_id=
'fleet-rest' on a 9.0 target → resolves fleet-rest (operator-preference
tie-break)."* That is **not** how the resolver behaves, and it is internally
inconsistent with the second case. The tie-break ladder runs
**most-specific-version (step 2) *before* operator preference (step 3)** — see
the :mod:`~meho_backplane.connectors.resolver` module docstring, and the pin
``test_resolve_operator_preference_does_not_override_specificity`` in
:mod:`tests.test_connectors_resolver` ("This pins the documented ordering
against the issue body's alternative reading"). For any 9.x target *both* impls
are in range, and ``fleet-lcm``'s
``>=9.0,<10.0`` (bounded span 1.0) is **strictly** more specific than
``fleet-rest``'s ``>=8.0,<10.0`` (span 2.0), so specificity resolves to
``fleet-lcm`` and returns *before* ``preferred_impl_id`` is ever consulted.
Operator preference only breaks a specificity **tie** (equal spans), which
these two impls never have. Satisfying "9.0 → fleet-lcm by most-specific-
version" (case 2) and "9.0 + preferred fleet-rest → fleet-rest" (case 3)
simultaneously is impossible under the ladder: case 2 requires ``fleet-lcm``
strictly more specific, which is exactly what makes preference moot in case 3.

So case 3 below asserts the **actual, documented** behaviour — the more-specific
``fleet-lcm`` still wins on a 9.0 target even with
``preferred_impl_id="fleet-rest"`` — and documents that pinning the legacy
``/lcm/*`` surface on a genuine 9.0 target is **not** achievable via
``preferred_impl_id`` under the current (deliberate, previously-settled)
ladder ordering. The generic operator-preference-breaks-a-tie mechanism is
covered by :mod:`tests.test_connectors_resolver`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from meho_backplane.connectors.fleet_lcm.connector import FleetLcmConnector
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.connectors.vcf_fleet.connector import VcfFleetConnector

# ---------------------------------------------------------------------------
# Duck-typed target — the resolver reads .product, .fingerprint.version,
# .version, .preferred_impl_id (mirrors tests.test_connectors_resolver).
# ---------------------------------------------------------------------------


@dataclass
class _FakeFingerprint:
    version: str | None


@dataclass
class _FakeTarget:
    product: str
    fingerprint: _FakeFingerprint | None = None
    preferred_impl_id: str | None = None
    version: str | None = None


def _fp(version: str | None) -> _FakeFingerprint:
    return _FakeFingerprint(version=version)


def _register_both_impls() -> None:
    """Register both fleet impls exactly as their ``__init__`` modules do.

    * ``fleet-rest`` (legacy): the versioned triple **plus** the
      ``("fleet","","")`` wildcard fallback (the G0.15-T6 #1215 fanout).
    * ``fleet-lcm`` (modern): the versioned triple only — a second class on
      the wildcard key would raise ``RuntimeError``.

    Uses the real connector classes (not stand-ins) so a future drift in
    either ``supported_version_range`` breaks this test — the point of a
    first-two-impl guard.
    """
    register_connector_v2(
        product=VcfFleetConnector.product,
        version=VcfFleetConnector.version,
        impl_id=VcfFleetConnector.impl_id,
        cls=VcfFleetConnector,
    )
    register_connector_v2(product="fleet", version="", impl_id="", cls=VcfFleetConnector)
    register_connector_v2(
        product=FleetLcmConnector.product,
        version=FleetLcmConnector.version,
        impl_id=FleetLcmConnector.impl_id,
        cls=FleetLcmConnector,
    )


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    _register_both_impls()
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Registration shape — the two impls coexist under distinct triples
# ---------------------------------------------------------------------------


def test_both_impls_register_under_distinct_triples() -> None:
    """The two impls share ``product="fleet"`` / ``version="9.0"`` but distinct impl_id.

    The v2 keys never collide (impl_id disambiguates), and the ``("fleet","","")``
    wildcard is owned by the legacy impl alone.
    """
    # Class attrs are the source of truth for the registration triples.
    assert (VcfFleetConnector.product, VcfFleetConnector.version, VcfFleetConnector.impl_id) == (
        "fleet",
        "9.0",
        "fleet-rest",
    )
    assert (FleetLcmConnector.product, FleetLcmConnector.version, FleetLcmConnector.impl_id) == (
        "fleet",
        "9.0",
        "fleet-lcm",
    )
    # The re-banded legacy range + the modern range are what the split hinges on.
    assert VcfFleetConnector.supported_version_range == ">=8.0,<10.0"
    assert FleetLcmConnector.supported_version_range == ">=9.0,<10.0"

    triples = set(all_connectors_v2())
    assert ("fleet", "9.0", "fleet-rest") in triples
    assert ("fleet", "9.0", "fleet-lcm") in triples
    assert ("fleet", "", "") in triples  # the single legacy-owned wildcard
    assert all_connectors_v2()[("fleet", "", "")] is VcfFleetConnector


# ---------------------------------------------------------------------------
# Case 1 — an 8.x target resolves the legacy fleet-rest (only in-range candidate)
# ---------------------------------------------------------------------------


def test_8x_target_resolves_legacy_fleet_rest() -> None:
    """A VCF Fleet / Aria Suite Lifecycle 8.5 target resolves ``fleet-rest``.

    ``fleet-lcm``'s ``>=9.0,<10.0`` excludes 8.5, so the legacy impl is the
    only versioned candidate in range; the wildcard is demoted by step 1.
    """
    target = _FakeTarget(product="fleet", fingerprint=_fp("8.5"))
    assert resolve_connector(target) is VcfFleetConnector


# ---------------------------------------------------------------------------
# Case 2 — a 9.0 target resolves the modern fleet-lcm (most-specific-version)
# ---------------------------------------------------------------------------


def test_9_0_target_resolves_modern_fleet_lcm_by_specificity() -> None:
    """A VCF Fleet 9.0 target resolves ``fleet-lcm`` by most-specific-version.

    Both versioned impls are in range for 9.0; ``fleet-lcm``'s bounded
    ``>=9.0,<10.0`` (span 1.0) beats ``fleet-rest``'s ``>=8.0,<10.0`` (span 2.0)
    at the resolver's step-2 specificity tie-break. The wildcard is demoted
    first (step 1). This is the initiative's core assertion — the split is by
    version-specificity, not priority (both impls keep ``priority=1``).
    """
    target = _FakeTarget(product="fleet", fingerprint=_fp("9.0"))
    assert resolve_connector(target) is FleetLcmConnector


def test_9_0_2_patch_target_still_resolves_fleet_lcm() -> None:
    """A patch-versioned 9.0.2 target resolves ``fleet-lcm`` too (in ``>=9.0,<10.0``)."""
    target = _FakeTarget(product="fleet", fingerprint=_fp("9.0.2"))
    assert resolve_connector(target) is FleetLcmConnector


# ---------------------------------------------------------------------------
# Case 3 — operator preference is MOOT on a 9.0 target (specificity wins first)
# ---------------------------------------------------------------------------


def test_9_0_preferred_fleet_rest_is_moot_specificity_still_wins() -> None:
    """``preferred_impl_id="fleet-rest"`` on a 9.0 target does NOT flip the result.

    Corrects the task's third assertion (see the module docstring). Specificity
    (step 2) resolves to the more-specific ``fleet-lcm`` and returns *before*
    operator preference (step 3) is consulted, so the operator **cannot** pin
    the legacy ``/lcm/*`` surface on a genuine 9.0 target this way. Mirrors the
    ``test_resolve_operator_preference_does_not_override_specificity`` pin in
    :mod:`tests.test_connectors_resolver`, scoped to the real fleet impls.
    """
    target = _FakeTarget(
        product="fleet",
        fingerprint=_fp("9.0"),
        preferred_impl_id="fleet-rest",
    )
    assert resolve_connector(target) is FleetLcmConnector


def test_9_0_preferred_fleet_lcm_also_resolves_fleet_lcm() -> None:
    """The consistent-with-specificity preference (``fleet-lcm``) resolves ``fleet-lcm``.

    A sanity companion to the moot case: when the operator's preference agrees
    with the specificity winner, the result is unchanged — preference neither
    helps nor hurts here because specificity already decided.
    """
    target = _FakeTarget(
        product="fleet",
        fingerprint=_fp("9.0"),
        preferred_impl_id="fleet-lcm",
    )
    assert resolve_connector(target) is FleetLcmConnector


# ---------------------------------------------------------------------------
# Wildcard fallback — an unfingerprinted target still resolves (legacy-owned)
# ---------------------------------------------------------------------------


def test_unfingerprinted_target_resolves_via_legacy_wildcard() -> None:
    """A fresh target (no fingerprint, no asserted version) resolves via the wildcard.

    Neither versioned entry matches without a version (the specificity filter
    needs one), so only the legacy-owned ``("fleet","","")`` wildcard survives.
    ``fleet-lcm``'s deliberate lack of a wildcard sibling does not break
    unfingerprinted resolution — one wildcard per product suffices.
    """
    target = _FakeTarget(product="fleet", fingerprint=None, version=None)
    assert resolve_connector(target) is VcfFleetConnector
