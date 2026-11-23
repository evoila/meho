# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The order text of ``search_knowledge`` (``KB_WIKI_INTERLEAVE``).

Off (the default): the tool text and the cut text are the same as before.
On: the text says the hits come in turns, and the cut text says the hits at
the end of the list were left out.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp import registry
from meho_backplane.mcp.short_texts import with_short_text
from meho_backplane.mcp.tools._kb_wiki_order_text import (
    INTERLEAVED_ORDER,
    PARTIAL_IN_TURNS,
    PLAIN_ORDER,
    PLAIN_PARTIAL,
)
from meho_backplane.mcp.tools._kb_word_hint import with_word_hint
from meho_backplane.mcp.tools._short_tool_texts import SEARCH_KNOWLEDGE_SHORT
from meho_backplane.mcp.tools.knowledge import _bounded_search_response
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_OLD_FULL_ORDER = "Returns one hit per entry, best first: slug, snippet, "
_OLD_SHORT_ORDER = "Returns one hit per entry, best first, with `passages`"
_TURNS = "store hits and knowledge repo hits in turns (each list best first)"
_OLD_CUT = (
    "2 lower-ranked hits were left out to keep this response under 200 bytes. "
    "Narrow the query or add filters to find them."
)


def _switch(monkeypatch: pytest.MonkeyPatch, *, interleave: bool, short: bool) -> None:
    monkeypatch.setenv("KB_WIKI_INTERLEAVE", "true" if interleave else "false")
    monkeypatch.setenv("MCP_SHORT_TOOL_TEXTS", "true" if short else "false")
    get_settings.cache_clear()


def _entry(client: TestClient) -> dict[str, Any]:
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = response.json()["result"]["tools"]
    return dict(next(t for t in tools if t["name"] == "search_knowledge"))


def _description(client: TestClient) -> str:
    return str(_entry(client)["description"])


def _main_entry(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The ``tools/list`` entry with the text chain of main (no order text)."""
    found = registry.get_tool("search_knowledge")
    assert found is not None
    definition, handler = found
    main_chain = with_word_hint(with_short_text(SEARCH_KNOWLEDGE_SHORT))
    monkeypatch.setitem(
        registry._TOOLS,
        "search_knowledge",
        (definition.model_copy(update={"for_settings": main_chain}), handler),
    )
    entry = _entry(client)
    monkeypatch.setitem(registry._TOOLS, "search_knowledge", found)
    return entry


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("short", [False, True])
def test_switch_off_the_description_says_best_first_as_before(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    short: bool,
) -> None:
    _switch(monkeypatch, interleave=False, short=short)
    text = _description(client_with_operator[0])
    assert (_OLD_SHORT_ORDER if short else _OLD_FULL_ORDER) in text
    assert "in turns" not in text
    if short:
        assert text.endswith(SEARCH_KNOWLEDGE_SHORT.description)


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("short", [False, True])
def test_switch_off_the_whole_entry_is_the_same_as_on_main(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    short: bool,
) -> None:
    _switch(monkeypatch, interleave=False, short=short)
    client = client_with_operator[0]
    assert _entry(client) == _main_entry(client, monkeypatch)


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("short", [False, True])
def test_switch_on_only_the_order_and_cut_words_change(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    short: bool,
) -> None:
    _switch(monkeypatch, interleave=True, short=short)
    client = client_with_operator[0]
    entry = _entry(client)
    main = _main_entry(client, monkeypatch)
    expected = str(main["description"]).replace(PLAIN_ORDER, INTERLEAVED_ORDER)
    expected = expected.replace(PLAIN_PARTIAL, PARTIAL_IN_TURNS)
    assert entry == {**main, "description": expected}


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_switch_on_the_full_text_agrees_with_the_cut_answer(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, interleave=True, short=False)
    text = _description(client_with_operator[0])
    assert PARTIAL_IN_TURNS in text
    assert "low hits" not in text
    hits = [{"slug": f"s{n}", "pad": "x" * 100} for n in range(4)]
    remediation = _bounded_search_response(hits, 200, in_turns=True)["remediation"]
    assert "hits at the end of the list were left out" in remediation


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("short", [False, True])
def test_switch_on_the_description_names_the_turns_and_not_best_first(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    short: bool,
) -> None:
    _switch(monkeypatch, interleave=True, short=short)
    text = _description(client_with_operator[0])
    assert _TURNS in text
    assert "one hit per entry, best first" not in text
    assert text.count("best first") == 1


def test_switch_off_the_cut_text_is_unchanged() -> None:
    hits = [{"slug": f"s{n}", "pad": "x" * 100} for n in range(4)]
    response = _bounded_search_response(hits, 200)
    assert response["delivery"] == "partial"
    assert response["remediation"] == _OLD_CUT.replace(
        "2 lower", f"{response['omitted_hits']} lower"
    )


def test_switch_on_the_cut_text_says_the_hits_at_the_end_were_left_out() -> None:
    hits = [{"slug": f"s{n}", "pad": "x" * 100} for n in range(4)]
    response = _bounded_search_response(hits, 200, in_turns=True)
    assert response["delivery"] == "partial"
    assert response["remediation"].startswith(
        f"{response['omitted_hits']} hits at the end of the list were left out"
    )
    assert "lower-ranked" not in response["remediation"]
