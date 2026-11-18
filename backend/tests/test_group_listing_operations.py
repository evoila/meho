# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``GROUP_LISTING_OPERATIONS`` switch of ``list_operation_groups`` (#4713).

With the switch on, each group also lists its typed and multi-step
operations (``op_id``, a one-line summary, the risk level, the approval
flag), so a model can walk from the group listing to the contract and the
call without a search query. One group stays under 4 KB and one answer
under 12 KB; a larger listing is paged. With the switch off, the listing is
as before.

The last test builds the listing for every connector MEHO registers at
start-up and fails when a group or an answer is over the limit.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
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
from meho_backplane.api.v1.operations import get_groups
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    EndpointDescriptor,
    OperationGroup,
    OperationPolicyOverride,
)
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.tools._group_operations import group_operations_for_settings
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._group_listing_operations import (
    ANSWER_MAX_BYTES,
    GROUP_MAX_BYTES,
    GROUP_OPERATIONS_LINE,
    GROUP_TEXT_MAX_BYTES,
    SUMMARY_MAX_CHARS,
    add_group_operations,
    compact_size,
    fit_group,
    fit_text,
    group_listing_operations,
    page_groups,
    summary_line,
)
from meho_backplane.operations.meta_tools import (
    call_operation,
    list_operation_groups,
    search_operations,
)
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings

_TENANT_A: UUID = UUID("00000000-0000-0000-0000-0000000000aa")
_TENANT_B: UUID = UUID("00000000-0000-0000-0000-0000000000bb")
_CONNECTOR = "vault-1.x"
_TRIPLE = {"product": "vault", "version": "1.x", "impl_id": "vault"}
_TYPED = "vault.library.item.list"
_COMPOSITE = "vault.composite.library.item.delete"
_RAW = "DELETE:/library/item/{id}"
_RAW_TAG = "POST:/library/item/{id}/tag"


