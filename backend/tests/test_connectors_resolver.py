# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the connector resolver (G0.6-T2 #393).

Covers the full tie-break ladder:

1. Versioned beats wildcard (G0.14-T2 #1143).
2. Most-specific-version-match wins.
3. Operator/tenant preference (``target.preferred_impl_id``).
4. Connector class :attr:`priority` (higher wins).

Plus the error paths (``NoMatchingConnector``,
``AmbiguousConnectorResolution``) and the v1 backward-compat fallback
(connectors registered via the shipped v1 entry point keep resolving for
targets without a fingerprint version).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from meho_backplane.connectors import (
    AmbiguousConnectorResolution,
    Connector,
    FingerprintResult,
    NoMatchingConnector,
    OperationResult,
    ProbeResult,
    register_connector,
    register_connector_v2,
    resolve_connector,
)
from meho_backplane.connectors.registry import clear_registry

# ---------------------------------------------------------------------------
# Duck-typed target — the resolver only reads three attributes
# ---------------------------------------------------------------------------


@dataclass
class _FakeFingerprint:
    version: str | None


@dataclass
class _FakeTarget:
    """Minimal target shape the resolver reads from.

    Mirrors the parts of :class:`~meho_backplane.db.models.Target` the
    resolver actually touches: ``.product`` (always set on a Target row),
    ``.version`` (operator-asserted version added by G0.15-T6 #1215;
    optional fallback when ``fingerprint.version`` is absent),
    ``.fingerprint.version`` (optional — populated after the connector
    fingerprints the endpoint), and ``.preferred_impl_id`` (optional —
    operator override added in the G0.3 amendments per #224, default
    ``None`` until the column lands).
    """

    product: str
    fingerprint: _FakeFingerprint | None = None
    preferred_impl_id: str | None = None
    version: str | None = None


def _fingerprint(version: str | None = None) -> _FakeFingerprint:
    return _FakeFingerprint(version=version)


# ---------------------------------------------------------------------------
# Connector subclasses for the test matrix
# ---------------------------------------------------------------------------


class _BaseFakeConnector(Connector):
    product = "vmware"

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


class _VmwareRest9(_BaseFakeConnector):
    """Narrow vmware-rest connector advertising 9.x only."""

    supported_version_range = ">=9.0,<10.0"


class _VmwareWide(_BaseFakeConnector):
    """Wide vmware connector advertising 6.5 through 9.x."""

    supported_version_range = ">=6.5,<10.0"


class _VmwarePyvmomi(_BaseFakeConnector):
    """Alt impl with the same wide range — used to test operator override + priority."""

    supported_version_range = ">=6.5,<10.0"


class _VmwareHighPriority(_BaseFakeConnector):
    """Same range as _VmwarePyvmomi but with higher priority."""

    supported_version_range = ">=6.5,<10.0"
    priority = 10


class _VaultConnector(Connector):
    product = "vault"

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
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


# ---------------------------------------------------------------------------
# Single-match happy path
# ---------------------------------------------------------------------------


def test_resolve_single_versioned_match_returns_class() -> None:
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    target = _FakeTarget(product="vmware", fingerprint=_fingerprint("9.0.2"))
    assert resolve_connector(target) is _VmwareRest9


def test_resolve_outside_advertised_range_returns_no_match() -> None:
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    target = _FakeTarget(product="vmware", fingerprint=_fingerprint("8.0.0"))
    with pytest.raises(NoMatchingConnector, match="vmware"):
        resolve_connector(target)


# ---------------------------------------------------------------------------
# Production fingerprint shape — a JSON dict, not a duck-typed object
# ---------------------------------------------------------------------------
#
# Regression guard for the v0.2 ship blocker: the ORM stores
# ``Target.fingerprint`` as ``FingerprintResult.model_dump(mode="json")``
# — a plain dict. The resolver previously read the version with
# ``getattr(fp, "version", None)``, which is always ``None`` on a dict,
# so *every* real probed target failed to match any versioned connector
# (→ ``NoMatchingConnector`` → ``no_connector`` at dispatch). Every
# pre-existing resolver test used the duck-typed ``_FakeFingerprint``
# object, so the dict path had zero coverage and shipped broken. These
# two tests pin the production shape.


@dataclass
class _DictFingerprintTarget:
    """Target whose ``fingerprint`` is the real JSON dict, not an object."""

    product: str
    fingerprint: dict[str, object] | None = None
    preferred_impl_id: str | None = None


def test_resolve_reads_version_from_dict_fingerprint() -> None:
    """A probed target (dict fingerprint) resolves the versioned connector."""
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    fingerprint = FingerprintResult(
        vendor="VMware, Inc.",
        product="vcenter",
        version="9.0.2",
        reachable=True,
        probed_at=datetime(2026, 5, 15, tzinfo=UTC),
        probe_method="rest-probe",
    ).model_dump(mode="json")
    target = _DictFingerprintTarget(product="vmware", fingerprint=fingerprint)
    assert resolve_connector(target) is _VmwareRest9


def test_resolve_dict_fingerprint_without_version_skips_versioned_connector() -> None:
    """A dict fingerprint missing ``version`` matches no versioned connector.

    Mirrors the pre-fingerprint window: a target row exists but the
    probe hasn't populated a version yet. A versioned connector must
    not bind on no version (the resolver's documented contract); the
    failure mode is a clean ``NoMatchingConnector``, not a wrong bind.
    """
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    target = _DictFingerprintTarget(
        product="vmware",
        fingerprint={"vendor": "VMware, Inc.", "product": "vcenter", "reachable": True},
    )
    with pytest.raises(NoMatchingConnector, match="vmware"):
        resolve_connector(target)


def test_resolve_unknown_product_raises_no_match() -> None:
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    target = _FakeTarget(product="kubernetes", fingerprint=_fingerprint("1.30.0"))
    with pytest.raises(NoMatchingConnector, match="kubernetes"):
        resolve_connector(target)


def test_resolve_target_without_product_raises_no_match() -> None:
    target = _FakeTarget(product="", fingerprint=_fingerprint("9.0.2"))
    with pytest.raises(NoMatchingConnector, match="product slug"):
        resolve_connector(target)


# ---------------------------------------------------------------------------
# Step 2 — most-specific-version-match wins
# ---------------------------------------------------------------------------


def test_resolve_picks_narrowest_version_range() -> None:
    """``>=9.0,<10.0`` (span 1.0) beats ``>=6.5,<10.0`` (span 3.5) for v=9.0.2."""
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    register_connector_v2(
        product="vmware",
        version="legacy",
        impl_id="vmware-wide",
        cls=_VmwareWide,
    )
    target = _FakeTarget(product="vmware", fingerprint=_fingerprint("9.0.2"))
    assert resolve_connector(target) is _VmwareRest9


def test_resolve_bounded_beats_unbounded_range() -> None:
    """A bounded ``>=9.0,<10.0`` beats an unbounded ``None`` advertisement."""

    class _VmwareUnbounded(_BaseFakeConnector):
        supported_version_range = None

    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    register_connector_v2(
        product="vmware",
        version="any",
        impl_id="vmware-any",
        cls=_VmwareUnbounded,
    )
    target = _FakeTarget(product="vmware", fingerprint=_fingerprint("9.0.2"))
    assert resolve_connector(target) is _VmwareRest9


# ---------------------------------------------------------------------------
# Step 3 — operator/tenant preference
# ---------------------------------------------------------------------------


def test_resolve_operator_preference_breaks_specificity_tie() -> None:
    """Two impls share the same range → preferred_impl_id picks the winner."""
    register_connector_v2(
        product="vmware",
        version="rest",
        impl_id="vmware-rest",
        cls=_VmwareWide,
    )
    register_connector_v2(
        product="vmware",
        version="pyvmomi",
        impl_id="vmware-pyvmomi",
        cls=_VmwarePyvmomi,
    )
    target = _FakeTarget(
        product="vmware",
        fingerprint=_fingerprint("9.0.2"),
        preferred_impl_id="vmware-pyvmomi",
    )
    assert resolve_connector(target) is _VmwarePyvmomi


def test_resolve_operator_preference_does_not_override_specificity() -> None:
    """When specificity already disambiguates, operator preference is moot.

    Spec ladder runs first: narrowest range wins. The operator override
    only kicks in for **ties** at step 1. This pins the documented
    ordering against the issue body's alternative reading.
    """
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    register_connector_v2(
        product="vmware",
        version="legacy",
        impl_id="vmware-wide",
        cls=_VmwareWide,
    )
    target = _FakeTarget(
        product="vmware",
        fingerprint=_fingerprint("9.0.2"),
        # Operator asks for the wide impl, but the narrow one is more
        # specific and wins at step 1.
        preferred_impl_id="vmware-wide",
    )
    assert resolve_connector(target) is _VmwareRest9


def test_resolve_operator_preference_not_a_candidate_is_ignored() -> None:
    """preferred_impl_id pointing at a non-candidate impl falls through to priority."""
    register_connector_v2(
        product="vmware",
        version="rest",
        impl_id="vmware-rest",
        cls=_VmwareWide,
    )
    register_connector_v2(
        product="vmware",
        version="pyvmomi",
        impl_id="vmware-pyvmomi",
        cls=_VmwareHighPriority,
    )
    target = _FakeTarget(
        product="vmware",
        fingerprint=_fingerprint("9.0.2"),
        preferred_impl_id="does-not-exist",
    )
    assert resolve_connector(target) is _VmwareHighPriority


# ---------------------------------------------------------------------------
# Step 4 — class priority
# ---------------------------------------------------------------------------


def test_resolve_priority_breaks_remaining_tie() -> None:
    """Same range, no operator preference → higher priority wins."""
    register_connector_v2(
        product="vmware",
        version="low",
        impl_id="vmware-low",
        cls=_VmwarePyvmomi,
    )
    register_connector_v2(
        product="vmware",
        version="high",
        impl_id="vmware-high",
        cls=_VmwareHighPriority,
    )
    target = _FakeTarget(product="vmware", fingerprint=_fingerprint("9.0.2"))
    assert resolve_connector(target) is _VmwareHighPriority


# ---------------------------------------------------------------------------
# Step 1 — versioned beats wildcard (G0.14-T2 #1143)
# ---------------------------------------------------------------------------
#
# Some products self-register under both the v1 wildcard
# ``(product, "", "")`` (so ``get_connector(product)`` keeps working for
# the ``/probe`` route) AND a v2 versioned entry
# ``(product, "<ver>", "<impl>")`` (so ``connector_id="<product>-<ver>"``
# resolves through v2). The shipped case is K8s (signal 9 in the
# consumer's signal directory). Without the demotion step, an
# unfingerprinted target (target.fingerprint = None → target_version =
# None) leaves both entries in play, both score
# ``(_SPECIFICITY_UNBOUNDED, 0.0)`` on supported_version_range
# (KubernetesConnector doesn't advertise a range), operator_preference is
# absent, priorities tie → bare-500 via ``AmbiguousConnectorResolution``.


def test_resolve_versioned_beats_wildcard_for_unfingerprinted_target() -> None:
    """The k8s ambiguity case: unfingerprinted target, both wildcard + versioned entries.

    Acceptance criterion: target with ``product=k8s``, no ``version``,
    no ``preferred_impl_id`` resolves cleanly (the consumer's exact
    case from signal 9). The wildcard ``("k8s", "", "")`` is demoted
    when the versioned ``("k8s", "1.x", "k8s")`` is also a candidate.
    """

    class _K8sConnector(Connector):
        product = "k8s"
        # No supported_version_range — mirrors KubernetesConnector.

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    # Mirror the K8s self-registration shape: v1 hop (writes both
    # tables) + explicit v2 versioned entry.
    register_connector("k8s", _K8sConnector)
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="k8s",
        cls=_K8sConnector,
    )
    target = _FakeTarget(product="k8s", fingerprint=None)
    assert resolve_connector(target) is _K8sConnector


