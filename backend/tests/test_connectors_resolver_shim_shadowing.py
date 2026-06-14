# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Resolver tie-break: a hand-rolled class beats a GenericRestConnector shim.

Regression guard for the v0.15.0 dogfood signal #1750. The
spec-ingestion pipeline auto-registers a ``GenericRestConnector`` shim
per ingested ``(product, version, impl_id)``. On first ingest under a
*novel* ``impl_id``, that shim becomes a candidate for the whole
``(product, version)`` label alongside a shipped hand-rolled
:class:`~meho_backplane.connectors.base.Connector` subclass. The shim's
:func:`~meho_backplane.operations.ingest.connector_registration.derive_supported_version_range`
pins a *narrower* range around the exact ingested version than a
hand-rolled class's broad range, so before this Task the shim won the
most-specific-version-match step before the hand-rolled class's
``priority`` was ever consulted — a stray probe ingest shadowing a
shipped connector for everyone on a shared registry.

The resolver's ``hand_rolled_over_shim`` rung (added in
:func:`~meho_backplane.connectors.resolver._run_tie_break_ladder`,
before the most-specific-version-match step) drops every shim candidate
the moment a hand-rolled candidate is present. These tests pin that
invariant — and the no-op-when-only-shims case that keeps a genuine
catalog-first staging connector resolving to its shim.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from meho_backplane.connectors import (
    Connector,
    FingerprintResult,
    OperationResult,
    ProbeResult,
    register_connector_v2,
    resolve_connector,
)
from meho_backplane.connectors.registry import all_connectors_v2, clear_registry
from meho_backplane.operations.ingest.connector_registration import (
    GenericRestConnector,
    ensure_connector_class_registered,
)

# ---------------------------------------------------------------------------
# Duck-typed target — the resolver only reads three attributes
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


# ---------------------------------------------------------------------------
# Hand-rolled connector under test — mirrors NsxConnector's resolver-relevant
# shape: a deliberately *broad* range (so the shim's derived range is
# narrower) and ``priority = 1`` (so the test proves priority is NOT what
# saves it — the new rung is).
# ---------------------------------------------------------------------------


class _HandRolledNsx(Connector):
    """Stand-in for the shipped NsxConnector (resolver-only contract)."""

    product = "nsx"
    # Broad on purpose: the auto-shim's derived ">=9.0,<10.0" is narrower,
    # so the pre-fix specificity step would have picked the shim.
    supported_version_range = ">=8.0,<11.0"
    priority = 1

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


def _register_probe_shim() -> type[Connector]:
    """Register an auto-shim the production way, return the synthesised class.

    Uses :func:`ensure_connector_class_registered` — the same entry
    point the ingest pipeline calls on first ingest under a novel
    ``impl_id`` — so the test exercises a real ``GenericRestConnector``
    subclass with the real derived ``supported_version_range`` and
    ``priority = 0``, not a hand-faked stand-in.
    """
    created = ensure_connector_class_registered(
        product="nsx",
        version="9.0",
        impl_id="nsx-rest-probe",
        base_url=None,
    )
    assert created is True
    return all_connectors_v2()[("nsx", "9.0", "nsx-rest-probe")]


# ---------------------------------------------------------------------------
# The shadowing repro — the acceptance criterion
# ---------------------------------------------------------------------------


def test_hand_rolled_class_outranks_auto_shim_for_same_label() -> None:
    """Hand-rolled class wins even when the shim's range is narrower.

    The shipped ``_HandRolledNsx`` advertises a broad ``>=8.0,<11.0``
    range; the auto-shim for the novel ``nsx-rest-probe`` impl_id derives
    a *narrower* ``>=9.0,<10.0`` from the ingested version. Both match a
    target fingerprinted at ``9.0.2``. Before the fix, the shim won the
    most-specific-version-match step before the hand-rolled class's
    ``priority = 1`` was consulted. The ``hand_rolled_over_shim`` rung
    drops the shim first, so the hand-rolled class resolves.
    """
    register_connector_v2(
        product="nsx",
        version="9.0",
        impl_id="nsx-rest",
        cls=_HandRolledNsx,
    )
    shim_cls = _register_probe_shim()

    # Sanity: the shim's derived range really is narrower than the
    # hand-rolled class's — otherwise the test wouldn't exercise the bug.
    assert shim_cls.supported_version_range == ">=9.0,<10.0"
    assert _HandRolledNsx.supported_version_range == ">=8.0,<11.0"
    assert issubclass(shim_cls, GenericRestConnector)
    assert not issubclass(_HandRolledNsx, GenericRestConnector)

    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint("9.0.2"))
    assert resolve_connector(target) is _HandRolledNsx


