# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved step of a multi-step operation is refused when its risk level went up.

Each test goes through the whole path. The real ``dispatch`` of a
multi-step operation makes its step wait for approval. While it waits, an
override row raises the step's risk level. A person approves the step, and
then MEHO runs the operation again: on the REST approve route, through
``approve_request`` with ``resume_dispatch_after_approval``, or in the
agent's own wait. Each test runs for an agent's request and a person's
request, with ``AGENT_GRANT_REQUIRED`` ``off`` and ``on``.

* A raise to ``dangerous`` that an ``operator``-role person approved runs
  nothing of the step.
* A raise to ``destructive`` runs nothing of the step.
* A raise to ``dangerous`` that a MEHO admin approved runs the step.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationPolicyOverride
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    ApprovalResultState,
    approval_result_state,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.operations.governed_subops import register_governed_subops
from tests._composite_wait_audit_helpers import register_composite
from tests._people_behind_helpers import give_known_person
from tests.test_agent_grant_required import _grant, _operator, _seed_vault_target
from tests.test_approved_run_rights import (
    _ADMIN_SUB,
    _CONNECTOR,
    _REVIEWER_SUB,
    _approve,
    _approve_on_route,
    _person,
    _request,
    _required_settings_env,
    _reset_state,
    client,
    stub_embedding_service,
    switch,
)
from tests.test_operations_dispatcher import _NoOpVaultConnector

__all__ = [
    "_required_settings_env",
    "_reset_state",
    "client",
    "stub_embedding_service",
    "switch",
]

_PARENT = "vault.kv.step_risk_rises"
_STEP = "PUT:/v1/secret/data/{path}"
_STEP_PARAMS: dict[str, Any] = {"path": "app/config"}
_ROLES = {_REVIEWER_SUB: TenantRole.OPERATOR, _ADMIN_SUB: TenantRole.TENANT_ADMIN}

#: Every run of the step after its gate let it go ahead.
_STEP_RUNS: list[dict[str, Any]] = []


async def _parent_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """The parent's handler: its one step is ``safe`` and waits for a person."""
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR,
        op_id=_STEP,
        safety_level="safe",
        requires_approval=True,
        target=target,
        params=_STEP_PARAMS,
    )
    if gated is not None:
        return gated  # type: ignore[return-value]
    _STEP_RUNS.append(params)
    return {"done": True}


@pytest.fixture
async def parent(stub_embedding_service: Any) -> None:
    del stub_embedding_service
    _STEP_RUNS.clear()
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    async with get_sessionmaker()() as session:
        await register_composite(session, _PARENT, "_parent_handler", module=__name__)
    register_governed_subops(composite_op_id=_PARENT, connector_id=_CONNECTOR, sub_op_ids=(_STEP,))
    await _grant(_PARENT, "auto-execute")
    await _grant(_STEP, "needs-approval")


def _requester(kind: str) -> Operator:
    return _operator() if kind == "agent" else _person("person-1")


async def _park_step(requester: Operator) -> uuid.UUID:
    """The real ``dispatch`` of the parent makes the step wait; returns the request id."""
    target = await _seed_vault_target()
    await give_known_person(requester)
    parked = await dispatch(
        operator=requester, connector_id=_CONNECTOR, op_id=_PARENT, target=target, params={}
    )
    assert parked.status == "awaiting_approval", parked
    request_id = uuid.UUID(parked.extras["approval_request_id"])
    request = await _request(request_id)
    assert request.op_id == _STEP
    assert request.resume_parent is not None
    assert request.proposed_effect["safety_level"] == "safe"
    return request_id


async def _raise_step_level(level: str) -> None:
    """An admin adds an override row that raises the step's risk level."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationPolicyOverride(
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id=_STEP,
                safety_level=level,
                set_by_sub="admin-1",
            )
        )
    reset_dispatcher_caches()


async def _run(
    client: tuple[httpx.AsyncClient, Any],
    request_id: uuid.UUID,
    requester: Operator,
    path: str,
    approver: str,
) -> tuple[str, str]:
    """Approve the step and run the parent again on *path*; returns status and error."""
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed

    if path == "approve_rest":
        response = await _approve_on_route(client, request_id, approver)
        assert response.status_code == 200, response.text
        body = response.json()
        return body["dispatch_status"], body.get("dispatch_error") or ""
    reviewer = _person(approver, _ROLES[approver])
    await _approve(request_id, reviewer)
    if path == "between":
        result = await resume_dispatch_after_approval(
            operator=reviewer, request=await _request(request_id), params=None
        )
        return result.status, result.error or ""
    envelope = await _resume_approved_or_already_resumed(
        operator=requester,
        call_arguments={
            "connector_id": _CONNECTOR,
            "op_id": _PARENT,
            "params": {},
            "target": "vault-grant-test",
        },
        awaiting_envelope={"op_id": _PARENT, "extras": {}},
        approval_request_id=request_id,
    )
    return envelope["status"], envelope.get("error") or ""


@pytest.mark.parametrize("change", ["dangerous", "destructive", "dangerous_admin_approved"])
@pytest.mark.parametrize("path", ["approve_rest", "between", "waiter"])
@pytest.mark.parametrize("kind", ["agent", "person"])
@pytest.mark.usefixtures("parent")
async def test_step_whose_risk_level_went_up(
    switch: str, client: tuple[httpx.AsyncClient, Any], kind: str, path: str, change: str
) -> None:
    del switch
    requester = _requester(kind)
    request_id = await _park_step(requester)
    await _raise_step_level(change.removesuffix("_admin_approved"))
    approver = _ADMIN_SUB if change == "dangerous_admin_approved" else _REVIEWER_SUB

    status, error = await _run(client, request_id, requester, path, approver)

    if change == "dangerous_admin_approved":
        assert status == "ok", error
        assert len(_STEP_RUNS) == 1
        return
    assert status == "denied", error
    assert _STEP_RUNS == []
    assert f"the risk level of {_STEP!r} is now {change!r}" in error
    assert "the approved step did not run" in error
    assert approval_result_state(await _request(request_id)) is ApprovalResultState.FAILED


@pytest.mark.usefixtures("parent")
async def test_refused_step_says_to_start_a_new_run(
    switch: str, client: tuple[httpx.AsyncClient, Any]
) -> None:
    """The full answer of a refused step names a next step that works."""
    del switch
    request_id = await _park_step(_person("person-1"))
    await _raise_step_level("destructive")

    status, error = await _run(client, request_id, _person("person-1"), "approve_rest", _ADMIN_SUB)

    assert status == "denied", error
    assert error.endswith(
        f"policy_denied: the risk level of {_STEP!r} is now 'destructive', higher than the "
        "approved one (the approved step did not run. A step of a multi-step operation "
        f"cannot run again on its own: start a new run of {_PARENT!r}.)"
    ), error
    assert "Run it again" not in error
    assert "run the request again" not in error
