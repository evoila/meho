# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A redirect after a change, seen through the dispatcher.

The transport is mocked and counts each request. MEHO marks the result
uncertain when the operation changes the target, and does not mark it for a
read. A change that gets a redirect is sent once, also when the connector
says the login ended. An approved ingested PUT that gets a 303 is sent once
and the message is stored. Names and addresses are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import _SameOriginRedirectClient
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.uncertain_outcome import UNCERTAIN_OUTCOME
from tests.test_operations_session_expired_hook import (  # noqa: F401
    _audit_rows,
    _NoHook403Connector,
    _required_settings_env,
    _reset_module_state,
    _run,
    _web_operator,
)


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


class _RedirectingConnector(_NoHook403Connector):
    """Sends each call through a mocked transport that answers with a redirect."""

    impl_id = "vcfops-rest-redirect"
    status: ClassVar[int] = 303
    expired: ClassVar[bool] = False

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def _send(self, method: str, path: str) -> dict[str, Any]:
        def answer(request: httpx.Request) -> httpx.Response:
            self.sent.append(request.method)
            return httpx.Response(self.status, headers={"Location": "/jobs/42"})

        async with _SameOriginRedirectClient(
            base_url="https://vcfops.example.test", transport=httpx.MockTransport(answer)
        ) as client:
            resp = await client.request(method, path, json={"name": "widget"})
        resp.raise_for_status()
        return {"ok": True}

    async def _request_json(self, target: Any, method: str, path: str, **kwargs: Any) -> Any:  # type: ignore[override]
        return await self._send(method, path)

    async def _post_json(self, target: Any, path: str, **kwargs: Any) -> Any:  # type: ignore[override]
        return await self._send(kwargs.get("verb", "POST"), path)

    def is_session_expired(self, response: httpx.Response) -> bool:
        return self.expired


class _LoginEndedRedirectConnector(_RedirectingConnector):
    impl_id = "vcfops-rest-redirect-login"
    status = 302
    # The product's profile says that a 302 means the login ended.
    profile = SimpleNamespace(expiry_statuses=frozenset({302}))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_change_that_gets_a_redirect_is_marked_uncertain(
    stub_embedding_service: AsyncMock, session: AsyncSession, status: int
) -> None:
    class _Conn(_RedirectingConnector):
        impl_id = f"vcfops-rest-redirect-{status}"

    _Conn.status = status
    result, connector = await _run(
        _Conn,
        stub_embedding_service,
        session,
        _web_operator(),
        method="PUT",
        safety_level="caution",
    )

    assert result.status == "error"
    assert result.extras["execution_outcome"] == UNCERTAIN_OUTCOME
    assert result.extras["retry"] is False
    assert connector.sent[0] == "PUT"


@pytest.mark.asyncio
async def test_read_that_gets_a_redirect_is_not_marked_uncertain(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, _ = await _run(_RedirectingConnector, stub_embedding_service, session, _web_operator())

    assert result.status == "error"
    assert "execution_outcome" not in result.extras


@pytest.mark.asyncio
async def test_login_ended_redirect_does_not_send_the_change_again(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    operator: Operator = _web_operator()
    result, connector = await _run(
        _LoginEndedRedirectConnector,
        stub_embedding_service,
        session,
        operator,
        method="PUT",
        safety_level="caution",
    )

    assert isinstance(connector, _LoginEndedRedirectConnector)
    assert connector.sent == ["PUT"]
    assert result.extras["execution_outcome"] == UNCERTAIN_OUTCOME
    assert result.extras["error_code"] != "connector_auth_failed"
    assert connector.invalidations >= 1


@pytest.mark.asyncio
async def test_approved_ingested_put_with_303_is_sent_once_and_stored(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(
        _RedirectingConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        approved=True,
        method="PUT",
        safety_level="caution",
    )

    assert isinstance(connector, _RedirectingConnector)
    assert connector.sent == ["PUT"]
    assert result.extras["execution_outcome"] == UNCERTAIN_OUTCOME
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"