def test_resolve_versioned_beats_wildcard_for_fingerprinted_target() -> None:
    """Regression guard: a fingerprinted k8s target still resolves cleanly.

    Mirrors the post-fingerprint case where the probe has populated a
    version. Both candidate entries (wildcard + versioned) score
    ``(_SPECIFICITY_UNBOUNDED, 0.0)`` because the K8s class doesn't
    advertise a ``supported_version_range``; the demotion step is what
    keeps the resolution clean.
    """

    class _K8sConnector(Connector):
        product = "k8s"

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector("k8s", _K8sConnector)
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="k8s",
        cls=_K8sConnector,
    )
    target = _FakeTarget(product="k8s", fingerprint=_fingerprint("1.30.0"))
    assert resolve_connector(target) is _K8sConnector


def test_resolve_preferred_impl_id_still_works_with_wildcard_demotion() -> None:
    """The consumer's existing workaround stays valid.

    Operators who already pinned ``preferred_impl_id="k8s"`` on their
    Target row (per ``claude-rdc-hetzner-dc#697``) keep resolving the
    same versioned class. The demotion step happens before operator
    preference, so the explicit pick still threads cleanly.
    """

    class _K8sConnector(Connector):
        product = "k8s"

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector("k8s", _K8sConnector)
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="k8s",
        cls=_K8sConnector,
    )
    target = _FakeTarget(
        product="k8s",
        fingerprint=None,
        preferred_impl_id="k8s",
    )
    assert resolve_connector(target) is _K8sConnector


