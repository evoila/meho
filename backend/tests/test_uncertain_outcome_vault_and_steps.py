# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Uncertain results of Vault changes, of multi-step operations and of wrapped faults.

* A change whose Vault client (``requests``) lost the answer is stored as
  ``uncertain``; a fault of the read of the target's secret stays a plain
  failure that can run again.
* When a step of a multi-step operation ends ``uncertain`` and the
  operation then fails, the operation's result is ``uncertain`` too, with
  the list of its steps and the state of each.
* A timeout that a connector wraps in another error (``raise ... from
  exc``) is still seen.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import requests.exceptions

import meho_backplane.connectors._shared.vault_creds as vault_creds_module
import meho_backplane.operations.dispatcher as dispatcher_module
from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultUnreachableError
from meho_backplane.connectors._shared.vault_creds import TargetSecretReadFailedError
from meho_backplane.connectors._shared.wrapped_creds import WrappedCredentialError
from meho_backplane.connectors.adapters import ssh as ssh_module
from meho_backplane.connectors.pfsense import ops_openvpn_user
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.operations import dispatch, register_typed_operation
from meho_backplane.operations.approval_queue import ApprovalResultState, approval_result_state
from meho_backplane.operations.approval_rerun import RerunNotAllowedError, rerun_failed_request
from meho_backplane.operations.typed_register import register_composite_operation
from meho_backplane.operations.uncertain_outcome import connection_lost_after_send
from tests.test_uncertain_outcome_any_connection import (
    _CONNECTOR_ID,
    _PARAMS,
    _assert_plain_failure,
    _assert_uncertain,
    _command_sent_then,
    _DemoSshConnector,
    _FakeDispatch,
    _operator,
    _park_with_result,
    _register,
    _required_settings_env,
    _reset_module_state,
    _row,
    _run,
    _Target,
    ssh_connected,
    stub_embedding_service,
)

# The fixtures of the module above, so this module sets up the same way.
__all__ = [
    "_required_settings_env",
    "_reset_module_state",
    "ssh_connected",
    "stub_embedding_service",
]


def _wrapped(outer: type[Exception], cause: BaseException) -> Exception:
    """*outer* raised ``from`` *cause*, as a connector wraps a fault."""
    try:
        raise cause
    except BaseException as exc:
        try:
            raise outer("the call failed") from exc
        except outer as wrapped:
            return wrapped
    raise AssertionError("unreachable")


#: The fault :func:`_raising_handler` raises; set by :func:`_register_raising`.
_FAULT: list[BaseException] = []


async def _raising_handler(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    raise _FAULT[0]


async def _register_raising(op_id: str, fault: BaseException, embedding: AsyncMock) -> None:
    """A dangerous typed op whose handler raises *fault* after it sent its call."""
    _FAULT[:] = [fault]
    register_connector_v2(product="sshdemo", version="", impl_id="", cls=_DemoSshConnector)
    await register_typed_operation(
        product="sshdemo",
        version="1.x",
        impl_id="sshdemo",
        op_id=op_id,
        handler=_raising_handler,
        summary="Demo change.",
        description="Demo change that fails after it sent its call.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="dangerous",
    )


async def _dispatch_approved(op_id: str) -> OperationResult:
    return await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=_Target(),
        params=dict(_PARAMS),
        _approved=True,
    )


