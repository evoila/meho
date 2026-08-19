# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vRLI two-impl resolver test — ``product=vrli`` (vrli-rest + vrli-vrli8), #3068.

``product=vrli`` is the codebase's **5th real two-implementation case** (after
``fleet``, ``vcfa``, ``sddc``, ``vrops``; initiative #3056, policy #3033/#3038):

* modern typed ``vrli-rest``
  (:class:`~meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector`) — VCF
  Operations for Logs 9.x, ``supported_version_range=">=9.0,<10.0"``.
* legacy typed ``vrli-vrli8``
  (:class:`~meho_backplane.connectors.vrli8.connector.Vrli8Connector`) — a thin
  subclass of the modern impl for vRealize Log Insight / Aria Operations for Logs
  8.x, ``supported_version_range=">=8.0,<9.0"``.

The **modern** ``vcf_logs`` owns the ``("vrli","","")`` wildcard, so the new legacy
``vrli-vrli8`` registers **only** its versioned triple — the *fleet inversion*: an
unfingerprinted ``vrli`` target resolves to **modern** ``vrli-rest``. A vRLI 8.x
appliance's ``GET /api/v2/version`` returns a real five-part ``8.x`` version string,
so a fingerprinted 8.x target resolves to the legacy impl **by fingerprint alone**.
Bands are **disjoint**, so no specificity tie. Mirrors
:mod:`tests.test_connectors_vrops8_dual_impl_resolution`.
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
from meho_backplane.connectors.vcf_logs.connector import VcfLogsConnector
from meho_backplane.connectors.vrli8.connector import Vrli8Connector


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
    """Register both vrli impls exactly as their ``__init__`` modules do.

    ``vrli-rest`` (modern): versioned triple **plus** the ``("vrli","","")`` wildcard;
    ``vrli-vrli8`` (legacy): the versioned triple only. Real classes so a band drift
    breaks this guard.
    """
    register_connector_v2(
        product=VcfLogsConnector.product,
        version=VcfLogsConnector.version,
        impl_id=VcfLogsConnector.impl_id,
        cls=VcfLogsConnector,
    )
    register_connector_v2(product="vrli", version="", impl_id="", cls=VcfLogsConnector)
    register_connector_v2(
        product=Vrli8Connector.product,
        version=Vrli8Connector.version,
        impl_id=Vrli8Connector.impl_id,
        cls=Vrli8Connector,
    )


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    _register_both_impls()
    yield
    clear_registry()


def test_both_impls_register_under_distinct_triples() -> None:
    """Two ``vrli`` impls, distinct (version, impl_id); the modern impl owns the wildcard."""
    assert (VcfLogsConnector.product, VcfLogsConnector.version, VcfLogsConnector.impl_id) == (
        "vrli",
        "9.0",
        "vrli-rest",
    )
    assert (Vrli8Connector.product, Vrli8Connector.version, Vrli8Connector.impl_id) == (
        "vrli",
        "8.0",
        "vrli-vrli8",
    )
    assert VcfLogsConnector.supported_version_range == ">=9.0,<10.0"
    assert Vrli8Connector.supported_version_range == ">=8.0,<9.0"

    triples = set(all_connectors_v2())
    assert ("vrli", "9.0", "vrli-rest") in triples
    assert ("vrli", "8.0", "vrli-vrli8") in triples
    assert ("vrli", "", "") in triples
    assert all_connectors_v2()[("vrli", "", "")] is VcfLogsConnector


def test_8x_target_resolves_legacy_vrli8() -> None:
    """A vRLI 8.12 target resolves ``vrli-vrli8`` by fingerprint alone."""
    assert resolve_connector(_FakeTarget(product="vrli", fingerprint=_fp("8.12"))) is Vrli8Connector


def test_8x_boundary_and_patch_targets_resolve_legacy() -> None:
    """The band floor (8.0) and a patch (8.18.1) both resolve ``vrli-vrli8`` (in ``>=8.0,<9.0``)."""
    assert resolve_connector(_FakeTarget(product="vrli", fingerprint=_fp("8.0"))) is Vrli8Connector
    assert (
        resolve_connector(_FakeTarget(product="vrli", fingerprint=_fp("8.18.1"))) is Vrli8Connector
    )


def test_8x_preferred_modern_is_moot_out_of_range() -> None:
    """``preferred_impl_id="vrli-rest"`` on an 8.x target cannot flip the out-of-range result."""
    target = _FakeTarget(product="vrli", fingerprint=_fp("8.12"), preferred_impl_id="vrli-rest")
    assert resolve_connector(target) is Vrli8Connector


def test_9x_target_resolves_modern_vrli_rest() -> None:
    """A 9.0 target resolves ``vrli-rest`` (its own wildcard demoted first)."""
    assert (
        resolve_connector(_FakeTarget(product="vrli", fingerprint=_fp("9.0"))) is VcfLogsConnector
    )
    assert (
        resolve_connector(_FakeTarget(product="vrli", fingerprint=_fp("9.0.2"))) is VcfLogsConnector
    )


def test_unfingerprinted_target_resolves_via_modern_wildcard() -> None:
    """A fresh (unfingerprinted, unversioned) ``vrli`` target resolves via the wildcard → MODERN."""
    target = _FakeTarget(product="vrli", fingerprint=None, version=None)
    assert resolve_connector(target) is VcfLogsConnector