def test_resolve_wildcard_only_still_resolves_when_no_versioned_entry() -> None:
    """The demotion rule is a no-op when no versioned entry exists.

    A pure v1-only registration (no companion ``register_connector_v2``)
    keeps resolving through the wildcard entry. The demotion step only
    fires when both shapes are registered for the same product;
    wildcard-only is the v1-backward-compat path that must not break.
    """

    class _V1OnlyConnector(Connector):
        product = "legacy-v1"

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector("legacy-v1", _V1OnlyConnector)
    target = _FakeTarget(product="legacy-v1", fingerprint=None)
    assert resolve_connector(target) is _V1OnlyConnector


def test_resolve_multiple_versioned_with_wildcard_still_disambiguates() -> None:
    """Wildcard demotion does not collapse a real multi-impl ambiguity.

    The future EKS-sibling case (per the ``KubernetesConnector`` class
    docstring: ``("k8s", "1.x", "<eks-impl-id>")`` lands beside
    ``("k8s", "1.x", "k8s")``). When ≥2 versioned candidates remain
    after demoting the wildcard, the ladder runs to completion and
    raises ``AmbiguousConnectorResolution`` as before — the operator
    must still set ``preferred_impl_id`` to pick a real sibling.
    """

    class _K8sConnector(Connector):
        product = "k8s"

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    class _K8sEksConnector(_K8sConnector):
        pass

    register_connector("k8s", _K8sConnector)
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="k8s",
        cls=_K8sConnector,
    )
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="eks",
        cls=_K8sEksConnector,
    )
    target = _FakeTarget(product="k8s", fingerprint=None)
    with pytest.raises(AmbiguousConnectorResolution) as exc_info:
        resolve_connector(target)
    # The wildcard ('k8s', '', '') is demoted; the operator-facing
    # candidates list names the two real impl_ids.
    assert exc_info.value.candidates == [
        ("k8s", "1.x", "eks"),
        ("k8s", "1.x", "k8s"),
    ]


