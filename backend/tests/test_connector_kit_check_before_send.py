# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector package checks the caller's block before each request of its own client.

The ``httpx`` clients come from ``kit.build_http_client``, the one way a
package builds them.

The test connector below uses only names of the connector kit. Each test
drives a real dispatch of a typed operation against the migrated SQLite
engine, blocks the caller in the middle of the call, and counts what reached
the stand-in target.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import meho_connector_kit as kit
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth import principal_block
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import block_principal, reset_block_cache
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from . import _composite_wait_audit_helpers as h

pytestmark = pytest.mark.asyncio

_HTTPX_OP = "vault.kit.httpx_two_requests"
_REDIRECT_OP = "vault.kit.httpx_redirect"
_RETRY_OP = "vault.kit.httpx_retry"
_LIBRARY_OP = "vault.kit.library_call"

#: Every request or call that reached the stand-in target, in order.
sent: list[str] = []
#: What happens after the first request (set by each test).
after_first: list[Callable[[], Awaitable[None]]] = []


async def _after_first() -> None:
    for hook in after_first:
        await hook()


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> httpx.AsyncClient:
    return kit.build_http_client(
        base_url="https://target.test", transport=httpx.MockTransport(handler), **kwargs
    )


async def two_requests_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    def _answer(request: httpx.Request) -> httpx.Response:
        sent.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={})

    async with _client(_answer) as client:
        await client.get("/one")
        await _after_first()
        await client.post("/two")
    return {"ok": True}


async def redirect_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    def _answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        if request.url.path == "/hop-1":
            return httpx.Response(302, headers={"location": "/hop-2"})
        if request.url.path == "/hop-2":
            return httpx.Response(302, headers={"location": "/hop-3"})
        return httpx.Response(200, json={})

    async def _block_after_first_hop(response: httpx.Response) -> None:
        if response.request.url.path == "/hop-1":
            await _after_first()

    client = _client(
        _answer, follow_redirects=True, event_hooks={"response": [_block_after_first_hop]}
    )
    async with client:
        await client.get("/hop-1")
    return {"ok": True}


async def retry_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """A package that retries a 503 and also retries after a refusal."""

    def _answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(503)

    async with _client(_answer) as client:
        for attempt in range(3):
            try:
                response = await client.get("/flaky")
            except kit.PrincipalBlockedStepError:
                continue
            if attempt == 0:
                await _after_first()
            if response.status_code != 503:
                break
    return {"ok": True}


async def library_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """A client that is not httpx: the check runs in the event loop, before the thread."""

    def _vendor_call(name: str) -> str:
        sent.append(name)
        return "ok"

    await kit.check_before_send()
    await asyncio.to_thread(_vendor_call, "statement-1")
    await _after_first()
    await kit.check_before_send()
    await asyncio.to_thread(_vendor_call, "statement-2")
    return {"ok": True}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    for reset in (get_settings.cache_clear, reset_dispatcher_caches, reset_block_cache):
        reset()
    clear_registry()
    sent.clear()
    after_first.clear()
    yield
    for reset in (get_settings.cache_clear, reset_dispatcher_caches, reset_block_cache):
        reset()
    clear_registry()
    sent.clear()
    after_first.clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s_:
        s_.add(Tenant(id=h.TENANT_ID, slug="kit-check", name="Kit Check"))
        await s_.commit()
        yield s_


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = h.EMBEDDING
    service.encode.return_value = [h.EMBEDDING]
    service.dimension = 384
    return service


async def _block_the_caller() -> None:
    admin = Operator(
        sub="admin-person",
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=h.TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )
    await block_principal(admin, h.operator().sub, "left the team")


async def _register(embedding: AsyncMock, op_id: str, handler: Any) -> None:
    await h.register_child(embedding)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=handler,
        summary="Typed.",
        description="Typed.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="safe",
        embedding_service=embedding,
    )


def _assert_blocked_and_uncertain(result: Any) -> None:
    assert result.status == "denied", result
    assert result.extras["error_code"] == "principal_blocked"
    assert result.extras["execution_outcome"] == "uncertain"
    assert "may have run" in str(result.error)


