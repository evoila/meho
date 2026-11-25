# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A block of a login stops a call that is already running, before its next send.

The first part drives the real dispatcher against the migrated SQLite engine:
a typed operation that sends two requests through the Kubernetes client
wrapper, a multi-step operation whose second step is a governed step, and a
step that waits in its policy gate while another step is refused.

The second part drives each shared send place on its own: the request hook
of the HTTP connector base, the Kubernetes client wrapper, the SSH command
runner (also through the sudo helpers), and the start of an operation's handler.

The last part checks that the two open live streams end when the login is
blocked.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.feed import token_liveness
from meho_backplane.auth import principal_block
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import (
    PrincipalBlockedStepError,
    block_principal,
    raise_if_send_refused,
    reset_block_cache,
)
from meho_backplane.auth.principal_block_run import tracking_run
from meho_backplane.connectors.adapters import ssh as ssh_module
from meho_backplane.connectors.adapters.http import HttpConnector, _note_request_sent
from meho_backplane.connectors.kubernetes._wire_span import instrument_api_client
from meho_backplane.connectors.linux import _sudo as linux_sudo
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.rke2 import _sudo as rke2_sudo
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.operations import (
    DispatchChild,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations import composite as composite_module
from meho_backplane.operations.composite import enforce_subop_policy, run_governed_subop
from meho_backplane.settings import get_settings

from . import _composite_wait_audit_helpers as h

pytestmark = pytest.mark.asyncio

_MODULE = "tests.test_principal_block_running_call"
_TWO_REQUESTS_OP_ID = "vault.things.two_requests"
_WRITE_AFTER_STEP_OP_ID = "vault.composite.write_after_step"
_RACE_OP_ID = "vault.composite.step_waits_while_other_refused"
_STEP_OP_ID = "vault.things.touch"
_GOVERNED_OP_ID = "vault.composite.governed_step"
_GATED_OP_ID = "vault.composite.gated_step"
_DIRECT_STEP = "POST:/v1/sys/things"

#: Every request that reached the stand-in target, in order.
sent: list[str] = []
#: What happens after the first request or step (set by each test).
after_first: list[Callable[[], Awaitable[None]]] = []
#: What happens after a step was refused (set by each test).
after_refusal: list[Callable[[], Awaitable[None]]] = []
#: Held steps of the race test.
race_events: list[asyncio.Event] = []


class _StandInRest:
    """The REST object of a Kubernetes client; it counts what it is asked to send."""

    async def request(self, method: str, url: str, **_kwargs: Any) -> str:
        sent.append(f"{method} {url}")
        return "ok"


def _kubernetes_client() -> Any:
    api_client = SimpleNamespace(rest_client=_StandInRest())
    return instrument_api_client(api_client)


async def _after_first() -> None:
    for hook in after_first:
        await hook()


async def _after_refusal() -> None:
    for hook in after_refusal:
        await hook()


async def two_requests_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Typed handler: read, then write, through the Kubernetes client wrapper."""
    rest = _kubernetes_client().rest_client
    await rest.request("GET", "/cronjobs/one")
    await _after_first()
    await rest.request("POST", "/jobs")
    return {"ok": True}


async def touch_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    sent.append(f"{_STEP_OP_ID}:{params['n']}")
    return {"ok": True}


async def write_after_step_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A first step through ``dispatch_child``, then a governed write."""
    first = await dispatch_child(connector_id=h.CONNECTOR_ID, op_id=_STEP_OP_ID, params={"n": 1})
    if first.status != "ok":
        return first
    await _after_first()

    async def _send() -> dict[str, Any]:
        sent.append(_DIRECT_STEP)
        return {"ok": True}

    step = await run_governed_subop(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=_DIRECT_STEP,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params={"name": "thing-1"},
        call=_send,
    )
    return step.stopped if step.stopped is not None else step.value


async def _governed_step(operator: Operator, target: Any, name: str) -> Any:
    async def _send() -> dict[str, Any]:
        sent.append(f"step-{name}")
        return {"ok": True}

    return await run_governed_subop(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=_DIRECT_STEP,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params={"name": name},
        call=_send,
    )


async def step_waits_while_other_refused_composite(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    """Step A passes its first check and waits in its policy gate; step B is refused."""
    step_a_waiting, step_b_refused = race_events
    task = asyncio.create_task(_governed_step(operator, target, "a"))
    await step_a_waiting.wait()
    await _after_first()
    await _governed_step(operator, target, "b")
    await _after_refusal()
    step_b_refused.set()
    await asyncio.gather(task, return_exceptions=True)
    return {"done": True}


async def gated_step_composite(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """One direct step checked by ``enforce_subop_policy``."""
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=_DIRECT_STEP,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params={"name": "a"},
    )
    if gated is not None:
        return gated
    sent.append("step-a")
    return {"ok": True}


async def governed_step_composite(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """One direct step run by ``run_governed_subop``."""
    step = await _governed_step(operator, target, "a")
    return step.stopped if step.stopped is not None else step.value


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
    after_first.clear()
    after_refusal.clear()
    race_events.clear()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_block_cache()
    clear_registry()
    sent.clear()
    after_first.clear()
    after_refusal.clear()
    race_events.clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s_:
        s_.add(Tenant(id=h.TENANT_ID, slug="block-running", name="Block Running"))
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


async def _block_the_caller() -> None:
    await block_principal(_admin(), h.operator().sub, "left the team")


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


def _block_during_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller is blocked while the step waits in its policy gate."""
    gate = composite_module._gate_subop

    async def _gate_then_block(**kwargs: Any) -> Any:
        gated = await gate(**kwargs)
        await _block_the_caller()
        return gated

    monkeypatch.setattr(composite_module, "_gate_subop", _gate_then_block)


async def _register_typed(embedding: AsyncMock, op_id: str, handler: Any) -> None:
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


async def _register_steps(
    session: AsyncSession, embedding: AsyncMock, op_id: str, name: str
) -> None:
    await _register_typed(embedding, _STEP_OP_ID, touch_handler)
    await h.register_composite(session, op_id, name, module=_MODULE)


async def _audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as s:
        return list((await s.execute(select(AuditLog))).scalars())


def _dispatch_row(rows: list[AuditLog], op_id: str) -> AuditLog:
    (found,) = [r for r in rows if r.payload.get("op_id") == op_id]
    return found


# ---------------------------------------------------------------------------
# A running call, through the real dispatcher
# ---------------------------------------------------------------------------


async def test_write_of_a_running_typed_call_is_not_sent_once_the_login_is_blocked(
    embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_typed(embedding, _TWO_REQUESTS_OP_ID, two_requests_handler)
    after_first.append(_block_the_caller)

    result = await h.run(_TWO_REQUESTS_OP_ID)

    assert sent == ["GET /cronjobs/one"]
    assert result.status == "denied", result
    assert result.extras["error_code"] == "principal_blocked"
    assert result.extras["execution_outcome"] == "uncertain"
    assert "may have run" in str(result.error)
    row = _dispatch_row(await _audit_rows(), _TWO_REQUESTS_OP_ID)
    assert row.payload["result_status"] == "denied"
    assert row.status_code == 403


async def test_call_of_a_login_that_is_not_blocked_sends_every_request(
    embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_typed(embedding, _TWO_REQUESTS_OP_ID, two_requests_handler)

    result = await h.run(_TWO_REQUESTS_OP_ID)

    assert sent == ["GET /cronjobs/one", "POST /jobs"]
    assert result.status == "ok", result


async def test_write_is_not_sent_when_the_block_list_cannot_be_read(
    embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register_typed(embedding, _TWO_REQUESTS_OP_ID, two_requests_handler)
    after_first.append(_unreadable(monkeypatch))

    result = await h.run(_TWO_REQUESTS_OP_ID)

    assert sent == ["GET /cronjobs/one"]
    assert result.status == "error", result
    assert result.extras["error_code"] == "principal_block_check_unavailable"
    assert "cannot read the block list" in str(result.error)
    row = _dispatch_row(await _audit_rows(), _TWO_REQUESTS_OP_ID)
    assert row.payload["result_status"] == "error"


async def test_second_write_of_a_multi_step_operation_is_not_sent_and_the_answer_names_the_first(
    embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_steps(session, embedding, _WRITE_AFTER_STEP_OP_ID, "write_after_step_composite")
    after_first.append(_block_the_caller)

    result = await h.run(_WRITE_AFTER_STEP_OP_ID)

    assert sent == [f"{_STEP_OP_ID}:1"]
    assert result.status == "denied", result
    assert result.extras["execution_outcome"] == "uncertain"
    assert "A part of the operation may have run" in str(result.error)
    assert f"These steps were sent: `{_STEP_OP_ID}`." in str(result.error)
    assert "The steps after them were not sent." in str(result.error)
    assert _DIRECT_STEP not in str(result.error)
    assert "nothing" not in str(result.error).lower()
    row = _dispatch_row(await _audit_rows(), _WRITE_AFTER_STEP_OP_ID)
    assert row.payload["result_status"] == "denied"


async def test_step_that_waited_in_its_policy_gate_is_not_sent_after_another_step_was_refused(
    embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register_steps(
        session, embedding, _RACE_OP_ID, "step_waits_while_other_refused_composite"
    )
    race_events.extend([asyncio.Event(), asyncio.Event()])
    after_first.append(_unreadable(monkeypatch))
    after_refusal.append(_readable(monkeypatch))
    gate = composite_module._gate_subop
    step_a_waiting, step_b_refused = race_events

    async def _hold_step_a(**kwargs: Any) -> Any:
        if kwargs["params"]["name"] == "a":
            step_a_waiting.set()
            await step_b_refused.wait()
        return await gate(**kwargs)

    monkeypatch.setattr(composite_module, "_gate_subop", _hold_step_a)

    result = await h.run(_RACE_OP_ID)

    assert sent == []
    assert result.status == "error", result
    assert result.extras["error_code"] == "principal_block_check_unavailable"


@pytest.mark.parametrize(
    ("op_id", "handler"),
    [
        pytest.param(_GOVERNED_OP_ID, "governed_step_composite", id="run_governed_subop"),
        pytest.param(_GATED_OP_ID, "gated_step_composite", id="enforce_subop_policy"),
    ],
)
async def test_governed_step_is_not_sent_when_its_login_is_blocked_during_its_policy_gate(
    embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    handler: str,
) -> None:
    await _register_steps(session, embedding, op_id, handler)
    _block_during_the_gate(monkeypatch)

    result = await h.run(op_id)

    assert sent == []
    assert result.status == "denied", result
    assert result.extras["error_code"] == "principal_blocked"


async def test_handler_of_a_call_blocked_while_it_waited_is_not_started(
    embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register_typed(embedding, _TWO_REQUESTS_OP_ID, two_requests_handler)
    original = principal_block.raise_if_send_refused
    calls: list[int] = []

    async def _block_before_the_handler() -> None:
        if not calls:
            await _block_the_caller()
        calls.append(1)
        await original()

    monkeypatch.setattr(
        "meho_backplane.operations.dispatcher.raise_if_send_refused", _block_before_the_handler
    )

    result = await h.run(_TWO_REQUESTS_OP_ID)

    assert sent == []
    assert result.status == "denied", result
    assert result.extras["error_code"] == "principal_blocked"


# ---------------------------------------------------------------------------
# Each shared send place on its own
# ---------------------------------------------------------------------------


def _operator() -> Operator:
    return h.operator()


async def test_http_request_hook_stops_the_request_of_a_blocked_login(
    session: AsyncSession,
) -> None:
    transport_calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        transport_calls.append(request.url.path)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        base_url="https://target.test",
        transport=httpx.MockTransport(_handler),
        event_hooks={"request": [_note_request_sent]},
    ) as client:
        operator = _operator()
        with tracking_run(is_step=False, login=(operator.sub, operator.tenant_id)):
            await client.get("/one")
            await _block_the_caller()
            with pytest.raises(PrincipalBlockedStepError):
                await client.post("/two")

    assert transport_calls == ["/one"]


class _SlowCredentialConnector(HttpConnector):
    """A connector whose credentials arrive after an admin blocked the caller."""

    product = "slow-credentials"

    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self._calls = calls

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        # The wait for credentials ends after the block took effect.
        await _block_the_caller()
        reset_block_cache()
        return {"Authorization": "x"}

    async def _http_client(self, target: Any) -> httpx.AsyncClient:
        def _handler(request: httpx.Request) -> httpx.Response:
            self._calls.append(request.url.path)
            return httpx.Response(200, json={})

        return httpx.AsyncClient(
            base_url="https://target.test",
            transport=httpx.MockTransport(_handler),
            event_hooks={"request": [_note_request_sent]},
        )

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


async def test_post_json_stops_after_a_slow_credential_wait(session: AsyncSession) -> None:
    transport_calls: list[str] = []
    connector = _SlowCredentialConnector(transport_calls)
    operator = _operator()
    target = SimpleNamespace(name="t", host="target.test", port=443)

    with (
        tracking_run(is_step=False, login=(operator.sub, operator.tenant_id)),
        pytest.raises(PrincipalBlockedStepError),
    ):
        await connector._post_json(target, "/write", operator=operator, json={})

    assert transport_calls == []


async def test_kubernetes_client_wrapper_stops_the_request_of_a_blocked_login(
    session: AsyncSession,
) -> None:
    rest = _kubernetes_client().rest_client
    operator = _operator()
    with tracking_run(is_step=False, login=(operator.sub, operator.tenant_id)):
        await rest.request("GET", "/a")
        await _block_the_caller()
        with pytest.raises(PrincipalBlockedStepError):
            await rest.request("POST", "/b")

    assert sent == ["GET /a"]


class _SshStandIn(ssh_module.SshConnector):
    async def fingerprint(self, target: Any, operator: Any = None) -> Any:
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError


class _EmptyStream:
    async def read(self, _n: int) -> bytes:
        return b""


class _FinishedProcess:
    """A remote command that ended at once with no output."""

    def __init__(self, cmd: str) -> None:
        self.command = cmd
        self.env: dict[str, str] = {}
        self.subsystem = None
        self.exit_status = 0
        self.exit_signal = None
        self.returncode = 0
        self.stdout = _EmptyStream()
        self.stderr = _EmptyStream()

    async def __aenter__(self) -> _FinishedProcess:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _RecordingConnection:
    """A pooled SSH connection; it counts the commands that went out."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    def create_process(self, cmd: str, **_kwargs: Any) -> _FinishedProcess:
        self.ran.append(cmd)
        return _FinishedProcess(cmd)


def _ssh_connector(monkeypatch: pytest.MonkeyPatch) -> tuple[_SshStandIn, _RecordingConnection]:
    conn = _RecordingConnection()

    async def _connect(self: Any, target: Any, operator: Any) -> _RecordingConnection:
        return conn

    monkeypatch.setattr(ssh_module.SshConnector, "_connect", _connect)
    return _SshStandIn(), conn


async def test_ssh_command_runner_stops_the_command_of_a_blocked_login(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector, conn = _ssh_connector(monkeypatch)
    target = SimpleNamespace(name="stand-in")
    operator = _operator()
    with tracking_run(is_step=False, login=(operator.sub, operator.tenant_id)):
        await connector._run_command(target, "first", operator=operator)
        await _block_the_caller()
        with pytest.raises(PrincipalBlockedStepError):
            await connector._run_command(target, "second", operator=operator)

    assert conn.ran == ["first"]


@pytest.mark.parametrize(
    "helper", [linux_sudo.run_remote_bash_with_sudo, rke2_sudo.run_remote_bash_with_sudo]
)
async def test_sudo_helper_stops_the_second_sudo_command_of_a_blocked_login(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    helper: Callable[..., Awaitable[Any]],
) -> None:
    connector, conn = _ssh_connector(monkeypatch)
    target = SimpleNamespace(name="stand-in")
    operator = _operator()
    with tracking_run(is_step=False, login=(operator.sub, operator.tenant_id)):
        await helper(connector, target, "echo one", operator=operator, sudo_password="pw")
        await _block_the_caller()
        with pytest.raises(PrincipalBlockedStepError):
            await helper(connector, target, "echo two", operator=operator, sudo_password="pw")

    assert len(conn.ran) == 1


async def test_outside_a_running_call_nothing_is_checked(session: AsyncSession) -> None:
    await _block_the_caller()
    await raise_if_send_refused()


async def test_a_fresh_copy_of_the_list_costs_no_database_read_per_send(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[int] = []
    original = principal_block._load_entries

    async def _counting(sessionmaker: Any) -> frozenset[tuple[str, str]]:
        reads.append(1)
        return await original(sessionmaker)

    monkeypatch.setattr("meho_backplane.auth.principal_block._load_entries", _counting)
    operator = _operator()
    with tracking_run(is_step=False, login=(operator.sub, operator.tenant_id)):
        for _ in range(5):
            await raise_if_send_refused()

    assert len(reads) == 1


# ---------------------------------------------------------------------------
# Open live streams
# ---------------------------------------------------------------------------


async def test_feed_stream_ends_once_the_login_is_blocked(session: AsyncSession) -> None:
    operator = _operator()
    liveness = token_liveness(operator)
    assert await liveness.alive()

    await _block_the_caller()

    assert not await liveness.alive()


async def test_feed_stream_ends_within_five_seconds_of_a_block(session: AsyncSession) -> None:
    from meho_backplane.api.v1.feed import _BLOCK_COPY_MAX_AGE_SECONDS

    liveness = token_liveness(_operator())
    assert await liveness.alive()
    # The longest wait of the stream plus the oldest copy of the list it accepts.
    worst_case_seconds = liveness.wait_ms() / 1000 + _BLOCK_COPY_MAX_AGE_SECONDS
    assert worst_case_seconds <= 5.0

    # The check after the longest wait finds an old copy of the list in place.
    old_copy = principal_block._cache
    assert old_copy is not None
    await _block_the_caller()
    principal_block._cache = old_copy
    old_copy.loaded_at -= _BLOCK_COPY_MAX_AGE_SECONDS + 0.1

    assert not await liveness.alive()


async def test_feed_stream_ends_when_the_block_list_cannot_be_read(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    liveness = token_liveness(_operator())
    assert await liveness.alive()

    await _unreadable(monkeypatch)()

    assert not await liveness.alive()


async def test_console_stream_ends_once_the_login_is_blocked(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.requests import Request

    from meho_backplane.ui.auth.middleware import UISessionContext
    from meho_backplane.ui.routes.broadcast import stream as stream_module

    operator = _operator()
    ctx = UISessionContext(
        session_id=uuid.uuid4(), operator_sub=operator.sub, tenant_id=h.TENANT_ID
    )
    monkeypatch.setattr(stream_module, "session_ref", lambda _session_id: "ref")
    liveness = stream_module.session_liveness(Request({"type": "http", "headers": []}), ctx)
    assert await liveness.alive()

    await _block_the_caller()

    assert not await liveness.alive()
