# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved request is checked again when it runs: the table of cases.

Each test goes through the whole path. The real ``dispatch`` makes the call
wait for approval, something changes while it waits, a person approves it,
and then the call runs: on the REST approve route, through
``approve_request`` with ``resume_dispatch_after_approval``, in the agent's
own wait, or through the REST re-run route after a failed first run. The
operation is a stand-in that counts its runs. Each test runs with
``AGENT_GRANT_REQUIRED`` ``off`` and ``on``.

* A risk level that went up stops the run, whoever asked.
* A call of an agent login that MEHO revoked does not run, on every path.
* A re-run of a request made inside a hosted agent run is checked with the
  acting agent too.
* The approved step of a multi-step operation is checked for the agent.
* An agent's card with no readable record of the agent is refused.
* The answer says that the request stays approved.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import structlog
from sqlalchemy import select

from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, ApprovalRequest, AuditLog
from meho_backplane.operations._preview import AGENT_REQUESTER_RECORD_KEY
from meho_backplane.operations.approval_queue import (
    ApprovalResultState,
    approval_result_state,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.role_floors import READER_CHANGE_SENTENCE
from tests.test_agent_grant_required import _AGENT, _TENANT, _grant, _operator
from tests.test_approved_run_rights import (
    _ADMIN_SUB,
    _CONNECTOR,
    _OP,
    _PARAMS,
    _REVIEWER_SUB,
    _RUNS,
    _approve,
    _approve_on_route,
    _park,
    _park_and_fail_first_run,
    _person,
    _raise_risk_level,
    _request,
    _requester,
    _requester_token,
    _required_settings_env,
    _rerun,
    _rerun_rows,
    _reset_state,
    _set_grant,
    _token,
    client,
    op,
    stub_embedding_service,
    switch,
)

__all__ = [
    "_required_settings_env",
    "_reset_state",
    "client",
    "op",
    "stub_embedding_service",
    "switch",
]

_ROLES = {_REVIEWER_SUB: TenantRole.OPERATOR, _ADMIN_SUB: TenantRole.TENANT_ADMIN}
_KINDS = ("agent", "person", "service", "console")


def _kind_requester(kind: str) -> Operator:
    if kind == "console":
        return _person("person-1").model_copy(update={"web_session": True})
    return _requester(kind)


async def _run_first(
    client: tuple[httpx.AsyncClient, Any], request_id: uuid.UUID, path: str, approver: str
) -> tuple[str, str]:
    """Approve and run on *path*; returns the run's status and error text."""
    if path == "approve_rest":
        response = await _approve_on_route(client, request_id, approver)
        assert response.status_code == 200, response.text
        body = response.json()
        return body["dispatch_status"], body.get("dispatch_error") or ""
    reviewer = _person(approver, _ROLES[approver])
    await _approve(request_id, reviewer)
    result = await resume_dispatch_after_approval(
        operator=reviewer, request=await _request(request_id), params=_PARAMS
    )
    return result.status, result.error or ""


async def _denied_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == _OP))
        return [r for r in rows.scalars() if r.payload.get("result_status") == "denied"]


async def _edit_card(request_id: uuid.UUID, **changes: Any) -> None:
    """Change keys of the stored card; a value ``None`` takes the key off."""
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        effect = dict(request.proposed_effect or {})
        for key, value in changes.items():
            if value is None:
                effect.pop(key, None)
            else:
                effect[key] = value
        request.proposed_effect = effect
        await session.commit()


# --- A risk level that went up -------------------------------------------------------


