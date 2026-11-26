# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved run checks the caller's rights at the time it runs.

Each test goes through the whole path: the real ``dispatch`` makes the
call wait for approval, a person approves it, and then the call runs: on
the REST approve route, in the agent's own wait, and through the REST
re-run route after a failed first run. The operation is a stand-in that
counts its runs. Each test runs with ``AGENT_GRANT_REQUIRED`` ``off`` and
``on``.

* A new call of the caller that would be refused now refuses the approved
  run, and nothing runs: a ``deny`` grant, a grant taken away (``on``), a
  role lowered to ``read_only``.
* A new call that would wait for approval runs on the approval, unless the
  risk level went up past what the person who approved may approve.
* A refused re-run leaves the request as it was, so it may run later when
  a new call would pass too.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import delete, select, update

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.main import app
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    ApprovalResultState,
    approval_result_state,
    approve_request,
)
from meho_backplane.operations.approval_rerun import RERUN_AUDIT_PATH
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests._people_behind_helpers import give_known_person
from tests.test_agent_grant_required import (
    _AGENT,
    _TENANT,
    _grant,
    _operator,
    _reset_state,
    _seed_vault_target,
    _switch,
    stub_embedding_service,
)
from tests.test_operations_dispatcher import _NoOpVaultConnector

__all__ = ["_reset_state", "stub_embedding_service"]

_OP = "vault.kv.rights_checked"
_CONNECTOR = "vault-1.x"
_PARAMS: dict[str, Any] = {"path": "app/config"}
_REVIEWER_SUB = "reviewer-1"
_ADMIN_SUB = "admin-1"
_ROLES = {_REVIEWER_SUB: TenantRole.OPERATOR, _ADMIN_SUB: TenantRole.TENANT_ADMIN}

#: Every run of the stand-in operation; the first one fails when asked to.
_RUNS: list[dict[str, Any]] = []
_FAIL_FIRST: list[bool] = []


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("AGENT_GRANT_REQUIRED", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("CHART_VERSION", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture(params=["off", "on"])
def switch(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """The grant switch; every test runs with ``off`` and ``on``."""
    mode = str(request.param)
    _switch(monkeypatch, mode)
    return mode


async def _handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del operator, target
    _RUNS.append(params)
    if _FAIL_FIRST and len(_RUNS) == 1:
        raise RuntimeError("the first run fails on purpose")
    return {"done": True}


@pytest.fixture
async def op(stub_embedding_service: AsyncMock) -> AsyncIterator[None]:
    """A ``caution`` operation that waits for a person for every caller."""
    _RUNS.clear()
    _FAIL_FIRST.clear()
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_OP,
        handler=_handler,
        summary="Stand-in op.",
        description="Counts its runs.",
        parameter_schema={"type": "object"},
        safety_level="caution",
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )
    yield
    _RUNS.clear()
    _FAIL_FIRST.clear()


def _person(sub: str, role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
    )


def _service() -> Operator:
    return Operator(
        sub="service-1",
        name="Service",
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
        client_id="svc-client",
    )


def _requester(kind: str) -> Operator:
    if kind == "agent":
        return _operator()
    if kind == "service":
        return _service()
    return _person("person-1")


async def _park(requester: Operator) -> uuid.UUID:
    """The real ``dispatch`` makes the call wait. Returns the request id."""
    target = await _seed_vault_target()
    await give_known_person(requester)
    parked = await dispatch(
        operator=requester, connector_id=_CONNECTOR, op_id=_OP, target=target, params=_PARAMS
    )
    assert parked.status == "awaiting_approval", parked
    return uuid.UUID(parked.extras["approval_request_id"])


async def _approve(request_id: uuid.UUID, approver: Operator) -> None:
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=_PARAMS)
        await session.commit()


def _token(
    key: Any,
    sub: str,
    *,
    role: TenantRole = TenantRole.OPERATOR,
    kind: str | None = None,
    client_id: str | None = None,
    fresh: bool = False,
) -> dict[str, str]:
    token = mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=str(_TENANT),
        principal_kind=kind,
        extra_claims={"client_id": client_id} if client_id else None,
        fresh_sign_in=fresh,
    )
    return {"Authorization": f"Bearer {token}"}


def _requester_token(key: Any, kind: str, role: TenantRole = TenantRole.OPERATOR) -> dict[str, str]:
    if kind == "agent":
        return _token(key, _operator().sub, role=role, kind="agent", client_id=_AGENT)
    if kind == "service":
        return _token(key, "service-1", role=role, kind="service", client_id="svc-client")
    return _token(key, "person-1", role=role)


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-approved-run-rights")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


