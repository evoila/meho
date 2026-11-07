# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the console "Approve group in order" action (#3597).

``GET`` / ``POST /ui/approvals/in-order`` approve the pending requests of one
change ticket oldest first. Caution steps go on by themselves, a dangerous or
destructive step waits for a per-step yes (``confirm_id``), and the run stops
at the first step whose run fails or whose approval is refused. The steps
after a stop stay pending. A second run skips only the steps that were
approved and ran OK. The run approves only the steps the form listed
(``step_id``): a pending request that joined the ticket later stops it
before anything is approved.

The harness (app, session, CSRF, the patched operator) is the one
``test_ui_approvals.py`` uses. The run of an approved op is replaced by a
stand-in that stores a result on the row the way the real resume does, so
a second run can read it. A second stand-in plays the waiting requester:
it answers ``already_resumed`` and stores the result a little later, the
way the in-process agent waiter does.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import respx
from sqlalchemy import select, update

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations.approval_rights import WEB_APPROVAL_HINT
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from meho_backplane.ui.routes.approvals import in_order
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


async def _store_result(request_id: uuid.UUID, status: str) -> None:
    async with get_sessionmaker()() as session, session.begin():
        error = "connector_error: disk full" if status != "ok" else None
        await session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == request_id)
            .values(resume_result={"status": status, "error": error})
        )


def _fake_other_caller(results: dict[str, str | None], *, after: float = 0.2) -> AsyncMock:
    """Stand in for a run another caller claimed (``already_resumed``).

    Marks the row as claimed and answers at once. The result named in
    *results* for the op (``None`` for none) lands on the row *after*
    seconds later, from a background task, like the agent waiter's write.
    """
    tasks: list[asyncio.Task[None]] = []

    async def _run(*, operator: Any, request: ApprovalRequest, params: Any) -> SimpleNamespace:
        async with get_sessionmaker()() as session, session.begin():
            await session.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == request.id)
                .values(resumed_at=datetime.now(UTC))
            )
        status = results.get(request.op_id)
        if status is not None:

            async def _later() -> None:
                await asyncio.sleep(after)
                await _store_result(request.id, status)

            tasks.append(asyncio.get_running_loop().create_task(_later()))
        return SimpleNamespace(status="already_resumed", op_id=request.op_id, result={}, error=None)

    return AsyncMock(side_effect=_run)


def _post(
    session_id: uuid.UUID,
    resume: AsyncMock,
    *,
    sid: str = "kc-sid-1",
    sign_in_age: int | None = 0,
    auth_time: int | None = None,
    operator: Operator | None = None,
    step_ids: list[str] | None = None,
    **form: str,
) -> Any:
    """POST a run as the reviewer.

    *sid* and *auth_time* name the sign-in behind the session: two posts
    with the same pair come from one sign-in. *auth_time* pins the time;
    without it the sign-in is from now minus *sign_in_age* seconds.
    *operator* replaces the default reviewer (an admin). *step_ids* are the
    ids the form lists; by default every request of the ticket now, as if
    the modal had just been rendered.
    """
    if step_ids is None:
        step_ids = _ticket_ids()
    csrf = _csrf_token(session_id)
    # An admin, so the reviewer may approve the dangerous and destructive
    # steps of another person too (#3328).
    operator = operator or _operator(
        tenant_id=_TENANT_A,
        sub=_REVIEWER_SUB,
        role=TenantRole.TENANT_ADMIN,
        sid=sid,
        sign_in_age=sign_in_age,
    )
    if auth_time is not None:
        operator = operator.model_copy(update={"auth_time": auth_time})
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RESUME, resume),
        ):
            return client.post(
                "/ui/approvals/in-order",
                data={"work_ref": _REF, "reason": "change window", "step_id": step_ids, **form},
                headers={CSRF_HEADER_NAME: csrf},
            )


def _ticket_ids() -> list[str]:
    """The ids of every request of change ticket ``_REF``."""

    async def _read() -> list[str]:
        async with get_sessionmaker()() as session:
            rows = await session.scalars(
                select(ApprovalRequest.id).where(ApprovalRequest.work_ref == _REF)
            )
            return [str(rid) for rid in rows]

    return asyncio.run(_read())


