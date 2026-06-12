# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tier-2 Microsoft Presidio NER adapter -- Initiative #805, Task #1072.

Free-text fields (error strings, descriptions, log lines) leak PII
that Tier-1 regex (:mod:`.engine`) cannot catch -- person names have
no regex shape, and an IP / FQDN buried in prose is wrapped in
punctuation the Tier-1 ``\\b`` anchors miss. The Microsoft Presidio
project ships an :class:`AnalyzerEngine` (spaCy-backed NER + bundled
recognisers) + :class:`AnonymizerEngine` (entity-span rewriter) that
covers the same shape -- this module is the thin adapter wiring it
into the redaction middleware's manifest contract.

**Capability-flagged.** Presidio is a heavyweight dependency (one
spaCy model load is ~560 MB on disk for ``en_core_web_lg``,
single-digit-millisecond per-call NER inference on short strings,
hundreds of milliseconds on cold model load). A
:class:`~meho_backplane.redaction.policy.RedactionPolicy` carrying no
``tier2`` block never imports ``presidio_analyzer`` or
``presidio_anonymizer`` -- :func:`apply_tier2` short-circuits before
the import chain runs. Operators pay for what they opt into.

**Lazy + cached engine.** :func:`get_engines` does the import + model
provisioning on first call and caches the result for the process
lifetime. The cache is keyed on the requested language so a policy
asking for ``"en"`` and another asking for ``"de"`` get distinct
engines -- spaCy models are language-specific. Cold-load failure
(missing model, presidio import error) raises
:class:`Tier2NotAvailableError`; the middleware catches it and
records the failure on the audit row so the dispatch still returns
a structured ``connector_error`` rather than crashing.

**Field-path matching.** Tier-2 only fires on leaves whose dotted
path matches one of a rule's :attr:`~Tier2Rule.fields` globs. The
globs support ``*`` (any single segment) and ``**`` (any depth)
metacharacters; everything else is literal. The matcher is
allocation-bounded -- one regex compile per glob per rule, cached on
the rule's lifetime via :func:`functools.lru_cache`.

**Manifest contract.** Tier-2 emits the same
:class:`RedactionManifestEntry` shape as Tier-1, with two
distinguishing fields:

* ``rule`` -- the firing :class:`Tier2Rule.name`.
* ``pattern`` -- ``f"presidio:{entity_type}"`` (e.g.
  ``"presidio:PERSON"``). The ``presidio:`` prefix lets audit
  consumers bin Tier-1 vs Tier-2 firings without reading the rule
  name.

Multiple Presidio matches on the same leaf collapse into one
manifest entry per ``(rule, entity_type)`` pair (matching Tier-1's
collapsing rule); the ``count`` field tracks how many matches the
rule resolved.

