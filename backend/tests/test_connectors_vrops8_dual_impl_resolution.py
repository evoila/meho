# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vROps two-impl resolver test — ``product=vrops`` (vrops-rest + vrops-vrops8), #3067.

``product=vrops`` is the codebase's **4th real two-implementation case** (after
``fleet``, ``vcfa``, ``sddc``; initiative #3056, policy #3033/#3038). Two
``HttpConnector`` subclasses register under the same product with distinct
``impl_id``s and **disjoint** version bands, and
:func:`~meho_backplane.connectors.resolver.resolve_connector` picks one per target
by fingerprint:

* modern typed ``vrops-rest``
  (:class:`~meho_backplane.connectors.vcf_operations.connector.VcfOperationsConnector`)
  — VCF Operations 9.x, ``supported_version_range=">=9.0,<10.0"``.
* legacy typed ``vrops-vrops8``
  (:class:`~meho_backplane.connectors.vrops8.connector.Vrops8Connector`) — a thin
  subclass of the modern impl for vRealize Operations / Aria Operations 8.x,
  ``supported_version_range=">=8.0,<9.0"``.

The inversion vs ``fleet``
--------------------------

The **modern** ``vcf_operations`` owns the ``("vrops","","")`` wildcard (it shipped
first, as the sole impl), so the new legacy ``vrops-vrops8`` registers **only** its
versioned triple. Consequence: an *unfingerprinted* / unversioned ``vrops`` target
resolves to **modern** ``vrops-rest`` (the wildcard owner).

Unlike vRA 8 (whose appliance fingerprint reports the API version, not a product
version, so an 8.x target needs an operator-asserted version), a vROps 8.x
appliance's ``GET /suite-api/api/versions/current`` returns a real product
``releaseName`` (e.g. ``8.18.x``), so a fingerprinted 8.x target resolves to the
legacy impl **by fingerprint alone**. Because the two bands are **disjoint**,
resolution never reaches the specificity tie-break: an 8.x target has exactly one
in-range versioned candidate (legacy), a 9.x target exactly one (modern). Mirrors
:mod:`tests.test_connectors_vra8_dual_impl_resolution`.
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
from meho_backplane.connectors.vcf_operations.connector import VcfOperationsConnector
from meho_backplane.connectors.vrops8.connector import Vrops8Connector


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
    """Register both vrops impls exactly as their ``__init__`` modules do.

    * ``vrops-rest`` (modern): the versioned triple **plus** the ``("vrops","","")``
      wildcard fallback (the G0.15-T6 #1215 fanout it shipped with as the sole impl).
    * ``vrops-vrops8`` (legacy): the versioned triple only — a second class on the
      wildcard key would raise ``RuntimeError``.

    Uses the real connector classes so a future drift in either
    ``supported_version_range`` breaks this test — the point of a two-impl guard.
    """
    register_connector_v2(
        product=VcfOperationsConnector.product,
        version=VcfOperationsConnector.version,
        impl_id=VcfOperationsConnector.impl_id,
        cls=VcfOperationsConnector,
    )
    register_connector_v2(product="vrops", version="", impl_id="", cls=VcfOperationsConnector)
    register_connector_v2(
        product=Vrops8Connector.product,
        version=Vrops8Connector.version,
        impl_id=Vrops8Connector.impl_id,
        cls=Vrops8Connector,
    )


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    _register_both_impls()
    yield
    clear_registry()


def test_both_impls_register_under_distinct_triples() -> None:
    """The two impls share ``product="vrops"`` but distinct (version, impl_id).

    The v2 keys never collide (impl_id + version disambiguate), and the
    ``("vrops","","")`` wildcard is owned by the **modern** impl alone (inversion vs fleet).
    """
    assert (
        VcfOperationsConnector.product,
        VcfOperationsConnector.version,
        VcfOperationsConnector.impl_id,
    ) == ("vrops", "9.0", "vrops-rest")
    assert (Vrops8Connector.product, Vrops8Connector.version, Vrops8Connector.impl_id) == (
        "vrops",
        "8.0",
        "vrops-vrops8",
    )
    # The disjoint bands are what the split hinges on (no overlap → no specificity tie).
    assert VcfOperationsConnector.supported_version_range == ">=9.0,<10.0"
    assert Vrops8Connector.supported_version_range == ">=8.0,<9.0"

    triples = set(all_connectors_v2())
    assert ("vrops", "9.0", "vrops-rest") in triples
    assert ("vrops", "8.0", "vrops-vrops8") in triples
    assert ("vrops", "", "") in triples  # the single wildcard
    # ...owned by the MODERN impl (inversion vs fleet, where the legacy owns it).
    assert all_connectors_v2()[("vrops", "", "")] is VcfOperationsConnector


