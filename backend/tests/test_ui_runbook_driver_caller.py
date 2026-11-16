# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The console's Advance passes the person's checked login to a check that runs an operation."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.runbooks.run_service import PreviousStepFailedError, StepCallerRequiredError
from meho_backplane.runbooks.runs_schemas import (
    CurrentStepResponse,
    RunCompletedResponse,
    StepBody,
    StepBodyVerify,
    StepPosition,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.routes.runbooks import driver
from meho_backplane.ui.routes.runbooks.driver_render import StepView
from meho_backplane.ui.templating import get_templates

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000012b4")
_SUB = "person-alpha"


def _session() -> UISessionContext:
    return UISessionContext(session_id=uuid.uuid4(), operator_sub=_SUB, tenant_id=_TENANT)


def _operator() -> Operator:
    return Operator(
        sub=_SUB,
        name=_SUB,
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture
def render(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    fake = AsyncMock(return_value="rendered")
    monkeypatch.setattr(driver, "render_step_fragment", fake)
    return fake


def _patch_next(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> AsyncMock:
    fake = AsyncMock(**kwargs)
    monkeypatch.setattr(driver.RunbookRunService, "next_step", fake)
    return fake


@pytest.mark.asyncio
async def test_operation_call_advance_passes_checked_login(
    monkeypatch: pytest.MonkeyPatch, render: AsyncMock
) -> None:
    operator = _operator()
    monkeypatch.setattr(driver, "resolve_ui_operator", AsyncMock(return_value=operator))
    run_id = uuid.uuid4()
    fake_next = _patch_next(
        monkeypatch,
        return_value=RunCompletedResponse(run_id=run_id, completed_at="2026-11-15T00:00:00Z"),
    )

    await driver._advance(MagicMock(), _session(), run_id, "operation_call", "")

    assert fake_next.await_args.kwargs["caller"] is operator


@pytest.mark.asyncio
async def test_confirm_advance_does_not_check_login(
    monkeypatch: pytest.MonkeyPatch, render: AsyncMock
) -> None:
    resolve = AsyncMock()
    monkeypatch.setattr(driver, "resolve_ui_operator", resolve)
    run_id = uuid.uuid4()
    fake_next = _patch_next(
        monkeypatch,
        return_value=RunCompletedResponse(run_id=run_id, completed_at="2026-11-15T00:00:00Z"),
    )

    await driver._advance(MagicMock(), _session(), run_id, "confirm", "yes")

    resolve.assert_not_awaited()
    assert fake_next.await_args.kwargs["caller"] is None


@pytest.mark.asyncio
async def test_login_not_checkable_shows_inline_alert(
    monkeypatch: pytest.MonkeyPatch, render: AsyncMock
) -> None:
    monkeypatch.setattr(driver, "resolve_ui_operator", AsyncMock(return_value=None))
    _patch_next(monkeypatch, side_effect=StepCallerRequiredError("needs the caller"))

    result = await driver._advance(MagicMock(), _session(), uuid.uuid4(), "operation_call", "")

    assert result == "rendered"
    assert render.await_args.kwargs["error_message"] == "needs the caller"


@pytest.mark.asyncio
async def test_failed_check_shows_the_reason(
    monkeypatch: pytest.MonkeyPatch, render: AsyncMock
) -> None:
    monkeypatch.setattr(driver, "resolve_ui_operator", AsyncMock(return_value=_operator()))
    reason = "step 'check' check call did not run: no grant for this operation"
    _patch_next(monkeypatch, side_effect=PreviousStepFailedError(reason))
    current = MagicMock(spec=CurrentStepResponse)
    monkeypatch.setattr(
        driver.RunbookRunService, "get_current_step", AsyncMock(return_value=current)
    )
    view = MagicMock(return_value="failed-view")
    monkeypatch.setattr(driver, "step_view", view)

    result = await driver._advance(MagicMock(), _session(), uuid.uuid4(), "operation_call", "")

    assert result == "rendered"
    view.assert_called_once_with(current, failed=True)
    assert render.await_args.kwargs["view"] == "failed-view"
    assert render.await_args.kwargs["failure_message"] == reason


def test_failed_step_banner_escapes_the_reason() -> None:
    """The stored reason shows in the failed-step banner, HTML-escaped."""
    step = StepBody(
        id="check",
        title="Check",
        body="",
        type="manual",
        verify=StepBodyVerify(type="confirm", prompt="ok?"),
    )
    html = (
        get_templates()
        .get_template("runbooks/_run_step.html")
        .render(
            run_id=str(uuid.uuid4()),
            csrf_token="t",
            step_view=StepView(
                step=step, body_html="", position=StepPosition(n=1, total=1), failed=True
            ),
            terminal_state=None,
            error_message=None,
            failure_message="check call did not run: <b>denied</b>",
            completed_at=None,
            abandoned_at=None,
            is_assignee=True,
            is_admin=False,
        )
    )

    assert "check call did not run: &lt;b&gt;denied&lt;/b&gt;" in html
    assert "<b>denied</b>" not in html
