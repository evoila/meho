# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approve in the console answers at once; the page follows the run (#5110).

The approved operation runs in the background. The outcome card and the
approvals list say "running", then ask the server again until the run
ends, and a notice on any page says when it ended.

The run is the real background run (``OperationRunService``); only the
approved operation itself is a stand-in that runs until the test releases
it. It claims the run and stores its result the way the real resume does.
The client keeps one event loop for the whole test (``with client:``), so
the background run goes on between requests.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    OperationRun,
    OperationRunOrigin,
    OperationRunStatus,
)
from meho_backplane.operations.approval_queue import (
    approval_result_state,
    claim_resume,
    persist_resume_result,
)
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from tests.test_ui_approvals import (
    _REQUESTER_SUB,
    _RESOLVE_OPERATOR,
    _RESUME_DISPATCH,
    _REVIEWER_SUB,
    _TENANT_A,
    _TENANT_B,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _build_app,
    _csrf_token,
    _operator,
    _seed_request,
    _seed_session_sync,
    _seed_tenant,
)

_OP_ID = "vsphere.vm.create"
_RUN_AUDIT_ID = "55555555-5555-5555-5555-555555555555"
_OK = {"status": "ok", "op_id": _OP_ID, "result": {"vm": "vm-1"}, "audit_id": _RUN_AUDIT_ID}
_FAILED = {
    "status": "error",
    "op_id": _OP_ID,
    "audit_id": _RUN_AUDIT_ID,
    "extras": {"error_code": "connector_vault_forbidden"},
}


class _SlowOperation:
    """The approved operation: claims the run, waits for the test, stores *envelope*."""

    def __init__(self, request_id: uuid.UUID, envelope: dict[str, Any]) -> None:
        self.request_id = request_id
        self.envelope = envelope
        self.release = threading.Event()
        self.calls = 0

    async def __call__(self, **_: object) -> dict[str, Any]:
        self.calls += 1
        assert await claim_resume(self.request_id)
        while not self.release.is_set():
            await asyncio.sleep(0.02)
        await persist_resume_result(self.request_id, self.envelope, parked_op_id=_OP_ID)
        return self.envelope


def _client() -> tuple[TestClient, str]:
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_REVIEWER_SUB)
    csrf = _csrf_token(session_id)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    return client, csrf


def _in_loop[T](client: TestClient, fn: Callable[[], Awaitable[T]]) -> T:
    """Run *fn* on the client's event loop, where the background run lives."""
    assert client.portal is not None
    return client.portal.call(fn)


def _stored(client: TestClient, request_id: uuid.UUID) -> ApprovalRequest:
    async def _read() -> ApprovalRequest:
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            return row

    return _in_loop(client, _read)


def _runs_of(client: TestClient, request_id: uuid.UUID) -> list[OperationRun]:
    async def _read() -> list[OperationRun]:
        async with get_sessionmaker()() as session:
            stmt = select(OperationRun).where(OperationRun.approval_request_id == request_id)
            return list((await session.execute(stmt)).scalars().all())

    return _in_loop(client, _read)


