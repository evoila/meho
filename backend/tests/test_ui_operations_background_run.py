# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run on the operations page answers at once; the dialog follows the run (#5111).

The operation runs as a background run (``OperationRunService``). The Run
button answers with "running" and the dialog asks the server again until
the run ends. Only the dispatcher is a stand-in: it runs until the test
releases it. The client keeps one event loop for the whole test
(``with client:``), so the background run goes on between requests.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationRun, OperationRunOrigin, OperationRunStatus
from tests.test_ui_operations import (
    _OP_ADMIN,
    _OP_OPERATOR,
    _TENANT_A,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _client_with_role_and_csrf,
    _csrf_headers,
    _register_recording_gh_connector,
    _seed_gh_target,
    _seed_ingested_op,
    _seed_tenant,
)

_CONNECTOR = "gh-rest-3"
_OP = "POST:/repos/{owner}/{repo}/issues"
_FORM = {
    "connector_id": _CONNECTOR,
    "op_id": _OP,
    "target": "gh-prod",
    "params": '{"owner": "evoila", "repo": "meho"}',
}
_OK = {
    "status": "ok",
    "op_id": _OP,
    "result": {"number": 7},
    "error": None,
    "duration_ms": 42.0,
    "handle": None,
    "extras": {},
}


class _SlowDispatcher:
    """The dispatcher: runs until the test releases it, then answers *envelope*."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self.envelope = envelope
        self.release = threading.Event()
        self.calls = 0

    async def __call__(self, operator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        # The cap keeps a route that waits for this call from hanging the test.
        for _ in range(250):
            if self.release.is_set():
                break
            await asyncio.sleep(0.02)
        return self.envelope


def _seed() -> tuple[TestClient, Any, str]:
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_recording_gh_connector()
    _seed_ingested_op(tenant_id=None)
    _seed_gh_target(tenant_id=_TENANT_A)
    return _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )


def _use(monkeypatch: pytest.MonkeyPatch, dispatcher: _SlowDispatcher) -> None:
    monkeypatch.setattr("meho_backplane.operations.meta_tools.call_operation", dispatcher)


def _in_loop[T](client: TestClient, fn: Callable[[], Awaitable[T]]) -> T:
    """Run *fn* on the client's event loop, where the background run lives."""
    assert client.portal is not None
    return client.portal.call(fn)


def _runs(client: TestClient) -> list[OperationRun]:
    async def _read() -> list[OperationRun]:
        async with get_sessionmaker()() as session:
            return list((await session.execute(select(OperationRun))).scalars().all())

    return _in_loop(client, _read)


def _run(client: TestClient, csrf: str, **form: str) -> tuple[Any, float]:
    started = time.monotonic()
    response = client.post(
        "/ui/operations/call", headers=_csrf_headers(csrf), data={**_FORM, **form}
    )
    return response, time.monotonic() - started


def _ask_until_ended(client: TestClient, run_id: str) -> str:
    """GET the run view until it stops asking again (at most 10 seconds)."""
    deadline = time.monotonic() + 10
    while True:
        body = client.get(f"/ui/operations/runs/{run_id}").text
        if "hx-trigger" not in body or time.monotonic() > deadline:
            return body
        time.sleep(0.05)


def _run_id_of(client: TestClient) -> str:
    (run,) = _runs(client)
    return str(run.id)


