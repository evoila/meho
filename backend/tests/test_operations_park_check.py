# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The opt-in check that runs before a call waits for approval.

An op registers a check with ``register_park_check``. When a call of that
op would wait for approval, the check runs first and can refuse the call,
for example a secret given as a plain value. These tests prove:

* the call is refused before it waits: no approval request is stored, the
  handler (the vendor call) never runs, and one ``denied`` audit row is
  written;
* ``preview_operation`` gives the same refusal;
* a step of a multi-step operation is refused the same way;
* the secret value is in none of: the error, the audit rows, the flight
  recorder traces, the approval step events and the event outbox;
* ops without a check, and calls that do not wait, work as before;
* the check is fail-closed, and an expired login is renewed once.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AddonStepEvent,
    ApprovalRequest,
    AuditLog,
    DispatchTrace,
    DispatchTraceSpan,
    EndpointDescriptor,
    EventOutbox,
    Tenant,
)
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._errors import result_park_check_refused
from meho_backplane.operations._park_check import register_park_check, run_park_check
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.settings import get_settings

#: A made-up secret. It must never show up in any output.
_SECRET = "made-up-plain-password-4711"
_CONNECTOR_ID = "vault-1.x"
_OP = "vault.demo.connect"
_STEP_OP = "vault.demo.connect_step"
_COMPOSITE_OP = "vault.demo.connect_all"

#: Every call of the op handler; a refused call must leave it empty.
_HANDLER_CALLS: list[dict[str, Any]] = []


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    _HANDLER_CALLS.clear()
    yield
    reset_dispatcher_caches()
    clear_registry()
    _HANDLER_CALLS.clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


class _VaultConnector(Connector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


class _FakeTarget:
    def __init__(self, tenant_id: uuid.UUID) -> None:
        self.product = "vault"
        self.fingerprint = type("_F", (), {"version": None})()
        self.preferred_impl_id: str | None = None
        self.id = uuid.uuid4()
        self.tenant_id = tenant_id
        self.name = "demo-target"
        self.host = "test.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


def _operator(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="op-park-check",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """The op's handler: the vendor call. Records that it ran."""
    _HANDLER_CALLS.append(params)
    return {"connected": True}


async def _composite_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any] | OperationResult:
    """A multi-step operation whose one step needs approval and has a check."""
    from meho_backplane.operations.composite import enforce_subop_policy

    step_params = {"password": params.get("password")}
    gate = await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=_STEP_OP,
        safety_level="caution",
        requires_approval=True,
        target=target,
        params=step_params,
    )
    if gate is not None:
        return gate
    _HANDLER_CALLS.append(step_params)
    return {"step": "ran"}


async def _refuse_plain_password(ctx: PreviewContext) -> str | None:
    """Refuse ``password`` given as a plain value; a reference is fine."""
    if isinstance(ctx.params.get("password"), str):
        return "param 'password' is a secret; give it as a reference, not as a plain value"
    return None


async def _seed_tenant() -> uuid.UUID:
    """A tenant with the flight recorder on, so a captured trace would show the secret."""
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            Tenant(
                id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="t", flight_recorder_enabled=True
            )
        )
        await s.commit()
    return tenant_id


async def _register_op(embedding: AsyncMock, *, waits: bool = True) -> None:
    """Register the op; with ``waits=False`` a person's call runs at once."""
    register_connector_v2(product="vault", version="", impl_id="", cls=_VaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_OP,
        handler=_handler,
        summary="Connect with a password.",
        description="Takes a password; waits for approval.",
        parameter_schema={"type": "object"},
        safety_level="caution" if waits else "safe",
        requires_approval=waits,
        when_to_use=None,
        embedding_service=embedding,
    )


async def _seed_composite(embedding: list[float]) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_VaultConnector)
    async with get_sessionmaker()() as s:
        s.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id=_COMPOSITE_OP,
                source_kind="composite",
                method=None,
                path=None,
                handler_ref="tests.test_operations_park_check._composite_handler",
                summary="Multi-step connect.",
                description="One step needs approval.",
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=embedding,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await s.commit()