def _form_step_ids(body: str) -> list[str]:
    """The ``step_id`` values of the rendered form."""
    marker = 'name="step_id" value="'
    return [part.split('"', 1)[0] for part in body.split(marker)[1:]]


def _get(session_id: uuid.UUID, path: str) -> Any:
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        return client.get(path)


def _plan(session_id: uuid.UUID, operator: Operator | None = None) -> Any:
    """Open the in-order modal of ``_REF`` as *operator* (default: an admin)."""
    operator = operator or _operator(
        tenant_id=_TENANT_A, sub=_REVIEWER_SUB, role=TenantRole.TENANT_ADMIN
    )
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            return client.get(f"/ui/approvals/in-order?work_ref={_REF_Q}")


def _sa_operator() -> Operator:
    """A person in the MEHO-SA-Operators group, with the operator role."""
    return _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB).model_copy(
        update={"self_approver": True}
    )


def _step_row(body: str, step: int) -> str:
    """The rendered row of step *step*."""
    return body.split(f'data-approval-in-order-step="{step}"')[1].split("</li>")[0]


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

    response = _plan(session_id)

    assert response.status_code == 200, response.text
    text = response.text
    assert text.index("op.step1") < text.index("op.step2")
    assert 'data-confirm="in-order-start"' in text
    assert response.cookies.get(CSRF_COOKIE_NAME)
    # Nothing ran: both rows still wait.
    assert _request_status(first) == "pending"
    assert _request_status(second) == "pending"


def test_plan_shows_for_each_pending_step_whether_the_person_may_approve_it() -> None:
    """The sign of the request window, per step (#3329): yes, own change, no."""
    session_id = _session()
    _seed(1, "caution", principal_sub=_REVIEWER_SUB)
    _seed(2, "caution")
    _seed(3, "destructive")
    _seed(4, "caution", status_value="approved", resume_result={"status": "ok"})

    body = _plan(session_id, _sa_operator()).text

    own = _step_row(body, 1)
    assert 'data-approve-right="yes"' in own
    assert 'data-approve-reason="own_change"' in own
    assert "You may approve it as a MEHO-SA-Operator." in own
    other = _step_row(body, 2)
    assert 'data-approve-right="yes"' in other
    assert 'data-approve-reason="ok"' in other
    assert 'data-approve-right="yes"' in _step_row(body, 3)
    # A decided step has no sign.
    assert "data-approve-right" not in _step_row(body, 4)
    assert "data-approval-in-order-refused" not in body
    assert 'data-confirm="in-order-start"' in body


def test_plan_says_where_the_run_will_stop_for_a_step_the_person_may_not_approve() -> None:
    """An operator outside the groups may not approve another person's destructive step."""
    session_id = _session()
    _seed(1, "caution")
    _seed(2, "destructive")

    body = _plan(session_id, _operator(tenant_id=_TENANT_A)).text

    assert 'data-approve-right="yes"' in _step_row(body, 1)
    refused = _step_row(body, 2)
    assert 'data-approve-right="no"' in refused
    assert 'data-approve-reason="wrong_group"' in refused
    assert "MEHO-SA-Operator or a MEHO-Admin" in refused
    assert 'data-approval-in-order-refused="2"' in body
    assert "You cannot approve step 2 of 2 (op.step2). The run will stop there." in body
    # Step 1 can still go: the Start button stays.
    assert 'data-confirm="in-order-start"' in body


_SIGN_IN_URL = (
    "/ui/auth/login?return_to=%2Fui%2Fapprovals%3Ftab%3Dpending%26work_ref%3D"
    + quote(_REF_Q, safe="")
    + "&amp;fresh_sign_in=1"
)


