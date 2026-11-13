# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One rule for a ticket id (work ref) at every door."""

from __future__ import annotations

import unicodedata
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from meho_backplane.api.v1.broadcast import BroadcastAnnounceRequest
from meho_backplane.audit import AuditMiddleware
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools import operations as mcp_operations
from meho_backplane.mcp.tools.agent_runs import _run_handler
from meho_backplane.mcp.tools.broadcast import _parse_announce_claims
from meho_backplane.operations._audit import decode_work_ref_header, work_ref_var
from meho_backplane.operations.meta_tools import CallOperationBody
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate
from meho_backplane.runbooks.runs_schemas import StartRunRequest
from meho_backplane.scheduler.schemas import ScheduledTriggerCreate
from meho_backplane.ui.routes.work_ref_form import form_work_ref
from meho_backplane.work_ref import WorkRefError, normalize_work_ref

_NFD = unicodedata.normalize("NFD", "Änderung-7")
_NFC = unicodedata.normalize("NFC", "Änderung-7")

_ZWSP = chr(0x200B)
_RLO = chr(0x202E)
_LSEP = chr(0x2028)

_GOOD = [
    ("CHG0012345", "CHG0012345"),
    ("  CHG1  ", "CHG1"),
    ("\tCHG1\n", "CHG1"),
    ("gh:evoila/meho#13", "gh:evoila/meho#13"),
    ("a" * 256, "a" * 256),
    (_NFD, _NFC),
    ("change 12", "change 12"),
]

_BAD = [
    ("", "empty"),
    ("   ", "empty"),
    ("\t\n", "empty"),
    ("CHG\x001", "control"),
    ("CHG1\x00", "control"),
    ("CHG\n1", "control"),
    ("CHG\t1", "control"),
    ("CHG" + _ZWSP + "1", "control"),
    ("CHG" + _RLO + "1", "control"),
    ("CHG" + _LSEP + "1", "control"),
    ("\ud800CHG", "control"),
    ("a" * 257, "at most 256"),
]


@pytest.mark.parametrize(("raw", "expected"), _GOOD)
def test_normalize_accepts_and_cleans(raw: str, expected: str) -> None:
    assert normalize_work_ref(raw) == expected


@pytest.mark.parametrize(("raw", "word"), _BAD)
def test_normalize_refuses_and_names_the_rule(raw: str, word: str) -> None:
    with pytest.raises(WorkRefError, match=word):
        normalize_work_ref(raw)


def test_length_is_counted_after_nfc() -> None:
    # 256 composed characters written as 512 code points in NFD form.
    assert normalize_work_ref(unicodedata.normalize("NFD", "Ä" * 256)) == "Ä" * 256


# ---------------------------------------------------------------- doors


def _run_approval(work_ref: str) -> RunApprovalCreate:
    return RunApprovalCreate(
        work_ref=work_ref,
        entries=[{"connector_id": "c", "op_id": "o"}],
        reason="r",
    )


def _call_body(work_ref: str) -> CallOperationBody:
    return CallOperationBody(connector_id="c", op_id="o", work_ref=work_ref)


def _runbook_start(work_ref: str) -> StartRunRequest:
    return StartRunRequest(template_slug="s", target="t", work_ref=work_ref)


def _scheduler(work_ref: str) -> ScheduledTriggerCreate:
    return ScheduledTriggerCreate(
        kind="cron",
        agent_definition_id="11111111-1111-1111-1111-111111111111",
        cron_expr="* * * * *",
        inputs={"prompt": "go"},
        work_ref=work_ref,
    )


def _announce_body(work_ref: str) -> BroadcastAnnounceRequest:
    return BroadcastAnnounceRequest(activity="x", work_ref=work_ref)


_MODEL_DOORS: list[Any] = [_run_approval, _call_body, _runbook_start, _scheduler, _announce_body]


@pytest.mark.parametrize("door", _MODEL_DOORS)
def test_model_doors_clean_and_refuse(door: Any) -> None:
    assert door("  CHG1 ").work_ref == "CHG1"
    for raw in ("", "CHG\x001", "CHG" + _ZWSP + "1", "a" * 257):
        with pytest.raises(ValidationError):
            door(raw)


def test_mcp_announce_door() -> None:
    assert _parse_announce_claims({"work_ref": " CHG1 "}).work_ref == "CHG1"
    with pytest.raises(McpInvalidParamsError):
        _parse_announce_claims({"work_ref": "CHG\n1"})