async def _handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Typed handler the seeded rows point at; never run here."""
    return {}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("GROUP_LISTING_OPERATIONS", raising=False)
    monkeypatch.delenv("SEARCH_TYPED_FIRST", raising=False)
    monkeypatch.delenv("AGENT_OPERATION_TOOLS_FROM_MCP", raising=False)
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


def _switch_on(monkeypatch: pytest.MonkeyPatch, *extra: str) -> None:
    for name in ("GROUP_LISTING_OPERATIONS", *extra):
        monkeypatch.setenv(name, "true")
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


async def _group(group_key: str, *, when_to_use: str | None = None) -> UUID:
    group_id = uuid.uuid4()
    async with get_sessionmaker()() as s, s.begin():
        s.add(
            OperationGroup(
                id=group_id,
                tenant_id=None,
                group_key=group_key,
                name=group_key.title(),
                when_to_use=when_to_use or f"Use for {group_key}.",
                review_status="enabled",
                **_TRIPLE,
            )
        )
    return group_id


async def _op(
    op_id: str,
    kind: str,
    summary: str | None,
    group_id: UUID,
    *,
    tenant_id: UUID | None = None,
    enabled: bool = True,
    safety_level: str = "safe",
    requires_approval: bool = False,
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
                handler_ref=None if raw else "tests.test_group_listing_operations._handler",
                summary=summary,
                description=summary,
                group_id=group_id,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level=safety_level,
                requires_approval=requires_approval,
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
async def mixed_connector() -> AsyncIterator[None]:
    """``library`` holds typed, multi-step and raw operations; ``tagging`` only a raw one."""
    library = await _group("library")
    tagging = await _group("tagging")
    await _op(_TYPED, "typed", "List the content library items. Paged by name.", library)
    await _op(
        _COMPOSITE,
        "composite",
        "Delete a content library item safely",
        library,
        safety_level="destructive",
        requires_approval=True,
    )
    await _op(_RAW, "ingested", "Delete a content library item", library)
    await _op(_RAW_TAG, "ingested", "Attach a tag to an item", tagging)
    yield


async def _groups(**extra: Any) -> dict[str, Any]:
    return await list_operation_groups(_operator(), {"connector_id": _CONNECTOR, **extra})


def _listed(answer: dict[str, Any]) -> dict[str, list[str]]:
    return {g["group_key"]: [o["op_id"] for o in g["operations"]] for g in answer["groups"]}


def _output_schema() -> dict[str, Any]:
    schema = mcp_registry._TOOLS["list_operation_groups"][0].outputSchema
    assert schema is not None
    return schema


# ---------------------------------------------------------------------------
# The summary line and the size limits
# ---------------------------------------------------------------------------


def test_switch_is_off_by_default() -> None:
    assert group_listing_operations() is False


@pytest.mark.parametrize(
    ("summary", "line"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("List pods.", "List pods."),
        ("List pods. Filters by label.", "List pods."),
        ("List pods,\n  by   label", "List pods, by label"),
        ("Read the config, e.g. the zone file.", "Read the config, e.g. the zone file."),
    ],
)
def test_summary_line_is_the_first_sentence(summary: str | None, line: str | None) -> None:
    assert summary_line(summary) == line


def test_summary_line_is_cut_to_the_fixed_length() -> None:
    line = summary_line("word " * 60)
    assert line is not None
    assert len(line) == SUMMARY_MAX_CHARS
    assert line.endswith("…")


def _line(n: int, summary_chars: int = SUMMARY_MAX_CHARS) -> dict[str, Any]:
    return {
        "op_id": f"demo.object{n:03d}.list",
        "summary": "s" * summary_chars,
        "safety_level": "safe",
        "requires_approval": False,
    }


def _group_entry(key: str, when_to_use: str = "Use it.") -> dict[str, Any]:
    return {
        "group_key": key,
        "name": key,
        "when_to_use": when_to_use,
        "operation_count": 0,
        "enabled_op_count": 0,
        "partial": False,
    }


def test_fit_group_keeps_all_lines_when_they_fit() -> None:
    fitted = fit_group(_group_entry("small"), [_line(1), _line(2)])
    assert [o["op_id"] for o in fitted["operations"]] == [
        "demo.object001.list",
        "demo.object002.list",
    ]
    assert "operations_more" not in fitted


def test_fit_group_cuts_at_the_limit_and_counts_the_rest() -> None:
    lines = [_line(n) for n in range(60)]
    fitted = fit_group(_group_entry("big"), lines)
    assert compact_size(fitted) < GROUP_MAX_BYTES
    assert 0 < len(fitted["operations"]) < 60
    assert fitted["operations_more"] == 60 - len(fitted["operations"])
    # One line more would not have fit.
    one_more = {**fitted, "operations": lines[: len(fitted["operations"]) + 1]}
    assert compact_size(one_more) >= GROUP_MAX_BYTES


#: The longest group text an operator or the grouping may write: a name of
#: 128 characters and a ``when_to_use`` of 2,048.
_NAME_MAX_CHARS = 128
_WHEN_TO_USE_MAX_CHARS = 2048


def _long_group(key: str, char: str) -> dict[str, Any]:
    return {
        **_group_entry(key, char * _WHEN_TO_USE_MAX_CHARS),
        "group_key": key[:64],
        "name": char * _NAME_MAX_CHARS,
    }


def test_fit_text_keeps_the_longest_ascii_text_whole() -> None:
    group = _long_group("g" * 64, "x")
    assert fit_text(group) == group


@pytest.mark.parametrize("char", ["漢", "é", '"', "\\", "😀"])
def test_fit_text_cuts_a_long_multibyte_text(char: str) -> None:
    group = _long_group("multibyte", char)
    fitted = fit_text(group)
    assert compact_size(fitted) < GROUP_TEXT_MAX_BYTES
    assert fitted["group_key"] == "multibyte"
    assert fitted["when_to_use"].endswith("…")
    assert fitted["when_to_use"].startswith(char)


def test_fit_text_cuts_the_name_when_the_text_alone_is_not_enough() -> None:
    group = {**_group_entry("g", "漢" * 4000), "name": "漢" * 4000}
    fitted = fit_text(group)
    assert compact_size(fitted) < GROUP_TEXT_MAX_BYTES
    assert fitted["name"].endswith("…")


@pytest.mark.parametrize("char", ["x", "漢", "😀"])
def test_fit_group_with_the_longest_text_stays_under_the_limit(char: str) -> None:
    lines = [_line(n) for n in range(60)]
    fitted = fit_group(_long_group("long", char), lines)
    assert compact_size(fitted) < GROUP_MAX_BYTES
    assert fitted["operations"], "the text leaves room for operation lines"
    assert fitted["operations_more"] == 60 - len(fitted["operations"])
    assert compact_size(fit_group(_long_group("long", char), [])) < GROUP_MAX_BYTES


@pytest.mark.parametrize("char", ["x", "漢", "😀"])
def test_an_answer_of_long_groups_stays_under_both_limits(char: str) -> None:
    groups = [_long_group(f"g{n:02d}", char) for n in range(20)]
    answer: dict[str, Any] = {
        "connector_id": _CONNECTOR,
        "groups": groups,
        "next_cursor": None,
    }
    lines = {g["group_key"]: [_line(n) for n in range(40)] for g in groups}
    add_group_operations(answer, lines, group_filter=None)
    assert 0 < len(answer["groups"]) < len(groups)
    assert compact_size(answer) < ANSWER_MAX_BYTES
    for group in answer["groups"]:
        assert compact_size(group) < GROUP_MAX_BYTES
    one = {"connector_id": _CONNECTOR, "groups": groups[:1], "next_cursor": None}
    add_group_operations(one, lines, group_filter="g00")
    assert compact_size(one) < ANSWER_MAX_BYTES


def test_page_groups_stops_under_the_limit_and_always_moves_on() -> None:
    groups = [fit_group(_group_entry(f"g{n:02d}"), [_line(n)] * 20) for n in range(10)]
    answer = {"connector_id": _CONNECTOR}
    count = page_groups(answer, groups)
    assert 0 < count < len(groups)
    page = {**answer, "groups": groups[:count], "next_cursor": groups[count - 1]["group_key"]}
    assert compact_size(page) < ANSWER_MAX_BYTES
    huge = [_group_entry("huge", "x" * ANSWER_MAX_BYTES)]
    assert page_groups(answer, huge) == 1


# ---------------------------------------------------------------------------
# Switch off: nothing changes
# ---------------------------------------------------------------------------


async def test_switch_off_lists_no_operations_and_ignores_group(mixed_connector: None) -> None:
    plain = await _groups()
    assert all("operations" not in g for g in plain["groups"])
    assert "note" not in plain
    assert await _groups(group="library") == plain
    assert await _groups(group="nope") == plain


async def test_switch_on_but_list_operations_false_is_the_old_listing(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    before = await _groups()
    _switch_on(monkeypatch)
    after = await list_operation_groups(
        _operator(), {"connector_id": _CONNECTOR, "group": "library"}, list_operations=False
    )
    assert after == before


# ---------------------------------------------------------------------------
# Switch on: the operation lines
# ---------------------------------------------------------------------------


async def test_each_group_lists_its_typed_and_multi_step_operations(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    _switch_on(monkeypatch)
    answer = await _groups()
    library = answer["groups"][0]
    assert library["operations"] == [
        {
            "op_id": _COMPOSITE,
            "summary": "Delete a content library item safely",
            "safety_level": "destructive",
            "requires_approval": True,
        },
        {
            "op_id": _TYPED,
            "summary": "List the content library items.",
            "safety_level": "safe",
            "requires_approval": False,
        },
    ]
    assert answer["groups"][1] == {**answer["groups"][1], "operations": []}
    assert answer["next_cursor"] is None
    jsonschema.validate(answer, _output_schema())


async def test_an_operation_without_summary_shows_its_op_id_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = await _group("misc")
    await _op("vault.misc.read", "typed", None, group)
    _switch_on(monkeypatch)
    answer = await _groups()
    assert answer["groups"][0]["operations"] == [
        {
            "op_id": "vault.misc.read",
            "summary": None,
            "safety_level": "safe",
            "requires_approval": False,
        }
    ]
    jsonschema.validate(answer, _output_schema())


async def test_disabled_and_other_tenant_operations_are_not_listed(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    async with get_sessionmaker()() as s:
        library = (
            await s.execute(select(OperationGroup.id).where(OperationGroup.group_key == "library"))
        ).scalar_one()
    await _op("vault.library.item.off", "typed", "Turned off", library, enabled=False)
    await _op("vault.library.item.other", "typed", "Tenant B", library, tenant_id=_TENANT_B)
    _switch_on(monkeypatch)
    assert _listed(await _groups())["library"] == [_COMPOSITE, _TYPED]


async def test_the_risk_level_is_the_one_a_call_uses(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    async with get_sessionmaker()() as s, s.begin():
        s.add(
            OperationPolicyOverride(
                tenant_id=_TENANT_A,
                op_id=_TYPED,
                safety_level="dangerous",
                requires_approval=True,
                set_by_sub="admin-a",
                **_TRIPLE,
            )
        )
    _switch_on(monkeypatch)
    lines = {o["op_id"]: o for o in (await _groups())["groups"][0]["operations"]}
    assert lines[_TYPED]["safety_level"] == "dangerous"
    assert lines[_TYPED]["requires_approval"] is True
    found = await search_operations(_operator(), {"connector_id": _CONNECTOR, "op_id": _TYPED})
    assert found["operation"]["safety_level"] == "dangerous"


async def test_with_typed_first_raw_operations_are_never_listed(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    _switch_on(monkeypatch, "SEARCH_TYPED_FIRST")
    assert _listed(await _groups()) == {"library": [_COMPOSITE, _TYPED]}
    with_raw = await _groups(include_raw=True)
    assert _listed(with_raw) == {"library": [_COMPOSITE, _TYPED], "tagging": []}
    assert with_raw["groups"][0]["operation_count"] == 3
    jsonschema.validate(with_raw, _output_schema())


async def test_group_lists_one_group(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    _switch_on(monkeypatch)
    answer = await _groups(group="tagging")
    assert [g["group_key"] for g in answer["groups"]] == ["tagging"]
    assert answer["next_cursor"] is None
    assert "note" not in answer


async def test_unknown_group_says_so(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    _switch_on(monkeypatch)
    answer = await _groups(group="nope")
    assert answer["groups"] == []
    assert "'nope'" in answer["note"]
    assert "without `group`" in answer["note"]
    jsonschema.validate(answer, _output_schema())


async def test_connector_without_its_code_lists_nothing_and_no_note(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    async with get_sessionmaker()() as s, s.begin():
        for row in (await s.execute(select(EndpointDescriptor))).scalars():
            if row.handler_ref:
                row.handler_ref = "tests.no_such_module_here._handler"
    _switch_on(monkeypatch)
    answer = await _groups(group="library")
    assert answer["groups"] == []
    assert "note" not in answer


async def test_a_full_group_counts_what_it_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    group = await _group("big")
    for n in range(60):
        await _op(f"vault.big.object{n:03d}.read", "typed", "Read one object. " + "x" * 120, group)
    _switch_on(monkeypatch)
    answer = await _groups()
    big = answer["groups"][0]
    assert compact_size(big) < GROUP_MAX_BYTES
    assert big["operations_more"] == 60 - len(big["operations"])
    assert big["operations"][0]["op_id"] == "vault.big.object000.read"
    jsonschema.validate(answer, _output_schema())
    hits = await search_operations(
        _operator(),
        {"connector_id": _CONNECTOR, "query": "read object", "group": "big", "limit": 50},
    )
    assert len(hits["hits"]) == 50


async def _walk(**extra: Any) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = await _groups(cursor=cursor, **extra)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 50, "paging does not end"


async def test_a_large_connector_is_paged_within_the_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """74 typed operations with long summaries in 8 groups, like a big vendor connector."""
    expected: set[str] = set()
    for g in range(8):
        group = await _group(f"group{g}", when_to_use="Pick this group when ... " * 20)
        for n in range(74 // 8 + (1 if g < 74 % 8 else 0)):
            op_id = f"vault.group{g}.object{n:02d}.list"
            expected.add(op_id)
            await _op(op_id, "composite", "List the objects of this kind. " + "y" * 120, group)
    assert len(expected) == 74
    _switch_on(monkeypatch)
    pages = await _walk()
    assert len(pages) > 1
    listed: set[str] = set()
    for page in pages:
        jsonschema.validate(page, _output_schema())
        assert compact_size(page) < ANSWER_MAX_BYTES
        for group in page["groups"]:
            assert compact_size(group) < GROUP_MAX_BYTES
            assert "operations_more" not in group
            listed.update(o["op_id"] for o in group["operations"])
    assert listed == expected
    keys = [g["group_key"] for page in pages for g in page["groups"]]
    assert keys == sorted(set(keys))


async def test_a_small_limit_still_pages_by_group_count(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    _switch_on(monkeypatch)
    first = await _groups(limit=1)
    assert [g["group_key"] for g in first["groups"]] == ["library"]
    assert first["next_cursor"] == "library"
    second = await _groups(limit=1, cursor="library")
    assert [g["group_key"] for g in second["groups"]] == ["tagging"]


# ---------------------------------------------------------------------------
# From the listing to the contract to a call, without a search query
# ---------------------------------------------------------------------------


async def test_walk_from_listing_to_contract_without_a_query(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    _switch_on(monkeypatch)
    listing = await _groups()
    picked = listing["groups"][0]["operations"][1]["op_id"]
    contract = await search_operations(_operator(), {"connector_id": _CONNECTOR, "op_id": picked})
    assert contract["operation"]["op_id"] == picked
    assert "parameter_schema" in contract["operation"]
    called = await call_operation(
        _operator(), {"connector_id": _CONNECTOR, "op_id": picked, "params": {}}
    )
    assert called["status"] == "ok", called


# ---------------------------------------------------------------------------
# MCP tool text, REST and hosted runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
def test_tool_text_follows_the_switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("GROUP_LISTING_OPERATIONS", "true" if on else "false")
    get_settings.cache_clear()
    registered = mcp_registry._TOOLS["list_operation_groups"][0]
    shown = group_operations_for_settings(registered)
    item = shown.outputSchema["properties"]["groups"]["items"]["properties"]  # type: ignore[index]
    assert ("group" in shown.inputSchema["properties"]) is on
    assert ("operations" in item) is on
    assert ("operations_more" in item) is on
    assert ("note" in shown.outputSchema["properties"]) is on  # type: ignore[index]
    assert shown.description.count(GROUP_OPERATIONS_LINE) == (1 if on else 0)
    assert group_operations_for_settings(shown).description == shown.description


async def test_rest_route_passes_group(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    _switch_on(monkeypatch)
    listed = await get_groups(
        connector_id=_CONNECTOR,
        limit=100,
        cursor=None,
        include_raw=False,
        group="library",
        operator=_operator(),
    )
    assert listed == await _groups(group="library")
    assert _listed(listed) == {"library": [_COMPOSITE, _TYPED]}


def _ctx() -> RunContext[Operator]:
    return RunContext(deps=_operator(), model=TestModel(), usage=RunUsage())


async def test_hosted_hand_written_tool_keeps_the_old_listing(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    """The hand-written copy has no ``cursor`` input, so it gets no operation lines."""
    _switch_on(monkeypatch)
    tools = {t.name: t for t in resolve_agent_tools({}, _operator())}
    answer = await tools["list_operation_groups"].function(_ctx(), connector_id=_CONNECTOR)
    assert all("operations" not in g for g in answer["groups"])
    with pytest.raises(ModelRetry, match="invalid arguments"):
        await tools["list_operation_groups"].function(
            _ctx(), connector_id=_CONNECTOR, group="library"
        )


async def test_hosted_tool_from_mcp_follows_the_switch(
    monkeypatch: pytest.MonkeyPatch, mixed_connector: None
) -> None:
    monkeypatch.setenv("AGENT_OPERATION_TOOLS_FROM_MCP", "true")
    get_settings.cache_clear()
    tools = {t.name: t for t in resolve_agent_tools({}, _operator())}
    with pytest.raises(ModelRetry, match="invalid arguments"):
        await tools["list_operation_groups"].function(
            _ctx(), connector_id=_CONNECTOR, group="library"
        )
    _switch_on(monkeypatch)
    tools = {t.name: t for t in resolve_agent_tools({}, _operator())}
    answer = await tools["list_operation_groups"].function(
        _ctx(), connector_id=_CONNECTOR, group="library"
    )
    assert _listed(answer) == {"library": [_COMPOSITE, _TYPED]}


# ---------------------------------------------------------------------------
# Every connector MEHO registers at start-up fits the limits
# ---------------------------------------------------------------------------


def _stub_registrar_embeddings() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def test_every_registered_connector_fits_the_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each answer stays under 12 KB and each group under 4 KB; every typed op is listed whole."""
    await run_typed_op_registrars(embedding_service=_stub_registrar_embeddings())
    async with get_sessionmaker()() as s:
        rows = (
            await s.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.tenant_id.is_(None),
                    EndpointDescriptor.source_kind.in_(("typed", "composite")),
                    EndpointDescriptor.is_enabled.is_(True),
                )
            )
        ).scalars()
        expected: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            expected[f"{row.impl_id}-{row.version}"].add(row.op_id)
    assert len(expected) > 20
    _switch_on(monkeypatch)
    for connector_id, op_ids in expected.items():
        listed: set[str] = set()
        cursor: str | None = None
        while True:
            page = await list_operation_groups(
                _operator(), {"connector_id": connector_id, "cursor": cursor}
            )
            assert compact_size(page) < ANSWER_MAX_BYTES, connector_id
            for group in page["groups"]:
                assert compact_size(group) < GROUP_MAX_BYTES, (connector_id, group["group_key"])
                assert "operations_more" not in group, (connector_id, group["group_key"])
                listed.update(o["op_id"] for o in group["operations"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert listed == op_ids, connector_id
