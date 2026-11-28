# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved request is checked again when it runs: hosted runs and other callers.

Each test goes through the whole path, with ``AGENT_GRANT_REQUIRED`` ``off``
and ``on``. The operation is a stand-in that counts its runs.

* A registered agent is the acting agent of a hosted agent run that a
  person started. The call waits, and an admin revokes the agent login on
  the REST route. Then a new call in the hosted run, the approved request
  (on the REST approve route and in the wait) and the person's re-run run
  nothing, and the answer says that the agent login is revoked.
* A re-run asked by a caller who did not make the request is refused and
  writes one ``approval.rerun`` row that names only the caller and the
  request id.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
import structlog
from sqlalchemy import select

from meho_backplane.audit_query.approval_payload import approval_payload_view
from meho_backplane.auth.delegation import ACTOR_SUB_KEY, delegated_operator
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Target
from meho_backplane.operations import dispatch
from meho_backplane.operations.approval_queue import (
    ApprovalResultState,
    approval_result_state,
)
from meho_backplane.operations.role_floors import READER_CHANGE_SENTENCE
from tests.test_agent_grant_required import _AGENT, _grant
from tests.test_approved_run_rights import (
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
    _request,
    _requester_token,
    _required_settings_env,
    _rerun,
    _rerun_rows,
    _reset_state,
    _token,
    client,
    op,
    stub_embedding_service,
    switch,
)
from tests.test_approved_run_rights_cases import _register_agent_login, _revoke_on_route

__all__ = [
    "_required_settings_env",
    "_reset_state",
    "client",
    "op",
    "stub_embedding_service",
    "switch",
]


@contextmanager
def _hosted_run() -> Iterator[None]:
    """The registered agent acts in a hosted run, as the agent runtime binds it."""
    with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: _AGENT}):
        yield


def _starter() -> Operator:
    """The person who started the hosted run, as the run's tools see them."""
    return delegated_operator(_person("person-1"))


async def _stored_target() -> Target:
    """The target the waiting call named."""
    async with get_sessionmaker()() as session:
        target = (
            await session.execute(select(Target).where(Target.name == "vault-grant-test"))
        ).scalar_one()
        session.expunge(target)
        return target


async def _denied_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == _OP))
        return [r for r in rows.scalars() if r.payload.get("result_status") == "denied"]


# --- A revoked agent login that acts in a hosted agent run ---------------------------


@pytest.mark.parametrize("path", ["new_call", "approve_rest", "waiter"])
@pytest.mark.usefixtures("op")
async def test_revoked_acting_agent_runs_nothing(
    switch: str, client: tuple[httpx.AsyncClient, Any], path: str
) -> None:
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed

    del switch
    await _register_agent_login()
    await _grant(_OP, "needs-approval")
    with _hosted_run():
        request_id = await _park(_starter())
    assert (await _request(request_id)).principal_act == _AGENT
    await _revoke_on_route(client)

    if path == "new_call":
        with _hosted_run():
            result = await dispatch(
                operator=_starter(),
                connector_id=_CONNECTOR,
                op_id=_OP,
                target=await _stored_target(),
                params=_PARAMS,
            )
        status, text = result.status, result.error or ""
    elif path == "approve_rest":
        response = await _approve_on_route(client, request_id)
        assert response.status_code == 200, response.text
        status, text = response.json()["dispatch_status"], response.json()["dispatch_error"]
    else:
        await _approve(request_id, _person(_REVIEWER_SUB))
        with _hosted_run():
            envelope = await _resume_approved_or_already_resumed(
                operator=_starter(),
                call_arguments={
                    "connector_id": _CONNECTOR,
                    "op_id": _OP,
                    "params": _PARAMS,
                    "target": "vault-grant-test",
                },
                awaiting_envelope={"op_id": _OP, "extras": {}},
                approval_request_id=request_id,
            )
        status, text = envelope["status"], envelope.get("error") or ""

    assert status == "denied", text
    assert f"the agent login {_AGENT!r} is revoked in MEHO" in text, text
    assert _RUNS == []
    assert any("is revoked" in str(row.payload) for row in await _denied_rows())
    if path != "new_call":
        assert approval_result_state(await _request(request_id)) is ApprovalResultState.FAILED


@pytest.mark.usefixtures("op")
async def test_rerun_after_the_acting_agent_was_revoked_runs_nothing(
    switch: str, client: tuple[httpx.AsyncClient, Any]
) -> None:
    del switch
    await _register_agent_login()
    await _grant(_OP, "needs-approval")
    with _hosted_run():
        request_id = await _park_and_fail_first_run(client, _starter())
    await _revoke_on_route(client)

    response = await _rerun(client, request_id, _requester_token(client[1], "person"))

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail.startswith("approval_rerun_refused"), detail
    assert f"the agent login {_AGENT!r} is revoked in MEHO" in detail
    assert len(_RUNS) == 1
    request = await _request(request_id)
    assert request.rerun_count == 0
    assert approval_result_state(request) is ApprovalResultState.FAILED
    (row,) = await _rerun_rows(request_id)
    assert row.payload["result_status"] == "refused"
    assert row.payload["refusal"] == "rights_refused"
    assert "is revoked" in row.payload["reason"]


# --- A re-run asked by a caller who did not make the request ------------------------


@pytest.mark.parametrize(
    ("role", "detail"),
    [(TenantRole.READ_ONLY, "insufficient_role"), (TenantRole.OPERATOR, "not_request_owner")],
)
@pytest.mark.usefixtures("op")
async def test_rerun_by_another_caller_writes_its_audit_row(
    switch: str, client: tuple[httpx.AsyncClient, Any], role: TenantRole, detail: str
) -> None:
    del switch
    request_id = await _park_and_fail_first_run(client, _person("person-1"))

    response = await _rerun(client, request_id, _token(client[1], "person-2", role=role))

    assert response.status_code == 403, response.text
    expected = {"detail": detail}
    if role is TenantRole.READ_ONLY:
        expected["message"] = READER_CHANGE_SENTENCE
    assert response.json() == expected
    (row,) = await _rerun_rows(request_id)
    assert row.operator_sub == "person-2"
    assert row.status_code == 403
    assert row.payload == {
        "approval_request_id": str(request_id),
        "principal_sub": "person-2",
        "requester_sub": None,
        "result_status": "refused",
        "refusal": detail,
    }
    assert len(_RUNS) == 1
    request = await _request(request_id)
    assert request.rerun_count == 0
    assert approval_result_state(request) is ApprovalResultState.FAILED

    # Every audit view shows the caller only as the person who acted.
    shown = await client[0].get(
        f"/api/v1/audit/show/{row.id}",
        headers=_token(client[1], "auditor", role=TenantRole.TENANT_ADMIN),
    )
    assert shown.status_code == 200, shown.text
    views = [
        shown.json()["payload"],
        # The audit query and the console audit page read the row this way.
        approval_payload_view(
            method=row.method, operator_sub=row.operator_sub, payload=row.payload
        ),
    ]
    for view in views:
        assert view["principal_sub"] == "person-2"
        assert view["requester_sub"] is None
        assert "person-1" not in str(view)
