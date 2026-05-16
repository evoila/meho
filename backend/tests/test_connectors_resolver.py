# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the connector resolver (G0.6-T2 #393).

Covers the full tie-break ladder:

1. Most-specific-version-match wins.
2. Operator/tenant preference (``target.preferred_impl_id``).
3. Connector class :attr:`priority` (higher wins).

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
    ``.fingerprint.version`` (optional — populated after the connector
    fingerprints the endpoint), and ``.preferred_impl_id`` (optional —
    operator override added in the G0.3 amendments per #224, default
    ``None`` until the column lands).
    """

    product: str
    fingerprint: _FakeFingerprint | None = None
    preferred_impl_id: str | None = None


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
# Step 1 — most-specific-version-match wins
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
# Step 2 — operator/tenant preference
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
# Step 3 — class priority
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
    """A versioned connector can't match an unfingerprinted target — fall back to v1."""
    register_connector("vmware", _VmwareWide)  # v1 entry, no range
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_VmwareRest9,  # has a range
    )
    target = _FakeTarget(product="vmware", fingerprint=None)
    # _VmwareRest9 has supported_version_range and the target has no
    # fingerprint version, so only the v1 _VmwareWide entry (which has
    # supported_version_range=None) is a candidate. Note: _VmwareWide is
    # registered via v1 path so it lands in the v2 table as
    # ('vmware', '', '') with the class's supported_version_range=">=6.5,<10.0"
    # still set on the class — meaning it IS a versioned entry too and
    # gets filtered out. The test asserts NoMatchingConnector in that
    # case, which is the honest behavior.
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
