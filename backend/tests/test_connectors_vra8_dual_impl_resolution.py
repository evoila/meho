# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The second two-impl resolver test — ``product=vcfa`` (vcfa-rest + vcfa-vra8), #3058.

``product=vcfa`` is the codebase's **second real two-implementation case** (after
``fleet``; initiative #3056, policy #3033/#3038). Two hand-rolled ``HttpConnector``
subclasses register under the same product with distinct ``impl_id``s and
**disjoint** version bands, and
:func:`~meho_backplane.connectors.resolver.resolve_connector` picks one per target by
fingerprint:

* modern typed ``vcfa-rest``
  (:class:`~meho_backplane.connectors.vcf_automation.connector.VcfAutomationConnector`)
  — VCF Automation 9.x, ``supported_version_range=">=9.0,<10.0"``.
* legacy typed ``vcfa-vra8``
  (:class:`~meho_backplane.connectors.vra8.connector.Vra8Connector`) — vRealize
  Automation 8.x, ``supported_version_range=">=8.0,<9.0"``.

The inversion vs ``fleet``
--------------------------

In the ``fleet`` case the **legacy** ``vcf_fleet`` owns the ``("fleet","","")``
wildcard and the modern ``fleet_lcm`` skips it. Here it is inverted: the **modern**
``vcf_automation`` owns ``("vcfa","","")`` (it shipped first, as the sole impl), so
the new legacy ``vcfa-vra8`` registers **only** its versioned triple. Consequence:
an *unfingerprinted* / unversioned ``vcfa`` target resolves to **modern**
``vcfa-rest`` (the wildcard owner) — a vRA 8 target must carry an 8.x version
(operator-asserted at onboarding) to resolve to the legacy impl.

