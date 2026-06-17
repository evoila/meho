# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.retrieval.retriever`.

Scope: the RRF fusion math, the :class:`RetrievalHit` pydantic
contract, and the :func:`_coerce_uuid` driver-portability helper.
These run against in-process inputs with no database and no
embedding service -- the load-bearing correctness checks for the
hybrid-retrieval ordering happen here.

PG-real coverage (the SQL bindings ``ts_rank_cd`` /
``plainto_tsquery`` / ``embedding <=> CAST(:emb AS vector)`` work
against a real pgvector cluster, plus tenant scoping + source/kind
filters + empty-corpus behaviour) lives in G0.4-T6's
:mod:`tests.integration.test_retrieval_e2e` because the operators
have no SQLite analogue and the testcontainer wiring needs an
async-fixture-managed event loop (the conftest's ``pg_engine`` is
the shape that handles that cleanly, vs the sync-test
``asyncio.run`` pattern that hits cross-loop asyncpg cleanup
issues).

A regression in the fusion math would corrupt every retrieval
downstream silently (both signals would still return results, just
in the wrong order). The unit tests below pin every fusion-math
branch -- empty inputs, BM25-only, cosine-only, intersection
outranking singletons, ``limit`` truncation, ``None``-score
gracefulness.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.retrieval.retriever import (
    CANDIDATE_LIMIT,
    RRF_K,
    RetrievalHit,
    _coerce_uuid,
    _FusedEntry,
    _rrf_fuse,
    retrieve,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# RRF fusion math (pure, no DB, no embedding)
# ---------------------------------------------------------------------------


def _row(doc_id: uuid.UUID, score: float | None) -> Any:
    """Build a minimal ``Row``-like object with ``.id`` and ``.score``.

    ``score`` accepts ``None`` so the ``None``-score branch test
    (`test_rrf_fuse_handles_none_score_gracefully`) can match the
    real PG shape -- ``ts_rank_cd`` returns NULL against a
    degenerate tsquery and the helper must keep summing ranks even
    when the score is missing.
    """
    row = MagicMock()
    row.id = doc_id
    row.score = score
    return row


def test_rrf_k_constant_matches_paper() -> None:
    """``RRF_K = 60`` is the Microsoft 2009 paper default. Locked in.

    The literature shows RRF is robust to k variations within
    10--100, but the *exact* value affects the per-document score
    magnitudes (not the ordering). Callers asserting score ranges
    in downstream tests depend on this constant being 60.
    """
    assert RRF_K == 60
    assert CANDIDATE_LIMIT == 50


def test_rrf_fuse_handles_empty_inputs() -> None:
    """Empty BM25 + empty cosine -> empty fused list."""
    fused = _rrf_fuse([], [], limit=10)
    assert fused == []


def test_rrf_fuse_only_bm25() -> None:
    """One signal -> single fused contribution per doc.

    A doc at BM25 rank 1 gets ``1/(60+1) = 0.01639...``; at rank 2
    gets ``1/62 = 0.01613...``. Sorting must preserve the BM25
    rank order, and cosine fields must be ``None``.
    """
    a = uuid.uuid4()
    b = uuid.uuid4()
    fused = _rrf_fuse([_row(a, 0.9), _row(b, 0.5)], [], limit=10)
    assert [e.document_id for e in fused] == [a, b]
    assert fused[0].bm25_rank == 1
    assert fused[0].cosine_rank is None
    assert fused[0].cosine_score is None
    assert fused[0].bm25_score == pytest.approx(0.9)
    assert fused[0].fused_score == pytest.approx(1.0 / (RRF_K + 1))


def test_rrf_fuse_only_cosine() -> None:
    """Cosine-only signal mirrors the BM25-only case."""
    a = uuid.uuid4()
    fused = _rrf_fuse([], [_row(a, 0.95)], limit=10)
    assert len(fused) == 1
    assert fused[0].bm25_rank is None
    assert fused[0].cosine_rank == 1
    assert fused[0].cosine_score == pytest.approx(0.95)
    assert fused[0].fused_score == pytest.approx(1.0 / (RRF_K + 1))


def test_rrf_fuse_intersection_doc_outranks_singletons() -> None:
    """Doc in both signals' top-50 beats docs in only one.

    Construct: doc-A is BM25 rank 1 only; doc-B is cosine rank 1
    only; doc-C is BM25 rank 5 AND cosine rank 5. C's fused score
    must exceed both A's and B's singleton contributions when the
    two singleton ranks are themselves modest -- exactly the
    "fusion outperforms either signal alone" claim that justifies
    using RRF in the first place.

    Math:
      A fused = 1/61 = 0.01639
      B fused = 1/61 = 0.01639
      C fused = 1/65 + 1/65 = 2/65 = 0.03077

    C > A == B, which the assertion checks directly.
    """
    a = uuid.uuid4()
    b = uuid.uuid4()
    c = uuid.uuid4()
    fused = _rrf_fuse(
        bm25_rows=[
            _row(a, 1.0),
            _row(uuid.uuid4(), 0.9),
            _row(uuid.uuid4(), 0.8),
            _row(uuid.uuid4(), 0.7),
            _row(c, 0.5),
        ],
        cosine_rows=[
            _row(b, 0.95),
            _row(uuid.uuid4(), 0.92),
            _row(uuid.uuid4(), 0.9),
            _row(uuid.uuid4(), 0.85),
            _row(c, 0.7),
        ],
        limit=10,
    )

    by_id = {e.document_id: e for e in fused}
    assert by_id[c].fused_score > by_id[a].fused_score
    assert by_id[c].fused_score > by_id[b].fused_score
    # Ranked order: C must be first.
    assert fused[0].document_id == c
    assert fused[0].bm25_rank == 5
    assert fused[0].cosine_rank == 5
    assert fused[0].bm25_score == pytest.approx(0.5)
    assert fused[0].cosine_score == pytest.approx(0.7)


def test_rrf_fuse_respects_limit() -> None:
    """``limit`` truncates the output list, ordering preserved."""
    ids = [uuid.uuid4() for _ in range(20)]
    bm25 = [_row(i, 1.0 - 0.01 * n) for n, i in enumerate(ids)]
    fused = _rrf_fuse(bm25, [], limit=5)
    assert len(fused) == 5
    # Top 5 are the BM25 top-5 (rank order preserved).
    assert [e.document_id for e in fused] == ids[:5]


def test_rrf_fuse_rejects_negative_limit() -> None:
    """``limit < 0`` raises :class:`ValueError` before any work.

    Without the guard, Python's slice semantics would silently
    truncate the fused list with a negative bound, returning a
    partial result with no operator-facing signal that the request
    was malformed. The helper fails fast at the boundary so callers
    surface the bug at the call site, not three hops downstream.
    """
    with pytest.raises(ValueError, match="limit must be >= 0"):
        _rrf_fuse([_row(uuid.uuid4(), 1.0)], [], limit=-1)


def test_rrf_fuse_zero_limit_returns_empty_without_sorting() -> None:
    """``limit == 0`` short-circuits to an empty list.

    Slicing with ``[:0]`` would yield the same result, but the
    short-circuit skips the sort -- meaningful when the candidate
    lists are large and the caller's intent is clearly "no hits".
    """
    a = uuid.uuid4()
    fused = _rrf_fuse([_row(a, 1.0)], [_row(a, 0.9)], limit=0)
    assert fused == []


def test_rrf_fuse_handles_none_score_gracefully() -> None:
    """A row with ``score = None`` still contributes its rank.

    Some PG configurations return NULL for ``ts_rank_cd`` against
    a degenerate tsquery; the fusion must not crash. The stored
    ``bm25_score`` / ``cosine_score`` field surfaces as ``None``
    but the RRF contribution still fires (rank-based, not score-
    based).
    """
    a = uuid.uuid4()
    fused = _rrf_fuse([_row(a, None)], [], limit=10)
    assert fused[0].bm25_rank == 1
    assert fused[0].bm25_score is None
    assert fused[0].fused_score == pytest.approx(1.0 / (RRF_K + 1))


# ---------------------------------------------------------------------------
# _coerce_uuid (driver-portability helper)
# ---------------------------------------------------------------------------


def test_coerce_uuid_passes_through_uuid_instance() -> None:
    """A real ``UUID`` returns unchanged."""
    u = uuid.uuid4()
    assert _coerce_uuid(u) is u


def test_coerce_uuid_parses_string_form() -> None:
    """A hex string round-trips through the UUID parser."""
    u = uuid.uuid4()
    assert _coerce_uuid(str(u)) == u


# ---------------------------------------------------------------------------
# RetrievalHit pydantic model
# ---------------------------------------------------------------------------


def test_retrieval_hit_is_frozen() -> None:
    """``RetrievalHit`` is frozen -- mutation raises a pydantic error.

    The frozen contract is what lets the API surface (T5) return the
    hits unchanged; a mutable shape would risk a handler accidentally
    mutating a hit between RRF fusion and response serialisation.
    """
    ts = datetime(2026, 5, 21, 10, 16, 12, tzinfo=UTC)
    hit = RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        source="kb",
        source_id="k8s",
        kind="kb-entry",
        body="body",
        doc_metadata={},
        created_at=ts,
        updated_at=ts,
        fused_score=0.03,
        bm25_score=0.5,
        cosine_score=0.7,
        bm25_rank=1,
        cosine_rank=1,
    )
    with pytest.raises((ValueError, TypeError)):
        hit.fused_score = 0.99  # type: ignore[misc]


