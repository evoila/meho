# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Search filters for knowledge and memory: time, product and version, status, tags, scope.

Unit tests of the wiring: each filter of ``search_knowledge`` and
``search_memory`` reaches the service, the service hands it to the
retriever, and the retriever binds it into both candidate SQL
statements. The SQL itself runs against PostgreSQL in
``tests/integration/test_search_filters_pg.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.kb.service import KbService
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools._search_filters import FILTER_PROPERTIES, FILTER_TOOL_TEXT
from meho_backplane.mcp.tools.knowledge import _search_knowledge_handler
from meho_backplane.mcp.tools.memory import _search_memory_handler
from meho_backplane.memory.service import MemoryService
from meho_backplane.retrieval.retriever import (
    _BM25_CANDIDATES_SQL,
    _COSINE_CANDIDATES_SQL,
    _TIME_WINDOW_PREDICATE_SQL,
    RetrievalHit,
    _bm25_candidates,
    _cosine_candidates,
    retrieve,
)
from meho_backplane.retrieval.time_window import TimeWindow, parse_time_limit
from meho_backplane.settings import get_settings

OWNER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
AUG_1 = datetime(2026, 8, 1, tzinfo=UTC)
NOV_1 = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KB_SHARED_TENANT_IDS", str(OWNER))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(tenant_id: uuid.UUID = OTHER) -> Operator:
    return Operator(
        sub="op-1",
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


def _hit(tenant_id: uuid.UUID, slug: str, score: float, **metadata: Any) -> RetrievalHit:
    ts = datetime(2026, 11, 7, tzinfo=UTC)
    return RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=tenant_id,
        source="kb",
        source_id=slug,
        kind="kb-entry",
        body=f"body of {slug}",
        doc_metadata=metadata,
        created_at=ts,
        updated_at=ts,
        fused_score=score,
        bm25_score=None,
        cosine_score=score,
        bm25_rank=None,
        cosine_rank=1,
    )


# --- the time window --------------------------------------------------------


def test_a_date_alone_means_midnight_utc() -> None:
    assert parse_time_limit("confirmed_after", "2026-08-01") == AUG_1


def test_a_time_without_an_offset_is_utc_and_an_offset_is_kept() -> None:
    assert parse_time_limit("x", "2026-08-01T10:00:00") == datetime(2026, 8, 1, 10, tzinfo=UTC)
    with_offset = parse_time_limit("x", "2026-08-01T10:00:00+02:00")
    assert with_offset == datetime(2026, 8, 1, 8, tzinfo=UTC)
    assert with_offset is not None
    assert with_offset.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize("value", ["last week", "2026-13-01", "", 20260801, True])
def test_a_time_limit_that_is_not_a_date_is_refused(value: object) -> None:
    with pytest.raises(ValueError, match="ISO 8601"):
        parse_time_limit("confirmed_after", value)


def test_no_time_limit_is_none() -> None:
    assert parse_time_limit("confirmed_after", None) is None


@pytest.mark.parametrize("field", ["confirmed", "updated"])
def test_after_later_than_before_is_refused(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field}_after is later than {field}_before"):
        TimeWindow(**{f"{field}_after": NOV_1, f"{field}_before": AUG_1})


def test_the_same_time_for_after_and_before_is_allowed() -> None:
    assert not TimeWindow(confirmed_after=AUG_1, confirmed_before=AUG_1).is_empty()


def test_a_time_without_a_zone_is_refused_in_the_window() -> None:
    with pytest.raises(ValueError, match="time zone"):
        TimeWindow(updated_after=datetime(2026, 8, 1))


def test_an_empty_window_binds_only_nulls() -> None:
    window = TimeWindow()
    assert window.is_empty()
    assert window.binds() == {
        "confirmed_after": None,
        "confirmed_before": None,
        "updated_after": None,
        "updated_before": None,
    }


# --- the retriever ----------------------------------------------------------


def test_both_candidate_statements_hold_the_time_window_predicate() -> None:
    predicate = " ".join(_TIME_WINDOW_PREDICATE_SQL.split())
    for statement in (_BM25_CANDIDATES_SQL, _COSINE_CANDIDATES_SQL):
        assert predicate in " ".join(str(statement.text).split())


