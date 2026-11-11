# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The up-front step check of an agent's multi-step call (#6285).

With ``AGENT_GRANT_REQUIRED=on`` the dispatcher checks every listed write
step of a multi-step operation before the call waits for approval or runs
a step. #6189 refuses a step that no grant covers. This module covers the
rest:

* A write step that meets a ``deny`` grant refuses the call up front, with
  the code ``agent_grant_denied``. The preview shows it, also for a call
  that waits for approval.
* With ``observe`` or ``off`` such a call runs or waits as before, and the
  preview of a multi-step call is unchanged with the switch off.
* People and service accounts on the multi-step path are not touched by
  the switch.
* The CI check: every multi-step operation that changes something declares
  its steps, or declares that it has none.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.broadcast.events import resolve_operation_op_class
from meho_backplane.broadcast.history import WRITE_OP_CLASSES
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.deny_reasons import AGENT_GRANT_DENIED
from meho_backplane.operations.governed_subops import (
    composites_without_declared_steps,
    register_governed_subops,
)
from meho_backplane.operations.typed_register import (
    register_composite_operation,
    run_typed_op_registrars,
)
from tests.test_agent_grant_required import (
    _MULTI_OP,
    _MULTI_RUNS,
    _WRITE_STEP,
    _approval_requests,
    _audit_rows,
    _dispatch,
    _grant,
    _operator,
    _register_multi_step_op,
    _required_settings_env,
    _reset_state,
    _switch,
    stub_embedding_service,
)
from tests.test_operations_dispatcher import _FakeTarget

# The fixtures of the #6189 module, so this module sets up the same way.
__all__ = ["_required_settings_env", "_reset_state", "stub_embedding_service"]


async def _preview(operator: Operator) -> dict[str, Any]:
    return await preview_dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id=_MULTI_OP,
        target=_FakeTarget(product="vault"),
        params={},
    )


# --- A deny grant on a write step -------------------------------------------------


@pytest.mark.parametrize("op_verdict", ["auto-execute", "needs-approval"])
async def test_denied_write_step_is_refused_up_front(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, op_verdict: str
) -> None:
    """The op has a grant, a deny grant meets its write step: refused before anything."""
    _switch(monkeypatch, "on")
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, op_verdict)
    await _grant(_WRITE_STEP, "deny")

    result = await _dispatch(_operator(), _MULTI_OP)

    assert result.status == "denied", result
    assert result.extras["reason_code"] == AGENT_GRANT_DENIED
    assert _WRITE_STEP in result.error
    assert "grant denies" in result.error
    assert _MULTI_RUNS == []
    assert await _approval_requests() == []
    (row,) = await _audit_rows(_MULTI_OP)
    assert row.policy_decision == "deny"
    assert row.payload["error"]["reason_code"] == AGENT_GRANT_DENIED
    # The preview checks the same step list and gives the same answer, also
    # when the operation itself would wait for approval.
    preview = await _preview(_operator())
    assert preview["policy_verdict"] == "deny", preview
    assert result.error == f"denied: {preview['policy_reason']}"


async def test_step_with_no_grant_is_still_refused_first_as_no_grant(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    """A deny grant on another op does not change the no-grant refusal."""
    _switch(monkeypatch, "on")
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, "auto-execute")
    await _grant("POST:/v1/other/{path}", "deny")

    result = await _dispatch(_operator(), _MULTI_OP)

    assert result.status == "denied", result
    assert result.extras["reason_code"] == "agent_no_grant"
    assert _MULTI_RUNS == []


@pytest.mark.parametrize("mode", ["observe", "off"])
@pytest.mark.parametrize(
    ("op_verdict", "status"),
    [
        ("auto-execute", "ok"),
        ("needs-approval", "awaiting_approval"),
    ],
)
async def test_denied_write_step_is_left_to_the_step_gate_when_not_on(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    mode: str,
    op_verdict: str,
    status: str,
) -> None:
    """With ``observe`` or ``off`` the call runs or waits as before."""
    _switch(monkeypatch, mode)
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, op_verdict)
    await _grant(_WRITE_STEP, "deny")

    result = await _dispatch(_operator(), _MULTI_OP)

    assert result.status == status, result


