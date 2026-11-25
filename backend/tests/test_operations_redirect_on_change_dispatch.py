# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A redirect after a change, seen through the dispatcher.

The transport is mocked and counts each request. MEHO marks the result
uncertain when the operation changes the target, and does not mark it for a
read. A change that gets a redirect is sent once, also when the connector
says the login ended. An approved ingested PUT that gets a 303 is sent once
and the message is stored. A one-time ticket in the redirect address, and the
address of an operation that sends a secret, stay out of the result and the
audit row, and the log lines. A change sent twice after a 307 is not sent a
third time after "the login ended", and a change sent once before the login
ended follows no redirect on its repeat. A login that gets a redirect is
reported as the login, also for an operation that sends a secret. A fresh
login during that repeat keeps its usual redirect rule.

The switch ``REDIRECT_ON_CHANGE_RESULT`` is on for each test here, unless a
test turns it off. With it off, the answer is the generic ``connector_error``
answer for that status. Both modes hide the same values.
Names and addresses are made up.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import _SameOriginRedirectClient
from meho_backplane.connectors.profiled import SessionLoginRedirectError
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.approval_queue import _dump_result_envelope
from meho_backplane.operations.uncertain_outcome import UNCERTAIN_OUTCOME
from meho_backplane.settings import get_settings
from tests.test_operations_session_expired_hook import (  # noqa: F401
    _audit_rows,
    _NoHook403Connector,
    _required_settings_env,
    _reset_module_state,
    _run,
    _web_operator,
)