Because the two bands are **disjoint** (no overlap at any version), resolution never
reaches the specificity tie-break the ``fleet`` case exercises: an 8.x target has
exactly one in-range versioned candidate (legacy), a 9.x target exactly one (modern).
This test constructs both registrations exactly as the two packages' ``__init__``
modules do and asserts the resolution matrix — mirroring
:mod:`tests.test_connectors_fleet_dual_impl_resolution`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
from meho_backplane.connectors.vra8.connector import Vra8Connector

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
    """Register both vcfa impls exactly as their ``__init__`` modules do.

    * ``vcfa-rest`` (modern): the versioned triple **plus** the ``("vcfa","","")``
      wildcard fallback (the G0.15-T6 #1215 fanout it shipped with as the sole impl).
    * ``vcfa-vra8`` (legacy): the versioned triple only — a second class on the
      wildcard key would raise ``RuntimeError``.

    Uses the real connector classes (not stand-ins) so a future drift in either
    ``supported_version_range`` breaks this test — the point of a two-impl guard.
    """
    register_connector_v2(
        product=VcfAutomationConnector.product,
        version=VcfAutomationConnector.version,
        impl_id=VcfAutomationConnector.impl_id,
        cls=VcfAutomationConnector,
    )
    register_connector_v2(product="vcfa", version="", impl_id="", cls=VcfAutomationConnector)
    register_connector_v2(
        product=Vra8Connector.product,
        version=Vra8Connector.version,
        impl_id=Vra8Connector.impl_id,
        cls=Vra8Connector,
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
    """The two impls share ``product="vcfa"`` but distinct (version, impl_id).

    The v2 keys never collide (impl_id + version disambiguate), and the
    ``("vcfa","","")`` wildcard is owned by the **modern** impl alone (the inversion
    vs fleet).
    """
    # Class attrs are the source of truth for the registration triples.
    assert (
        VcfAutomationConnector.product,
        VcfAutomationConnector.version,
        VcfAutomationConnector.impl_id,
    ) == ("vcfa", "9.0", "vcfa-rest")
    assert (Vra8Connector.product, Vra8Connector.version, Vra8Connector.impl_id) == (
        "vcfa",
        "8.0",
        "vcfa-vra8",
    )
    # The disjoint bands are what the split hinges on (no overlap → no specificity tie).
    assert VcfAutomationConnector.supported_version_range == ">=9.0,<10.0"
    assert Vra8Connector.supported_version_range == ">=8.0,<9.0"

    triples = set(all_connectors_v2())
    assert ("vcfa", "9.0", "vcfa-rest") in triples
    assert ("vcfa", "8.0", "vcfa-vra8") in triples
    assert ("vcfa", "", "") in triples  # the single wildcard
    # ...owned by the MODERN impl (inversion vs fleet, where the legacy owns it).
    assert all_connectors_v2()[("vcfa", "", "")] is VcfAutomationConnector


# ---------------------------------------------------------------------------
# Case 1 — an 8.x target resolves the legacy vcfa-vra8 (only in-range candidate)
# ---------------------------------------------------------------------------


def test_8x_target_resolves_legacy_vra8() -> None:
    """A vRA 8.12 target resolves ``vcfa-vra8``.

    ``vcfa-rest``'s ``>=9.0,<10.0`` excludes 8.12, so the legacy impl is the only
    in-range versioned candidate; the modern-owned wildcard is demoted by step 1.
    """
    target = _FakeTarget(product="vcfa", fingerprint=_fp("8.12"))
    assert resolve_connector(target) is Vra8Connector


def test_8x_boundary_and_patch_targets_resolve_legacy() -> None:
    """The band floor (8.0) and a patch (8.18.1) both resolve ``vcfa-vra8`` (in ``>=8.0,<9.0``)."""
    assert resolve_connector(_FakeTarget(product="vcfa", fingerprint=_fp("8.0"))) is Vra8Connector
    assert (
        resolve_connector(_FakeTarget(product="vcfa", fingerprint=_fp("8.18.1"))) is Vra8Connector
    )


def test_8x_preferred_modern_is_moot_out_of_range() -> None:
    """``preferred_impl_id="vcfa-rest"`` on an 8.x target cannot flip the result.

    Operator preference only breaks a tie among **in-range** candidates. At 8.12
    only the legacy impl is in range (modern's ``>=9.0,<10.0`` excludes it), so a
    preference for the out-of-range modern impl is moot — the disjoint-band
    robustness the ``fleet`` overlapping-band case cannot assert.
    """
    target = _FakeTarget(product="vcfa", fingerprint=_fp("8.12"), preferred_impl_id="vcfa-rest")
    assert resolve_connector(target) is Vra8Connector


# ---------------------------------------------------------------------------
# Case 2 — a 9.x target resolves the modern vcfa-rest (only in-range candidate)
# ---------------------------------------------------------------------------


def test_9x_target_resolves_modern_vcfa_rest() -> None:
    """A VCF Automation 9.0 target resolves ``vcfa-rest``.

    ``vcfa-vra8``'s ``>=8.0,<9.0`` excludes 9.0, so the modern impl is the only
    in-range versioned candidate; its own wildcard is demoted first (step 1).
    """
    target = _FakeTarget(product="vcfa", fingerprint=_fp("9.0"))
    assert resolve_connector(target) is VcfAutomationConnector


def test_9_0_2_patch_target_resolves_modern() -> None:
    """A patch-versioned 9.0.2 target resolves ``vcfa-rest`` too (in ``>=9.0,<10.0``)."""
    target = _FakeTarget(product="vcfa", fingerprint=_fp("9.0.2"))
    assert resolve_connector(target) is VcfAutomationConnector


# ---------------------------------------------------------------------------
# Wildcard fallback — an unfingerprinted target resolves to MODERN (the inversion)
# ---------------------------------------------------------------------------


def test_unfingerprinted_target_resolves_via_modern_wildcard() -> None:
    """A fresh target (no fingerprint, no asserted version) resolves via the wildcard.

    Neither versioned entry matches without a version (the specificity filter needs
    one), so only the ``("vcfa","","")`` wildcard survives — and here that wildcard is
    owned by the **modern** ``vcfa-rest`` (the inversion vs fleet). So an
    unfingerprinted vRA-8 estate resolves to modern until it carries an 8.x version;
    this pins that documented behaviour so a future wildcard re-assignment can't
    silently change it.
    """
    target = _FakeTarget(product="vcfa", fingerprint=None, version=None)
    assert resolve_connector(target) is VcfAutomationConnector
