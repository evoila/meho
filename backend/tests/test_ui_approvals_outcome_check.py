# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console: the "Outcome unknown" tab and "Mark as checked" (#5026).

* The tab lists the same approved requests as the service list.
* The detail modal of such a request says what to check and offers the
  form; after the check it shows what the person recorded.
* The POST records the check, runs nothing, and refuses a request that is
  not on the list.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import respx

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations.approval_outcome_check import list_outcome_unknown
from meho_backplane.operations.uncertain_outcome import result_interrupted_by_restart
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from tests.test_ui_approvals import (
    _RESOLVE_OPERATOR,
    _REVIEWER_SUB,
    _TENANT_A,
    _TENANT_B,
    _authenticated_client,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _csrf_token,
    _operator,
    _seed_request,
    _seed_session_sync,
    _seed_tenant,
)

_RESUME = "meho_backplane.operations.approval_queue.resume_dispatch_after_approval"
_UNCERTAIN = result_interrupted_by_restart("vsphere.vm.create").model_dump(mode="json")


def _ago(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _approved(*, tenant_id: uuid.UUID = _TENANT_A, **kwargs: object) -> uuid.UUID:
    return _seed_request(
        tenant_id=tenant_id,
        status_value=ApprovalRequestStatus.APPROVED.value,
        reviewed_by="op-approver",
        decided_at=_ago(60),
        **kwargs,  # type: ignore[arg-type]
    )


def _load(request_id: uuid.UUID) -> ApprovalRequest:
    async def _do() -> ApprovalRequest:
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            return row

    return asyncio.run(_do())


def _set_columns(request_id: uuid.UUID, **values: object) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            for name, value in values.items():
                setattr(row, name, value)
            await session.commit()

    asyncio.run(_do())


def _service_ids() -> set[uuid.UUID]:
    async def _do() -> set[uuid.UUID]:
        async with get_sessionmaker()() as session:
            return {r.id for r in await list_outcome_unknown(session, tenant_id=_TENANT_A)}

    return asyncio.run(_do())


def test_outcome_unknown_tab_lists_the_service_list() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    uncertain = _approved(op_id="op.uncertain", resumed_at=_ago(1), resume_result=_UNCERTAIN)
    started = _approved(op_id="op.started.long.ago", resumed_at=_ago(30))
    _approved(op_id="op.started.just.now", resumed_at=_ago(2))
    _approved(op_id="op.ran.ok", resumed_at=_ago(30), resume_result={"status": "ok"})
    _approved(tenant_id=_TENANT_B, op_id="op.other.tenant", resumed_at=_ago(30))
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/list?tab=outcome_unknown")

    assert response.status_code == 200, response.text
    page = response.text
    assert "Outcome unknown" in page
    assert "op.uncertain" in page
    assert "op.started.long.ago" in page
    assert 'data-outcome-unknown="started_no_result"' in page
    for absent in ("op.started.just.now", "op.ran.ok", "op.other.tenant"):
        assert absent not in page
    assert _service_ids() == {uncertain, started}


def test_outcome_unknown_tab_empty_state() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        response = _authenticated_client(session_id).get("/ui/approvals?tab=outcome_unknown")
    assert response.status_code == 200, response.text
    assert "No approved changes with an unknown outcome" in response.text


def test_detail_modal_offers_the_check_for_a_run_with_no_result() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _approved(resumed_at=_ago(30))
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A)

    with (
        respx.mock(assert_all_called=False),
        patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
    ):
        response = _authenticated_client(session_id).get(f"/ui/approvals/{rid}")

    assert response.status_code == 200, response.text
    assert 'data-testid="approval-outcome-unknown"' in response.text
    assert "The run started and stored no result." in response.text
    assert 'data-testid="approval-outcome-check-form"' in response.text
    assert f'hx-post="/ui/approvals/{rid}/outcome-check"' in response.text


