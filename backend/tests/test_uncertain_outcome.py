# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "uncertain" outcome of an approved change (#5025).

MEHO shows ``uncertain`` when it does not know if an approved change
happened: a lost vendor answer after the request was sent, a failed audit
write, or a restart in the middle of an approved run. These tests pin the
shared rule, the transport-fault rule, the summary, and the start-up
recovery. The console, reaper and dispatcher have their own tests next to
the code they cover.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    AuditLog,
    Tenant,
)
from meho_backplane.operations._errors import result_audit_commit_failed, result_connector_timeout
from meho_backplane.operations.approval_outcome import resume_outcome, resume_outcome_from_envelope
from meho_backplane.operations.approval_queue import (
    ApprovalResultState,
    approval_result_state,
    result_state_of_envelope,
)
from meho_backplane.operations.approval_rerun import _not_failed_state
from meho_backplane.operations.approval_restart_recovery import recover_interrupted_approvals
from meho_backplane.operations.uncertain_outcome import (
    UNCERTAIN_ADVICE,
    changes_target,
    envelope_is_uncertain,
    request_may_have_reached_vendor,
    result_interrupted_by_restart,
)
from meho_backplane.settings import get_settings

_REQUEST = httpx.Request("POST", "https://vendor.test/api/items")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# The one rule every view shares
# ---------------------------------------------------------------------------


def test_envelope_rule_tells_ok_uncertain_and_failed_apart() -> None:
    assert result_state_of_envelope({"status": "ok"}) is ApprovalResultState.EXECUTED
    assert (
        result_state_of_envelope({"status": "error", "extras": {"error_code": "connector_error"}})
        is ApprovalResultState.FAILED
    )
    assert (
        result_state_of_envelope({"status": "error", "extras": {"execution_outcome": "uncertain"}})
        is ApprovalResultState.UNCERTAIN
    )


@pytest.mark.parametrize(
    "envelope",
    [
        None,
        {},
        {"status": "error"},
        {"status": "error", "extras": None},
        {"status": "error", "extras": "uncertain"},
        {"status": "error", "extras": {"execution_outcome": "certain"}},
        # An ok result never reads as uncertain, whatever its extras say.
        {"status": "ok", "extras": {"execution_outcome": "uncertain"}},
    ],
)
def test_odd_envelopes_are_not_uncertain(envelope: dict[str, Any] | None) -> None:
    assert envelope_is_uncertain(envelope) is False


def test_audit_commit_failed_result_is_uncertain_in_every_view() -> None:
    envelope = result_audit_commit_failed("vm.create", 5.0).model_dump(mode="json")
    assert result_state_of_envelope(envelope) is ApprovalResultState.UNCERTAIN
    summary = resume_outcome_from_envelope(envelope)
    assert summary.execution_outcome == "uncertain"
    assert summary.error_code == "audit_commit_failed"
    assert summary.guidance == UNCERTAIN_ADVICE


def test_every_uncertain_summary_carries_the_advice() -> None:
    """A timeout, a failed audit write and a restart store no guidance stamp.

    Their summary still says what to do: the fixed advice. The full result's
    ``remediation`` (owner-only) may say more.
    """
    timeout = result_connector_timeout(
        "vm.create", httpx.ReadTimeout("x", request=_REQUEST), 1.0, changes_target=True
    )
    for result in (
        timeout,
        result_audit_commit_failed("vm.create", 5.0),
        result_interrupted_by_restart("vm.create"),
    ):
        envelope = result.model_dump(mode="json")
        assert "approval_guidance" not in envelope["extras"]
        summary = resume_outcome_from_envelope(envelope)
        assert summary.execution_outcome == "uncertain"
        assert summary.guidance == UNCERTAIN_ADVICE
        assert "Do not run it again" in envelope["extras"]["remediation"]


def test_uncertain_summary_keeps_a_stored_guidance_stamp() -> None:
    envelope = {
        "status": "error",
        "extras": {
            "error_code": "result_unknown",
            "execution_outcome": "uncertain",
            "approval_guidance": "The VM waits on question _vmx1.",
        },
    }
    summary = resume_outcome_from_envelope(envelope)
    assert summary.execution_outcome == "uncertain"
    assert summary.guidance == "The VM waits on question _vmx1."


def test_real_error_summary_has_no_execution_outcome() -> None:
    summary = resume_outcome_from_envelope(
        {"status": "error", "extras": {"error_code": "connector_error"}}
    )
    assert summary.execution_outcome is None
    assert summary.guidance is None


def test_uncertain_run_is_not_offered_for_a_re_run() -> None:
    """Only a failed run can be re-run, so an uncertain one is refused."""
    request = ApprovalRequest(
        status=ApprovalRequestStatus.APPROVED.value,
        resumed_at=datetime.now(UTC),
        resume_result=result_interrupted_by_restart("vm.create").model_dump(mode="json"),
    )
    assert approval_result_state(request) is ApprovalResultState.UNCERTAIN
    assert _not_failed_state(request) == "uncertain"


