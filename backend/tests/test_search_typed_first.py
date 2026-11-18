# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``SEARCH_TYPED_FIRST`` switch of the discovery meta-tools (#4712).

With the switch on, ``search_operations`` and ``list_operation_groups``
show typed and multi-step operations by default. Raw operations (made from
a vendor API description) come back with ``include_raw``, or when the
connector has nothing else. With the switch off, both answer as before.

The rows are seeded directly with their ``source_kind`` and no embedding,
so the SQLite search ranks by the words of the query only.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import jsonschema
import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import select

from meho_backplane.agent.toolset import resolve_agent_tools
from meho_backplane.api.v1.operations import get_groups, get_search
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, TenantOperationSetting
from meho_backplane.mcp.tools._include_raw import include_raw_for_settings
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._search_typed_first import (
    INCLUDE_RAW_LINE,
    connector_has_typed_operations,
    search_typed_first,
)
from meho_backplane.operations.meta_tools import list_operation_groups, search_operations
from meho_backplane.settings import get_settings

_TENANT_A: UUID = UUID("00000000-0000-0000-0000-0000000000aa")
_TENANT_B: UUID = UUID("00000000-0000-0000-0000-0000000000bb")
_CONNECTOR = "vault-1.x"
_TRIPLE = {"product": "vault", "version": "1.x", "impl_id": "vault"}
_COMPOSITE = "vault.composite.library.item.delete"
_TYPED = "vault.library.item.list"
_RAW_DELETE = "DELETE:/library/item/{id}"
_RAW_TAG = "POST:/library/item/{id}/tag"