@pytest.fixture(autouse=True)
def _answer_switch_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("REDIRECT_ON_CHANGE_RESULT", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def answer_switch(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set the switch ``REDIRECT_ON_CHANGE_RESULT`` for one test."""

    def set_to(on: bool) -> None:
        monkeypatch.setenv("REDIRECT_ON_CHANGE_RESULT", "true" if on else "false")
        get_settings.cache_clear()

    return set_to


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
    location: ClassVar[str] = "/jobs/42"
    expired: ClassVar[bool] = False

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.status, headers={"Location": self.location})

    async def _send(self, method: str, path: str) -> dict[str, Any]:
        def answer(request: httpx.Request) -> httpx.Response:
            self.sent.append(request.method)
            return self.answer(request)

        async with _SameOriginRedirectClient(
            base_url="https://vcfops.example.test", transport=httpx.MockTransport(answer)
        ) as client:
            # The operation's own request, sent the way an operation sends it.
            resp = await self._send_traced(client, method, path, headers={}, json={"name": "w"})
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
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["session_relogin"] is True
    assert rows[0].payload["session_resent"] is False
    assert rows[0].payload["result_unknown"] is True
    assert rows[0].payload["session_relogin_status"] == 302


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
    assert result.error is not None
    assert "The target says the change ran" in result.error
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"
    assert rows[0].payload["error"]["http_status"] == 303
    # The result that is stored on the approval request holds the message.
    stored = _dump_result_envelope(result)
    assert stored is not None
    assert "The target says the change ran" in json.dumps(stored)


class _TicketRedirectConnector(_RedirectingConnector):
    impl_id = "vcfops-rest-redirect-ticket"
    location = "/jobs/42?ticket=one-time-ticket-123"


@pytest.mark.asyncio
async def test_ticket_in_the_redirect_address_stays_out_of_result_and_audit_row(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, _ = await _run(
        _TicketRedirectConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        method="POST",
        safety_level="caution",
    )

    assert result.status == "error"
    assert "one-time-ticket-123" not in json.dumps(result.model_dump(mode="json"))
    assert "one-time-ticket-123" not in json.dumps(_dump_result_envelope(result))
    rows = await _audit_rows()
    assert len(rows) == 1
    assert "one-time-ticket-123" not in json.dumps(rows[0].payload)


class _SecretPathRedirectConnector(_RedirectingConnector):
    impl_id = "vcfops-rest-redirect-secret"
    status = 307
    location = "/keys/the-secret-value"


@pytest.mark.asyncio
async def test_operation_that_sends_a_secret_keeps_the_address_out(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    with capture_logs() as captured:
        result, _ = await _run(
            _SecretPathRedirectConnector,
            stub_embedding_service,
            session,
            _web_operator(),
            approved=True,
            method="PUT",
            safety_level="caution",
            tags=["secret"],
        )

    assert any(e["event"] == "connector_redirect_not_followed_on_change" for e in captured)
    assert "the-secret-value" not in json.dumps(captured, default=str)
    assert result.status == "error"
    assert "the-secret-value" not in json.dumps(result.model_dump(mode="json"))
    assert result.error is not None
    assert "because this call sends a secret" in result.error
    assert "the-secret-value" not in json.dumps(_dump_result_envelope(result))
    rows = await _audit_rows()
    assert len(rows) == 1
    assert "the-secret-value" not in json.dumps(rows[0].payload)


class _RedirectThenLoginEndedConnector(_RedirectingConnector):
    """A 307 to the same path, then 401 on the second send."""

    impl_id = "vcfops-rest-redirect-then-401"

    def answer(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/"):
            return httpx.Response(401)
        return httpx.Response(307, headers={"Location": request.url.path + "/"})


@pytest.mark.asyncio
async def test_change_sent_twice_after_a_307_is_not_sent_again_after_login(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(
        _RedirectThenLoginEndedConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        method="PUT",
        safety_level="caution",
    )

    assert isinstance(connector, _RedirectThenLoginEndedConnector)
    assert connector.sent == ["PUT", "PUT"]
    assert result.status == "error"
    assert result.extras["error_code"] == "connector_auth_failed"
    assert connector.invalidations == 1
    rows = await _audit_rows()
    assert rows[0].payload["session_resent"] is False


class _LoginRedirectConnector(_RedirectingConnector):
    """The login before the operation gets a redirect; the operation is not sent."""

    impl_id = "vcfops-rest-login-redirect"

    async def _send(self, method: str, path: str) -> dict[str, Any]:
        raise SessionLoginRedirectError(303)


@pytest.mark.asyncio
async def test_login_redirect_is_reported_as_the_login(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(
        _LoginRedirectConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        method="PUT",
        safety_level="caution",
    )

    assert isinstance(connector, _LoginRedirectConnector)
    assert connector.sent == []
    assert result.status == "error"
    assert "execution_outcome" not in result.extras
    assert "login of this connector got HTTP 303" in result.extras["exception_message"]
    assert "did not send the operation" in result.extras["exception_message"]
    assert "change ran" not in (result.error or "")


class _SecretInSentPathConnector(_RedirectingConnector):
    """The sent path itself carries the secret; the target answers with a 307."""

    impl_id = "vcfops-rest-redirect-sent-secret"
    status = 307
    location = "/elsewhere"

    async def _post_json(self, target: Any, path: str, **kwargs: Any) -> Any:  # type: ignore[override]
        return await self._send(kwargs.get("verb", "POST"), "/keys/sent-secret-value")


@pytest.mark.asyncio
async def test_operation_that_sends_a_secret_keeps_the_sent_path_out(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    with capture_logs() as captured:
        result, _ = await _run(
            _SecretInSentPathConnector,
            stub_embedding_service,
            session,
            _web_operator(),
            approved=True,
            method="PUT",
            safety_level="caution",
            tags=["secret"],
        )

    assert result.status == "error"
    assert result.extras["exception_message"] == (
        "HTTP 307 redirect after PUT https://vcfops.example.test"
    )
    assert "sent-secret-value" not in json.dumps(result.model_dump(mode="json"))
    assert "sent-secret-value" not in json.dumps(_dump_result_envelope(result))
    assert "sent-secret-value" not in json.dumps(captured, default=str)
    rows = await _audit_rows()
    assert len(rows) == 1
    assert "sent-secret-value" not in json.dumps(rows[0].payload)


class _LoginEndedThenRedirectConnector(_RedirectingConnector):
    """401 on the first send; after the new login, a 307 to the same path."""

    impl_id = "vcfops-rest-401-then-redirect"

    def answer(self, request: httpx.Request) -> httpx.Response:
        if len(self.sent) == 1:
            return httpx.Response(401)
        if request.url.path.endswith("/"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(307, headers={"Location": request.url.path + "/"})


@pytest.mark.asyncio
async def test_change_repeated_after_login_follows_no_redirect(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(
        _LoginEndedThenRedirectConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        method="PUT",
        safety_level="caution",
    )

    assert isinstance(connector, _LoginEndedThenRedirectConnector)
    # One send before the login ended, one repeat after it, no third send.
    assert connector.sent == ["PUT", "PUT"]
    assert connector.invalidations == 1
    assert result.status == "error"
    assert result.extras["http_status"] == 307
    assert result.error is not None
    assert "did not run this call" in result.error
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["session_resent"] is True


class _SecretLoginRedirectConnector(_RedirectingConnector):
    """The login before an operation that sends a secret gets a redirect."""

    impl_id = "vcfops-rest-secret-login-redirect"

    async def _send(self, method: str, path: str) -> dict[str, Any]:
        raise SessionLoginRedirectError(303)


@pytest.mark.asyncio
async def test_login_redirect_message_stays_for_an_operation_that_sends_a_secret(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(
        _SecretLoginRedirectConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        method="PUT",
        safety_level="caution",
        tags=["secret"],
    )

    assert isinstance(connector, _SecretLoginRedirectConnector)
    assert connector.sent == []
    assert result.status == "error"
    assert result.extras["exception_message"] == (
        "The login of this connector got HTTP 303, a redirect. MEHO did not follow it "
        "and did not send the operation."
    )
    assert "execution_outcome" not in result.extras
    rows = await _audit_rows()
    assert len(rows) == 1
    assert "did not send the operation" in json.dumps(rows[0].payload)


class _LoginRedirectedOnRecoveryConnector(_RedirectingConnector):
    """401 on the first send. The fresh login then gets a 307 to its own path.

    The login is a direct client call, as a real login is. Its 307 to the
    same path with a trailing slash is followed. The operation is then sent
    once more.
    """

    impl_id = "vcfops-rest-login-redirect-on-recovery"

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, str]] = []
        self.logged_out = False

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.url.path == "/api/session":
            return httpx.Response(307, headers={"Location": "/api/session/"})
        if request.url.path == "/api/session/":
            return httpx.Response(200, json={"token": "made-up"})
        if len([r for r in self.requests if r[1] == "/api/v2/events"]) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    async def _send(self, method: str, path: str) -> dict[str, Any]:
        async with _SameOriginRedirectClient(
            base_url="https://vcfops.example.test", transport=httpx.MockTransport(self.answer)
        ) as client:
            if self.logged_out:
                login = await client.request("POST", "/api/session", json={"user": "made-up"})
                login.raise_for_status()
                self.logged_out = False
            resp = await self._send_traced(client, method, path, headers={}, json={"name": "w"})
        resp.raise_for_status()
        return {"ok": True}

    async def invalidate_session(self, target: Any, *, rejected: Any = None) -> None:
        await super().invalidate_session(target, rejected=rejected)
        self.logged_out = True


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "safety_level"), [("GET", "safe"), ("PUT", "caution")])
async def test_login_redirected_during_recovery_is_followed_and_the_call_runs_again(
    stub_embedding_service: AsyncMock, session: AsyncSession, method: str, safety_level: str
) -> None:
    result, connector = await _run(
        _LoginRedirectedOnRecoveryConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        method=method,
        safety_level=safety_level,
    )

    assert isinstance(connector, _LoginRedirectedOnRecoveryConnector)
    assert result.status == "ok", result.error
    assert connector.requests == [
        (method, "/api/v2/events"),
        ("POST", "/api/session"),
        ("POST", "/api/session/"),
        (method, "/api/v2/events"),
    ]
    assert connector.invalidations == 1


_UNCERTAIN_KEYS = ("execution_outcome", "retry", "remediation", "message_source")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_with_the_switch_off_a_change_redirect_gets_the_generic_answer(
    stub_embedding_service: AsyncMock, session: AsyncSession, status: int, answer_switch: Any
) -> None:
    answer_switch(False)

    class _Conn(_RedirectingConnector):
        impl_id = f"vcfops-rest-redirect-off-{status}"

    _Conn.status = status
    result, connector = await _run(
        _Conn,
        stub_embedding_service,
        session,
        _web_operator(),
        method="PUT",
        safety_level="caution",
    )

    assert connector.sent == ["PUT"]
    assert result.status == "error"
    assert result.error == "connector_error: HTTPStatusError"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["http_status"] == status
    for key in _UNCERTAIN_KEYS:
        assert key not in result.extras


_MARKERS = (
    "marker-sent-query",
    "marker-sent-fragment",
    "marker-sent-user",
    "marker-redirect-query",
    "marker-redirect-fragment",
    "marker-redirect-user",
    "marker-body",
)


class _MarkedRedirectConnector(_RedirectingConnector):
    """Made-up marker values in every part of the two addresses and in the body."""

    impl_id = "vcfops-rest-redirect-marked"
    status = 307
    sent_path = "/items/7"

    def answer(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            self.status,
            headers={
                "Location": (
                    "https://marker-redirect-user@vcfops.example.test/jobs/42"
                    "?t=marker-redirect-query#marker-redirect-fragment"
                )
            },
            json={"message": "marker-body"},
        )

    async def _post_json(self, target: Any, path: str, **kwargs: Any) -> Any:  # type: ignore[override]
        return await self._send(
            kwargs.get("verb", "POST"),
            f"https://marker-sent-user@vcfops.example.test{self.sent_path}"
            "?t=marker-sent-query#marker-sent-fragment",
        )


class _MarkedSecretPathConnector(_MarkedRedirectConnector):
    impl_id = "vcfops-rest-redirect-marked-secret"
    sent_path = "/keys/marker-secret-path"


async def _assert_no_marker(result: Any, markers: tuple[str, ...]) -> None:
    rows = await _audit_rows()
    assert len(rows) == 1
    for label, text in (
        ("the answer", json.dumps(result.model_dump(mode="json"))),
        ("the stored result", json.dumps(_dump_result_envelope(result))),
        ("the audit row", json.dumps(rows[0].payload, default=str)),
    ):
        for marker in markers:
            assert marker not in text, f"{marker} is in {label}"


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_both_modes_hide_the_marked_values(
    stub_embedding_service: AsyncMock, session: AsyncSession, on: bool, answer_switch: Any
) -> None:
    answer_switch(on)
    result, _ = await _run(
        _MarkedRedirectConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        approved=True,
        method="PUT",
        safety_level="caution",
    )

    assert result.status == "error"
    assert result.extras["http_status"] == 307
    await _assert_no_marker(result, _MARKERS)


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_both_modes_hide_the_path_of_an_operation_that_sends_a_secret(
    stub_embedding_service: AsyncMock, session: AsyncSession, on: bool, answer_switch: Any
) -> None:
    answer_switch(on)
    result, _ = await _run(
        _MarkedSecretPathConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        approved=True,
        method="PUT",
        safety_level="caution",
        tags=["secret"],
    )

    assert result.status == "error"
    await _assert_no_marker(result, (*_MARKERS, "marker-secret-path", "/jobs/42"))


class _LoginHttpRedirectConnector(_RedirectingConnector):
    """The login POST gets a 303; the error is the login's own status error."""

    impl_id = "vcfops-rest-login-http-redirect"

    async def _send(self, method: str, path: str) -> dict[str, Any]:
        request = httpx.Request("POST", "https://vcfops.example.test/api/session")
        httpx.Response(303, headers={"Location": "/ui/"}, request=request).raise_for_status()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_with_the_switch_off_a_login_redirect_gets_the_generic_answer(
    stub_embedding_service: AsyncMock, session: AsyncSession, answer_switch: Any
) -> None:
    answer_switch(False)
    result, connector = await _run(
        _LoginHttpRedirectConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        method="PUT",
        safety_level="caution",
    )

    assert connector.sent == []
    assert result.status == "error"
    assert result.error == "connector_error: HTTPStatusError"
    assert result.extras["http_status"] == 303
    for key in _UNCERTAIN_KEYS:
        assert key not in result.extras
