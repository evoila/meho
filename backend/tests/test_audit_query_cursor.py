# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the opaque forward-only cursor (G8.1-T1).

Covers:

* Encode/decode round-trip preserves the ``(ts, id)`` pair exactly.
* URL-safe base64 — no ``+`` / ``/`` characters in the encoded token, so it
  survives unencoded as a query-string value.
* Tamper rejection: malformed base64, malformed JSON, missing fields, bad
  ISO-8601, bad UUID — each raises :class:`InvalidCursorError` with the
  underlying exception preserved as ``__cause__``.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from meho_backplane.audit_query import (
    CursorPosition,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
)


def test_cursor_round_trip_preserves_ts_and_id() -> None:
    """Encode → decode produces the same ``(ts, id)`` pair byte-for-byte."""
    pos = CursorPosition(ts=datetime(2026, 5, 14, 12, 34, 56, tzinfo=UTC), id=uuid.uuid4())
    decoded = decode_cursor(encode_cursor(pos))

    assert decoded.ts == pos.ts
    assert decoded.id == pos.id


def test_cursor_encoded_is_urlsafe() -> None:
    """The wire format avoids ``+`` / ``/`` so the token is query-string-safe."""
    pos = CursorPosition(ts=datetime.now(UTC), id=uuid.uuid4())
    token = encode_cursor(pos)

    assert "+" not in token
    assert "/" not in token


def test_cursor_distinct_positions_encode_distinctly() -> None:
    """Different ``(ts, id)`` pairs produce different tokens."""
    now = datetime.now(UTC)
    pos_a = CursorPosition(ts=now, id=uuid.uuid4())
    pos_b = CursorPosition(ts=now + timedelta(seconds=1), id=pos_a.id)

    assert encode_cursor(pos_a) != encode_cursor(pos_b)


def test_cursor_rejects_invalid_base64() -> None:
    """Random non-base64 bytes raise :class:`InvalidCursorError`."""
    with pytest.raises(InvalidCursorError):
        decode_cursor("not%%base64$$")


def test_cursor_strict_base64_rejects_trailing_garbage() -> None:
    """A valid base64 prefix followed by non-base64 garbage is rejected.

    Python's lenient ``base64.urlsafe_b64decode`` silently discards
    non-base64 bytes — e.g. ``aGVsbG8=!`` decodes to ``b"hello"`` without
    raising — which the docstring's "any tampering ... InvalidCursorError"
    contract forbids. The strict-validate decoder catches this.
    """
    # Valid b64 (``aGVsbG8=`` = "hello") + trailing ``!`` which is not in the
    # urlsafe-base64 alphabet. The lax decoder accepts and returns b"hello";
    # strict validation rejects.
    with pytest.raises(InvalidCursorError):
        decode_cursor("aGVsbG8=!")


def test_cursor_rejects_valid_base64_but_invalid_json() -> None:
    """Base64 that decodes to non-JSON raises :class:`InvalidCursorError`."""
    token = base64.urlsafe_b64encode(b"this is not json").decode("ascii")
    with pytest.raises(InvalidCursorError):
        decode_cursor(token)


def test_cursor_rejects_json_array_payload() -> None:
    """JSON that decodes to a non-object raises :class:`InvalidCursorError`."""
    token = base64.urlsafe_b64encode(json.dumps(["array", "not", "object"]).encode()).decode(
        "ascii",
    )
    with pytest.raises(InvalidCursorError):
        decode_cursor(token)


def test_cursor_rejects_missing_ts() -> None:
    """Payload without ``ts`` raises :class:`InvalidCursorError`."""
    token = base64.urlsafe_b64encode(json.dumps({"id": str(uuid.uuid4())}).encode()).decode(
        "ascii",
    )
    with pytest.raises(InvalidCursorError):
        decode_cursor(token)


def test_cursor_rejects_missing_id() -> None:
    """Payload without ``id`` raises :class:`InvalidCursorError`."""
    token = base64.urlsafe_b64encode(
        json.dumps({"ts": datetime.now(UTC).isoformat()}).encode(),
    ).decode("ascii")
    with pytest.raises(InvalidCursorError):
        decode_cursor(token)


def test_cursor_rejects_malformed_ts() -> None:
    """``ts`` that does not parse as ISO-8601 raises :class:`InvalidCursorError`."""
    token = base64.urlsafe_b64encode(
        json.dumps({"ts": "yesterday", "id": str(uuid.uuid4())}).encode(),
    ).decode("ascii")
    with pytest.raises(InvalidCursorError):
        decode_cursor(token)


def test_cursor_rejects_malformed_uuid() -> None:
    """``id`` that does not parse as UUID raises :class:`InvalidCursorError`."""
    token = base64.urlsafe_b64encode(
        json.dumps({"ts": datetime.now(UTC).isoformat(), "id": "not-a-uuid"}).encode(),
    ).decode("ascii")
    with pytest.raises(InvalidCursorError):
        decode_cursor(token)


def test_cursor_invalid_error_preserves_cause() -> None:
    """The original decode exception is available as ``__cause__``."""
    try:
        decode_cursor("%%%")
    except InvalidCursorError as exc:
        assert exc.__cause__ is not None
    else:  # pragma: no cover - test must raise above
        pytest.fail("expected InvalidCursorError")
