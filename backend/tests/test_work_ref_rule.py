# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One rule for a ticket id (work ref) at every door."""

from __future__ import annotations

import json
import unicodedata
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import jsonschema
import pytest
import structlog.testing
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from meho_backplane.api.request_validation import request_validation_exception_handler
from meho_backplane.api.v1.broadcast import BroadcastAnnounceRequest
from meho_backplane.audit import AuditMiddleware
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools import operations as mcp_operations
from meho_backplane.mcp.tools import scheduler as _mcp_scheduler  # noqa: F401  (registers the tool)
from meho_backplane.mcp.tools.agent_runs import _run_handler
from meho_backplane.mcp.tools.broadcast import _parse_announce_claims
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations._audit import decode_work_ref_header, work_ref_var
from meho_backplane.operations.meta_tools import CallOperationBody
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate
from meho_backplane.runbooks.runs_schemas import StartRunRequest
from meho_backplane.scheduler.schemas import ScheduledTriggerCreate
from meho_backplane.ui.routes.work_ref_form import form_work_ref
from meho_backplane.work_ref import WorkRefError, normalize_work_ref, work_ref_filter_values

_NFD = unicodedata.normalize("NFD", "Änderung-7")
_NFC = unicodedata.normalize("NFC", "Änderung-7")

#: 130 composed characters written as 260 code points in NFD form: within
#: the rule, but longer than 256 before NFC.
_LONG_NFD = unicodedata.normalize("NFD", "Ä" * 130)

_ZWSP = chr(0x200B)

#: The cases shared with the Go test of the command line tool
#: (cli/internal/api/workref_test.go), so the two rules cannot drift.
_CASES_FILE = Path(__file__).resolve().parents[2] / "testdata" / "work_ref_cases.json"
_CASES = json.loads(_CASES_FILE.read_text(encoding="utf-8"))


def _text(case: dict[str, Any], key: str) -> str:
    count = case.get("repeat", 1) if key == "raw" else case.get("clean_repeat", 1)
    return str(case[key]) * int(count)


_GOOD = [pytest.param(_text(c, "raw"), _text(c, "clean"), id=c["name"]) for c in _CASES["valid"]]
_BAD = [
    pytest.param(_text(c, "raw"), c["rule"], id=c["name"])
    for c in _CASES["invalid"] + _CASES["invalid_python_only"]
]


@pytest.mark.parametrize(("raw", "expected"), _GOOD)
def test_normalize_accepts_and_cleans(raw: str, expected: str) -> None:
    assert normalize_work_ref(raw) == expected


@pytest.mark.parametrize(("raw", "rule"), _BAD)
def test_normalize_refuses_and_names_the_rule(raw: str, rule: str) -> None:
    with pytest.raises(WorkRefError) as caught:
        normalize_work_ref(raw)
    assert caught.value.rule == rule


@pytest.mark.parametrize(
    ("raw", "words"),
    [
        ("", "must not be empty"),
        ("CHG\x001", "control or invisible characters (found U+0000)"),
        (_ZWSP.join(["a", "b"]), "(found U+200B)"),
        ("a" * 257, "at most 256 characters"),
    ],
)
def test_the_message_names_the_rule_and_never_the_value(raw: str, words: str) -> None:
    with pytest.raises(WorkRefError) as caught:
        normalize_work_ref(raw)
    assert words in str(caught.value)
    assert raw not in str(caught.value) or raw == ""


def test_length_is_counted_after_nfc() -> None:
    # 256 composed characters written as 512 code points in NFD form.
    assert normalize_work_ref(unicodedata.normalize("NFD", "Ä" * 256)) == "Ä" * 256


def test_a_search_filter_matches_the_cleaned_id_and_the_old_text() -> None:
    assert work_ref_filter_values("CHG1") == ("CHG1",)
    assert work_ref_filter_values("  CHG1  ") == ("CHG1", "  CHG1  ")
    nfd = unicodedata.normalize("NFD", "Ä-1")
    assert work_ref_filter_values(nfd) == ("Ä-1", nfd)
    # A console filter box does not check the rule: its text matches exactly.
    assert work_ref_filter_values("CHG\t1") == ("CHG\t1",)


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