Replay determinism: Presidio's analyser is not strictly
deterministic across spaCy version changes (NER model weights can
shift on a minor bump). The :class:`Tier2NotAvailableError` is also
raised when the spaCy model the policy requests is not installed on
the host -- the C1-d round-trip CI gate (#1073) treats both as the
"model drift" signal it needs to flag.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.redaction.engine import RedactionManifestEntry
from meho_backplane.redaction.path_glob import path_matches
from meho_backplane.redaction.policy import (
    PRESIDIO_SUPPORTED_ENTITIES,
    RedactionPolicy,
    Tier2Rule,
)

if TYPE_CHECKING:
    # Type-only imports keep the module import-safe when Presidio
    # is not yet installed (e.g. during a partial CI environment).
    # The runtime imports happen inside :func:`get_engines`, which is
    # only called when a policy carries a tier2 block.
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine

__all__ = [
    "DEFAULT_SPACY_MODEL",
    "SPACY_MODEL_ENV_VAR",
    "Tier2EnginePair",
    "Tier2NotAvailableError",
    "Tier2Result",
    "apply_tier2",
    "clear_engine_cache",
    "get_engines",
    "policy_uses_tier2",
]


#: spaCy model the AnalyzerEngine loads when no override is provided.
#: ``en_core_web_lg`` is the Microsoft Presidio default -- the
#: documentation pins it as the recommended English model for
#: production NER quality. CI provisions it via ``python -m spacy
#: download en_core_web_lg`` (see ``.github/workflows/ci.yml``); the
#: model resolves through spaCy's pip-installed package data, so a
#: deployed wheel inherits whatever ``en_core_web_lg`` version the
#: install pinned.
DEFAULT_SPACY_MODEL: Final[str] = "en_core_web_lg"

#: Environment-variable override for the spaCy model. The default
#: (:data:`DEFAULT_SPACY_MODEL` = ``en_core_web_lg``) matches the
#: Presidio documentation; deployments that need a smaller footprint
#: (sandboxes, fast-CI lanes, dev laptops) can set
#: ``MEHO_REDACTION_SPACY_MODEL=en_core_web_sm`` to swap in spaCy's
#: 12 MB small model with reduced NER quality. The model name is a
#: deployment concern, not a policy concern -- pinning it on the
#: policy would couple operator-authored YAML to host-installed
#: state.
SPACY_MODEL_ENV_VAR: Final[str] = "MEHO_REDACTION_SPACY_MODEL"


def _resolved_spacy_model() -> str:
    """Read the spaCy-model override env var (or default).

    Read on every :func:`get_engines` cache miss so an operator can
    swap the model out by restarting the process (the cache is keyed
    on language, so the swap takes effect immediately on first cold
    load after the restart). Within a single process the cached
    engines hold the original model -- :func:`clear_engine_cache`
    forces a rebuild if a test needs to swap mid-process.
    """
    override = os.environ.get(SPACY_MODEL_ENV_VAR)
    if override is None or not override.strip():
        return DEFAULT_SPACY_MODEL
    return override.strip()


class Tier2NotAvailableError(RuntimeError):
    """Raised when Presidio + the requested spaCy model cannot load.

    The middleware (:mod:`.middleware`) catches this and emits a
    structured ``connector_error`` on the audit row, so a Tier-2
    misconfiguration never leaks raw payloads through a 500 with no
    audit record. Operators see the underlying reason (missing
    package, model not downloaded, version-mismatch) in the audit
    row's ``payload.error`` field.

    Mirrors the
    :class:`~meho_backplane.redaction.policy.RedactionPolicyError`
    posture: callers get one remediation-bearing exception type
    rather than reasoning about ``ImportError`` / ``OSError`` /
    Presidio's own internal errors separately.
    """


@dataclass(frozen=True)
class Tier2EnginePair:
    """Cached pair of Presidio engines for one language."""

    analyzer: AnalyzerEngine
    anonymizer: AnonymizerEngine


@dataclass(frozen=True)
class Tier2Result:
    """Adapter return value -- mirrors
    :class:`~meho_backplane.redaction.engine.RedactionResult`.

    ``redacted`` is the payload with Tier-2 rewrites applied; same
    nesting as the input, only string leaves at matched paths change.
    ``manifest`` is the Tier-2 manifest entries (one per firing per
    leaf per ``(rule, entity_type)`` pair); the middleware appends
    these to the Tier-1 manifest before persisting.
    """

    redacted: object
    manifest: tuple[RedactionManifestEntry, ...]


_engines: dict[str, Tier2EnginePair] = {}
_engines_lock: threading.Lock = threading.Lock()


def policy_uses_tier2(policy: RedactionPolicy) -> bool:
    """Cheap check: does *policy* carry a non-empty ``tier2`` block?

    Hot-path predicate the middleware calls before any Presidio
    import -- if it returns ``False`` the Tier-2 lane is skipped
    entirely (no import, no engine warm-up, zero cost). This is the
    load-bearing guarantee of the "capability-flagged" contract:
    Tier-1-only policies see no Presidio overhead.
    """
    return bool(policy.tier2)


def get_engines(language: str = "en") -> Tier2EnginePair:
    """Return a process-cached :class:`Tier2EnginePair` for *language*.

    First call per language:

    1. Imports ``presidio_analyzer`` / ``presidio_anonymizer`` --
       deferred so a Tier-1-only deployment never pays the import
       cost.
    2. Constructs an :class:`NlpEngineProvider` with the requested
       spaCy model (:data:`DEFAULT_SPACY_MODEL` for English).
    3. Constructs :class:`AnalyzerEngine` + :class:`AnonymizerEngine`,
       caches both behind ``_engines_lock``.

    Subsequent calls return the cached pair directly without touching
    the lock on the fast path (we double-check the cache after the
    lock acquire, mirroring the double-checked-locking idiom).

    Raises :class:`Tier2NotAvailableError` (with the underlying
    cause chained) when:

    * ``presidio_analyzer`` or ``presidio_anonymizer`` is not
      installed,
    * the requested spaCy model is not provisioned on the host, or
    * Presidio's own constructor raises (mismatched dependency
      versions, broken model artefact, etc.).

    The exception type is the only failure shape the middleware
    handles; everything else propagates as a programmer bug.
    """
    cached = _engines.get(language)
    if cached is not None:
        return cached
    with _engines_lock:
        cached = _engines.get(language)
        if cached is not None:
            return cached
        try:
            # Imports are deliberately scoped to this function so a
            # Tier-1-only policy can run the middleware without
            # paying the Presidio import + spaCy model load.
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider
            from presidio_anonymizer import AnonymizerEngine
        except ImportError as exc:  # pragma: no cover -- exercised only when dep missing
            raise Tier2NotAvailableError(
                "presidio-analyzer / presidio-anonymizer are not installed; "
                "Tier-2 redaction is unavailable",
            ) from exc

        model_name = _resolved_spacy_model()
        try:
            provider = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [
                        {
                            "lang_code": language,
                            "model_name": model_name,
                        },
                    ],
                },
            )
            nlp_engine = provider.create_engine()
            analyzer = AnalyzerEngine(
                nlp_engine=nlp_engine,
                supported_languages=[language],
            )
            anonymizer = AnonymizerEngine()
        except Exception as exc:
            raise Tier2NotAvailableError(
                f"Failed to construct Presidio engines for language {language!r} "
                f"with spaCy model {model_name!r}: {exc}",
            ) from exc

        pair = Tier2EnginePair(analyzer=analyzer, anonymizer=anonymizer)
        _engines[language] = pair
        return pair


