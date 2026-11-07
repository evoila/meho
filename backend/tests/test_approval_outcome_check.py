# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approved runs whose outcome MEHO does not know, for operators to check (#5026).

The rules:

* An approved request is listed when its stored result is ``uncertain``, or
  when its run started more than ``APPROVAL_OUTCOME_UNKNOWN_AFTER_MINUTES``
  ago (default 15) and stored no result. A run that started less than that
  ago is not listed. Neither is a run with a known result, a request that
  never ran, a request that is not approved, a checked request, or a
  request of another tenant.
* REST and MCP show the same list as the service.
* "Mark as checked" writes one audit row, keeps the stored result and runs
  nothing. Only a person who may decide approvals can do it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    AuditEventClass,
    AuditLog,
    Tenant,
)
from meho_backplane.main import app
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools.approvals import _list_handler
from meho_backplane.operations.approval_outcome_check import (
    STARTED_NO_RESULT_ADVICE,
    OutcomeAlreadyCheckedError,
    OutcomeCheckVerdict,
    OutcomeNotUnknownError,
    OutcomeUnknownReason,
    list_outcome_unknown,
    mark_outcome_checked,
    outcome_unknown,
)
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    NonHumanApprovalError,
    UnauthorizedApprovalError,
)
from meho_backplane.operations.uncertain_outcome import (
    UNCERTAIN_ADVICE,
    result_interrupted_by_restart,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000005026")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-000000005027")
_CHECKER = "op:checker"
_UNCERTAIN = result_interrupted_by_restart("vm.create").model_dump(mode="json")
_FAILED = {"status": "error", "op_id": "vm.create", "extras": {"error_code": "connector_error"}}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("APPROVAL_OUTCOME_UNKNOWN_AFTER_MINUTES", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _ago(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _operator(
    *,
    sub: str = _CHECKER,
    role: TenantRole = TenantRole.OPERATOR,
    kind: PrincipalKind = PrincipalKind.USER,
    tenant_id: uuid.UUID = _TENANT,
    approver: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        name="Carl Checker",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=kind,
        approver=approver,
    )


async def _seed(
    *,
    tenant_id: uuid.UUID = _TENANT,
    status: str = ApprovalRequestStatus.APPROVED.value,
    resumed_at: datetime | None = None,
    resume_result: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    request_id = uuid.uuid4()
    # Leave the column unset for "no result".
    result_kwargs = {} if resume_result is None else {"resume_result": resume_result}
    async with get_sessionmaker()() as session, session.begin():
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[-6:]}", name="Test tenant"))
            await session.flush()
        session.add(
            ApprovalRequest(
                id=request_id,
                tenant_id=tenant_id,
                run_id=None,
                principal_sub="agent:requester",
                op_id="vm.create",
                connector_id="vsphere-1.x",
                target_id=None,
                params_hash="0" * 64,
                params={"flavor": "small"},
                proposed_effect={"op_id": "vm.create"},
                status=status,
                reviewed_by="op:approver",
                resumed_at=resumed_at,
                created_at=created_at or datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                **result_kwargs,
            )
        )
    return request_id


async def _load(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row


async def _listed_ids(tenant_id: uuid.UUID = _TENANT) -> set[uuid.UUID]:
    async with get_sessionmaker()() as session:
        rows = await list_outcome_unknown(session, tenant_id=tenant_id)
    return {r.id for r in rows}


async def _check_audit_rows(request_id: uuid.UUID) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.path == "approval.outcome_check")
    async with get_sessionmaker()() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [r for r in rows if r.payload.get("approval_request_id") == str(request_id)]


async def _mark(
    request_id: uuid.UUID,
    *,
    operator: Operator | None = None,
    verdict: OutcomeCheckVerdict = OutcomeCheckVerdict.HAPPENED,
    note: str = "the VM exists on the host",
) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await mark_outcome_checked(
            session, request_id, operator=operator or _operator(), verdict=verdict, note=note
        )
        await session.commit()
        return row


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_has_uncertain_and_old_started_runs_only() -> None:
    uncertain = await _seed(resumed_at=_ago(1), resume_result=_UNCERTAIN)
    old_started = await _seed(resumed_at=_ago(16))
    recent_started = await _seed(resumed_at=_ago(14))
    not_started = await _seed()
    ran_ok = await _seed(resumed_at=_ago(30), resume_result={"status": "ok"})
    failed = await _seed(resumed_at=_ago(30), resume_result=_FAILED)
    pending = await _seed(status=ApprovalRequestStatus.PENDING.value, resumed_at=_ago(30))
    other_tenant = await _seed(tenant_id=_OTHER_TENANT, resumed_at=_ago(30))

    listed = await _listed_ids()

    assert listed == {uncertain, old_started}
    assert other_tenant in await _listed_ids(_OTHER_TENANT)
    # The Python rule says the same for each row.
    for request_id in (recent_started, not_started, ran_ok, failed, pending):
        assert outcome_unknown(await _load(request_id)) is None
    uncertain_reason = outcome_unknown(await _load(uncertain))
    assert uncertain_reason is not None
    assert uncertain_reason.reason is OutcomeUnknownReason.UNCERTAIN
    assert uncertain_reason.advice == UNCERTAIN_ADVICE
    started_reason = outcome_unknown(await _load(old_started))
    assert started_reason is not None
    assert started_reason.reason is OutcomeUnknownReason.STARTED_NO_RESULT
    assert started_reason.advice == STARTED_NO_RESULT_ADVICE


@pytest.mark.asyncio
async def test_a_run_stored_with_a_json_null_result_counts_as_no_result() -> None:
    """A row written with an explicit ``None`` holds JSON ``null``, not SQL NULL."""
    request_id = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=_TENANT, slug="t-json-null", name="Test tenant"))
        await session.flush()
        session.add(
            ApprovalRequest(
                id=request_id,
                tenant_id=_TENANT,
                run_id=None,
                principal_sub="agent:requester",
                op_id="vm.create",
                connector_id="vsphere-1.x",
                target_id=None,
                params_hash="0" * 64,
                params={},
                proposed_effect={},
                status=ApprovalRequestStatus.APPROVED.value,
                resumed_at=_ago(20),
                resume_result=None,
            )
        )
    assert await _listed_ids() == {request_id}
    assert outcome_unknown(await _load(request_id)) is not None