# --- With the switch off, the preview is unchanged ---------------------------------


@pytest.mark.parametrize(
    ("op_verdict", "step_verdict", "verdict", "reason_part"),
    [
        ("auto-execute", None, "needs-approval", "these steps have no grant"),
        ("auto-execute", "deny", "deny", f"the step {_WRITE_STEP} is denied"),
        ("needs-approval", "deny", "needs-approval", None),
        ("needs-approval", None, "needs-approval", None),
    ],
)
async def test_preview_of_a_multi_step_call_is_unchanged_with_the_switch_off(
    stub_embedding_service: AsyncMock,
    op_verdict: str,
    step_verdict: str | None,
    verdict: str,
    reason_part: str | None,
) -> None:
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, op_verdict)
    if step_verdict is not None:
        await _grant(_WRITE_STEP, step_verdict)

    preview = await _preview(_operator())

    assert preview["policy_verdict"] == verdict, preview
    reason = preview["policy_reason"] or ""
    assert "agent_no_grant" not in reason
    assert "grant denies" not in reason
    if reason_part is not None:
        assert reason_part in reason


# --- People and service accounts ----------------------------------------------------


@pytest.mark.parametrize("kind", [PrincipalKind.USER, PrincipalKind.SERVICE])
async def test_people_and_services_on_the_multi_step_path_behave_as_before(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, kind: PrincipalKind
) -> None:
    """Switch on or off: the same answer, the same preview, and the call runs."""
    await _register_multi_step_op(stub_embedding_service)
    # Agent grants do not apply to them, also not a deny on the write step.
    await _grant(_WRITE_STEP, "deny")
    answers: dict[str, tuple[str, str, str | None]] = {}
    for mode in ("off", "on"):
        _switch(monkeypatch, mode)
        result = await _dispatch(_operator(kind), _MULTI_OP)
        preview = await _preview(_operator(kind))
        answers[mode] = (result.status, preview["policy_verdict"], preview["policy_reason"])

    assert answers["on"] == answers["off"]
    assert answers["on"][0] == "ok"
    assert len(_MULTI_RUNS) == 2


# --- The CI check: every multi-step operation that changes something declares steps --


async def _noop_composite_handler(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: Any
) -> dict[str, Any]:
    raise AssertionError("never run")


async def _write_composites_without_steps() -> list[str]:
    """Every registered multi-step operation that changes something and declares no steps."""
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.tenant_id.is_(None),
                    EndpointDescriptor.source_kind == "composite",
                )
            )
        ).scalars()
        writes = [
            row.op_id
            for row in rows
            if resolve_operation_op_class(
                row.op_id, source_kind=row.source_kind, safety_level=row.safety_level
            )
            in WRITE_OP_CLASSES
        ]
    assert writes, "no multi-step operation that changes something was registered"
    return composites_without_declared_steps(writes)


async def test_every_multi_step_operation_that_changes_something_declares_its_steps(
    stub_embedding_service: AsyncMock,
) -> None:
    """The real list: every registrar MEHO runs at start-up."""
    await run_typed_op_registrars(embedding_service=stub_embedding_service)

    missing = await _write_composites_without_steps()

    assert not missing, (
        "these multi-step operations change something but declare no steps. Call "
        "register_governed_subops with the steps, or declare_no_governed_subops when "
        f"no step is checked on its own: {missing}"
    )


@pytest.mark.parametrize("safety_level", ["caution", "dangerous", "destructive"])
async def test_the_check_names_a_multi_step_operation_without_steps(
    stub_embedding_service: AsyncMock, safety_level: Any
) -> None:
    await run_typed_op_registrars(embedding_service=stub_embedding_service)
    await register_composite_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.composite.change",
        handler=_noop_composite_handler,
        summary="A test change.",
        description="Changes something in steps.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level=safety_level,
        embedding_service=stub_embedding_service,
    )

    assert await _write_composites_without_steps() == ["vault.composite.change"]

    register_governed_subops(
        composite_op_id="vault.composite.change",
        connector_id="vault-1.x",
        sub_op_ids=(_WRITE_STEP,),
    )
    assert await _write_composites_without_steps() == []