def test_console_form_door() -> None:
    assert form_work_ref(None) is None
    assert form_work_ref("   ") is None
    assert form_work_ref(" CHG1 ") == "CHG1"
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        form_work_ref("CHG\x001")
    assert caught.value.status_code == 422


async def test_mcp_agent_run_door_refuses_before_the_run() -> None:
    with pytest.raises(McpInvalidParamsError):
        await _run_handler(
            object(),  # type: ignore[arg-type]
            {"name": "a", "input": "x", "work_ref": "CHG" + _ZWSP + "1"},
        )


async def test_mcp_call_operation_refuses_before_anything_runs() -> None:
    with (
        patch.object(mcp_operations, "call_operation", new=AsyncMock()) as call,
        patch.object(mcp_operations, "recorded_call", new=AsyncMock()) as recorded,
        pytest.raises(McpInvalidParamsError) as caught,
    ):
        await mcp_operations._call_operation_handler(
            object(),  # type: ignore[arg-type]
            {"connector_id": "c", "op_id": "o", "work_ref": "CHG\x001"},
        )
    assert caught.value.data == {"reason": "invalid_work_ref"}
    call.assert_not_called()
    recorded.assert_not_called()


def _fake_settings() -> SimpleNamespace:
    return SimpleNamespace(result_complete_response_wire_max_bytes=1)


async def test_mcp_call_operation_cleans_work_ref() -> None:
    seen: list[dict[str, Any]] = []

    async def _recorded(
        _op: object, arguments: dict[str, Any], *, transport: str, run: Any
    ) -> dict[str, Any]:
        seen.append(arguments)
        return {"status": "ok"}

    with (
        patch.object(mcp_operations, "recorded_call", new=_recorded),
        patch.object(mcp_operations, "with_recall", new=AsyncMock(return_value={})),
        patch.object(
            mcp_operations,
            "get_settings",
            new=lambda: SimpleNamespace(result_complete_response_wire_max_bytes=1),
        ),
    ):
        await mcp_operations._call_operation_handler(
            object(),  # type: ignore[arg-type]
            {"connector_id": "c", "op_id": "o", "work_ref": "  CHG1 "},
        )
    assert seen[0]["work_ref"] == "CHG1"


# ---------------------------------------------------------------- header


def test_header_decode_follows_the_rule() -> None:
    assert decode_work_ref_header(b"  CHG1 ") == "CHG1"
    assert decode_work_ref_header(b"   ") is None
    assert decode_work_ref_header("Änderung-7".encode()) == "Änderung-7"
    with pytest.raises(WorkRefError):
        decode_work_ref_header(b"CHG\x001")


def _app_with_middleware(reached: list[str | None]) -> TestClient:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        reached.append(work_ref_var.get())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return TestClient(AuditMiddleware(inner))


def test_header_door_refuses_before_the_request_runs() -> None:
    reached: list[str | None] = []
    client = _app_with_middleware(reached)
    response = client.get("/x", headers={"Meho-Work-Ref": "CHG\tONE"})
    assert response.status_code == 400
    assert "work_ref" in response.json()["detail"]
    assert reached == []


def test_header_door_refuses_a_long_value() -> None:
    reached: list[str | None] = []
    client = _app_with_middleware(reached)
    response = client.get("/x", headers={"Meho-Work-Ref": "a" * 257})
    assert response.status_code == 400
    assert reached == []


def test_header_door_binds_the_cleaned_value() -> None:
    reached: list[str | None] = []
    client = _app_with_middleware(reached)
    # ``/mcp`` skips the audit row, so this test needs no database.
    assert client.get("/mcp", headers={"Meho-Work-Ref": "  CHG1 "}).status_code == 200
    assert reached == ["CHG1"]


# ---------------------------------------------------------------- one id, every door


def test_one_ticket_id_is_the_same_text_at_every_door() -> None:
    sent = "  CHG1 "
    texts = {
        _run_approval(sent).work_ref,
        _call_body(sent).work_ref,
        _runbook_start(sent).work_ref,
        _scheduler(sent).work_ref,
        decode_work_ref_header(sent.encode()),
        form_work_ref(sent),
        _parse_announce_claims({"work_ref": sent}).work_ref,
    }
    assert texts == {"CHG1"}