@pytest.mark.parametrize("change", ["dangerous", "destructive", "dangerous_admin_approved"])
@pytest.mark.parametrize("path", ["approve_rest", "between"])
@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.usefixtures("op")
async def test_risk_raise_on_the_first_run(
    switch: str, client: tuple[httpx.AsyncClient, Any], kind: str, path: str, change: str
) -> None:
    """The card says ``caution``; the level goes up while the call waits."""
    await _grant(_OP, "needs-approval")
    request_id = await _park(_kind_requester(kind))
    await _raise_risk_level(change.removesuffix("_admin_approved"))
    approver = _ADMIN_SUB if change == "dangerous_admin_approved" else _REVIEWER_SUB

    status, error = await _run_first(client, request_id, path, approver)

    if change == "dangerous_admin_approved":
        assert status == "ok", error
        assert len(_RUNS) == 1
        return
    assert status == "denied", error
    assert _RUNS == []
    assert approval_result_state(await _request(request_id)) is ApprovalResultState.FAILED
    if change == "dangerous":
        assert "risk level" in error
        (row,) = await _denied_rows()
        assert "risk level" in str(row.payload)


@pytest.mark.parametrize("change", ["none", "dangerous"])
@pytest.mark.usefixtures("op")
async def test_risk_raise_in_the_agent_wait(switch: str, change: str) -> None:
    """The agent's own wait binds the approved request, so the level is compared."""
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed

    await _grant(_OP, "needs-approval")
    agent = _operator()
    request_id = await _park(agent)
    if change == "dangerous":
        await _raise_risk_level("dangerous")
    await _approve(request_id, _person(_REVIEWER_SUB))

    envelope = await _resume_approved_or_already_resumed(
        operator=agent,
        call_arguments={
            "connector_id": _CONNECTOR,
            "op_id": _OP,
            "params": _PARAMS,
            "target": "vault-grant-test",
        },
        awaiting_envelope={"op_id": _OP, "extras": {}},
        approval_request_id=request_id,
    )

    if change == "none":
        assert envelope["status"] == "ok", envelope
        assert len(_RUNS) == 1
        return
    assert envelope["status"] == "denied", envelope
    assert "risk level" in envelope["error"]
    assert _RUNS == []


@pytest.mark.parametrize("path", ["approve_rest", "rerun"])
@pytest.mark.usefixtures("op")
async def test_card_with_no_stamped_level_is_refused_when_destructive_now(
    switch: str, client: tuple[httpx.AsyncClient, Any], path: str
) -> None:
    if path == "rerun":
        request_id = await _park_and_fail_first_run(client, _person("person-1"))
    else:
        request_id = await _park(_person("person-1"))
    await _edit_card(request_id, safety_level=None)
    await _raise_risk_level("destructive")

    if path == "approve_rest":
        # A card with no level counts as ``destructive`` for who may approve it.
        status, _ = await _run_first(client, request_id, path, _ADMIN_SUB)
        assert status == "denied"
        assert _RUNS == []
        return
    response = await _rerun(client, request_id, _requester_token(client[1], "person"))
    assert response.status_code in (403, 409), response.text
    assert len(_RUNS) == 1
    assert (await _request(request_id)).rerun_count == 0


@pytest.mark.usefixtures("op")
async def test_decision_row_key_false_decides_over_a_high_risk_rule(switch: str) -> None:
    """The key on the decision row says ``false``; its rule name is a high-risk rule."""
    request_id = await _park(_person("person-1"))
    await _approve(request_id, _person(_ADMIN_SUB, TenantRole.TENANT_ADMIN))
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == "approval.decision"))
        (row,) = [
            r for r in rows.scalars() if r.payload.get("approval_request_id") == str(request_id)
        ]
        row.payload = {
            **row.payload,
            "approval_rule": "sa_operator_or_admin",
            "approver_may_approve_high_risk": False,
        }
        await session.commit()
    await _raise_risk_level("dangerous")

    result = await resume_dispatch_after_approval(
        operator=_person(_ADMIN_SUB, TenantRole.TENANT_ADMIN),
        request=await _request(request_id),
        params=_PARAMS,
    )

    assert result.status == "denied", result
    assert _RUNS == []


# --- An agent login that MEHO revoked ------------------------------------------------


async def _register_agent_login() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AgentPrincipal(
                tenant_id=_TENANT,
                name="grant-test",
                keycloak_client_id=_AGENT,
                keycloak_internal_id="kc-internal-1",
                owner_sub="owner-1",
                created_by_sub="owner-1",
            )
        )
        await session.commit()