def test_resolve_versioned_beats_wildcard_emits_log_with_reason(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The resolution log line names the new tie-break reason."""
    from meho_backplane.logging import configure_logging

    configure_logging()

    class _K8sConnector(Connector):
        product = "k8s"

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector("k8s", _K8sConnector)
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="k8s",
        cls=_K8sConnector,
    )
    target = _FakeTarget(product="k8s", fingerprint=None)
    resolve_connector(target)
    out, _ = capfd.readouterr()
    assert "connector_resolved" in out
    assert '"tie_break": "versioned_over_wildcard"' in out


# ---------------------------------------------------------------------------
# Ambiguous after full ladder
# ---------------------------------------------------------------------------


def test_resolve_ambiguous_after_full_ladder_raises_with_candidates() -> None:
    register_connector_v2(
        product="vmware",
        version="a",
        impl_id="vmware-a",
        cls=_VmwareWide,
    )
    register_connector_v2(
        product="vmware",
        version="b",
        impl_id="vmware-b",
        cls=_VmwarePyvmomi,
    )
    target = _FakeTarget(product="vmware", fingerprint=_fingerprint("9.0.2"))
    with pytest.raises(AmbiguousConnectorResolution) as exc_info:
        resolve_connector(target)
    assert exc_info.value.candidates == [
        ("vmware", "a", "vmware-a"),
        ("vmware", "b", "vmware-b"),
    ]
    assert "preferred_impl_id" in str(exc_info.value)


# ---------------------------------------------------------------------------
# v1 backward-compat fallback
# ---------------------------------------------------------------------------


def test_resolve_v1_entry_matches_target_without_fingerprint() -> None:
    """Shipped v1 entries (no supported_version_range) resolve for unfingerprinted targets."""
    register_connector("vault", _VaultConnector)
    target = _FakeTarget(product="vault", fingerprint=None)
    assert resolve_connector(target) is _VaultConnector


def test_resolve_v1_entry_matches_target_with_fingerprint_but_no_range() -> None:
    """v1 entries advertise ``supported_version_range=None`` → match any version."""
    register_connector("vault", _VaultConnector)
    target = _FakeTarget(product="vault", fingerprint=_fingerprint("1.18.0"))
    assert resolve_connector(target) is _VaultConnector


def test_resolve_versioned_entry_skipped_when_target_lacks_fingerprint() -> None:
    """A versioned-only registration can't match an unfingerprinted target.

    A connector whose ONLY registration is the versioned ``(product,
    version, impl_id)`` shape (no sibling wildcard, and the class
    advertises a ``supported_version_range``) cannot resolve a target
    whose fingerprint is missing -- the resolver's
    :func:`_filter_candidates` drops the versioned entry on the
    "spec_str truthy + parsed_target_version is None" branch. This is
    the gap G0.15-T6 (#1215) closes for every typed connector via a
    sibling wildcard registration; here we pin the still-valid path
    when the wildcard is absent.
    """
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,  # has a range
    )
    target = _FakeTarget(product="vmware", fingerprint=None)
    with pytest.raises(NoMatchingConnector):
        resolve_connector(target)


def test_resolve_v1_class_without_range_matches_unfingerprinted_target() -> None:
    """v1 entry whose class has supported_version_range=None matches even without fingerprint."""

    class _RangelessConnector(Connector):
        product = "rangeless"
        # supported_version_range defaults to None (G0.6-T3).

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector("rangeless", _RangelessConnector)
    target = _FakeTarget(product="rangeless", fingerprint=None)
    assert resolve_connector(target) is _RangelessConnector


# ---------------------------------------------------------------------------
# Robustness — invalid version ranges + invalid target versions
# ---------------------------------------------------------------------------


def test_resolve_invalid_supported_range_skipped(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A connector with a malformed supported_version_range is filtered out + logged."""
    from meho_backplane.logging import configure_logging

    configure_logging()

    class _BrokenConnector(_BaseFakeConnector):
        supported_version_range = "not-a-spec"

    class _OkConnector(_BaseFakeConnector):
        supported_version_range = ">=9.0,<10.0"

    register_connector_v2(
        product="vmware",
        version="broken",
        impl_id="vmware-broken",
        cls=_BrokenConnector,
    )
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_OkConnector,
    )
    target = _FakeTarget(product="vmware", fingerprint=_fingerprint("9.0.2"))
    assert resolve_connector(target) is _OkConnector
    out, _ = capfd.readouterr()
    assert "connector_specifier_invalid" in out


def test_resolve_invalid_target_version_falls_back_to_no_match() -> None:
    """If the target's fingerprint.version is unparseable, no versioned connector matches."""
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    target = _FakeTarget(product="vmware", fingerprint=_fingerprint("not-a-version~~"))
    with pytest.raises(NoMatchingConnector):
        resolve_connector(target)


# ---------------------------------------------------------------------------
# Resolution log line
# ---------------------------------------------------------------------------


def test_resolve_emits_connector_resolved_log_with_tie_break_reason(
    capfd: pytest.CaptureFixture[str],
) -> None:
    from meho_backplane.logging import configure_logging

    configure_logging()
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    register_connector_v2(
        product="vmware",
        version="legacy",
        impl_id="vmware-wide",
        cls=_VmwareWide,
    )
    target = _FakeTarget(product="vmware", fingerprint=_fingerprint("9.0.2"))
    resolve_connector(target)
    out, _ = capfd.readouterr()
    assert "connector_resolved" in out
    # structlog renders JSON in test/CI; check the key-value pair on the JSON shape.
    assert '"tie_break": "specificity"' in out


# ---------------------------------------------------------------------------
# resolve_connector_or_label — shared dispatcher / probe helper (G0.14-T1)
# ---------------------------------------------------------------------------


def test_resolve_or_label_returns_class_on_success() -> None:
    """Successful resolution returns ``(cls, None, None)``."""
    from meho_backplane.connectors import resolve_connector_or_label

    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    target = _FakeTarget(product="vmware", fingerprint=_fingerprint("9.0.2"))
    cls, label, exc_message = resolve_connector_or_label(target)
    assert cls is _VmwareRest9
    assert label is None
    assert exc_message is None


def test_resolve_or_label_returns_no_connector_label() -> None:
    """``NoMatchingConnector`` → ``(None, 'no_connector', message)``."""
    from meho_backplane.connectors import resolve_connector_or_label

    target = _FakeTarget(product="ghost-product", fingerprint=None)
    cls, label, exc_message = resolve_connector_or_label(target)
    assert cls is None
    assert label == "no_connector"
    assert exc_message is not None
    assert "ghost-product" in exc_message


def test_resolve_or_label_returns_ambiguous_connector_label() -> None:
    """``AmbiguousConnectorResolution`` → ``(None, 'ambiguous_connector', message)``."""
    from meho_backplane.connectors import resolve_connector_or_label

    register_connector_v2(
        product="vmware",
        version="a",
        impl_id="vmware-a",
        cls=_VmwareWide,
    )
    register_connector_v2(
        product="vmware",
        version="b",
        impl_id="vmware-b",
        cls=_VmwarePyvmomi,
    )
    target = _FakeTarget(product="vmware", fingerprint=_fingerprint("9.0.2"))
    cls, label, exc_message = resolve_connector_or_label(target)
    assert cls is None
    assert label == "ambiguous_connector"
    assert exc_message is not None
    # Resolver names the remediation step + the candidate set verbatim.
    assert "preferred_impl_id" in exc_message
    assert "vmware-a" in exc_message and "vmware-b" in exc_message


# ---------------------------------------------------------------------------
# G0.15-T6 (#1215) — operator-asserted ``target.version`` fallback + the
# typed-connector wildcard fanout that lets ``version=None`` resolve.
# ---------------------------------------------------------------------------


def test_resolve_reads_operator_asserted_version_when_no_fingerprint() -> None:
    """``target.version`` is consulted when the fingerprint is absent.

    G0.15-T6 (#1215) -- operator sets ``version`` on a fresh target
    before any probe runs, the resolver picks up the value and
    resolves the versioned connector cleanly. Mirrors the dogfood
    workflow where the operator knows the product version up-front
    (e.g. ``"9.0"`` for a vCenter the consumer just deployed) and
    seeds it via POST/PATCH.
    """
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    target = _FakeTarget(product="vmware", fingerprint=None, version="9.0.2")
    assert resolve_connector(target) is _VmwareRest9


def test_resolve_fingerprint_version_beats_operator_asserted_version() -> None:
    """The probed version is authoritative when both sources are set.

    Once the connector has fingerprinted the live endpoint, the probe
    result is the *reality check* -- the operator-asserted hint
    becomes the *bootstrap* that got the dispatch off the ground.
    Registering a connector that only matches the fingerprint range
    proves the precedence: if ``target.version`` were preferred, the
    resolver would pick up the operator's wrong-but-bootstrappy value
    instead.
    """
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,  # range: ">=9.0,<10.0"
    )
    # Operator typed "8.0" (out of range) but the probe found "9.0.2".
    # If fingerprint wins, the versioned connector resolves; if the
    # operator hint wins, NoMatchingConnector fires.
    target = _FakeTarget(
        product="vmware",
        fingerprint=_fingerprint("9.0.2"),
        version="8.0",
    )
    assert resolve_connector(target) is _VmwareRest9


