# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The order text of ``search_knowledge`` (``KB_WIKI_INTERLEAVE``).

Off (the default): the tool text and the cut text are the same as before.
On: the text says the hits come in turns, and the cut text says the hits at
the end of the list were left out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.tools._kb_wiki_order_text import (
    INTERLEAVED_ORDER,
    PARTIAL_IN_TURNS,
    PLAIN_ORDER,
    PLAIN_PARTIAL,
)
from meho_backplane.mcp.tools._short_tool_texts import SEARCH_KNOWLEDGE_SHORT
from meho_backplane.mcp.tools.knowledge import _bounded_search_response
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

#: The bytes of the full and short ``search_knowledge`` entries of ``tools/list``
#: on main (switch off), saved exactly as main sent them. A change to the text,
#: or to the order of the fields, shows here.
_FIXTURES = Path(__file__).parent / "fixtures"

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


def _tools_list_bytes(client: TestClient) -> bytes:
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return bytes(response.content)


def _main_entry_bytes(short: bool) -> bytes:
    """The bytes of the ``tools/list`` entry as main sent it, saved before this change."""
    name = f"search_knowledge_tools_list_main_{'short' if short else 'full'}.json"
    saved = (_FIXTURES / name).read_bytes()
    return saved.removesuffix(b"\n")


def _has_whole_entry(body: bytes, entry: bytes) -> bool:
    """*entry* is one whole item of the tool list in *body*, byte for byte."""
    return any(start + entry + end in body for start in (b"[", b",") for end in (b",", b"]"))


def _json_text(text: str) -> bytes:
    """*text* as it is written inside a JSON string of the answer."""
    return json.dumps(text, ensure_ascii=False)[1:-1].encode("utf-8")


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
    saved = _main_entry_bytes(short)
    assert saved.startswith(b'{"name":"search_knowledge",')
    assert _has_whole_entry(_tools_list_bytes(client_with_operator[0]), saved)


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("short", [False, True])
def test_switch_on_only_the_order_and_cut_words_change(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    short: bool,
) -> None:
    _switch(monkeypatch, interleave=True, short=short)
    expected = _main_entry_bytes(short).replace(
        _json_text(PLAIN_ORDER), _json_text(INTERLEAVED_ORDER)
    )
    expected = expected.replace(_json_text(PLAIN_PARTIAL), _json_text(PARTIAL_IN_TURNS))
    assert expected != _main_entry_bytes(short)
    assert _has_whole_entry(_tools_list_bytes(client_with_operator[0]), expected)


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
