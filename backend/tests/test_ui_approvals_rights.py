# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "may I approve this?" sign in the web interface (#3329).

The request window shows whether the person may approve the request and
why not. The answer comes from the same service checks as the approve, so
the Approve button state matches what the click then does. After a
self-approval, the outcome card says so.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.operations.approval_rights import WEB_APPROVAL_HINT
from tests.test_ui_approvals import (
    _RESOLVE_OPERATOR,
    _REVIEWER_SUB,
    _TENANT_A,
    _authenticated_client,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _dispatch_result,
    _operator,
    _post_decision,
    _request_status,
    _seed_destructive_request,
    _seed_request,
    _seed_session_sync,
    _seed_tenant,
)


def _sa_operator(**kwargs: object) -> Operator:
    """A person in the MEHO-SA-Operators group."""
    return _operator(tenant_id=_TENANT_A, **kwargs).model_copy(  # type: ignore[arg-type]
        update={"self_approver": True}
    )


def _own_destructive_request() -> uuid.UUID:
    return _seed_request(
        tenant_id=_TENANT_A,
        principal_sub=_REVIEWER_SUB,
        op_id="vsphere.vm.destroy",
        proposed_effect={"op_id": "vsphere.vm.destroy", "safety_level": "destructive"},
        preview_hash="preview-hash",
    )


def _modal(operator: Operator, rid: uuid.UUID) -> str:
    """Open the request window of *rid* as *operator*."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=operator.sub)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/{rid}")
    assert response.status_code == 200, response.text
    return response.text


def _approve_button(body: str) -> str:
    return body.split('data-action="approve"')[1].split("</button>")[0]


def test_an_sa_operator_sees_they_may_approve_their_own_change() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _own_destructive_request()

    body = _modal(_sa_operator(), rid)

    assert 'data-approve-right="yes"' in body
    assert 'data-approve-reason="own_change"' in body
    assert "You may approve it as a MEHO-SA-Operator." in body
    assert "disabled" not in _approve_button(body)
    # The armed approve confirm says it is a self-approval.
    assert 'data-approval-confirm="self-approval"' in body
    # No "you cannot approve" guidance for a person who may.
    assert "you cannot approve it" not in body


def test_another_persons_change_shows_yes_without_a_self_approval_line() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, proposed_effect={"safety_level": "caution"})

    body = _modal(_operator(tenant_id=_TENANT_A), rid)

    assert 'data-approve-right="yes"' in body
    assert 'data-approve-reason="ok"' in body
    assert "You may approve this change." in body
    assert 'data-approval-confirm="self-approval"' not in body


def test_the_wrong_group_sees_why_and_approve_is_disabled() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_destructive_request()

    body = _modal(_operator(tenant_id=_TENANT_A), rid)

    assert 'data-approve-right="no"' in body
    assert 'data-approve-reason="wrong_group"' in body
    assert "You cannot approve this request" in body
    assert "MEHO-SA-Operator or a MEHO-Admin" in body
    button = _approve_button(body)
    assert "disabled" in button
    assert "MEHO-SA-Operator or a MEHO-Admin" in button
    # No approve confirm state is reachable; Deny stays.
    assert 'data-confirm="approve"' not in body
    assert 'data-action="reject"' in body


def test_an_admin_may_approve_another_persons_destructive_change() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_destructive_request()

    body = _modal(_operator(tenant_id=_TENANT_A, role=TenantRole.TENANT_ADMIN), rid)

    assert 'data-approve-right="yes"' in body
    assert "disabled" not in _approve_button(body)


def test_an_operator_cannot_approve_their_own_destructive_change() -> None:
    """Break-glass does not help here, so the text does not name it."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _own_destructive_request()

    body = _modal(_operator(tenant_id=_TENANT_A), rid)

    assert 'data-approve-reason="own_change"' in body
    assert "You cannot approve your own change." in body
    assert "disabled" in _approve_button(body)


def test_the_button_matches_what_the_click_does() -> None:
    """Disabled for a person the approve refuses; enabled for a person it allows."""
    _seed_tenant(_TENANT_A, "tenant-a")
    refused = _seed_destructive_request()
    allowed = _own_destructive_request()
    operator = _operator(tenant_id=_TENANT_A)
    sa_operator = _sa_operator()

    assert "disabled" in _approve_button(_modal(operator, refused))
    assert "disabled" not in _approve_button(_modal(sa_operator, allowed))

    resume_mock = AsyncMock(return_value=_dispatch_result())
    _post_decision(operator, refused, resume_mock=resume_mock)
    _post_decision(sa_operator, allowed)

    assert _request_status(refused) == ApprovalRequestStatus.PENDING.value
    assert _request_status(allowed) == ApprovalRequestStatus.APPROVED.value
    resume_mock.assert_not_awaited()


def test_a_self_approval_shows_a_confirmation_after_the_click() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _own_destructive_request()

    response = _post_decision(_sa_operator(), rid)

    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    body = response.text  # type: ignore[attr-defined]
    assert 'data-testid="approval-self-approved"' in body
    assert "You approved your own change as a MEHO-SA-Operator." in body
    assert "The audit log records it as a self-approval." in body


def test_an_ordinary_approval_shows_no_self_approval_line() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A)

    response = _post_decision(_operator(tenant_id=_TENANT_A), rid)

    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    assert 'data-testid="approval-self-approved"' not in response.text  # type: ignore[attr-defined]


def test_a_forced_own_destructive_approve_shows_the_refusal_text() -> None:
    """The 403 text of the approve is shown as it is, with the guidance."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _own_destructive_request()
    resume_mock = AsyncMock(return_value=_dispatch_result())

    response = _post_decision(_operator(tenant_id=_TENANT_A), rid, resume_mock=resume_mock)

    body = response.text  # type: ignore[attr-defined]
    assert "You cannot approve your own request" in body
    assert "Ask a MEHO-SA-Operator or a MEHO-Admin to approve this" in body
    assert "agent-requester" in body
    # The refusal says where to approve (#3329), after the refusal text.
    assert 'data-testid="approval-refusal-hint"' in body
    assert body.index("Ask a MEHO-SA-Operator") < body.index(WEB_APPROVAL_HINT)
    assert _request_status(rid) == ApprovalRequestStatus.PENDING.value
    resume_mock.assert_not_awaited()


def test_a_forced_wrong_group_approve_shows_the_refusal_text_and_the_hint() -> None:
    """A 403 for another person's destructive change keeps its text and adds the hint."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_destructive_request()
    resume_mock = AsyncMock(return_value=_dispatch_result())

    response = _post_decision(_operator(tenant_id=_TENANT_A), rid, resume_mock=resume_mock)

    body = response.text  # type: ignore[attr-defined]
    assert "Could not complete (403)" in body
    assert "Only a MEHO-SA-Operator or a MEHO-Admin may approve" in body
    assert 'data-testid="approval-refusal-hint"' in body
    assert WEB_APPROVAL_HINT in body
    assert _request_status(rid) == ApprovalRequestStatus.PENDING.value
    resume_mock.assert_not_awaited()


def test_a_refused_reject_carries_no_web_hint() -> None:
    """The hint is for approve refusals only, as on the command line."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A)
    read_only = _operator(tenant_id=_TENANT_A, role=TenantRole.READ_ONLY)

    response = _post_decision(read_only, rid, action="reject")

    body = response.text  # type: ignore[attr-defined]
    assert "Could not complete (403)" in body
    assert "approval-refusal-hint" not in body
    assert WEB_APPROVAL_HINT not in body
    assert _request_status(rid) == ApprovalRequestStatus.PENDING.value