def test_slow_operation_answers_at_once_then_shows_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run answers with "running" before the operation ends; the dialog later shows the result."""
    client, mock, csrf = _seed()
    dispatcher = _SlowDispatcher(_OK)
    _use(monkeypatch, dispatcher)
    with mock, client:
        response, seconds = _run(client, csrf)
        run_id = _run_id_of(client)
        running = response.text
        final = ""
        # The dispatcher waits for the test, so the run cannot have ended yet.
        assert response.status_code == 200
        dispatcher.release.set()
        final = _ask_until_ended(client, run_id)

    assert seconds < 2.0 + 1.0  # the short wait, plus the slow test machine
    assert "The operation is running." in running
    assert "Closing does not stop the operation" in running
    assert f'hx-get="/ui/operations/runs/{run_id}"' in running
    assert "data-run-lock" in running
    assert f'data-meho-watch-run="{run_id}"' in running
    assert 'data-run-state="running"' in running
    assert 'data-run-status="ok"' not in running
    # The same result view as an inline call, with nothing left to wait for.
    assert 'data-run-status="ok"' in final
    assert 'data-run-result="inline"' in final
    assert "hx-get" not in final
    assert "data-run-lock" not in final
    assert f'data-meho-run-seen="{run_id}"' in final
    assert dispatcher.calls == 1


def test_a_quick_operation_shows_its_result_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mock, csrf = _seed()
    dispatcher = _SlowDispatcher(_OK)
    dispatcher.release.set()
    _use(monkeypatch, dispatcher)
    with mock, client:
        response, _ = _run(client, csrf)
    assert 'data-run-status="ok"' in response.text
    assert "data-run-lock" not in response.text


def test_failing_operation_shows_the_error_and_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mock, csrf = _seed()
    dispatcher = _SlowDispatcher(
        {
            "status": "error",
            "error": "the vendor refused it",
            "extras": {"error_code": "vendor_refused"},
        }
    )
    _use(monkeypatch, dispatcher)
    with mock, client:
        response, _ = _run(client, csrf)
        run_id = _run_id_of(client)
        assert "The operation is running." in response.text
        dispatcher.release.set()
        final = _ask_until_ended(client, run_id)
    assert 'data-run-status="error"' in final
    assert "vendor_refused" in final
    assert "the vendor refused it" in final


def test_operation_that_needs_approval_shows_the_link(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mock, csrf = _seed()
    approval_id = str(uuid.uuid4())
    dispatcher = _SlowDispatcher(
        {"status": "awaiting_approval", "extras": {"approval_request_id": approval_id}}
    )
    _use(monkeypatch, dispatcher)
    with mock, client:
        _run(client, csrf)
        dispatcher.release.set()
        final = _ask_until_ended(client, _run_id_of(client))
    assert 'data-run-status="awaiting_approval"' in final
    assert f'hx-get="/ui/approvals/{approval_id}"' in final
    assert "data-run-lock" not in final


def test_bad_params_json_stays_a_form_error_and_starts_no_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, mock, csrf = _seed()
    dispatcher = _SlowDispatcher(_OK)
    _use(monkeypatch, dispatcher)
    with mock, client:
        response, _ = _run(client, csrf, params="{not valid json")
        runs = _runs(client)
    assert response.status_code == 400
    assert 'data-run-status="form_error"' in response.text
    assert runs == []
    assert dispatcher.calls == 0


@pytest.mark.parametrize("error_code", ["target_required", "no_target"])
def test_a_target_that_did_not_resolve_stays_a_form_error(
    monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    """A quick answer and a late answer (a poll) both show the inline form error."""
    client, mock, csrf = _seed()
    dispatcher = _SlowDispatcher(
        {"status": "error", "error": "no such target", "extras": {"error_code": error_code}}
    )
    _use(monkeypatch, dispatcher)
    with mock, client:
        late, _ = _run(client, csrf)
        assert "The operation is running." in late.text
        run_id = _run_id_of(client)
        dispatcher.release.set()
        deadline = time.monotonic() + 10
        poll = client.get(f"/ui/operations/runs/{run_id}")
        while poll.status_code == 200 and time.monotonic() < deadline:
            time.sleep(0.05)
            poll = client.get(f"/ui/operations/runs/{run_id}")
        quick, _ = _run(client, csrf)
    for response in (poll, quick):
        assert response.status_code == 400
        assert 'data-run-status="form_error"' in response.text
        assert "no such target" in response.text
        assert "data-run-lock" not in response.text
    # A form error ends the watch, so no "the run has ended" notice follows.
    assert f'data-meho-run-seen="{run_id}"' in poll.text


def test_an_unknown_target_by_name_stays_a_form_error() -> None:
    """The real dispatcher: a target name that does not exist."""
    client, mock, csrf = _seed()
    with mock, client:
        response, _ = _run(client, csrf, target="no-such-target")
    assert response.status_code == 400
    assert 'data-run-status="form_error"' in response.text


def test_closing_the_dialog_does_not_stop_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nobody asks again after the Run: the run still ends and stores its result."""
    client, mock, csrf = _seed()
    dispatcher = _SlowDispatcher(_OK)
    _use(monkeypatch, dispatcher)
    with mock, client:
        _run(client, csrf)
        dispatcher.release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            (run,) = _runs(client)
            if run.status == OperationRunStatus.SUCCEEDED.value:
                break
            time.sleep(0.05)
    assert run.status == OperationRunStatus.SUCCEEDED.value
    assert run.result is not None
    assert run.result["status"] == "ok"
    assert run.origin == OperationRunOrigin.DIRECT.value


