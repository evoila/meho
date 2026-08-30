# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure helpers for the flight-recorder capture seam (#3214).

Split out of :mod:`meho_backplane.flight_recorder.capture` so that module
stays focused on the scope lifecycle + the three capture entry points. Every
function here is side-effect-free (beyond reading structlog's ambient
contextvars) and **never raises** -- the capture seam's F7 best-effort contract
depends on it. F3's per-span body cap lives here (:func:`cap_body`); the
per-trace and span-count caps live on the scope in :mod:`.capture`.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import msgspec
import structlog

from meho_backplane.flight_recorder.store import SpanInput

__all__ = [
    "MAX_SPAN_BODY_BYTES",
    "approx_bytes",
    "approx_span_bytes",
    "as_mapping",
    "cap_body",
    "coerce_uuid",
    "elapsed_ms",
    "header_value",
    "now_utc",
    "request_id",
    "response_content",
    "safe_url",
    "serialize",
]

#: Per-span body cap (F3). A request/response body larger than this truncates
#: (marker + forced redaction-uncertainty); it never errors a dispatch.
MAX_SPAN_BODY_BYTES: Final[int] = 64 * 1024


def now_utc() -> datetime:
    return datetime.now(UTC)


def cap_body(body: Any) -> tuple[Any, bool]:
    """Cap *body* at :data:`MAX_SPAN_BODY_BYTES`; return ``(body, truncated)``.

    Oversize truncates (never errors, F3). A truncated body is handed to the
    redaction engine as raw bytes with ``truncated=True`` so it fails closed to
    a redaction-uncertain omission -- a secret split across the cut is
    unverifiable.
    """
    if body is None:
        return None, False
    if isinstance(body, (bytes, bytearray)):
        if len(body) > MAX_SPAN_BODY_BYTES:
            return bytes(body[:MAX_SPAN_BODY_BYTES]), True
        return bytes(body), False
    if isinstance(body, str):
        encoded = body.encode("utf-8", "replace")
        if len(encoded) > MAX_SPAN_BODY_BYTES:
            return encoded[:MAX_SPAN_BODY_BYTES], True
        return body, False
    encoded = serialize(body)
    if len(encoded) > MAX_SPAN_BODY_BYTES:
        return encoded[:MAX_SPAN_BODY_BYTES], True
    return body, False


def response_content(response: Any) -> bytes | None:
    """Return the response body bytes, or ``None`` if unreadable."""
    try:
        content = response.content
    except Exception:
        return None
    return bytes(content) if isinstance(content, (bytes, bytearray)) else None


def header_value(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:
        return None
    return value if isinstance(value, str) else None


def as_mapping(headers: Any) -> Any:
    """Coerce an ``httpx.Headers`` (or ``None``) into a plain dict.

    The redactor fails closed on a non-mapping, so a plain ``dict`` is passed
    when possible and the raw object otherwise (the redactor handles it).
    """
    if headers is None:
        return None
    try:
        return dict(headers)
    except Exception:
        return headers


def safe_url(url: Any) -> str:
    """Return ``scheme://host[:port]/path`` -- no query, userinfo, or fragment.

    Query strings and userinfo can carry tokens / signed-URL material, and the
    redaction engine has no URL redactor, so the capture seam strips them here:
    only the non-secret request line survives.
    """
    try:
        scheme = getattr(url, "scheme", None)
        host = getattr(url, "host", None)
        path = getattr(url, "path", None)
        if scheme and host:
            port = getattr(url, "port", None)
            netloc = host if port in (None, 80, 443) else f"{host}:{port}"
            return f"{scheme}://{netloc}{path or ''}"
        return str(url).split("?", 1)[0].split("#", 1)[0]
    except Exception:
        return "?"


def coerce_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def request_id() -> str | None:
    """The ambient ``request_id`` correlation key, if bound (middleware)."""
    try:
        value = structlog.contextvars.get_contextvars().get("request_id")
    except Exception:
        return None
    return value if isinstance(value, str) else None


def elapsed_ms(monotonic_start: float) -> Decimal:
    return Decimal(str(round((time.monotonic() - monotonic_start) * 1000, 2)))


def serialize(payload: Any) -> bytes:
    """Serialize *payload* to JSON bytes, never raising (mirrors the reducer)."""
    try:
        return msgspec.json.encode(payload)
    except (TypeError, msgspec.EncodeError):
        return str(payload).encode("utf-8", "replace")


def approx_bytes(payload: Any) -> int:
    return len(serialize(payload))


def approx_span_bytes(span: SpanInput) -> int:
    """Cheap upper-ish estimate of a span's persisted size for the trace cap."""
    return len(span.name) + approx_bytes(span.attributes)
