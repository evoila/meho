# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tri-state dispatchability predicate + ``ProfiledRestConnector`` (G0.28-T1 #1967).

The gating task of Initiative #1965 replaces the binary
``issubclass(GenericRestConnector)`` "is this a dead shim" discriminator
with a tri-state :func:`~meho_backplane.connectors.base.shim_kind`
classifier (``"none"`` hand-coded > ``"profiled"`` > ``"bare"`` auto-shim)
at all six live sites, and introduces
:class:`~meho_backplane.connectors.profiled.ProfiledRestConnector` as a
**sibling** (not subclass) of ``GenericRestConnector``.

These tests pin the five acceptance criteria:

1. A ``ProfiledRestConnector`` candidate participates in resolution and is
   **not** demoted (it beats a bare shim).
2. A bare ``GenericRestConnector`` shim still demotes when a dispatchable
   candidate exists.
3. For the same ``(product, version)``, a more-specific hand-coded class
   still out-ranks a profiled class — even when the profiled class's range
   is *narrower* (the #1750/#1798 shadowing scenario).
4. The dispatcher / delete / sibling sites read the tri-state, not the
   subclass relationship (profiled is classified dispatchable, bare is not).
5. ``register_connector_v2``'s product↔impl_id round-trip still hard-fails
   on a divergent profiled registration.
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
    ProfiledRestConnector,
    register_connector_v2,
    resolve_connector,
    shim_kind,
)
from meho_backplane.connectors.registry import all_connectors_v2, clear_registry
from meho_backplane.operations.ingest.connector_registration import (
    GenericRestConnector,
    ensure_connector_class_registered,
    handrolled_class_for_impl_id,
    sibling_handrolled_impl_id,
)
from meho_backplane.operations.ingest.delete_connector import _auto_shim_keys_for_triple

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
# Test connectors — a hand-coded class and a profiled class for the same label
# ---------------------------------------------------------------------------


class _HandRolledNsx(Connector):
    """Stand-in for a shipped hand-coded connector (resolver-only contract).

    Deliberately *broad* range so a profiled / shim candidate's narrower
    range would win the specificity step if the tier rung did not run first.
    """

    product = "nsx"
    supported_version_range = ">=8.0,<11.0"
    priority = 1

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


class _ProfiledNsx(ProfiledRestConnector):
    """A profiled connector with a *narrower* range than ``_HandRolledNsx``.

    The narrower ``>=9.0,<10.0`` is the shadowing trap: on the specificity
    step it would out-specific the hand-coded class's broad range, so the
    tier-demotion rung must drop it first.
    """

    product = "nsx"
    supported_version_range = ">=9.0,<10.0"


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


def _register_bare_shim(impl_id: str = "nsx-rest-probe") -> type[Connector]:
    """Register a real auto-shim via the production entry point."""
    created = ensure_connector_class_registered(
        product="nsx",
        version="9.0",
        impl_id=impl_id,
        base_url=None,
    )
    assert created is True
    return all_connectors_v2()[("nsx", "9.0", impl_id)]


# ---------------------------------------------------------------------------
# shim_kind classification (the single seam every site routes through)
# ---------------------------------------------------------------------------


def test_shim_kind_classifies_all_three_tiers() -> None:
    """``shim_kind`` reports ``none`` / ``profiled`` / ``bare`` per class.

    Also pins the structural invariant: ``ProfiledRestConnector`` is a
    *sibling* of ``GenericRestConnector`` (an ``HttpConnector`` subclass),
    NOT a subclass — the whole reason the binary predicate had to become
    tri-state.
    """
    assert shim_kind(_HandRolledNsx) == "none"
    assert shim_kind(ProfiledRestConnector) == "profiled"
    assert shim_kind(GenericRestConnector) == "bare"
    assert not issubclass(ProfiledRestConnector, GenericRestConnector)


def test_shim_kind_reads_instances_and_synthesised_shims() -> None:
    """``shim_kind`` works on instances and on dynamically-synthesised shims.

    The dispatcher classifies the live ``connector_instance``; the resolver
    classifies ``AutoShim_*`` subclasses produced by ``type()``. Both must
    report through the inherited ``_shim_kind`` without setting it.
    """
    assert shim_kind(ProfiledRestConnector()) == "profiled"
    shim_cls = _register_bare_shim()
    assert shim_kind(shim_cls) == "bare"
    assert shim_kind(shim_cls()) == "bare"


# ---------------------------------------------------------------------------
# AC1 + AC2 — profiled participates and beats a bare shim; bare still demotes
# ---------------------------------------------------------------------------


def test_profiled_connector_beats_bare_shim() -> None:
    """AC1/AC2: a profiled candidate is not demoted; the bare shim is.

    With only a profiled class and a bare shim for the same label, the tier
    rung keeps the profiled connector (dispatchable) and drops the shim.
    """
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest-prof", cls=_ProfiledNsx)
    _register_bare_shim()

    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint("9.0.2"))
    assert resolve_connector(target) is _ProfiledNsx


def test_profiled_over_shim_log_reason(capfd: pytest.CaptureFixture[str]) -> None:
    """The resolution log names the ``profiled_over_shim`` rung, not ``specificity``.

    Pins the ordering: the profiled connector wins on the dispatch-tier rung
    (before the specificity step), so a narrower-ranged bare shim never
    out-specifics it.
    """
    from meho_backplane.logging import configure_logging

    configure_logging()
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest-prof", cls=_ProfiledNsx)
    _register_bare_shim()

    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint("9.0.2"))
    assert resolve_connector(target) is _ProfiledNsx
    out, _ = capfd.readouterr()
    assert '"tie_break": "profiled_over_shim"' in out