async def _handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Typed handler the seeded rows point at; never run here."""
    return {}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SEARCH_TYPED_FIRST", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture(autouse=True)
def _stub_embeddings(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_TYPED_FIRST", "true")
    get_settings.cache_clear()


def _operator(tenant_id: UUID = _TENANT_A) -> Operator:
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _group(group_key: str, *, tenant_id: UUID | None = None) -> UUID:
    group_id = uuid.uuid4()
    async with get_sessionmaker()() as s, s.begin():
        s.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
                group_key=group_key,
                name=group_key.title(),
                when_to_use=f"Use for {group_key}.",
                review_status="enabled",
                **_TRIPLE,
            )
        )
    return group_id


async def _op(
    op_id: str,
    kind: str,
    summary: str,
    group_id: UUID | None,
    *,
    tenant_id: UUID | None = None,
    enabled: bool = True,
) -> None:
    raw = kind == "ingested"
    async with get_sessionmaker()() as s, s.begin():
        s.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                op_id=op_id,
                source_kind=kind,
                method=op_id.split(":", 1)[0] if raw else None,
                path=op_id.split(":", 1)[1] if raw else None,
                handler_ref=None if raw else "tests.test_search_typed_first._handler",
                summary=summary,
                description=summary,
                group_id=group_id,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="caution" if raw else "safe",
                requires_approval=False,
                is_enabled=enabled,
                embedding=None,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                **_TRIPLE,
            )
        )


@pytest.fixture
async def mixed_connector() -> AsyncIterator[dict[str, UUID]]:
    """A connector with typed, multi-step and raw operations.

    ``library`` holds all three kinds. ``tagging`` holds only a raw one.
    """
    library = await _group("library")
    tagging = await _group("tagging")
    await _op(_COMPOSITE, "composite", "Delete a content library item safely", library)
    await _op(_TYPED, "typed", "List the content library items", library)
    await _op(_RAW_DELETE, "ingested", "Delete a content library item", library)
    await _op(_RAW_TAG, "ingested", "Attach a tag to an item", tagging)
    yield {"library": library, "tagging": tagging}


async def _search(query: str, **extra: Any) -> dict[str, Any]:
    return await search_operations(
        _operator(), {"connector_id": _CONNECTOR, "query": query, **extra}
    )


async def _groups(**extra: Any) -> dict[str, Any]:
    return await list_operation_groups(_operator(), {"connector_id": _CONNECTOR, **extra})


def _ids(answer: dict[str, Any]) -> list[str]:
    return [hit["op_id"] for hit in answer["hits"]]


def test_switch_is_off_by_default() -> None:
    assert search_typed_first() is False


# ---------------------------------------------------------------------------
# Switch off: nothing changes
# ---------------------------------------------------------------------------


async def test_switch_off_search_shows_raw_and_ignores_the_flag(
    mixed_connector: dict[str, UUID],
) -> None:
    plain = await _search("delete content library item")
    flagged = await _search("delete content library item", include_raw=True)
    assert _RAW_DELETE in _ids(plain)
    assert "raw_operations" not in plain
    flagged.pop("query_duration_ms")
    plain.pop("query_duration_ms")
    assert flagged == plain


async def test_switch_off_groups_count_every_operation(
    mixed_connector: dict[str, UUID],
) -> None:
    answer = await _groups()
    counts = {g["group_key"]: g["operation_count"] for g in answer["groups"]}
    assert counts == {"library": 3, "tagging": 1}
    assert "raw_operations" not in answer
    assert await _groups(include_raw=True) == answer


# ---------------------------------------------------------------------------
# Hosted agent runs with the hand-written tools (the default form)
# ---------------------------------------------------------------------------


def _hosted_tools() -> dict[str, Any]:
    assert get_settings().agent_operation_tools_from_mcp is False
    return {t.name: t for t in resolve_agent_tools({}, _operator())}


def _ctx() -> RunContext[Operator]:
    return RunContext(deps=_operator(), model=TestModel(), usage=RunUsage())


async def test_hosted_switch_off_tools_take_no_include_raw(
    mixed_connector: dict[str, UUID],
) -> None:
    tools = _hosted_tools()
    with pytest.raises(ModelRetry, match="invalid arguments"):
        await tools["search_operations"].function(
            _ctx(), connector_id=_CONNECTOR, query="delete content library item", include_raw=True
        )
    answer = await tools["search_operations"].function(
        _ctx(), connector_id=_CONNECTOR, query="delete content library item"
    )
    assert _RAW_DELETE in _ids(answer)
    assert "raw_operations" not in answer


async def test_hosted_retry_with_include_raw_shows_raw(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: dict[str, UUID]
) -> None:
    """The note names ``include_raw``; the hosted tool must take it (#4712)."""
    _switch_on(monkeypatch)
    tools = _hosted_tools()
    first = await tools["search_operations"].function(
        _ctx(), connector_id=_CONNECTOR, query="attach tag"
    )
    assert "include_raw" in first["raw_operations"]["note"]
    retry = await tools["search_operations"].function(
        _ctx(), connector_id=_CONNECTOR, query="attach tag", include_raw=True
    )
    assert _RAW_TAG in _ids(retry)
    assert retry["raw_operations"]["included"] is True
    groups = await tools["list_operation_groups"].function(
        _ctx(), connector_id=_CONNECTOR, include_raw=True
    )
    assert {g["group_key"] for g in groups["groups"]} == {"library", "tagging"}


# ---------------------------------------------------------------------------
# Switch on: search
# ---------------------------------------------------------------------------


async def test_search_hides_raw_and_puts_the_multi_step_operation_first(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: dict[str, UUID]
) -> None:
    _switch_on(monkeypatch)
    answer = await _search("delete content library item")
    assert _ids(answer)[0] == _COMPOSITE
    assert _RAW_DELETE not in _ids(answer)
    assert answer["raw_operations"] == {"included": False, "hidden_matches": 0, "note": None}


async def test_search_with_include_raw_shows_raw(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: dict[str, UUID]
) -> None:
    _switch_on(monkeypatch)
    answer = await _search("delete content library item", include_raw=True)
    assert _RAW_DELETE in _ids(answer)
    assert answer["raw_operations"] == {"included": True, "hidden_matches": 0, "note": None}


async def test_search_says_how_many_raw_matches_were_left_out(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: dict[str, UUID]
) -> None:
    """Only a raw operation matches: no dead end, the answer names the flag."""
    _switch_on(monkeypatch)
    answer = await _search("attach tag")
    assert answer["hits"] == []
    assert answer["outcome"] == "no_match"
    block = answer["raw_operations"]
    assert block["included"] is False
    assert block["hidden_matches"] == 1
    assert "include_raw: true" in block["note"]


async def test_search_in_a_group_of_raw_operations_only(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: dict[str, UUID]
) -> None:
    _switch_on(monkeypatch)
    answer = await _search("attach tag", group="tagging")
    assert answer["hits"] == []
    assert answer["raw_operations"]["hidden_matches"] == 1
    shown = await _search("attach tag", group="tagging", include_raw=True)
    assert _ids(shown) == [_RAW_TAG]


async def test_search_on_a_connector_with_only_raw_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    group = await _group("library")
    await _op(_RAW_DELETE, "ingested", "Delete a content library item", group)
    answer = await _search("delete library item")
    assert _ids(answer) == [_RAW_DELETE]
    block = answer["raw_operations"]
    assert block["included"] is True
    assert "no typed or multi-step operations" in block["note"]


async def test_a_disabled_typed_operation_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed op the caller cannot use leaves the connector raw-only."""
    _switch_on(monkeypatch)
    group = await _group("library")
    await _op(_TYPED, "typed", "List the content library items", group, enabled=False)
    await _op(_RAW_DELETE, "ingested", "Delete a content library item", group)
    answer = await _search("delete library item")
    assert _ids(answer) == [_RAW_DELETE]
    assert answer["raw_operations"]["included"] is True


async def test_a_typed_operation_turned_off_for_the_tenant_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    group = await _group("library")
    await _op(_TYPED, "typed", "List the content library items", group)
    async with get_sessionmaker()() as s, s.begin():
        s.add(
            TenantOperationSetting(
                tenant_id=_TENANT_A,
                op_id=_TYPED,
                enabled=False,
                set_by_sub="admin-test",
                set_at=datetime.now(UTC),
                **_TRIPLE,
            )
        )
    async with get_sessionmaker()() as s:
        assert not await connector_has_typed_operations(s, tenant_id=_TENANT_A, **_TRIPLE)
        assert await connector_has_typed_operations(s, tenant_id=_TENANT_B, **_TRIPLE)


async def test_another_tenants_typed_operation_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant B's own typed row does not hide tenant A's raw rows."""
    _switch_on(monkeypatch)
    group = await _group("library")
    await _op(_RAW_DELETE, "ingested", "Delete a content library item", group)
    other = await _group("library", tenant_id=_TENANT_B)
    await _op(_COMPOSITE, "composite", "Delete a library item", other, tenant_id=_TENANT_B)
    answer = await _search("delete library item")
    assert _ids(answer) == [_RAW_DELETE]
    assert answer["raw_operations"]["included"] is True


async def test_exact_op_mode_still_finds_a_raw_operation(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: dict[str, UUID]
) -> None:
    _switch_on(monkeypatch)
    answer = await search_operations(
        _operator(), {"connector_id": _CONNECTOR, "op_id": _RAW_DELETE}
    )
    assert answer["operation"]["op_id"] == _RAW_DELETE
    assert "raw_operations" not in answer


# ---------------------------------------------------------------------------
# Switch on: group listing
# ---------------------------------------------------------------------------


async def test_groups_count_typed_operations_and_hide_raw_only_groups(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: dict[str, UUID]
) -> None:
    _switch_on(monkeypatch)
    answer = await _groups()
    assert [
        (g["group_key"], g["operation_count"], g["enabled_op_count"]) for g in answer["groups"]
    ] == [("library", 2, 2)]
    block = answer["raw_operations"]
    assert block["included"] is False
    assert block["hidden_groups"] == 1
    assert "include_raw: true" in block["note"]


async def test_groups_with_include_raw_count_everything(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: dict[str, UUID]
) -> None:
    _switch_on(monkeypatch)
    answer = await _groups(include_raw=True)
    counts = {g["group_key"]: g["operation_count"] for g in answer["groups"]}
    assert counts == {"library": 3, "tagging": 1}
    assert answer["raw_operations"] == {"included": True, "hidden_groups": 0, "note": None}


async def test_groups_keep_paging_with_raw_only_groups_left_out(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: dict[str, UUID]
) -> None:
    """A page of one skips the raw-only group, and the last page ends the list."""
    _switch_on(monkeypatch)
    extra = await _group("zones")
    await _op("vault.zone.list", "typed", "List zones", extra)
    first = await _groups(limit=1)
    assert [g["group_key"] for g in first["groups"]] == ["library"]
    second = await _groups(limit=1, cursor=first["next_cursor"])
    assert [g["group_key"] for g in second["groups"]] == ["zones"]
    third = await _groups(limit=1, cursor=second["next_cursor"])
    assert third["groups"] == []
    assert third["next_cursor"] is None


async def test_groups_on_a_connector_with_only_raw_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    group = await _group("library")
    await _op(_RAW_DELETE, "ingested", "Delete a content library item", group)
    answer = await _groups()
    assert [(g["group_key"], g["operation_count"]) for g in answer["groups"]] == [("library", 1)]
    assert answer["raw_operations"]["included"] is True
    assert "no typed or multi-step operations" in answer["raw_operations"]["note"]


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("include_raw", [False, True])
async def test_rest_routes_pass_include_raw(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: dict[str, UUID], include_raw: bool
) -> None:
    """``GET /search`` and ``GET /groups`` answer like the MCP tools."""
    _switch_on(monkeypatch)
    found = await get_search(
        connector_id=_CONNECTOR,
        q="delete content library item",
        query=None,
        group=None,
        limit=10,
        op_id=None,
        include_scores=False,
        include_raw=include_raw,
        operator=_operator(),
    )
    assert (_RAW_DELETE in _ids(found)) is include_raw
    assert found["raw_operations"]["included"] is include_raw
    listed = await get_groups(
        connector_id=_CONNECTOR,
        limit=100,
        cursor=None,
        include_raw=include_raw,
        operator=_operator(),
    )
    keys = [g["group_key"] for g in listed["groups"]]
    assert keys == (["library", "tagging"] if include_raw else ["library"])


# ---------------------------------------------------------------------------
# Tool text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("on", [False, True])
def test_tool_text_follows_the_switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    from meho_backplane.mcp import registry as mcp_registry

    if on:
        _switch_on(monkeypatch)
    for name in ("search_operations", "list_operation_groups"):
        registered = mcp_registry._TOOLS[name][0]
        shown = include_raw_for_settings(registered)
        assert ("include_raw" in shown.inputSchema["properties"]) is on
        assert (INCLUDE_RAW_LINE in shown.description) is on
        assert ("raw_operations" in str(shown.outputSchema)) is on
        if on:
            assert shown == registered


async def test_stored_rows_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: dict[str, UUID]
) -> None:
    """Only what discovery shows changes: no row is written or turned off."""
    _switch_on(monkeypatch)
    await _search("delete content library item")
    await _groups()
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(EndpointDescriptor.is_enabled))).scalars().all()
    assert rows == [True, True, True, True]


@pytest.mark.parametrize("include_raw", [False, True])
async def test_answers_match_the_registered_output_schema(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: dict[str, UUID], include_raw: bool
) -> None:
    """With the switch on, both answers match the output schema, also with a null note."""
    from meho_backplane.mcp import registry as mcp_registry

    _switch_on(monkeypatch)
    answers = {
        "search_operations": await _search("attach tag", include_raw=include_raw),
        "list_operation_groups": await _groups(include_raw=include_raw),
    }
    for name, answer in answers.items():
        schema = mcp_registry._TOOLS[name][0].outputSchema
        jsonschema.validate(answer, schema)