def test_one_click_is_one_run_and_the_view_locks_the_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The running view asks the script to keep Run disabled; a final view lets go."""
    client, mock, csrf = _seed()
    dispatcher = _SlowDispatcher(_OK)
    _use(monkeypatch, dispatcher)
    with mock, client:
        response, _ = _run(client, csrf)
        run_id = _run_id_of(client)
        again = client.get(f"/ui/operations/runs/{run_id}").text
        dispatcher.release.set()
        final = _ask_until_ended(client, run_id)
        runs = _runs(client)
    assert "data-run-lock" in response.text
    assert "data-run-lock" in again
    assert "data-run-lock" not in final
    assert len(runs) == 1
    assert dispatcher.calls == 1


def test_a_call_that_reveals_secrets_runs_inline_with_no_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reveal call cannot run in the background: its answer would be stored on the run."""
    client, mock, csrf = _seed()
    dispatcher = _SlowDispatcher(_OK)
    dispatcher.release.set()
    _use(monkeypatch, dispatcher)
    inline = _SlowDispatcher(_OK)
    inline.release.set()
    monkeypatch.setattr("meho_backplane.ui.routes.operations.routes.call_operation", inline)
    with mock, client:
        response, _ = _run(client, csrf, params='{"reveal_secret": true}')
        runs = _runs(client)
    assert 'data-run-status="ok"' in response.text
    assert runs == []
    assert inline.calls == 1
    assert dispatcher.calls == 0


def _seed_run(
    status: OperationRunStatus,
    *,
    result: dict[str, Any] | None = None,
    identity_sub: str = _OP_OPERATOR,
    tenant_id: uuid.UUID = _TENANT_A,
    origin: OperationRunOrigin = OperationRunOrigin.DIRECT,
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
                    connector_id=_CONNECTOR,
                    op_id=_OP,
                    status=status.value,
                    result=result,
                )
            )

    asyncio.run(_do())
    return run_id


@pytest.mark.parametrize(
    ("status", "result", "text", "state"),
    [
        (OperationRunStatus.FAILED, None, "ended without a result", "failed"),
        (OperationRunStatus.SUCCEEDED, None, "ended without a result", "no_result"),
        (OperationRunStatus.UNCERTAIN, None, "The outcome is unknown", "uncertain"),
        (OperationRunStatus.CANCELLED, None, "The run was cancelled", "cancelled"),
    ],
)
def test_a_run_that_ended_without_a_result_is_not_running_forever(
    status: OperationRunStatus, result: dict[str, Any] | None, text: str, state: str
) -> None:
    """Every end state is final: the view says so and stops asking."""
    client, mock, _ = _seed()
    run_id = _seed_run(status, result=result)
    with mock:
        body = client.get(f"/ui/operations/runs/{run_id}").text
    assert text in body
    assert f'data-run-state="{state}"' in body
    assert "hx-get" not in body
    assert "data-run-lock" not in body
    assert "is running" not in body


def test_a_run_that_still_goes_on_keeps_asking() -> None:
    client, mock, _ = _seed()
    run_id = _seed_run(OperationRunStatus.RUNNING)
    with mock:
        body = client.get(f"/ui/operations/runs/{run_id}").text
    assert f'hx-get="/ui/operations/runs/{run_id}"' in body
    assert "data-run-lock" in body


def test_only_the_person_who_started_a_run_reads_it() -> None:
    """Another person, another tenant, an approval run and an unknown id are all 404."""
    client, mock, _ = _seed()
    other_tenant = uuid.UUID("22222222-2222-2222-2222-222222222222")
    _seed_tenant(other_tenant, "tenant-b")
    ok = {"status": "ok", "result": {"secret": "made-up"}, "extras": {}}
    mine = _seed_run(OperationRunStatus.SUCCEEDED, result=ok)
    other_person = _seed_run(OperationRunStatus.SUCCEEDED, result=ok, identity_sub=_OP_ADMIN)
    other_tenant_run = _seed_run(OperationRunStatus.SUCCEEDED, result=ok, tenant_id=other_tenant)
    approval_run = _seed_run(
        OperationRunStatus.SUCCEEDED, result=ok, origin=OperationRunOrigin.APPROVAL_RESUME
    )
    with mock:
        assert client.get(f"/ui/operations/runs/{mine}").status_code == 200
        for run_id in (other_person, other_tenant_run, approval_run, uuid.uuid4()):
            response = client.get(f"/ui/operations/runs/{run_id}")
            assert response.status_code == 404
            assert "made-up" not in response.text


def test_every_page_loads_the_run_lock_script() -> None:
    client, mock, _ = _seed()
    with mock:
        body = client.get("/ui/operations").text
    assert '<script src="/ui/static/src/app/operation-run-lock.js" defer></script>' in body