def test_only_profiled_for_label_resolves_to_profiled() -> None:
    """A lone profiled candidate resolves (the tier rung is a no-op)."""
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest-prof", cls=_ProfiledNsx)
    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint("9.0.2"))
    assert resolve_connector(target) is _ProfiledNsx


# ---------------------------------------------------------------------------
# AC3 — hand-coded class out-ranks a profiled class (the shadowing guard)
# ---------------------------------------------------------------------------


def test_hand_coded_outranks_profiled_even_with_narrower_profiled_range() -> None:
    """AC3: a more-specific hand-coded class still beats a profiled class.

    The profiled class advertises a *narrower* ``>=9.0,<10.0`` than the
    hand-coded class's ``>=8.0,<11.0``. Before the tri-state rung this would
    let the profiled connector win the specificity step and shadow the
    bespoke connector (the #1750/#1798 footgun). The ``none > profiled``
    tier demotion drops the profiled class first.
    """
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest", cls=_HandRolledNsx)
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest-prof", cls=_ProfiledNsx)

    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint("9.0.2"))
    resolved = resolve_connector(target)
    assert resolved is _HandRolledNsx
    assert shim_kind(resolved) == "none"


def test_hand_coded_outranks_profiled_log_reason(capfd: pytest.CaptureFixture[str]) -> None:
    """The hand-coded win still reports ``hand_rolled_over_shim`` (unchanged)."""
    from meho_backplane.logging import configure_logging

    configure_logging()
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest", cls=_HandRolledNsx)
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest-prof", cls=_ProfiledNsx)

    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint("9.0.2"))
    assert resolve_connector(target) is _HandRolledNsx
    out, _ = capfd.readouterr()
    assert '"tie_break": "hand_rolled_over_shim"' in out


def test_hand_coded_beats_profiled_and_bare_together() -> None:
    """All three tiers present → the hand-coded class wins; both shims drop."""
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest", cls=_HandRolledNsx)
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest-prof", cls=_ProfiledNsx)
    _register_bare_shim()

    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint("9.0.2"))
    assert resolve_connector(target) is _HandRolledNsx


# ---------------------------------------------------------------------------
# AC4 — the ingest-guard / sibling / delete sites read the tri-state
# ---------------------------------------------------------------------------


def test_handrolled_class_for_impl_id_returns_profiled_as_dispatchable() -> None:
    """A profiled class is a dispatchable class the ingest guard defers to.

    ``handrolled_class_for_impl_id`` returns any non-bare class for the
    ``(version, impl_id)``; a profiled class qualifies (it can be shadowed
    by a divergent-product bare shim exactly as a hand-coded one can).
    """
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest-prof", cls=_ProfiledNsx)
    assert handrolled_class_for_impl_id(version="9.0", impl_id="nsx-rest-prof") is _ProfiledNsx


def test_handrolled_class_for_impl_id_skips_bare_shim() -> None:
    """A bare shim is not a class to defer to — it provides no dispatchability."""
    _register_bare_shim(impl_id="nsx-rest-probe")
    assert handrolled_class_for_impl_id(version="9.0", impl_id="nsx-rest-probe") is None


def test_sibling_names_a_profiled_sibling() -> None:
    """A profiled sibling is named by ``sibling_handrolled_impl_id`` (dispatchable)."""
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest-prof", cls=_ProfiledNsx)
    sibling = sibling_handrolled_impl_id(
        product="nsx", version="9.0", exclude_impl_id="nsx-rest-probe"
    )
    assert sibling == "nsx-rest-prof"


def test_delete_excludes_profiled_keeps_bare() -> None:
    """AC4: the delete sweep auto-deregisters only ``bare`` shims.

    A profiled connector is excluded from ``_auto_shim_keys_for_triple``
    (its registration lifecycle is owned by the profile-stamping path, T5);
    a bare shim is included exactly as before.
    """
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest-prof", cls=_ProfiledNsx)
    _register_bare_shim(impl_id="nsx-rest-probe")

    # Profiled triple → not auto-deregistered.
    assert _auto_shim_keys_for_triple("nsx", "9.0", "nsx-rest-prof") == ()
    # Bare-shim triple → auto-deregistered (unchanged behaviour).
    assert _auto_shim_keys_for_triple("nsx", "9.0", "nsx-rest-probe") == (
        ("nsx", "9.0", "nsx-rest-probe"),
    )


# ---------------------------------------------------------------------------
# AC5 — the product↔impl_id round-trip hard-fail still fires for profiled
# ---------------------------------------------------------------------------


def test_divergent_profiled_registration_hard_fails() -> None:
    """AC5: a profiled class registered under a divergent product still raises.

    The round-trip guard is class-agnostic: ``impl_id`` ``vrli-rest`` parses
    to product ``vrli``, so registering a ``ProfiledRestConnector`` under
    ``product="vcf-logs"`` for that impl_id is the product-namespace shadow
    shape and must hard-fail at registration.
    """

    class _DivergentProfiled(ProfiledRestConnector):
        product = "vcf-logs"
        supported_version_range = ">=9.0,<10.0"

    with pytest.raises(RuntimeError, match="resolves a different namespace"):
        register_connector_v2(
            product="vcf-logs",
            version="9.0",
            impl_id="vrli-rest",
            cls=_DivergentProfiled,
        )


def test_aligned_profiled_registration_succeeds() -> None:
    """A profiled class whose product round-trips registers cleanly."""
    register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest-prof", cls=_ProfiledNsx)
    assert all_connectors_v2()[("nsx", "9.0", "nsx-rest-prof")] is _ProfiledNsx