def test_request_back_on_the_list_offers_the_check_again() -> None:
    """A re-run after a check puts the request back on the list.

    The old check covers the first run only. The row shows the warning
    badge, and the modal shows the old check as an earlier check and offers
    the form again.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _approved(op_id="op.rerun.after.check", resumed_at=_ago(90))
    _set_columns(
        rid,
        outcome_checked_at=_ago(60),
        outcome_checked_by=_REVIEWER_SUB,
        outcome_check_verdict="did_not_happen",
        outcome_check_note="nothing on the host",
        outcome_check_reason="started_no_result",
        rerun_count=1,
        rerun_started_at=_ago(30),
    )
    assert rid in _service_ids()
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A)

    with (
        respx.mock(assert_all_called=False),
        patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
    ):
        client = _authenticated_client(session_id)
        rows = client.get("/ui/approvals/list?tab=outcome_unknown")
        modal = client.get(f"/ui/approvals/{rid}")

    assert rows.status_code == 200, rows.text
    assert "op.rerun.after.check" in rows.text
    assert 'data-outcome-unknown="started_no_result"' in rows.text
    assert "data-outcome-check=" not in rows.text
    assert modal.status_code == 200, modal.text
    assert 'data-testid="approval-outcome-unknown"' in modal.text
    assert 'data-earlier="true"' in modal.text
    assert "Earlier check" in modal.text
    assert "nothing on the host" in modal.text
    assert 'data-testid="approval-outcome-check-form"' in modal.text


def test_mark_as_checked_records_the_check_and_runs_nothing() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _approved(resumed_at=_ago(1), resume_result=_UNCERTAIN)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)
    resume = AsyncMock()

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RESUME, resume),
        ):
            response = client.post(
                f"/ui/approvals/{rid}/outcome-check",
                data={"verdict": "did_not_happen", "note": "no new VM on the host"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    assert response.headers.get("HX-Trigger") == "meho:approval-bump"
    assert 'data-testid="approval-outcome-check"' in response.text
    assert 'data-verdict="did_not_happen"' in response.text
    assert 'data-earlier="false"' in response.text
    assert "no new VM on the host" in response.text
    assert 'data-testid="approval-outcome-check-form"' not in response.text
    resume.assert_not_called()
    row = _load(rid)
    assert row.outcome_check_verdict == "did_not_happen"
    assert row.resume_result == _UNCERTAIN
    assert rid not in _service_ids()


def test_mark_as_checked_refusals_show_in_the_modal() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    ran_ok = _approved(resumed_at=_ago(30), resume_result={"status": "ok"})
    uncertain = _approved(resumed_at=_ago(1), resume_result=_UNCERTAIN)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    def post(rid: uuid.UUID, role: TenantRole, note: str = "checked") -> str:
        operator = _operator(tenant_id=_TENANT_A, role=role)
        with respx.mock(assert_all_called=False):
            client = _authenticated_client(session_id)
            client.cookies.set(CSRF_COOKIE_NAME, csrf)
            with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
                response = client.post(
                    f"/ui/approvals/{rid}/outcome-check",
                    data={"verdict": "happened", "note": note},
                    headers={CSRF_HEADER_NAME: csrf},
                )
        assert response.status_code == 200, response.text
        return response.text

    assert "has no unknown outcome to check" in post(ran_ok, TenantRole.OPERATOR)
    assert "Your role cannot mark" in post(uncertain, TenantRole.READ_ONLY)
    assert "Write what you checked" in post(uncertain, TenantRole.OPERATOR, note="  ")
    assert _load(uncertain).outcome_checked_at is None


def test_mark_as_checked_without_csrf_is_refused() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _approved(resumed_at=_ago(1), resume_result=_UNCERTAIN)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        response = _authenticated_client(session_id).post(
            f"/ui/approvals/{rid}/outcome-check",
            data={"verdict": "happened", "note": "checked"},
        )
    assert response.status_code == 403
    assert _load(rid).outcome_checked_at is None
