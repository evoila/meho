# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Structured error envelope on the caller result AND the audit payload (#2680).

Two halves, each pinned directly at its layer:

* HALF 1 (caller-facing) -- ``result_connector_error`` enriches its extras
  with ``http_status`` + the extracted ``upstream_message`` when the raised
  exception is an :exc:`httpx.HTTPStatusError` (the 404 / 429 / 5xx statuses
  the dispatcher's ``_classify_http_status_error`` leaves to the generic arm).
  Before #2680 a 5xx flattened to a bare ``connector_error`` whose only
  free-text was ``str(exc)`` -- the httpx status line, not the vendor body.

* HALF 2 (durable audit) -- ``_build_audit_payload`` threads a passed
  ``error_extras`` dict into ``payload["error"]`` so the DISPATCH audit row
  records the same envelope the caller received, not merely
  ``result_status='error'``. This is the persistence-layer pin the DoD asks
  for; the end-to-end wiring (dispatch -> audit_and_broadcast_safe ->
  write_audit_row) is exercised against a real DB in
  ``test_connectors_argocd_write_e2e.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx

from meho_backplane.operations._audit import _build_audit_payload
from meho_backplane.operations._errors import result_connector_error


def _http_status_error(status_code: int, *, json_body: dict[str, Any]) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://argocd.test/api/v1/applications/guestbook/sync")
    response = httpx.Response(status_code, json=json_body, request=request)
    return httpx.HTTPStatusError(f"{status_code} Server Error", request=request, response=response)


# ---------------------------------------------------------------------------
# HALF 1 -- result_connector_error 5xx enrichment
# ---------------------------------------------------------------------------


def test_connector_error_http_status_error_carries_upstream_body() -> None:
    """A 5xx HTTPStatusError adds http_status + the upstream body message."""
    exc = _http_status_error(
        500, json_body={"code": 13, "message": "application dry-run failed: pruning Service"}
    )
    result = result_connector_error("argocd.app.sync", exc, 1.0)

    # Top-level summary is unchanged so existing string matchers keep working.
    assert result.error == "connector_error: HTTPStatusError"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "HTTPStatusError"
    # New detail is additive and connector-agnostic.
    assert result.extras["http_status"] == 500
    assert "application dry-run failed" in result.extras["upstream_message"]


def test_connector_error_non_http_omits_http_fields() -> None:
    """A non-HTTP exception is unchanged: no http_status / upstream_message keys."""
    result = result_connector_error("op.read", RuntimeError("boom"), 1.0)
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "RuntimeError"
    assert "http_status" not in result.extras
    assert "upstream_message" not in result.extras


# ---------------------------------------------------------------------------
# HALF 2 -- _build_audit_payload error-envelope threading
# ---------------------------------------------------------------------------


def _descriptor() -> Any:
    # _build_audit_payload reads only these attributes; a namespace avoids a
    # DB-backed EndpointDescriptor for a pure-composition test.
    return SimpleNamespace(
        op_id="argocd.app.sync",
        source_kind="typed",
        product="argocd",
        version="3.x",
        impl_id="argocd-api",
    )


def test_build_audit_payload_threads_error_envelope() -> None:
    """A passed error_extras dict lands verbatim under payload['error']."""
    envelope = {
        "error_code": "connector_error",
        "http_status": 500,
        "upstream_message": "application dry-run failed: pruning Service",
    }
    payload = _build_audit_payload(
        _descriptor(),
        "params-hash",
        "error",
        error_extras=envelope,
    )
    assert payload["result_status"] == "error"
    assert payload["error"] == envelope
    # Persisted as a copy, not the caller's live dict.
    assert payload["error"] is not envelope


def test_build_audit_payload_no_error_extras_leaves_key_absent() -> None:
    """A success/non-error write carries no 'error' key."""
    payload = _build_audit_payload(_descriptor(), "params-hash", "ok")
    assert "error" not in payload


def test_build_audit_payload_empty_error_extras_leaves_key_absent() -> None:
    """An empty envelope writes no 'error' key (no empty-dict noise)."""
    payload = _build_audit_payload(
        _descriptor(),
        "params-hash",
        "error",
        error_extras={},
    )
    assert "error" not in payload
