# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A waiting approval is bound to the exact MEHO release (#5030).

A person approves one exact release of MEHO. This suite pins the rule:

* A request records the release it was made under (``release_id``).
* At start-up, every ``pending`` request from another release, or with no
  release (saved before MEHO recorded it), expires with the reason
  ``release_changed`` and one audit row.
* An approved request from another release that has not run is refused
  before the vendor call, on the operator path and on the agent path.
* A request made under the running release works as before.
* A build with no release identity (local development) binds nothing.
* REST, MCP and the console show the reason and the advice to ask again.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

import meho_backplane.operations.dispatcher as dispatcher_module
from meho_backplane.api.v1.approvals import _view
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog, Tenant
from meho_backplane.mcp.tools.approvals import _row_to_dict
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_expiry import (
    expire_approvals_from_older_release,
    sweep_expired_approvals,
)
from meho_backplane.operations.approval_queue import (
    EXPIRED_REASON_MESSAGES,
    ApprovalExpiredReason,
    approval_expired_reason,
    create_pending_request,
    get_request,
    resume_dispatch_after_approval,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.approvals.render import project_request_to_view
from meho_backplane.version import running_release_id

pytestmark = pytest.mark.asyncio

_OLD_SHA = "1" * 40
_NEW_SHA = "2" * 40
_OP_ID = "vault.kv.put"
_CONNECTOR_ID = "vault-1.x"
_PARAMS: dict[str, Any] = {"path": "app/config", "value": "<test>"}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("CHART_VERSION", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run_release(monkeypatch: pytest.MonkeyPatch, sha: str | None) -> None:
    """Make MEHO run as the release *sha* (``None``: no release identity)."""
    if sha is None:
        monkeypatch.delenv("GIT_SHA", raising=False)
    else:
        monkeypatch.setenv("GIT_SHA", sha)


def _operator(tenant_id: uuid.UUID, sub: str = "agent-requester") -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


async def _seed_tenant() -> uuid.UUID:
    tenant_id = uuid.uuid4()
    slug = f"rel-{tenant_id.hex[:8]}"
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()
    return tenant_id


async def _park(tenant_id: uuid.UUID, *, approved: bool = False) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_operator(tenant_id),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=dict(_PARAMS),
            params_hash=compute_params_hash(_PARAMS),
        )
        if approved:
            request.status = ApprovalRequestStatus.APPROVED.value
            request.decided_at = datetime.now(UTC)
            request.reviewed_by = "op-approver"
        await session.commit()
        return request.id


async def _row(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row


async def _audit_rows(request_id: uuid.UUID, path: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.path == path))).scalars()
        return [r for r in rows if r.payload.get("approval_request_id") == str(request_id)]


def _spy_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the dispatcher; every call stands for a call to the vendor."""
    calls: list[dict[str, Any]] = []

    async def _fake_dispatch(**kwargs: Any) -> OperationResult:
        calls.append(kwargs)
        return OperationResult(status="ok", op_id=kwargs["op_id"], data={}, duration_ms=1.0)

    monkeypatch.setattr(dispatcher_module, "dispatch", _fake_dispatch)
    return calls


# ---------------------------------------------------------------------------
# The release identity
# ---------------------------------------------------------------------------


async def test_running_release_id_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    assert running_release_id() is None
    monkeypatch.setenv("GIT_SHA", "unknown")
    assert running_release_id() is None
    monkeypatch.setenv("GIT_SHA", _NEW_SHA)
    assert running_release_id() == _NEW_SHA
    monkeypatch.setenv("CHART_VERSION", "0.40.0")
    assert running_release_id() == f"{_NEW_SHA} (chart 0.40.0)"


async def test_request_records_the_running_release(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = await _seed_tenant()
    _run_release(monkeypatch, _OLD_SHA)
    request_id = await _park(tenant)
    assert (await _row(request_id)).release_id == _OLD_SHA


# ---------------------------------------------------------------------------
# Start-up: waiting requests from an older release expire
# ---------------------------------------------------------------------------


async def test_startup_expires_pending_request_from_older_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await _seed_tenant()
    _run_release(monkeypatch, _OLD_SHA)
    request_id = await _park(tenant)

    _run_release(monkeypatch, _NEW_SHA)
    assert await expire_approvals_from_older_release() == 1

    row = await _row(request_id)
    assert row.status == ApprovalRequestStatus.EXPIRED.value
    assert approval_expired_reason(row) is ApprovalExpiredReason.RELEASE_CHANGED
    (audit,) = await _audit_rows(request_id, "approval.decision")
    assert audit.status_code == 410
    assert audit.payload["decision"] == "expired"
    assert audit.payload["reason"] == "release_changed"
    assert audit.payload["request_release"] == _OLD_SHA
    assert audit.payload["running_release"] == _NEW_SHA
    assert audit.operator_sub == "system:approval-expiry"


async def test_startup_expires_request_saved_without_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request saved before this change has no release; the first start ends it."""
    tenant = await _seed_tenant()
    _run_release(monkeypatch, None)
    request_id = await _park(tenant)
    assert (await _row(request_id)).release_id is None

    _run_release(monkeypatch, _NEW_SHA)
    assert await expire_approvals_from_older_release() == 1

    row = await _row(request_id)
    assert row.status == ApprovalRequestStatus.EXPIRED.value
    assert approval_expired_reason(row) is ApprovalExpiredReason.RELEASE_CHANGED
    (audit,) = await _audit_rows(request_id, "approval.decision")
    assert audit.payload["request_release"] is None