@pytest.mark.asyncio
async def test_the_setting_moves_the_line(monkeypatch: pytest.MonkeyPatch) -> None:
    started = await _seed(resumed_at=_ago(30))
    assert await _listed_ids() == {started}

    monkeypatch.setenv("APPROVAL_OUTCOME_UNKNOWN_AFTER_MINUTES", "60")
    get_settings.cache_clear()

    assert await _listed_ids() == set()
    assert outcome_unknown(await _load(started)) is None


@pytest.mark.asyncio
async def test_list_pages_and_sorts() -> None:
    first = await _seed(resumed_at=_ago(30), created_at=_ago(300))
    second = await _seed(resume_result=_UNCERTAIN, resumed_at=_ago(1), created_at=_ago(200))
    async with get_sessionmaker()() as session:
        newest = await list_outcome_unknown(session, tenant_id=_TENANT, limit=1)
        oldest = await list_outcome_unknown(session, tenant_id=_TENANT, limit=1, order="oldest")
        rest = await list_outcome_unknown(session, tenant_id=_TENANT, limit=1, offset=1)
    assert [r.id for r in newest] == [second]
    assert [r.id for r in oldest] == [first]
    assert [r.id for r in rest] == [first]


# ---------------------------------------------------------------------------
# Mark as checked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_writes_one_audit_row_keeps_the_result_and_runs_nothing() -> None:
    request_id = await _seed(resumed_at=_ago(5), resume_result=_UNCERTAIN)
    before = await _load(request_id)

    # No route is mocked, so any HTTP call to a vendor fails the test.
    with (
        respx.mock(assert_all_called=False),
        patch(
            "meho_backplane.operations.approval_queue.resume_dispatch_after_approval",
            new_callable=AsyncMock,
        ) as resume,
        patch(
            "meho_backplane.operations.approval_queue._dispatch_resume_with_bound_context",
            new_callable=AsyncMock,
        ) as dispatch,
    ):
        await _mark(request_id, verdict=OutcomeCheckVerdict.DID_NOT_HAPPEN, note="  no new VM  ")
        resume.assert_not_called()
        dispatch.assert_not_called()

    after = await _load(request_id)
    assert after.resume_result == before.resume_result
    assert after.resumed_at == before.resumed_at
    assert after.rerun_count == before.rerun_count
    assert after.status == ApprovalRequestStatus.APPROVED.value
    assert after.outcome_check_verdict == "did_not_happen"
    assert after.outcome_check_note == "no new VM"
    assert after.outcome_checked_by == _CHECKER
    assert after.outcome_checked_by_name == "Carl Checker"
    assert after.outcome_checked_at is not None

    (audit,) = await _check_audit_rows(request_id)
    assert audit.event_class == AuditEventClass.APPROVAL_DECISION
    assert audit.operator_sub == _CHECKER
    assert audit.status_code == 200
    assert audit.payload["verdict"] == "did_not_happen"
    assert audit.payload["note"] == "no new VM"
    assert audit.payload["outcome_unknown_reason"] == "uncertain"
    # A checked request leaves the list.
    assert request_id not in await _listed_ids()


