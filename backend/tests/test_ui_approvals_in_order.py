# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the console "Approve group in order" action (#3597).

``GET`` / ``POST /ui/approvals/in-order`` approve the pending requests of one
change ticket oldest first. Caution steps go on by themselves, a dangerous or
destructive step waits for a per-step yes (``confirm_id``), and the run stops
at the first step whose run fails or whose approval is refused. The steps
after a stop stay pending. A second run skips only the steps that were
approved and ran OK.

The harness (app, session, CSRF, the patched operator) is the one
``test_ui_approvals.py`` uses. The run of an approved op is replaced by a
stand-in that stores a result on the row the way the real resume does, so
a second run can read it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import respx
from sqlalchemy import update

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from meho_backplane.ui.routes.approvals.in_order import group_warnings
from tests.test_ui_approvals import (
    _RESOLVE_OPERATOR,
    _REVIEWER_SUB,
    _TENANT_A,
    _authenticated_client,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _csrf_token,
    _operator,
    _request_status,
    _seed_session_sync,
    _seed_tenant,
)

_RESUME = "meho_backplane.ui.routes.approvals.in_order.resume_dispatch_after_approval"
_REF = "gh:example/repo#7"
_REF_Q = quote(_REF, safe="")
_BASE = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)


def _seed(
    step: int,
    level: str,
    *,
    status_value: str = ApprovalRequestStatus.PENDING.value,
    resume_result: dict[str, Any] | None = None,
    principal_sub: str = "agent-7",
    created_at: datetime | None = None,
) -> uuid.UUID:
    """Insert step *step* of change ticket ``_REF`` at risk level *level*."""
    rid = uuid.uuid4()

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                ApprovalRequest(
                    id=rid,
                    tenant_id=_TENANT_A,
                    run_id=None,
                    principal_sub=principal_sub,
                    op_id=f"op.step{step}",
                    connector_id="linux",
                    target_id=None,
                    params_hash="0" * 64,
                    params={"path": "/etc/example"},
                    proposed_effect={"op_id": f"op.step{step}", "safety_level": level},
                    preview_hash="1" * 64 if level == "destructive" else None,
                    status=status_value,
                    work_ref=_REF,
                    created_at=created_at or _BASE + timedelta(minutes=step),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    resumed_at=_BASE if resume_result is not None else None,
                    resume_result=resume_result,
                    decided_at=_BASE if status_value != "pending" else None,
                    reviewed_by="op-1" if status_value != "pending" else None,
                )
            )

    asyncio.run(_do())
    return rid


def _fake_resume(failing_ops: set[str]) -> AsyncMock:
    """Stand in for the run of an approved op; store its result on the row."""

    async def _run(*, operator: Any, request: ApprovalRequest, params: Any) -> SimpleNamespace:
        status = "error" if request.op_id in failing_ops else "ok"
        async with get_sessionmaker()() as session, session.begin():
            await session.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == request.id)
                .values(resumed_at=datetime.now(UTC), resume_result={"status": status})
            )
        error = "connector_error: file not found" if status == "error" else None
        return SimpleNamespace(status=status, op_id=request.op_id, result={}, error=error)

    return AsyncMock(side_effect=_run)


def _post(session_id: uuid.UUID, resume: AsyncMock, **form: str) -> Any:
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RESUME, resume),
        ):
            return client.post(
                "/ui/approvals/in-order",
                data={"work_ref": _REF, "reason": "change window", **form},
                headers={CSRF_HEADER_NAME: csrf},
            )


def _get(session_id: uuid.UUID, path: str) -> Any:
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        return client.get(path)


def _session() -> uuid.UUID:
    _seed_tenant(_TENANT_A, "tenant-a")
    return _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_REVIEWER_SUB)


def test_history_offers_in_order_button_only_for_a_ticket_with_pending_steps() -> None:
    session_id = _session()
    _seed(1, "caution")
    _seed(2, "caution")

    filtered = _get(session_id, f"/ui/approvals/list?tab=pending&work_ref={_REF_Q}")
    unfiltered = _get(session_id, "/ui/approvals/list?tab=pending")
    other_tab = _get(session_id, f"/ui/approvals/list?tab=all&work_ref={_REF_Q}")

    assert filtered.status_code == 200
    assert "data-approval-in-order-open" in filtered.text
    assert "/ui/approvals/in-order?work_ref=gh%3Aexample/repo%237" in filtered.text
    assert "data-approval-in-order-open" not in unfiltered.text
    assert "data-approval-in-order-open" not in other_tab.text


def test_plan_lists_steps_oldest_first_with_a_start_form() -> None:
    session_id = _session()
    second = _seed(2, "dangerous")
    first = _seed(1, "caution")

    response = _get(session_id, f"/ui/approvals/in-order?work_ref={_REF_Q}")

    assert response.status_code == 200, response.text
    text = response.text
    assert text.index("op.step1") < text.index("op.step2")
    assert 'data-confirm="in-order-start"' in text
    assert response.cookies.get(CSRF_COOKIE_NAME)
    # Nothing ran: both rows still wait.
    assert _request_status(first) == "pending"
    assert _request_status(second) == "pending"


