# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A step of a multi-step operation uses the override rows of its operation.

An override row of an operation (install-wide or for one tenant) can raise
its risk level or set its approval flag. A write step of a multi-step
operation that runs the same operation gets the same values as a direct
call, at the step gate and in the step preview:

* an override with ``requires_approval: true`` makes the step wait;
* an override that raises the risk level gives the step the same verdict
  and the same risk level on its approval request as the direct call;
* this holds for a person, an agent and a service login, for an install
  and a tenant override row, and with the policy switches off or on;
* the approval of the whole run covers a step the override raised to
  ``dangerous``, but not a step the override gives the approval flag;
* with no override row, the step keeps its declared values.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    EndpointDescriptor,
    OperationPolicyOverride,
    PermissionVerdict,
)
from meho_backplane.operations import _preview_steps, _validate
from meho_backplane.operations._preview_steps import preview_step_verdict
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.composite import (
    approved_run_var,
    composite_dispatch_var,
    composite_resume_scope_var,
    enforce_subop_policy,
)
from meho_backplane.operations.governed_subops import (
    register_governed_subops,
    reset_governed_subop_registry,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.op_policy import apply_step_policy
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000005712")
_FIX = {"product": "stepovr", "version": "1.x", "impl_id": "stepovr"}
_CONNECTOR = "stepovr-1.x"
_OP = "stepovr.disk.resize"
_COMPOSITE = "stepovr.composite.disk.grow"
_CALL: dict[str, Any] = {"connector_id": _CONNECTOR, "op_id": _OP, "params": {}}

_LEVELS = ("safe", "caution", "dangerous", "destructive")

#: ``(POLICY_CLASSIFICATION, POLICY_APPROVER_ROUTING)``.
_SWITCHES = pytest.mark.parametrize(
    ("classification", "routing"),
    [(False, False), (True, False), (True, True)],
    ids=["switches-off", "classification-on", "both-on"],
)
_SCOPES = pytest.mark.parametrize("scope", ["install", "tenant"])
_CALLERS = pytest.mark.parametrize("caller", ["person", "agent", "service"])


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _step_lists() -> Iterator[None]:
    reset_governed_subop_registry()
    yield
    reset_governed_subop_registry()


def _switches(monkeypatch: pytest.MonkeyPatch, *, classification: bool, routing: bool) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true" if classification else "false")
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true" if routing else "false")
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _register(embedding: AsyncMock) -> None:
    """A ``safe`` operation: every caller runs it at once without an override."""
    await register_typed_operation(
        **_FIX,
        op_id=_OP,
        handler=_handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema={"type": "object", "properties": {}},
        group_key="disks",
        when_to_use="Fixture group.",
        safety_level="safe",
        embedding_service=embedding,
    )


def _operator(caller: str) -> Operator:
    kind = {
        "person": PrincipalKind.USER,
        "agent": PrincipalKind.AGENT,
        "service": PrincipalKind.SERVICE,
    }[caller]
    return Operator(
        sub=f"{caller}:stepovr",
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _override(scope: str, **fields: Any) -> None:
    tenant_id = None if scope == "install" else _TENANT
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationPolicyOverride(
                tenant_id=tenant_id, op_id=_OP, set_by_sub="admin-a", **_FIX, **fields
            )
        )


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.op_id == _OP)
            .order_by(ApprovalRequest.created_at)
        )
        return list(result.scalars().all())


async def _step(caller: str) -> str | None:
    """Run the step gate for ``_OP`` declared ``safe``; ``None`` means the step runs."""
    result = await enforce_subop_policy(
        operator=_operator(caller),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level="safe",
        requires_approval=False,
        target=None,
        params={},
    )
    return None if result is None else result.status


async def _direct_and_step(caller: str) -> tuple[str, str | None]:
    """The direct call's status and the step's status of the same operation."""
    call = await call_operation(_operator(caller), _CALL)
    return call["status"], await _step(caller)


# ---------------------------------------------------------------------------
# The step gives the same answer as a direct call
# ---------------------------------------------------------------------------


