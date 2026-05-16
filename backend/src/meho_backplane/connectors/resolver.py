# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector resolver — pick the best implementation for a target.

G0.6-T2 (#393). The dispatcher (G0.6-T5, #396) calls
:func:`resolve_connector` to map a :class:`~meho_backplane.db.models.Target`
to a concrete connector class given the connectors registered via
:func:`~meho_backplane.connectors.registry.register_connector_v2` (or the
shipped v1 entry point).

Resolution input
================

The resolver reads two attributes from ``target``:

* ``target.product`` — product slug (matches ``Connector.product``).
* ``target.fingerprint`` → ``version`` — version string from the most
  recent probe of this target (e.g. ``"9.0.2"``). In production
  ``target.fingerprint`` is a JSON **dict** (the probe route persists
  ``FingerprintResult.model_dump(mode="json")``); duck-typed test
  targets may instead expose an object with ``.version``. The resolver
  reads both shapes. Optional — when unknown, the resolver falls back
  to v1-style entries (entries registered with ``version=""``).

A third, optional attribute participates in the tie-break:

* ``target.preferred_impl_id`` — operator/tenant override pinning a
  specific implementation when multiple connectors advertise support
  for the target's ``(product, version)``. Per the G0.6 Initiative
  scope, this column is added to the Target model by the G0.3
  amendments (#224); the resolver tolerates its absence today by
  reading via ``getattr(..., None)``.

Tie-break ladder
================

When two or more connectors advertise support for a target's
``(product, version)``:

1. **Most-specific-version-match wins.** A connector with
   ``supported_version_range=">=9.0,<10.0"`` (span = 1.0 minor versions)
   beats ``">=6.5,<10.0"`` (span = 3.5 minor versions) for a target with
   ``version="9.0.2"``. Specificity is measured by the size of the
   bounded interval covered by the SpecifierSet — smaller = more
   specific. A bounded range (both lower and upper) is always more
   specific than a half-bounded one; a half-bounded range is more
   specific than an unbounded one (``None`` / no range).

2. **Operator/tenant preference.** When specificity ties, the
   ``target.preferred_impl_id`` (if set) selects the matching
   implementation. Operators set this on the Target row to break ties
   that the version-range ladder cannot resolve (e.g. two vendors
   both advertising the same range for the same product).

3. **Connector class priority.** When operator preference doesn't
   disambiguate (not set, or the preferred impl isn't a candidate),
   the integer :attr:`Connector.priority` class attribute breaks the
   tie — higher wins.

If after all three steps two or more candidates remain, the resolver
raises :exc:`AmbiguousConnectorResolution` listing the candidates so the
operator can set ``preferred_impl_id`` to pick one.

Zero candidates → :exc:`NoMatchingConnector`.

Why ``packaging.specifiers``
============================

The :mod:`packaging` library ships PEP 440-compliant version + specifier
parsing — the same code paths pip + setuptools use. Reusing it means
``Connector.supported_version_range`` strings parse identically to
``Requires-Dist`` strings in any Python project metadata. The library
is already installed in the backplane via transitive dependencies
(``pip``, ``setuptools``); G0.6-T2 promotes it to a direct dependency
to make the binding visible in ``pyproject.toml``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import all_connectors_v2

__all__ = [
    "AmbiguousConnectorResolution",
    "NoMatchingConnector",
    "resolve_connector",
]

_log = structlog.get_logger(__name__)


# N818: the Error-suffix naming convention is intentionally violated —
# the acceptance criteria on the parent task (#393) name these exceptions
# `NoMatchingConnector` and `AmbiguousConnectorResolution` verbatim; the
# names ship across the spec, the docs, and the operator-facing error
# messages and shouldn't drift.
class NoMatchingConnector(LookupError):  # noqa: N818
    """No registered connector advertises support for the target."""


class AmbiguousConnectorResolution(LookupError):  # noqa: N818
    """Multiple registered connectors remain after the full tie-break ladder.

    The exception carries a sorted list of ``(product, version, impl_id)``
    tuples in :attr:`candidates` so the operator can set
    ``target.preferred_impl_id`` to one of them.
    """

    def __init__(self, message: str, candidates: list[tuple[str, str, str]]) -> None:
        super().__init__(message)
        self.candidates = candidates


# ---------------------------------------------------------------------------
# Internal candidate record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """A v2 registry entry that matches a target's (product, version).

    Held by the resolver during ranking; never leaks to callers (callers
    get a :class:`Connector` class or one of the two LookupError
    subclasses).
    """

    product: str
    version: str
    impl_id: str
    cls: type[Connector]
    # Specificity score; lower = more specific. Computed from
    # supported_version_range. See _specificity_score for the ladder.
    specificity_score: tuple[int, float]


# Sentinel specificity scores. The tuple ordering makes "smaller is more
# specific" sort naturally: (rank, span) with rank 0 = both bounds, 1 =
# one bound, 2 = no bound / unparseable.
#
# - (0, span)  — bounded range; span is the (upper - lower) distance.
# - (1, 0.0)   — one bound only (>= or < without the other side).
# - (2, 0.0)   — no range advertised (``supported_version_range is None``)
#                or unparseable. Treated as "matches anything"; least
#                specific.
_SPECIFICITY_BOUNDED = 0
_SPECIFICITY_HALF_BOUNDED = 1
_SPECIFICITY_UNBOUNDED = 2


def _specificity_score(spec_str: str | None) -> tuple[int, float]:
    """Compute a (rank, span) specificity score for a SpecifierSet string.

    Smaller score = more specific:

    * ``">=9.0,<10.0"`` → ``(0, 1.0)`` — bounded; span = 1.0
    * ``">=6.5,<10.0"`` → ``(0, 3.5)`` — bounded; span = 3.5
    * ``">=9.0"``       → ``(1, 0.0)`` — half-bounded
    * ``None`` / ``""`` → ``(2, 0.0)`` — unbounded / unparseable

    Spans are computed from the ``release`` tuple of each bound parsed as
    a :class:`packaging.version.Version`. The release tuple is converted
    to a float by treating it as a major.minor-style decimal (joining
    the first two release components with ``.``). For semver-typed
    products this matches operator intuition: ``>=9.0,<10.0`` spans
    1.0 minor versions, ``>=6.5,<10.0`` spans 3.5. Non-semver products
    (e.g. ``"5.0.0"`` with a fourth release component) still produce a
    deterministic, monotonic float; only the relative ordering matters
    for tie-break.
    """
    if not spec_str:
        return (_SPECIFICITY_UNBOUNDED, 0.0)
    try:
        spec = SpecifierSet(spec_str)
    except InvalidSpecifier:
        # Treat unparseable as "matches anything" rather than raising —
        # the connector still advertises support (we got here because it
        # matched on product + the SpecifierSet contains call below
        # decided membership), and a tie-break shouldn't blow up on a
        # cosmetic typo. The structured log line at registration time is
        # the right place to surface the typo.
        return (_SPECIFICITY_UNBOUNDED, 0.0)

    lower = _extract_lower_bound(spec)
    upper = _extract_upper_bound(spec)

    if lower is not None and upper is not None:
        return (_SPECIFICITY_BOUNDED, _version_to_float(upper) - _version_to_float(lower))
    if lower is not None or upper is not None:
        return (_SPECIFICITY_HALF_BOUNDED, 0.0)
    return (_SPECIFICITY_UNBOUNDED, 0.0)


def _extract_lower_bound(spec: SpecifierSet) -> Version | None:
    """Pick the lower bound from a SpecifierSet (``>=`` or ``>`` operator)."""
    for s in spec:
        if s.operator in (">=", ">", "=="):
            try:
                return Version(s.version)
            except InvalidVersion:
                return None
    return None


def _extract_upper_bound(spec: SpecifierSet) -> Version | None:
    """Pick the upper bound from a SpecifierSet (``<=`` or ``<`` operator)."""
    for s in spec:
        if s.operator in ("<=", "<"):
            try:
                return Version(s.version)
            except InvalidVersion:
                return None
    return None


def _version_to_float(v: Version) -> float:
    """Convert a :class:`Version` to a decimal float for span arithmetic.

    Uses the first two ``release`` components as ``major.minor``. Trailing
    components are ignored — they don't affect the relative ordering
    needed for tie-break specificity scoring (only the magnitude of the
    difference matters, and the major+minor pair captures it for every
    versioning scheme observed in target products).
    """
    release = v.release
    if not release:
        return 0.0
    if len(release) == 1:
        return float(release[0])
    return float(release[0]) + float(release[1]) / 10.0


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


def resolve_connector(target: Any) -> type[Connector]:
    """Resolve a :class:`Target` to the best registered connector class.

    Reads ``target.product`` and (when available)
    ``target.fingerprint.version`` to build the candidate list, then
    applies the tie-break ladder documented in the module docstring.

    Returns the connector **class** — the dispatcher instantiates it.
    Tests can supply a duck-typed target with ``.product`` and a
    ``.fingerprint`` exposing ``.version`` (or ``None``).

    Raises
    ------
    NoMatchingConnector
        Zero candidates after filtering.
    AmbiguousConnectorResolution
        Two or more candidates remain after the full tie-break ladder.
    """
    product = getattr(target, "product", None)
    if not isinstance(product, str) or not product:
        raise NoMatchingConnector(
            f"target has no product slug; cannot resolve a connector: target={target!r}"
        )

    target_version = _resolve_target_version(target)
    preferred_impl_id = getattr(target, "preferred_impl_id", None)

    candidates = _filter_candidates(product, target_version)

    if not candidates:
        raise NoMatchingConnector(
            f"no connector advertises support for (product={product!r}, version={target_version!r})"
        )

    winner, reason, remaining = _run_tie_break_ladder(candidates, preferred_impl_id)
    if winner is not None:
        return _select(winner, product, target_version, reason)

    keys = sorted((c.product, c.version, c.impl_id) for c in remaining)
    raise AmbiguousConnectorResolution(
        f"resolution ambiguous after tie-break ladder for "
        f"(product={product!r}, version={target_version!r}); "
        f"candidates={keys}; set target.preferred_impl_id to one of them",
        candidates=keys,
    )


def _run_tie_break_ladder(
    candidates: list[_Candidate],
    preferred_impl_id: str | None,
) -> tuple[_Candidate | None, str, list[_Candidate]]:
    """Apply the three-step tie-break ladder to a non-empty candidate list.

    Returns ``(winner, reason, remaining)``:

    * ``winner`` — the single chosen candidate, or ``None`` if the ladder
      ended with two or more candidates still tied.
    * ``reason`` — the step that picked the winner (``"specificity"`` /
      ``"operator_preference"`` / ``"priority"``) or ``"ambiguous"``.
    * ``remaining`` — the post-ladder candidate list. When ``winner`` is
      ``None`` the caller raises ``AmbiguousConnectorResolution`` with
      these as the candidates the operator must disambiguate.
    """
    # Step 1 — most-specific-version-match.
    best_score = min(c.specificity_score for c in candidates)
    candidates = [c for c in candidates if c.specificity_score == best_score]
    if len(candidates) == 1:
        return candidates[0], "specificity", candidates

    # Step 2 — operator/tenant preference. Falls through to priority when
    # the override doesn't disambiguate (zero matches → ignored; multiple
    # matches → corner case where two impls share the preferred id, so
    # let priority break it rather than raising).
    if preferred_impl_id:
        preferred = [c for c in candidates if c.impl_id == preferred_impl_id]
        if len(preferred) == 1:
            return preferred[0], "operator_preference", preferred
        if len(preferred) > 1:
            candidates = preferred

    # Step 3 — connector class priority (higher wins).
    best_priority = max(c.cls.priority for c in candidates)
    candidates = [c for c in candidates if c.cls.priority == best_priority]
    if len(candidates) == 1:
        return candidates[0], "priority", candidates

    return None, "ambiguous", candidates


def _resolve_target_version(target: Any) -> str | None:
    """Pull the target's fingerprinted version, tolerating absence.

    ``target.fingerprint`` is a JSON **dict** in production: the probe
    route persists ``FingerprintResult.model_dump(mode="json")`` to the
    ``Target.fingerprint`` column, so the ORM hands the resolver a
    ``Mapping``, not an object. Tests (and the module docstring's
    duck-typed contract) may instead supply an object exposing
    ``.version``. Read both shapes — dict via key access, object via
    attribute access — so a real probed target resolves the same way a
    duck-typed test target does. Reading only the attribute form (the
    prior behaviour) made every versioned connector unresolvable for
    every real target, since ``getattr(dict, "version", None)`` is
    always ``None``.
    """
    fp = getattr(target, "fingerprint", None)
    if fp is None:
        return None
    version = fp.get("version") if isinstance(fp, Mapping) else getattr(fp, "version", None)
    if not isinstance(version, str) or not version:
        return None
    return version


def _filter_candidates(product: str, target_version: str | None) -> list[_Candidate]:
    """Enumerate v2 registry entries matching ``product`` and target version."""
    out: list[_Candidate] = []
    parsed_target_version: Version | None
    if target_version is not None:
        try:
            parsed_target_version = Version(target_version)
        except InvalidVersion:
            parsed_target_version = None
    else:
        parsed_target_version = None

    for key, cls in all_connectors_v2().items():
        entry_product, entry_version, entry_impl_id = key
        if entry_product != product:
            continue

        spec_str = cls.supported_version_range
        if spec_str:
            # Versioned advertisement — the connector must accept the
            # target's version. If the target version is unknown
            # (fingerprint missing), a versioned connector cannot match;
            # only the v1-style entries (no supported_version_range) are
            # eligible.
            if parsed_target_version is None:
                continue
            try:
                spec = SpecifierSet(spec_str)
            except InvalidSpecifier:
                # Unparseable advertisement — log and skip rather than
                # crashing resolution. The connector author has a bug to
                # fix; the resolver shouldn't be the surface that
                # surfaces it.
                _log.warning(
                    "connector_specifier_invalid",
                    product=entry_product,
                    version=entry_version,
                    impl_id=entry_impl_id,
                    cls=cls.__name__,
                    supported_version_range=spec_str,
                )
                continue
            if parsed_target_version not in spec:
                continue

        out.append(
            _Candidate(
                product=entry_product,
                version=entry_version,
                impl_id=entry_impl_id,
                cls=cls,
                specificity_score=_specificity_score(spec_str),
            )
        )
    return out


def _select(
    candidate: _Candidate,
    product: str,
    target_version: str | None,
    reason: str,
) -> type[Connector]:
    """Emit a resolution log line and return the chosen class."""
    _log.info(
        "connector_resolved",
        product=product,
        target_version=target_version,
        chosen_version=candidate.version,
        chosen_impl_id=candidate.impl_id,
        cls=candidate.cls.__name__,
        tie_break=reason,
    )
    return candidate.cls