def test_retrieval_hit_requires_timestamps() -> None:
    """``RetrievalHit`` rejects construction without ``created_at`` / ``updated_at``.

    G0.9.1-T4 (#776). The substrate must carry the persisted column
    values through to downstream consumers (memory ``search_memory``
    today, every other read surface that wants honest mtime later);
    defaulting them to ``None`` or an epoch sentinel would re-open the
    silent-corruption trap the issue closed. Pinning the required-
    field contract here keeps future schema edits honest.
    """
    with pytest.raises(ValueError, match=r"created_at|updated_at"):
        RetrievalHit(  # type: ignore[call-arg]
            document_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            source="kb",
            source_id="k8s",
            kind="kb-entry",
            body="body",
            doc_metadata={},
            fused_score=0.03,
            bm25_score=0.5,
            cosine_score=0.7,
            bm25_rank=1,
            cosine_rank=1,
        )


# ---------------------------------------------------------------------------
# _FusedEntry shape (regression test for slots + None defaults)
# ---------------------------------------------------------------------------


def test_fused_entry_starts_with_none_scores() -> None:
    """``_FusedEntry`` defaults: all score / rank fields ``None``, fused 0.0.

    ``pytest.approx`` for the float default matches the style every
    other float assertion in this module uses (lines 195, 213, 230,
    etc.); Sonar's ``python:S1244`` rule flags exact float equality
    unconditionally, even when the literal is ``0.0``. The default
    is deterministic by construction, so the assertion is over-
    flagged -- but the unified style keeps the SonarCloud Quality
    Gate clean and the file internally consistent.
    """
    e = _FusedEntry(uuid.uuid4())
    assert e.bm25_score is None
    assert e.cosine_score is None
    assert e.bm25_rank is None
    assert e.cosine_rank is None
    assert e.fused_score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# metadata_filters plumbing (G4.4-T1 / #1177)
