# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the delivered-result wire budget (#3641)."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from fastapi.responses import JSONResponse

from meho_backplane.connectors.schemas import OperationResult, ResultHandle
from meho_backplane.operations.delivery_budget import (
    bound_complete_response,
    measure_delivered_wire_bytes,
    tool_result_text,
)

_AWKWARD: dict[str, Any] = {
    "quote": 'say "hi"',
    "backslash": "C:\\temp\\new",
    "unicode": "\u00e9t\u00e9 \u2713 \U0001f600",
    "control": "line1\nline2\ttab",
    "nested": [{"a": 1.5, "b": None, "c": True}],
}


def _body_len(value: Any) -> int:
    """Length of *value* as Starlette's JSONResponse writes it."""
    return len(JSONResponse(content=value).body)


@pytest.mark.parametrize("value", [_AWKWARD, {"rows": []}, {"x": "plain"}])
def test_measurement_matches_the_real_response_encoder(value: dict[str, Any]) -> None:
    """Each block is measured with the encoder the HTTP layer uses."""
    text = tool_result_text(value)
    assert measure_delivered_wire_bytes(value) == _body_len(text) + _body_len(value)
    assert measure_delivered_wire_bytes(value, structured=False) == _body_len(text)


def test_escaping_is_counted() -> None:
    """Quotes and non-ASCII text cost more in the text block than in the object."""
    text_only = measure_delivered_wire_bytes(_AWKWARD, structured=False)
    object_only = measure_delivered_wire_bytes(_AWKWARD) - text_only
    assert text_only > object_only
    assert text_only == len(json.dumps(json.dumps(_AWKWARD), ensure_ascii=False).encode())


def _handle() -> ResultHandle:
    return ResultHandle(
        handle_id=uuid4(),
        summary_md="# 500 rows",
        schema_={"type": "array"},
        total_rows=500,
        sample_rows=[{"k": "v"}],
        ttl_seconds=3600,
    )


def _ok(*, handle: ResultHandle | None = None, size: int = 50_000) -> OperationResult:
    return OperationResult(
        status="ok",
        op_id="vmware.vm.create",
        result={"blob": "x" * size},
        duration_ms=12.0,
        handle=handle,
        audit_id=uuid4(),
        delivery="complete",
        extras={"advisory": "kept only when in budget"},
    )


def test_in_budget_response_is_unchanged() -> None:
    result = _ok(size=10)
    assert bound_complete_response(result, 262144) is result


def test_over_budget_with_handle_is_partial() -> None:
    """Receipt and handle fit: delivery is partial, status stays ok."""
    result = _ok(handle=_handle())
    bounded = bound_complete_response(result, 4096)
    assert bounded.status == "ok"
    assert bounded.delivery == "partial"
    assert bounded.handle == result.handle
    assert bounded.audit_id == result.audit_id
    assert bounded.result is None
    assert "result_query" in bounded.extras["remediation"]
    assert "Do not re-invoke" in bounded.extras["remediation"]
    assert bounded.extras["delivered_bytes"] > 4096
    assert measure_delivered_wire_bytes(bounded.model_dump(mode="json")) <= 4096


def test_over_budget_without_room_for_handle_is_unavailable() -> None:
    """When even receipt plus handle is too large, only the receipt remains."""
    result = _ok(handle=_handle())
    bounded = bound_complete_response(result, 64)
    assert bounded.status == "ok"
    assert bounded.delivery == "unavailable"
    assert bounded.handle is None
    assert bounded.audit_id == result.audit_id
    assert bounded.result is None
    assert "audit" in bounded.extras["remediation"]


def test_over_budget_without_handle_is_unavailable() -> None:
    result = _ok()
    bounded = bound_complete_response(result, 4096)
    assert bounded.status == "ok"
    assert bounded.delivery == "unavailable"
    assert bounded.audit_id == result.audit_id


@pytest.mark.parametrize("status", ["error", "denied", "awaiting_approval"])
def test_non_ok_responses_are_not_rewritten(status: str) -> None:
    result = OperationResult(
        status=status, op_id="vmware.vm.create", error="e" * 10_000, duration_ms=1.0
    )
    assert bound_complete_response(result, 64) is result