def clear_engine_cache() -> None:
    """Drop the cached Presidio engines; the next :func:`get_engines`
    call rebuilds them.

    Test-only API. Production callers do not invalidate the cache
    mid-process; the engines are immutable across the lifetime of a
    deployment. Tests that monkey-patch the engine factory call this
    in their teardown so the patched factory does not leak into the
    next test.
    """
    with _engines_lock:
        _engines.clear()


def apply_tier2(
    payload: object,
    policy: RedactionPolicy,
    *,
    connector_id: str | None = None,
    tenant: str | None = None,
    op: str | None = None,
) -> Tier2Result:
    """Apply *policy*'s ``tier2`` block to *payload*.

    Returns the same shape Tier-1 does: a payload with Tier-2 rewrites
    applied at matched paths, plus a tuple of manifest entries. The
    middleware merges these with the Tier-1 manifest before persisting.

    Walking strategy mirrors :func:`~.engine.redact`: nested dict /
    list / str payloads are traversed in-order, but Tier-2 only
    rewrites a string leaf when (a) at least one rule's ``fields``
    glob matches the leaf's dotted path and (b) at least one of the
    rule's :attr:`~Tier2Rule.entities` resolves at or above its
    ``threshold``. Non-matching leaves are passed through verbatim --
    Tier-2 is *additive* on top of whatever Tier-1 already did.

    Engine construction is lazy (:func:`get_engines`); if the policy
    carries no ``tier2`` block this function short-circuits and never
    triggers a Presidio import.

    Raises :class:`Tier2NotAvailableError` (propagated from
    :func:`get_engines`) when the engines cannot be constructed. The
    middleware catches this; callers below it never see it.
    """
    if not policy_uses_tier2(policy):
        return Tier2Result(redacted=payload, manifest=())

    applicable_rules = tuple(
        rule
        for rule in policy.tier2
        if rule.scope.matches(connector_id=connector_id, tenant=tenant, op=op)
    )
    if not applicable_rules:
        return Tier2Result(redacted=payload, manifest=())

    # Group rules by language so we construct each engine pair at most
    # once. In practice every rule defaults to ``"en"`` so this is a
    # single-key dict, but the grouping is the right shape if a future
    # multi-language policy lands.
    by_language: dict[str, list[Tier2Rule]] = {}
    for rule in applicable_rules:
        by_language.setdefault(rule.language, []).append(rule)

    # Warm engines up-front so a ``Tier2NotAvailableError`` raises
    # before we mutate the payload (no partial-redaction state).
    engines_by_language: dict[str, Tier2EnginePair] = {
        language: get_engines(language) for language in by_language
    }

    manifest: list[RedactionManifestEntry] = []
    current = payload
    for rule in applicable_rules:
        engines = engines_by_language[rule.language]
        current = _walk_and_apply(current, rule, engines, manifest, path="")
    return Tier2Result(redacted=current, manifest=tuple(manifest))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _walk_and_apply(
    node: object,
    rule: Tier2Rule,
    engines: Tier2EnginePair,
    manifest: list[RedactionManifestEntry],
    *,
    path: str,
) -> object:
    """Recurse through *node*, applying *rule* at every matched leaf."""
    if isinstance(node, str):
        if path_matches(rule.fields, path):
            return _apply_to_str(node, rule, engines, manifest, path=path)
        return node
    if isinstance(node, Mapping):
        return {
            key: _walk_and_apply(
                value,
                rule,
                engines,
                manifest,
                path=_join_path(path, str(key)),
            )
            for key, value in node.items()
        }
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        return [
            _walk_and_apply(
                item,
                rule,
                engines,
                manifest,
                path=_join_path(path, str(index)),
            )
            for index, item in enumerate(node)
        ]
    return node


