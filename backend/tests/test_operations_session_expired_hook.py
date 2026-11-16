# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector can say that an answer means "the login session ended".

The dispatcher drops the kept session, also for a 4xx status that is not in
the known set (a product that answers an ended session with 403 and a body
of its own). It signs in again and repeats the call once only when that
cannot send a change twice: a ``safe`` operation, or an ingested operation
with one request. It never loops. A typed write, a multi-step operation and
an approved call are not sent again. A real 403, a 5xx and a connector with
no hook behave as before.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.flight_recorder import capture
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations._session_expired_hook import (
    mark_session_relogin,
    session_expired_by_hook,
)
from meho_backplane.settings import get_settings
from tests.test_operations_connector_auth_failed import (
    _FakeTarget,
    _Http401Connector,
    _make_http_status_error,
    _make_operator,
)

_OP_ID = "GET:/api/v2/events"
_EXPIRED_BODY = {"error": "session-ended", "code": "SESSION_GONE"}
_REAL_403_BODY = {"message": "Resource not accessible by integration"}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


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


class _NoHook403Connector(_Http401Connector):
    """First call: 403 with the product's body. Later calls: ok. No hook."""

    impl_id = "vcfops-rest-nohook"
    first_status: ClassVar[int] = 403
    first_body: ClassVar[dict[str, Any]] = _EXPIRED_BODY
    #: Calls that answer with the status and body above (``None``: every call).
    failing_calls: ClassVar[int | None] = 1
    #: The error the sign-in (the second call) raises instead of answering.
    login_error: ClassVar[ConnectorAuthError | None] = None

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.invalidations = 0

    def _next(self) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 2 and self.login_error is not None:
            raise self.login_error
        if self.failing_calls is None or self.calls <= self.failing_calls:
            raise _make_http_status_error(
                headers={"Content-Type": "application/json"},
                json_body=self.first_body,
                status_code=self.first_status,
            )
        return {"ok": True}

    async def _request_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        return self._next()

    async def _post_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        return self._next()

    async def invalidate_session(self, target: Any, *, rejected: Any = None) -> None:
        self.invalidations += 1


class _HookConnector(_NoHook403Connector):
    """The same, with the hook: the product's body means the session ended."""

    impl_id = "vcfops-rest-hook"

    def is_session_expired(self, response: httpx.Response) -> bool:
        return response.status_code == 403 and response.json().get("code") == "SESSION_GONE"


class _HookRecoversConnector(_HookConnector):
    impl_id = "vcfops-rest-hook-recovers"


class _HookNeverRecoversConnector(_HookConnector):
    impl_id = "vcfops-rest-hook-never"
    failing_calls: ClassVar[int | None] = None


class _HookLoginRefusedConnector(_HookConnector):
    impl_id = "vcfops-rest-hook-refused"
    login_error: ClassVar[ConnectorAuthError | None] = ConnectorAuthError(
        "login refused",
        status_code=401,
        cause="session_establish_401",
        target_name="lab-target",
    )


class _HookRealForbiddenConnector(_HookConnector):
    """A real 403: the body is not the product's "session ended" body."""

    impl_id = "vcfops-rest-hook-real403"
    first_body: ClassVar[dict[str, Any]] = _REAL_403_BODY
    failing_calls: ClassVar[int | None] = None


class _HookRaisesConnector(_HookConnector):
    impl_id = "vcfops-rest-hook-raises"
    failing_calls: ClassVar[int | None] = None

    def is_session_expired(self, response: httpx.Response) -> bool:
        raise ValueError("body is not json")


class _NoHookRealForbiddenConnector(_NoHook403Connector):
    impl_id = "vcfops-rest-nohook-real403"
    failing_calls: ClassVar[int | None] = None


class _HookServerErrorConnector(_HookConnector):
    """A hook that says yes to everything, and a 503 answer."""

    impl_id = "vcfops-rest-hook-503"
    first_status: ClassVar[int] = 503
    failing_calls: ClassVar[int | None] = None

    def is_session_expired(self, response: httpx.Response) -> bool:
        return True


class _TypedWriteConnector(_HookConnector):
    """A typed handler that sends a write, then gets "session ended" on a read."""

    impl_id = "vcfops-typed-write"

    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    async def write_then_read(self, target: Any, params: dict[str, Any]) -> dict[str, Any]:
        self.writes += 1
        return self._next()


class _CompositeWriteConnector(_TypedWriteConnector):
    impl_id = "vcfops-composite-write"


_TYPED_REF = f"{__name__}._TypedWriteConnector.write_then_read"
_COMPOSITE_REF = f"{__name__}._composite_write_then_read"