async def test_startup_keeps_request_from_running_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await _seed_tenant()
    _run_release(monkeypatch, _NEW_SHA)
    request_id = await _park(tenant)

    assert await expire_approvals_from_older_release() == 0
    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


async def test_startup_does_nothing_without_release_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local development run has no release identity and binds nothing."""
    tenant = await _seed_tenant()
    _run_release(monkeypatch, _OLD_SHA)
    request_id = await _park(tenant)

    _run_release(monkeypatch, None)
    assert await expire_approvals_from_older_release() == 0
    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


async def test_startup_leaves_decided_requests_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = await _seed_tenant()
    _run_release(monkeypatch, _OLD_SHA)
    request_id = await _park(tenant, approved=True)

    _run_release(monkeypatch, _NEW_SHA)
    assert await expire_approvals_from_older_release() == 0
    assert (await _row(request_id)).status == ApprovalRequestStatus.APPROVED.value


async def test_overdue_request_keeps_the_deadline_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request past its deadline expires as the TTL sweep would expire it."""
    tenant = await _seed_tenant()
    _run_release(monkeypatch, _OLD_SHA)
    request_id = await _park(tenant)
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    _run_release(monkeypatch, _NEW_SHA)
    assert await expire_approvals_from_older_release() == 1

    row = await _row(request_id)
    assert approval_expired_reason(row) is ApprovalExpiredReason.DEADLINE_PASSED
    (audit,) = await _audit_rows(request_id, "approval.decision")
    assert "reason" not in audit.payload


async def test_ttl_sweep_reason_is_deadline_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = await _seed_tenant()
    request_id = await _park(tenant)
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    assert await sweep_expired_approvals() == 1
    assert approval_expired_reason(await _row(request_id)) is (
        ApprovalExpiredReason.DEADLINE_PASSED
    )


# ---------------------------------------------------------------------------
# Before an approved run: a request from an older release is refused
# ---------------------------------------------------------------------------


async def test_approved_request_from_older_release_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await _seed_tenant()
    _run_release(monkeypatch, _OLD_SHA)
    request_id = await _park(tenant, approved=True)
    calls = _spy_dispatch(monkeypatch)

    _run_release(monkeypatch, _NEW_SHA)
    row = await _row(request_id)
    result = await resume_dispatch_after_approval(
        operator=_operator(tenant, "op-approver"), request=row
    )

    assert calls == [], "the vendor must get no call"
    assert result.status == "denied"
    assert result.extras["error_code"] == "release_changed_while_waiting"
    assert result.extras["request_release"] == _OLD_SHA
    assert result.extras["running_release"] == _NEW_SHA
    (audit,) = await _audit_rows(request_id, "approval.resume")
    assert audit.status_code == 409
    assert audit.payload["result_status"] == "release_changed_while_waiting"
    assert result.audit_id == audit.id

    # The request can never run later: the one-resumer claim is taken, and
    # the refusal is its stored run result.
    stored = await _row(request_id)
    assert stored.resumed_at is not None
    assert stored.resume_result is not None
    assert stored.resume_result["extras"]["error_code"] == "release_changed_while_waiting"
    again = await resume_dispatch_after_approval(
        operator=_operator(tenant, "op-approver"), request=stored
    )
    assert again.status == "already_resumed"
    assert calls == []


