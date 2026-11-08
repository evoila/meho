# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approvals page on a phone (#6000).

* The Pending tab lists the requests this person may approve first, each
  with a "Waiting for you" badge. The check is the same one the approve
  uses. When the person cannot be checked, the list keeps its plain order
  and marks nothing.
* The decision window opens from the bottom on a phone, and its decision
  bar (Deny / Approve) sticks to the bottom of the window, so the buttons
  are in reach without scrolling.
* The rows stack on a phone, the text wraps instead of being cut, and the
  Review button fills the row.

The console has no browser test harness, so these tests pin the markup
that makes the phone layout work. The look itself was checked in a real
browser at 390 px and 1366 px wide.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import respx
from fastapi import HTTPException

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.ui.routes.approvals.waiting import waiting_for_me_first
from tests.test_ui_approvals import (
    _HX_HEADERS,
    _RESOLVE_OPERATOR,
    _REVIEWER_SUB,
    _TENANT_A,
    _authenticated_client,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _operator,
    _seed_request,
    _seed_session_sync,
    _seed_tenant,
)

_WAITING_BADGE = "Waiting for you"


def _row(op_id: str, principal_sub: str = "agent-7") -> ApprovalRequest:
    return ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=_TENANT_A,
        principal_sub=principal_sub,
        op_id=op_id,
        connector_id="vsphere-1.x",
        params_hash="0" * 64,
        params={},
        proposed_effect={"op_id": op_id},
        status=ApprovalRequestStatus.PENDING.value,
        created_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
    )


def _row_ops(body: str) -> list[str]:
    """The op ids of the history rows, in page order."""
    rows = body.split('id="approvals-history-rows"', 1)[1]
    return re.findall(r'aria-label="(?:Review|View) approval request for ([^"]+)"', rows)


def _waiting_ops(body: str) -> list[str]:
    """The op ids of the rows marked "waiting for you", in page order."""
    return [
        m.group(1)
        for m in re.finditer(
            r"<li [^>]*data-waiting-for-me>.*?approval request for ([^\"]+)\"", body, re.DOTALL
        )
    ]