async def _composite_write_then_read(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: _TypedWriteConnector,
) -> dict[str, Any]:
    """A multi-step operation: a write step, then a read on the same session."""
    return await connector.write_then_read(target, params)


def _web_operator() -> Operator:
    """A person in the web interface: a ``caution`` call runs at once."""
    return _make_operator().model_copy(update={"web_session": True})


def _assert_not_resent(result: Any, connector: _NoHook403Connector) -> None:
    assert result.status == "error"
    assert result.extras["error_code"] == "connector_auth_failed"
    assert result.extras["cause"] == "session_ended_403_not_resent"
    assert "may or may not have been made" in result.extras["remediation"]
    assert "Check the state on the product" in result.extras["remediation"]
    assert connector.calls == 1
    assert connector.invalidations == 1


async def _insert_descriptor(
    session: AsyncSession,
    impl_id: str,
    embedding: list[float],
    *,
    source_kind: str = "ingested",
    method: str = "GET",
    handler_ref: str | None = None,
    safety_level: str = "safe",
) -> None:
    session.add(
        EndpointDescriptor(
            id=uuid.uuid4(),
            tenant_id=None,
            product="vcfops",
            version="9",
            impl_id=impl_id,
            op_id=_OP_ID,
            source_kind=source_kind,
            method=method,
            path="/api/v2/events",
            handler_ref=handler_ref,
            summary="Events.",
            description="Session-ended test op.",
            tags=[],
            parameter_schema={"type": "object", "properties": {}},
            response_schema=None,
            llm_instructions=None,
            safety_level=safety_level,
            requires_approval=False,
            is_enabled=True,
            embedding=embedding,
            custom_description=None,
            custom_notes=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def _run(
    cls: type[_NoHook403Connector],
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    operator: Operator | None = None,
    *,
    approved: bool = False,
    **descriptor: Any,
) -> tuple[Any, _NoHook403Connector]:
    register_connector_v2(product="vcfops", version="9", impl_id=cls.impl_id, cls=cls)
    await _insert_descriptor(
        session, cls.impl_id, stub_embedding_service.encode_one.return_value, **descriptor
    )
    result = await dispatch(
        operator=operator or _make_operator(),
        connector_id=f"{cls.impl_id}-9",
        op_id=_OP_ID,
        target=_FakeTarget(name="lab-target", host="vcfops.example.test"),
        params={},
        _approved=approved,
    )
    connector = get_or_create_connector_instance(cls)
    assert isinstance(connector, _NoHook403Connector)
    return result, connector


async def _audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == _OP_ID))).scalars()
        return list(rows.all())