async def test_approved_request_from_running_release_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await _seed_tenant()
    _run_release(monkeypatch, _NEW_SHA)
    request_id = await _park(tenant, approved=True)
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator(tenant, "op-approver"), request=await _row(request_id)
    )

    assert result.status == "ok"
    assert len(calls) == 1
    assert await _audit_rows(request_id, "approval.resume") == []


async def test_no_release_identity_runs_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = await _seed_tenant()
    _run_release(monkeypatch, _OLD_SHA)
    request_id = await _park(tenant, approved=True)
    calls = _spy_dispatch(monkeypatch)

    _run_release(monkeypatch, None)
    result = await resume_dispatch_after_approval(
        operator=_operator(tenant, "op-approver"), request=await _row(request_id)
    )

    assert result.status == "ok"
    assert len(calls) == 1


async def test_agent_resume_of_older_release_request_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-process agent waiter refuses the same way and runs nothing."""
    import meho_backplane.operations.meta_tools as meta_tools_module
    from meho_backplane.agent.approval_wait import resume_or_surface_awaiting_approval

    tenant = await _seed_tenant()
    _run_release(monkeypatch, _OLD_SHA)
    request_id = await _park(tenant, approved=True)

    async def _no_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("the vendor must get no call")

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _no_call)

    _run_release(monkeypatch, _NEW_SHA)
    envelope = await resume_or_surface_awaiting_approval(
        operator=_operator(tenant),
        call_arguments={
            "connector_id": _CONNECTOR_ID,
            "op_id": _OP_ID,
            "params": dict(_PARAMS),
            "target": None,
        },
        awaiting_envelope={
            "status": "awaiting_approval",
            "op_id": _OP_ID,
            "extras": {"approval_request_id": str(request_id)},
        },
        timeout_seconds=2.0,
    )

    assert envelope["status"] == "denied"
    assert envelope["extras"]["error_code"] == "release_changed_while_waiting"
    assert len(await _audit_rows(request_id, "approval.resume")) == 1


# ---------------------------------------------------------------------------
# Read surfaces: the reason and the advice to ask again
# ---------------------------------------------------------------------------


async def test_read_surfaces_show_reason_and_advice(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = await _seed_tenant()
    _run_release(monkeypatch, _OLD_SHA)
    request_id = await _park(tenant)
    _run_release(monkeypatch, _NEW_SHA)
    await expire_approvals_from_older_release()

    async with get_sessionmaker()() as session:
        row = await get_request(session, tenant_id=tenant, request_id=request_id)
    message = EXPIRED_REASON_MESSAGES[ApprovalExpiredReason.RELEASE_CHANGED]
    assert "Ask again" in message

    rest = _view(row)
    assert rest.expired_reason is ApprovalExpiredReason.RELEASE_CHANGED
    assert rest.expired_message == message
    assert rest.release_id == _OLD_SHA

    mcp = _row_to_dict(row)
    assert mcp["expired_reason"] == "release_changed"
    assert mcp["expired_message"] == message
    assert mcp["release_id"] == _OLD_SHA

    console = project_request_to_view(row)
    assert console["expired_reason"] == "release_changed"
    assert console["expired_message"] == message
    assert console["release_id"] == _OLD_SHA


async def test_read_surfaces_have_no_reason_for_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = await _seed_tenant()
    _run_release(monkeypatch, _NEW_SHA)
    request_id = await _park(tenant)
    row = await _row(request_id)

    assert _view(row).expired_reason is None
    assert _row_to_dict(row)["expired_message"] is None
    assert project_request_to_view(row)["expired_reason"] is None