#
# These tests don't hit a real PG cluster -- they patch the per-signal
# candidate helpers + the embedding service to assert that the
# ``metadata_filters`` parameter threads from :func:`retrieve` through
# both candidate queries' bind dicts as a stable JSON string. PG-real
# coverage (the actual ``documents.metadata @> :filters_jsonb``
# containment query semantics) lives in the integration suite per the
# same SQLite-undefined contract the BM25 + cosine operators carry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_threads_metadata_filters_to_candidate_helpers() -> None:
    """A ``metadata_filters`` dict reaches both per-signal SQL helpers.

    The substrate's contract: both BM25 and cosine candidate queries
    apply the same ``documents.metadata @>`` containment so the fused
    list is the intersection of two equally-scoped sets, not a
    surprise asymmetry. Assert the JSON payload arrives at both
    helpers byte-for-byte identical.
    """
    captured_bm25: dict[str, object] = {}
    captured_cosine: dict[str, object] = {}

    async def fake_bm25(
        session: Any,
        tenant_id: uuid.UUID,
        query: str,
        source: str | None,
        kind: str | None,
        metadata_filters_json: str | None,
        principal_sub: str | None,
    ) -> list[Any]:
        captured_bm25["metadata_filters_json"] = metadata_filters_json
        return []

    async def fake_cosine(
        session: Any,
        tenant_id: uuid.UUID,
        embedding_literal: str,
        source: str | None,
        kind: str | None,
        metadata_filters_json: str | None,
        principal_sub: str | None,
    ) -> list[Any]:
        captured_cosine["metadata_filters_json"] = metadata_filters_json
        return []

    fake_embedding_service = MagicMock()
    fake_embedding_service.encode_one = AsyncMock(return_value=[0.0] * 384)
    fake_session = MagicMock()

    with (
        patch(
            "meho_backplane.retrieval.retriever.get_embedding_service",
            return_value=fake_embedding_service,
        ),
        patch(
            "meho_backplane.retrieval.retriever._bm25_candidates",
            side_effect=fake_bm25,
        ),
        patch(
            "meho_backplane.retrieval.retriever._cosine_candidates",
            side_effect=fake_cosine,
        ),
    ):
        hits = await retrieve(
            tenant_id=uuid.uuid4(),
            query="anything",
            metadata_filters={"source_kind": "evoila-distilled", "product": "vcenter"},
            session=fake_session,
        )

    assert hits == []
    # Both helpers received the same serialised JSON.
    bm25_json = captured_bm25["metadata_filters_json"]
    cosine_json = captured_cosine["metadata_filters_json"]
    assert bm25_json == cosine_json
    assert isinstance(bm25_json, str)
    # Sort_keys=True is load-bearing for the audit-payload reproducibility
    # the API surface tests pin -- assert the sorted key ordering directly.
    assert json.loads(bm25_json) == {
        "product": "vcenter",
        "source_kind": "evoila-distilled",
    }
    assert bm25_json.index('"product"') < bm25_json.index('"source_kind"')