def test_the_confirmed_check_cannot_abort_on_a_bad_value() -> None:
    # The shape regex and pg_input_is_valid run before any cast.
    sql = _TIME_WINDOW_PREDICATE_SQL
    assert sql.index("pg_input_is_valid") < sql.index("CAST(metadata ->> 'confirmed_at' AS")
    assert "WHEN metadata ->> 'confirmed_at' IS NULL THEN NULL" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", [_bm25_candidates, _cosine_candidates])
async def test_the_window_is_bound_into_each_candidate_query(helper: Any) -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(all=lambda: []))
    window = TimeWindow(confirmed_after=AUG_1, updated_before=NOV_1)
    await helper(session, OWNER, "q", "kb", None, None, None, None, time_window=window)
    binds = session.execute.await_args.args[1]
    assert binds["confirmed_after"] == AUG_1
    assert binds["confirmed_before"] is None
    assert binds["updated_after"] is None
    assert binds["updated_before"] == NOV_1


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", [_bm25_candidates, _cosine_candidates])
async def test_no_window_binds_nulls(helper: Any) -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(all=lambda: []))
    await helper(session, OWNER, "q", "kb", None, None, None, None)
    binds = session.execute.await_args.args[1]
    assert [binds[k] for k in TimeWindow().binds()] == [None, None, None, None]


@pytest.mark.asyncio
async def test_retrieve_hands_the_same_window_to_both_signals() -> None:
    seen: list[TimeWindow | None] = []

    async def fake(*args: Any, time_window: TimeWindow | None = None, **_: Any) -> list[Any]:
        seen.append(time_window)
        return []

    embedding = MagicMock()
    embedding.encode_one = AsyncMock(return_value=[0.0] * 384)
    window = TimeWindow(updated_after=AUG_1)
    with (
        patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=embedding),
        patch("meho_backplane.retrieval.retriever._bm25_candidates", side_effect=fake),
        patch("meho_backplane.retrieval.retriever._cosine_candidates", side_effect=fake),
    ):
        await retrieve(OWNER, "q", source="kb", session=MagicMock(), time_window=window)
    assert seen == [window, window]


# --- the knowledge service: scope and source tenant -------------------------


def _fake_retrieve(calls: list[dict[str, Any]]) -> Any:
    async def fake(**kwargs: Any) -> list[RetrievalHit]:
        calls.append(kwargs)
        if kwargs["tenant_id"] == OWNER:
            return [_hit(OWNER, "company-note", 0.9, shared=True)]
        return [_hit(OTHER, "own-note", 0.5)]

    return fake


@pytest.mark.asyncio
async def test_a_second_tenant_finds_the_shared_note_marked_with_its_tenant() -> None:
    calls: list[dict[str, Any]] = []
    with patch("meho_backplane.kb.service.retrieve", side_effect=_fake_retrieve(calls)):
        hits = await KbService().search_entries(OTHER, "snapshot")
    assert [(h.slug, h.tenant_id, h.shared) for h in hits] == [
        ("company-note", OWNER, True),
        ("own-note", OTHER, False),
    ]


@pytest.mark.asyncio
async def test_scope_tenant_searches_only_the_own_tenant() -> None:
    calls: list[dict[str, Any]] = []
    with patch("meho_backplane.kb.service.retrieve", side_effect=_fake_retrieve(calls)):
        hits = await KbService().search_entries(OTHER, "snapshot", scope="tenant")
    assert [c["tenant_id"] for c in calls] == [OTHER]
    assert [h.slug for h in hits] == ["own-note"]


@pytest.mark.asyncio
async def test_scope_shared_from_a_tenant_that_shares_nothing_searches_only_owners() -> None:
    calls: list[dict[str, Any]] = []
    with patch("meho_backplane.kb.service.retrieve", side_effect=_fake_retrieve(calls)):
        hits = await KbService().search_entries(OTHER, "snapshot", scope="shared")
    assert [c["tenant_id"] for c in calls] == [OWNER]
    assert calls[0]["metadata_filters"] == {"shared": True}
    assert [h.slug for h in hits] == ["company-note"]