@pytest.mark.asyncio
async def test_hook_yes_signs_in_once_and_repeats_the_call(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(_HookRecoversConnector, stub_embedding_service, session)

    assert result.status == "ok", result.error
    assert connector.calls == 2
    assert connector.invalidations == 1
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "ok"
    assert rows[0].payload["session_relogin"] is True
    assert rows[0].payload["session_resent"] is True
    assert rows[0].payload["session_relogin_status"] == 403


@pytest.mark.asyncio
async def test_hook_yes_and_same_answer_again_stops_with_a_clear_error(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(_HookNeverRecoversConnector, stub_embedding_service, session)

    assert result.status == "error"
    assert connector.calls == 2  # one first call, one repeat, no loop
    assert connector.invalidations == 1
    assert result.extras["error_code"] == "connector_auth_failed"
    assert result.extras["http_status"] == 403
    assert result.extras["cause"] == "session_dispatch_403_after_relogin"
    assert "accepted the new sign-in" in result.extras["remediation"]
    assert 'same "session ended" answer' in result.extras["remediation"]
    assert "refused the sign-in" not in result.extras["remediation"]
    assert "permission" not in result.extras["remediation"].lower()
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"
    assert rows[0].payload["session_relogin"] is True
    assert rows[0].payload["session_resent"] is True


@pytest.mark.asyncio
async def test_hook_yes_and_refused_sign_in_makes_one_try_and_a_clear_error(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(_HookLoginRefusedConnector, stub_embedding_service, session)

    assert result.status == "error"
    assert connector.calls == 2  # the second call is the refused sign-in
    assert connector.invalidations == 1
    assert result.extras["error_code"] == "connector_auth_failed"
    assert "refused the sign-in" in result.extras["remediation"]
    assert "locked" in result.extras["remediation"]
    assert "permission" not in result.extras["remediation"].lower()


@pytest.mark.asyncio
async def test_hook_no_keeps_the_insufficient_permission_answer(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(_HookRealForbiddenConnector, stub_embedding_service, session)

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_http_403"
    assert connector.calls == 1
    assert connector.invalidations == 0
    rows = await _audit_rows()
    assert "session_relogin" not in rows[0].payload


@pytest.mark.asyncio
async def test_hook_that_raises_counts_as_no(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(_HookRaisesConnector, stub_embedding_service, session)

    assert result.extras["error_code"] == "connector_http_403"
    assert connector.calls == 1
    assert connector.invalidations == 0


@pytest.mark.asyncio
async def test_connector_without_hook_behaves_as_before(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(_NoHookRealForbiddenConnector, stub_embedding_service, session)

    assert result.extras["error_code"] == "connector_http_403"
    assert connector.calls == 1
    assert connector.invalidations == 0


def test_session_expired_by_hook_edge_inputs() -> None:
    response = httpx.Response(
        403, json=_EXPIRED_BODY, request=httpx.Request("GET", "https://x.test")
    )
    assert session_expired_by_hook(None, response) is False
    assert session_expired_by_hook(object(), response) is False
    assert session_expired_by_hook(_HookConnector(), response) is True
    # Only an exact True counts: a truthy non-bool answer is "no".
    connector = _HookConnector()
    connector.is_session_expired = lambda _r: "yes"  # type: ignore[method-assign, assignment, return-value]
    assert session_expired_by_hook(connector, response) is False
    empty = httpx.Response(403, request=httpx.Request("GET", "https://x.test"))
    assert (
        session_expired_by_hook(_HookConnector(), empty) is False
    )  # body is not json -> raises -> no


@pytest.mark.asyncio
async def test_ingested_write_with_one_request_is_repeated_once(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(
        _HookRecoversConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        method="POST",
        safety_level="caution",
    )

    assert result.status == "ok", result.error
    assert connector.calls == 2
    assert connector.invalidations == 1


@pytest.mark.asyncio
async def test_typed_write_is_not_sent_again(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(
        _TypedWriteConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        source_kind="typed",
        handler_ref=_TYPED_REF,
        safety_level="caution",
    )

    _assert_not_resent(result, connector)
    assert isinstance(connector, _TypedWriteConnector)
    assert connector.writes == 1
    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"
    assert rows[0].payload["session_relogin"] is False
    assert rows[0].payload["session_resent"] is False
    assert rows[0].payload["session_relogin_status"] == 403


@pytest.mark.asyncio
async def test_multi_step_operation_is_not_sent_again(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(
        _CompositeWriteConnector,
        stub_embedding_service,
        session,
        _web_operator(),
        source_kind="composite",
        handler_ref=_COMPOSITE_REF,
        safety_level="caution",
    )

    _assert_not_resent(result, connector)
    assert isinstance(connector, _TypedWriteConnector)
    assert connector.writes == 1


@pytest.mark.asyncio
async def test_approved_call_is_not_sent_again(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """One approval is one run, also for an ingested write with one request."""
    result, connector = await _run(
        _HookRecoversConnector,
        stub_embedding_service,
        session,
        approved=True,
        method="POST",
        safety_level="caution",
    )

    _assert_not_resent(result, connector)


@pytest.mark.asyncio
async def test_safe_typed_read_is_repeated_once(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    result, connector = await _run(
        _TypedWriteConnector,
        stub_embedding_service,
        session,
        source_kind="typed",
        handler_ref=_TYPED_REF,
        safety_level="safe",
    )

    assert result.status == "ok", result.error
    assert connector.calls == 2
    assert connector.invalidations == 1


@pytest.mark.asyncio
async def test_server_error_never_asks_the_hook(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """After a 5xx the call may have run, so it is never repeated."""
    result, connector = await _run(_HookServerErrorConnector, stub_embedding_service, session)

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert connector.calls == 1
    assert connector.invalidations == 0


@pytest.mark.parametrize("resent", [True, False])
def test_session_relogin_span(resent: bool) -> None:
    scope = capture._CaptureScope(audit_id=uuid.uuid4(), tenant_id=uuid.uuid4(), target_id=None)
    token = capture._active_capture_var.set(scope)
    try:
        mark_session_relogin(403, resent=resent)
    finally:
        capture._active_capture_var.reset(token)

    assert [(span.span_kind, span.name) for span in scope.spans] == [("typed", "session_relogin")]
    attributes = scope.spans[0].attributes
    assert attributes["rejected_status"] == 403
    assert attributes["resent"] is resent
    assert attributes["session_relogin"] is resent
