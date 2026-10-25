# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the shared paged property read (``vim_retrieve``, #4862).

A fake VI-JSON seam splits a many-object answer into pages the way vCenter
may (``tests._vim_pages.PagedVim``). No test calls a vCenter. All ids are
made up.
"""

from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest import vim_retrieve
from meho_backplane.connectors.vmware_rest.vim_body import retrieve_properties_body
from meho_backplane.connectors.vmware_rest.vim_retrieve import (
    TooManyPagesError,
    retrieve_all_pages,
)
from tests._vim_pages import PagedVim
from tests._vmware_teardown_fake import operator


class _Vim(PagedVim):
    """Answers every ``RetrievePropertiesEx`` with the same objects, paged."""

    def __init__(self, objects: list[dict[str, Any]]) -> None:
        super().__init__()
        self.objects = objects
        #: ``(method, body, promote)`` of every call.
        self.calls: list[tuple[str, Any, bool]] = []

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
        session_used: Any = None,
        session_token: str | None = None,
    ) -> Any:
        self.calls.append(
            (path.rsplit("/", 1)[-1], copy.deepcopy(json), promote_managed_object_not_found)
        )
        self._record_session(session_used)
        if path.endswith("/ContinueRetrievePropertiesEx"):
            return self._continue(json["token"], session_token)
        assert path == "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
        return self._serve_objects(copy.deepcopy(self.objects))


def _obj(moid: str) -> dict[str, Any]:
    return {
        "obj": {"type": "Datastore", "value": moid},
        "propSet": [{"name": "name", "val": f"name-{moid}"}],
    }


_OBJECTS = [_obj("ds-1"), _obj("ds-2"), _obj("ds-3")]
_BODY = retrieve_properties_body("Datastore", ["ds-1", "ds-2", "ds-3"], ["name"])


async def _read(vim: _Vim, **kwargs: Any) -> dict[str, Any]:
    return await retrieve_all_pages(vim, object(), operator(), _BODY, **kwargs)  # type: ignore[arg-type]


async def test_one_page_needs_no_continuation_call() -> None:
    vim = _Vim(_OBJECTS)
    out = await _read(vim)
    assert out == {"objects": _OBJECTS}
    assert [method for method, _, _ in vim.calls] == ["RetrievePropertiesEx"]
    assert vim.continue_calls == []


async def test_promote_missing_goes_to_the_first_call_only() -> None:
    vim = _Vim(_OBJECTS)
    vim.page_size = 2
    await _read(vim, promote_missing=True)
    assert [(method, promote) for method, _, promote in vim.calls] == [
        ("RetrievePropertiesEx", True),
        ("ContinueRetrievePropertiesEx", False),
    ]


async def test_three_pages_give_every_object_in_order() -> None:
    vim = _Vim(_OBJECTS)
    vim.page_size = 1
    out = await _read(vim)
    assert out == {"objects": _OBJECTS}
    # Each token goes once, to the next call, on the session of the first call.
    assert [token for token, _ in vim.continue_calls] == ["<token-1>", "<token-2>"]
    assert {session for _, session in vim.continue_calls} == {"<session-1>"}
    assert [body for method, body, _ in vim.calls if method == "ContinueRetrievePropertiesEx"] == [
        {"token": "<token-1>"},
        {"token": "<token-2>"},
    ]
    assert vim._pending == {}


async def test_a_boxed_answer_is_read_the_same_way() -> None:
    """A ``{"value": ...}`` box around a page (the legacy mount) is unwrapped."""

    class _Boxed(_Vim):
        def _serve(self, pages: list[list[dict[str, Any]]]) -> dict[str, Any]:
            return {"value": super()._serve(pages)}

    vim = _Boxed(_OBJECTS)
    vim.page_size = 2
    out = await _read(vim)
    assert out == {"objects": _OBJECTS}
    assert len(vim.continue_calls) == 1


async def test_a_failed_page_fails_the_read_with_no_partial_answer() -> None:
    vim = _Vim(_OBJECTS)
    vim.page_size = 2
    vim.fail_continue = httpx.ConnectError("page lost")
    with pytest.raises(httpx.ConnectError):
        await _read(vim)


async def test_an_answer_that_never_ends_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vim_retrieve, "_MAX_PAGES", 3)
    vim = _Vim(_OBJECTS)
    vim.endless_pages = True
    with pytest.raises(TooManyPagesError, match="had not ended after 3 pages"):
        await _read(vim)
    # Three pages were read (one first call, two continuations); the fourth is refused.
    assert len(vim.continue_calls) == 2


def test_the_page_limit_error_is_a_runtime_error() -> None:
    """Callers that handle a failed call with ``except RuntimeError`` handle it too."""
    assert issubclass(TooManyPagesError, RuntimeError)
