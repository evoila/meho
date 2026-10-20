# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flight-recorder span for a vendor call that raises a transport fault (#3408).

A timeout or a refused connection has no HTTP response, so
:func:`capture.record_vendor_call` never runs. The shared httpx seam records an
error span instead and re-raises, so the trace shows where the time went. These
tests drive the real seam (``HttpConnector._request_json`` / ``_post_json``)
against respx and check that the success / 4xx / 5xx spans do not change.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.flight_recorder import capture
from meho_backplane.flight_recorder._capture_util import transport_fault_phase
from meho_backplane.operations._errors import result_connector_timeout
from tests.test_flight_recorder_capture import (
    _BASE_URL,
    _ConcreteHttpConnector,
    _descriptor,
    _fetch_trace,
    _make_operator,
    _make_target,
    _required_settings_env,  # noqa: F401 -- autouse fixture
    _seed_tenant,
    span_json,
)


async def _run_in_scope(slug: str, call: Any) -> tuple[uuid.UUID, BaseException | None, Any]:
    """Run *call(conn, operator, target)* inside a capture scope.

    Returns the audit id, the raised exception (or ``None``), and the result.
    """
    tenant_id = await _seed_tenant(slug=slug)
    conn = _ConcreteHttpConnector()
    operator = _make_operator(tenant_id)
    target = _make_target()
    audit_id = uuid.uuid4()
    handle = await capture.begin_dispatch_capture(
        audit_id=audit_id,
        operator=operator,
        target=target,
        descriptor=_descriptor("svc.items.op"),
        connector_id="test-http-1.x",
    )
    raised: BaseException | None = None
    result: Any = None
    try:
        result = await call(conn, operator, target)
    except Exception as exc:
        raised = exc
    finally:
        await capture.end_dispatch_capture(handle)
        await conn.aclose()
    return audit_id, raised, result


@pytest.mark.asyncio
async def test_read_timeout_records_one_error_span_and_reraises() -> None:
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/api/items").mock(side_effect=httpx.ReadTimeout("timed out"))
        audit_id, raised, _ = await _run_in_scope(
            "fr-fault-read",
            lambda conn, op, tgt: conn._request_json(
                tgt, "GET", "/api/items", operator=op, params={"token": "sekret"}
            ),
        )

    assert isinstance(raised, httpx.ReadTimeout)
    _, spans = await _fetch_trace(audit_id)
    assert len(spans) == 1
    span = spans[0]
    assert span.span_kind == "vendor_call"
    assert span.status == "error"
    assert span.name == "GET https://vcenter.example.com/api/items"
    assert span.duration_ms is not None and span.duration_ms > 0
    assert span.attributes["exception_class"] == "ReadTimeout"
    assert span.attributes["phase"] == "read"
    assert span.attributes["method"] == "GET"
    assert span.attributes["op_id"] == "svc.items.op"
    # No query string, header, or body reaches the span.
    assert "sekret" not in span_json(span)
    assert "tok-secret" not in span_json(span)
    assert "request_headers" not in span.attributes
    assert "request_body" not in span.attributes


@pytest.mark.asyncio
async def test_connect_error_on_post_records_transport_phase() -> None:
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post("/api/items").mock(side_effect=httpx.ConnectError("refused"))
        audit_id, raised, _ = await _run_in_scope(
            "fr-fault-connect",
            lambda conn, op, tgt: conn._post_json(
                tgt, "/api/items", operator=op, json={"password": "hunter2"}
            ),
        )

    assert isinstance(raised, httpx.ConnectError)
    _, spans = await _fetch_trace(audit_id)
    assert len(spans) == 1
    span = spans[0]
    assert span.status == "error"
    assert span.name == "POST https://vcenter.example.com/api/items"
    assert span.attributes["exception_class"] == "ConnectError"
    assert span.attributes["phase"] == "transport"
    assert "hunter2" not in span_json(span)


@pytest.mark.asyncio
async def test_4xx_and_5xx_spans_unchanged() -> None:
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/api/missing").respond(404, json={"error": "nope"})
        audit_404, raised_404, _ = await _run_in_scope(
            "fr-fault-404",
            lambda conn, op, tgt: conn._request_json(tgt, "GET", "/api/missing", operator=op),
        )
        mock.post("/api/items").respond(503, json={"error": "down"})
        audit_503, raised_503, _ = await _run_in_scope(
            "fr-fault-503",
            lambda conn, op, tgt: conn._post_json(tgt, "/api/items", operator=op, json={}),
        )

    assert isinstance(raised_404, httpx.HTTPStatusError)
    assert isinstance(raised_503, httpx.HTTPStatusError)
    for audit_id, code in ((audit_404, "404"), (audit_503, "503")):
        _, spans = await _fetch_trace(audit_id)
        assert len(spans) == 1
        assert spans[0].status == code
        assert "exception_class" not in spans[0].attributes


@pytest.mark.asyncio
async def test_success_span_unchanged() -> None:
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/api/items").respond(200, json={"items": [1]})
        audit_id, raised, result = await _run_in_scope(
            "fr-fault-ok",
            lambda conn, op, tgt: conn._request_json(tgt, "GET", "/api/items", operator=op),
        )

    assert raised is None
    assert result == {"items": [1]}
    _, spans = await _fetch_trace(audit_id)
    assert len(spans) == 1
    assert spans[0].status == "200"
    assert "exception_class" not in spans[0].attributes


@pytest.mark.asyncio
async def test_recorder_failure_never_masks_the_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """F7: a broken recorder drops the span; the original fault still raises."""

    def _boom(_span: Any) -> int:
        raise RuntimeError("caps module down")

    monkeypatch.setattr(capture, "approx_span_bytes", _boom)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/api/items").mock(side_effect=httpx.ReadTimeout("timed out"))
        audit_id, raised, _ = await _run_in_scope(
            "fr-fault-f7",
            lambda conn, op, tgt: conn._request_json(tgt, "GET", "/api/items", operator=op),
        )

    assert isinstance(raised, httpx.ReadTimeout)
    _, spans = await _fetch_trace(audit_id)
    assert spans == []


def test_record_error_is_noop_without_capture() -> None:
    capture.record_vendor_call_error(None, method="GET", exc=httpx.ReadTimeout("x"))


def test_error_span_url_falls_back_when_no_request_attached() -> None:
    """An exception with no request attached must not raise into the seam."""
    scope = capture._CaptureScope(audit_id=uuid.uuid4(), tenant_id=uuid.uuid4(), target_id=None)
    token = capture._active_capture_var.set(scope)
    try:
        start = capture.span_start()
        capture.record_vendor_call_error(start, method="GET", exc=httpx.ConnectError("x"))
    finally:
        capture._active_capture_var.reset(token)
    assert len(scope.spans) == 1
    assert scope.spans[0].attributes["url"] == "?"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("x"),
        httpx.ReadTimeout("x"),
        httpx.WriteTimeout("x"),
        httpx.PoolTimeout("x"),
        httpx.ConnectError("x"),
        httpx.ReadError("x"),
    ],
)
def test_phase_matches_connector_timeout_envelope(exc: httpx.TransportError) -> None:
    """The span phase and the ``connector_timeout`` error name the same phase."""
    envelope = result_connector_timeout("svc.op", exc, 1.0)
    assert transport_fault_phase(exc) == envelope.extras["phase"]