async def test_httpx_hook_does_not_send_the_second_request_of_a_blocked_login(
    embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(embedding, _HTTPX_OP, two_requests_handler)
    after_first.append(_block_the_caller)

    result = await h.run(_HTTPX_OP)

    assert sent == ["GET /one"]
    _assert_blocked_and_uncertain(result)


async def test_httpx_hook_sends_every_request_of_a_login_that_is_not_blocked(
    embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(embedding, _HTTPX_OP, two_requests_handler)

    result = await h.run(_HTTPX_OP)

    assert sent == ["GET /one", "POST /two"]
    assert result.status == "ok", result


async def test_httpx_hook_does_not_send_the_next_redirect_hop_of_a_blocked_login(
    embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(embedding, _REDIRECT_OP, redirect_handler)
    after_first.append(_block_the_caller)

    result = await h.run(_REDIRECT_OP)

    assert sent == ["/hop-1"]
    _assert_blocked_and_uncertain(result)


async def test_httpx_hook_does_not_send_a_retry_of_a_blocked_login(
    embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(embedding, _RETRY_OP, retry_handler)
    after_first.append(_block_the_caller)

    result = await h.run(_RETRY_OP)

    assert sent == ["/flaky"]
    _assert_blocked_and_uncertain(result)


async def test_direct_check_does_not_start_the_library_call_of_a_blocked_login(
    embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(embedding, _LIBRARY_OP, library_handler)
    after_first.append(_block_the_caller)

    result = await h.run(_LIBRARY_OP)

    assert sent == ["statement-1"]
    _assert_blocked_and_uncertain(result)


async def test_direct_check_ends_the_call_when_the_block_list_cannot_be_read(
    embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(embedding, _LIBRARY_OP, library_handler)

    async def _broken(_sessionmaker: Any) -> frozenset[tuple[str, str]]:
        raise RuntimeError("database down")

    async def _break_now() -> None:
        monkeypatch.setattr(principal_block, "_load_entries", _broken)
        reset_block_cache()

    after_first.append(_break_now)

    result = await h.run(_LIBRARY_OP)

    assert sent == ["statement-1"]
    assert result.status == "error", result
    assert result.extras["error_code"] == "principal_block_check_unavailable"


async def test_check_outside_a_running_call_does_nothing(session: AsyncSession) -> None:
    await _block_the_caller()

    await kit.check_before_send()
    await kit.check_before_send(httpx.Request("GET", "https://target.test/one"))


class _StandInConnector(HttpConnector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any) -> Any:
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError


async def test_core_client_carries_the_kit_function_as_its_request_hook() -> None:
    connector = _StandInConnector.__new__(_StandInConnector)
    target = type(
        "T",
        (),
        {
            "name": "t",
            "host": "target.test",
            "port": 443,
            "extras": {},
            "verify_tls": True,
            "secret_ref": None,
        },
    )()
    client = connector._build_http_client(target)  # type: ignore[arg-type]
    try:
        assert client.event_hooks["request"] == [kit.check_before_send]
    finally:
        await client.aclose()


async def test_kit_client_adds_the_check_as_the_last_request_hook() -> None:
    async def _sign(_request: httpx.Request) -> None:
        return None

    async def _log(_response: httpx.Response) -> None:
        return None

    hooks = {"request": [_sign], "response": [_log]}
    client = kit.build_http_client(event_hooks=hooks)
    try:
        assert client.event_hooks["request"] == [_sign, kit.check_before_send]
        assert client.event_hooks["response"] == [_log]
        assert hooks["request"] == [_sign]
        assert client.follow_redirects is False
    finally:
        await client.aclose()


async def test_kit_client_takes_a_transport_that_does_not_retry() -> None:
    client = kit.build_http_client(
        transport=httpx.AsyncHTTPTransport(retries=0),
        mounts={"https://other.test": httpx.AsyncHTTPTransport()},
    )
    await client.aclose()


@pytest.mark.parametrize(
    ("given", "named"),
    [
        ({"transport": httpx.AsyncHTTPTransport(retries=2)}, "transport"),
        ({"mounts": {"https://t.test": httpx.AsyncHTTPTransport(retries=1)}}, "mounts"),
    ],
)
async def test_kit_client_refuses_a_transport_that_retries(
    given: dict[str, Any], named: str
) -> None:
    with pytest.raises(ValueError, match=f"{named}.*retries by itself"):
        kit.build_http_client(**given)