def test_mixed_group_goes_on_waits_at_risky_steps_and_stops_at_the_failure() -> None:
    """Caution steps go on, risky steps wait, a destructive step needs its own sign-in.

    One sign-in approves steps 1 and 2. Step 3 is destructive, so that
    sign-in is refused there and the run links to a new sign-in. The new
    sign-in approves step 3 and is then used up, so step 4 needs a third
    sign-in. Step 4 runs and fails, and the run stops; step 5 stays
    pending.
    """
    session_id = _session()
    s1 = _seed(1, "caution")
    s2 = _seed(2, "dangerous")
    s3 = _seed(3, "destructive")
    s4 = _seed(4, "caution")
    s5 = _seed(5, "caution")
    resume = _fake_resume(failing_ops={"op.step4"})
    signed_in = int(time.time()) - 20

    # Start: step 1 (caution) runs, the run waits at step 2 (dangerous).
    first = _post(session_id, resume, auth_time=signed_in)
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
    second = _post(session_id, resume, auth_time=signed_in, confirm_id=str(s2))
    assert 'data-approval-in-order-waiting="3"' in second.text
    assert "ran OK before, skipped" in second.text
    assert [_request_status(r) for r in (s1, s2, s3)] == ["approved", "approved", "pending"]

    # Yes for step 3 with the same sign-in: step 3 is destructive and this
    # sign-in already approved steps 1 and 2, so the run stops and asks
    # for a new sign-in. Nothing ran.
    third = _post(session_id, resume, auth_time=signed_in, confirm_id=str(s3))
    assert 'data-approval-in-order-stopped="3"' in third.text
    assert "needs a new sign-in" in third.text
    assert f'href="{_SIGN_IN_URL}"' in third.text
    assert _request_status(s3) == "pending"
    assert resume.await_count == 2

    # A new sign-in, yes for step 3: step 3 runs. That sign-in approved a
    # destructive step, so step 4 needs yet another sign-in.
    fourth = _post(session_id, resume, sid="kc-sid-2", confirm_id=str(s3))
    assert 'data-approval-in-order-stopped="4"' in fourth.text
    assert f'href="{_SIGN_IN_URL}"' in fourth.text
    assert [_request_status(r) for r in (s3, s4)] == ["approved", "pending"]

    # A third sign-in: steps 1 to 3 are skipped, step 4 runs and fails, the
    # run stops; step 5 stays pending.
    fifth = _post(session_id, resume, sid="kc-sid-3")
    assert 'data-approval-in-order-stopped="4"' in fifth.text
    assert "connector_error: file not found" in fifth.text
    assert 'data-action="sign-in-to-approve"' not in fifth.text
    assert "Not run: step" in fifth.text
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


def test_other_caller_claims_the_run_and_it_fails_stops_the_run() -> None:
    """``already_resumed`` is not success: the run waits for the stored result."""
    session_id = _session()
    s1 = _seed(1, "caution")
    s2 = _seed(2, "caution")
    resume = _fake_other_caller({"op.step1": "error"})

    with patch.object(in_order, "_RESULT_POLL_SECONDS", 0.02):
        response = _post(session_id, resume)

    assert 'data-approval-in-order-stopped="1"' in response.text
    assert "The waiting requester ran it" in response.text
    assert "connector_error: disk full" in response.text
    assert _request_status(s1) == "approved"
    assert _request_status(s2) == "pending"
    assert resume.await_count == 1


def test_other_caller_claims_the_run_and_it_ends_ok_goes_on() -> None:
    session_id = _session()
    s1 = _seed(1, "caution")
    s2 = _seed(2, "caution")
    resume = _fake_other_caller({"op.step1": "ok", "op.step2": "ok"})

    with patch.object(in_order, "_RESULT_POLL_SECONDS", 0.02):
        response = _post(session_id, resume)

    assert "data-approval-in-order-done" in response.text
    assert [_request_status(r) for r in (s1, s2)] == ["approved", "approved"]
    assert [call.kwargs["request"].op_id for call in resume.await_args_list] == [
        "op.step1",
        "op.step2",
    ]


def test_no_stored_result_in_time_stops_the_run_and_the_rerun() -> None:
    """An unknown outcome stops the run; a rerun stops at the same step."""
    session_id = _session()
    s1 = _seed(1, "caution")
    s2 = _seed(2, "caution")
    resume = _fake_other_caller({"op.step1": None})

    with (
        patch.object(in_order, "_RESULT_POLL_SECONDS", 0.02),
        patch.object(in_order, "_RESULT_WAIT_SECONDS", 0.1),
    ):
        first = _post(session_id, resume)
        again = _post(session_id, resume)

    assert 'data-approval-in-order-stopped="1"' in first.text
    assert "has not ended yet" in first.text
    assert 'data-approval-in-order-stopped="1"' in again.text
    assert "has not ended yet" in again.text
    assert "send it again" not in again.text
    assert _request_status(s1) == "approved"
    assert _request_status(s2) == "pending"
    assert resume.await_count == 1


