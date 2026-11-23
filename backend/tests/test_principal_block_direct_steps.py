# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A request that a multi-step operation sends straight to the target checks the block list.

Some multi-step operations call the target through the connector's own
session, not through ``dispatch_child`` or a governed step. The HTTP
connector base checks the block list before each such request. Covered:

* through the real dispatcher: a multi-step operation sends one request,
  the person is blocked, and its next request is not sent. The run ends
  ``denied`` with ``principal_blocked``; with an unreadable list it ends
  ``error`` with ``principal_block_check_unavailable``;
* ``gh.composite.branch_commit_files`` on the real GitHub connector: the
  person is blocked after the commit is created, and the branch move is
  never sent;
* once a request is refused, no later request of the run is sent, also when
  the handler catches the refusal and the list can be read again;
* outside a multi-step operation the base sends the request without a
  block-list read (the top-level call checked the list already).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit_query import AuditQueryFilters, query_audit
from meho_backplane.auth import principal_block
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import (
    PrincipalBlockedStepError,
    block_principal,
    checking_logins_for_steps,
    reset_block_cache,
)
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.github.composites._git import branch_commit_files_composite
from meho_backplane.connectors.github.connector import GitHubRestConnector
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings

from . import _composite_wait_audit_helpers as h

pytestmark = pytest.mark.asyncio

_MODULE = "tests.test_principal_block_direct_steps"
_DIRECT_OP_ID = "vault.composite.direct_requests"
_BASE_SHA = "a" * 40
_NEW_COMMIT_SHA = "e" * 40

#: Every request that reached the target, as ``"<METHOD> <path>"``.
sent: list[str] = []
#: What happens after the first request (set by each test).
after_first_request: list[Callable[[], Awaitable[None]]] = []
#: What happens before the first request (set by each test).
before_first_request: list[Callable[[], Awaitable[None]]] = []
#: What happens after the handler caught a refusal (set by each test).
after_refusal: list[Callable[[], Awaitable[None]]] = []


class _DirectConnector(HttpConnector):
    """An HTTP connector whose session answers every request with ``{}``."""

    product = "vault"

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        return {}

    async def _http_client(self, target: Any) -> httpx.AsyncClient:
        async def _answer(request: httpx.Request) -> httpx.Response:
            sent.append(f"{request.method} {request.url.path}")
            return httpx.Response(200, json={})

        return httpx.AsyncClient(
            base_url="https://target.test", transport=httpx.MockTransport(_answer)
        )

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


_TARGET = SimpleNamespace(name="t", host="target.test", port=443)


