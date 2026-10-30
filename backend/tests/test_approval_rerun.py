# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run an approved request again after its run failed (#5417).

Covers the service (:func:`rerun_failed_request`) and the REST route
``POST /api/v1/approvals/{id}/rerun``. The rules:

* Only the requester, and only when the last run failed.
* No new approval when nothing changed. Different params, a passed
  deadline, a new MEHO release or another work reference are refused, and
  nothing runs.
* Two re-runs at the same time run the operation once.
* Every re-run, and every refused re-run, writes an ``approval.rerun``
  audit row that names the attempt.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
import structlog
from httpx import ASGITransport
from sqlalchemy import select

import meho_backplane.operations.dispatcher as dispatcher_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus, AuditEventClass, AuditLog
from meho_backplane.main import app
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ApprovalResultState,
    approval_result_state,
    create_pending_request,
    get_request,
)
from meho_backplane.operations.approval_rerun import (
    RERUN_AUDIT_PATH,
    RerunForbiddenError,
    RerunNeedsNewApprovalError,
    RerunNotAllowedError,
    RerunParamsRequiredError,
    RerunRefusal,
    rerun_failed_request,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000005417")
_OTHER_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000005418")
_REQUESTER_SUB = "agent:requester"
_OTHER_SUB = "op:someone-else"
_OP_ID = "vsphere.vm.power_off"
_CONNECTOR_ID = "vmware-rest-9.0"
_PARAMS = {"vm": "vm-42"}
_FAILED_RESULT = {
    "status": "error",
    "op_id": _OP_ID,
    "error": "connector_error: parent folder does not exist",
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("CHART_VERSION", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _operator(*, sub: str = _REQUESTER_SUB, tenant_id: uuid.UUID = _TENANT_ID) -> Operator:
    return Operator(
        sub=sub,
        name="Test Principal",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


async def _park_failed(
    *,
    tenant_id: uuid.UUID = _TENANT_ID,
    resume_result: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
    stored_params: bool = True,
    release_id: str | None = None,
) -> uuid.UUID:
    """Park one request, approve it, and store a failed run. Return its id."""
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_operator(tenant_id=tenant_id),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
        )
        request.status = ApprovalRequestStatus.APPROVED.value
        request.reviewed_by = _OTHER_SUB
        request.decided_at = datetime.now(UTC)
        request.resumed_at = datetime.now(UTC)
        request.resume_result = resume_result if resume_result is not None else _FAILED_RESULT
        if expires_at is not None:
            request.expires_at = expires_at
        if not stored_params:
            request.params = None
        if release_id is not None:
            request.release_id = release_id
        await s.commit()
        return request.id


async def _row(request_id: uuid.UUID, tenant_id: uuid.UUID = _TENANT_ID) -> Any:
    async with get_sessionmaker()() as s:
        return await get_request(s, tenant_id=tenant_id, request_id=request_id)


async def _rerun_rows(request_id: uuid.UUID) -> list[AuditLog]:
    async with get_sessionmaker()() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.path == RERUN_AUDIT_PATH)))
            .scalars()
            .all()
        )
    return [r for r in rows if r.payload.get("approval_request_id") == str(request_id)]


class _FakeDispatch:
    """Stand-in for ``dispatch``: records calls and returns *status*."""

    def __init__(self, status: str = "ok", delay: float = 0.0) -> None:
        self.status = status
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.contexts: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> OperationResult:
        self.calls.append(kwargs)
        self.contexts.append(dict(structlog.contextvars.get_contextvars()))
        if self.delay:
            await asyncio.sleep(self.delay)
        return OperationResult(
            status=self.status,
            op_id=kwargs["op_id"],
            result={"done": True} if self.status == "ok" else None,
            error=None if self.status == "ok" else "connector_error: still broken",
            duration_ms=1.0,
        )


@pytest.fixture
def fake_dispatch(monkeypatch: pytest.MonkeyPatch) -> _FakeDispatch:
    fake = _FakeDispatch()
    monkeypatch.setattr(dispatcher_module, "dispatch", fake)
    return fake


