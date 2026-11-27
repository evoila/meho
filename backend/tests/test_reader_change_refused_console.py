# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A Reader in the web console who asks for a change is refused at once.

The console answers ``403`` with the code ``insufficient_role`` and the
plain sentence, the same as REST and MCP. Nothing is stored and nothing
runs. A preview and the pages that only read keep their answer
``operator_required``.

A walk over every console route that does not only read, from the route
table, checks that a Reader session stores no approval request on any of
them, and that every route behind the ``operator`` door gives the code and
the sentence. The REST and MCP doors are in
``test_reader_change_refused.py``.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.op_classes import declare_op_classes
from meho_backplane.operations.grant_approval import register_rights_change_op
from meho_backplane.operations.role_floors import READER_CHANGE_CODE, READER_CHANGE_SENTENCE
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.middleware import require_ui_operator, require_ui_operator_for_call
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from tests import _composite_wait_audit_helpers as h
from tests._route_tree_helpers import iter_routes
from tests.test_ui_operations import (
    _TENANT_A,
    _bff_env,
    _build_app,
    _client_with_role_and_csrf,
    _csrf_headers,
    _make_keypair_and_jwks,
    _mint_token,
    _mock_discovery_and_jwks,
    _patch_call_operation,
    _seed_group_and_op,
    _seed_session_sync,
    _seed_tenant,
    _wait_for_the_answer,
)

__all__ = ["_bff_env", "_wait_for_the_answer"]

_BODY = {"detail": READER_CHANGE_CODE, "message": READER_CHANGE_SENTENCE}

#: Stand-in operations for the console call form; the door looks them up.
_SAFE_READ = "vault.reader.console_read"
_CAUTION_WRITE = "vault.reader.console_write"
_UNKNOWN_OP = "vault.reader.no_such_operation"

#: kind -> (op_id, risk level, approval flag) of each stand-in that asks
#: for a change. The multi-step operation is a composite.
_CHANGE_KINDS: dict[str, tuple[str, str, bool]] = {
    "write_safe": ("vault.reader.console_write_safe", "safe", False),
    "write_caution": (_CAUTION_WRITE, "caution", False),
    "write_dangerous": ("vault.reader.console_write_dangerous", "dangerous", False),
    "delete": ("vault.reader.console_delete", "destructive", True),
    "rights_change": ("vault.reader.console_rights_change", "safe", False),
    "credential_write": ("vault.reader.console_credential_write", "safe", False),
    "multi_step": ("vault.reader.console_multi_step", "caution", False),
}
_PLAIN = {"detail": "operator_required"}

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_PATH_PARAM = re.compile(r"\{[^}]+\}")