@pytest.mark.asyncio
async def test_a_started_run_with_no_result_can_be_marked() -> None:
    request_id = await _seed(resumed_at=_ago(20))
    await _mark(request_id)
    (audit,) = await _check_audit_rows(request_id)
    assert audit.payload["outcome_unknown_reason"] == "started_no_result"
    assert (await _load(request_id)).resume_result is None


@pytest.mark.asyncio
async def test_mark_twice_is_refused_and_writes_no_second_row() -> None:
    request_id = await _seed(resumed_at=_ago(5), resume_result=_UNCERTAIN)
    await _mark(request_id)
    with pytest.raises(OutcomeAlreadyCheckedError):
        await _mark(request_id, verdict=OutcomeCheckVerdict.DID_NOT_HAPPEN)
    assert len(await _check_audit_rows(request_id)) == 1
    assert (await _load(request_id)).outcome_check_verdict == "happened"


@pytest.mark.parametrize(
    ("status", "minutes", "result"),
    [
        (ApprovalRequestStatus.APPROVED.value, 30, {"status": "ok"}),
        (ApprovalRequestStatus.APPROVED.value, 30, _FAILED),
        (ApprovalRequestStatus.APPROVED.value, 5, None),
        (ApprovalRequestStatus.APPROVED.value, None, None),
        (ApprovalRequestStatus.PENDING.value, None, None),
        (ApprovalRequestStatus.REJECTED.value, None, None),
    ],
)
@pytest.mark.asyncio
async def test_mark_refuses_a_request_that_is_not_on_the_list(
    status: str, minutes: int | None, result: dict[str, Any] | None
) -> None:
    request_id = await _seed(
        status=status,
        resumed_at=_ago(minutes) if minutes is not None else None,
        resume_result=result,
    )
    with pytest.raises(OutcomeNotUnknownError):
        await _mark(request_id)
    assert await _check_audit_rows(request_id) == []
    assert (await _load(request_id)).outcome_checked_at is None


@pytest.mark.asyncio
async def test_mark_refuses_an_empty_note() -> None:
    request_id = await _seed(resumed_at=_ago(5), resume_result=_UNCERTAIN)
    with pytest.raises(ValueError, match="note"):
        await _mark(request_id, note="   ")
    assert (await _load(request_id)).outcome_checked_at is None


@pytest.mark.parametrize(
    ("operator", "error"),
    [
        (_operator(kind=PrincipalKind.AGENT), NonHumanApprovalError),
        (_operator(kind=PrincipalKind.SERVICE), NonHumanApprovalError),
        (_operator(role=TenantRole.READ_ONLY), UnauthorizedApprovalError),
    ],
)
@pytest.mark.asyncio
async def test_only_a_person_who_may_decide_can_mark(
    operator: Operator, error: type[Exception]
) -> None:
    request_id = await _seed(resumed_at=_ago(5), resume_result=_UNCERTAIN)
    with pytest.raises(error):
        await _mark(request_id, operator=operator)
    assert (await _load(request_id)).outcome_checked_at is None


@pytest.mark.asyncio
async def test_a_read_only_approver_can_mark() -> None:
    request_id = await _seed(resumed_at=_ago(5), resume_result=_UNCERTAIN)
    await _mark(request_id, operator=_operator(role=TenantRole.READ_ONLY, approver=True))
    assert (await _load(request_id)).outcome_check_verdict == "happened"


@pytest.mark.asyncio
async def test_mark_cannot_reach_another_tenant() -> None:
    request_id = await _seed(tenant_id=_OTHER_TENANT, resumed_at=_ago(5), resume_result=_UNCERTAIN)
    with pytest.raises(ApprovalNotFoundError):
        await _mark(request_id)
    assert (await _load(request_id)).outcome_checked_at is None


# ---------------------------------------------------------------------------
# REST and MCP show the same list
# ---------------------------------------------------------------------------


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-outcome-check")
    with respx.mock(assert_all_called=False) as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


