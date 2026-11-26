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

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations.role_floors import READER_CHANGE_CODE, READER_CHANGE_SENTENCE
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.middleware import require_ui_operator
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from tests._route_tree_helpers import iter_routes
from tests.test_ui_operations import (
    _TENANT_A,
    _bff_env,
    _build_app,
    _csrf_headers,
    _make_keypair_and_jwks,
    _mint_token,
    _mock_discovery_and_jwks,
    _patch_call_operation,
    _seed_session_sync,
    _seed_tenant,
    _wait_for_the_answer,
)

__all__ = ["_bff_env", "_wait_for_the_answer"]

_BODY = {"detail": READER_CHANGE_CODE, "message": READER_CHANGE_SENTENCE}

#: One op id per kind of change; the console door refuses before it reads them.
_KIND_OPS = (
    "vault.reader.write_caution",
    "vault.reader.write_dangerous",
    "vault.reader.delete",
    "vault.reader.rights_change",
    "vault.reader.credential_write",
    "vault.reader.multi_step",
)

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


def test_a_reader_console_run_of_each_kind_gets_the_code_and_the_sentence(
    monkeypatch: pytest.MonkeyPatch, reader: tuple[TestClient, str]
) -> None:
    client, csrf = reader
    received = _patch_call_operation(monkeypatch, {"status": "ok", "result": {}, "extras": {}})

    for op_id in _KIND_OPS:
        response = client.post(
            "/ui/operations/call",
            headers=_csrf_headers(csrf),
            data={"connector_id": "vault-1.x", "op_id": op_id, "target": "", "params": "{}"},
        )
        assert response.status_code == 403, (op_id, response.text)
        assert response.json() == _BODY

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
        data={"connector_id": "vault-1.x", "op_id": _KIND_OPS[0], "target": "", "params": "{}"},
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
        behind_operator_door = require_ui_operator in set(_calls(route.dependant))
        if behind_operator_door and (response.status_code, response.json()) != (403, _BODY):
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
    assert any(route.path == "/ui/operations/call" for route in behind)
    assert all(route.path != "/ui/operations/preview" for route in behind)
