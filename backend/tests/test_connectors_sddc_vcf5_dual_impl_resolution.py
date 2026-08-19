# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The third two-impl resolver test — ``product=sddc`` (sddc-rest + sddc-vcf5), #3059.

``product=sddc`` becomes a two-implementation case (after ``fleet`` and ``vcfa``;
initiative #3056, policy #3033/#3038). Two hand-rolled ``HttpConnector`` subclasses
register under the same product with distinct ``impl_id``s and **disjoint** version
bands, and :func:`~meho_backplane.connectors.resolver.resolve_connector` picks one per
target by fingerprint:

* modern ``sddc-rest``
  (:class:`~meho_backplane.connectors.sddc_manager.connector.SddcManagerConnector`) —
  VCF 9.x, ``supported_version_range=">=9.0,<10.0"``.
* legacy ``sddc-vcf5``
  (:class:`~meho_backplane.connectors.sddc_vcf5.connector.SddcVcf5Connector`) — VCF
  5.x, ``supported_version_range=">=5.0,<9.0"``.

The inversion vs ``fleet``: the **modern** ``sddc_manager`` owns the ``("sddc","","")``
wildcard (it shipped first), so the legacy ``sddc-vcf5`` registers **only** its
versioned triple. An unfingerprinted ``sddc`` target resolves to modern — but in
practice a VCF 5.x target fingerprints straight into the ``>=5.0,<9.0`` band (SDDC
Manager exposes a real product version at ``GET /v1/sddc-managers``, e.g.
``"5.2.0.0-24276214"``), so it resolves to the legacy impl without an operator-asserted
version. Mirrors :mod:`tests.test_connectors_vra8_dual_impl_resolution`.
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
from meho_backplane.connectors.sddc_manager.connector import SddcManagerConnector
from meho_backplane.connectors.sddc_vcf5.connector import SddcVcf5Connector


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
    """Register both sddc impls exactly as their ``__init__`` modules do.

    * ``sddc-rest`` (modern): the versioned triple **plus** the ``("sddc","","")``
      wildcard fallback.
    * ``sddc-vcf5`` (legacy): the versioned triple only — a second class on the
      wildcard key would raise ``RuntimeError``.
    """
    register_connector_v2(
        product=SddcManagerConnector.product,
        version=SddcManagerConnector.version,
        impl_id=SddcManagerConnector.impl_id,
        cls=SddcManagerConnector,
    )
    register_connector_v2(product="sddc", version="", impl_id="", cls=SddcManagerConnector)
    register_connector_v2(
        product=SddcVcf5Connector.product,
        version=SddcVcf5Connector.version,
        impl_id=SddcVcf5Connector.impl_id,
        cls=SddcVcf5Connector,
    )


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    _register_both_impls()
    yield
    clear_registry()


def test_both_impls_register_under_distinct_triples() -> None:
    """Two impls share ``product="sddc"`` with distinct (version, impl_id); modern owns wildcard."""
    assert (
        SddcManagerConnector.product,
        SddcManagerConnector.version,
        SddcManagerConnector.impl_id,
    ) == ("sddc", "9.0", "sddc-rest")
    assert (SddcVcf5Connector.product, SddcVcf5Connector.version, SddcVcf5Connector.impl_id) == (
        "sddc",
        "5.0",
        "sddc-vcf5",
    )
    # Disjoint bands (no overlap → no specificity tie).
    assert SddcManagerConnector.supported_version_range == ">=9.0,<10.0"
    assert SddcVcf5Connector.supported_version_range == ">=5.0,<9.0"

    triples = set(all_connectors_v2())
    assert ("sddc", "9.0", "sddc-rest") in triples
    assert ("sddc", "5.0", "sddc-vcf5") in triples
    assert ("sddc", "", "") in triples
    # ...owned by the MODERN impl (inversion vs fleet).
    assert all_connectors_v2()[("sddc", "", "")] is SddcManagerConnector


def test_vcf5_full_version_string_resolves_legacy() -> None:
    """A realistic VCF 5.2 fingerprint (``"5.2.0.0-24276214"``) resolves ``sddc-vcf5``.

    This is the load-bearing case: the connector's fingerprint reads the full VCF
    version string with a build suffix off ``GET /v1/sddc-managers``, so resolution
    must accept it into ``>=5.0,<9.0`` — a VCF 5.x source resolves to the legacy impl
    on fingerprint alone (no operator-asserted version needed).
    """
    target = _FakeTarget(product="sddc", fingerprint=_fp("5.2.0.0-24276214"))
    assert resolve_connector(target) is SddcVcf5Connector


def test_5x_boundary_and_patch_targets_resolve_legacy() -> None:
    """The band floor (5.0) and a 5.1/5.2 target resolve ``sddc-vcf5`` (in ``>=5.0,<9.0``)."""
    assert (
        resolve_connector(_FakeTarget(product="sddc", fingerprint=_fp("5.0"))) is SddcVcf5Connector
    )
    assert (
        resolve_connector(_FakeTarget(product="sddc", fingerprint=_fp("5.1.1.0")))
        is SddcVcf5Connector
    )


def test_9x_target_resolves_modern_sddc_rest() -> None:
    """A VCF 9.0 target resolves ``sddc-rest`` (``>=9.0,<10.0``; legacy excludes it)."""
    target = _FakeTarget(product="sddc", fingerprint=_fp("9.0.0.0-24000000"))
    assert resolve_connector(target) is SddcManagerConnector


def test_5x_preferred_modern_is_moot_out_of_range() -> None:
    """``preferred_impl_id="sddc-rest"`` on a 5.x target cannot flip the result (out of range)."""
    target = _FakeTarget(
        product="sddc", fingerprint=_fp("5.2.0.0-24276214"), preferred_impl_id="sddc-rest"
    )
    assert resolve_connector(target) is SddcVcf5Connector


def test_unfingerprinted_target_resolves_via_modern_wildcard() -> None:
    """A fresh target (no fingerprint / version) resolves via the MODERN-owned wildcard.

    The inversion vs fleet: an unfingerprinted ``sddc`` estate resolves to modern
    ``sddc-rest`` until it carries a 5.x version. Pins the documented behaviour so a
    future wildcard re-assignment can't silently change it.
    """
    target = _FakeTarget(product="sddc", fingerprint=None, version=None)
    assert resolve_connector(target) is SddcManagerConnector