def _apply_to_str(
    leaf: str,
    rule: Tier2Rule,
    engines: Tier2EnginePair,
    manifest: list[RedactionManifestEntry],
    *,
    path: str,
) -> str:
    """Run analyser + anonymiser; emit manifest entries."""
    results = engines.analyzer.analyze(
        text=leaf,
        entities=list(rule.entities),
        language=rule.language,
    )
    filtered = [r for r in results if r.score >= rule.threshold]
    if not filtered:
        return leaf

    operators = _build_operators(rule)
    engine_result = engines.anonymizer.anonymize(
        text=leaf,
        analyzer_results=filtered,
        operators=operators,
    )

    # Manifest collapsing: one entry per (rule, entity_type) per leaf,
    # mirroring Tier-1's "one entry per leaf per rule" rule. The
    # ``count`` field tracks how many matches collapsed into the entry;
    # ``span`` is the first match (analyser results are sorted by
    # start offset).
    counts_by_entity: dict[str, int] = {}
    first_span_by_entity: dict[str, tuple[int, int]] = {}
    for result in filtered:
        counts_by_entity[result.entity_type] = counts_by_entity.get(result.entity_type, 0) + 1
        if result.entity_type not in first_span_by_entity:
            first_span_by_entity[result.entity_type] = (result.start, result.end)
    for entity_type, count in counts_by_entity.items():
        start, end = first_span_by_entity[entity_type]
        manifest.append(
            RedactionManifestEntry(
                rule=rule.name,
                pattern=f"presidio:{entity_type}",
                action=rule.action,
                count=count,
                span=(start, end),
                reason=rule.reason,
                path=path,
            ),
        )

    # Presidio's EngineResult.text carries the rewritten string.
    return str(engine_result.text)