def test_group_larger_than_one_page_is_read_completely() -> None:
    """A change ticket with more requests than one page holds is read whole."""
    session_id = _session()
    page = in_order._PAGE
    last = uuid.uuid4()

    async def _seed_many() -> None:
        async with get_sessionmaker()() as session, session.begin():
            for n in range(1, page + 2):
                done = n <= page
                session.add(
                    ApprovalRequest(
                        id=last if n == page + 1 else uuid.uuid4(),
                        tenant_id=_TENANT_A,
                        run_id=None,
                        principal_sub="agent-7",
                        op_id=f"op.step{n}",
                        connector_id="linux",
                        target_id=None,
                        params_hash="0" * 64,
                        params={"path": "/etc/example"},
                        proposed_effect={"op_id": f"op.step{n}", "safety_level": "caution"},
                        status="approved" if done else "pending",
                        work_ref=_REF,
                        created_at=_BASE + timedelta(seconds=n),
                        expires_at=datetime.now(UTC) + timedelta(days=1),
                        resumed_at=_BASE if done else None,
                        resume_result={"status": "ok"} if done else None,
                        decided_at=_BASE if done else None,
                        reviewed_by="op-1" if done else None,
                    )
                )

    asyncio.run(_seed_many())
    resume = _fake_resume(failing_ops=set())

    plan = _plan(session_id)
    run = _post(session_id, resume)

    assert f'data-approval-in-order-step="{page + 1}"' in plan.text
    assert "data-approval-in-order-done" in run.text
    assert "Approved 1 step in order" in run.text
    assert _request_status(last) == "approved"
    assert [call.kwargs["request"].op_id for call in resume.await_args_list] == [
        f"op.step{page + 1}"
    ]


def test_a_yes_for_another_request_does_not_approve_the_risky_step() -> None:
    session_id = _session()
    s1 = _seed(1, "dangerous")
    resume = _fake_resume(failing_ops=set())

    response = _post(session_id, resume, confirm_id=str(uuid.uuid4()))

    assert 'data-approval-in-order-waiting="1"' in response.text
    assert _request_status(s1) == "pending"
    resume.assert_not_awaited()


def test_old_sign_in_stops_the_run_at_the_first_step_with_a_sign_in_link() -> None:
    """A sign-in from ten days ago approves nothing; the run links to a new sign-in."""
    session_id = _session()
    s1 = _seed(1, "caution")
    s2 = _seed(2, "caution")
    resume = _fake_resume(failing_ops=set())

    response = _post(session_id, resume, sign_in_age=10 * 24 * 3600)

    assert 'data-approval-in-order-stopped="1"' in response.text
    assert "needs a new sign-in" in response.text
    assert f'href="{_SIGN_IN_URL}"' in response.text
    assert [_request_status(r) for r in (s1, s2)] == ["pending", "pending"]
    resume.assert_not_awaited()


def test_self_approval_refusal_stops_the_run() -> None:
    session_id = _session()
    s1 = _seed(1, "caution", principal_sub=_REVIEWER_SUB)
    s2 = _seed(2, "caution")
    resume = _fake_resume(failing_ops=set())

    response = _post(session_id, resume)

    assert 'data-approval-in-order-stopped="1"' in response.text
    assert "The approval was refused" in response.text
    assert "You cannot approve your own change." in response.text
    # The refusal says where to approve (#3329), after the refusal text.
    assert WEB_APPROVAL_HINT in response.text
    assert _request_status(s1) == "pending"
    assert _request_status(s2) == "pending"
    resume.assert_not_awaited()


def test_a_self_approved_step_says_so_and_the_done_banner_counts_it() -> None:
    """A MEHO-SA-Operator approves their own step: the row and the banner say so (#3329)."""
    session_id = _session()
    s1 = _seed(1, "caution", principal_sub=_REVIEWER_SUB)
    s2 = _seed(2, "caution")
    resume = _fake_resume(failing_ops=set())

    response = _post(session_id, resume, operator=_sa_operator())

    assert response.status_code == 200, response.text
    body = response.text
    assert "data-approval-in-order-done" in body
    assert "Done. Approved 2 steps in order." in body
    assert "1 of them was your own change. The audit log records it as a self-approval." in body
    own = _step_row(body, 1)
    assert 'data-testid="approval-self-approved"' in own
    assert "You approved your own change as a MEHO-SA-Operator." in own
    assert "data-approve-right" not in own
    other = _step_row(body, 2)
    assert 'data-testid="approval-self-approved"' not in other
    assert [_request_status(r) for r in (s1, s2)] == ["approved", "approved"]


