# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two-impl resolver test for ``product=fleet`` (fleet-rest + fleet-lcm).

``product=fleet`` was the codebase's first real two-implementation case
(initiative #3033). Two hand-rolled ``HttpConnector`` subclasses register
under the same product with distinct ``impl_id``s, and
:func:`~meho_backplane.connectors.resolver.resolve_connector` picks one per
target by fingerprint:

* legacy typed ``fleet-rest``
  (:class:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector`) —
  the ``/lcm/*`` API, ``supported_version_range=">=8.0,<9.1"``. Aria Suite
  Lifecycle 8.x and VCF Fleet 9.0 serve this API.
* modern ``fleet-lcm``
  (:class:`~meho_backplane.connectors.fleet_lcm.connector.FleetLcmConnector`) —
  the ``/fleet-lcm/v1`` API, ``supported_version_range=">=9.1,<10.0"``. This
  API first ships with VCF 9.1 (its spec file is in the ``9.1.0.0`` tag of
  ``vmware/vcf-api-specs``).

The two ranges do not overlap (#4442). Before that change ``fleet-lcm``
claimed ``>=9.0,<10.0`` and won a 9.0 target by the most-specific-range
rule, so a 9.0 appliance got reads against paths it does not serve. Now
every version has exactly one versioned candidate, and operator preference
(``preferred_impl_id``) cannot pick an impl that is out of range.

This test registers both impls exactly as the two packages' ``__init__``
modules do (the legacy adds the ``("fleet","","")`` wildcard fallback; the
modern impl registers only its versioned triple) and checks the resolution
matrix against fake targets — the same target shape and ``resolve_connector``
call as :mod:`tests.test_connectors_resolver`.
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
    # The two ranges meet at 9.1 and do not overlap; the split hinges on them.
    assert VcfFleetConnector.supported_version_range == ">=8.0,<9.1"
    assert FleetLcmConnector.supported_version_range == ">=9.1,<10.0"

    triples = set(all_connectors_v2())
    assert ("fleet", "9.0", "fleet-rest") in triples
    assert ("fleet", "9.0", "fleet-lcm") in triples
    assert ("fleet", "", "") in triples  # the single legacy-owned wildcard
    assert all_connectors_v2()[("fleet", "", "")] is VcfFleetConnector


# ---------------------------------------------------------------------------
# 8.x and 9.0 targets resolve the legacy fleet-rest (/lcm/* API)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["8.5", "8.18", "9.0", "9.0.2"])
def test_8x_and_9_0_targets_resolve_legacy_fleet_rest(version: str) -> None:
    """Aria Suite Lifecycle 8.x and VCF Fleet 9.0 targets resolve ``fleet-rest``.

    ``fleet-lcm``'s ``>=9.1,<10.0`` excludes them, so the legacy impl is the
    only versioned candidate in range; the wildcard is demoted by step 1.
    """
    target = _FakeTarget(product="fleet", fingerprint=_fp(version))
    assert resolve_connector(target) is VcfFleetConnector


# ---------------------------------------------------------------------------
# 9.1 and newer targets resolve the modern fleet-lcm (/fleet-lcm/v1 API)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["9.1", "9.1.0", "9.1.1"])
def test_9_1_targets_resolve_modern_fleet_lcm(version: str) -> None:
    """VCF Fleet 9.1 and newer targets resolve ``fleet-lcm``.

    ``fleet-rest``'s ``>=8.0,<9.1`` excludes them, so the modern impl is the
    only versioned candidate in range.
    """
    target = _FakeTarget(product="fleet", fingerprint=_fp(version))
    assert resolve_connector(target) is FleetLcmConnector


# ---------------------------------------------------------------------------
# Operator preference cannot pick an out-of-range impl
# ---------------------------------------------------------------------------


def test_9_0_preferred_fleet_lcm_still_resolves_fleet_rest() -> None:
    """``preferred_impl_id="fleet-lcm"`` on a 9.0 target does not pick ``fleet-lcm``.

    Preference only breaks a tie between in-range candidates. On 9.0 only
    ``fleet-rest`` is in range, so the 9.0 appliance keeps the ``/lcm/*`` API.
    """
    target = _FakeTarget(
        product="fleet",
        fingerprint=_fp("9.0.2"),
        preferred_impl_id="fleet-lcm",
    )
    assert resolve_connector(target) is VcfFleetConnector


def test_9_1_preferred_fleet_rest_still_resolves_fleet_lcm() -> None:
    """``preferred_impl_id="fleet-rest"`` on a 9.1 target does not pick ``fleet-rest``."""
    target = _FakeTarget(
        product="fleet",
        fingerprint=_fp("9.1.0"),
        preferred_impl_id="fleet-rest",
    )
    assert resolve_connector(target) is FleetLcmConnector


# ---------------------------------------------------------------------------
# Wildcard fallback — an unfingerprinted target still resolves (legacy-owned)
# ---------------------------------------------------------------------------


def test_unfingerprinted_target_resolves_via_legacy_wildcard() -> None:
    """A fresh target (no fingerprint, no asserted version) resolves via the wildcard.

    Neither versioned entry matches without a version (the range filter
    needs one), so only the legacy-owned ``("fleet","","")`` wildcard survives.
    ``fleet-lcm``'s deliberate lack of a wildcard sibling does not break
    unfingerprinted resolution — one wildcard per product suffices.
    """
    target = _FakeTarget(product="fleet", fingerprint=None, version=None)
    assert resolve_connector(target) is VcfFleetConnector