def _headers(key: Any, *, kind: str | None = None, role: str = "operator") -> dict[str, str]:
    token = mint_token(
        key,
        sub=_CHECKER,
        name="Carl Checker",
        tenant_role=role,
        tenant_id=str(_TENANT),
        principal_kind=kind,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_rest_and_mcp_list_match_the_service(
    client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = client
    uncertain = await _seed(resumed_at=_ago(1), resume_result=_UNCERTAIN)
    started = await _seed(resumed_at=_ago(20))
    await _seed(resumed_at=_ago(2))
    await _seed(resumed_at=_ago(20), resume_result={"status": "ok"})

    response = await ac.get(
        "/api/v1/approvals", params={"outcome_unknown": "true"}, headers=_headers(key)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert {uuid.UUID(item["id"]) for item in body} == {uncertain, started} == await _listed_ids()
    by_id = {uuid.UUID(item["id"]): item for item in body}
    assert by_id[uncertain]["outcome_unknown"] == {
        "reason": "uncertain",
        "advice": UNCERTAIN_ADVICE,
    }
    assert by_id[started]["outcome_unknown"]["reason"] == "started_no_result"
    assert by_id[started]["resume_outcome"]["resumed_at"] is not None
    assert by_id[started]["reviewed_by"] == "op:approver"
    assert by_id[started]["outcome_check"] is None

    mcp = await _list_handler(_operator(), {"outcome_unknown": True})
    assert {uuid.UUID(item["id"]) for item in mcp["items"]} == {uncertain, started}
    mcp_by_id = {uuid.UUID(item["id"]): item for item in mcp["items"]}
    assert mcp_by_id[started]["outcome_unknown"]["advice"] == STARTED_NO_RESULT_ADVICE


@pytest.mark.asyncio
async def test_mcp_refuses_a_non_boolean_filter() -> None:
    with pytest.raises(McpInvalidParamsError):
        await _list_handler(_operator(), {"outcome_unknown": "yes"})


@pytest.mark.asyncio
async def test_rest_mark_records_the_check_and_refuses_a_second(
    client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = client
    request_id = await _seed(resumed_at=_ago(1), resume_result=_UNCERTAIN)
    url = f"/api/v1/approvals/{request_id}/outcome-check"

    response = await ac.post(
        url, json={"verdict": "happened", "note": "the VM is there"}, headers=_headers(key)
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["outcome_check"]["verdict"] == "happened"
    assert view["outcome_check"]["note"] == "the VM is there"
    assert view["outcome_check"]["checked_by"] == _CHECKER
    assert view["outcome_check"]["checked_by_name"] == "Carl Checker"
    assert view["outcome_unknown"] is None
    # The original result stays: the run is still shown as uncertain.
    assert view["result_state"] == "uncertain"

    again = await ac.post(
        url, json={"verdict": "did_not_happen", "note": "second look"}, headers=_headers(key)
    )
    assert again.status_code == 409
    assert again.json()["detail"] == "outcome_already_checked"


@pytest.mark.parametrize(
    ("body", "status_code"),
    [
        ({"verdict": "maybe", "note": "n"}, 422),
        ({"verdict": "happened", "note": ""}, 422),
        ({"verdict": "happened", "note": "   "}, 422),
        ({"verdict": "happened"}, 422),
        ({"verdict": "happened", "note": "x" * 2001}, 422),
    ],
)
@pytest.mark.asyncio
async def test_rest_mark_refuses_a_bad_body(
    client: tuple[httpx.AsyncClient, Any], body: dict[str, Any], status_code: int
) -> None:
    ac, key = client
    request_id = await _seed(resumed_at=_ago(1), resume_result=_UNCERTAIN)
    response = await ac.post(
        f"/api/v1/approvals/{request_id}/outcome-check", json=body, headers=_headers(key)
    )
    assert response.status_code == status_code, response.text
    assert (await _load(request_id)).outcome_checked_at is None


@pytest.mark.asyncio
async def test_rest_mark_refuses_an_agent_a_known_result_and_a_missing_request(
    client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = client
    uncertain = await _seed(resumed_at=_ago(1), resume_result=_UNCERTAIN)
    ran_ok = await _seed(resumed_at=_ago(20), resume_result={"status": "ok"})
    body = {"verdict": "happened", "note": "checked"}

    as_agent = await ac.post(
        f"/api/v1/approvals/{uncertain}/outcome-check",
        json=body,
        headers=_headers(key, kind="agent"),
    )
    assert as_agent.status_code == 403
    known = await ac.post(
        f"/api/v1/approvals/{ran_ok}/outcome-check", json=body, headers=_headers(key)
    )
    assert known.status_code == 409
    assert known.json()["detail"] == "outcome_not_unknown"
    missing = await ac.post(
        f"/api/v1/approvals/{uuid.uuid4()}/outcome-check", json=body, headers=_headers(key)
    )
    assert missing.status_code == 404
    assert (await _load(uncertain)).outcome_checked_at is None
