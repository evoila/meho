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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.login_redirect import SessionLoginRedirectError
from meho_backplane.connectors.adapters.http import _SameOriginRedirectClient, login_request
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations.approval_queue import _dump_result_envelope
from meho_backplane.operations.uncertain_outcome import UNCERTAIN_OUTCOME
from meho_backplane.settings import get_settings
from tests.test_operations_session_expired_hook import (  # noqa: F401
    _OP_ID,
    _audit_rows,
    _NoHook403Connector,
    _required_settings_env,
    _reset_module_state,
    _run,
    _web_operator,
)


def _core_client(**kwargs: Any) -> _SameOriginRedirectClient:
    """A pooled client with the change rule, as a connector that comes with MEHO gets it."""
    client = _SameOriginRedirectClient(**kwargs)
    client.change_rule = True
    return client


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
    location: ClassVar[str | None] = "/jobs/42"
    expired: ClassVar[bool] = False

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        headers = {} if self.location is None else {"Location": self.location}
        return httpx.Response(self.status, headers=headers)

    async def _send(self, method: str, path: str) -> dict[str, Any]:
        def answer(request: httpx.Request) -> httpx.Response:
            self.sent.append(request.method)
            return self.answer(request)

        async with _core_client(
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
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_change_that_may_have_run_is_marked_uncertain_in_the_audit_row(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    status: int,
    on: bool,
    answer_switch: Any,
) -> None:
    # A 301, 302 or 303 after a change: the target may have run it. The audit
    # row says so in both switch states. A 307 or 308 gets no mark. The
    # "do not send again" advice in the answer needs the switch on.
    answer_switch(on)

    class _Conn(_RedirectingConnector):
        impl_id = f"vcfops-rest-redirect-{status}-{'on' if on else 'off'}"

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
    assert connector.sent == ["PUT"]
    may_have_run = status in (301, 302, 303)
    rows = await _audit_rows()
    assert len(rows) == 1
    audited = rows[0].payload["error"]
    assert (audited.get("execution_outcome") == UNCERTAIN_OUTCOME) is may_have_run
    if on and may_have_run:
        assert result.extras["execution_outcome"] == UNCERTAIN_OUTCOME
        assert result.extras["retry"] is False
        assert "remediation" in result.extras
    else:
        for key in ("execution_outcome", "retry", "remediation"):
            assert key not in result.extras


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
@pytest.mark.parametrize("status", [301, 302, 303])
async def test_change_redirect_without_location_is_marked_uncertain_in_the_audit_row(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    status: int,
    on: bool,
    answer_switch: Any,
) -> None:
    # The answer names no address, but the target got the change all the same.
    answer_switch(on)

    class _Conn(_RedirectingConnector):
        impl_id = f"vcfops-rest-redirect-nolocation-{status}-{'on' if on else 'off'}"
        location = None

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
    assert connector.sent == ["PUT"]
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["error"].get("execution_outcome") == UNCERTAIN_OUTCOME
    assert (result.extras.get("execution_outcome") == UNCERTAIN_OUTCOME) is on


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
    assert "directed MEHO to another resource" in result.error
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"
    assert rows[0].payload["error"]["http_status"] == 303
    # The result that is stored on the approval request holds the message.
    stored = _dump_result_envelope(result)
    assert stored is not None
    assert "directed MEHO to another resource" in json.dumps(stored)


class _TicketRedirectConnector(_RedirectingConnector):
    impl_id = "vcfops-rest-redirect-ticket"
    location = "/jobs/42?ticket=one-time-ticket-123"


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_ticket_in_the_redirect_address_stays_out_of_result_and_audit_row(
    stub_embedding_service: AsyncMock, session: AsyncSession, on: bool, answer_switch: Any
) -> None:
    answer_switch(on)

    class _Conn(_TicketRedirectConnector):
        impl_id = f"vcfops-rest-redirect-ticket-{'on' if on else 'off'}"

    with capture_logs() as captured:
        result, _ = await _run(
            _Conn,
            stub_embedding_service,
            session,
            _web_operator(),
            method="POST",
            safety_level="caution",
        )

    assert result.status == "error"
    assert result.error is not None
    message = result.extras["exception_message"]
    if on:
        assert message == "HTTP 303 redirect after POST vcfops.example.test"
        assert "vcfops.example.test." in result.error.split()
    else:
        assert result.error == "connector_error: HTTPStatusError"
        assert message == (
            "Redirect response 303 from vcfops.example.test to <unparseable-location>, "
            "not followed."
        )
    for text in (result.error, message):
        assert "one-time-ticket-123" not in text
        assert "/jobs/42" not in text
    assert "one-time-ticket-123" not in json.dumps(result.model_dump(mode="json"))
    assert "one-time-ticket-123" not in json.dumps(_dump_result_envelope(result))
    assert "one-time-ticket-123" not in json.dumps(captured, default=str)
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["error"]["exception_message"] == message
    assert "one-time-ticket-123" not in json.dumps(rows[0].payload)


class _NoLocationRedirectConnector(_RedirectingConnector):
    location = None


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
@pytest.mark.parametrize("status", [307, 308])
async def test_307_or_308_without_location_gives_the_plain_refusal(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    status: int,
    on: bool,
    answer_switch: Any,
) -> None:
    # There is no address to approve, so the answer asks for no approval.
    answer_switch(on)

    class _Conn(_NoLocationRedirectConnector):
        impl_id = f"vcfops-rest-redirect-noloc-{status}-{'on' if on else 'off'}"

    _Conn.status = status
    async with get_sessionmaker()() as fresh:
        approvals_before = await fresh.scalar(select(func.count()).select_from(ApprovalRequest))
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
    assert result.extras["exception_message"] == (
        f"Redirect response {status} from vcfops.example.test, with no Location header."
    )
    assert "approval" not in json.dumps(result.model_dump(mode="json")).lower()
    for key in ("execution_outcome", "retry", "remediation", "message_source"):
        assert key not in result.extras
    async with get_sessionmaker()() as fresh:
        approvals_after = await fresh.scalar(select(func.count()).select_from(ApprovalRequest))
    assert approvals_after == approvals_before
    rows = await _audit_rows()
    assert len(rows) == 1
    assert "execution_outcome" not in rows[0].payload["error"]


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
    assert "MEHO did not follow it" in result.extras["exception_message"]
    assert "did not send the operation" not in result.extras["exception_message"]
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
    assert result.extras["exception_message"] == "HTTP 307 redirect after PUT vcfops.example.test"
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
    assert "not the approved call" in result.error
    assert "did not run" not in result.error
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
        "The login of this connector got HTTP 303, a redirect. MEHO did not follow it, "
        "so the login did not finish."
    )
    assert "execution_outcome" not in result.extras
    rows = await _audit_rows()
    assert len(rows) == 1
    assert "so the login did not finish" in json.dumps(rows[0].payload)


