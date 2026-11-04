# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may approve, in the web interface (#3328).

The web interface calls the same service checks as REST. A MEHO-SA-Operator
approves their own change with a click; another person's ``destructive``
change, approved by someone outside the groups that may approve it, shows
the refusal text and stays pending.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock

from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus, AuditLog
from tests.test_ui_approvals import (
    _REVIEWER_SUB,
    _TENANT_A,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _dispatch_result,
    _operator,
    _post_decision,
    _request_status,
    _seed_destructive_request,
    _seed_request,
    _seed_tenant,
)


def _decision_payload(request_id: uuid.UUID) -> dict[str, Any]:
    async def _do() -> dict[str, Any]:
        async with get_sessionmaker()() as session:
            rows = await session.execute(
                select(AuditLog.payload).where(AuditLog.path == "approval.decision")
            )
            for (payload,) in rows:
                if payload.get("approval_request_id") == str(request_id):
                    return dict(payload)
        raise AssertionError("no approval.decision audit row")

    return asyncio.run(_do())


def test_an_sa_operator_approves_their_own_destructive_change_with_a_click() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(
        tenant_id=_TENANT_A,
        principal_sub=_REVIEWER_SUB,
        op_id="vsphere.vm.destroy",
        proposed_effect={"op_id": "vsphere.vm.destroy", "safety_level": "destructive"},
        preview_hash="preview-hash",
    )
    sa_operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB).model_copy(
        update={"self_approver": True}
    )

    response = _post_decision(sa_operator, rid)

    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    assert _request_status(rid) == ApprovalRequestStatus.APPROVED.value
    payload = _decision_payload(rid)
    assert payload["self_approved"] is True
    assert payload["approval_rule"] == "sa_operator_self_approval"
    assert payload["approval_auth"]["channel"] == "web"


def test_an_operator_sees_why_they_cannot_approve_another_persons_destructive_change() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_destructive_request()
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)
    resume_mock = AsyncMock(return_value=_dispatch_result())

    response = _post_decision(operator, rid, resume_mock=resume_mock)

    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    body = response.text  # type: ignore[attr-defined]
    assert "403" in body
    assert "You cannot approve this destructive change of another person." in body
    assert "MEHO-SA-Operator or a MEHO-Admin" in body
    assert _request_status(rid) == ApprovalRequestStatus.PENDING.value
    resume_mock.assert_not_awaited()