def _approve(client: TestClient, csrf: str, request_id: uuid.UUID) -> tuple[str, float]:
    started = time.monotonic()
    response = client.post(
        f"/ui/approvals/{request_id}/approve",
        data={"reason": "looks safe"},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert response.status_code == 200, response.text
    return response.text, time.monotonic() - started


def _ask_until(client: TestClient, url: str, done: Callable[[str], bool]) -> str:
    """GET *url* until *done* says the answer is final (at most 10 seconds)."""
    deadline = time.monotonic() + 10
    while True:
        body = client.get(url).text
        if done(body) or time.monotonic() > deadline:
            return body
        time.sleep(0.05)


def _has_ended(body: str) -> bool:
    return "hx-trigger" not in body


def test_slow_operation_answers_at_once_then_shows_ran_ok() -> None:
    """The approve answers within 2 seconds with "running"; the page later shows "ran OK"."""
    client, csrf = _client()
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    operation = _SlowOperation(rid, _OK)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)

    with (
        respx.mock(assert_all_called=False),
        patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
        patch(_RESUME_DISPATCH, operation),
        client,
    ):
        card, seconds = _approve(client, csrf, rid)

        assert seconds < 2
        assert "Approved. The operation is running." in card
        assert "Closing does not stop the operation." in card
        assert f'hx-get="/ui/approvals/{rid}/run"' in card
        run_id = str(_runs_of(client, rid)[0].id)
        assert f'data-meho-watch-run="{run_id}"' in card
        # The request says the run started; the list row says running and asks again.
        assert approval_result_state(_stored(client, rid)).value == "started"  # type: ignore[union-attr]
        listing = client.get("/ui/approvals/list?tab=approved").text
        assert 'data-result-state="running"' in listing
        assert f'hx-get="/ui/approvals/{rid}/row"' in listing
        assert "/ui/runs/notices" not in listing

        operation.release.set()
        card = _ask_until(client, f"/ui/approvals/{rid}/run", _has_ended)
        row = _ask_until(client, f"/ui/approvals/{rid}/row", _has_ended)

        assert approval_result_state(_stored(client, rid)).value == "executed"  # type: ignore[union-attr]

    assert "Request approved" in card
    assert "ran OK" in card
    assert "hx-get" not in card
    assert f'data-meho-run-seen="{run_id}"' in card
    assert 'data-result-state="executed"' in row
    assert "ran OK" in row
    assert 'hx-get="/ui/approvals/' + str(rid) + '/row"' not in row
    assert operation.calls == 1


def test_failing_operation_shows_run_failed_with_the_audit_link() -> None:
    """A run that fails ends as "run failed", with the link to its audit row."""
    client, csrf = _client()
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    operation = _SlowOperation(rid, _FAILED)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)

    with (
        respx.mock(assert_all_called=False),
        patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
        patch(_RESUME_DISPATCH, operation),
        client,
    ):
        card, _ = _approve(client, csrf, rid)
        assert "Approved. The operation is running." in card

        operation.release.set()
        card = _ask_until(client, f"/ui/approvals/{rid}/run", _has_ended)
        row = _ask_until(client, f"/ui/approvals/{rid}/row", _has_ended)
        state = approval_result_state(_stored(client, rid))

    assert state is not None and state.value == "failed"
    assert "Approved, but the run failed" in card
    assert "connector_vault_forbidden" in card
    assert f'href="/ui/audit?audit_id={_RUN_AUDIT_ID}"' in card
    assert 'data-result-state="failed"' in row
    assert "run failed" in row


def test_closing_the_dialog_does_not_stop_the_run() -> None:
    """Nobody asks again after the approve: the run still ends and stores its result."""
    client, csrf = _client()
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    operation = _SlowOperation(rid, _OK)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)

    with (
        respx.mock(assert_all_called=False),
        patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
        patch(_RESUME_DISPATCH, operation),
        client,
    ):
        _approve(client, csrf, rid)
        operation.release.set()
        deadline = time.monotonic() + 10
        while _stored(client, rid).resume_result is None and time.monotonic() < deadline:
            time.sleep(0.05)
        stored = _stored(client, rid)
        runs = _runs_of(client, rid)

    assert stored.resume_result is not None
    assert stored.resume_result["status"] == "ok"
    assert [run.status for run in runs] == [OperationRunStatus.SUCCEEDED.value]


def test_a_second_approve_is_refused_and_the_operation_runs_once() -> None:
    """The second approve gets "already decided"; one run, one call of the operation."""
    client, csrf = _client()
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    operation = _SlowOperation(rid, _OK)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)

    with (
        respx.mock(assert_all_called=False),
        patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
        patch(_RESUME_DISPATCH, operation),
        client,
    ):
        _approve(client, csrf, rid)
        again, _ = _approve(client, csrf, rid)
        operation.release.set()
        _ask_until(client, f"/ui/approvals/{rid}/run", _has_ended)
        runs = _runs_of(client, rid)

    assert "This request was already approved." in again
    assert operation.calls == 1
    assert len(runs) == 1
    assert runs[0].origin == OperationRunOrigin.APPROVAL_RESUME.value