async def _approve_on_route(
    client: tuple[httpx.AsyncClient, Any], request_id: uuid.UUID, approver: str = _REVIEWER_SUB
) -> httpx.Response:
    ac, key = client
    return await ac.post(
        f"/api/v1/approvals/{request_id}/approve",
        json={"params": _PARAMS},
        headers=_token(key, approver, role=_ROLES[approver], fresh=True),
    )


async def _park_and_fail_first_run(
    client: tuple[httpx.AsyncClient, Any], requester: Operator, approver: str = _REVIEWER_SUB
) -> uuid.UUID:
    """Park, approve on the REST route, and let the first run fail."""
    _FAIL_FIRST.append(True)
    request_id = await _park(requester)
    response = await _approve_on_route(client, request_id, approver)
    assert response.status_code == 200, response.text
    assert len(_RUNS) == 1
    assert approval_result_state(await _request(request_id)) is ApprovalResultState.FAILED
    return request_id


async def _rerun(
    client: tuple[httpx.AsyncClient, Any], request_id: uuid.UUID, headers: dict[str, str]
) -> httpx.Response:
    ac, _ = client
    return await ac.post(f"/api/v1/approvals/{request_id}/rerun", json={}, headers=headers)


async def _request(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        return request


async def _rerun_rows(request_id: uuid.UUID) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == RERUN_AUDIT_PATH))
        found = rows.scalars().all()
    return [r for r in found if r.payload.get("approval_request_id") == str(request_id)]


async def _set_grant(verdict: str) -> None:
    """An admin changes the agent's grant for the operation."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(AgentPermission).where(AgentPermission.op_pattern == _OP).values(verdict=verdict)
        )
        await session.commit()


async def _revoke_grant() -> None:
    """An admin takes the agent's grant for the operation away."""
    async with get_sessionmaker()() as session:
        await session.execute(delete(AgentPermission).where(AgentPermission.op_pattern == _OP))
        await session.commit()