# ---------------------------------------------------------------------------
# Service: the re-run runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_request_runs_again_once_with_no_new_approval(
    fake_dispatch: _FakeDispatch,
) -> None:
    request_id = await _park_failed()

    outcome = await rerun_failed_request(operator=_operator(), request_id=request_id)

    assert outcome.attempt == 2
    assert outcome.result.status == "ok"
    assert len(fake_dispatch.calls) == 1
    call = fake_dispatch.calls[0]
    assert call["op_id"] == _OP_ID
    assert call["connector_id"] == _CONNECTOR_ID
    assert call["params"] == _PARAMS
    assert call["_approved"] is True
    assert call["operator"].sub == _REQUESTER_SUB
    # The run row names the request and the attempt.
    assert fake_dispatch.contexts[0]["audit_approval_request_id"] == str(request_id)
    assert fake_dispatch.contexts[0]["audit_approval_rerun_attempt"] == 2

    row = await _row(request_id)
    assert row.status == ApprovalRequestStatus.APPROVED.value
    assert row.rerun_count == 1
    assert row.resume_result["status"] == "ok"
    assert approval_result_state(row) is ApprovalResultState.EXECUTED

    (audit,) = await _rerun_rows(request_id)
    assert audit.operator_sub == _REQUESTER_SUB
    assert audit.event_class == AuditEventClass.POLICY_DECISION
    assert audit.status_code == 202
    assert audit.payload["result_status"] == "accepted"
    assert audit.payload["attempt"] == 2
    assert audit.parent_audit_id == row.request_audit_id


@pytest.mark.asyncio
async def test_failed_rerun_can_run_again_and_counts_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = _FakeDispatch(status="error")
    monkeypatch.setattr(dispatcher_module, "dispatch", failing)
    request_id = await _park_failed()

    first = await rerun_failed_request(operator=_operator(), request_id=request_id)
    second = await rerun_failed_request(operator=_operator(), request_id=request_id)

    assert (first.attempt, second.attempt) == (2, 3)
    assert len(failing.calls) == 2
    row = await _row(request_id)
    assert row.rerun_count == 2
    assert approval_result_state(row) is ApprovalResultState.FAILED
    assert [r.payload["attempt"] for r in await _rerun_rows(request_id)] in ([2, 3], [3, 2])


@pytest.mark.asyncio
async def test_matching_params_sent_again_run(fake_dispatch: _FakeDispatch) -> None:
    request_id = await _park_failed()
    outcome = await rerun_failed_request(
        operator=_operator(), request_id=request_id, params=dict(_PARAMS)
    )
    assert outcome.result.status == "ok"
    assert len(fake_dispatch.calls) == 1


@pytest.mark.asyncio
async def test_two_reruns_at_the_same_time_run_once(monkeypatch: pytest.MonkeyPatch) -> None:
    slow = _FakeDispatch(delay=0.2)
    monkeypatch.setattr(dispatcher_module, "dispatch", slow)
    request_id = await _park_failed()

    results = await asyncio.gather(
        rerun_failed_request(operator=_operator(), request_id=request_id),
        rerun_failed_request(operator=_operator(), request_id=request_id),
        return_exceptions=True,
    )

    assert len(slow.calls) == 1
    refused = [r for r in results if isinstance(r, RerunNotAllowedError)]
    ran = [r for r in results if not isinstance(r, BaseException)]
    assert len(refused) == 1
    assert len(ran) == 1
    assert (await _row(request_id)).rerun_count == 1
    statuses = sorted(r.payload["result_status"] for r in await _rerun_rows(request_id))
    assert statuses == ["accepted", "refused"]