def _seed_run(
    *,
    approval_request_id: uuid.UUID | None,
    status: OperationRunStatus,
    result: dict[str, Any] | None = None,
    tenant_id: uuid.UUID = _TENANT_A,
    identity_sub: str = _REVIEWER_SUB,
    origin: OperationRunOrigin = OperationRunOrigin.APPROVAL_RESUME,
) -> uuid.UUID:
    run_id = uuid.uuid4()

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                OperationRun(
                    id=run_id,
                    tenant_id=tenant_id,
                    identity_sub=identity_sub,
                    origin=origin.value,
                    connector_id="vsphere-1.x",
                    op_id=_OP_ID,
                    approval_request_id=approval_request_id,
                    status=status.value,
                    result=result,
                )
            )

    asyncio.run(_do())
    return run_id


def _approved(*, resumed_minutes_ago: float | None) -> uuid.UUID:
    now = datetime.now(UTC)
    return _seed_request(
        tenant_id=_TENANT_A,
        principal_sub=_REQUESTER_SUB,
        status_value=ApprovalRequestStatus.APPROVED.value,
        reviewed_by=_REVIEWER_SUB,
        decided_at=now,
        resumed_at=(
            now - timedelta(minutes=resumed_minutes_ago)
            if resumed_minutes_ago is not None
            else None
        ),
    )


def test_a_run_that_ended_without_a_result_is_not_running_forever() -> None:
    """A run that died (for example in a restart) ends the asking with "no result"."""
    client, _ = _client()
    rid = _approved(resumed_minutes_ago=1)
    _seed_run(approval_request_id=rid, status=OperationRunStatus.UNCERTAIN)

    with respx.mock(assert_all_called=False), client:
        card = client.get(f"/ui/approvals/{rid}/run").text
        row = client.get(f"/ui/approvals/{rid}/row").text
        listing = client.get("/ui/approvals/list?tab=approved").text

    assert "Approved, but the run ended without a result" in card
    assert "Do not run it again." in card
    assert "hx-trigger" not in card
    assert 'data-result-state="ended_no_result"' in row
    assert "no result: check it" in row
    assert "hx-trigger" not in row
    assert f'hx-get="/ui/approvals/{rid}/row"' not in listing


def test_a_run_that_was_never_started_by_this_server_stops_asking_at_the_outcome_list() -> None:
    """Started elsewhere, no result, no run row: asks again only until it is "outcome unknown"."""
    client, _ = _client()
    fresh = _approved(resumed_minutes_ago=1)
    old = _approved(resumed_minutes_ago=60)

    with respx.mock(assert_all_called=False), client:
        fresh_row = client.get(f"/ui/approvals/{fresh}/row").text
        old_row = client.get(f"/ui/approvals/{old}/row").text

    assert "hx-trigger" in fresh_row
    assert 'data-result-state="started"' in fresh_row
    assert "hx-trigger" not in old_row
    assert "no result: check it" in old_row


def test_run_state_routes_are_tenant_scoped() -> None:
    """A request of another tenant is a 404 on both routes."""
    client, _ = _client()
    _seed_tenant(_TENANT_B, "tenant-b")
    other = _seed_request(
        tenant_id=_TENANT_B,
        status_value=ApprovalRequestStatus.APPROVED.value,
        decided_at=datetime.now(UTC),
    )

    with respx.mock(assert_all_called=False), client:
        assert client.get(f"/ui/approvals/{other}/run").status_code == 404
        assert client.get(f"/ui/approvals/{other}/row").status_code == 404


# ---------------------------------------------------------------------------
# "The run has ended" notice
# ---------------------------------------------------------------------------


def _notices(client: TestClient, *run_ids: uuid.UUID) -> str:
    query = "&".join(f"run_id={rid}" for rid in run_ids)
    response = client.get(f"/ui/runs/notices?{query}")
    assert response.status_code == 200, response.text
    return response.text


