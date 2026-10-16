# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the delivered-result wire budget (#3641)."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from fastapi.responses import JSONResponse
from structlog.testing import capture_logs

from meho_backplane.connectors.schemas import (
    DrillInUnavailableReason,
    FetchMore,
    FetchMoreDrillIn,
    FetchMoreNativePagination,
    OperationResult,
    ResultHandle,
)
from meho_backplane.operations.delivery_budget import (
    RESULT_COMPLETE_RESPONSE_WIRE_MIN_BYTES,
    bound_complete_response,
    handle_is_readable,
    measure_delivered_wire_bytes,
    receipt_wire_bytes,
    refuse_undeliverable_receipt,
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


def _fetch_more(drill_in: FetchMoreDrillIn) -> FetchMore:
    return FetchMore(
        drill_in=drill_in,
        native_pagination=FetchMoreNativePagination(
            available=False, rationale="op has no pagination hint"
        ),
    )


def _readable_drill_in() -> FetchMoreDrillIn:
    return FetchMoreDrillIn(
        available=True,
        rationale="Call result_query with the handle id.",
        mcp_tool="result_query",
        example_call={"tool": "result_query", "args": {"offset": 0, "limit": 50}},
    )


def _unreadable_drill_in(reason: DrillInUnavailableReason) -> FetchMoreDrillIn:
    return FetchMoreDrillIn(
        available=False, rationale="Re-call with narrower params.", reason=reason
    )


def _handle(*, fetch_more: FetchMore | None = None, summary_size: int = 10) -> ResultHandle:
    """A handle as the reducer mints it: stored rows, readable through ``result_query``."""
    return ResultHandle(
        handle_id=uuid4(),
        summary_md="#" * summary_size,
        schema_={"type": "array"},
        total_rows=500,
        sample_rows=[{"k": "v"}],
        ttl_seconds=3600,
        fetch_more=fetch_more or _fetch_more(_readable_drill_in()),
    )


def _ok(
    *, handle: ResultHandle | None = None, size: int = 50_000, op_id: str = "vmware.vm.create"
) -> OperationResult:
    return OperationResult(
        status="ok",
        op_id=op_id,
        result={"blob": "x" * size},
        duration_ms=12.0,
        handle=handle,
        audit_id=uuid4(),
        delivery="complete",
        extras={"advisory": "kept only when in budget"},
    )


_LIMIT = RESULT_COMPLETE_RESPONSE_WIRE_MIN_BYTES


def test_in_budget_response_is_unchanged() -> None:
    result = _ok(size=10)
    assert bound_complete_response(result, 262144) is result


def test_over_budget_with_readable_handle_is_partial() -> None:
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
    result = _ok(handle=_handle(summary_size=4096))
    bounded = bound_complete_response(result, _LIMIT)
    assert bounded.status == "ok"
    assert bounded.delivery == "unavailable"
    assert bounded.handle is None
    assert bounded.audit_id == result.audit_id
    assert bounded.result is None
    assert "audit" in bounded.extras["remediation"]
    assert measure_delivered_wire_bytes(bounded.model_dump(mode="json")) <= _LIMIT


@pytest.mark.parametrize(
    "reason", ["result_store_unavailable", "no_tenant_context", "admission_limit_exceeded"]
)
def test_over_budget_with_unreadable_handle_is_unavailable(
    reason: DrillInUnavailableReason,
) -> None:
    """A handle whose rows were never stored cannot be read; no result_query advice."""
    handle = _handle(fetch_more=_fetch_more(_unreadable_drill_in(reason)))
    assert not handle_is_readable(handle)
    bounded = bound_complete_response(_ok(handle=handle), 4096)
    assert bounded.status == "ok"
    assert bounded.delivery == "unavailable"
    assert bounded.handle is None
    assert "result_query" not in bounded.extras["remediation"]
    assert "audit" in bounded.extras["remediation"]
    assert "Do not re-invoke" in bounded.extras["remediation"]


def test_handle_without_fetch_more_is_not_readable() -> None:
    """Only the reducer's drill-in proves a stored result; a bare handle does not."""
    assert not handle_is_readable(None)
    assert not handle_is_readable(_handle(fetch_more=None).model_copy(update={"fetch_more": None}))
    assert handle_is_readable(_handle())


def test_over_budget_without_handle_is_unavailable() -> None:
    result = _ok()
    bounded = bound_complete_response(result, 4096)
    assert bounded.status == "ok"
    assert bounded.delivery == "unavailable"
    assert bounded.audit_id == result.audit_id


def test_unavailable_response_fits_the_floor() -> None:
    """The receipt fits the smallest allowed ceiling with a 256-character ASCII op_id."""
    result = _ok(op_id="x" * 256, size=1_000_000)
    bounded = bound_complete_response(result, _LIMIT)
    assert bounded.delivery == "unavailable"
    assert measure_delivered_wire_bytes(bounded.model_dump(mode="json")) <= _LIMIT


_AWKWARD_OP_IDS = [
    "vmware.vm.create",
    "x" * 256,
    "\u00e9" * 256,
    "\U0001f600" * 64,
    '"\\' * 128,
    "line\nbreak\ttab" * 20,
]


@pytest.mark.parametrize("op_id", _AWKWARD_OP_IDS)
def test_receipt_wire_bytes_bounds_the_delivered_receipt(op_id: str) -> None:
    """The pre-dispatch measurement is never smaller than the receipt a run sends."""
    for audit_id in (uuid4(), None):
        result = _ok(op_id=op_id, size=1_000_000).model_copy(update={"audit_id": audit_id})
        bounded = bound_complete_response(result, _LIMIT)
        assert bounded.delivery == "unavailable"
        delivered = measure_delivered_wire_bytes(bounded.model_dump(mode="json"))
        assert delivered <= receipt_wire_bytes(op_id, _LIMIT)


def test_receipt_wire_bytes_counts_escaping() -> None:
    """Non-ASCII text is escaped twice in the text block, so it costs more than ASCII."""
    assert receipt_wire_bytes("\u00e9" * 256, _LIMIT) > receipt_wire_bytes("x" * 256, _LIMIT)


def test_realistic_op_id_is_not_refused_at_the_floor() -> None:
    """A 256-character ASCII op_id fits the floor, so the call runs."""
    assert receipt_wire_bytes("x" * 256, _LIMIT) <= _LIMIT
    assert refuse_undeliverable_receipt("x" * 256, _LIMIT) is None
    assert refuse_undeliverable_receipt("vmware.vm.create", _LIMIT) is None


def test_non_ascii_op_id_over_the_floor_is_refused_before_the_run() -> None:
    """The review's case: 256 non-ASCII characters push the receipt past the floor.

    The call is refused with an error envelope and nothing runs. At the
    default ceiling the same op_id fits and is not refused.
    """
    op_id = "\u00e9" * 256
    refusal = refuse_undeliverable_receipt(op_id, _LIMIT)
    assert refusal is not None
    assert refusal.status == "error"
    assert refusal.op_id == op_id
    assert refusal.audit_id is None
    assert refusal.delivery is None
    assert refusal.result is None
    assert refusal.extras["error_code"] == "receipt_too_large"
    assert refusal.extras["receipt_bytes"] == receipt_wire_bytes(op_id, _LIMIT)
    assert refusal.extras["receipt_bytes"] > _LIMIT
    assert refusal.extras["delivery_max_bytes"] == _LIMIT
    assert refusal.error is not None
    assert refusal.error.startswith("receipt_too_large:")
    assert "did not run" in refusal.error
    assert op_id not in refusal.error
    assert "safe to call again" in refusal.extras["remediation"]
    assert refuse_undeliverable_receipt(op_id, 262144) is None


def test_receipt_over_ceiling_is_still_delivered_and_logged() -> None:
    """Defensive branch for a caller that skipped the pre-check.

    ``call_operation`` refuses such an op_id before the run, so this branch
    is unreachable through it. Called directly, the receipt still ships with
    a warning, because dropping the audit_id of a completed write is worse.
    """
    result = _ok(op_id="x" * 4096)
    assert refuse_undeliverable_receipt(result.op_id, _LIMIT) is not None
    with capture_logs() as logs:
        bounded = bound_complete_response(result, _LIMIT)
    assert bounded.status == "ok"
    assert bounded.delivery == "unavailable"
    assert bounded.audit_id == result.audit_id
    assert bounded.op_id == result.op_id
    assert measure_delivered_wire_bytes(bounded.model_dump(mode="json")) > _LIMIT
    warnings = [entry for entry in logs if entry["event"] == "delivery_receipt_over_ceiling"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["delivery_max_bytes"] == _LIMIT


@pytest.mark.parametrize("status", ["error", "denied", "awaiting_approval"])
def test_non_ok_responses_are_not_rewritten(status: str) -> None:
    result = OperationResult(
        status=status, op_id="vmware.vm.create", error="e" * 10_000, duration_ms=1.0
    )
    assert bound_complete_response(result, _LIMIT) is result