def _get_list(url: str, resolve_operator: AsyncMock) -> str:
    """GET a list *url*, with *resolve_operator* standing in for the person check."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new=resolve_operator):
            response = client.get(url, headers=_HX_HEADERS)
    assert response.status_code == 200, response.text
    return response.text


# ---------------------------------------------------------------------------
# The order: waiting for you first
# ---------------------------------------------------------------------------


def test_waiting_for_me_first_puts_approvable_rows_first_and_keeps_order() -> None:
    own_a = _row("own.a", principal_sub=_REVIEWER_SUB)
    other_a = _row("other.a")
    own_b = _row("own.b", principal_sub=_REVIEWER_SUB)
    other_b = _row("other.b")
    operator = _operator(tenant_id=_TENANT_A)

    rows, waiting = waiting_for_me_first([own_a, other_a, own_b, other_b], operator)

    assert [r.op_id for r in rows] == ["other.a", "other.b", "own.a", "own.b"]
    assert waiting == {other_a.id, other_b.id}


def test_waiting_for_me_first_without_operator_keeps_order_and_marks_nothing() -> None:
    first, second = _row("a"), _row("b")

    rows, waiting = waiting_for_me_first([first, second], None)

    assert rows == [first, second]
    assert waiting == set()


def test_waiting_for_me_first_marks_nothing_for_a_role_that_cannot_decide() -> None:
    rows_in = [_row("a"), _row("b")]
    read_only = _operator(tenant_id=_TENANT_A, role=TenantRole.READ_ONLY)

    rows, waiting = waiting_for_me_first(rows_in, read_only)

    assert [r.op_id for r in rows] == ["a", "b"]
    assert waiting == set()


def test_pending_tab_lists_requests_waiting_for_me_first() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    # Newest first: own.new is the newest, so without the split it leads.
    _seed_request(
        tenant_id=_TENANT_A,
        op_id="other.old",
        created_at=datetime(2026, 6, 15, 10, 0, tzinfo=UTC),
    )
    _seed_request(
        tenant_id=_TENANT_A,
        op_id="own.new",
        principal_sub=_REVIEWER_SUB,
        created_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
    )
    _seed_request(
        tenant_id=_TENANT_A,
        op_id="other.mid",
        created_at=datetime(2026, 6, 15, 11, 0, tzinfo=UTC),
    )
    operator = _operator(tenant_id=_TENANT_A)

    body = _get_list("/ui/approvals/list?tab=pending", AsyncMock(return_value=operator))

    assert _row_ops(body) == ["other.mid", "other.old", "own.new"]
    assert _waiting_ops(body) == ["other.mid", "other.old"]
    assert body.count(_WAITING_BADGE) == 2


def test_pending_tab_without_a_checked_person_shows_the_plain_order() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(
        tenant_id=_TENANT_A, op_id="older", created_at=datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    )
    _seed_request(
        tenant_id=_TENANT_A, op_id="newer", created_at=datetime(2026, 6, 15, 11, 0, tzinfo=UTC)
    )

    for failure in (HTTPException(status_code=401, detail="ui_session_required"), OSError("down")):
        body = _get_list("/ui/approvals/list?tab=pending", AsyncMock(side_effect=failure))

        assert _row_ops(body) == ["newer", "older"]
        assert _WAITING_BADGE not in body
        assert "data-waiting-for-me" not in body


def test_other_tabs_do_not_mark_waiting_rows() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A, op_id="pending.one")
    operator = AsyncMock(return_value=_operator(tenant_id=_TENANT_A))

    body = _get_list("/ui/approvals/list?tab=all", operator)

    assert _row_ops(body) == ["pending.one"]
    assert _WAITING_BADGE not in body
    operator.assert_not_awaited()


def test_load_more_page_is_split_the_same_way() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    for i in range(25):
        _seed_request(
            tenant_id=_TENANT_A,
            op_id=f"first.{i:02d}",
            created_at=datetime(2026, 6, 15, 12, i, tzinfo=UTC),
        )
    # The two oldest rows land on page two; the newer one is the own one.
    _seed_request(
        tenant_id=_TENANT_A,
        op_id="page2.other",
        created_at=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
    )
    _seed_request(
        tenant_id=_TENANT_A,
        op_id="page2.own",
        principal_sub=_REVIEWER_SUB,
        created_at=datetime(2026, 6, 15, 10, 0, tzinfo=UTC),
    )
    operator = _operator(tenant_id=_TENANT_A)

    body = _get_list(
        "/ui/approvals/list?tab=pending&order=newest&offset=25&partial=rows",
        AsyncMock(return_value=operator),
    )

    ops = re.findall(r'aria-label="Review approval request for ([^"]+)"', body)
    assert ops == ["page2.other", "page2.own"]
    assert _waiting_ops(body) == ["page2.other"]


# ---------------------------------------------------------------------------
# The phone markup
# ---------------------------------------------------------------------------


def test_history_rows_stack_and_wrap_on_a_phone() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A, op_id="vsphere.vm.create")
    operator = _operator(tenant_id=_TENANT_A)

    body = _get_list("/ui/approvals/list?tab=pending", AsyncMock(return_value=operator))

    row = body.split('id="approvals-history-rows"', 1)[1].split("<li ", 1)[1].split(">", 1)[0]
    assert "flex-col" in row
    assert "sm:flex-row" in row
    # The text wraps on a phone and is cut to one line from sm up.
    assert "[overflow-wrap:anywhere] sm:truncate" in body
    # The Review button fills the row on a phone and is large.
    assert "btn btn-md w-full shrink-0 sm:btn-sm sm:w-auto" in body
    # The six status tabs may wrap to a second line instead of pushing the page sideways.
    assert 'class="tabs tabs-boxed w-fit max-w-full"' in body


def _modal_body(rid: uuid.UUID) -> str:
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/{rid}")
    assert response.status_code == 200, response.text
    return response.text


def test_decision_window_opens_from_the_bottom_with_a_sticky_decision_bar() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A)

    body = _modal_body(rid)

    assert 'class="modal modal-bottom sm:modal-middle"' in body
    before_bar, bar = body.split("data-approval-decision-bar", 1)
    bar_tag = before_bar.rsplit("<div", 1)[1]
    assert "sticky" in bar_tag
    assert "-bottom-6" in bar_tag
    # Both decisions and both confirm steps are in the bar.
    for marker in (
        'data-action="reject"',
        'data-action="approve"',
        'data-confirm="reject"',
        'data-confirm="approve"',
    ):
        assert marker in bar
    # The reason field is above the bar, not in it; the confirm forms read it by id.
    assert 'id="meho-approval-reason"' in before_bar
    assert 'id="meho-approval-reason"' not in bar
    assert bar.count('hx-include="#meho-approval-reason"') == 2
    # Large buttons that share the row on a phone.
    assert "btn btn-md btn-error btn-outline flex-1 gap-1 sm:btn-sm sm:flex-none" in bar
    assert "btn btn-md btn-primary flex-1 gap-1 sm:btn-sm sm:flex-none" in bar


def test_decided_request_has_no_decision_bar() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(
        tenant_id=_TENANT_A,
        status_value=ApprovalRequestStatus.APPROVED.value,
        reviewed_by="op-99",
        decided_at=datetime(2026, 6, 15, 13, 0, tzinfo=UTC),
    )

    body = _modal_body(rid)

    assert 'class="modal modal-bottom sm:modal-middle"' in body
    assert "data-approval-decision-bar" not in body


def test_bell_panel_opens_from_the_bottom_and_stacks_rows() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A, op_id="vsphere.vm.create")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals", headers=_HX_HEADERS)

    assert response.status_code == 200, response.text
    body = response.text
    assert 'class="modal modal-bottom sm:modal-middle"' in body
    assert "flex flex-col items-stretch gap-2 py-3 sm:flex-row" in body
    assert "btn btn-md btn-primary w-full shrink-0 sm:btn-sm sm:w-auto" in body
