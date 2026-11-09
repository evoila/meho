# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``SEARCH_CLEAR_LEAD`` switch of ``search_operations`` (#6014).

These tests use the scores of a real search and a stand-in retriever. The
same rule against the real retriever is in
``tests/integration/test_search_clear_lead_pg.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.operations._search import _rrf_fuse
from meho_backplane.operations._search_clear_lead import (
    search_clear_lead,
    top_hit_lead_is_unclear,
)
from meho_backplane.operations.meta_tools import (
    SEARCH_CLASSIFY_MIN_HITS,
    OperationSearchHit,
    _run_search,
)
from meho_backplane.settings import get_settings

_LIST_VMS = "vmware.vm.list"


def _hit(op_id: str, fused: float, bm25: float | None, cosine: float | None) -> OperationSearchHit:
    return OperationSearchHit(
        op_id=op_id,
        summary=None,
        description=None,
        group_key=None,
        safety_level="safe",
        requires_approval=False,
        fused_score=fused,
        bm25_score=bm25,
        cosine_score=cosine,
    )


#: The scores of a live search for "list virtual machines". The right
#: operation is fifth; the two hits with the best meaning scores sit below
#: the top hit.
_LIVE_SEARCH = [
    _hit("GET:/vcenter/vm/{vm}/data-sets", 0.02991, 0.0167, 0.746),
    _hit("GET:/vcenter/storage/policies/compliance/vm", 0.02964, 0.0625, 0.735),
    _hit("GET:/vcenter/vm/{vm}/hardware/boot/device", 0.02844, 0.0917, 0.721),
    _hit("GET:/vcenter/vm/{vm}/hardware/disk", 0.01639, None, 0.775),
    _hit(_LIST_VMS, 0.01613, None, 0.774),
]
#: One clear winner: first on both signals.
_CLEAR_TOP = [
    _hit("vault.kv.list", 0.0328, 0.2, 0.9),
    _hit("vault.kv.get", 0.0322, 0.1, 0.8),
    _hit("vault.kv.put", 0.0164, None, 0.7),
]
#: Ten keyword hits above a hit found by meaning alone, which ranks
#: eleventh. Its meaning score is the best of all hits.
_DEEP_CLOSER = [
    *(_hit(f"vmware.vm.part{n}", 0.033 - n * 0.001, 1.0 - n * 0.05, 0.7) for n in range(10)),
    _hit(_LIST_VMS, 0.0164, None, 0.9),
]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SEARCH_CLEAR_LEAD", raising=False)
    monkeypatch.delenv("SEARCH_PLAIN_SENTENCES", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch_on(monkeypatch: pytest.MonkeyPatch, name: str = "SEARCH_CLEAR_LEAD") -> None:
    monkeypatch.setenv(name, "true")
    get_settings.cache_clear()


async def _outcome(
    monkeypatch: pytest.MonkeyPatch,
    hits: list[OperationSearchHit],
    *,
    limit: int = 10,
) -> tuple[list[OperationSearchHit], str, list[tuple[int, bool]]]:
    """Run the search with a stand-in retriever and return what it asked for.

    The stand-in cuts like the real search: the top ``limit`` hits, plus
    the hit with the best meaning score when ``keep_best_meaning`` is set
    and the cut left it out (:func:`_rrf_fuse`).
    """
    asked: list[tuple[int, bool]] = []

    async def fake_hybrid_search(session: Any, **kwargs: Any) -> list[OperationSearchHit]:
        cut, keep = kwargs["limit"], kwargs["keep_best_meaning"]
        asked.append((cut, keep))
        kept = hits[:cut]
        scored = [h for h in hits if h.cosine_score is not None]
        if keep and scored:
            best = max(scored, key=lambda h: h.cosine_score or 0.0)
            if best not in kept:
                kept.append(best)
        return kept

    monkeypatch.setattr("meho_backplane.operations.meta_tools.hybrid_search", fake_hybrid_search)
    found, outcome, _ = await _run_search(
        AsyncMock(),
        tenant_id=uuid.uuid4(),
        product="vmware",
        version="8.0",
        impl_id="vmware-rest",
        group_key=None,
        query="list virtual machines",
        limit=limit,
    )
    return found, outcome, asked


def test_switch_is_off_by_default() -> None:
    assert search_clear_lead() is False


@pytest.mark.asyncio
async def test_live_search_stays_matched_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits, outcome, asked = await _outcome(monkeypatch, _LIVE_SEARCH)
    assert outcome == "matched"
    assert hits == _LIVE_SEARCH
    assert asked == [(10, False)]


@pytest.mark.asyncio
async def test_live_search_is_ambiguous_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    hits, outcome, _ = await _outcome(monkeypatch, _LIVE_SEARCH)
    assert outcome == "ambiguous"
    # Only the outcome changes: same hits, same order, same scores.
    assert hits == _LIVE_SEARCH


@pytest.mark.asyncio
async def test_clear_top_hit_stays_matched_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    hits, outcome, _ = await _outcome(monkeypatch, _CLEAR_TOP)
    assert outcome == "matched"
    assert hits == _CLEAR_TOP


@pytest.mark.parametrize("limit", [1, 2, 3])
@pytest.mark.asyncio
async def test_small_limit_cannot_hide_the_closer_hit(
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
) -> None:
    _switch_on(monkeypatch)
    hits, outcome, asked = await _outcome(monkeypatch, _LIVE_SEARCH, limit=limit)
    assert outcome == "ambiguous"
    assert hits == _LIVE_SEARCH[:limit]
    assert asked == [(max(limit, SEARCH_CLASSIFY_MIN_HITS), True)]


@pytest.mark.asyncio
async def test_closer_hit_below_tenth_is_found_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default ``limit`` of ten cannot hide the closer hit at rank eleven."""
    _, outcome_off, _ = await _outcome(monkeypatch, _DEEP_CLOSER)
    assert outcome_off == "matched"

    _switch_on(monkeypatch)
    hits, outcome, _ = await _outcome(monkeypatch, _DEEP_CLOSER)
    assert outcome == "ambiguous"
    assert hits == _DEEP_CLOSER[:10]


@pytest.mark.asyncio
async def test_small_limit_reads_the_old_number_of_hits_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, asked = await _outcome(monkeypatch, _LIVE_SEARCH, limit=1)
    assert asked == [(SEARCH_CLASSIFY_MIN_HITS, False)]


@pytest.mark.asyncio
async def test_large_limit_is_kept_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    _, _, asked = await _outcome(monkeypatch, _CLEAR_TOP, limit=30)
    assert asked == [(30, True)]


@pytest.mark.asyncio
async def test_near_tie_stays_ambiguous_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    tied = [_hit("a", 0.05, 2.0, 0.9), _hit("b", 0.05 - 1e-6, 1.0, 0.8)]
    _, outcome, _ = await _outcome(monkeypatch, tied)
    assert outcome == "ambiguous"


@pytest.mark.asyncio
async def test_plain_sentence_top_hit_stays_matched_with_both_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A top hit found by meaning alone leads on meaning, so both switches work together."""
    _switch_on(monkeypatch)
    _switch_on(monkeypatch, "SEARCH_PLAIN_SENTENCES")
    monkeypatch.setattr(
        "meho_backplane.operations.meta_tools.top_hit_holds_a_query_word",
        AsyncMock(return_value=True),
    )
    meaning_only = [_hit("a", 1 / 61, None, 0.9), _hit("b", 1 / 62, None, 0.8)]
    _, outcome, _ = await _outcome(monkeypatch, meaning_only)
    assert outcome == "matched"


def test_one_hit_is_never_unclear(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    assert top_hit_lead_is_unclear([_hit("a", 0.03, 1.0, None)]) is False
    assert top_hit_lead_is_unclear([]) is False


def test_equal_meaning_scores_are_not_unclear(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    assert top_hit_lead_is_unclear([_hit("a", 0.03, 1.0, 0.8), _hit("b", 0.02, 0.5, 0.8)]) is False


def test_top_hit_without_a_meaning_score_is_behind_a_hit_with_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    assert top_hit_lead_is_unclear([_hit("a", 0.03, 1.0, None), _hit("b", 0.02, 0.5, 0.1)]) is True


def test_no_meaning_scores_at_all_say_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    assert (
        top_hit_lead_is_unclear([_hit("a", 0.03, 1.0, None), _hit("b", 0.02, 0.5, None)]) is False
    )


class _Row:
    def __init__(self, score: float | None, row_id: uuid.UUID | None = None) -> None:
        self.id = row_id or uuid.uuid4()
        self.score = score


def test_cut_keeps_the_best_meaning_candidate_below_it() -> None:
    """The real cut: ten keyword hits, and the best meaning candidate ranks eleventh."""
    keyword = [_Row(1.0 - n * 0.05) for n in range(10)]
    meaning_only = _Row(0.9)
    meaning = [meaning_only, *(_Row(0.7 - n * 0.01, row.id) for n, row in enumerate(keyword))]

    plain = _rrf_fuse(keyword, meaning, limit=10)
    kept = _rrf_fuse(keyword, meaning, limit=10, keep_best_meaning=True)

    assert meaning_only.id not in [e.descriptor_id for e in plain]
    # The top ten and their order stay the same; the best meaning
    # candidate comes after them.
    assert [e.descriptor_id for e in kept[:10]] == [e.descriptor_id for e in plain]
    assert kept[10].descriptor_id == meaning_only.id
    assert len(kept) == 11


def test_cut_adds_nothing_when_the_best_meaning_candidate_is_inside() -> None:
    keyword = [_Row(1.0), _Row(0.5)]
    meaning = [keyword[0], _Row(0.4)]
    assert len(_rrf_fuse(keyword, meaning, limit=2, keep_best_meaning=True)) == 2
    assert len(_rrf_fuse(keyword, [], limit=1, keep_best_meaning=True)) == 1
