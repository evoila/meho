# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A stored approval request keeps the values of its call only while it can run.

* An approved request that nobody starts within ``APPROVAL_DEFAULT_TTL``
  after its decision ends as ``expired``. Its encrypted handoff row is
  deleted and its stored params keep no secret-named value. A claim of its
  run after that is refused, and the agent that waits reads the usual
  expired answer.
* An approved request that starts in time runs as before.
* At start-up, every request that is not waiting, was never started and cannot
  run gets the same clean-up, in batches. This covers rows stored
  by an older release.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

import meho_backplane.operations.dispatcher as dispatcher_module
from meho_backplane.agent.approval_wait import _lost_claim_envelope
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog, Tenant
from meho_backplane.operations import approval_stored_values
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_expiry import sweep_expired_approvals
from meho_backplane.operations.approval_handoff import ApprovalExecutionPayload
from meho_backplane.operations.approval_queue import (
    ApprovalExpiredReason,
    approval_expired_reason,
    claim_resume,
    consume_remote_write_approval,
    create_pending_request,
    get_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.approval_stored_values import (
    CLEAN_UP_BATCH_SIZE,
    hide_values_of_requests_that_cannot_run,
)
from meho_backplane.settings import get_settings

_REQUESTER_SUB = "agent:requester"
_MARKER = "[REDACTED:param_name]"
_PW = "made-up-pw-value"
_SPEC_OP = "installer.sddc.bringup.start"
_SPEC_PARAMS: dict[str, Any] = {"spec": {"name": "lab-1", "rootPassword": _PW}}
_SECRET_WRITE_OP = "k8s.secret.create"
_SECRET_WRITE_PARAMS: dict[str, Any] = {
    "name": "db-login",
    "namespace": "apps",
    "string_data": {"password": _PW},
}
_APPLY_OP = "k8s.apply"
_SECRET_MANIFEST = (
    "apiVersion: v1\n"
    "kind: Secret\n"
    "metadata: {name: db-login, namespace: apps}\n"
    f"stringData: {{password: {_PW}}}\n"
)
_RUNNING = "release-b"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("CHART_VERSION", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _k8s_rules() -> None:
    """The rules the Kubernetes connector declares for its operations."""
    import meho_backplane.connectors.kubernetes.op_classes  # noqa: F401


def _ttl() -> timedelta:
    return timedelta(seconds=get_settings().approval_default_ttl_seconds)


def _requester(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub=_REQUESTER_SUB,
        name="Requester",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


async def _tenant() -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="Tenant"))
        await s.commit()
    return tenant_id


async def _park(
    tenant_id: uuid.UUID,
    op_id: str,
    params: dict[str, Any],
    *,
    status: ApprovalRequestStatus = ApprovalRequestStatus.APPROVED,
    decided_ago: timedelta | None = None,
    release_id: str | None = None,
) -> uuid.UUID:
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_requester(tenant_id),
            connector_id="example-1.x",
            op_id=op_id,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        if status is not ApprovalRequestStatus.PENDING:
            request.status = status.value
            request.reviewed_by = "op:reviewer"
            request.decided_at = datetime.now(UTC) - (decided_ago or timedelta())
        request.release_id = release_id
        await s.commit()
        return request.id


async def _load(tenant_id: uuid.UUID, request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as s:
        return await get_request(s, tenant_id=tenant_id, request_id=request_id)


async def _handoff_rows(request_id: uuid.UUID) -> int:
    async with get_sessionmaker()() as s:
        rows = await s.scalars(
            select(ApprovalExecutionPayload).where(
                ApprovalExecutionPayload.request_id == request_id
            )
        )
        return len(list(rows))


def _past_ttl() -> timedelta:
    return _ttl() + timedelta(minutes=1)


# ---------------------------------------------------------------------------
# An approved request that nobody starts ends after the TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unstarted_approved_credential_write_expires_and_keeps_no_value() -> None:
    tenant_id = await _tenant()
    request_id = await _park(
        tenant_id, _SECRET_WRITE_OP, _SECRET_WRITE_PARAMS, decided_ago=_past_ttl()
    )
    assert await _handoff_rows(request_id) == 1

    assert await sweep_expired_approvals() == 1

    row = await _load(tenant_id, request_id)
    assert row.status == ApprovalRequestStatus.EXPIRED.value
    assert approval_expired_reason(row) is ApprovalExpiredReason.DEADLINE_PASSED
    assert await _handoff_rows(request_id) == 0
    assert _PW not in json.dumps({"params": row.params, "parent": row.resume_parent})
    async with get_sessionmaker()() as s:
        decisions = (
            await s.scalars(
                select(AuditLog).where(
                    AuditLog.path == "approval.decision", AuditLog.tenant_id == tenant_id
                )
            )
        ).all()
    assert [d.status_code for d in decisions] == [410]


@pytest.mark.asyncio
async def test_an_unstarted_approved_request_keeps_no_secret_named_value() -> None:
    tenant_id = await _tenant()
    request_id = await _park(tenant_id, _SPEC_OP, _SPEC_PARAMS, decided_ago=_past_ttl())

    assert await sweep_expired_approvals() == 1

    row = await _load(tenant_id, request_id)
    assert row.status == ApprovalRequestStatus.EXPIRED.value
    assert row.params == {"spec": {"name": "lab-1", "rootPassword": _MARKER}}


@pytest.mark.asyncio
async def test_a_claim_after_the_expiry_is_refused() -> None:
    tenant_id = await _tenant()
    request_id = await _park(tenant_id, _SPEC_OP, _SPEC_PARAMS, decided_ago=_past_ttl())
    await sweep_expired_approvals()

    assert await claim_resume(request_id) is False
    row = await _load(tenant_id, request_id)
    assert row.resumed_at is None
    async with get_sessionmaker()() as s:
        assert await consume_remote_write_approval(s, row) is False


@pytest.mark.asyncio
async def test_an_agent_that_resumes_too_late_reads_the_expired_answer() -> None:
    tenant_id = await _tenant()
    request_id = await _park(tenant_id, _SPEC_OP, _SPEC_PARAMS, decided_ago=_past_ttl())
    await sweep_expired_approvals()
    waiting = {"status": "awaiting_approval", "op_id": _SPEC_OP, "extras": {}}

    answer = await _lost_claim_envelope(
        operator=_requester(tenant_id),
        original_envelope=waiting,
        approval_request_id=request_id,
    )

    assert answer["status"] == "awaiting_approval"
    assert answer["extras"]["error_code"] == "approval_expired"
    assert answer["extras"]["decision"] == "expired"


@pytest.mark.asyncio
async def test_an_approved_request_started_in_time_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def _dispatch(**kwargs: Any) -> OperationResult:
        calls.append(kwargs)
        return OperationResult(status="ok", op_id=kwargs["op_id"], result={}, duration_ms=1.0)

    monkeypatch.setattr(dispatcher_module, "dispatch", _dispatch)
    tenant_id = await _tenant()
    request_id = await _park(
        tenant_id, _SPEC_OP, _SPEC_PARAMS, decided_ago=_ttl() - timedelta(minutes=5)
    )

    assert await sweep_expired_approvals() == 0
    row = await _load(tenant_id, request_id)
    assert row.status == ApprovalRequestStatus.APPROVED.value

    result = await resume_dispatch_after_approval(operator=_requester(tenant_id), request=row)

    assert result.status == "ok"
    assert calls[0]["params"] == _SPEC_PARAMS


@pytest.mark.asyncio
async def test_a_started_approved_request_does_not_expire() -> None:
    tenant_id = await _tenant()
    request_id = await _park(tenant_id, _SPEC_OP, _SPEC_PARAMS, decided_ago=_past_ttl())
    assert await claim_resume(request_id) is True

    assert await sweep_expired_approvals() == 0
    row = await _load(tenant_id, request_id)
    assert row.status == ApprovalRequestStatus.APPROVED.value


# ---------------------------------------------------------------------------
# The start-up clean-up
# ---------------------------------------------------------------------------


@pytest.fixture
def running_release(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(approval_stored_values, "running_release_id", lambda: _RUNNING)
    return _RUNNING


async def _store_the_old_way(
    tenant_id: uuid.UUID, request_id: uuid.UUID, params: dict[str, Any]
) -> None:
    """Put *params* on the row in plain text, as an older release stored them."""
    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=tenant_id, request_id=request_id)
        row.params = params
        await s.commit()


@pytest.mark.asyncio
async def test_an_old_row_of_an_apply_of_a_secret_has_its_manifest_hidden(
    running_release: str,
) -> None:
    tenant_id = await _tenant()
    params = {"manifest": _SECRET_MANIFEST}
    request_id = await _park(tenant_id, _APPLY_OP, params, release_id="release-a")
    await _store_the_old_way(tenant_id, request_id, params)

    count = await hide_values_of_requests_that_cannot_run()

    row = await _load(tenant_id, request_id)
    assert row.params == {"manifest": _MARKER}
    assert row.status == ApprovalRequestStatus.EXPIRED.value
    assert await _handoff_rows(request_id) == 0
    assert count.params_hidden == 1
    assert count.handoffs_removed == 1
    assert count.approvals_ended == 1


@pytest.mark.asyncio
async def test_an_approved_row_of_another_release_ends_with_its_clean_up(
    running_release: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await _tenant()
    params = {"manifest": _SECRET_MANIFEST}
    request_id = await _park(tenant_id, _APPLY_OP, params, release_id="release-a")
    await _store_the_old_way(tenant_id, request_id, params)
    await _store_a_parent_the_old_way(tenant_id, request_id, {"admin_password": _PW, **params})

    await hide_values_of_requests_that_cannot_run()

    row = await _load(tenant_id, request_id)
    assert row.status == ApprovalRequestStatus.EXPIRED.value
    assert approval_expired_reason(row) is ApprovalExpiredReason.RELEASE_CHANGED
    async with get_sessionmaker()() as s:
        decisions = (
            await s.scalars(
                select(AuditLog).where(
                    AuditLog.path == "approval.decision", AuditLog.tenant_id == tenant_id
                )
            )
        ).all()
    assert [d.status_code for d in decisions] == [410]

    # A MEHO that still runs the release of the request cannot start it.
    monkeypatch.setattr(
        "meho_backplane.operations.approval_queue.running_release_id", lambda: "release-a"
    )
    assert await claim_resume(request_id) is False
    async with get_sessionmaker()() as s:
        assert await consume_remote_write_approval(s, row) is False
    after = await _load(tenant_id, request_id)
    assert after.resumed_at is None
    assert after.resume_parent == {"op_id": "example.deploy"}


async def _decisions(tenant_id: uuid.UUID) -> list[AuditLog]:
    async with get_sessionmaker()() as s:
        rows = await s.scalars(
            select(AuditLog).where(
                AuditLog.path == "approval.decision", AuditLog.tenant_id == tenant_id
            )
        )
        return list(rows)


@pytest.mark.asyncio
async def test_a_claim_of_an_older_release_after_the_clean_up_commit_is_refused(
    running_release: str,
) -> None:
    """The claim an older release writes: it checks only that ``resumed_at`` is empty."""
    tenant_id = await _tenant()
    request_id = await _park(tenant_id, _SPEC_OP, _SPEC_PARAMS, release_id="release-a")

    # The older release reads the approved row first ...
    seen = await _load(tenant_id, request_id)
    assert seen.status == ApprovalRequestStatus.APPROVED.value

    # ... the start-up clean-up of the new release ends it and commits ...
    await hide_values_of_requests_that_cannot_run()
    assert (await _load(tenant_id, request_id)).status == ApprovalRequestStatus.EXPIRED.value

    # ... and then the older release writes its claim.
    async with get_sessionmaker()() as s:
        with pytest.raises(IntegrityError):
            await s.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == seen.id, ApprovalRequest.resumed_at.is_(None))
                .values(resumed_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            await s.flush()
        await s.rollback()

    after = await _load(tenant_id, request_id)
    assert after.status == ApprovalRequestStatus.EXPIRED.value
    assert after.resumed_at is None


@pytest.mark.asyncio
async def test_an_overdue_approval_of_another_release_ends_with_one_reason(
    running_release: str,
) -> None:
    tenant_id = await _tenant()
    request_id = await _park(
        tenant_id,
        _SPEC_OP,
        _SPEC_PARAMS,
        decided_ago=_past_ttl(),
        release_id="release-a",
    )

    count = await hide_values_of_requests_that_cannot_run()

    row = await _load(tenant_id, request_id)
    assert count.approvals_ended == 1
    assert row.status == ApprovalRequestStatus.EXPIRED.value
    assert approval_expired_reason(row) is ApprovalExpiredReason.DEADLINE_PASSED
    decisions = await _decisions(tenant_id)
    assert [d.status_code for d in decisions] == [410]
    assert "reason" not in decisions[0].payload


@pytest.mark.asyncio
async def test_an_approval_of_another_release_in_its_time_ends_as_release_changed(
    running_release: str,
) -> None:
    tenant_id = await _tenant()
    request_id = await _park(tenant_id, _SPEC_OP, _SPEC_PARAMS, release_id="release-a")

    await hide_values_of_requests_that_cannot_run()

    row = await _load(tenant_id, request_id)
    assert approval_expired_reason(row) is ApprovalExpiredReason.RELEASE_CHANGED
    decisions = await _decisions(tenant_id)
    assert [d.payload["reason"] for d in decisions] == ["release_changed"]


async def _store_a_parent_the_old_way(
    tenant_id: uuid.UUID, request_id: uuid.UUID, params: dict[str, Any]
) -> None:
    """Put the params of a multi-step parent on the row, as an older release stored them."""
    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=tenant_id, request_id=request_id)
        row.resume_parent = {
            "op_id": "example.deploy",
            "params": params,
            "state": {"step": 2},
        }
        await s.commit()


@pytest.mark.asyncio
async def test_an_old_step_row_keeps_no_copy_in_its_parent(running_release: str) -> None:
    tenant_id = await _tenant()
    params = {"manifest": _SECRET_MANIFEST}
    request_id = await _park(tenant_id, _APPLY_OP, params, release_id="release-a")
    await _store_the_old_way(tenant_id, request_id, params)
    await _store_a_parent_the_old_way(tenant_id, request_id, {"admin_password": _PW, **params})

    count = await hide_values_of_requests_that_cannot_run()

    row = await _load(tenant_id, request_id)
    assert row.params == {"manifest": _MARKER}
    assert row.resume_parent == {"op_id": "example.deploy"}
    assert _PW not in json.dumps({"params": row.params, "parent": row.resume_parent})
    assert await _handoff_rows(request_id) == 0
    assert count.params_hidden == 1
    again = await hide_values_of_requests_that_cannot_run()
    assert again.params_hidden == 0


@pytest.mark.asyncio
async def test_a_step_row_that_expires_keeps_only_the_parent_name() -> None:
    tenant_id = await _tenant()
    request_id = await _park(tenant_id, _SPEC_OP, _SPEC_PARAMS, decided_ago=_past_ttl())
    await _store_a_parent_the_old_way(tenant_id, request_id, {"admin_password": _PW})

    await sweep_expired_approvals()

    row = await _load(tenant_id, request_id)
    assert row.status == ApprovalRequestStatus.EXPIRED.value
    assert row.resume_parent == {"op_id": "example.deploy"}


@pytest.mark.asyncio
async def test_the_clean_up_covers_ended_rows_and_leaves_rows_that_can_run(
    running_release: str,
) -> None:
    tenant_id = await _tenant()
    rejected = await _park(tenant_id, _SPEC_OP, _SPEC_PARAMS, status=ApprovalRequestStatus.REJECTED)
    await _store_the_old_way(tenant_id, rejected, _SPEC_PARAMS)
    waiting = await _park(tenant_id, _SPEC_OP, _SPEC_PARAMS, status=ApprovalRequestStatus.PENDING)
    approved_now = await _park(tenant_id, _SPEC_OP, _SPEC_PARAMS, release_id=running_release)
    started = await _park(tenant_id, _SPEC_OP, _SPEC_PARAMS, release_id="release-a")
    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=tenant_id, request_id=started)
        row.resumed_at = datetime.now(UTC)
        await s.commit()

    await hide_values_of_requests_that_cannot_run()

    assert (await _load(tenant_id, rejected)).params == {
        "spec": {"name": "lab-1", "rootPassword": _MARKER}
    }
    for request_id in (waiting, approved_now, started):
        assert (await _load(tenant_id, request_id)).params == _SPEC_PARAMS


@pytest.mark.asyncio
async def test_without_a_release_identity_no_approved_row_is_cleaned() -> None:
    tenant_id = await _tenant()
    request_id = await _park(tenant_id, _SPEC_OP, _SPEC_PARAMS, release_id="release-a")

    await hide_values_of_requests_that_cannot_run()

    assert (await _load(tenant_id, request_id)).params == _SPEC_PARAMS


@pytest.mark.asyncio
async def test_a_second_clean_up_writes_nothing(running_release: str) -> None:
    tenant_id = await _tenant()
    request_id = await _park(
        tenant_id, _SPEC_OP, _SPEC_PARAMS, status=ApprovalRequestStatus.REJECTED
    )
    await _store_the_old_way(tenant_id, request_id, _SPEC_PARAMS)

    first = await hide_values_of_requests_that_cannot_run()
    second = await hide_values_of_requests_that_cannot_run()

    assert first.params_hidden == 1
    assert second.params_hidden == 0
    assert second.requests_read == 1


@pytest.mark.asyncio
async def test_the_clean_up_of_many_rows_runs_in_batches(
    running_release: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await _tenant()
    total = 2 * CLEAN_UP_BATCH_SIZE + 137
    params_hash = compute_params_hash(_SPEC_PARAMS)
    async with get_sessionmaker()() as s:
        s.add_all(
            ApprovalRequest(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                principal_sub=_REQUESTER_SUB,
                op_id=_SPEC_OP,
                connector_id="example-1.x",
                params_hash=params_hash,
                params=_SPEC_PARAMS,
                status=ApprovalRequestStatus.REJECTED.value,
            )
            for _ in range(total)
        )
        await s.commit()

    sizes: list[int] = []
    real_batch = approval_stored_values._clean_batch

    async def _recording_batch(
        session: Any, rows: list[ApprovalRequest], count: Any, **kwargs: Any
    ) -> list[ApprovalRequest]:
        sizes.append(len(rows))
        return await real_batch(session, rows, count, **kwargs)

    monkeypatch.setattr(approval_stored_values, "_clean_batch", _recording_batch)

    count = await hide_values_of_requests_that_cannot_run()

    assert sizes == [CLEAN_UP_BATCH_SIZE, CLEAN_UP_BATCH_SIZE, 137]
    assert count.batches == 3
    assert count.requests_read == total
    assert count.params_hidden == total
    async with get_sessionmaker()() as s:
        stored = (
            await s.scalars(
                select(ApprovalRequest.params).where(ApprovalRequest.tenant_id == tenant_id)
            )
        ).all()
    assert len(stored) == total
    assert all(_PW not in json.dumps(params) for params in stored)