@pytest.mark.asyncio
async def test_scope_shared_from_the_owner_asks_its_own_rows_for_shared_only() -> None:
    calls: list[dict[str, Any]] = []
    with patch("meho_backplane.kb.service.retrieve", side_effect=_fake_retrieve(calls)):
        await KbService().search_entries(OWNER, "snapshot", filters={"team": "x"}, scope="shared")
    assert [(c["tenant_id"], c["metadata_filters"]) for c in calls] == [
        (OWNER, {"team": "x", "shared": True})
    ]


@pytest.mark.asyncio
async def test_scope_shared_with_a_shared_false_filter_searches_nothing() -> None:
    calls: list[dict[str, Any]] = []
    with patch("meho_backplane.kb.service.retrieve", side_effect=_fake_retrieve(calls)):
        hits = await KbService().search_entries(
            OWNER, "snapshot", filters={"shared": False}, scope="shared"
        )
    assert calls == []
    assert hits == []


@pytest.mark.asyncio
async def test_an_owner_that_is_no_longer_listed_is_not_marked_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_SHARED_TENANT_IDS", "")
    get_settings.cache_clear()
    calls: list[dict[str, Any]] = []
    with patch("meho_backplane.kb.service.retrieve", side_effect=_fake_retrieve(calls)):
        hits = await KbService().search_entries(OWNER, "snapshot")
    assert [(h.slug, h.shared) for h in hits] == [("company-note", False)]


@pytest.mark.asyncio
async def test_the_time_window_reaches_the_own_and_the_shared_search() -> None:
    calls: list[dict[str, Any]] = []
    window = TimeWindow(confirmed_after=AUG_1)
    with patch("meho_backplane.kb.service.retrieve", side_effect=_fake_retrieve(calls)):
        await KbService().search_entries(OTHER, "snapshot", time_window=window)
    assert [c["time_window"] for c in calls] == [window, window]


# --- the search_knowledge tool ----------------------------------------------


@pytest.mark.asyncio
async def test_search_knowledge_passes_each_filter_to_the_service() -> None:
    search = AsyncMock(return_value=[])
    with patch.object(KbService, "search_entries", search):
        await _search_knowledge_handler(
            _operator(),
            {
                "query": "snapshot",
                "filters": {"kind": "kb-entry", "status": "candidate"},
                "product": "vcenter",
                "version": "9.0",
                "status": "confirmed",
                "tags": ["vm", "snapshot"],
                "confirmed_after": "2026-08-01",
                "confirmed_before": "2026-11-01",
                "updated_after": "2026-08-01T00:00:00Z",
                "updated_before": "2026-11-01",
                "scope": "shared",
            },
        )
    kwargs = search.await_args.kwargs
    # A named filter wins over the same key in ``filters``.
    assert kwargs["filters"] == {
        "kind": "kb-entry",
        "status": "confirmed",
        "product": "vcenter",
        "version": "9.0",
        "tags": ["vm", "snapshot"],
    }
    assert kwargs["time_window"] == TimeWindow(
        confirmed_after=AUG_1,
        confirmed_before=NOV_1,
        updated_after=AUG_1,
        updated_before=NOV_1,
    )
    assert kwargs["scope"] == "shared"


@pytest.mark.asyncio
async def test_search_knowledge_without_filters_changes_nothing() -> None:
    search = AsyncMock(return_value=[])
    with patch.object(KbService, "search_entries", search):
        await _search_knowledge_handler(_operator(), {"query": "snapshot", "scope": None})
    kwargs = search.await_args.kwargs
    assert kwargs["filters"] is None
    assert kwargs["time_window"] is None
    assert kwargs["scope"] == "all"