@pytest.mark.parametrize(
    ("tool", "path"),
    [
        ("meho_broadcast_announce", ("work_ref",)),
        ("meho_scheduler_create", ("work_ref",)),
    ],
)
def test_mcp_schema_counts_length_after_nfc(tool: str, path: tuple[str, ...]) -> None:
    """The MCP input check leaves the length to the shared rule."""
    entry = mcp_registry.get_tool(tool)
    assert entry is not None
    schema = entry[0].inputSchema["properties"]
    for key in path:
        schema = schema[key]
    jsonschema.validate(_LONG_NFD, schema, cls=jsonschema.Draft202012Validator)


def test_long_nfd_id_passes_the_mcp_and_model_doors() -> None:
    nfc = "Ä" * 130
    assert _parse_announce_claims({"work_ref": _LONG_NFD}).work_ref == nfc
    assert _scheduler(_LONG_NFD).work_ref == nfc
    assert form_work_ref(_LONG_NFD) == nfc


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
    for blank in (b"", b"   "):
        with pytest.raises(WorkRefError, match="empty"):
            decode_work_ref_header(blank)
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


@pytest.mark.parametrize("blank", ["", "   "])
def test_header_door_refuses_a_blank_value(blank: str) -> None:
    """A header that is sent but blank is refused; only a missing header means no ticket."""
    reached: list[str | None] = []
    client = _app_with_middleware(reached)
    response = client.get("/mcp", headers={"Meho-Work-Ref": blank})
    assert response.status_code == 400
    assert "must not be empty" in response.json()["detail"]
    assert reached == []


@pytest.mark.parametrize(
    ("sent", "rule"),
    [
        (b"   ", "empty"),
        (b"CHG\tHIDDEN-9", "invisible_character"),
        (b"HIDDEN-" + b"a" * 257, "too_long"),
    ],
)
def test_refused_header_leaves_a_log_line_without_the_value(sent: bytes, rule: str) -> None:
    """The log line has the request id, the route and the rule, never the value."""

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("the request must not run")

    client = TestClient(RequestContextMiddleware(AuditMiddleware(inner)))
    request_id = uuid.uuid4().hex
    with structlog.testing.capture_logs() as logs:
        response = client.get(
            "/api/v1/thing", headers={"Meho-Work-Ref": sent.decode(), "X-Request-Id": request_id}
        )
    assert response.status_code == 400
    (line,) = [entry for entry in logs if entry["event"] == "work_ref_header_refused"]
    assert line["rule"] == rule
    assert line["route"] == "/api/v1/thing"
    assert line["method"] == "GET"
    assert uuid.UUID(line["request_id"]).hex == request_id
    assert "HIDDEN" not in str(logs)


def test_header_door_without_header_binds_nothing() -> None:
    reached: list[str | None] = []
    client = _app_with_middleware(reached)
    assert client.get("/mcp").status_code == 200
    assert reached == [None]


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


# ---------------------------------------------------------------- unencodable input


def _body_app() -> TestClient:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)

    @app.post("/call")
    async def _call(body: CallOperationBody) -> dict[str, str | None]:
        return {"work_ref": body.work_ref}

    return TestClient(app, raise_server_exceptions=False)


def test_body_with_a_lone_surrogate_gets_a_safe_422() -> None:
    """A lone surrogate is valid JSON but cannot be sent back as UTF-8."""
    response = _body_app().post(
        "/call",
        content=b'{"connector_id": "c", "op_id": "o", "work_ref": "\\ud800CHG"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "work_ref"]
    assert "must not contain control or invisible characters" in error["msg"]
    assert "U+D800" in error["msg"]
    assert "input" not in error


def test_body_422_does_not_show_the_refused_ticket_id() -> None:
    """The 422 names the rule and the code point, like the header and MCP answers."""
    sent = "CHG-hidden" + _ZWSP + "-7"
    response = _body_app().post("/call", json={"connector_id": "c", "op_id": "o", "work_ref": sent})
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "work_ref"]
    assert "U+200B" in error["msg"]
    assert "input" not in error
    assert "CHG-hidden" not in response.text


def test_body_422_for_a_blank_ticket_id_names_the_rule() -> None:
    response = _body_app().post("/call", json={"connector_id": "c", "op_id": "o", "work_ref": " "})
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert "input" not in error
    assert "must not be empty" in error["msg"]


def test_body_422_keeps_the_input_of_other_fields() -> None:
    response = _body_app().post("/call", json={"connector_id": 7, "op_id": "o"})
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "connector_id"]
    assert error["input"] == 7


def test_app_uses_the_safe_422_handler() -> None:
    from meho_backplane.main import app

    assert app.exception_handlers[RequestValidationError] is request_validation_exception_handler