async def _revoke_on_route(client: tuple[httpx.AsyncClient, Any]) -> None:
    """An admin revokes the agent login on the REST route."""
    keycloak = AsyncMock()
    keycloak.__aenter__ = AsyncMock(return_value=keycloak)
    keycloak.__aexit__ = AsyncMock(return_value=None)
    with patch(
        "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
        MagicMock(return_value=keycloak),
    ):
        response = await client[0].delete(
            "/api/v1/agent-principals/grant-test/revoke",
            headers=_token(client[1], _ADMIN_SUB, role=TenantRole.TENANT_ADMIN),
        )
    assert response.status_code == 200, response.text
    assert response.json()["revoked"] is True


@pytest.mark.parametrize("path", ["new_call", "approve_rest", "waiter", "rerun"])
@pytest.mark.usefixtures("op")
async def test_revoked_agent_login_runs_nothing(
    switch: str, client: tuple[httpx.AsyncClient, Any], path: str
) -> None:
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed

    await _register_agent_login()
    await _grant(_OP, "needs-approval")
    agent = _operator()
    if path == "rerun":
        request_id = await _park_and_fail_first_run(client, agent)
    else:
        request_id = await _park(agent)
    await _revoke_on_route(client)
    runs_before = len(_RUNS)

    if path == "new_call":
        response = await client[0].post(
            "/api/v1/operations/call",
            json={"connector_id": _CONNECTOR, "op_id": _OP, "params": _PARAMS},
            headers=_requester_token(client[1], "agent"),
        )
        text = response.text
    elif path == "approve_rest":
        response = await _approve_on_route(client, request_id)
        assert response.json()["dispatch_status"] == "denied", response.text
        text = response.json()["dispatch_error"]
    elif path == "waiter":
        await _approve(request_id, _person(_REVIEWER_SUB))
        envelope = await _resume_approved_or_already_resumed(
            operator=agent,
            call_arguments={
                "connector_id": _CONNECTOR,
                "op_id": _OP,
                "params": _PARAMS,
                "target": "vault-grant-test",
            },
            awaiting_envelope={"op_id": _OP, "extras": {}},
            approval_request_id=request_id,
        )
        assert envelope["status"] == "denied", envelope
        text = envelope["error"]
    else:
        response = await _rerun(client, request_id, _requester_token(client[1], "agent"))
        assert response.status_code == 403, response.text
        assert (await _request(request_id)).rerun_count == 0
        text = response.json()["detail"]

    assert "is revoked" in text, text
    assert len(_RUNS) == runs_before
    if path != "rerun":
        assert any("is revoked" in str(row.payload) for row in await _denied_rows())


# --- A re-run of a request made inside a hosted agent run ----------------------------


@pytest.mark.usefixtures("op")
async def test_rerun_of_a_hosted_agent_request_checks_the_acting_agent(
    switch: str, client: tuple[httpx.AsyncClient, Any]
) -> None:
    """A person started the run; the hosted agent gets a ``deny`` grant after a failed run."""
    await _grant(_OP, "needs-approval")
    with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: _AGENT}):
        request_id = await _park_and_fail_first_run(client, _person("person-1"))
    assert (await _request(request_id)).principal_act == _AGENT
    await _set_grant("deny")

    response = await _rerun(client, request_id, _requester_token(client[1], "person"))

    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith("approval_rerun_refused")
    assert len(_RUNS) == 1
    assert (await _request(request_id)).rerun_count == 0


# --- An agent's card with no readable record of the agent ----------------------------


_UNREADABLE = {"sub": _operator().sub, "tenant_role": "no-such-role", "principal_kind": "agent"}
_OTHER_SUB = {"sub": "someone-else", "tenant_role": "operator", "principal_kind": "agent"}