class _LoginRedirectedOnRecoveryConnector(_RedirectingConnector):
    """401 on the first send. The fresh login then gets a redirect to its own path.

    The login is a direct client call, as a real login is. It follows a
    redirect on the same origin, as a read does. The operation is then sent
    once more.
    """

    impl_id = "vcfops-rest-login-redirect-on-recovery"
    login_status: ClassVar[int] = 307

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, str]] = []
        self.logged_out = False

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.url.path == "/api/session":
            return httpx.Response(self.login_status, headers={"Location": "/api/session/"})
        if request.url.path == "/api/session/":
            return httpx.Response(200, json={"token": "made-up"})
        if len([r for r in self.requests if r[1] == "/api/v2/events"]) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    async def _send(self, method: str, path: str) -> dict[str, Any]:
        async with _core_client(
            base_url="https://vcfops.example.test", transport=httpx.MockTransport(self.answer)
        ) as client:
            if self.logged_out:
                with login_request():
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
@pytest.mark.parametrize("login_status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(("method", "safety_level"), [("GET", "safe"), ("PUT", "caution")])
async def test_login_redirected_during_recovery_is_followed_and_the_call_runs_again(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    method: str,
    safety_level: str,
    login_status: int,
) -> None:
    class _Conn(_LoginRedirectedOnRecoveryConnector):
        impl_id = f"vcfops-rest-login-redirect-on-recovery-{login_status}"

    _Conn.login_status = login_status
    result, connector = await _run(
        _Conn,
        stub_embedding_service,
        session,
        _web_operator(),
        method=method,
        safety_level=safety_level,
    )

    assert isinstance(connector, _LoginRedirectedOnRecoveryConnector)
    assert result.status == "ok", result.error
    login_again = "POST" if login_status in (307, 308) else "GET"
    assert connector.requests == [
        (method, "/api/v2/events"),
        ("POST", "/api/session"),
        (login_again, "/api/session/"),
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
async def test_the_switch_on_answer_hides_the_marked_values(
    stub_embedding_service: AsyncMock, session: AsyncSession, answer_switch: Any
) -> None:
    answer_switch(True)
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
async def test_the_switch_on_answer_hides_the_path_of_an_operation_that_sends_a_secret(
    stub_embedding_service: AsyncMock, session: AsyncSession, answer_switch: Any
) -> None:
    answer_switch(True)
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


class _CrossOriginLoginConnector(_RedirectingConnector):
    """The login gets a redirect to another origin; it runs on the pooled client's rules."""

    impl_id = "vcfops-rest-login-cross-origin"
    status = 307

    def answer(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "vcfops.example.test":
            return httpx.Response(200, json={"sessionId": "elsewhere"})
        if request.url.path.startswith("/api/mrk-login-path-3a"):
            return httpx.Response(
                self.status,
                headers={
                    "Location": "https://other.example.test/mrk-location-path-3a?q=mrk-query-3a"
                },
                json={"message": "mrk-login-body-3a"},
            )
        return httpx.Response(200, json={"ok": True})

    async def _send(self, method: str, path: str) -> dict[str, Any]:
        from meho_backplane.connectors._shared.vcf_auth import vcf_session_login

        def answer(request: httpx.Request) -> httpx.Response:
            self.sent.append(f"{request.method} {request.url.host}{request.url.path}")
            return self.answer(request)

        async with _core_client(
            base_url="https://vcfops.example.test", transport=httpx.MockTransport(answer)
        ) as client:
            await vcf_session_login(
                client,
                "/api/mrk-login-path-3a/session",
                username="made-up",
                password="made-up",
                target_name="lab-target",
                token_extractor=lambda r: r.json().get("sessionId"),
            )
            resp = await self._send_traced(client, method, path, headers={}, json={"name": "w"})
        resp.raise_for_status()
        return {"ok": True}


_LOGIN_MARKERS = ("mrk-login-path-3a", "mrk-location-path-3a", "mrk-query-3a", "mrk-login-body-3a")


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", [[], ["credential"]], ids=["plain", "sends-a-secret"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_a_login_redirect_to_another_origin_ends_the_login_in_both_modes(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    on: bool,
    status: int,
    tags: list[str],
    answer_switch: Any,
) -> None:
    answer_switch(on)

    class _Conn(_CrossOriginLoginConnector):
        impl_id = f"vcfops-rest-login-cross-origin-{status}-{int(on)}-{len(tags)}"

    _Conn.status = status
    with capture_logs() as logs:
        result, connector = await _run(
            _Conn,
            stub_embedding_service,
            session,
            _web_operator(),
            approved=True,
            method="PUT",
            safety_level="caution",
            tags=tags,
        )

    # One login request, nothing to the other origin, no operation request.
    assert isinstance(connector, _CrossOriginLoginConnector)
    assert connector.sent == ["POST vcfops.example.test/api/mrk-login-path-3a/session"]
    assert result.status == "error"
    if on:
        # The fixed sentence of MEHO: no address and no text of the answer.
        assert result.extras["exception_message"] == (
            f"The login of this connector got HTTP {status}, a redirect. MEHO did not "
            "follow it, so the login did not finish."
        )
        await _assert_no_marker(result, _LOGIN_MARKERS)
    elif tags:
        # The login's own error, withheld as for every error of a secret call.
        assert result.extras["exception_message"] == (
            "<vendor text withheld: this operation sends a secret>"
        )
        await _assert_no_marker(result, _LOGIN_MARKERS)
    else:
        # The login's own error for the status, as the login code words it.
        assert result.extras["exception_message"] == (
            "vcf session-login failed for target 'lab-target': "
            f"POST /api/mrk-login-path-3a/session returned HTTP {status}"
        )
        await _assert_no_marker(result, _LOGIN_MARKERS[1:])
    dumped_logs = json.dumps(logs, default=str)
    for marker in ("mrk-location-path-3a", "mrk-query-3a", "mrk-login-body-3a"):
        assert marker not in dumped_logs


class _FaultRedirectConnector(_RedirectingConnector):
    """A change gets a redirect to another origin whose body may be a vendor fault."""

    impl_id = "vcfops-rest-redirect-fault"
    fault: ClassVar[bool] = True

    def __init__(self) -> None:
        super().__init__()
        self.error: httpx.HTTPStatusError | None = None

    def answer(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = (
            {"_typeName": "InvalidArgument", "faultstring": "mrk-fault-text-6b"}
            if self.fault
            else {"message": "mrk-fault-text-6b"}
        )
        return httpx.Response(
            self.status, headers={"Location": "https://other.example.test/jobs/42"}, json=body
        )

    async def _send(self, method: str, path: str) -> dict[str, Any]:
        try:
            return await super()._send(method, path)
        except httpx.HTTPStatusError as exc:
            self.error = exc
            raise


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", [[], ["credential"]], ids=["plain", "sends-a-secret"])
@pytest.mark.parametrize("fault", [True, False], ids=["vendor-fault", "other-body"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_with_the_switch_off_the_answer_is_the_generic_answer(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    status: int,
    fault: bool,
    tags: list[str],
    answer_switch: Any,
) -> None:
    from meho_backplane.operations._errors import result_connector_error
    from meho_backplane.operations._vendor_text import redact_error_result, withhold_vendor_text

    answer_switch(False)

    class _Conn(_FaultRedirectConnector):
        impl_id = f"vcfops-rest-redirect-fault-{status}-{int(fault)}-{len(tags)}"

    _Conn.status = status
    _Conn.fault = fault
    result, connector = await _run(
        _Conn,
        stub_embedding_service,
        session,
        _web_operator(),
        approved=True,
        method="PUT",
        safety_level="caution",
        tags=tags,
    )

    assert isinstance(connector, _FaultRedirectConnector)
    assert connector.error is not None
    # The generic answer for that status, through the same last steps that
    # every error answer passes (the vendor text of a secret call is withheld).
    generic = redact_error_result(
        withhold_vendor_text(
            result_connector_error(_OP_ID, connector.error, 1.0), op_id=_OP_ID, tags=tags
        )
    )
    assert generic.extras is not None
    assert ("upstream_fault" in generic.extras) is fault
    assert result.status == generic.status == "error"
    assert result.error == generic.error
    assert result.extras == generic.extras