async def _rows(model: Any) -> list[Any]:
    async with get_sessionmaker()() as s:
        return list((await s.execute(select(model))).scalars().all())


async def _approval_count() -> int:
    async with get_sessionmaker()() as s:
        return int(await s.scalar(select(func.count()).select_from(ApprovalRequest)) or 0)


def _row_text(row: Any) -> str:
    """Every column value of *row* as one text, for a search for the secret."""
    return repr({c.key: getattr(row, c.key) for c in row.__table__.columns})


async def _assert_secret_stored_nowhere() -> None:
    for model in (
        ApprovalRequest,
        AuditLog,
        DispatchTrace,
        DispatchTraceSpan,
        AddonStepEvent,
        EventOutbox,
    ):
        for row in await _rows(model):
            assert _SECRET not in _row_text(row), model.__name__


def _assert_secret_not_in_result(result: OperationResult) -> None:
    assert _SECRET not in (result.error or "")
    assert _SECRET not in repr(result.extras)
    assert _SECRET not in repr(result.result)


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_with_plain_secret_is_refused_before_it_waits(
    stub_embedding_service: AsyncMock,
) -> None:
    tenant_id = await _seed_tenant()
    await _register_op(stub_embedding_service)
    register_park_check(_OP, _refuse_plain_password)

    result = await dispatch(
        operator=_operator(tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_FakeTarget(tenant_id),
        params={"password": _SECRET},
    )

    assert result.status == "denied"
    assert result.extras["error_code"] == "refused_before_approval"
    assert "give it as a reference" in (result.error or "")
    assert "does not store this call for approval" in (result.error or "")
    _assert_secret_not_in_result(result)
    assert _HANDLER_CALLS == []
    assert await _approval_count() == 0

    audit = await _rows(AuditLog)
    assert [r.path for r in audit] == [_OP]
    assert audit[0].payload["result_status"] == "denied"
    assert audit[0].payload["error"]["error_code"] == "refused_before_approval"
    assert result.audit_id == audit[0].id
    assert await _rows(AddonStepEvent) == []
    await _assert_secret_stored_nowhere()


@pytest.mark.asyncio
async def test_call_that_passes_the_check_waits_for_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    tenant_id = await _seed_tenant()
    await _register_op(stub_embedding_service)
    register_park_check(_OP, _refuse_plain_password)

    result = await dispatch(
        operator=_operator(tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_FakeTarget(tenant_id),
        params={"password": {"vault_ref": "<secret-ref>"}},
    )

    assert result.status == "awaiting_approval", result.error
    assert await _approval_count() == 1
    assert _HANDLER_CALLS == []


@pytest.mark.asyncio
async def test_op_without_a_check_waits_as_before(stub_embedding_service: AsyncMock) -> None:
    tenant_id = await _seed_tenant()
    await _register_op(stub_embedding_service)

    result = await dispatch(
        operator=_operator(tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_FakeTarget(tenant_id),
        params={"password": _SECRET},
    )

    assert result.status == "awaiting_approval", result.error
    assert await _approval_count() == 1


@pytest.mark.asyncio
async def test_call_that_does_not_wait_runs_without_the_check(
    stub_embedding_service: AsyncMock,
) -> None:
    """The check guards only what is stored for approval; a call that runs at once skips it."""
    tenant_id = await _seed_tenant()
    await _register_op(stub_embedding_service, waits=False)
    check = AsyncMock(return_value="refused")
    register_park_check(_OP, check)

    result = await dispatch(
        operator=_operator(tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_FakeTarget(tenant_id),
        params={"password": _SECRET},
    )

    assert result.status == "ok", result.error
    assert _HANDLER_CALLS == [{"password": _SECRET}]
    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_that_raises_refuses_the_call(stub_embedding_service: AsyncMock) -> None:
    tenant_id = await _seed_tenant()
    await _register_op(stub_embedding_service)

    async def _boom(ctx: PreviewContext) -> str | None:
        raise RuntimeError(f"form read failed for {ctx.params['password']}")

    register_park_check(_OP, _boom)

    result = await dispatch(
        operator=_operator(tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_FakeTarget(tenant_id),
        params={"password": _SECRET},
    )

    assert result.status == "denied"
    assert result.extras["error_code"] == "refused_before_approval"
    assert "RuntimeError" in (result.error or "")
    _assert_secret_not_in_result(result)
    assert await _approval_count() == 0
    assert _HANDLER_CALLS == []
    await _assert_secret_stored_nowhere()


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_gives_the_same_refusal_as_the_call(
    stub_embedding_service: AsyncMock,
) -> None:
    tenant_id = await _seed_tenant()
    await _register_op(stub_embedding_service)
    register_park_check(_OP, _refuse_plain_password)
    operator = _operator(tenant_id)
    target = _FakeTarget(tenant_id)

    envelope = await preview_dispatch(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=target,
        params={"password": _SECRET},
    )
    called = await dispatch(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=target,
        params={"password": _SECRET},
    )

    assert envelope["status"] == "error"
    assert envelope["extras"]["error_code"] == "refused_before_approval"
    assert envelope["error"] == called.error
    assert envelope["extras"] == called.extras
    assert envelope["policy_verdict"] == "needs-approval"
    assert _SECRET not in repr(envelope)
    assert await _approval_count() == 0
    assert _HANDLER_CALLS == []


@pytest.mark.asyncio
async def test_preview_that_passes_the_check_is_the_normal_preview(
    stub_embedding_service: AsyncMock,
) -> None:
    tenant_id = await _seed_tenant()
    await _register_op(stub_embedding_service)
    register_park_check(_OP, _refuse_plain_password)

    envelope = await preview_dispatch(
        operator=_operator(tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_FakeTarget(tenant_id),
        params={"password": {"vault_ref": "<secret-ref>"}},
    )

    assert envelope["status"] == "ok", envelope
    assert "preview_hash" in envelope


@pytest.mark.asyncio
async def test_preview_of_a_call_that_does_not_wait_skips_the_check(
    stub_embedding_service: AsyncMock,
) -> None:
    tenant_id = await _seed_tenant()
    await _register_op(stub_embedding_service, waits=False)
    check = AsyncMock(return_value="refused")
    register_park_check(_OP, check)

    envelope = await preview_dispatch(
        operator=_operator(tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_FakeTarget(tenant_id),
        params={"password": _SECRET},
    )

    assert envelope.get("extras", {}).get("error_code") != "refused_before_approval"
    check.assert_not_awaited()


# ---------------------------------------------------------------------------
# A step of a multi-step operation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_with_plain_secret_is_refused_before_it_waits(
    stub_embedding_service: AsyncMock,
) -> None:
    tenant_id = await _seed_tenant()
    await _seed_composite(stub_embedding_service.encode_one.return_value)
    register_park_check(_STEP_OP, _refuse_plain_password)

    result = await dispatch(
        operator=_operator(tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_COMPOSITE_OP,
        target=_FakeTarget(tenant_id),
        params={"password": _SECRET},
    )

    assert result.status == "denied"
    assert result.op_id == _STEP_OP
    assert result.extras["error_code"] == "refused_before_approval"
    _assert_secret_not_in_result(result)
    assert _HANDLER_CALLS == []
    assert await _approval_count() == 0

    audit = await _rows(AuditLog)
    (step_row,) = [r for r in audit if r.path == _STEP_OP]
    assert step_row.payload["result_status"] == "denied"
    assert step_row.payload["error"]["error_code"] == "refused_before_approval"
    assert [r for r in audit if r.path == "approval.request"] == []
    # The flight recorder captured the multi-step run; the secret is not in it.
    assert await _rows(DispatchTrace) != []
    await _assert_secret_stored_nowhere()


@pytest.mark.asyncio
async def test_step_that_passes_the_check_waits_for_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    tenant_id = await _seed_tenant()
    await _seed_composite(stub_embedding_service.encode_one.return_value)
    register_park_check(_STEP_OP, _refuse_plain_password)

    result = await dispatch(
        operator=_operator(tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_COMPOSITE_OP,
        target=_FakeTarget(tenant_id),
        params={"password": {"vault_ref": "<secret-ref>"}},
    )

    assert result.status == "awaiting_approval", result.error
    assert result.op_id == _STEP_OP
    assert await _approval_count() == 1


# ---------------------------------------------------------------------------
# run_park_check and the refusal text
# ---------------------------------------------------------------------------


def _ctx(op_id: str, connector_instance: Any = None) -> PreviewContext:
    return PreviewContext(
        descriptor=EndpointDescriptor(op_id=op_id),
        connector_instance=connector_instance,
        operator=_operator(uuid.uuid4()),
        target=object(),
        params={"password": _SECRET},
    )


@pytest.mark.asyncio
async def test_run_park_check_is_none_without_a_check() -> None:
    assert await run_park_check(_ctx("vault.demo.unregistered")) is None


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://vendor.test.invalid/form")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"status {status_code}", request=request, response=response)


class _SessionConnector:
    """A connector stand-in with the dispatcher's login-drop hook."""

    def __init__(self) -> None:
        self.invalidate_session = AsyncMock(return_value=None)


def _rejecting_check(statuses: list[int]) -> tuple[Any, list[int]]:
    """A check that raises each status in turn, then passes; counts its runs."""
    runs: list[int] = []

    async def _check(_ctx: PreviewContext) -> str | None:
        runs.append(1)
        if statuses:
            raise _status_error(statuses.pop(0))
        return None

    return _check, runs


@pytest.mark.asyncio
async def test_run_park_check_renews_a_rejected_login_once() -> None:
    check, runs = _rejecting_check([401])
    register_park_check("vault.demo.session", check)
    connector = _SessionConnector()
    ctx = _ctx("vault.demo.session", connector)

    assert await run_park_check(ctx) is None
    assert len(runs) == 2
    connector.invalidate_session.assert_awaited_once()
    args, kwargs = connector.invalidate_session.await_args
    assert args == (ctx.target,)
    assert isinstance(kwargs["rejected"], httpx.Request)


@pytest.mark.asyncio
async def test_run_park_check_refuses_when_the_new_login_is_rejected_too() -> None:
    check, runs = _rejecting_check([401, 401])
    register_park_check("vault.demo.session_twice", check)
    connector = _SessionConnector()

    reason = await run_park_check(_ctx("vault.demo.session_twice", connector))

    assert reason is not None
    assert "HTTPStatusError" in reason
    assert len(runs) == 2
    connector.invalidate_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_park_check_does_not_retry_a_status_that_is_not_a_login_failure() -> None:
    check, runs = _rejecting_check([404])
    register_park_check("vault.demo.not_found", check)
    connector = _SessionConnector()

    reason = await run_park_check(_ctx("vault.demo.not_found", connector))

    assert reason is not None
    assert len(runs) == 1
    connector.invalidate_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_park_check_does_not_retry_without_the_login_hook() -> None:
    check, runs = _rejecting_check([401])
    register_park_check("vault.demo.no_hook", check)

    reason = await run_park_check(_ctx("vault.demo.no_hook", object()))

    assert reason is not None
    assert len(runs) == 1


def test_refusal_text_hides_a_secret_shaped_reason() -> None:
    """A reason that carries a credential shape is redacted before it is shown."""
    token = "Bearer madeupbearertokenvalue0123456789"
    result = result_park_check_refused("vault.demo.connect", f"bad token {token}", 1.0)
    assert result.status == "denied"
    assert result.extras["error_code"] == "refused_before_approval"
    assert token not in (result.error or "")
    assert token not in repr(result.extras)