async def _raise_risk_level(level: str) -> None:
    """The operation's risk level goes up after the approval."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == _OP)
            .values(safety_level=level)
        )
        await session.commit()
    reset_dispatcher_caches()


async def _assert_refused_and_kept(
    response: httpx.Response, request_id: uuid.UUID, *, status: int, detail: str, refusal: str
) -> None:
    """Refused, nothing ran again, one refused row, and the request is unchanged."""
    assert response.status_code == status, response.text
    assert response.json()["detail"].startswith(detail), response.text
    assert len(_RUNS) == 1
    (row,) = await _rerun_rows(request_id)
    assert row.payload["result_status"] == "refused"
    assert row.payload["refusal"] == refusal
    assert row.payload["reason"]
    request = await _request(request_id)
    assert request.rerun_count == 0
    assert approval_result_state(request) is ApprovalResultState.FAILED


# --- The re-run --------------------------------------------------------------------


@pytest.mark.usefixtures("op")
async def test_rerun_with_nothing_changed_runs(
    switch: str, client: tuple[httpx.AsyncClient, Any]
) -> None:
    await _grant(_OP, "needs-approval")
    request_id = await _park_and_fail_first_run(client, _operator())

    response = await _rerun(client, request_id, _requester_token(client[1], "agent"))

    assert response.status_code == 200, response.text
    assert response.json()["dispatch_status"] == "ok"
    assert len(_RUNS) == 2


@pytest.mark.usefixtures("op")
async def test_rerun_after_a_deny_grant_is_refused(
    switch: str, client: tuple[httpx.AsyncClient, Any]
) -> None:
    await _grant(_OP, "needs-approval")
    request_id = await _park_and_fail_first_run(client, _operator())
    await _set_grant("deny")

    response = await _rerun(client, request_id, _requester_token(client[1], "agent"))

    await _assert_refused_and_kept(
        response,
        request_id,
        status=403,
        detail="approval_rerun_refused",
        refusal="rights_refused",
    )
    # The refusal keeps the approval open: with the grant back, the re-run runs.
    await _set_grant("needs-approval")
    response = await _rerun(client, request_id, _requester_token(client[1], "agent"))
    assert response.status_code == 200, response.text
    assert len(_RUNS) == 2


@pytest.mark.usefixtures("op")
async def test_rerun_after_the_grant_is_taken_away(
    switch: str, client: tuple[httpx.AsyncClient, Any]
) -> None:
    """``on``: a new call has no grant, so the re-run is refused.

    ``off``: a new call would wait for a person, and the approval covers it.
    """
    await _grant(_OP, "needs-approval")
    request_id = await _park_and_fail_first_run(client, _operator())
    await _revoke_grant()

    response = await _rerun(client, request_id, _requester_token(client[1], "agent"))

    if switch == "on":
        await _assert_refused_and_kept(
            response,
            request_id,
            status=403,
            detail="approval_rerun_refused",
            refusal="rights_refused",
        )
        assert "agent_no_grant" in response.json()["detail"]
    else:
        assert response.status_code == 200, response.text
        assert len(_RUNS) == 2


@pytest.mark.parametrize("kind", ["person", "service"])
@pytest.mark.usefixtures("op")
async def test_rerun_with_a_read_only_token_is_refused(
    switch: str, client: tuple[httpx.AsyncClient, Any], kind: str
) -> None:
    """The same answer as a new call: 403 ``insufficient_role``."""
    request_id = await _park_and_fail_first_run(client, _requester(kind), approver=_ADMIN_SUB)
    headers = _requester_token(client[1], kind, role=TenantRole.READ_ONLY)

    response = await _rerun(client, request_id, headers)
    call = await client[0].post(
        "/api/v1/operations/call",
        json={"connector_id": _CONNECTOR, "op_id": _OP, "params": _PARAMS},
        headers=headers,
    )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}
    assert call.status_code == 403
    assert call.json() == response.json()
    assert len(_RUNS) == 1
    assert (await _request(request_id)).rerun_count == 0


@pytest.mark.usefixtures("op")
async def test_rerun_after_the_risk_level_went_up_needs_a_new_approval(
    switch: str, client: tuple[httpx.AsyncClient, Any]
) -> None:
    """A person with the operator role approved a ``caution`` call; it is now ``dangerous``."""
    await _grant(_OP, "needs-approval")
    request_id = await _park_and_fail_first_run(client, _operator())
    await _raise_risk_level("dangerous")

    response = await _rerun(client, request_id, _requester_token(client[1], "agent"))

    await _assert_refused_and_kept(
        response,
        request_id,
        status=409,
        detail="approval_rerun_needs_new_approval",
        refusal="risk_raised",
    )
    assert "risk level" in response.json()["detail"]


@pytest.mark.usefixtures("op")
async def test_rerun_after_the_risk_level_went_up_runs_when_the_approver_may_approve_it(
    switch: str, client: tuple[httpx.AsyncClient, Any]
) -> None:
    """An admin approved it, and an admin may approve a ``dangerous`` call."""
    await _grant(_OP, "needs-approval")
    request_id = await _park_and_fail_first_run(client, _operator(), approver=_ADMIN_SUB)
    await _raise_risk_level("dangerous")

    response = await _rerun(client, request_id, _requester_token(client[1], "agent"))

    assert response.status_code == 200, response.text
    assert len(_RUNS) == 2


# --- The first run after the approval -----------------------------------------------


@pytest.mark.parametrize("change", ["deny", "revoke"])
@pytest.mark.usefixtures("op")
async def test_first_run_on_the_approve_route_checks_the_agent_again(
    switch: str, client: tuple[httpx.AsyncClient, Any], change: str
) -> None:
    """The grant changes while the call waits; the person approves it afterwards."""
    await _grant(_OP, "needs-approval")
    request_id = await _park(_operator())
    await (_set_grant("deny") if change == "deny" else _revoke_grant())

    response = await _approve_on_route(client, request_id)

    assert response.status_code == 200, response.text
    if change == "revoke" and switch == "off":
        # A new call would wait for a person, and the approval covers it.
        assert len(_RUNS) == 1
        return
    assert response.json()["dispatch_status"] == "denied", response.text
    assert _RUNS == []
    request = await _request(request_id)
    assert approval_result_state(request) is ApprovalResultState.FAILED


@pytest.mark.parametrize("change", ["deny", "revoke", "none"])
@pytest.mark.usefixtures("op")
async def test_first_run_in_the_agent_wait_checks_the_agent_again(switch: str, change: str) -> None:
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed

    await _grant(_OP, "needs-approval")
    agent = _operator()
    request_id = await _park(agent)
    if change == "deny":
        await _set_grant("deny")
    elif change == "revoke":
        await _revoke_grant()
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

    runs_now = change == "none" or (change == "revoke" and switch == "off")
    if runs_now:
        assert envelope["status"] == "ok", envelope
        assert len(_RUNS) == 1
        return
    assert envelope["status"] == "denied", envelope
    assert _RUNS == []
    assert approval_result_state(await _request(request_id)) is ApprovalResultState.FAILED


@pytest.mark.usefixtures("op")
async def test_first_run_on_the_approve_route_after_the_risk_level_went_up(
    switch: str, client: tuple[httpx.AsyncClient, Any]
) -> None:
    """The card says ``caution``; the operation is ``dangerous`` when a reviewer approves it."""
    await _grant(_OP, "needs-approval")
    request_id = await _park(_operator())
    await _raise_risk_level("dangerous")

    response = await _approve_on_route(client, request_id)

    assert response.status_code == 200, response.text
    assert response.json()["dispatch_status"] == "denied", response.text
    assert "a new approval is needed" in response.json()["dispatch_error"]
    assert _RUNS == []
    assert approval_result_state(await _request(request_id)) is ApprovalResultState.FAILED