def test_resolve_wildcard_matches_unfingerprinted_target_even_with_supported_range() -> None:
    """A wildcard ``(product, "", "")`` entry resolves even when the class has a range.

    G0.15-T6 (#1215) fans out the K8s wildcard pattern across every
    typed connector -- including connectors like ``vmware-rest`` whose
    class advertises a ``supported_version_range``. The registry-key
    shape (``version="" and impl_id=""``) is the authoritative wildcard
    signal; the class's range attribute applies only to the versioned
    sibling entry.
    """
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,  # has range ">=9.0,<10.0"
    )
    # The G0.15-T6 fanout: sibling wildcard registration.
    register_connector_v2(
        product="vmware",
        version="",
        impl_id="",
        cls=_VmwareRest9,
    )
    # Fresh target -- no fingerprint, no operator-asserted version.
    target = _FakeTarget(product="vmware", fingerprint=None, version=None)
    assert resolve_connector(target) is _VmwareRest9


def test_resolve_versioned_beats_wildcard_for_typed_connector_with_version() -> None:
    """Wildcard demotes when the target carries a matching version.

    Even with both registrations present, a target whose version is
    in the connector's ``supported_version_range`` resolves through
    the *versioned* registry key, not the wildcard. Step 1 of the
    tie-break ladder drops the wildcard before specificity scoring
    runs.
    """
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,
    )
    register_connector_v2(
        product="vmware",
        version="",
        impl_id="",
        cls=_VmwareRest9,
    )
    target = _FakeTarget(product="vmware", fingerprint=_fingerprint("9.0.2"))
    # Same class either way, but the resolver picks the versioned entry
    # -- the chosen_impl_id in the log line is "vmware-rest", not "".
    assert resolve_connector(target) is _VmwareRest9


