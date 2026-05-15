# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the memory eval corpus (G4.3-T4, Task #443).

Coverage matrix:

* ``load_corpus("memory")`` returns 10 ``MemoryCorpusQuery`` rows from
  the shipped ``memory_queries.yaml`` — acceptance #1 + #3.
* Every row's first ``expected_hits`` pair lands on one of the 5
  ``MemoryScope`` values (live enum import; the corpus can't drift
  past the canonical scope list).
* The 10 rows cover all 5 scopes with each scope appearing as the
  top-1 ground truth for ~2 queries — acceptance #2 (~2 queries per
  scope: user / user-tenant / user-target / tenant / target).
* Every slug in the corpus matches the live ``SLUG_PATTERN`` regex —
  guards against typos that would silently misalign the corpus
  against any memory service consumer that hydrates these slugs into
  real entries.

The corpus is the contract; T2's eval runner (#441) consumes it
against `search_memory` once G5.1's memory module ships its
integration fixtures populating these `(scope, slug)` pairs. The
schema-validation gate here is purely the load-time contract — the
runtime end-to-end behaviour is T2's domain.
"""

from __future__ import annotations

import re
from collections import Counter

from meho_backplane.memory.schemas import SLUG_PATTERN, MemoryScope
from meho_backplane.retrieval.eval.corpus import (
    MemoryCorpusQuery,
    load_corpus,
)

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


def test_memory_corpus_scopes_are_valid_enum_members() -> None:
    """Every scope in every expected_hit must be a live ``MemoryScope`` value.

    Imports :class:`MemoryScope` directly so a future scope rename or
    removal forces a corpus update in the same PR — the corpus can't
    silently drift past the canonical scope list.
    """
    rows = load_corpus("memory")

    valid_scopes = {member.value for member in MemoryScope}
    bad: list[tuple[str, str]] = []
    for row in rows:
        for scope, _slug in row.expected_hits:
            if scope not in valid_scopes:
                bad.append((row.query, scope))

    assert not bad, (
        f"corpus references scopes outside MemoryScope: {bad}. Valid scopes: {sorted(valid_scopes)}"
    )


def test_memory_corpus_covers_all_five_scopes() -> None:
    """Acceptance #2: each scope appears as a top-1 ground truth ~2 times.

    The corpus is balanced at exactly 2 queries per scope on the
    first-hit (top-1) position, so a regression in any one scope's
    RBAC filter or ranking surfaces because at least one query
    targets it. Checking the top-1 position (rather than the union of
    all ``expected_hits`` pairs) is the property we actually care
    about — fallback pairs are acceptable adjacencies, not the load-
    bearing ground truth that defines a scope's coverage.
    """
    rows = load_corpus("memory")

    top1_scope_counts = Counter(row.expected_hits[0][0] for row in rows)

    expected_scopes = {member.value for member in MemoryScope}
    missing = expected_scopes - set(top1_scope_counts)
    assert not missing, (
        f"corpus omits scopes from its top-1 ground truth: {sorted(missing)}. "
        f"Every scope must have ≥1 top-1 query so a regression in that "
        f"scope's filter or ranking surfaces."
    )

    for scope, count in top1_scope_counts.items():
        assert count >= 2, (
            f"scope {scope!r} has only {count} top-1 query/queries; "
            f"the corpus targets ~2 per scope (10 queries / 5 scopes)"
        )


def test_memory_corpus_slugs_match_slug_pattern() -> None:
    """Every slug must match the live ``SLUG_PATTERN`` from the memory schema.

    Imports :data:`SLUG_PATTERN` directly so a future pattern tighten-
    ing (e.g. forbidding dots, requiring lower-case) forces the
    corpus to update in the same PR. A slug that fails this gate
    here would also fail the service-layer ``validate_slug`` at
    write-time — surfacing the regression at corpus-validation time
    saves operator round-trips.
    """
    rows = load_corpus("memory")

    pattern = re.compile(SLUG_PATTERN)
    bad: list[tuple[str, str, str]] = []
    for row in rows:
        for scope, slug in row.expected_hits:
            if not pattern.fullmatch(slug):
                bad.append((row.query, scope, slug))

    assert not bad, f"corpus references slugs that fail SLUG_PATTERN ({SLUG_PATTERN}): {bad}"