@pytest.mark.asyncio
async def test_retrieve_passes_none_metadata_filters_by_default() -> None:
    """``metadata_filters=None`` (default) preserves the pre-G4.4-T1 path.

    Both candidate helpers receive ``metadata_filters_json=None``; the
    SQL ``CAST(:metadata_filters AS text) IS NULL`` branch short-
    circuits the containment predicate so existing tests are
    byte-for-byte unaffected.
    """
    captured_bm25: dict[str, object] = {}

    async def fake_bm25(
        session: Any,
        tenant_id: uuid.UUID,
        query: str,
        source: str | None,
        kind: str | None,
        metadata_filters_json: str | None,
        principal_sub: str | None,
    ) -> list[Any]:
        captured_bm25["metadata_filters_json"] = metadata_filters_json
        return []

    async def fake_cosine(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    fake_embedding_service = MagicMock()
    fake_embedding_service.encode_one = AsyncMock(return_value=[0.0] * 384)

    with (
        patch(
            "meho_backplane.retrieval.retriever.get_embedding_service",
            return_value=fake_embedding_service,
        ),
        patch(
            "meho_backplane.retrieval.retriever._bm25_candidates",
            side_effect=fake_bm25,
        ),
        patch(
            "meho_backplane.retrieval.retriever._cosine_candidates",
            side_effect=fake_cosine,
        ),
    ):
        await retrieve(tenant_id=uuid.uuid4(), query="q", session=MagicMock())

    assert captured_bm25["metadata_filters_json"] is None


@pytest.mark.asyncio
async def test_retrieve_normalises_empty_dict_to_none() -> None:
    """``metadata_filters={}`` short-circuits to ``None`` (no predicate).

    ``@> '{}'::jsonb`` matches every row, so emitting the predicate
    against an empty dict is pure DB-side parse cost with zero
    filtering benefit. Assert the boundary normalises ``{}`` →
    ``None`` so the SQL stays clean.
    """
    captured: dict[str, object] = {}

    async def fake_bm25(
        session: Any,
        tenant_id: uuid.UUID,
        query: str,
        source: str | None,
        kind: str | None,
        metadata_filters_json: str | None,
        principal_sub: str | None,
    ) -> list[Any]:
        captured["metadata_filters_json"] = metadata_filters_json
        return []

    async def fake_cosine(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    fake_embedding_service = MagicMock()
    fake_embedding_service.encode_one = AsyncMock(return_value=[0.0] * 384)

    with (
        patch(
            "meho_backplane.retrieval.retriever.get_embedding_service",
            return_value=fake_embedding_service,
        ),
        patch(
            "meho_backplane.retrieval.retriever._bm25_candidates",
            side_effect=fake_bm25,
        ),
        patch(
            "meho_backplane.retrieval.retriever._cosine_candidates",
            side_effect=fake_cosine,
        ),
    ):
        await retrieve(tenant_id=uuid.uuid4(), query="q", metadata_filters={}, session=MagicMock())

    assert captured["metadata_filters_json"] is None


@pytest.mark.asyncio
async def test_retrieve_serialises_metadata_filters_with_sorted_keys() -> None:
    """The bind JSON sorts keys so the same dict produces the same string.

    Two semantically-equal filter dicts with different in-memory key
    insertion orders must produce byte-identical bind strings. The
    audit payload's key-only digest pins the same reproducibility on
    the API surface; the substrate-side stability is the foundation
    that makes the API-side digest stable across Python's dict ordering.
    """
    payloads: list[str | None] = []

    async def fake_bm25(
        session: Any,
        tenant_id: uuid.UUID,
        query: str,
        source: str | None,
        kind: str | None,
        metadata_filters_json: str | None,
        principal_sub: str | None,
    ) -> list[Any]:
        payloads.append(metadata_filters_json)
        return []

    async def fake_cosine(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    fake_embedding_service = MagicMock()
    fake_embedding_service.encode_one = AsyncMock(return_value=[0.0] * 384)

    with (
        patch(
            "meho_backplane.retrieval.retriever.get_embedding_service",
            return_value=fake_embedding_service,
        ),
        patch(
            "meho_backplane.retrieval.retriever._bm25_candidates",
            side_effect=fake_bm25,
        ),
        patch(
            "meho_backplane.retrieval.retriever._cosine_candidates",
            side_effect=fake_cosine,
        ),
    ):
        await retrieve(
            tenant_id=uuid.uuid4(),
            query="q",
            metadata_filters={"b": 2, "a": 1, "c": 3},
            session=MagicMock(),
        )
        await retrieve(
            tenant_id=uuid.uuid4(),
            query="q",
            metadata_filters={"c": 3, "a": 1, "b": 2},
            session=MagicMock(),
        )

    assert payloads[0] == payloads[1]
    assert payloads[0] == '{"a": 1, "b": 2, "c": 3}'