@pytest.mark.asyncio
async def test_search_knowledge_hit_shows_its_source_tenant() -> None:
    calls: list[dict[str, Any]] = []
    with patch("meho_backplane.kb.service.retrieve", side_effect=_fake_retrieve(calls)):
        result = await _search_knowledge_handler(_operator(), {"query": "snapshot"})
    first = result["hits"][0]
    assert first["slug"] == "company-note"
    assert first["tenant_id"] == str(OWNER)
    assert first["shared"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"confirmed_after": "last week"},
        {"updated_after": "2026-11-01", "updated_before": "2026-08-01"},
    ],
)
async def test_search_knowledge_refuses_a_bad_time_filter(bad: dict[str, Any]) -> None:
    search = AsyncMock(return_value=[])
    with (
        patch.object(KbService, "search_entries", search),
        pytest.raises(McpInvalidParamsError, match="search_knowledge"),
    ):
        await _search_knowledge_handler(_operator(), {"query": "q", **bad})
    search.assert_not_awaited()


# --- the search_memory tool and service --------------------------------------


@pytest.mark.asyncio
async def test_search_memory_passes_each_filter_to_the_service() -> None:
    search = AsyncMock(return_value=[])
    with patch.object(MemoryService, "search_memories", search):
        await _search_memory_handler(
            _operator(),
            {
                "query": "snapshot",
                "product": "vcenter",
                "version": "9.0",
                "status": "confirmed",
                "tags": ["vm"],
                "confirmed_after": "2026-08-01",
                "updated_before": "2026-11-01",
            },
        )
    kwargs = search.await_args.kwargs
    assert kwargs["metadata_filters"] == {
        "product": "vcenter",
        "version": "9.0",
        "status": "confirmed",
        "tags": ["vm"],
    }
    assert kwargs["time_window"] == TimeWindow(confirmed_after=AUG_1, updated_before=NOV_1)


@pytest.mark.asyncio
async def test_search_memory_without_filters_changes_nothing() -> None:
    search = AsyncMock(return_value=[])
    with patch.object(MemoryService, "search_memories", search):
        await _search_memory_handler(_operator(), {"query": "snapshot", "tags": None})
    kwargs = search.await_args.kwargs
    assert kwargs["metadata_filters"] is None
    assert kwargs["time_window"] is None


@pytest.mark.asyncio
async def test_search_memory_refuses_a_bad_time_filter() -> None:
    with pytest.raises(McpInvalidParamsError, match="search_memory"):
        await _search_memory_handler(_operator(), {"query": "q", "confirmed_before": "2026-02-30"})


@pytest.mark.asyncio
async def test_memory_service_hands_the_filters_to_the_eligible_retrieve() -> None:
    window = TimeWindow(confirmed_after=AUG_1)
    with patch(
        "meho_backplane.memory.service.retrieve", new=AsyncMock(return_value=[])
    ) as retrieve_mock:
        await MemoryService().search_memories(
            _operator(),
            "q",
            metadata_filters={"status": "confirmed", "tags": ["vm"]},
            time_window=window,
        )
    kwargs = retrieve_mock.await_args.kwargs
    assert kwargs["metadata_filters"] == {"status": "confirmed", "tags": ["vm"]}
    assert kwargs["time_window"] == window
    # The eligibility rule is still applied: a filter only narrows.
    assert kwargs["memory_eligibility"].principal_sub == "op-1"


# --- the input schemas -------------------------------------------------------


@pytest.mark.parametrize("tool", ["search_knowledge", "search_memory"])
def test_both_tools_take_each_filter(tool: str) -> None:
    found = get_tool(tool)
    assert found is not None
    properties = found[0].inputSchema["properties"]
    assert set(FILTER_PROPERTIES) <= set(properties)
    # The tool text names the filters.
    assert FILTER_TOOL_TEXT in found[0].description


@pytest.mark.parametrize(
    "bad",
    [{"status": "maybe"}, {"tags": []}, {"tags": [""]}, {"product": ""}, {"version": 9.0}],
)
def test_the_filter_schema_refuses_bad_values(bad: dict[str, Any]) -> None:
    schema = {"type": "object", "properties": FILTER_PROPERTIES}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_the_knowledge_scope_schema_refuses_an_unknown_scope() -> None:
    found = get_tool("search_knowledge")
    assert found is not None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"query": "q", "scope": "everyone"}, found[0].inputSchema)