def test_notice_shows_when_the_run_has_ended() -> None:
    """A running run gets no notice; an ended one gets a notice with the request link."""
    client, csrf = _client()
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    operation = _SlowOperation(rid, _OK)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)

    with (
        respx.mock(assert_all_called=False),
        patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
        patch(_RESUME_DISPATCH, operation),
        client,
    ):
        _approve(client, csrf, rid)
        run_id = _runs_of(client, rid)[0].id
        while_running = _notices(client, run_id)
        operation.release.set()
        _ask_until(client, f"/ui/approvals/{rid}/run", _has_ended)
        ended = _notices(client, run_id)

    assert "data-run-ended" not in while_running
    assert f'data-run-ended="{run_id}"' in ended
    assert 'data-run-state="executed"' in ended
    assert "The run has ended: ran OK" in ended
    assert _OP_ID in ended
    assert f'hx-get="/ui/approvals/{rid}"' in ended
    assert "data-run-notice-close" in ended
    assert "onclick" not in ended


def test_notice_of_a_failed_and_of_a_lost_run() -> None:
    """A failed run says "run failed"; a run that died says "no result: check it"."""
    client, _ = _client()
    failed_request = _approved(resumed_minutes_ago=1)
    failed = _seed_run(
        approval_request_id=failed_request,
        status=OperationRunStatus.SUCCEEDED,
        result=_FAILED,
    )
    lost = _seed_run(
        approval_request_id=_approved(resumed_minutes_ago=1), status=OperationRunStatus.FAILED
    )

    with respx.mock(assert_all_called=False), client:
        body = _notices(client, failed, lost)

    assert "The run has ended: run failed" in body
    assert "The run has ended: no result: check it" in body


def test_notice_is_only_for_the_person_who_started_the_run() -> None:
    """Another person's run, another tenant's run and an unknown id get a hidden marker only."""
    client, _ = _client()
    _seed_tenant(_TENANT_B, "tenant-b")
    someone_else = _seed_run(
        approval_request_id=None,
        status=OperationRunStatus.SUCCEEDED,
        result=_OK,
        identity_sub="someone-else",
        origin=OperationRunOrigin.DIRECT,
    )
    other_tenant = _seed_run(
        approval_request_id=None,
        status=OperationRunStatus.SUCCEEDED,
        result=_OK,
        tenant_id=_TENANT_B,
        origin=OperationRunOrigin.DIRECT,
    )
    unknown = uuid.uuid4()

    with respx.mock(assert_all_called=False), client:
        body = _notices(client, someone_else, other_tenant, unknown)

    assert "data-run-notice " not in body
    assert "The run has ended" not in body
    for rid in (someone_else, other_tenant, unknown):
        assert f'data-run-ended="{rid}" data-run-notice-drop' in body


def test_notice_of_a_run_without_an_approval_has_no_request_link() -> None:
    """The notice is not tied to approvals: a plain run gets it without a link."""
    client, _ = _client()
    plain = _seed_run(
        approval_request_id=None,
        status=OperationRunStatus.SUCCEEDED,
        result=_OK,
        origin=OperationRunOrigin.DIRECT,
    )

    with respx.mock(assert_all_called=False), client:
        body = _notices(client, plain)

    assert "The run has ended: ran OK" in body
    assert "View request" not in body


def test_notice_route_refuses_too_many_ids() -> None:
    client, _ = _client()
    query = "&".join(f"run_id={uuid.uuid4()}" for _ in range(21))
    with respx.mock(assert_all_called=False), client:
        assert client.get(f"/ui/runs/notices?{query}").status_code == 422


def test_every_page_loads_the_notice_script_and_its_corner() -> None:
    """``base.html`` carries the notice script and the corner the notices go to."""
    client, _ = _client()
    with respx.mock(assert_all_called=False), client:
        body = client.get("/ui/approvals").text
    assert '<script src="/ui/static/src/app/run-notices.js" defer></script>' in body
    assert 'id="meho-run-notices"' in body
