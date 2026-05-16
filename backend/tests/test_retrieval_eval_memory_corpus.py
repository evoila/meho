# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the memory eval corpus (G4.3-T4, Task #443).

Coverage matrix:

* ``load_corpus("memory")`` returns 10 ``MemoryCorpusQuery`` rows
  from the shipped ``memory_queries.yaml`` — acceptance #1 + #3.
* Every row's ``expected_hits`` is a list of ``(scope, slug)``
  tuples (the schema's locked shape from T1 #440) — acceptance
  schema-validation criterion.
* Every scope value is one of the five ``MemoryScope`` enum
  members — guards against typos that the loose ``tuple[str, str]``
  schema can't catch on its own.
* All five scopes are represented in the corpus (acceptance #2:
  "~2 queries per scope") — regression detection so the corpus
  can't silently drift into one scope shape.
* Every slug is in the safe-URL alphabet ``MemoryService`` accepts,
  so a corpus entry is round-trippable against a real seeded
  memory record (the eval contract).
"""

from __future__ import annotations

import re

from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.retrieval.eval.corpus import (
    MemoryCorpusQuery,
    load_corpus,
)

# ---------------------------------------------------------------------------
# Slug-alphabet pattern.
#
# Mirrors the safe-URL alphabet documented on
# ``meho_backplane.memory.schemas.validate_slug``: letters / digits /
# hyphen / underscore / dot. Anchored at both ends so a stray space
# or angle-bracket placeholder (`<operator>` from the issue body) is
# rejected, not just internalised. The same alphabet is enforced by
# the memory service at write time — keeping the corpus inside it
# means a corpus entry round-trips against a real seeded memory.
# ---------------------------------------------------------------------------

_SAFE_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


#: All five MemoryScope values rendered as their wire-level strings.
#: A scope outside this set is either a typo or an enum extension
#: that requires a deliberate corpus update.
_VALID_SCOPES: frozenset[str] = frozenset(s.value for s in MemoryScope)


# ---------------------------------------------------------------------------
# Loader behaviour
# ---------------------------------------------------------------------------


def test_load_corpus_memory_returns_ten_typed_rows() -> None:
    """Acceptance #1 + #3: memory corpus loads 10 ``MemoryCorpusQuery`` rows."""
    rows = load_corpus("memory")

    assert len(rows) == 10
    assert all(isinstance(row, MemoryCorpusQuery) for row in rows)
    for row in rows:
        assert row.query.strip(), f"empty query: {row}"
        assert row.expected_hits, f"empty expected_hits: {row.query}"


def test_memory_corpus_expected_hits_are_pair_tuples() -> None:
    """Every expected_hits entry is a 2-tuple (Pydantic coerced from YAML list).

    T1 #440's schema declares ``expected_hits: list[tuple[str, str]]``
    with ``strict=True``; the corpus's ``mode="before"`` field
    validator pre-coerces YAML 2-element lists into tuples so strict
    validation accepts them. This test guards the post-coercion shape
    so a future schema refactor that drops the coercion (e.g. moves
    to a sub-model) fails loudly here rather than passing silently
    with mixed list / tuple types in the loaded rows.
    """
    rows = load_corpus("memory")

    for row in rows:
        for pair in row.expected_hits:
            assert isinstance(pair, tuple), (
                f"expected_hits pair is {type(pair).__name__}, not tuple: "
                f"query {row.query!r}, pair {pair!r}"
            )
            assert len(pair) == 2, (
                f"expected_hits pair has {len(pair)} elements, not 2: "
                f"query {row.query!r}, pair {pair!r}"
            )


def test_memory_corpus_uses_valid_scope_values() -> None:
    """Every (scope, slug) pair's scope is one of the five MemoryScope values.

    The schema's ``tuple[str, str]`` shape accepts any string in the
    scope slot — this test rejects typos (``user_target`` instead of
    ``user-target``) and stale scope names that no longer exist on
    the enum. Tying the assertion to the live enum means a future
    enum reshape surfaces here rather than at first eval run.
    """
    rows = load_corpus("memory")

    for row in rows:
        for scope, slug in row.expected_hits:
            assert scope in _VALID_SCOPES, (
                f"unknown scope {scope!r} in query {row.query!r} (pair "
                f"{(scope, slug)!r}); valid scopes: {sorted(_VALID_SCOPES)}"
            )


def test_memory_corpus_slugs_use_safe_alphabet() -> None:
    """Every slug fits the alphabet ``MemoryService.validate_slug`` enforces.

    A corpus entry that the memory service couldn't actually persist
    is non-load-bearing as ground truth — the eval can't compare
    MEHO retrieval against a row that fails the write boundary.
    """
    rows = load_corpus("memory")

    for row in rows:
        for _scope, slug in row.expected_hits:
            assert _SAFE_SLUG_PATTERN.fullmatch(slug), (
                f"slug {slug!r} in query {row.query!r} contains characters "
                f"outside the safe alphabet (letters / digits / hyphen / "
                f"underscore / dot) — MemoryService would reject this on write"
            )


def test_memory_corpus_covers_all_five_scopes() -> None:
    """Acceptance #2: every MemoryScope value appears in at least one query.

    Encodes the issue body's "~2 queries per scope (user /
    user-tenant / user-target / tenant / target)" coverage demand as
    a structural test. Counted over the union of expected_hits
    scopes, so a query with a multi-scope fallback (e.g. user-target
    primary + target fallback) credits both sides. A corpus that
    drifts into one scope shape fails here so future edits keep the
    cross-scope regression-detection property.
    """
    rows = load_corpus("memory")

    referenced_scopes = {scope for row in rows for scope, _slug in row.expected_hits}
    missing = _VALID_SCOPES - referenced_scopes

    assert not missing, (
        f"corpus is missing queries for these scopes: {sorted(missing)}. "
        f"Initiative #373 requires every MemoryScope value to appear at "
        f"least once so a regression in any one scope surfaces in the eval."
    )


def test_memory_corpus_has_at_least_two_queries_per_scope() -> None:
    """The "~2 queries per scope" target from #443's corpus content section.

    Distinct from :func:`test_memory_corpus_covers_all_five_scopes`
    which only asserts presence — this one asserts the per-scope
    *count*. Counted over the primary (top-1) expected hit per
    query, so a multi-scope fallback doesn't inflate the count of
    its fallback scope. The target is ≥2, not ==2, so a future
    operator-driven corpus expansion (e.g. an extra tenant-scope
    query covering a newly-locked policy) doesn't trip this test.
    """
    rows = load_corpus("memory")

    primary_scope_counts: dict[str, int] = dict.fromkeys(_VALID_SCOPES, 0)
    for row in rows:
        primary_scope, _slug = row.expected_hits[0]
        primary_scope_counts[primary_scope] += 1

    under_quota = {scope: count for scope, count in primary_scope_counts.items() if count < 2}
    assert not under_quota, (
        f"these scopes have fewer than 2 primary-hit queries: {under_quota}. "
        f"Initiative #373 calls for ~2 queries per scope so each scope shape "
        f"has independent regression coverage."
    )