@_CALLERS
@_SCOPES
@_SWITCHES
@pytest.mark.asyncio
async def test_an_override_approval_flag_makes_the_step_wait_like_a_direct_call(
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    scope: str,
    classification: bool,
    routing: bool,
) -> None:
    _switches(monkeypatch, classification=classification, routing=routing)
    await _register(embedding)
    await _override(scope, requires_approval=True)

    direct, step = await _direct_and_step(caller)

    assert direct == "awaiting_approval"
    assert step == "awaiting_approval"
    assert len(await _requests()) == 2


@_CALLERS
@_SCOPES
@_SWITCHES
@pytest.mark.asyncio
async def test_an_override_risk_level_gives_the_step_the_verdict_of_a_direct_call(
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    scope: str,
    classification: bool,
    routing: bool,
) -> None:
    _switches(monkeypatch, classification=classification, routing=routing)
    await _register(embedding)
    await _override(scope, safety_level="dangerous")

    direct, step = await _direct_and_step(caller)

    if direct == "awaiting_approval":
        assert step == "awaiting_approval"
        levels = [row.proposed_effect["safety_level"] for row in await _requests()]
        assert levels == ["dangerous", "dangerous"]
    else:
        assert step is None
        assert await _requests() == []


# ---------------------------------------------------------------------------
# The approval of the run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_approval_of_the_run_covers_a_step_the_override_made_dangerous(
    embedding: AsyncMock,
) -> None:
    await _register(embedding)
    await _override("install", safety_level="dangerous")
    assert await _step("service") == "awaiting_approval"

    token = approved_run_var.set(True)
    try:
        assert await _step("service") is None
    finally:
        approved_run_var.reset(token)


@pytest.mark.asyncio
async def test_the_approval_of_the_run_does_not_cover_a_step_with_the_override_flag(
    embedding: AsyncMock,
) -> None:
    await _register(embedding)
    await _override("install", requires_approval=True)

    token = approved_run_var.set(True)
    try:
        assert await _step("service") == "awaiting_approval"
    finally:
        approved_run_var.reset(token)


@pytest.mark.asyncio
async def test_a_resume_waits_again_at_a_step_with_the_override_flag(embedding: AsyncMock) -> None:
    await _register(embedding)
    await _override("install", requires_approval=True)

    dispatch_token = composite_dispatch_var.set((_COMPOSITE, {}))
    scope_token = composite_resume_scope_var.set(("stepovr.other.step", "hash"))
    try:
        result = await enforce_subop_policy(
            operator=_operator("service"),
            connector_id=_CONNECTOR,
            op_id=_OP,
            safety_level="safe",
            requires_approval=False,
            target=None,
            params={},
        )
    finally:
        composite_resume_scope_var.reset(scope_token)
        composite_dispatch_var.reset(dispatch_token)

    assert result is not None
    assert result.status == "awaiting_approval", result.error
    assert len(await _requests()) == 1


# ---------------------------------------------------------------------------
# The step preview gives the same answer as the step gate
# ---------------------------------------------------------------------------


@_CALLERS
@pytest.mark.parametrize(
    "fields",
    [{"requires_approval": True}, {"safety_level": "destructive"}, {}],
    ids=["approval-flag", "destructive", "no-override"],
)
@pytest.mark.usefixtures("_step_lists")
@pytest.mark.asyncio
async def test_the_preview_shows_the_answer_of_the_step_gate(
    embedding: AsyncMock, caller: str, fields: dict[str, Any]
) -> None:
    await _register(embedding)
    if fields:
        await _override("install", **fields)
    register_governed_subops(composite_op_id=_COMPOSITE, connector_id=_CONNECTOR, sub_op_ids=(_OP,))

    preview = await preview_step_verdict(
        operator=_operator(caller), op_id=_COMPOSITE, connector_id=_CONNECTOR, target=None
    )
    # The preview checks a write step as ``dangerous`` without the flag.
    gate = await enforce_subop_policy(
        operator=_operator(caller),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level="dangerous",
        requires_approval=False,
        target=None,
        params={},
    )

    # The gate refuses to start the wait of a destructive step; the
    # preview says that step waits. Either way, it does not run at once.
    if gate is None:
        assert preview is None
    else:
        assert gate.status in {"awaiting_approval", "denied"}
        assert preview is not None
        assert preview[0] is PermissionVerdict.NEEDS_APPROVAL