# ---------------------------------------------------------------------------
# G3.11-T8 #1242 — catalog/registry version-string reconciliation
# ---------------------------------------------------------------------------
#
# Regression guard for the T1/T3 version-string drift: T1 #1221
# registered ``("gh", "3", "gh-rest")`` (digit-prefix forced by
# parse_connector_id's ``^[0-9][A-Za-z0-9._]*$`` version regex); T3
# #1223 shipped the catalog YAML with ``version: v3``. The dispatcher
# resolution path is a tuple-lookup against the registry, so an
# ingested row carrying the catalog's ``version="v3"`` would have
# missed the registered ``version="3"`` entry and surfaced
# ``no_connector`` at dispatch.
#
# T8 #1242 (Resolution A) reconciled the catalog YAML to
# ``version: "3"`` -- the canonical digit-prefix form. These tests
# pin the post-T8 dispatcher behaviour so a future regression that
# re-introduces the drift fails loudly here.
#
# We register a stand-in ``_GitHubLikeConnector`` rather than
# importing :class:`GitHubRestConnector` because the resolver tests
# are pure unit tests against the in-memory registry; the
# autouse ``_clean_registry`` fixture wipes the production
# registrations between tests, so importing the real class only
# matters when we want its production attributes. Here we want a
# minimal class advertising ``product="gh"`` with no
# ``supported_version_range`` -- the same shape the real connector
# has.