# ---------------------------------------------------------------------------
# A timeout after sending
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("x", request=_REQUEST),
        httpx.WriteTimeout("x", request=_REQUEST),
        httpx.ReadError("x", request=_REQUEST),
        httpx.RemoteProtocolError("x", request=_REQUEST),
    ],
)
def test_change_that_may_have_been_sent_is_uncertain(exc: httpx.TransportError) -> None:
    assert request_may_have_reached_vendor(exc)
    result = result_connector_timeout("vm.create", exc, 1.0, changes_target=True)
    assert result.status == "error"
    assert result.extras["error_code"] == "connector_timeout"
    assert result.extras["execution_outcome"] == "uncertain"
    assert result.extras["retry"] is False
    assert UNCERTAIN_ADVICE in (result.error or "")
    assert envelope_is_uncertain(result.model_dump(mode="json"))


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("x", request=_REQUEST),
        httpx.PoolTimeout("x", request=_REQUEST),
        httpx.ConnectError("x", request=_REQUEST),
        httpx.NetworkError("x", request=_REQUEST),
    ],
)
def test_change_that_was_not_sent_stays_a_plain_error(exc: httpx.TransportError) -> None:
    assert not request_may_have_reached_vendor(exc)
    result = result_connector_timeout("vm.create", exc, 1.0, changes_target=True)
    assert "execution_outcome" not in result.extras
    assert "retry" not in result.extras
    assert result.error == f"connector_timeout: {type(exc).__name__}"


def test_read_operation_timeout_stays_a_plain_error() -> None:
    result = result_connector_timeout(
        "vm.list", httpx.ReadTimeout("x", request=_REQUEST), 1.0, changes_target=False
    )
    assert "execution_outcome" not in result.extras
    assert result.error == "connector_timeout: ReadTimeout"
    # The default is a read, so a caller that says nothing keeps the old answer.
    default = result_connector_timeout("vm.list", httpx.ReadTimeout("x", request=_REQUEST), 1.0)
    assert default.extras == result.extras


@pytest.mark.parametrize(
    ("level", "expected"),
    [("safe", False), ("caution", True), ("dangerous", True), ("destructive", True), (None, True)],
)
def test_only_safe_is_a_read(level: str | None, expected: bool) -> None:
    assert changes_target(level) is expected


# ---------------------------------------------------------------------------
# After a restart
# ---------------------------------------------------------------------------

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000005025")


async def _seed_request(
    *,
    status: str = ApprovalRequestStatus.APPROVED.value,
    resumed_at: datetime | None,
    resume_result: dict[str, Any] | None = None,
    op_id: str = "vm.create",
) -> uuid.UUID:
    request_id = uuid.uuid4()
    # Leave the column unset for "no result": a Python None would be stored as JSON null.
    result_kwargs = {} if resume_result is None else {"resume_result": resume_result}
    async with get_sessionmaker()() as session, session.begin():
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="uncertain-5025", name="Uncertain 5025"))
            await session.flush()
        session.add(
            ApprovalRequest(
                id=request_id,
                tenant_id=_TENANT,
                run_id=None,
                principal_sub="requester",
                op_id=op_id,
                connector_id="vsphere-1.x",
                target_id=None,
                params_hash="0" * 64,
                params={"flavor": "small"},
                proposed_effect={"op_id": op_id},
                status=status,
                resumed_at=resumed_at,
                expires_at=datetime.now(UTC),
                **result_kwargs,
            )
        )
    return request_id


async def _load(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_started_request_with_no_result_becomes_uncertain_after_a_restart() -> None:
    started = await _seed_request(resumed_at=datetime.now(UTC))

    assert approval_result_state(await _load(started)) is ApprovalResultState.STARTED
    assert await recover_interrupted_approvals() >= 1

    row = await _load(started)
    assert approval_result_state(row) is ApprovalResultState.UNCERTAIN
    assert row.resume_result is not None
    assert row.resume_result["extras"]["error_code"] == "interrupted_by_restart"
    assert row.resume_result["extras"]["retry"] is False
    outcome = resume_outcome(row)
    assert outcome is not None and outcome.execution_outcome == "uncertain"
    # One audit row, linked from the stored result.
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "approval.recovery")))
            .scalars()
            .all()
        )
    mine = [r for r in rows if r.payload.get("approval_request_id") == str(started)]
    assert len(mine) == 1
    assert mine[0].payload["execution_outcome"] == "uncertain"
    assert str(mine[0].id) == row.resume_result["audit_id"]


@pytest.mark.asyncio
async def test_recovery_never_runs_the_operation_and_leaves_other_requests_alone() -> None:
    from unittest.mock import AsyncMock, patch

    started = await _seed_request(resumed_at=datetime.now(UTC))
    not_started = await _seed_request(resumed_at=None)
    ran_ok = await _seed_request(resumed_at=datetime.now(UTC), resume_result={"status": "ok"})
    pending = await _seed_request(
        status=ApprovalRequestStatus.PENDING.value, resumed_at=datetime.now(UTC)
    )

    with patch(
        "meho_backplane.operations.approval_queue.resume_dispatch_after_approval",
        new_callable=AsyncMock,
    ) as resume:
        await recover_interrupted_approvals()
        resume.assert_not_called()

    assert approval_result_state(await _load(started)) is ApprovalResultState.UNCERTAIN
    assert approval_result_state(await _load(not_started)) is ApprovalResultState.NOT_STARTED
    assert approval_result_state(await _load(ran_ok)) is ApprovalResultState.EXECUTED
    assert (await _load(pending)).resume_result is None


@pytest.mark.asyncio
async def test_recovery_is_safe_to_run_twice() -> None:
    started = await _seed_request(resumed_at=datetime.now(UTC))
    await recover_interrupted_approvals()
    first = (await _load(started)).resume_result

    await recover_interrupted_approvals()

    assert (await _load(started)).resume_result == first