def _build_operators(rule: Tier2Rule) -> dict[str, Any]:
    """Translate *rule*'s action to a Presidio operator config map.

    Presidio's anonymiser is operator-driven: a single ``"DEFAULT"``
    entry applies the same operator to every entity unless overridden
    by entity-type-keyed entries. We use ``"DEFAULT"`` because every
    entity within a single :class:`Tier2Rule` shares the same action
    (the rule is the unit of redaction policy, not the entity).

    Action mapping:

    * ``redact`` -> :class:`OperatorConfig("replace", ...)` with
      ``new_value=f"[REDACTED:presidio:{entity_type}]"``. We use
      ``replace`` rather than the bare ``redact`` operator because
      ``redact`` strips the entity completely (zero-width
      replacement), which collapses surrounding whitespace and
      shifts every downstream offset. The fixed marker preserves
      string-length stability for replay diffing.
    * ``mask`` -> :class:`OperatorConfig("mask", ...)` with
      ``masking_char="*"``, ``chars_to_mask=<len>``,
      ``from_end=False``. Length-preserving like Tier-1's mask, but
      Presidio's built-in operator handles the per-entity span.
    * ``hash`` -> :class:`OperatorConfig("hash", ...)` with
      ``hash_type="sha256"``. Maps to a stable hex digest the audit
      replay can compare across calls.
    """
    from presidio_anonymizer.entities import OperatorConfig

    if rule.action == "redact":
        # ``lambda params: ...`` would be the cleanest way to vary
        # the marker by entity, but ``OperatorConfig.params`` only
        # accepts a static dict. Presidio interpolates ``<TYPE>`` in
        # the operator's default behaviour, but we want the explicit
        # ``[REDACTED:presidio:<TYPE>]`` shape for parity with Tier-1.
        # The trick: use ``replace`` with ``new_value`` set per-call
        # via the ``"DEFAULT"`` entry plus per-entity overrides. We
        # generate a per-entity OperatorConfig so each entity gets
        # its own marker.
        return {
            entity: OperatorConfig(
                "replace",
                {"new_value": f"[REDACTED:presidio:{entity}]"},
            )
            for entity in PRESIDIO_SUPPORTED_ENTITIES
        }
    if rule.action == "mask":
        return {
            "DEFAULT": OperatorConfig(
                "mask",
                {
                    "masking_char": "*",
                    # Presidio caps at entity-span length when
                    # ``chars_to_mask`` exceeds it; passing a large
                    # number is the documented idiom for
                    # "mask the whole span".
                    "chars_to_mask": _LARGE_MASK_LENGTH,
                    "from_end": False,
                },
            ),
        }
    if rule.action == "hash":
        return {
            "DEFAULT": OperatorConfig(
                "hash",
                {"hash_type": "sha256"},
            ),
        }
    # The Literal union on RedactionAction excludes any other value
    # at the schema layer; the catch-all guards against future action
    # additions that forget to extend this match.
    raise RuntimeError(f"unsupported Tier-2 redaction action {rule.action!r}")


#: Sentinel value that exceeds the longest realistic entity span
#: (5+ million chars) but stays well below Presidio's masking
#: assertion ceiling. Used in :func:`_build_operators` to ask
#: Presidio to mask the entire entity span without computing the
#: span length per entity (the operator caps at span length anyway).
_LARGE_MASK_LENGTH: Final[int] = 10_000_000


def _join_path(parent: str, child: str) -> str:
    """Same path semantics as the Tier-1 engine."""
    if not parent:
        return child
    return f"{parent}.{child}"