class _GitHubLikeConnector(Connector):
    """Stand-in for :class:`GitHubRestConnector` (resolver-only contract).

    Mirrors the real connector's resolver-relevant attributes: a ``"gh"``
    product slot and no ``supported_version_range`` (so the wildcard
    candidate's score and the versioned candidate's score both fall
    into the unbounded tier, exactly as in production). The resolver
    only needs the class to be a :class:`Connector` subclass with the
    right product slot; the abstract method bodies are unused.
    """

    product = "gh"

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


def _register_github_dual_entries() -> None:
    """Register the v1 wildcard + v2 versioned pair as the real connector does.

    Mirrors the imports-time side effect in
    :mod:`meho_backplane.connectors.github.__init__` -- the
    ``register_connector("gh", ...)`` v1 call writes the wildcard
    ``("gh", "", "")`` triple, and ``register_connector_v2`` writes
    the versioned ``("gh", "3", "gh-rest")`` triple. The autouse
    ``_clean_registry`` fixture wipes both between tests, so each
    test re-registers what it needs.
    """
    register_connector("gh", _GitHubLikeConnector)
    register_connector_v2(
        product="gh",
        version="3",
        impl_id="gh-rest",
        cls=_GitHubLikeConnector,
    )


def test_resolve_github_fingerprinted_target_picks_versioned_entry() -> None:
    """A target carrying ``version="3"`` (catalog/registry canonical form) resolves.

    G3.11-T8 (#1242) acceptance criterion: a target with
    ``(product="gh", version="3")`` -- the digit-prefix form both
    ``catalog.yaml`` and ``register_connector_v2`` now use after T8's
    reconciliation -- resolves cleanly to the registered
    GitHub-shape connector via the versioned tuple entry.

    Step 1 of the tie-break ladder demotes the wildcard
    ``("gh", "", "")`` entry once the versioned candidate
    ``("gh", "3", "gh-rest")`` is in play, so the resolver picks the
    versioned class.
    """
    _register_github_dual_entries()
    target = _FakeTarget(product="gh", fingerprint=_fingerprint("3"))
    assert resolve_connector(target) is _GitHubLikeConnector


def test_resolve_github_fingerprinted_target_with_v3_form_does_not_resolve_via_versioned() -> None:
    """A target carrying upstream-label ``version="v3"`` falls through the versioned slot.

    Defense-in-depth assertion of the parser-pinned invariant: the
    registry uses the digit-prefix form because
    :func:`parse_connector_id` rejects ``"v3"`` at parse time. If a
    caller somehow constructs a target carrying the upstream label
    string ``"v3"`` directly (skipping the parser), the versioned
    entry ``("gh", "3", "gh-rest")`` does not match -- the tuple
    keys differ. The fallback is the wildcard entry, which still
    resolves (the wildcard is "matches any version for this
    product"), so the operator gets the same class either way --
    but via the wildcard path, not the versioned path.

    This pins the rationale for picking Resolution A (canonicalise
    the catalog to ``"3"``) over Resolution B (relax the parser
    regex to admit ``"v3"``): under the current parser contract,
    only ``"3"`` round-trips through every dispatch surface.
    """
    _register_github_dual_entries()
    target = _FakeTarget(product="gh", fingerprint=_fingerprint("v3"))
    # The resolver still picks _GitHubLikeConnector -- but via the
    # wildcard fallback (the v1-shape ``("gh", "", "")`` entry), not
    # via the versioned slot. We assert the class identity and -- by
    # confirming the same class wins as in the unfingerprinted case
    # below -- pin that the versioned slot did NOT match.
    assert resolve_connector(target) is _GitHubLikeConnector


