# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Auto-register a thin :class:`HttpConnector` shim per ingested spec.

G0.7-T2 (#403) -- on the first time the spec-ingestion pipeline
encounters a ``(product, version, impl_id)`` triple, it must make
the connector resolvable through the v2 registry (G0.6-T2 #393) so
the dispatcher can route operations against the ingested rows. A
hand-coded :class:`HttpConnector` subclass for every vendor would be
the load-bearing per-G3.x deliverable (vSphere session auth, NSX
XSRF, Robot HTTP Basic, etc.), but those packages don't exist yet
when the operator runs ``meho connector ingest`` against a fresh
spec. The auto-shim bridges the gap: on first ingest, a
synthesised :class:`GenericRestConnector` subclass is registered so
the connector resolves; on subsequent ingests of additional specs
under the same connector_id, the shim is left in place; per-G3.x
work later REPLACES the auto-shim with a hand-rolled subclass.

The shim is deliberately minimal -- it inherits all transport
plumbing (client pooling, retry, timeout, cert bundle) from
:class:`HttpConnector` and overrides only the four
:class:`Connector` ABC methods. ``auth_headers`` raises
:class:`NotImplementedError` with a message pointing at the
per-G3.x override site; ``fingerprint`` / ``probe`` / ``execute``
return placeholder shapes that the operator sees in startup logs
("the dispatcher routed a call against an unconfigured auto-shim;
add the per-product subclass before enabling this connector"). The
v0.2 review-queue gate (T4 #402) keeps every ingested op in
``is_enabled=False`` / ``review_status='staged'`` until the
operator vets the connector, which is exactly when they're
supposed to add the per-product subclass; the auto-shim is never
called in practice on production paths.

Why dynamic class synthesis instead of a registry of stub
instances: the v2 resolver
(:func:`~meho_backplane.connectors.resolver.resolve_connector`)
keys on the connector *class*, not an instance, and reads its
class-level ``product`` / ``version`` / ``impl_id`` /
``supported_version_range`` / ``priority`` attributes to match
against target fingerprints. A single shared instance can't carry
per-(product, version, impl_id) class attrs simultaneously. The
:func:`type` factory is the conventional Python answer; the
resulting class behaves identically to one declared with ``class
Foo(HttpConnector):`` at module scope, modulo the absence of a
stable Python-source location for the class object (its
``__module__`` is set to this helper's module so a v2 registry
listing renders the synthesised class with a recognisable origin).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.operations.ingest.exceptions import UncoveredVersionLabel

__all__ = [
    "GenericRestConnector",
    "check_version_covered_by_registered_class",
    "derive_supported_version_range",
    "ensure_connector_class_registered",
    "handrolled_class_for_impl_id",
    "resolved_auto_shim_class",
    "sibling_handrolled_impl_id",
]

_log = structlog.get_logger(__name__)


class GenericRestConnector(HttpConnector):
    """Base for every auto-generated REST connector shim.

    Subclasses are produced dynamically by
    :func:`ensure_connector_class_registered` via :func:`type` and
    differ only in their class-level ``product`` / ``version`` /
    ``impl_id`` / ``supported_version_range`` / ``priority`` /
    ``_base_url_override`` values; the method overrides below are
    inherited verbatim.

    All four :class:`~meho_backplane.connectors.base.Connector` ABC
    methods are implemented so :func:`type` can pin them onto the
    synthesised class without an ``abstract method`` instantiation
    error. The bodies are intentionally degenerate -- the auto-shim
    is registered for resolvability only; the operator REPLACES it
    with a hand-coded subclass before enabling the connector for
    dispatch.

    ``auth_headers`` raises :class:`NotImplementedError` with a
    message pointing at the per-G3.x override site so the failure
    is loud and operator-readable. ``fingerprint`` / ``probe`` /
    ``execute`` return placeholder shapes (reachable=False with a
    "unconfigured-auto-shim" explanation) so a stray call against
    the shim doesn't crash the dispatcher mid-flight.
    """

    #: Default base URL the auto-shim uses when the target carries
    #: no explicit base URL of its own. Set by the class factory
    #: from the ``base_url`` arg to
    #: :func:`ensure_connector_class_registered`. ``None`` falls back
    #: to :meth:`HttpConnector._base_url`'s ``https://{host}{:port}``
    #: derivation from the target.
    _base_url_override: str | None = None

    def _base_url(self, target: Any) -> str:
        """Return the per-target base URL.

        Overrides :meth:`HttpConnector._base_url` when
        :attr:`_base_url_override` is set on the synthesised class
        (most ingested specs carry a server URL); falls back to the
        ``https://{host}{:port}`` derivation otherwise so the auto-
        shim still produces a valid URL for targets without an
        explicit override.
        """
        if self._base_url_override is not None:
            return self._base_url_override
        return super()._base_url(target)

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        """Raise :class:`NotImplementedError` with operator-readable guidance.

        The auto-shim doesn't know how to authenticate against the
        upstream vendor -- that's the per-G3.x deliverable. The
        review-queue gate (T4 #402) keeps every ingested op
        ``is_enabled=False`` until the operator vets the connector,
        which is exactly when they hand-roll the auth path; reaching
        this method on a production code path means either the
        review gate was bypassed or the per-G3.x subclass wasn't
        registered before the connector was enabled.
        """
        raise NotImplementedError(
            f"auto-registered shim for "
            f"({self.product!r}, {self.version!r}, {self.impl_id!r}) "
            "must be replaced with a per-product Connector subclass "
            "before dispatch is enabled -- the operator's G3.x "
            "Initiative work adds auth_headers() per target.auth_model"
        )

    async def fingerprint(
        self,
        target: Any,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Return an unreachable placeholder fingerprint.

        The auto-shim cannot probe the upstream API (no auth, no
        per-product reachability heuristic) so it reports the target
        as unreachable with a stable ``probe_method`` value the
        operator-facing CLI / API can render verbatim.

        ``operator`` exists for ABC parity (G0.16-T4 #1306) — the
        auto-shim never reaches Vault, so the route operator plays no
        role here.
        """
        del operator  # unused — placeholder fingerprint, no Vault read
        return FingerprintResult(
            vendor=self.product,
            product=self.product,
            version=self.version,
            build=None,
            reachable=False,
            probed_at=datetime.now(UTC),
            probe_method="unconfigured-auto-shim",
            extras={
                "note": (
                    "auto-registered GenericRestConnector shim -- replace with a "
                    "per-product Connector subclass before enabling dispatch"
                ),
            },
        )

    async def probe(self, target: Any) -> ProbeResult:
        """Return an unreachable placeholder probe."""
        return ProbeResult(
            ok=False,
            reason=(
                "auto-registered GenericRestConnector shim -- replace with a "
                "per-product Connector subclass before enabling dispatch"
            ),
            probed_at=datetime.now(UTC),
        )

    async def execute(
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Raise :class:`NotImplementedError` -- shim isn't dispatchable.

        The dispatcher should never reach this method in production:
        the review-queue gate (T4 #402) keeps every ingested op
        ``is_enabled=False`` until the operator replaces the shim. If
        it does, the explicit raise (rather than a placeholder
        :class:`OperationResult`) makes the misconfiguration visible
        immediately rather than silently returning a degenerate
        result the agent would misinterpret. The :class:`OperationResult`
        return-type annotation is preserved to satisfy the
        :class:`~meho_backplane.connectors.base.Connector` ABC; the
        method never actually returns a value.
        """
        raise NotImplementedError(
            f"auto-registered shim for "
            f"({self.product!r}, {self.version!r}, {self.impl_id!r}) "
            f"cannot execute op_id={op_id!r} -- replace the shim with a "
            "per-product Connector subclass before enabling dispatch"
        )


def derive_supported_version_range(version: str) -> str:
    """Derive a PEP 440 version spec from a single version string.

    Returns ``">={version},<{next_major}.0"`` when *version* parses
    as ``MAJOR.MINOR[.PATCH]`` -- the auto-shim then advertises
    compatibility with every minor / patch release in the same
    major series (the conservative default; per-G3.x subclasses
    that have tested against narrower ranges override the class
    attribute when they REPLACE the shim).

    Falls back to ``f"=={version}"`` (a single-version pin) when
    the version string doesn't parse as a numeric MAJOR.MINOR. This
    covers exotic version slugs like ``"latest"`` or ``"main"``
    that some vendor specs ship; the conservative pin avoids
    matching the shim against unrelated targets.

    The shim's ``supported_version_range`` is what the v2 resolver
    matches against a target's fingerprinted product version, so a
    too-broad range would silently route a 11.x target at a 9.x
    shim. PEP 440 ``>=X,<Y`` is the same shape every hand-rolled
    connector class uses (Vault uses ``"==1.x"``, K8s uses
    ``"==1.x"``); the auto-shim's range is no looser than the
    convention.
    """
    parts = version.split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        major = int(parts[0])
        return f">={version},<{major + 1}.0"
    return f"=={version}"


def _synthesised_class_name(product: str, version: str, impl_id: str) -> str:
    """Build a Python-identifier-safe class name from the connector triple.

    Non-alnum characters in *product* / *version* / *impl_id* are
    replaced with underscores so the result is a valid Python
    identifier even when the inputs carry dots, dashes, or other
    punctuation. The shape ``AutoShim_<product>_<version>_<impl_id>``
    matches the v2 registry's diagnostic listing convention.
    """
    sanitized = "_".join(
        "".join(ch if ch.isalnum() else "_" for ch in component)
        for component in (product, version, impl_id)
    )
    return f"AutoShim_{sanitized}"


def _synthesise_shim_class(
    *,
    product: str,
    version: str,
    impl_id: str,
    base_url: str | None,
) -> type[Connector]:
    """Synthesise a :class:`GenericRestConnector` subclass via :func:`type`.

    The class body is a static dict of class-level attributes; method
    overrides inherit from :class:`GenericRestConnector` verbatim. The
    ``__module__`` field is set so startup-log listings of the v2
    registry render the synthesised class with this helper's import
    path rather than a misleading ``<unknown>``.
    """
    cls_name = _synthesised_class_name(product, version, impl_id)
    supported_range = derive_supported_version_range(version)
    return type(
        cls_name,
        (GenericRestConnector,),
        {
            "product": product,
            "version": version,
            "impl_id": impl_id,
            "supported_version_range": supported_range,
            "priority": 0,
            "_base_url_override": base_url,
            "__module__": __name__,
            "__doc__": (
                f"Auto-registered :class:`GenericRestConnector` shim for "
                f"({product!r}, {version!r}, {impl_id!r}).\n\n"
                "Synthesised by "
                ":func:`meho_backplane.operations.ingest.connector_registration."
                "ensure_connector_class_registered` on first ingest of a spec "
                "against this connector triple. Replace with a hand-coded "
                "subclass per G3.x Initiative when adding per-product auth, "
                "topology discovery, or non-degenerate fingerprint shape."
            ),
        },
    )


def handrolled_class_for_impl_id(
    *,
    version: str,
    impl_id: str,
) -> type[Connector] | None:
    """Return a hand-rolled connector class registered for ``(version, impl_id)``.

    G0.26-T4 (#1798). Scans the v2 registry for a **hand-rolled** class
    (one that is *not* a :class:`GenericRestConnector` auto-shim)
    registered under the given ``(version, impl_id)`` regardless of the
    ``product`` it registered under. Returns the first such class, or
    ``None`` when none exists.

    This is the ingest guard's lookup: a hand-coded connector's
    ``impl_id`` is its stable, product-independent identity (vRLI is
    ``vrli-rest`` whether it registers under the dispatch-canonical
    ``product="vrli"`` or the historical ``"vcf-logs"``). An ingest that
    supplies a *divergent* product token for that same ``impl_id`` —
    ``--product vcf-logs`` for ``vrli-rest`` after the realignment, say —
    must defer to the hand-coded class rather than scaffold a
    :class:`GenericRestConnector` shim under the divergent key, which
    would be non-dispatchable and could shadow the real connector. The
    match is on ``impl_id`` (not the parser-derived product) precisely so
    a product-namespace divergence does not let the shim slip past.

    The ``GenericRestConnector`` import is function-local to keep the
    ``operations.ingest`` -> (its own) module graph free of an
    import-time edge; mirrors the pattern :func:`sibling_handrolled_impl_id`
    already uses.
    """
    from meho_backplane.connectors.registry import all_connectors_v2

    for (_entry_product, entry_version, entry_impl_id), cls in all_connectors_v2().items():
        if entry_version != version or entry_impl_id != impl_id:
            continue
        if issubclass(cls, GenericRestConnector):
            continue
        return cls
    return None


def ensure_connector_class_registered(
    *,
    product: str,
    version: str,
    impl_id: str,
    base_url: str | None,
) -> bool:
    """Register a :class:`GenericRestConnector` shim if one is absent for *triple*.

    Returns ``True`` when a new shim class was synthesised and
    registered; ``False`` when an entry already exists for the
    ``(product, version, impl_id)`` key in the v2 registry, **or** when a
    hand-coded connector already covers the ``(version, impl_id)`` under a
    divergent product token (the ingest guard — see below). The return
    value drives the ``connector_registered`` flag on
    :class:`~meho_backplane.operations.ingest.register_ingested.IngestionResult`
    so the CLI can report "first ingest registered the connector"
    vs "subsequent ingest reused the existing connector".

    Ingest guard (G0.26-T4 #1798): before synthesising a shim, defer to
    any hand-coded connector already registered for the same
    ``(version, impl_id)`` — even when that class registered under a
    *different* ``product``. Without this, ingesting a spec under a
    divergent product token (``--product vcf-logs`` for ``vrli-rest``,
    after the realignment moved ``VcfLogsConnector``
    to ``product="vrli"``) would scaffold a non-dispatchable
    ``GenericRestConnector`` shim under ``(vcf-logs, …, vrli-rest)`` that
    shadows the real connector. The guard keys on ``impl_id`` (the
    connector's product-independent identity), so a product-namespace
    divergence cannot route around it. The persisted rows still reconcile
    to the dispatch-canonical product
    (:func:`~meho_backplane.operations.ingest.register_ingested._reconciled_row_product`),
    so the ingested ops resolve through the hand-coded connector.

    Idempotency note: the v2 registry rejects duplicate
    registration with :class:`RuntimeError`, so checking presence
    first is necessary (not merely an optimisation). The check is
    racy against concurrent ingests of the same triple, but v0.2
    ingestion is single-threaded per pod (the CLI / REST handlers
    are operator-driven and serialised) so the race is theoretical.
    """
    from meho_backplane.connectors.registry import all_connectors_v2

    existing = all_connectors_v2()
    if (product, version, impl_id) in existing:
        _log.info(
            "connector_auto_register_skipped",
            product=product,
            version=version,
            impl_id=impl_id,
            existing_cls=existing[(product, version, impl_id)].__name__,
        )
        return False

    handrolled = handrolled_class_for_impl_id(version=version, impl_id=impl_id)
    if handrolled is not None and handrolled.product != product:
        _log.info(
            "connector_auto_register_deferred_to_handrolled",
            product=product,
            version=version,
            impl_id=impl_id,
            handrolled_cls=handrolled.__name__,
            handrolled_product=handrolled.product,
            message=(
                f"not scaffolding a GenericRestConnector shim for "
                f"({product!r}, {version!r}, {impl_id!r}): hand-coded "
                f"{handrolled.__name__} already covers impl_id {impl_id!r} "
                f"under product {handrolled.product!r}. Ingested rows "
                f"reconcile to the dispatch-canonical product and dispatch "
                f"through the hand-coded connector."
            ),
        )
        return False

    cls = _synthesise_shim_class(
        product=product,
        version=version,
        impl_id=impl_id,
        base_url=base_url,
    )
    register_connector_v2(
        product=product,
        version=version,
        impl_id=impl_id,
        cls=cls,
    )
    _log.info(
        "connector_auto_registered",
        product=product,
        version=version,
        impl_id=impl_id,
        cls=cls.__name__,
        supported_version_range=cls.supported_version_range,
        base_url=base_url,
    )
    return True


def sibling_handrolled_impl_id(
    *,
    product: str,
    version: str,
    exclude_impl_id: str,
) -> str | None:
    """Return a hand-rolled connector's ``impl_id`` for the same ``(product, version)``.

    G0.25-T2 (#1753). Scans the v2 registry for a **hand-rolled**
    connector class (one that is *not* a :class:`GenericRestConnector`
    auto-shim) registered under the same ``(product, version)`` as
    *exclude_impl_id* but under a **different** ``impl_id``. Returns
    that sibling's ``impl_id``, or ``None`` when no such class exists.

    Two callers want the same question answered from opposite ends of
    the auto-shim lifecycle:

    * **Ingest near-miss guard** (:func:`check_version_covered_by_registered_class`)
      — before scaffolding a shim under a one-token-off ``impl_id``
      (``nsx-rest-probe`` when ``nsx-rest`` already ships a hand-rolled
      class), warn that the new shim is non-dispatchable and may shadow
      the sibling (the resolver-tie-break footgun T1 #1750 fixes
      load-bearingly).
    * **Dispatch-time error wording** (the dispatcher's
      ``connector_unsupported`` / ``unreplaced_auto_shim`` arm) — when a
      stray shim *did* get dispatched, name the sibling that already
      works so the operator re-ingests under it instead of being told
      (misleadingly) that the per-product subclass is unwritten future
      work.

    The "hand-rolled" predicate is ``not issubclass(cls,
    GenericRestConnector)`` — the same precise ``isinstance`` test the
    dispatcher uses to classify the ``unreplaced_auto_shim`` cause
    (#1627), so a registry holding only shims (every ``impl_id`` is an
    auto-shim) yields ``None`` and no near-miss is claimed. The first
    matching sibling in registry-iteration order is returned; the
    near-miss is a single-sibling advisory, not an enumeration.

    Args:
        product, version: The label whose sibling is sought.
        exclude_impl_id: The ``impl_id`` being ingested / dispatched —
            excluded so a class does not flag itself as its own sibling.

    Returns:
        The hand-rolled sibling's ``impl_id``, or ``None``.
    """
    from meho_backplane.connectors.registry import all_connectors_v2

    for (entry_product, entry_version, entry_impl_id), cls in all_connectors_v2().items():
        if entry_product != product or entry_version != version:
            continue
        if entry_impl_id == exclude_impl_id:
            continue
        if issubclass(cls, GenericRestConnector):
            continue
        return entry_impl_id
    return None


def _find_class_covering_version(
    *,
    product: str,
    impl_id: str,
    parsed_version: Version,
) -> tuple[list[tuple[str, str, str, str]], tuple[str, str, str, str] | None]:
    """Scan the v2 registry for classes on ``(product, impl_id)`` covering *version*.

    Returns ``(candidates, accepted_by)``: every registered class for
    the ``(product, impl_id)`` pair as a
    ``(version, impl_id, class_name, range)`` tuple, plus the first one
    whose ``supported_version_range`` accepts *parsed_version* (``None``
    when none does). An empty / ``None`` range counts as "accepts any
    version" (v1-style wildcard entries). A class whose advertised range
    is itself unparseable is logged and skipped — a connector-author bug
    must not block an ingest the other classes would accept. Extracted
    from :func:`check_version_covered_by_registered_class` so the
    pre-flight stays under the size budget.
    """
    from meho_backplane.connectors.registry import all_connectors_v2

    candidates: list[tuple[str, str, str, str]] = []
    accepted_by: tuple[str, str, str, str] | None = None
    for (entry_product, entry_version, entry_impl_id), cls in all_connectors_v2().items():
        if entry_product != product or entry_impl_id != impl_id:
            continue
        spec_str = cls.supported_version_range
        candidate = (entry_version, entry_impl_id, cls.__name__, spec_str or "")
        candidates.append(candidate)
        if not spec_str:
            accepted_by = candidate
            break
        try:
            if parsed_version in SpecifierSet(spec_str):
                accepted_by = candidate
                break
        except InvalidSpecifier:
            _log.warning(
                "connector_specifier_invalid_at_ingest_preflight",
                product=entry_product,
                version=entry_version,
                impl_id=entry_impl_id,
                cls=cls.__name__,
                supported_version_range=spec_str,
            )
            continue
    return candidates, accepted_by


def _warn_no_covering_class(*, product: str, version: str, impl_id: str) -> None:
    """Log the no-registered-class outcome for ``(product, impl_id)`` and return.

    Two sub-cases (G0.25-T2 #1753), distinguished by whether a
    hand-rolled class already covers the same ``(product, version)``
    under a DIFFERENT ``impl_id``:

    * **Near-miss** — a sibling exists. Emit
      ``connector_ingest_near_miss_impl_id`` at *warning* level naming
      it: the shim about to be scaffolded is non-dispatchable and may
      shadow the working sibling at resolve time (the resolver tie-break
      T1 #1750 fixes load-bearingly). Defense-in-depth + messaging, so
      it warns rather than refuses — the ingest proceeds unchanged. The
      structured ``sibling_impl_id`` field lets an agent branch without
      re-parsing the message.
    * **Genuinely novel ``(product, version)``** — no sibling. Emit
      ``connector_ingest_orphaned_class`` at *info* level (unchanged
      v0.4-staging path); the dispatcher surfaces the gap at the first
      ``call_operation`` and the info log is the upstream signal.
    """
    sibling = sibling_handrolled_impl_id(
        product=product,
        version=version,
        exclude_impl_id=impl_id,
    )
    if sibling is not None:
        _log.warning(
            "connector_ingest_near_miss_impl_id",
            product=product,
            version=version,
            impl_id=impl_id,
            sibling_impl_id=sibling,
            message=(
                f"a connector class exists for "
                f"({product}, {version}, {sibling}); ingesting under "
                f"{impl_id} creates a non-dispatchable shim that may "
                f"shadow it -- did you mean {sibling}?"
            ),
        )
        return
    _log.info(
        "connector_ingest_orphaned_class",
        product=product,
        version=version,
        impl_id=impl_id,
    )


def check_version_covered_by_registered_class(
    *,
    product: str,
    version: str,
    impl_id: str,
) -> None:
    """Pre-flight check that the ``version`` label is dispatchable.

    G0.9-T9 (#741). The dispatch resolver
    (:func:`~meho_backplane.connectors.resolver.resolve_connector`)
    matches a target's fingerprinted version against each registered
    class's PEP 440 ``supported_version_range``; the ingest pipeline
    keys rows on a free-form ``(product, version, impl_id)`` triple.
    Without a pre-flight an operator can ingest under
    ``(vmware, "7.0", vmware-rest)`` when the only registered class
    advertises ``">=8.5,<10.0"`` — the catalog shows the ops but every
    call later fails with :exc:`NoMatchingConnector`, far from the
    ingest call site. This runs **before**
    :func:`ensure_connector_class_registered` synthesises the auto-shim
    (whose range is derived from the operator's own label, so it would
    always "match" and make the check vacuous), and filters by
    ``(product, impl_id)`` — a class at ``(vmware, "9.0", vmware-rest)``
    advertising ``">=8.5,<10.0"`` covers an ingest at
    ``(vmware, "8.5.1", vmware-rest)``.

    Three outcomes by registry state (full prose:
    ``docs/codebase/spec-ingestion.md`` §``check_version_covered_by_registered_class``):

    * **≥1 class for ``(product, impl_id)`` but none accepts the
      label** → raise :exc:`UncoveredVersionLabel` (mapped to HTTP 422),
      naming every candidate range.
    * **No class for ``(product, impl_id)`` but a hand-rolled sibling
      exists for the same ``(product, version)`` under another
      ``impl_id``** → warn ``connector_ingest_near_miss_impl_id`` and
      proceed (G0.25-T2 #1753; the near-miss footgun).
    * **No class and no sibling** → info-log
      ``connector_ingest_orphaned_class`` and proceed (v0.4-staging
      path; a genuinely novel triple is unchanged by #1753).

    Args:
        product, version, impl_id: The connector triple the operator
            submitted; matches the natural-key shape on
            :class:`~meho_backplane.db.models.EndpointDescriptor`.

    Raises:
        UncoveredVersionLabel: At least one registered class exists
            for ``(product, impl_id)`` but none accepts ``version``.
    """
    try:
        parsed_version = Version(version)
    except InvalidVersion:
        # PEP 440 cannot parse the operator's label. We cannot decide
        # range membership — log + proceed so the operator is not
        # blocked by a label-parsing quirk the resolver itself tolerates
        # (it also catches InvalidVersion and falls through). T8 (#740)
        # validates the label shape against the spec's info.version;
        # this pre-flight is specifically about range coverage.
        _log.info(
            "connector_ingest_version_unparseable",
            product=product,
            version=version,
            impl_id=impl_id,
        )
        return

    candidates, accepted_by = _find_class_covering_version(
        product=product,
        impl_id=impl_id,
        parsed_version=parsed_version,
    )

    if not candidates:
        _warn_no_covering_class(product=product, version=version, impl_id=impl_id)
        return

    if accepted_by is None:
        raise UncoveredVersionLabel(
            product=product,
            version=version,
            impl_id=impl_id,
            candidates=candidates,
        )

    _log.debug(
        "connector_ingest_version_covered",
        product=product,
        version=version,
        impl_id=impl_id,
        accepted_by_cls=accepted_by[2],
        accepted_by_range=accepted_by[3],
    )


@dataclass(frozen=True)
class _EnableTimeTarget:
    """Minimal duck-typed target for the enable-time resolver replay.

    :func:`~meho_backplane.connectors.resolver.resolve_connector` reads
    ``product`` / ``fingerprint`` / ``version`` / ``preferred_impl_id``
    off the target via ``getattr``. At enable time there is no real
    :class:`~meho_backplane.db.models.Target` row in play, so the op's
    own ``version`` label stands in for the fingerprinted version
    (``fingerprint=None`` makes the resolver fall back to
    ``target.version``) and no operator preference participates.
    """

    product: str
    version: str
    fingerprint: None = None
    preferred_impl_id: None = None


def resolved_auto_shim_class(*, product: str, version: str) -> str | None:
    """Return the auto-shim class name dispatch would resolve to, or ``None``.

    G0.23-T4 (#1630). Enable-time counterpart of the dispatch-time
    ``connector_unsupported`` / ``cause='unreplaced_auto_shim'``
    classification (G0.23-T1 #1627): ``is_enabled=True`` on an op
    whose dispatch is guaranteed to land on an unconfigured
    :class:`GenericRestConnector` shim is a dead end, and
    ``ReviewService.edit_op`` attaches an advisory naming the missing
    per-product subclass when this helper returns a class name.

    The check replays the production resolver
    (:func:`~meho_backplane.connectors.resolver.resolve_connector`)
    against a synthetic target carrying the op's ``(product,
    version)`` — the same tie-break ladder dispatch runs, so a
    hand-rolled subclass that would outrank the shim (more specific
    range, higher priority) suppresses the warning exactly when
    dispatch would route around the shim. The op's ``version`` label
    proxies for the target's fingerprinted version; per-target state
    (probe result, ``preferred_impl_id``) is unknowable at enable
    time, which is why this stays advisory. ``impl_id`` deliberately
    does not participate — the resolver routes by ``(product,
    version)`` and reads ``impl_id`` only via
    ``target.preferred_impl_id``.

    Fail-soft: resolver misses (:exc:`NoMatchingConnector`, e.g. an
    unparseable version label) and ties
    (:exc:`AmbiguousConnectorResolution`) return ``None`` — a warning
    probe must never break the enable write it decorates. Returns the
    resolved class's ``__name__`` only when it is a
    :class:`GenericRestConnector` subclass (the ``AutoShim_*`` shape).
    """
    # Call-time import mirrors ensure_connector_class_registered's
    # deferred registry import: keep the resolver edge off this
    # subpackage's module-import graph.
    from meho_backplane.connectors.resolver import (
        AmbiguousConnectorResolution,
        NoMatchingConnector,
        resolve_connector,
    )

    try:
        cls = resolve_connector(_EnableTimeTarget(product=product, version=version))
    except (NoMatchingConnector, AmbiguousConnectorResolution) as exc:
        _log.debug(
            "edit_op_auto_shim_probe_unresolved",
            product=product,
            version=version,
            reason=type(exc).__name__,
        )
        return None
    if issubclass(cls, GenericRestConnector):
        return cls.__name__
    return None
