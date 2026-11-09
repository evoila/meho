# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every approval surface shows when an approved run returns hidden values.

A request whose requester sent ``reveal_secret: true`` keeps the choice on
its row (``approval_request.reveal_secret``), and the approved run returns
the secret values of the answer to the requester. The approver sees it:

* REST (``ApprovalRequestView.reveal_secret``) and MCP
  (``meho_approvals_list`` / ``meho_approvals_get``) carry the field.
* The console request window names it in the detail list on every status,
  and in the warning above the Approve button of a pending request.
* The in-order window marks the step.

A request without the flag shows none of this.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest
import respx
from sqlalchemy import update

from meho_backplane.api.v1.approvals import _view
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.mcp.tools.approvals import _get_handler, _list_handler, _row_to_dict
from meho_backplane.ui.routes.approvals.render import project_request_to_view
from tests.test_ui_approvals import (
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

_REF = "change-ticket-9"
_ROW_MARK = "data-approval-reveal-secret"
_GATE_MARK = "data-approval-gate-reveal-secret"
_STEP_MARK = "data-approval-in-order-reveal-secret"


def _set_reveal(request_id: uuid.UUID) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            await session.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == request_id)
                .values(reveal_secret=True)
            )

    asyncio.run(_do())


def _load(request_id: uuid.UUID) -> ApprovalRequest:
    async def _do() -> ApprovalRequest:
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            return row

    return asyncio.run(_do())


def _modal(request_id: uuid.UUID) -> str:
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/{request_id}")
    assert response.status_code == 200, response.text
    return str(response.text)


@pytest.fixture(autouse=True)
def _tenant() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")


def _seed(*, reveal: bool, **kwargs: Any) -> uuid.UUID:
    rid = _seed_request(tenant_id=_TENANT_A, op_id="vault.kv.read", **kwargs)
    if reveal:
        _set_reveal(rid)
    return rid


# ---------------------------------------------------------------------------
# REST, MCP and the console view dict
# ---------------------------------------------------------------------------


def test_read_views_carry_the_reveal_choice() -> None:
    shown = _load(_seed(reveal=True))
    plain = _load(_seed(reveal=False))

    assert _view(shown).reveal_secret is True
    assert _row_to_dict(shown)["reveal_secret"] is True
    assert project_request_to_view(shown)["reveal_secret"] is True

    assert _view(plain).reveal_secret is False
    assert _row_to_dict(plain)["reveal_secret"] is False
    assert project_request_to_view(plain)["reveal_secret"] is False
    assert _view(plain).model_dump(mode="json")["reveal_secret"] is False


def test_mcp_list_and_get_return_the_reveal_choice() -> None:
    rid = _seed(reveal=True)
    plain = _seed(reveal=False)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)

    got = asyncio.run(_get_handler(operator, {"approval_request_id": str(rid)}))
    listed = asyncio.run(_list_handler(operator, {"status": "pending"}))

    assert got["reveal_secret"] is True
    by_id = {item["id"]: item["reveal_secret"] for item in listed["items"]}
    assert by_id == {str(rid): True, str(plain): False}


# ---------------------------------------------------------------------------
# Console request window
# ---------------------------------------------------------------------------


def test_modal_of_a_pending_reveal_request_says_approving_returns_the_values() -> None:
    body = _modal(_seed(reveal=True))

    assert _ROW_MARK in body
    assert "Returned to the requester" in body
    assert _GATE_MARK in body
    assert "Approving returns the hidden values to the requester" in body


def test_modal_of_a_decided_reveal_request_still_names_it() -> None:
    rid = _seed(
        reveal=True,
        status_value=ApprovalRequestStatus.REJECTED.value,
        reviewed_by=_REVIEWER_SUB,
    )

    body = _modal(rid)

    assert _ROW_MARK in body
    # A decided request has no decision forms, so no warning above them.
    assert _GATE_MARK not in body


def test_modal_without_the_flag_shows_no_reveal_line() -> None:
    body = _modal(_seed(reveal=False))

    assert _ROW_MARK not in body
    assert _GATE_MARK not in body
    assert "hidden values" not in body


# ---------------------------------------------------------------------------
# In-order window
# ---------------------------------------------------------------------------


def test_in_order_window_marks_only_the_reveal_step() -> None:
    _seed(reveal=True, work_ref=_REF)
    _seed(reveal=False, work_ref=_REF)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_REVIEWER_SUB)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB, role=TenantRole.TENANT_ADMIN)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/in-order?work_ref={quote(_REF, safe='')}")

    assert response.status_code == 200, response.text
    assert response.text.count(_STEP_MARK) == 1