def test_8x_target_resolves_legacy_vrops8() -> None:
    """A vROps 8.18 target resolves ``vrops-vrops8`` — the only in-range candidate.

    A real 8.x appliance reports this version off ``versions/current``, so the split
    happens by fingerprint alone (no operator-asserted version needed, unlike vRA 8).
    """
    target = _FakeTarget(product="vrops", fingerprint=_fp("8.18"))
    assert resolve_connector(target) is Vrops8Connector


def test_8x_boundary_and_patch_targets_resolve_legacy() -> None:
    """The band floor (8.0) and a patch (8.18.1) both resolve ``vrops-vrops8``."""
    assert (
        resolve_connector(_FakeTarget(product="vrops", fingerprint=_fp("8.0"))) is Vrops8Connector
    )
    assert (
        resolve_connector(_FakeTarget(product="vrops", fingerprint=_fp("8.18.1")))
        is Vrops8Connector
    )


def test_8x_preferred_modern_is_moot_out_of_range() -> None:
    """``preferred_impl_id="vrops-rest"`` on an 8.x target cannot flip the result.

    Operator preference only breaks a tie among **in-range** candidates; at 8.18 only
    the legacy impl is in range, so a preference for the out-of-range modern impl is
    moot — the disjoint-band robustness the ``fleet`` overlapping-band case cannot assert.
    """
    target = _FakeTarget(product="vrops", fingerprint=_fp("8.18"), preferred_impl_id="vrops-rest")
    assert resolve_connector(target) is Vrops8Connector


def test_9x_target_resolves_modern_vrops_rest() -> None:
    """A VCF Operations 9.0 target resolves ``vrops-rest`` (its own wildcard demoted first)."""
    assert (
        resolve_connector(_FakeTarget(product="vrops", fingerprint=_fp("9.0")))
        is VcfOperationsConnector
    )
    assert (
        resolve_connector(_FakeTarget(product="vrops", fingerprint=_fp("9.0.2")))
        is VcfOperationsConnector
    )


def test_unfingerprinted_target_resolves_via_modern_wildcard() -> None:
    """A fresh target (no fingerprint, no asserted version) resolves via the wildcard → MODERN.

    Neither versioned entry matches without a version, so only the ``("vrops","","")``
    wildcard survives — owned here by the **modern** ``vrops-rest`` (the inversion vs
    fleet). An unfingerprinted vROps estate resolves to modern until it carries an 8.x
    version; this pins that documented behaviour so a future wildcard re-assignment
    can't silently change it.
    """
    target = _FakeTarget(product="vrops", fingerprint=None, version=None)
    assert resolve_connector(target) is VcfOperationsConnector


def test_unparseable_release_name_falls_back_to_modern_documenting_limitation() -> None:
    """A non-PEP-440 fingerprint version resolves *open* to modern — the named residual risk.

    The inherited fingerprint stores ``versions/current``'s ``releaseName`` raw, so
    resolution depends on it being PEP 440-parseable. If an 8.x appliance ever reported
    a non-parseable ``releaseName`` (a word / a space), the resolver cannot band it and
    only the modern-owned wildcard survives → the target resolves to ``vrops-rest``
    (which would present the wrong 9.x ``OpsToken`` scheme). This pins that documented
    limitation (the #3067 review's top finding) as *visible, regression-guarded*
    behaviour rather than a silent surprise; the operator escape hatch is to pin
    ``version`` / ``preferred_impl_id`` on the target. The observed ``releaseName`` is
    always a dotted numeric release, so this path is not expected in practice — but it
    is unverified against a live 8.x appliance.
    """
    target = _FakeTarget(product="vrops", fingerprint=_fp("Aria Operations 8.18"))
    assert resolve_connector(target) is VcfOperationsConnector
