# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vendor-call spans for typed connectors that speak HTTP through an SDK (#3841).

The generic-connector seam (:func:`.capture.record_vendor_call`) records one
``vendor_call`` span per ``httpx`` round-trip. A typed connector built on a
vendor SDK -- for example the Kubernetes connector on ``kubernetes_asyncio``,
which uses ``aiohttp`` under the hood -- never reaches that seam, so its trace
showed only the op-level ``typed`` span: no method, path, status, or response
headers for the actual vendor request.

:func:`record_sdk_vendor_call` closes that gap. A transport hook calls it once
per SDK HTTP round-trip with plain values (no ``httpx`` objects). It records
the **same** ``vendor_call`` span shape, through the **same** redaction path
(header allowlist, body caps + scrub, hard-excluded op families), plus:

* ``query`` -- the query string, with values kept only for names the
  transport lists as safe (:func:`~meho_backplane.redaction.flight_recorder.redact_query`);
* any non-secret ``extra`` attributes the transport adds (for Kubernetes: the
  response object's ``uid`` / ``resourceVersion`` / ``managedFields`` summary).

Like every capture function, it never raises and is a no-op when capture is
off (F7). Any redaction uncertainty degrades the trace to operator-only (F5).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import structlog

from meho_backplane.flight_recorder._capture_util import elapsed_ms
from meho_backplane.flight_recorder.capture import (
    SpanStart,
    _active_capture_var,
    _redact_vendor_call,
    _vendor_attributes,
)
from meho_backplane.flight_recorder.store import SpanInput
from meho_backplane.redaction.flight_recorder import redact_query

__all__ = ["record_sdk_vendor_call"]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _RequestView:
    url: str


@dataclass(frozen=True, slots=True)
class _ResponseView:
    """The ``httpx.Response`` attributes the shared redaction path reads."""

    status_code: int | None
    headers: Any
    content: bytes | None
    request: _RequestView


def record_sdk_vendor_call(
    start: SpanStart | None,
    *,
    method: str,
    url: str,
    status: str,
    request_headers: Any = None,
    request_body: Any = None,
    response_headers: Any = None,
    response_body: bytes | None = None,
    query: Any = None,
    safe_query_names: Iterable[str] = (),
    extra: dict[str, Any] | None = None,
) -> None:
    """Record one SDK-made vendor HTTP call as a ``vendor_call`` span (F7).

    *start* comes from :func:`.capture.span_start` (``None`` -> no-op).
    *url* is the request URL; its query part (if any) is dropped and only the
    redacted *query* is recorded. *status* is the HTTP status as text, or
    ``error:<ExceptionClass>`` when no response came back. Headers and bodies
    are redacted before anything is placed on the span.
    """
    scope = _active_capture_var.get()
    if start is None or scope is None:
        return
    try:
        status_code = int(status) if status.isdigit() else None
        view = _ResponseView(
            status_code=status_code,
            headers=response_headers,
            content=response_body,
            request=_RequestView(url=url),
        )
        content_type = _header(request_headers, "content-type")
        redaction, safe_url, truncated = _redact_vendor_call(
            method=method,
            request_headers=request_headers,
            response=view,
            request_body=request_body,
            request_content_type=content_type,
        )
        attributes = _vendor_attributes(redaction, method, safe_url, truncated)
        redacted_query = redact_query(query, safe_query_names)
        attributes["query"] = redacted_query.value
        if extra:
            attributes.update(extra)
        scope.add(
            SpanInput(
                span_kind="vendor_call",
                name=f"{method} {safe_url}",
                started_at=start.started_at,
                duration_ms=elapsed_ms(start.monotonic),
                status=status,
                attributes=attributes,
            )
        )
        if redaction.uncertain or redacted_query.uncertain:
            scope.redaction_uncertain = True
    except Exception:
        _log.warning("flight_recorder_sdk_vendor_span_failed", exc_info=True)


def _header(headers: Any, name: str) -> str | None:
    """Case-insensitive header read from a plain dict; never raises."""
    try:
        for key, value in headers.items():
            if str(key).lower() == name and isinstance(value, str):
                return value
    except Exception:
        return None
    return None