def test_mixed_group_goes_on_waits_at_risky_steps_and_stops_at_the_failure() -> None:
    session_id = _session()
    s1 = _seed(1, "caution")
    s2 = _seed(2, "dangerous")
    s3 = _seed(3, "destructive")
    s4 = _seed(4, "caution")
    s5 = _seed(5, "caution")
    resume = _fake_resume(failing_ops={"op.step4"})

    # Start: step 1 (caution) runs, the run waits at step 2 (dangerous).
    first = _post(session_id, resume)
    assert first.status_code == 200, first.text
    assert 'data-approval-in-order-waiting="2"' in first.text
    assert f'name="confirm_id" value="{s2}"' in first.text
    assert first.headers.get("HX-Trigger") == "meho:approval-bump"
    assert first.cookies.get(CSRF_COOKIE_NAME)
    assert [_request_status(r) for r in (s1, s2, s3, s4, s5)] == [
        "approved",
        "pending",
        "pending",
        "pending",
        "pending",
    ]

    # Yes for step 2: step 1 is skipped (ran OK), step 2 runs, the run
    # waits at step 3 (destructive).
    second = _post(session_id, resume, confirm_id=str(s2))
    assert 'data-approval-in-order-waiting="3"' in second.text
    assert "ran OK before, skipped" in second.text
    assert [_request_status(r) for r in (s1, s2, s3)] == ["approved", "approved", "pending"]

    # Yes for step 3: step 3 runs, step 4 runs and fails, the run stops;
    # step 5 stays pending.
    third = _post(session_id, resume, confirm_id=str(s3))
    assert 'data-approval-in-order-stopped="4"' in third.text
    assert "connector_error: file not found" in third.text
    assert "Not run: step" in third.text
    assert [_request_status(r) for r in (s3, s4, s5)] == ["approved", "approved", "pending"]
    assert [call.kwargs["request"].op_id for call in resume.await_args_list] == [
        "op.step1",
        "op.step2",
        "op.step3",
        "op.step4",
    ]

    # A second run stops at the failed step again and never decides step 5.
    again = _post(session_id, resume)
    assert 'data-approval-in-order-stopped="4"' in again.text
    assert "send it again" in again.text
    assert _request_status(s5) == "pending"
    assert resume.await_count == 4


def test_a_yes_for_another_request_does_not_approve_the_risky_step() -> None:
    session_id = _session()
    s1 = _seed(1, "dangerous")
    resume = _fake_resume(failing_ops=set())

    response = _post(session_id, resume, confirm_id=str(uuid.uuid4()))

    assert 'data-approval-in-order-waiting="1"' in response.text
    assert _request_status(s1) == "pending"
    resume.assert_not_awaited()


def test_self_approval_refusal_stops_the_run() -> None:
    session_id = _session()
    s1 = _seed(1, "caution", principal_sub=_REVIEWER_SUB)
    s2 = _seed(2, "caution")
    resume = _fake_resume(failing_ops=set())

    response = _post(session_id, resume)

    assert 'data-approval-in-order-stopped="1"' in response.text
    assert "The approval was refused" in response.text
    assert _request_status(s1) == "pending"
    assert _request_status(s2) == "pending"
    resume.assert_not_awaited()


def test_rerun_skips_only_steps_that_ran_ok() -> None:
    session_id = _session()
    _seed(1, "caution", status_value="approved", resume_result={"status": "ok"})
    _seed(2, "caution", status_value="approved", resume_result={"status": "error"})
    s3 = _seed(3, "caution")
    resume = _fake_resume(failing_ops=set())

    response = _post(session_id, resume)

    assert 'data-approval-in-order-stopped="2"' in response.text
    assert "ran OK before, skipped" in response.text
    assert _request_status(s3) == "pending"
    resume.assert_not_awaited()


def test_rejected_predecessor_stops_the_run() -> None:
    session_id = _session()
    _seed(1, "caution", status_value="rejected")
    s2 = _seed(2, "caution")
    resume = _fake_resume(failing_ops=set())

    response = _post(session_id, resume)

    assert 'data-approval-in-order-stopped="1"' in response.text
    assert _request_status(s2) == "pending"


def test_run_without_csrf_token_is_refused() -> None:
    session_id = _session()
    s1 = _seed(1, "caution")
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post("/ui/approvals/in-order", data={"work_ref": _REF})

    assert response.status_code == 403
    assert _request_status(s1) == "pending"


def test_group_warnings_name_mixed_requesters_and_equal_times() -> None:
    rows = [
        ApprovalRequest(principal_sub="alice", created_at=_BASE),
        ApprovalRequest(principal_sub="bob", created_at=_BASE),
    ]

    warnings = group_warnings(rows)

    assert any("2 requesters (alice, bob)" in w for w in warnings)
    assert any("Steps 1 and 2 were created at the same time" in w for w in warnings)
    assert group_warnings(rows[:1]) == []
