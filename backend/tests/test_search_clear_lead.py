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

from meho_backplane.operations._search_clear_lead import (
    SEARCH_CLEAR_LEAD_MIN_HITS,
    clear_lead_min_hits,
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
) -> tuple[list[OperationSearchHit], str, list[int]]:
    """Run the search with a stand-in retriever and return what it asked for."""
    asked: list[int] = []

    async def fake_hybrid_search(session: Any, **kwargs: Any) -> list[OperationSearchHit]:
        asked.append(kwargs["limit"])
        return hits[: kwargs["limit"]]

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
    assert clear_lead_min_hits() == 0


@pytest.mark.asyncio
async def test_live_search_stays_matched_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits, outcome, asked = await _outcome(monkeypatch, _LIVE_SEARCH)
    assert outcome == "matched"
    assert hits == _LIVE_SEARCH
    assert asked == [10]


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
    assert asked == [SEARCH_CLEAR_LEAD_MIN_HITS]


@pytest.mark.asyncio
async def test_small_limit_reads_the_old_number_of_hits_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, asked = await _outcome(monkeypatch, _LIVE_SEARCH, limit=1)
    assert asked == [SEARCH_CLASSIFY_MIN_HITS]


@pytest.mark.asyncio
async def test_large_limit_is_kept_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    _, _, asked = await _outcome(monkeypatch, _CLEAR_TOP, limit=30)
    assert asked == [30]


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
