# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved script that exits non-zero must not look like a plain success.

The run is ``ok`` when MEHO ran the script. The script's own exit code is
the result its requester cares about, so the run summary carries it, and
the console card shows it. The MCP row shows it only with the switch
``MCP_APPROVAL_EXIT_CODE`` on (off by default).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.mcp.tools.approvals import _row_to_dict
from meho_backplane.operations.approval_outcome import resume_outcome_from_envelope
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.approvals.render import project_run_envelope
from meho_backplane.ui.templating import get_templates

_OP_ID = "linux.script.run"


def _envelope(result: Any, status: str = "ok") -> dict[str, Any]:
    return {"status": status, "op_id": _OP_ID, "result": result}


@pytest.mark.parametrize("code", [0, 3, 5])
def test_summary_carries_the_exit_code(code: int) -> None:
    outcome = resume_outcome_from_envelope(_envelope({"stdout": "x", "exit_code": code}))
    assert outcome.exit_code == code
    assert outcome.result_status == "ok"


@pytest.mark.parametrize(
    "result",
    [
        None,
        "text",
        {"stdout": "x"},
        {"exit_code": None},
        {"exit_code": "3"},
        {"exit_code": True},
        {"exit_code": 3.5},
    ],
)
def test_summary_has_no_exit_code_for_an_odd_result(result: Any) -> None:
    assert resume_outcome_from_envelope(_envelope(result)).exit_code is None


def test_summary_has_no_exit_code_for_a_failed_run() -> None:
    outcome = resume_outcome_from_envelope(_envelope({"exit_code": 3}, status="error"))
    assert outcome.exit_code is None


def _card(code: int | None) -> str:
    result: dict[str, Any] = {"stdout": "x"}
    if code is not None:
        result["exit_code"] = code
    run = project_run_envelope(_envelope(result), parked_op_id=_OP_ID)
    run["polling"] = False
    run["run_id"] = None
    request = {"id": str(uuid.uuid4()), "op_id": _OP_ID}
    template = get_templates().env.get_template("approvals/_run_state.html")
    return template.render(request=request, run=run, poll_seconds=2)


def test_console_card_shows_a_non_zero_exit_code() -> None:
    html = _card(3)
    assert "ran, but it failed (exit 3)" in html
    assert 'data-exit-code="3"' in html
    assert "Request approved" not in html


@pytest.mark.parametrize("code", [0, None])
def test_console_card_stays_plain_success_for_exit_zero_or_none(code: int | None) -> None:
    html = _card(code)
    assert "Request approved" in html
    assert "approval-run-exit-code" not in html


def _mcp_row() -> dict[str, Any]:
    from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus

    row = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        principal_sub="addon:x",
        op_id=_OP_ID,
        connector_id="linux",
        status=ApprovalRequestStatus.APPROVED.value,
        resume_result=_envelope({"stdout": "x", "exit_code": 3}),
    )
    return _row_to_dict(row)


@pytest.fixture
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("MCP_APPROVAL_EXIT_CODE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("_settings_env")
def test_mcp_row_is_unchanged_with_the_switch_off() -> None:
    assert get_settings().mcp_approval_exit_code is False
    assert set(_mcp_row()["resume_outcome"]) == {
        "resumed_at",
        "result_status",
        "error_code",
        "audit_id",
        "guidance",
        "execution_outcome",
    }


@pytest.mark.usefixtures("_settings_env")
def test_mcp_row_carries_the_exit_code_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_APPROVAL_EXIT_CODE", "true")
    get_settings.cache_clear()
    assert _mcp_row()["resume_outcome"]["exit_code"] == 3