def test_an_ordinary_run_shows_no_self_approval_line() -> None:
    session_id = _session()
    _seed(1, "caution")
    resume = _fake_resume(failing_ops=set())

    body = _post(session_id, resume).text

    assert "Done. Approved 1 step in order." in body
    assert "approval-self-approved" not in body


def test_an_own_destructive_step_waits_with_the_self_approval_note_then_says_so() -> None:
    """The per-step yes names the self-approval before the click, and the row after it."""
    session_id = _session()
    s1 = _seed(1, "destructive", principal_sub=_REVIEWER_SUB)
    resume = _fake_resume(failing_ops=set())

    waiting = _post(session_id, resume, operator=_sa_operator()).text
    assert 'data-approval-in-order-waiting="1"' in waiting
    assert 'data-approval-confirm="self-approval"' in waiting
    assert "This is your own change: the audit log records a self-approval." in waiting
    assert _request_status(s1) == "pending"

    done = _post(session_id, resume, operator=_sa_operator(), confirm_id=str(s1)).text

    assert _request_status(s1) == "approved"
    assert "You approved your own change as a MEHO-SA-Operator." in _step_row(done, 1)
    assert "1 of them was your own change." in done


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


def test_a_caution_step_added_after_the_plan_was_shown_is_not_approved() -> None:
    """A request that joins the ticket after the plan was rendered stops the run.

    MEHO approves nothing, marks the new step and offers a fresh Start that
    lists it. Only that second Start approves both steps.
    """
    session_id = _session()
    s1 = _seed(1, "caution")
    plan = _plan(session_id)
    shown = _form_step_ids(plan.text)
    assert shown == [str(s1)]
    s2 = _seed(2, "caution")
    resume = _fake_resume(failing_ops=set())

    blocked = _post(session_id, resume, step_ids=shown)

    assert blocked.status_code == 200, blocked.text
    assert "data-approval-in-order-unseen" in blocked.text
    assert "MEHO approved nothing" in blocked.text
    assert "step 2 (op.step2)" in blocked.text
    assert "data-approval-in-order-new" in _step_row(blocked.text, 2)
    assert "data-approval-in-order-new" not in _step_row(blocked.text, 1)
    assert 'data-confirm="in-order-start"' in blocked.text
    assert "HX-Trigger" not in blocked.headers
    assert [_request_status(r) for r in (s1, s2)] == ["pending", "pending"]
    resume.assert_not_awaited()

    again = _post(session_id, resume, step_ids=_form_step_ids(blocked.text))

    assert "data-approval-in-order-done" in again.text
    assert [_request_status(r) for r in (s1, s2)] == ["approved", "approved"]


def test_a_step_added_while_the_run_waits_stops_the_yes() -> None:
    """The yes for a risky step covers only the list it was shown with."""
    session_id = _session()
    s1 = _seed(1, "dangerous")
    resume = _fake_resume(failing_ops=set())
    first = _post(session_id, resume)
    assert 'data-approval-in-order-waiting="1"' in first.text
    shown = _form_step_ids(first.text)
    s2 = _seed(2, "caution")

    response = _post(session_id, resume, step_ids=shown, confirm_id=str(s1))

    assert "data-approval-in-order-unseen" in response.text
    assert [_request_status(r) for r in (s1, s2)] == ["pending", "pending"]
    resume.assert_not_awaited()


def test_a_post_without_step_ids_approves_nothing() -> None:
    session_id = _session()
    s1 = _seed(1, "caution")
    resume = _fake_resume(failing_ops=set())

    response = _post(session_id, resume, step_ids=[])

    assert "data-approval-in-order-unseen" in response.text
    assert _request_status(s1) == "pending"
    resume.assert_not_awaited()


def test_a_step_id_that_is_not_a_request_id_is_refused() -> None:
    session_id = _session()
    s1 = _seed(1, "caution")
    resume = _fake_resume(failing_ops=set())

    response = _post(session_id, resume, step_ids=["not-an-id"])

    assert response.status_code == 422
    assert _request_status(s1) == "pending"
    resume.assert_not_awaited()