async def direct_requests_composite(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """Two requests through the connector's own session, the hook between them."""
    connector = _DirectConnector()
    for hook in before_first_request:
        await hook()
    await connector._post_json(_TARGET, "/things", operator=operator, json={"n": 1})
    for hook in after_first_request:
        await hook()
    await connector._post_json(_TARGET, "/things/2", operator=operator, json={"n": 2})
    return {"done": True}


async def catching_composite(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """The same requests; the handler catches the error and sends one more request."""
    try:
        await direct_requests_composite(operator, target, params)
    except Exception:
        for hook in after_refusal:
            await hook()
    connector = _DirectConnector()
    try:
        await connector._post_json(_TARGET, "/things/3", operator=operator, json={"n": 3})
    except Exception:
        return {"done": False}
    return {"done": True}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_block_cache()
    clear_registry()
    sent.clear()
    after_first_request.clear()
    before_first_request.clear()
    after_refusal.clear()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_block_cache()
    clear_registry()
    sent.clear()
    after_first_request.clear()
    before_first_request.clear()
    after_refusal.clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s_:
        s_.add(Tenant(id=h.TENANT_ID, slug="block-direct", name="Block Direct"))
        await s_.commit()
        yield s_


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = h.EMBEDDING
    service.encode.return_value = [h.EMBEDDING]
    service.dimension = 384
    return service


def _admin() -> Operator:
    return Operator(
        sub="admin-person",
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=h.TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


async def _block_requester() -> None:
    await block_principal(_admin(), h.operator().sub, "left the team")


async def _unblock_requester() -> None:
    await principal_block.unblock_principal(_admin(), h.operator().sub)


def _unreadable(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[None]]:
    async def _broken(_sessionmaker: Any) -> frozenset[tuple[str, str]]:
        raise RuntimeError("database down")

    async def _break_now() -> None:
        monkeypatch.setattr("meho_backplane.auth.principal_block._load_entries", _broken)
        reset_block_cache()

    return _break_now


def _readable(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[None]]:
    original = principal_block._load_entries

    async def _restore_now() -> None:
        monkeypatch.setattr("meho_backplane.auth.principal_block._load_entries", original)
        reset_block_cache()

    return _restore_now


def _assert_part_may_have_run(result: OperationResult) -> None:
    """The answer after an earlier request was sent."""
    assert result.extras["execution_outcome"] == "uncertain"
    assert result.extras["retry"] is False
    assert "A part of the operation may have run." in str(result.error)
    assert "did not run" not in str(result.error)
    assert "try again" not in str(result.error).lower()


async def test_direct_request_of_a_blocked_person_is_not_sent(
    session: AsyncSession, embedding: AsyncMock
) -> None:
    await h.register_child(embedding)
    await h.register_composite(session, _DIRECT_OP_ID, "direct_requests_composite", module=_MODULE)
    after_first_request.append(_block_requester)

    result = await h.run(_DIRECT_OP_ID)

    assert sent == ["POST /things"]
    assert result.status == "denied", result
    assert result.extras["error_code"] == "principal_blocked"
    _assert_part_may_have_run(result)


async def test_audit_row_of_a_refused_direct_request_says_denied(
    session: AsyncSession, embedding: AsyncMock
) -> None:
    await h.register_child(embedding)
    await h.register_composite(session, _DIRECT_OP_ID, "direct_requests_composite", module=_MODULE)
    after_first_request.append(_block_requester)

    result = await h.run(_DIRECT_OP_ID)

    (row,) = await h.rows(_DIRECT_OP_ID)
    assert row.id == result.audit_id
    assert row.payload["result_status"] == "denied"
    assert row.status_code == 403
    assert row.payload["error"]["error_code"] == "principal_blocked"
    async with get_sessionmaker()() as fresh:
        found = await query_audit(
            AuditQueryFilters(result_status="denied"), tenant_id=h.TENANT_ID, session=fresh
        )
    assert [entry.id for entry in found.rows] == [row.id]


@pytest.mark.parametrize("case", ["blocked", "unreadable"])
async def test_first_direct_request_refused_means_nothing_started(
    session: AsyncSession, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    await h.register_child(embedding)
    await h.register_composite(session, _DIRECT_OP_ID, "direct_requests_composite", module=_MODULE)
    before_first_request.append(_block_requester if case == "blocked" else _unreadable(monkeypatch))

    result = await h.run(_DIRECT_OP_ID)

    assert sent == []
    assert result.status == ("denied" if case == "blocked" else "error"), result
    assert result.extras["execution_outcome"] == "not_started"
    assert ("Try again" in str(result.error)) is (case == "unreadable")


@pytest.mark.parametrize("case", ["blocked", "unreadable"])
async def test_refused_direct_request_stops_a_handler_that_catches_errors(
    session: AsyncSession, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    await h.register_child(embedding)
    await h.register_composite(session, _DIRECT_OP_ID, "catching_composite", module=_MODULE)
    if case == "blocked":
        after_first_request.append(_block_requester)
        after_refusal.append(_unblock_requester)
    else:
        # The list can be read again before the third request is tried.
        after_refusal.append(_readable(monkeypatch))
        after_first_request.append(_unreadable(monkeypatch))

    result = await h.run(_DIRECT_OP_ID)

    assert sent == ["POST /things"]
    assert result.status == ("denied" if case == "blocked" else "error"), result
    assert result.extras["error_code"] == (
        "principal_blocked" if case == "blocked" else "principal_block_check_unavailable"
    )
    _assert_part_may_have_run(result)


async def test_direct_request_is_refused_when_the_block_list_cannot_be_read(
    session: AsyncSession, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await h.register_child(embedding)
    await h.register_composite(session, _DIRECT_OP_ID, "direct_requests_composite", module=_MODULE)
    after_first_request.append(_unreadable(monkeypatch))

    result = await h.run(_DIRECT_OP_ID)

    assert sent == ["POST /things"]
    assert result.status == "error", result
    assert result.extras["error_code"] == "principal_block_check_unavailable"
    _assert_part_may_have_run(result)


async def test_request_outside_a_multi_step_operation_reads_no_block_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _unreadable(monkeypatch)()

    await _DirectConnector()._post_json(_TARGET, "/things", operator=h.operator(), json={})

    assert sent == ["POST /things"]


def _github_connector() -> GitHubRestConnector:
    """The real GitHub connector with a recording session that answers each git step."""

    async def _answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        sent.append(f"{request.method} {path}")
        if path == "/repos/acme/widgets/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"type": "commit", "sha": _BASE_SHA}})
        if path == "/repos/acme/widgets":
            return httpx.Response(200, json={"node_id": "R_kgDOtest"})
        if path == f"/repos/acme/widgets/git/commits/{_BASE_SHA}":
            return httpx.Response(200, json={"tree": {"sha": "c" * 40}})
        if path == "/repos/acme/widgets/git/trees":
            return httpx.Response(201, json={"sha": "d" * 40})
        if path == "/repos/acme/widgets/git/commits":
            # The commit exists now; an admin blocks the person before the branch moves.
            await _block_requester()
            return httpx.Response(201, json={"sha": _NEW_COMMIT_SHA, "tree": {"sha": "d" * 40}})
        return httpx.Response(200, json={"data": {"updateRefs": {"clientMutationId": None}}})

    connector = GitHubRestConnector()
    client = httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(_answer)
    )
    connector._http_client = AsyncMock(return_value=client)  # type: ignore[method-assign]
    connector.auth_headers = AsyncMock(return_value={})  # type: ignore[method-assign]
    return connector


async def test_branch_commit_files_never_moves_the_branch_once_the_person_is_blocked(
    session: AsyncSession,
) -> None:
    operator = h.operator()
    params = {
        "owner": "acme",
        "repo": "widgets",
        "branch": "main",
        "base_sha": _BASE_SHA,
        "message": "Add a file",
        "files": [{"path": "docs/a.md", "content": "hello"}],
    }

    with (
        checking_logins_for_steps(operator.sub, operator.tenant_id),
        pytest.raises(PrincipalBlockedStepError) as refused,
    ):
        await branch_commit_files_composite(
            operator=operator,
            target=SimpleNamespace(name="gh", host="api.github.com", port=443),
            params=params,
            connector=_github_connector(),
        )

    assert refused.value.unavailable is False
    assert sent[-1] == "POST /repos/acme/widgets/git/commits"
    assert "POST /graphql" not in sent