@pytest.fixture
def reader() -> Iterator[tuple[TestClient, str]]:
    """A console session of a person with the Reader role, and its CSRF token."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _mint_token(
        keypair, sub="reader-person", tenant_id=str(_TENANT_A), tenant_role="read_only"
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=token, operator_sub="reader-person"
    )
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    mock = respx.mock(assert_all_called=False, assert_all_mocked=False)
    _mock_discovery_and_jwks(mock, jwks)
    with mock:
        yield client, csrf


def _approval_requests() -> int:
    async def _count() -> int:
        async with get_sessionmaker()() as session:
            query = select(func.count()).select_from(ApprovalRequest)
            return int((await session.execute(query)).scalar_one())

    return asyncio.run(_count())


def _seed_stand_ins() -> None:
    declare_op_classes(
        credential_write=[_CHANGE_KINDS["credential_write"][0]],
        write=[_CHANGE_KINDS["write_safe"][0]],
    )
    register_rights_change_op(_CHANGE_KINDS["rights_change"][0])
    typed = [(_SAFE_READ, "safe", False)]
    typed += [spec for kind, spec in _CHANGE_KINDS.items() if kind != "multi_step"]
    for op_id, level, needs_approval in typed:
        _seed_group_and_op(
            tenant_id=None,
            product="vault",
            version="1.x",
            impl_id="vault",
            group_key=f"group-{op_id}",
            group_name=f"Group {op_id}",
            op_id=op_id,
            safety_level=level,
            requires_approval=needs_approval,
        )

    async def _composite() -> None:
        op_id, level, _ = _CHANGE_KINDS["multi_step"]
        async with get_sessionmaker()() as session:
            await h.register_composite(
                session,
                op_id,
                "multi_step_composite",
                safety_level=level,
                module="tests.test_reader_change_controls",
            )

    asyncio.run(_composite())


def _call_form(op_id: str) -> dict[str, str]:
    return {"connector_id": "vault-1.x", "op_id": op_id, "target": "", "params": "{}"}


def test_a_reader_console_run_of_each_kind_gets_the_code_and_the_sentence(
    monkeypatch: pytest.MonkeyPatch, reader: tuple[TestClient, str]
) -> None:
    """The call form looks the operation up before it answers a Reader."""
    client, csrf = reader
    received = _patch_call_operation(monkeypatch, {"status": "ok", "result": {}, "extras": {}})
    _seed_stand_ins()
    cases: list[tuple[str, dict[str, str], dict[str, str]]] = [
        (kind, _call_form(op_id), _BODY) for kind, (op_id, _level, _flag) in _CHANGE_KINDS.items()
    ]
    cases += [
        ("safe_read", _call_form(_SAFE_READ), _PLAIN),
        ("unknown_op", _call_form(_UNKNOWN_OP), _PLAIN),
        ("empty_form", {}, _PLAIN),
    ]

    for name, form, body in cases:
        response = client.post("/ui/operations/call", headers=_csrf_headers(csrf), data=form)
        assert (response.status_code, response.json()) == (403, body), (name, response.text)
        assert _approval_requests() == 0, name
        assert received == [], name
    assert len(cases) == 10


def _nested(levels: int) -> str:
    return "[" * levels + "]" * levels


#: ``params`` of the call form that the role check cannot read, or that are
#: controls next to them: (``params``, Reader answer, Operator answer). An
#: answer is (status, ``detail`` or the first error type, or ``None``).
_NESTED_PARAMS: dict[str, tuple[str, tuple[int, str | None], tuple[int, str | None]]] = {
    "160,000 levels": (_nested(160_000), (403, "operator_required"), (422, "string_too_long")),
    "100,000 levels": (_nested(100_000), (403, "operator_required"), (422, "string_too_long")),
    "50,000 levels": (_nested(50_000), (403, "operator_required"), (422, "string_too_long")),
    "2,000 levels": (_nested(2_000), (403, "operator_required"), (400, None)),
    "200,000 levels, over the form limit": (
        _nested(200_000),
        (400, "Field exceeded maximum size of 1024KB."),
        (400, "Field exceeded maximum size of 1024KB."),
    ),
}


def _answer(response: Any) -> tuple[int, str | None]:
    if "json" not in response.headers.get("content-type", ""):
        return response.status_code, None
    detail = response.json().get("detail")
    if isinstance(detail, list):
        return response.status_code, str(detail[0]["type"])
    return response.status_code, detail


def _nested_form(params: str) -> dict[str, str]:
    return {"connector_id": "vault-1.x", "op_id": "made-up.op", "target": "", "params": params}


@pytest.mark.parametrize("case", list(_NESTED_PARAMS))
def test_a_reader_console_call_form_the_role_check_cannot_read_gets_the_plain_answer(
    monkeypatch: pytest.MonkeyPatch, reader: tuple[TestClient, str], case: str
) -> None:
    client, csrf = reader
    received = _patch_call_operation(monkeypatch, {"status": "ok", "result": {}, "extras": {}})
    params, expected, _ = _NESTED_PARAMS[case]

    response = client.post(
        "/ui/operations/call", headers=_csrf_headers(csrf), data=_nested_form(params)
    )

    assert _answer(response) == expected, response.text[:300]
    assert received == []
    assert _approval_requests() == 0


@pytest.mark.parametrize("case", list(_NESTED_PARAMS))
@pytest.mark.usefixtures("_wait_for_the_answer")
def test_an_operator_console_call_form_with_the_same_params_keeps_its_answer(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    received = _patch_call_operation(monkeypatch, {"status": "ok", "result": {}, "extras": {}})
    params, _, expected = _NESTED_PARAMS[case]
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub="operator-person", role=TenantRole.OPERATOR
    )

    with mock:
        response = client.post(
            "/ui/operations/call", headers=_csrf_headers(csrf), data=_nested_form(params)
        )

    assert _answer(response) == expected, response.text[:300]
    assert received == []
    assert _approval_requests() == 0


def test_a_reader_console_preview_keeps_its_answer(
    monkeypatch: pytest.MonkeyPatch, reader: tuple[TestClient, str]
) -> None:
    client, csrf = reader
    received = _patch_call_operation(monkeypatch, {"status": "ok", "result": {}, "extras": {}})

    response = client.post(
        "/ui/operations/preview",
        headers=_csrf_headers(csrf),
        data=_call_form(_CAUTION_WRITE),
    )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "operator_required"}
    assert received == []


def _calls(dependant: Dependant) -> Iterator[Any]:
    for sub in dependant.dependencies:
        yield sub.call
        yield from _calls(sub)


def _change_routes() -> list[tuple[str, str, APIRoute]]:
    """Every console route that does not only read, from the route table."""
    found = []
    for route in iter_routes(_build_app().routes):
        if not isinstance(route, APIRoute) or not route.path.startswith("/ui"):
            continue
        for method in sorted(route.methods - _READ_METHODS):
            found.append((method, _PATH_PARAM.sub(str(uuid.uuid4()), route.path), route))
    return found


def _walk(client: TestClient, csrf: str) -> list[str]:
    """Send every change route as a Reader; return what went wrong."""
    problems: list[str] = []
    for method, path, route in _change_routes():
        before = _approval_requests()
        response = client.request(method, path, headers=_csrf_headers(csrf), data={})
        if _approval_requests() != before:
            problems.append(f"{method} {route.path}: a Reader stored an approval request")
        doors = set(_calls(route.dependant))
        # The walk sends an empty form: the call form names no operation.
        expected = _BODY if require_ui_operator in doors else None
        if require_ui_operator_for_call in doors:
            expected = _PLAIN
        if expected and (response.status_code, response.json()) != (403, expected):
            problems.append(f"{method} {route.path}: {response.status_code} {response.text[:120]}")
    return problems


def test_the_walk_over_every_console_change_route(
    monkeypatch: pytest.MonkeyPatch, reader: tuple[TestClient, str]
) -> None:
    client, csrf = reader
    received = _patch_call_operation(monkeypatch, {"status": "ok", "result": {}, "extras": {}})
    assert len(_change_routes()) > 50

    assert _walk(client, csrf) == []
    assert received == []


def test_the_console_walk_reads_the_operator_door_of_each_route() -> None:
    """The walk finds the routes behind the ``operator`` door, so its check can fail."""
    behind = [
        r for _m, _p, r in _change_routes() if require_ui_operator in set(_calls(r.dependant))
    ]
    call_door = [
        r.path
        for _m, _p, r in _change_routes()
        if require_ui_operator_for_call in set(_calls(r.dependant))
    ]
    assert len(behind) > 10
    assert call_door == ["/ui/operations/call"]
    assert all(route.path != "/ui/operations/preview" for route in behind)


#: The console ``POST`` routes that only read: a Reader gets ``operator_required``.
_READ_POSTS = (
    ("/ui/corpus/search", {"q": "made-up question"}),
    ("/ui/corpus/chunks/made-up-collection/made-up-chunk", {"read_handle": "made-up-handle"}),
    ("/ui/kb/editor-preview", {"body": "# Made-up text"}),
    ("/ui/kb/search", {"q": "made-up question"}),
)


@pytest.mark.parametrize(("path", "form"), _READ_POSTS)
def test_a_reader_console_post_that_only_reads_keeps_its_answer(
    reader: tuple[TestClient, str], path: str, form: dict[str, str]
) -> None:
    client, csrf = reader

    response = client.post(path, headers=_csrf_headers(csrf), data=form)

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "operator_required"}