async def _assert_rerun_refused(result: OperationResult, monkeypatch: pytest.MonkeyPatch) -> None:
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


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        requests.exceptions.ReadTimeout("read timed out"),
        requests.exceptions.ConnectionError("connection aborted"),
        requests.exceptions.ChunkedEncodingError("answer cut"),
    ],
    ids=["read-timeout", "connection-error", "chunked"],
)
async def test_vault_change_that_loses_its_answer_is_uncertain_and_not_run_again(
    fault: BaseException, stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register_raising("sshdemo.secret.write", fault, stub_embedding_service)

    result = await _dispatch_approved("sshdemo.secret.write")

    _assert_uncertain(result)
    await _assert_rerun_refused(result, monkeypatch)


@pytest.mark.asyncio
async def test_vault_change_whose_connect_timed_out_stays_a_plain_failure(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register_raising(
        "sshdemo.secret.write",
        requests.exceptions.ConnectTimeout("connect timed out"),
        stub_embedding_service,
    )

    _assert_plain_failure(await _dispatch_approved("sshdemo.secret.write"))


class _TimingOutBackend:
    async def load_secret_data(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        raise requests.exceptions.ReadTimeout("read timed out")


@pytest.mark.asyncio
async def test_secret_read_that_times_out_stays_a_plain_failure_that_can_run_again(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_capped = _command_sent_then(None)
    monkeypatch.setattr(ssh_module, "run_capped", run_capped)
    monkeypatch.setattr(
        vault_creds_module, "resolve_credential_backend", lambda kind: _TimingOutBackend()
    )

    result = await _run("sshdemo.zone.reload", "dangerous", stub_embedding_service)

    assert run_capped.sent == []
    _assert_plain_failure(result)
    assert result.extras["exception_class"] == "TargetSecretReadFailedError"

    request_id = await _park_with_result(result.model_dump(mode="json"))
    fake = _FakeDispatch()
    monkeypatch.setattr(dispatcher_module, "dispatch", fake)
    outcome = await rerun_failed_request(operator=_operator(), request_id=request_id)
    assert len(fake.calls) == 1
    assert outcome.result.status == "ok"


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ReadTimeout(),
        requests.exceptions.ConnectionError(),
        requests.exceptions.ChunkedEncodingError(),
    ],
)
def test_requests_faults_after_sending(exc: BaseException) -> None:
    assert connection_lost_after_send(exc)


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectTimeout(),
        _wrapped(VaultUnreachableError, requests.exceptions.ReadTimeout()),
        TargetSecretReadFailedError(target_name="t", cause=requests.exceptions.ReadTimeout()),
        _wrapped(WrappedCredentialError, requests.exceptions.ReadTimeout()),
    ],
    ids=["connect-timeout", "vault-login", "target-secret-read", "wrapped-credential"],
)
def test_faults_before_sending_stay_plain(exc: BaseException) -> None:
    assert not connection_lost_after_send(exc)


# ---------------------------------------------------------------------------
# A wrapped timeout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "lost"),
    [
        (_wrapped(RuntimeError, TimeoutError()), True),
        (_wrapped(RuntimeError, _wrapped(ValueError, TimeoutError())), True),
        (_wrapped(RuntimeError, httpx.ReadTimeout("slow")), True),
        (_wrapped(RuntimeError, ConnectionRefusedError()), False),
        (_wrapped(RuntimeError, httpx.ConnectTimeout("slow")), False),
        (_wrapped(RuntimeError, ValueError("bad input")), False),
    ],
    ids=["timeout", "twice", "httpx-read", "refused", "httpx-connect", "not-a-fault"],
)
def test_cause_chain_is_read(exc: BaseException, lost: bool) -> None:
    assert connection_lost_after_send(exc) is lost


@pytest.mark.asyncio
async def test_wrapped_timeout_of_a_change_is_uncertain(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register_raising(
        "sshdemo.zone.reload", _wrapped(RuntimeError, TimeoutError()), stub_embedding_service
    )

    _assert_uncertain(await _dispatch_approved("sshdemo.zone.reload"))


@pytest.mark.asyncio
async def test_pfsense_user_create_keeps_the_timeout_as_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ops_openvpn_user, "_apply_playback", AsyncMock(side_effect=TimeoutError()))

    with pytest.raises(RuntimeError) as exc_info:
        await ops_openvpn_user._play_create(
            AsyncMock(), object(), "vpnuser", "a VPN user", _operator()
        )

    assert connection_lost_after_send(exc_info.value)


# ---------------------------------------------------------------------------
# Multi-step operations
# ---------------------------------------------------------------------------