@pytest.mark.parametrize("record", ["none", "unreadable", "other_sub"])
@pytest.mark.usefixtures("op")
async def test_agent_card_with_no_readable_record_is_refused(
    switch: str, client: tuple[httpx.AsyncClient, Any], record: str
) -> None:
    await _grant(_OP, "needs-approval")
    request_id = await _park(_operator())
    value = {"none": None, "unreadable": _UNREADABLE, "other_sub": _OTHER_SUB}[record]
    preflight = None if value is None else {"requester": value}
    await _edit_card(
        request_id, **{AGENT_REQUESTER_RECORD_KEY: value, "permission_preflight": preflight}
    )
    await _set_grant("deny")

    response = await _approve_on_route(client, request_id)

    assert response.status_code == 200, response.text
    assert response.json()["dispatch_status"] == "denied", response.text
    error = response.json()["dispatch_error"]
    assert error.endswith(
        f"approval request {request_id} did not run: the grants of the agent that asked for "
        f"{_OP!r} cannot be checked, because the approval request has no record of the agent. "
        "The agent must ask again with a new request."
    ), error
    assert "would be refused" not in error
    assert "Run it again" not in error
    assert _RUNS == []


@pytest.mark.usefixtures("op")
async def test_agent_card_keeps_the_record_internal(switch: str) -> None:
    """The card a caller reads has no new key; the record stays internal."""
    from meho_backplane.operations._preview import without_person_only_effect_keys

    await _grant(_OP, "needs-approval")
    request_id = await _park(_operator())
    effect = (await _request(request_id)).proposed_effect

    assert effect[AGENT_REQUESTER_RECORD_KEY]["sub"] == _operator().sub
    assert AGENT_REQUESTER_RECORD_KEY not in without_person_only_effect_keys(effect)
    shown_requester = "requester" in (effect.get("permission_preflight") or {})
    assert shown_requester is (switch != "off")


# --- The answer to a refused approved run --------------------------------------------


@pytest.mark.usefixtures("op")
async def test_refused_rerun_says_the_request_stays_approved(
    switch: str, client: tuple[httpx.AsyncClient, Any]
) -> None:
    await _grant(_OP, "needs-approval")
    request_id = await _park_and_fail_first_run(client, _operator())
    await _set_grant("deny")

    response = await _rerun(client, request_id, _requester_token(client[1], "agent"))

    detail = response.json()["detail"]
    assert detail.startswith(f"approval_rerun_refused: approval request {request_id} did not run")
    assert "a new call of yours would be refused now (" in detail
    assert "The request stays approved. Run it again when your rights allow it" in detail
    assert "cannot run again" not in detail


@pytest.mark.usefixtures("op")
async def test_refused_first_run_says_the_request_stays_approved(
    switch: str, client: tuple[httpx.AsyncClient, Any]
) -> None:
    await _grant(_OP, "needs-approval")
    request_id = await _park(_operator())
    await _set_grant("deny")

    response = await _approve_on_route(client, request_id)

    error = response.json()["dispatch_error"]
    assert f"approval request {request_id} did not run" in error
    assert "a new call of the login that asked would be refused now (" in error
    assert "The request stays approved. Run it again when the rights allow it" in error
    assert "cannot run again" not in error


# --- A re-run refused for the caller's role ------------------------------------------


@pytest.mark.parametrize("kind", ["person", "service"])
@pytest.mark.usefixtures("op")
async def test_rerun_with_a_read_only_token_writes_its_audit_row(
    switch: str, client: tuple[httpx.AsyncClient, Any], kind: str
) -> None:
    request_id = await _park_and_fail_first_run(client, _requester(kind), approver=_ADMIN_SUB)

    response = await _rerun(
        client, request_id, _requester_token(client[1], kind, role=TenantRole.READ_ONLY)
    )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role", "message": READER_CHANGE_SENTENCE}
    (row,) = await _rerun_rows(request_id)
    assert row.payload["result_status"] == "refused"
    assert row.payload["refusal"] == "insufficient_role"
    assert row.payload["reason"] == READER_CHANGE_SENTENCE
    assert len(_RUNS) == 1
    assert (await _request(request_id)).rerun_count == 0
