# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Eval corpus loader + per-surface Pydantic schemas.

G4.3-T1 (#440). The :func:`load_corpus` entry point reads one of the
three per-surface YAML files (``kb_queries.yaml`` /
``memory_queries.yaml`` / ``operation_queries.yaml``) co-located with
this module, validates every entry against its frozen Pydantic v2
schema, and returns the typed list. Validation failures raise
:class:`CorpusValidationError` (a loud failure surfacing the bad
entry) — a broken corpus must not silently run an eval that reports
spuriously perfect numbers.

T1 ships the kb seed corpus only. The memory + operations YAML files
land in T4 (#443) / T3 (#442); :func:`load_corpus` returns ``[]`` for
those surfaces until the corresponding file lands so T2's eval runner
can iterate every surface without crashing.

Schemas are **frozen + extra=forbid + strict** (Pydantic v2). Frozen
keeps the loaded corpus immutable for the run lifetime — a regression
where an eval pass quietly rewrote ``expected_hits`` mid-run is
impossible by construction. ``extra=forbid`` rejects YAML keys that
look right but aren't (e.g. ``expected_slug`` instead of
``expected_hits`` — a copy-paste from the issue body would otherwise
silently ignore the field). ``strict`` blocks the YAML-typing footgun
where a bare ``yes`` or unquoted slug becomes a ``bool``.
"""

from __future__ import annotations

import re
from importlib import resources
from typing import Annotated, Any, Final, Literal, overload

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    TypeAdapter,
    ValidationError,
    model_validator,
)

__all__ = [
    "KB_PASSAGE_FIXTURE_VERSION",
    "CorpusValidationError",
    "KbCorpusQuery",
    "KbPassageFixtureEntry",
    "KbPassageFixtures",
    "KbPassageQuery",
    "MemoryCorpusQuery",
    "OperationCorpusQuery",
    "load_corpus",
    "load_kb_passage_fixtures",
    "parse_kb_passage_fixtures",
]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

# Shared model config: immutable after load, no unknown fields, no silent
# coercion. Reused by every per-surface schema so the discipline can't
# drift between surfaces.
_CORPUS_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid", strict=True)


class KbCorpusQuery(BaseModel):
    """One ground-truth eval row for the ``kb`` retrieval surface.

    Attributes
    ----------
    query
        The natural-language phrasing an operator would type. Sourced
        from operator Slack history + recent Claude session logs (see
        per-entry ``notes`` for the originating context).
    expected_hits
        Ordered list of ``kb`` slugs (without the ``.md`` suffix) the
        operator would consider a correct hit. The first entry is the
        ideal top-1; subsequent entries are acceptable top-N
        alternatives. Order matters for MRR scoring (T2's runner).
    notes
        Free-form rationale — operator who phrased the query, the
        reason this slug ranks first, anything that informs human
        review on regression. Optional; informational only.
    """

    model_config = _CORPUS_MODEL_CONFIG

    query: str
    expected_hits: list[str]
    notes: str | None = None


def _coerce_scope_slug_pair(value: Any) -> Any:
    """Convert a 2-element YAML sequence to a ``(scope, slug)`` tuple.

    YAML has no native tuple type — ``yaml.safe_load`` always produces
    ``list`` for sequence values — but the :class:`MemoryCorpusQuery`
    schema is strict-mode and declares ``expected_hits`` as
    ``list[tuple[str, str]]``. Strict Pydantic v2 rejects
    ``list`` → ``tuple`` coercion, so a YAML row like
    ``["user", "kubectl-preferences"]`` fails validation despite being
    the wire shape the task body specifies.

    This ``BeforeValidator`` runs ahead of the tuple-type check and
    converts only the exact shape we accept (2-element sequence of
    strings) to a tuple. Any other shape — wrong length, non-string
    member, plain string, dict — falls through unchanged so the
    downstream tuple validator surfaces the original error message
    naming the offending field path.
    """
    if isinstance(value, list) and len(value) == 2:
        return tuple(value)
    return value


#: Type alias for one ``(scope, slug)`` expected-hit pair. The
#: :func:`_coerce_scope_slug_pair` ``BeforeValidator`` runs first, so
#: YAML-native ``[scope, slug]`` sequences are accepted as well as
#: Python-native ``(scope, slug)`` tuples from direct construction.
_ScopeSlugPair = Annotated[tuple[str, str], BeforeValidator(_coerce_scope_slug_pair)]


class MemoryCorpusQuery(BaseModel):
    """One ground-truth eval row for the ``memory`` retrieval surface.

    Mirror of :class:`KbCorpusQuery` for the G5 memory layer — each
    expected hit is a ``(scope, slug)`` pair because the same slug can
    legitimately exist under different scopes (``user`` vs
    ``user-tenant`` vs ``tenant`` etc.) and the eval cares which one
    surfaced. The corpus YAML ships in T4 (#443); the pair is exposed
    on the Python side as a ``tuple`` so callers can use it as a dict
    key without intermediate normalisation.
    """

    model_config = _CORPUS_MODEL_CONFIG

    query: str
    expected_hits: list[_ScopeSlugPair]
    notes: str | None = None


class OperationCorpusQuery(BaseModel):
    """One ground-truth eval row for the ``operations`` retrieval surface.

    Captures the agent-facing ``search_operations`` UX: a free-text
    query, the connector implementation it should resolve to, and the
    set of acceptable ``op_id`` values. ``govc_equivalent`` is the
    pre-MEHO operator workflow (the consumer's existing CLI) — the
    baseline T2's runner compares MEHO ranking against. The corpus
    YAML lands in T3 (#442).
    """

    model_config = _CORPUS_MODEL_CONFIG

    query: str
    expected_connector_id: str
    expected_op_ids: list[str]
    govc_equivalent: str | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# KB passage benchmark fixtures (#3765)
# ---------------------------------------------------------------------------

#: Version of ``kb_passage_fixtures.yaml`` this code reads. Bump it with
#: the YAML whenever a body, query or expected reference changes, so
#: reports from different fixture versions are never compared.
KB_PASSAGE_FIXTURE_VERSION: Final[int] = 1

KbPassageScenario = Literal[
    "middle_passage", "end_passage", "duplicate", "conflict", "stale", "no_match"
]
KbPassageLanguage = Literal["en", "de"]


class KbPassageFixtureEntry(BaseModel):
    """One synthetic kb entry: a Markdown body and whether it is still true.

    ``stale`` marks an outdated entry and ``conflicting`` marks a
    current-looking entry that contradicts the reviewed one. Queries
    may only expect ``current`` entries.
    """

    model_config = _CORPUS_MODEL_CONFIG

    slug: str
    language: KbPassageLanguage
    status: Literal["current", "stale", "conflicting"]
    body: str


class KbPassageQuery(BaseModel):
    """One benchmark query with its expected source and passage.

    See the header of ``kb_passage_fixtures.yaml`` for the meaning of
    each field. ``no_match`` queries have no expected values.
    """

    model_config = _CORPUS_MODEL_CONFIG

    id: str
    scenario: KbPassageScenario
    language: KbPassageLanguage
    query: str
    expected_slugs: list[str] = []
    expected_heading: str | None = None
    answer_terms: list[str] = []
    harmful_slugs: list[str] = []


def _has_heading(body: str, heading: str) -> bool:
    return re.search(rf"^#{{1,6}}[ \t]+{re.escape(heading)}[ \t]*$", body, re.MULTILINE) is not None


class KbPassageFixtures(BaseModel):
    """The whole versioned fixture file, checked for internal consistency.

    The checks make a broken fixture fail at load time: every expected
    entry exists, is current, has the expected heading and holds every
    answer term; every harmful entry exists and is not current.
    """

    model_config = _CORPUS_MODEL_CONFIG

    version: int
    entries: list[KbPassageFixtureEntry]
    queries: list[KbPassageQuery]

    @model_validator(mode="after")
    def _check_consistency(self) -> KbPassageFixtures:
        if self.version != KB_PASSAGE_FIXTURE_VERSION:
            raise ValueError(
                f"fixture version {self.version} does not match the code "
                f"version {KB_PASSAGE_FIXTURE_VERSION}"
            )
        entries = {e.slug: e for e in self.entries}
        if len(entries) != len(self.entries):
            raise ValueError("entry slugs must be unique")
        if len({q.id for q in self.queries}) != len(self.queries):
            raise ValueError("query ids must be unique")
        for q in self.queries:
            _check_query(q, entries)
        return self


def _check_expected(
    q: KbPassageQuery, heading: str, slug: str, entry: KbPassageFixtureEntry | None
) -> None:
    if entry is None or entry.status != "current":
        raise ValueError(f"{q.id}: expected slug {slug!r} is not a current entry")
    if not _has_heading(entry.body, heading):
        raise ValueError(f"{q.id}: {slug!r} has no heading {heading!r}")
    missing = [t for t in q.answer_terms if t not in entry.body]
    if missing:
        raise ValueError(f"{q.id}: {slug!r} does not contain {missing!r}")


def _check_query(q: KbPassageQuery, entries: dict[str, KbPassageFixtureEntry]) -> None:
    if q.scenario == "no_match":
        if q.expected_slugs or q.expected_heading or q.answer_terms:
            raise ValueError(f"{q.id}: a no_match query has no expected values")
        return
    if not q.expected_slugs or q.expected_heading is None or not q.answer_terms:
        raise ValueError(f"{q.id}: needs expected_slugs, expected_heading and answer_terms")
    if q.scenario in ("conflict", "stale") and not q.harmful_slugs:
        raise ValueError(f"{q.id}: a {q.scenario} query needs harmful_slugs")
    for slug in q.expected_slugs:
        _check_expected(q, q.expected_heading, slug, entries.get(slug))
    for slug in q.harmful_slugs:
        entry = entries.get(slug)
        if entry is None or entry.status == "current":
            raise ValueError(f"{q.id}: harmful slug {slug!r} is not a stale or conflicting entry")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class CorpusValidationError(Exception):
    """Raised when a corpus YAML file is malformed or schema-mismatched.

    The message names the surface, the file path that was loaded, and
    the underlying parse / validation error. Operators running
    ``meho retrieval eval`` (T2) see the message verbatim; the loud
    failure is intentional — a corpus that loads with garbage entries
    would produce eval numbers that look authoritative but reflect
    nothing about retrieval quality.
    """


# Per-surface adapters — built once at import time so each call to
# ``load_corpus`` reuses the cached schema. ``TypeAdapter`` is the
# Pydantic v2 escape hatch for validating non-``BaseModel`` shapes
# (here, ``list[T]``); it picks up the model's frozen / extra=forbid
# / strict configuration without needing a wrapper class.
_KB_ADAPTER = TypeAdapter(list[KbCorpusQuery])
_MEMORY_ADAPTER = TypeAdapter(list[MemoryCorpusQuery])
_OPERATION_ADAPTER = TypeAdapter(list[OperationCorpusQuery])


# Per-surface YAML filenames inside this package. Resolved relative to
# the package via ``importlib.resources`` so the loader works in both
# editable installs (source tree) and built wheels (where the YAML is
# packaged as resource data).
_CORPUS_FILES: dict[str, str] = {
    "kb": "kb_queries.yaml",
    "memory": "memory_queries.yaml",
    "operations": "operation_queries.yaml",
}


@overload
def load_corpus(surface: Literal["kb"]) -> list[KbCorpusQuery]: ...
@overload
def load_corpus(surface: Literal["memory"]) -> list[MemoryCorpusQuery]: ...
@overload
def load_corpus(surface: Literal["operations"]) -> list[OperationCorpusQuery]: ...


def load_corpus(
    surface: Literal["kb", "memory", "operations"],
) -> list[KbCorpusQuery] | list[MemoryCorpusQuery] | list[OperationCorpusQuery]:
    """Load + validate the YAML eval corpus for *surface*.

    Returns the validated list of typed query rows. The order of the
    returned list mirrors the YAML file order — eval runners that need
    deterministic ordering for diff-friendly output get it for free.

    A missing YAML file for the ``memory`` or ``operations`` surface
    returns ``[]`` (the corpora land in T4 / T3 respectively); a
    missing ``kb`` file is a hard error since this Task ships it.
    Any file present must parse + validate, regardless of surface — a
    half-baked memory corpus that fails validation must surface, not
    silently return ``[]``.

    Parameters
    ----------
    surface
        One of ``"kb"`` / ``"memory"`` / ``"operations"``.

    Returns
    -------
    list[KbCorpusQuery] | list[MemoryCorpusQuery] | list[OperationCorpusQuery]
        Typed list whose element type matches *surface*.

    Raises
    ------
    CorpusValidationError
        Raised when the YAML file is present but malformed, when its
        top-level value is not a list, or when any entry fails
        Pydantic validation. The message names the surface, file
        path, and underlying error.
    """
    filename = _CORPUS_FILES[surface]
    resource = resources.files(__package__).joinpath(filename)

    if not resource.is_file():
        if surface == "kb":
            # The kb corpus is mandatory in T1 — its absence is a
            # packaging error (the YAML failed to ship with the
            # wheel) or a developer who deleted it locally without
            # recreating it.
            raise CorpusValidationError(
                f"kb corpus YAML missing: expected {filename} alongside "
                f"{__package__}; T1 (#440) must ship this file"
            )
        return []

    raw = resource.read_text(encoding="utf-8")

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise CorpusValidationError(
            f"{surface} corpus YAML failed to parse ({filename}): {exc}"
        ) from exc

    # Empty file (``parsed is None``) is treated as "no entries" rather
    # than a parse error — useful when stubbing a surface during an
    # in-flight migration. Schema validation still rejects entries that
    # are present-but-malformed.
    if parsed is None:
        return []

    if not isinstance(parsed, list):
        raise CorpusValidationError(
            f"{surface} corpus YAML must be a top-level list ({filename}); "
            f"got {type(parsed).__name__}"
        )

    try:
        if surface == "kb":
            return _KB_ADAPTER.validate_python(parsed)
        if surface == "memory":
            return _MEMORY_ADAPTER.validate_python(parsed)
        return _OPERATION_ADAPTER.validate_python(parsed)
    except ValidationError as exc:
        # Pydantic's default message names the failing field path
        # (e.g. ``2.expected_hits.0``) which is what an operator
        # editing the YAML needs to find the bad entry.
        raise CorpusValidationError(
            f"{surface} corpus YAML failed validation ({filename}):\n{exc}"
        ) from exc


_KB_PASSAGE_FIXTURE_FILE: Final[str] = "kb_passage_fixtures.yaml"


def parse_kb_passage_fixtures(raw: str) -> KbPassageFixtures:
    """Parse and check KB passage fixture YAML text.

    Raises
    ------
    CorpusValidationError
        The YAML does not parse, or the content fails a schema or
        consistency check. The message names the failing field.
    """
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise CorpusValidationError(f"kb passage fixtures failed to parse: {exc}") from exc
    try:
        return KbPassageFixtures.model_validate(parsed)
    except ValidationError as exc:
        raise CorpusValidationError(f"kb passage fixtures failed validation:\n{exc}") from exc


def load_kb_passage_fixtures() -> KbPassageFixtures:
    """Load the shipped ``kb_passage_fixtures.yaml`` (#3765)."""
    resource = resources.files(__package__).joinpath(_KB_PASSAGE_FIXTURE_FILE)
    return parse_kb_passage_fixtures(resource.read_text(encoding="utf-8"))
