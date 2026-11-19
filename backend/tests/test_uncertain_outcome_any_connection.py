# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved change whose answer was lost is uncertain on every connection.

The dispatcher stores a change that timed out or lost its connection after
the call started as ``uncertain`` with ``retry`` ``False``: on SSH, on the
Kubernetes client and on the database drivers, not only on HTTP. The re-run
of an approved request refuses such a result, in its Python check and in
the SQL of its claim. An error before anything was sent, and a read that
times out, stay a plain failure that can run again.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import aiohttp
import asyncpg
import asyncssh
import pymongo.errors
import pytds
import pytest

import meho_backplane.operations.dispatcher as dispatcher_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.adapters import ssh as ssh_module
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.operations import (
    PassThroughReducer,
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalResultState,
    approval_result_state,
    create_pending_request,
    get_request,
)
from meho_backplane.operations.approval_rerun import (
    RerunNotAllowedError,
    _claim_rerun,
    rerun_failed_request,
)
from meho_backplane.operations.uncertain_outcome import (
    UNCERTAIN_ADVICE,
    connection_lost_after_send,
    envelope_is_uncertain,
)
from meho_backplane.settings import get_settings

_TENANT_ID = UUID("00000000-0000-0000-0000-00000000c0c0")
_REQUESTER_SUB = "agent:requester"
_CONNECTOR_ID = "sshdemo-1.x"
_PARAMS = {"zone": "example.test"}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("CHART_VERSION", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    set_default_reducer(PassThroughReducer())
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


class _DemoSshConnector(SshConnector):
    """An SSH connector of the test: it runs one command per call."""

    product = "sshdemo"
    version = "1.x"
    impl_id = "sshdemo"

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


_connector = _DemoSshConnector()


async def _ssh_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """A typed handler that sends one SSH command, like the SSH connectors do."""
    result = await _connector._run_command(
        target, "rndc reload example.test", operator=operator, timeout=0.05
    )
    return {"exit_status": result.exit_status}


class _Target:
    def __init__(self) -> None:
        self.product = "sshdemo"
        self.fingerprint = None
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = "demo-ssh-target"
        self.host = "ssh.example.test"
        self.port = 22
        self.auth_model = "shared_service_account"
        self.secret_ref = "tenants/demo/ssh"


def _operator() -> Operator:
    return Operator(
        sub=_REQUESTER_SUB,
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _register(op_id: str, safety_level: str, embedding: AsyncMock) -> None:
    register_connector_v2(product="sshdemo", version="", impl_id="", cls=_DemoSshConnector)
    await register_typed_operation(
        product="sshdemo",
        version="1.x",
        impl_id="sshdemo",
        op_id=op_id,
        handler=_ssh_handler,
        summary="Demo SSH op.",
        description="Demo SSH op that sends one command.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level=safety_level,
    )


async def _run(op_id: str, safety_level: str, embedding: AsyncMock) -> OperationResult:
    await _register(op_id, safety_level, embedding)
    return await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=_Target(),
        params=dict(_PARAMS),
        _approved=safety_level != "safe",
    )


def _command_sent_then(fault: BaseException | None) -> Any:
    """A ``run_capped`` stand-in: the command is sent, then *fault* (or no answer)."""
    sent: list[str] = []

    async def _run_capped(conn: Any, cmd: str, **_kwargs: Any) -> Any:
        sent.append(cmd)
        if fault is not None:
            raise fault
        await asyncio.sleep(10)

    _run_capped.sent = sent  # type: ignore[attr-defined]
    return _run_capped


@pytest.fixture
def ssh_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSH login works; the command is what fails."""
    monkeypatch.setattr(_connector, "_connect", AsyncMock(return_value=object()))


def _assert_uncertain(result: OperationResult) -> None:
    assert result.status == "error"
    assert result.extras["execution_outcome"] == "uncertain"
    assert result.extras["retry"] is False
    assert result.extras["remediation"] == UNCERTAIN_ADVICE
    assert UNCERTAIN_ADVICE in (result.error or "")
    assert envelope_is_uncertain(result.model_dump(mode="json"))


def _assert_plain_failure(result: OperationResult) -> None:
    assert result.status == "error"
    assert "execution_outcome" not in result.extras
    assert "retry" not in result.extras
    assert not envelope_is_uncertain(result.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("ssh_connected")
async def test_dangerous_ssh_change_that_times_out_after_sending_is_uncertain(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_capped = _command_sent_then(None)
    monkeypatch.setattr(ssh_module, "run_capped", run_capped)

    result = await _run("sshdemo.zone.reload", "dangerous", stub_embedding_service)

    assert run_capped.sent == ["rndc reload example.test"]
    _assert_uncertain(result)
    assert result.extras["exception_class"] == "TimeoutError"


@pytest.mark.asyncio
@pytest.mark.usefixtures("ssh_connected")
async def test_dangerous_ssh_change_whose_connection_is_lost_is_uncertain(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_capped = _command_sent_then(asyncssh.ConnectionLost("connection lost"))
    monkeypatch.setattr(ssh_module, "run_capped", run_capped)

    result = await _run("sshdemo.zone.reload", "dangerous", stub_embedding_service)

    assert run_capped.sent == ["rndc reload example.test"]
    _assert_uncertain(result)
    assert result.extras["exception_class"] == "ConnectionLost"


@pytest.mark.asyncio
@pytest.mark.usefixtures("ssh_connected")
async def test_safe_ssh_read_that_times_out_stays_a_plain_failure(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ssh_module, "run_capped", _command_sent_then(None))

    result = await _run("sshdemo.zone.show", "safe", stub_embedding_service)

    _assert_plain_failure(result)
    assert result.extras["exception_class"] == "TimeoutError"


@pytest.mark.asyncio
async def test_failed_secret_lookup_before_sending_stays_a_plain_failure(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_capped = _command_sent_then(None)
    monkeypatch.setattr(ssh_module, "run_capped", run_capped)
    monkeypatch.setattr(
        _connector,
        "_resolve_secret",
        AsyncMock(side_effect=VaultCredentialsReadError("target 'demo-ssh-target': no secret")),
    )

    result = await _run("sshdemo.zone.reload", "dangerous", stub_embedding_service)

    assert run_capped.sent == []
    _assert_plain_failure(result)


@pytest.mark.asyncio
async def test_refused_ssh_connection_stays_a_plain_failure(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_capped = _command_sent_then(None)
    monkeypatch.setattr(ssh_module, "run_capped", run_capped)
    monkeypatch.setattr(
        _connector,
        "_auth_config",
        AsyncMock(return_value={"username": "demo", "known_hosts": None, "password": "x"}),
    )
    monkeypatch.setattr(
        ssh_module, "_dial", AsyncMock(side_effect=ConnectionRefusedError("refused"))
    )

    result = await _run("sshdemo.zone.reload", "dangerous", stub_embedding_service)

    assert run_capped.sent == []
    _assert_plain_failure(result)


# ---------------------------------------------------------------------------
# The list of "connection lost" types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        ConnectionResetError(),
        BrokenPipeError(),
        ConnectionAbortedError(),
        asyncssh.ConnectionLost("lost"),
        aiohttp.ServerDisconnectedError(),
        aiohttp.SocketTimeoutError(),
        aiohttp.ClientPayloadError("cut"),
        pymongo.errors.AutoReconnect("lost"),
        pymongo.errors.NetworkTimeout("slow"),
        asyncpg.exceptions.ConnectionDoesNotExistError("lost"),
        pytds.TimeoutError("slow"),
        pytds.ClosedConnectionError(),
    ],
)
def test_lost_after_send(exc: BaseException) -> None:
    assert connection_lost_after_send(exc)


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError(),
        aiohttp.ConnectionTimeoutError(),
        pymongo.errors.ServerSelectionTimeoutError("no server"),
        OSError("no route to host"),
        RuntimeError("connection reset"),
        ValueError("bad input"),
        VaultCredentialsReadError("no secret"),
    ],
)
def test_not_sent_or_not_a_connection_fault(exc: BaseException) -> None:
    assert not connection_lost_after_send(exc)


# ---------------------------------------------------------------------------
# The re-run of an approved request
# ---------------------------------------------------------------------------


async def _park_with_result(resume_result: dict[str, Any]) -> uuid.UUID:
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="sshdemo.zone.reload",
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
        )
        request.status = ApprovalRequestStatus.APPROVED.value
        request.reviewed_by = "op:approver"
        request.decided_at = datetime.now(UTC)
        request.resumed_at = datetime.now(UTC)
        request.resume_result = resume_result
        await s.commit()
        return request.id


async def _row(request_id: uuid.UUID) -> Any:
    async with get_sessionmaker()() as s:
        return await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)


class _FakeDispatch:
    def __init__(self, fault: BaseException | None = None) -> None:
        self.fault = fault
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> OperationResult:
        self.calls.append(kwargs)
        if self.fault is not None:
            raise self.fault
        return OperationResult(
            status="ok", op_id=kwargs["op_id"], result={"done": True}, duration_ms=1.0
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("ssh_connected")
@pytest.mark.parametrize(
    "fault", [None, asyncssh.ConnectionLost("connection lost")], ids=["timeout", "lost"]
)
async def test_rerun_refuses_an_uncertain_ssh_change_and_runs_nothing(
    fault: BaseException | None,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssh_module, "run_capped", _command_sent_then(fault))
    result = await _run("sshdemo.zone.reload", "dangerous", stub_embedding_service)
    request_id = await _park_with_result(result.model_dump(mode="json"))
    fake = _FakeDispatch()
    monkeypatch.setattr(dispatcher_module, "dispatch", fake)

    with pytest.raises(RerunNotAllowedError) as exc_info:
        await rerun_failed_request(operator=_operator(), request_id=request_id)

    assert exc_info.value.state == "uncertain"
    assert fake.calls == []
    row = await _row(request_id)
    assert row.rerun_count == 0
    assert approval_result_state(row) is ApprovalResultState.UNCERTAIN


@pytest.mark.asyncio
@pytest.mark.usefixtures("ssh_connected")
async def test_claim_sql_does_not_pick_an_uncertain_result(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim alone, without the Python check before it, leaves it out."""
    monkeypatch.setattr(ssh_module, "run_capped", _command_sent_then(None))
    result = await _run("sshdemo.zone.reload", "dangerous", stub_embedding_service)
    request_id = await _park_with_result(result.model_dump(mode="json"))

    assert await _claim_rerun(_operator(), await _row(request_id)) is None

    row = await _row(request_id)
    assert row.rerun_count == 0
    assert approval_result_state(row) is ApprovalResultState.UNCERTAIN


@pytest.mark.asyncio
async def test_claim_sql_picks_a_plain_failure() -> None:
    request_id = await _park_with_result(
        {"status": "error", "op_id": "sshdemo.zone.reload", "error": "connector_error: x"}
    )

    # The first run was attempt 1, so the first re-run is attempt 2.
    assert await _claim_rerun(_operator(), await _row(request_id)) == 2


@pytest.mark.asyncio
async def test_failure_before_sending_can_run_again(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _connector,
        "_resolve_secret",
        AsyncMock(side_effect=VaultCredentialsReadError("target 'demo-ssh-target': no secret")),
    )
    result = await _run("sshdemo.zone.reload", "dangerous", stub_embedding_service)
    request_id = await _park_with_result(result.model_dump(mode="json"))
    fake = _FakeDispatch()
    monkeypatch.setattr(dispatcher_module, "dispatch", fake)

    outcome = await rerun_failed_request(operator=_operator(), request_id=request_id)

    assert len(fake.calls) == 1
    assert outcome.attempt == 2
    assert outcome.result.status == "ok"


@pytest.mark.asyncio
async def test_rerun_that_loses_its_connection_is_stored_as_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = await _park_with_result(
        {"status": "error", "op_id": "sshdemo.zone.reload", "error": "connector_error: x"}
    )
    monkeypatch.setattr(dispatcher_module, "dispatch", _FakeDispatch(TimeoutError()))

    with pytest.raises(TimeoutError):
        await rerun_failed_request(operator=_operator(), request_id=request_id)

    row = await _row(request_id)
    assert approval_result_state(row) is ApprovalResultState.UNCERTAIN
    assert row.resume_result["extras"]["retry"] is False
    assert row.resume_result["error"].startswith("rerun_failed:")

    fake = _FakeDispatch()
    monkeypatch.setattr(dispatcher_module, "dispatch", fake)
    with pytest.raises(RerunNotAllowedError):
        await rerun_failed_request(operator=_operator(), request_id=request_id)
    assert fake.calls == []