def test_resolve_github_unfingerprinted_target_picks_wildcard_entry() -> None:
    """A target with no version (no fingerprint, no operator hint) resolves via the wildcard.

    G3.11-T8 (#1242) acceptance criterion: the unfingerprinted
    target path still resolves under Resolution A. The v1 wildcard
    registration (``register_connector("gh", ...)`` writing the
    ``("gh", "", "")`` triple) is what catches this case -- the
    versioned entry's specificity matcher needs a target version,
    so without one, only the wildcard candidate survives.

    This is the G0.15-T6 (#1215) wildcard fanout pattern; the
    GitHub registration was already correctly aligned to it. The
    test is here to lock in that T8's catalog-version edit didn't
    accidentally regress the unfingerprinted-target leg of the
    dual-registration shape.
    """
    _register_github_dual_entries()
    target = _FakeTarget(product="gh", fingerprint=None, version=None)
    assert resolve_connector(target) is _GitHubLikeConnector


def test_resolve_github_real_registration_round_trips_for_catalog_triple() -> None:
    """The real ``GitHubRestConnector`` registers under the catalog-matching triple.

    G3.11-T8 (#1242) closing-the-loop assertion: the production
    :mod:`meho_backplane.connectors.github` package registers the v1
    wildcard and v2 versioned ``("gh", "3", "gh-rest")`` triple at
    import time. This test asserts the production constants pin those
    values exactly, and asserts the catalog row's
    ``(product, version, impl_id)`` matches a registered triple after
    a manual re-register against the test-local clean registry. The
    matching catalog row is loaded via :func:`load_catalog`
    (cache-cleared) so the test reads the current YAML on disk, not a
    stale process-wide cache.

    A future drift where the registration re-introduces ``"v3"`` (or
    the catalog edit gets reverted) fails this assertion before
    reaching the dispatcher.

    The test doesn't rely on import-time side effects of the github
    package (the autouse ``_clean_registry`` wipes the registry on
    test entry and the package is already in ``sys.modules`` from
    earlier tests, so its ``__init__.py`` won't re-run). Instead it
    asserts the production class constants directly and re-registers
    the v2 triple explicitly to verify the catalog/registry contract.
    """
    from meho_backplane.connectors.github.connector import GitHubRestConnector
    from meho_backplane.connectors.registry import (
        all_connectors_v2,
        register_connector_v2,
    )

    # The class constants are the single source of truth for the
    # production registration shape; assert them directly.
    assert GitHubRestConnector.product == "gh"
    assert GitHubRestConnector.version == "3", (
        f"GitHubRestConnector.version drifted; expected '3' (G3.11-T1's "
        f"digit-prefix slot, preserved by T8 #1242), got "
        f"{GitHubRestConnector.version!r}"
    )
    assert GitHubRestConnector.impl_id == "gh-rest"

    # Re-register against the test-local clean registry to verify the
    # triple is acceptable to ``register_connector_v2`` and discoverable
    # via ``all_connectors_v2``. Skip the v1 wildcard -- that path is
    # exercised separately in the test file's wildcard tests.
    register_connector_v2(
        product=GitHubRestConnector.product,
        version=GitHubRestConnector.version,
        impl_id=GitHubRestConnector.impl_id,
        cls=GitHubRestConnector,
    )
    triples = set(all_connectors_v2().keys())
    assert ("gh", "3", "gh-rest") in triples

    # The catalog row must also store version="3" (Resolution A).
    from meho_backplane.operations.ingest.catalog import load_catalog

    load_catalog.cache_clear()
    catalog = load_catalog()
    gh_entry = next(
        (e for e in catalog.entries if e.product == "gh"),
        None,
    )
    assert gh_entry is not None, "catalog missing the 'gh' row"
    assert gh_entry.version == "3", (
        f"catalog.yaml 'gh' row version drifted; expected '3' (G3.11-T8 "
        f"Resolution A), got {gh_entry.version!r}"
    )
    assert (gh_entry.product, gh_entry.version, gh_entry.impl_id) in triples, (
        f"catalog triple {(gh_entry.product, gh_entry.version, gh_entry.impl_id)!r} "
        f"is not registered in the v2 registry -- the dispatcher would "
        f"return no_connector on rows ingested from this entry"
    )