def test_hand_rolled_over_shim_runs_before_specificity_log_reason(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The resolution log line names the ``hand_rolled_over_shim`` reason.

    Pins the *ordering*: the winning tie-break reason is
    ``hand_rolled_over_shim``, not ``specificity``. If the rung were
    placed after the most-specific-version-match step, the shim's
    narrower range would win on specificity and this assertion would
    fail.
    """
    from meho_backplane.logging import configure_logging

    configure_logging()
    register_connector_v2(
        product="nsx",
        version="9.0",
        impl_id="nsx-rest",
        cls=_HandRolledNsx,
    )
    _register_probe_shim()

    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint("9.0.2"))
    assert resolve_connector(target) is _HandRolledNsx
    out, _ = capfd.readouterr()
    assert "connector_resolved" in out
    assert '"tie_break": "hand_rolled_over_shim"' in out


def test_hand_rolled_wins_even_with_default_priority() -> None:
    """The rung is independent of ``priority`` — a default-priority class wins too.

    ``priority`` semantics are unchanged by this Task; the new rung does
    not lean on the hand-rolled class having a higher priority. A
    hand-rolled class left at the default ``priority = 0`` (same as the
    shim) still beats the shim for the same label.
    """

    class _DefaultPriorityNsx(_HandRolledNsx):
        priority = 0  # same as the shim — the rung, not priority, decides.

    register_connector_v2(
        product="nsx",
        version="9.0",
        impl_id="nsx-rest",
        cls=_DefaultPriorityNsx,
    )
    _register_probe_shim()

    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint("9.0.2"))
    assert resolve_connector(target) is _DefaultPriorityNsx


# ---------------------------------------------------------------------------
# No behaviour change when only auto-shims exist for a label
# ---------------------------------------------------------------------------


def test_only_shim_for_label_still_resolves_to_shim() -> None:
    """A genuine catalog-first staging connector still resolves to its shim.

    When no hand-rolled candidate exists for the label, the
    ``hand_rolled_over_shim`` rung is a no-op: dropping all candidates
    would be wrong. The single auto-shim must still resolve so an
    ingested-but-not-yet-replaced connector keeps dispatching (to the
    degenerate shim, which the review-queue gate keeps disabled until a
    real subclass lands).
    """
    shim_cls = _register_probe_shim()
    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint("9.0.2"))
    assert resolve_connector(target) is shim_cls


def test_two_shims_no_hand_rolled_falls_through_to_later_ladder() -> None:
    """Two shims, no hand-rolled candidate → rung is a no-op, ladder continues.

    With two competing shims and no hand-rolled class, the rung drops
    nothing (it only fires when a hand-rolled candidate is present). The
    rest of the ladder runs: here the two shims derive the *same*
    ``>=9.0,<10.0`` range and carry the same ``priority``, so resolution
    stays ambiguous exactly as it would without the rung — the rung
    neither resolves nor collapses a real shim-vs-shim ambiguity.
    """
    from meho_backplane.connectors import AmbiguousConnectorResolution

    _register_probe_shim()
    second = ensure_connector_class_registered(
        product="nsx",
        version="9.0",
        impl_id="nsx-rest-probe-b",
        base_url=None,
    )
    assert second is True

    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint("9.0.2"))
    with pytest.raises(AmbiguousConnectorResolution) as exc_info:
        resolve_connector(target)
    assert exc_info.value.candidates == [
        ("nsx", "9.0", "nsx-rest-probe"),
        ("nsx", "9.0", "nsx-rest-probe-b"),
    ]


def test_shim_dropped_when_multiple_hand_rolled_remain() -> None:
    """The rung drops shims even when ≥2 hand-rolled candidates remain.

    With two hand-rolled impls plus a shim, the shim is dropped and the
    ladder continues among the hand-rolled candidates alone. Here the two
    hand-rolled classes share a range and priority, so the resolver
    raises ``AmbiguousConnectorResolution`` over the *hand-rolled*
    candidates — the shim is gone from the operator-facing candidate
    list, proving the rung fired before specificity collapsed anything.
    """
    from meho_backplane.connectors import AmbiguousConnectorResolution

    class _HandRolledNsxAlt(_HandRolledNsx):
        pass

    register_connector_v2(
        product="nsx",
        version="9.0",
        impl_id="nsx-rest",
        cls=_HandRolledNsx,
    )
    register_connector_v2(
        product="nsx",
        version="9.0",
        impl_id="nsx-soap",
        cls=_HandRolledNsxAlt,
    )
    _register_probe_shim()

    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint("9.0.2"))
    with pytest.raises(AmbiguousConnectorResolution) as exc_info:
        resolve_connector(target)
    # The shim ("nsx", "9.0", "nsx-rest-probe") is absent — dropped by the
    # rung before the ambiguity surfaced.
    assert exc_info.value.candidates == [
        ("nsx", "9.0", "nsx-rest"),
        ("nsx", "9.0", "nsx-soap"),
    ]