@pytest.mark.asyncio
async def test_rerun_that_read_the_old_failure_is_refused_after_a_failed_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two callers read the original failure. The first re-run fails fast.

    The second caller still holds the row it read before the first re-run.
    Its claim must not match, although the stored result is a failure again,
    or both callers would run the operation.
    """
    import meho_backplane.operations.approval_rerun as rerun_module

    failing = _FakeDispatch(status="error")
    monkeypatch.setattr(dispatcher_module, "dispatch", failing)
    request_id = await _park_failed()

    real_rehydrate = rerun_module.rehydrate_resume_target
    both_read = asyncio.Event()
    first_done = asyncio.Event()
    counts_read: list[int] = []

    async def _gated_rehydrate(operator: Operator, request: Any) -> Any:
        # Both callers pass here after reading the row and before claiming.
        # The first one waits until the second has read the row too, then
        # runs. The second one waits until the first re-run finished.
        counts_read.append(request.rerun_count)
        if len(counts_read) == 1:
            await both_read.wait()
        else:
            both_read.set()
            await first_done.wait()
        return await real_rehydrate(operator, request)

    monkeypatch.setattr(rerun_module, "rehydrate_resume_target", _gated_rehydrate)

    async def _attempt() -> Any:
        try:
            return await rerun_failed_request(operator=_operator(), request_id=request_id)
        finally:
            first_done.set()

    results = await asyncio.gather(_attempt(), _attempt(), return_exceptions=True)

    assert counts_read == [0, 0]
    assert len(failing.calls) == 1
    ran = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, RerunNotAllowedError)]
    assert len(ran) == 1
    assert ran[0].attempt == 2
    assert ran[0].result.status == "error"
    assert len(refused) == 1
    row = await _row(request_id)
    assert row.rerun_count == 1
    assert approval_result_state(row) is ApprovalResultState.FAILED
    audits = await _rerun_rows(request_id)
    assert sorted(a.payload["result_status"] for a in audits) == ["accepted", "refused"]
    assert [a.payload["refusal"] for a in audits if "refusal" in a.payload] == ["claim_lost"]


@pytest.mark.asyncio
async def test_run_that_raises_stores_a_failure_so_it_can_run_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(**_kwargs: Any) -> OperationResult:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(dispatcher_module, "dispatch", _boom)
    request_id = await _park_failed()

    with pytest.raises(RuntimeError):
        await rerun_failed_request(operator=_operator(), request_id=request_id)

    row = await _row(request_id)
    assert approval_result_state(row) is ApprovalResultState.FAILED
    assert row.resume_result["error"].startswith("rerun_failed:")


# ---------------------------------------------------------------------------
# Service: refused re-runs run nothing
# ---------------------------------------------------------------------------


async def _assert_refused_and_nothing_ran(
    request_id: uuid.UUID, fake: _FakeDispatch, refusal: str
) -> None:
    assert fake.calls == []
    row = await _row(request_id)
    assert row.rerun_count == 0
    assert approval_result_state(row) is ApprovalResultState.FAILED
    (audit,) = await _rerun_rows(request_id)
    assert audit.payload["result_status"] == "refused"
    assert audit.payload["refusal"] == refusal


@pytest.mark.asyncio
async def test_different_params_are_refused_and_need_a_new_approval(
    fake_dispatch: _FakeDispatch,
) -> None:
    request_id = await _park_failed()

    with pytest.raises(RerunNeedsNewApprovalError) as exc_info:
        await rerun_failed_request(
            operator=_operator(), request_id=request_id, params={"vm": "vm-43"}
        )

    assert exc_info.value.reason is RerunRefusal.PARAMS_CHANGED
    assert "new request" in str(exc_info.value)
    await _assert_refused_and_nothing_ran(request_id, fake_dispatch, "params_changed")
    (audit,) = await _rerun_rows(request_id)
    assert audit.status_code == 409


@pytest.mark.asyncio
async def test_rerun_after_the_deadline_is_refused(fake_dispatch: _FakeDispatch) -> None:
    request_id = await _park_failed(expires_at=datetime.now(UTC) - timedelta(minutes=1))

    with pytest.raises(RerunNeedsNewApprovalError) as exc_info:
        await rerun_failed_request(operator=_operator(), request_id=request_id)

    assert exc_info.value.reason is RerunRefusal.DEADLINE_PASSED
    await _assert_refused_and_nothing_ran(request_id, fake_dispatch, "deadline_passed")


@pytest.mark.asyncio
async def test_rerun_after_a_meho_update_is_refused(
    fake_dispatch: _FakeDispatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = await _park_failed(release_id="a" * 40)
    monkeypatch.setenv("GIT_SHA", "b" * 40)

    with pytest.raises(RerunNeedsNewApprovalError) as exc_info:
        await rerun_failed_request(operator=_operator(), request_id=request_id)

    assert exc_info.value.reason is RerunRefusal.RELEASE_CHANGED
    await _assert_refused_and_nothing_ran(request_id, fake_dispatch, "release_changed")


@pytest.mark.asyncio
async def test_other_work_ref_is_refused(fake_dispatch: _FakeDispatch) -> None:
    request_id = await _park_failed()

    token = work_ref_var.set("CHANGE-2")
    try:
        with pytest.raises(RerunNeedsNewApprovalError) as exc_info:
            await rerun_failed_request(operator=_operator(), request_id=request_id)
    finally:
        work_ref_var.reset(token)

    assert exc_info.value.reason is RerunRefusal.WORK_REF_CHANGED
    await _assert_refused_and_nothing_ran(request_id, fake_dispatch, "work_ref_changed")


@pytest.mark.asyncio
@pytest.mark.parametrize("same_preview", [True, False])
async def test_destructive_request_checks_the_preview_again(
    fake_dispatch: _FakeDispatch, monkeypatch: pytest.MonkeyPatch, same_preview: bool
) -> None:
    import meho_backplane.operations._request_preview as preview_module

    envelope = {
        "status": "ok",
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "method": "POST",
        "resolved_path": "/api/vcenter/vm/vm-42/power?action=stop",
        "query": {},
        "redacted_body": None,
    }

    async def _preview(**_kwargs: Any) -> dict[str, Any]:
        return envelope

    monkeypatch.setattr(preview_module, "preview_dispatch", _preview)
    request_id = await _park_failed()
    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
        row.preview_hash = (
            preview_module.compute_preview_hash(envelope) if same_preview else "0" * 64
        )
        await s.commit()

    if same_preview:
        outcome = await rerun_failed_request(operator=_operator(), request_id=request_id)
        assert outcome.result.status == "ok"
        return
    with pytest.raises(RerunNeedsNewApprovalError) as exc_info:
        await rerun_failed_request(operator=_operator(), request_id=request_id)
    assert exc_info.value.reason is RerunRefusal.PREVIEW_CHANGED
    await _assert_refused_and_nothing_ran(request_id, fake_dispatch, "preview_changed")


@pytest.mark.asyncio
async def test_credential_write_needs_the_params_sent_again(
    fake_dispatch: _FakeDispatch,
) -> None:
    request_id = await _park_failed(stored_params=False)

    with pytest.raises(RerunParamsRequiredError):
        await rerun_failed_request(operator=_operator(), request_id=request_id)
    await _assert_refused_and_nothing_ran(request_id, fake_dispatch, "params_required")

    outcome = await rerun_failed_request(
        operator=_operator(), request_id=request_id, params=dict(_PARAMS)
    )
    assert outcome.result.status == "ok"
    assert fake_dispatch.calls[0]["params"] == _PARAMS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resume_result", "state"),
    [
        ({"status": "ok", "op_id": _OP_ID}, "executed"),
        (None, "started"),
    ],
)
async def test_request_that_did_not_fail_cannot_run_again(
    fake_dispatch: _FakeDispatch, resume_result: dict[str, Any] | None, state: str
) -> None:
    request_id = await _park_failed(resume_result={"status": "ok", "op_id": _OP_ID})
    if resume_result is None:
        async with get_sessionmaker()() as s:
            row = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
            row.resume_result = None
            await s.commit()

    with pytest.raises(RerunNotAllowedError) as exc_info:
        await rerun_failed_request(operator=_operator(), request_id=request_id)

    assert exc_info.value.state == state
    assert fake_dispatch.calls == []


@pytest.mark.asyncio
async def test_pending_request_cannot_run_again(fake_dispatch: _FakeDispatch) -> None:
    request_id = await _park_failed()
    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
        row.status = ApprovalRequestStatus.PENDING.value
        await s.commit()

    with pytest.raises(RerunNotAllowedError) as exc_info:
        await rerun_failed_request(operator=_operator(), request_id=request_id)

    assert exc_info.value.state == "pending"
    assert fake_dispatch.calls == []


@pytest.mark.asyncio
async def test_only_the_requester_can_rerun(fake_dispatch: _FakeDispatch) -> None:
    request_id = await _park_failed()
    with pytest.raises(RerunForbiddenError):
        await rerun_failed_request(operator=_operator(sub=_OTHER_SUB), request_id=request_id)
    assert fake_dispatch.calls == []


@pytest.mark.asyncio
async def test_other_tenant_request_is_not_found(fake_dispatch: _FakeDispatch) -> None:
    request_id = await _park_failed(tenant_id=_OTHER_TENANT_ID)
    with pytest.raises(ApprovalNotFoundError):
        await rerun_failed_request(operator=_operator(), request_id=request_id)
    assert fake_dispatch.calls == []


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-rerun")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


def _headers(key: Any, *, sub: str = _REQUESTER_SUB) -> dict[str, str]:
    token = mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.READ_ONLY.value,
        tenant_id=str(_TENANT_ID),
        principal_kind="agent",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_route_reruns_the_failed_request(
    client: tuple[httpx.AsyncClient, Any], fake_dispatch: _FakeDispatch
) -> None:
    ac, key = client
    request_id = await _park_failed()

    response = await ac.post(
        f"/api/v1/approvals/{request_id}/rerun", json={}, headers=_headers(key)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approval_request_id"] == str(request_id)
    assert body["attempt"] == 2
    assert body["dispatch_status"] == "ok"
    assert body["dispatch_result"] == {"done": True}
    assert len(fake_dispatch.calls) == 1


@pytest.mark.asyncio
async def test_route_different_params_get_409_and_nothing_runs(
    client: tuple[httpx.AsyncClient, Any], fake_dispatch: _FakeDispatch
) -> None:
    ac, key = client
    request_id = await _park_failed()

    response = await ac.post(
        f"/api/v1/approvals/{request_id}/rerun",
        json={"params": {"vm": "vm-43"}},
        headers=_headers(key),
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"].startswith("approval_rerun_needs_new_approval:")
    assert fake_dispatch.calls == []


@pytest.mark.asyncio
async def test_route_non_requester_gets_403(
    client: tuple[httpx.AsyncClient, Any], fake_dispatch: _FakeDispatch
) -> None:
    ac, key = client
    request_id = await _park_failed()

    response = await ac.post(
        f"/api/v1/approvals/{request_id}/rerun", json={}, headers=_headers(key, sub=_OTHER_SUB)
    )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "not_request_owner"}
    assert fake_dispatch.calls == []


@pytest.mark.asyncio
async def test_route_missing_params_of_a_credential_write_get_422(
    client: tuple[httpx.AsyncClient, Any], fake_dispatch: _FakeDispatch
) -> None:
    ac, key = client
    request_id = await _park_failed(stored_params=False)

    response = await ac.post(
        f"/api/v1/approvals/{request_id}/rerun", json={}, headers=_headers(key)
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("approval_rerun_params_required:")
    assert fake_dispatch.calls == []


@pytest.mark.asyncio
async def test_route_unknown_request_gets_404(client: tuple[httpx.AsyncClient, Any]) -> None:
    ac, key = client
    response = await ac.post(
        f"/api/v1/approvals/{uuid.uuid4()}/rerun", json={}, headers=_headers(key)
    )
    assert response.status_code == 404, response.text