_MULTI_OP = "sshdemo.zone.multi"
_READ_STEP = "sshdemo.zone.check"
_CHANGE_STEP = "sshdemo.zone.reload"
_STEPS = [
    {"op_id": _READ_STEP, "state": "done"},
    {"op_id": _CHANGE_STEP, "state": "unknown"},
]


async def _ok_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"checked": True}


async def _run_steps(dispatch_child: Any) -> OperationResult:
    """Run the read step, then the change step; return the result of the change."""
    await dispatch_child(connector_id=_CONNECTOR_ID, op_id=_READ_STEP, params={})
    result: OperationResult = await dispatch_child(
        connector_id=_CONNECTOR_ID, op_id=_CHANGE_STEP, params={}
    )
    return result


async def _hands_back_the_step(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: Any
) -> Any:
    return await _run_steps(dispatch_child)


async def _returns_its_own_error(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: Any
) -> Any:
    await _run_steps(dispatch_child)
    return OperationResult(
        status="error",
        op_id=_MULTI_OP,
        duration_ms=0.0,
        error="multi_failed: the change step did not answer",
        extras={"error_code": "multi_failed"},
    )


async def _returns_an_error_envelope(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: Any
) -> Any:
    await _run_steps(dispatch_child)
    return {
        "status": "multi_failed",
        "issues": [{"severity": "error", "message": "the change step did not answer"}],
    }


async def _raises_its_own_error(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: Any
) -> Any:
    await _run_steps(dispatch_child)
    raise RuntimeError("the change step did not answer")


async def _returns_ok(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: Any
) -> Any:
    await _run_steps(dispatch_child)
    return {"status": "done"}


async def _register_multi(handler: Any, embedding: AsyncMock) -> None:
    await _register(_CHANGE_STEP, "dangerous", embedding)
    await register_typed_operation(
        product="sshdemo",
        version="1.x",
        impl_id="sshdemo",
        op_id=_READ_STEP,
        handler=_ok_handler,
        summary="Demo check.",
        description="Demo check that answers ok.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="safe",
    )
    await register_composite_operation(
        product="sshdemo",
        version="1.x",
        impl_id="sshdemo",
        op_id=_MULTI_OP,
        handler=handler,
        summary="Demo multi-step change.",
        description="Checks, then changes.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="dangerous",
        embedding_service=embedding,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("ssh_connected")
@pytest.mark.parametrize(
    "handler",
    [
        _hands_back_the_step,
        _returns_its_own_error,
        _returns_an_error_envelope,
        _raises_its_own_error,
    ],
    ids=["hands-back-step", "returns-own-error", "returns-error-envelope", "raises-own-error"],
)
async def test_failed_multi_step_run_with_an_uncertain_step_is_uncertain(
    handler: Any, stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_capped = _command_sent_then(None)
    monkeypatch.setattr(ssh_module, "run_capped", run_capped)
    await _register_multi(handler, stub_embedding_service)

    result = await _dispatch_approved(_MULTI_OP)

    assert run_capped.sent == ["rndc reload example.test"]
    _assert_uncertain(result)
    assert result.extras["steps"] == _STEPS
    assert result.error is not None
    assert result.error.count("Do not run it again") == 1
    await _assert_rerun_refused(result, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.usefixtures("ssh_connected")
async def test_multi_step_run_that_ends_ok_stays_ok(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ssh_module, "run_capped", _command_sent_then(None))
    await _register_multi(_returns_ok, stub_embedding_service)

    result = await _dispatch_approved(_MULTI_OP)

    assert result.status == "ok"


@pytest.mark.asyncio
@pytest.mark.usefixtures("ssh_connected")
async def test_multi_step_run_whose_steps_failed_plainly_stays_a_plain_failure(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ssh_module, "run_capped", _command_sent_then(ConnectionRefusedError("refused"))
    )
    await _register_multi(_raises_its_own_error, stub_embedding_service)

    result = await _dispatch_approved(_MULTI_OP)

    _assert_plain_failure(result)
    assert "steps" not in result.extras