@_CALLERS
@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"requires_approval": True},
        {"safety_level": "destructive"},
        {"safety_level": "caution", "requires_approval": True},
    ],
    ids=["no-override", "approval-flag", "destructive", "caution-and-flag"],
)
@pytest.mark.usefixtures("_step_lists")
@pytest.mark.asyncio
async def test_the_preview_checks_the_step_with_the_values_of_the_step_gate(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch, caller: str, fields: dict[str, Any]
) -> None:
    """The preview and the gate pass the same risk level and approval flag to the check."""
    await _register(embedding)
    if fields:
        await _override("install", **fields)
    register_governed_subops(composite_op_id=_COMPOSITE, connector_id=_CONNECTOR, sub_op_ids=(_OP,))
    seen: list[tuple[str, bool]] = []

    async def _recording_gate(**kwargs: Any) -> Any:
        descriptor = kwargs["descriptor"]
        seen.append((descriptor.safety_level, descriptor.requires_approval))
        return await policy_gate(**kwargs)

    monkeypatch.setattr(_preview_steps, "policy_gate", _recording_gate)
    monkeypatch.setattr(_validate, "policy_gate", _recording_gate)

    await preview_step_verdict(
        operator=_operator(caller), op_id=_COMPOSITE, connector_id=_CONNECTOR, target=None
    )
    # The preview checks a write step as ``dangerous`` without the flag.
    await enforce_subop_policy(
        operator=_operator(caller),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level="dangerous",
        requires_approval=False,
        target=None,
        params={},
    )

    level = "destructive" if fields.get("safety_level") == "destructive" else "dangerous"
    flag = level == "destructive" or bool(fields.get("requires_approval"))
    assert seen == [(level, flag), (level, flag)]


@pytest.mark.asyncio
async def test_the_step_policy_reads_the_override_values(embedding: AsyncMock) -> None:
    await _register(embedding)
    await _override("tenant", safety_level="dangerous", requires_approval=True)
    step = _descriptor("safe", requires_approval=False)

    await apply_step_policy(tenant_id=_TENANT, step=step)

    assert (step.safety_level, step.requires_approval) == ("dangerous", True)


# ---------------------------------------------------------------------------
# Without an override row, or with one that turns approval off
# ---------------------------------------------------------------------------


def _descriptor(level: str, *, requires_approval: bool) -> EndpointDescriptor:
    return EndpointDescriptor(
        **_FIX,
        op_id=_OP,
        source_kind="composite",
        safety_level=level,
        requires_approval=requires_approval,
        parameter_schema={},
    )


@pytest.mark.parametrize("level", _LEVELS)
@pytest.mark.parametrize("requires_approval", [False, True])
@_SWITCHES
@pytest.mark.asyncio
async def test_without_an_override_the_step_keeps_its_declared_values(
    monkeypatch: pytest.MonkeyPatch,
    level: str,
    requires_approval: bool,
    classification: bool,
    routing: bool,
) -> None:
    _switches(monkeypatch, classification=classification, routing=routing)
    step = _descriptor(level, requires_approval=requires_approval)

    await apply_step_policy(tenant_id=_TENANT, step=step)

    # The classification makes a destructive step wait, as it does without this change.
    expected_approval = requires_approval or (classification and level == "destructive")
    assert (step.safety_level, step.requires_approval) == (level, expected_approval)
    for caller in ("person", "agent", "service"):
        verdict, _ = await policy_gate(
            operator=_operator(caller),
            descriptor=step,
            target=None,
            connector_id=_CONNECTOR,
            decide_only=True,
        )
        expected, _ = await policy_gate(
            operator=_operator(caller),
            descriptor=_descriptor(level, requires_approval=requires_approval),
            target=None,
            connector_id=_CONNECTOR,
            decide_only=True,
        )
        assert verdict is expected


@pytest.mark.parametrize("level", _LEVELS)
@pytest.mark.asyncio
async def test_an_override_that_turns_approval_off_keeps_the_step_flag(
    embedding: AsyncMock, level: str
) -> None:
    await _register(embedding)
    await _override("install", requires_approval=False)
    step = _descriptor(level, requires_approval=True)

    await apply_step_policy(tenant_id=_TENANT, step=step)

    assert (step.safety_level, step.requires_approval) == (level, True)
