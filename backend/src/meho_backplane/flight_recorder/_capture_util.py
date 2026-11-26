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

import re
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from urllib.parse import unquote

import httpx
import msgspec
import structlog

from meho_backplane.flight_recorder.store import SpanInput

__all__ = [
    "MAX_SPAN_BODY_BYTES",
    "PATH_VALUE_MARK",
    "FilledPath",
    "approx_bytes",
    "approx_span_bytes",
    "as_mapping",
    "cap_body",
    "coerce_uuid",
    "elapsed_ms",
    "encoded_safe_url",
    "header_value",
    "mask_path_values",
    "now_utc",
    "path_values",
    "request_id",
    "response_content",
    "safe_url",
    "serialize",
    "transport_fault_phase",
]

#: Per-span body cap (F3). A request/response body larger than this truncates
#: (marker + forced redaction-uncertainty); it never errors a dispatch.
MAX_SPAN_BODY_BYTES: Final[int] = 64 * 1024

_AUTHORITY_END: Final = re.compile(r"[/?#]")

#: Timeout class -> phase name. Same labels as the ``connector_timeout``
#: error result (``operations._errors.result_connector_timeout``), so the
#: trace and the error the caller sees name the same phase.
_TIMEOUT_PHASES: Final[tuple[tuple[type[httpx.TimeoutException], str], ...]] = (
    (httpx.ConnectTimeout, "connect"),
    (httpx.ReadTimeout, "read"),
    (httpx.WriteTimeout, "write"),
    (httpx.PoolTimeout, "pool"),
)


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

    Query strings and userinfo can carry tokens / signed-URL material, so the
    capture seam strips them here, for a parsed URL and for a plain string
    alike: only the non-secret request line survives.
    """
    try:
        scheme = getattr(url, "scheme", None)
        netloc = getattr(url, "netloc", None)
        path = getattr(url, "path", None)
        if scheme and isinstance(netloc, bytes) and netloc:
            # ``netloc`` is the host and port as sent: a port that is not the
            # scheme's default stays, and an IPv6 host keeps its brackets.
            return f"{scheme}://{netloc.decode('ascii')}{path or ''}"
        text = str(url)
        scheme_end = text.find("://")
        if scheme_end == -1:
            return text.split("?", 1)[0].split("#", 1)[0]
        # The host part ends at the first ``/``, ``?`` or ``#`` (RFC 3986);
        # only what follows its last ``@`` is the host.
        rest = text[scheme_end + 3 :]
        end_mark = _AUTHORITY_END.search(rest)
        authority_end = len(rest) if end_mark is None else end_mark.start()
        host = rest[:authority_end].rpartition("@")[2]
        path = rest[authority_end:].split("?", 1)[0].split("#", 1)[0]
        return f"{text[:scheme_end]}://{host}{path}"
    except Exception:
        return "?"


def encoded_safe_url(url: Any) -> str:
    """Like :func:`safe_url`, with the path still percent-encoded as it was sent.

    :attr:`httpx.URL.path` decodes ``%2F``, so a parameter value that holds a
    ``/`` would look like two path segments. A plain string is taken as it is.
    """
    try:
        raw_path = getattr(url, "raw_path", None)
        netloc = getattr(url, "netloc", None)
        if isinstance(raw_path, bytes) and isinstance(netloc, bytes):
            path = raw_path.split(b"?", 1)[0].decode("ascii")
            return f"{url.scheme}://{netloc.decode('ascii')}{path}"
    except Exception:
        return "?"
    return safe_url(url)


#: Stands in for a path segment that holds a value of the call's parameters.
PATH_VALUE_MARK: Final[str] = "[redacted]"

#: A parameter value this long is masked also inside a longer path segment
#: where it does not stand as a word of its own (``/keys/x<value>``).
_MIN_VALUE_INSIDE_SEGMENT: Final[int] = 8

#: A ``{name}`` placeholder of an operation's path template. ``{+name}`` and
#: ``{#name}`` may hold a ``/``; every other form may not.
_PLACEHOLDER: Final = re.compile(r"\{([+#./;?&]?)[^{}]*\}")


class FilledPath(str):
    """A request path that a typed handler filled from a path template.

    It is the filled path itself, so it goes to HTTPX as it is. It also keeps
    the *template* (``/admin/realms/{realm}``), so the trace knows which path
    segments the handler filled with a value and which are fixed.
    """

    template: str

    def __new__(cls, path: str, template: str) -> FilledPath:
        filled = super().__new__(cls, path)
        filled.template = template
        return filled


def path_values(params: Any) -> frozenset[str]:
    """The texts of the call's top-level parameter values that can stand in a path.

    Strings and whole numbers only; an empty string is left out.
    """
    if not isinstance(params, Mapping):
        return frozenset()
    values: set[str] = set()
    for value in params.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, str | int) and str(value):
            values.add(str(value))
    return frozenset(values)


def body_values(body: Any, is_secret_key: Callable[[str], bool]) -> frozenset[str]:
    """The texts of the values a request body carries, under a key that is not secret.

    Walks a JSON or form body (mappings and lists) and keeps strings and
    whole numbers. A value under a key that *is_secret_key* names a secret
    (or under any key below one) is left out.
    """
    values: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if not (isinstance(key, str) and is_secret_key(key)):
                    walk(value)
        elif isinstance(node, list | tuple):
            for item in node:
                walk(item)
        elif isinstance(node, str | int) and not isinstance(node, bool) and str(node):
            values.add(str(node))

    walk(body)
    return frozenset(values)


def mask_path_values(url_text: str, values: frozenset[str], template: str | None = None) -> str:
    """Replace each path segment of *url_text* that holds a value of the call with a mark.

    *url_text* is an :func:`encoded_safe_url` result. With the operation's
    path *template* (an ingested operation), the segments where the template
    puts a parameter are masked, and the fixed segments of the template stay.
    Without a template, or when the address does not follow it (a redirect to
    another path), a segment is masked when, after percent-decoding, it holds
    one of *values* as a word of its own, or a long value anywhere.
    """
    scheme_end = url_text.find("://")
    if scheme_end == -1:
        return url_text
    path_start = url_text.find("/", scheme_end + 3)
    if path_start == -1:
        return url_text
    segments = url_text[path_start:].split("/")
    masked = _template_segments(url_text[path_start:], template) if template else None
    if masked is None:
        masked = _value_segments(segments, values)
    if not masked:
        return url_text
    kept = [PATH_VALUE_MARK if index in masked else seg for index, seg in enumerate(segments)]
    return url_text[:path_start] + "/".join(kept)


def _segment_indexes(segments: list[str], start: int, end: int) -> set[int]:
    """The indexes of *segments* that ``[start, end)`` of ``"/".join(segments)`` touches."""
    indexes: set[int] = set()
    seg_start = 0
    for index, segment in enumerate(segments):
        seg_end = seg_start + len(segment)
        if seg_start < end and start < seg_end:
            indexes.add(index)
        seg_start = seg_end + 1
    return indexes


def _template_segments(path: str, template: str) -> set[int] | None:
    """The segments of *path* where *template* puts a parameter; ``None`` if it does not match.

    The template is matched against the end of the encoded *path*, so a mount
    prefix before it and one slash after it (a redirect to the same path with
    a slash) are allowed.
    """
    pattern = []
    position = 0
    for match in _PLACEHOLDER.finditer(template):
        pattern.append(re.escape(template[position : match.start()]))
        pattern.append("(.*?)" if match.group(1) in ("+", "#") else "([^/]*)")
        position = match.end()
    pattern.append(re.escape(template[position:]))
    # The client follows a change's redirect to the same path in another
    # letter case, so the fixed parts of the template match in any case.
    found = re.search("".join(pattern) + "/?$", path, re.IGNORECASE)
    if found is None:
        return None
    masked: set[int] = set()
    for group in range(1, (found.lastindex or 0) + 1):
        start, end = found.span(group)
        if end > start:
            masked |= _segment_indexes(path.split("/"), start, end)
    return masked


def _value_segments(segments: list[str], values: frozenset[str]) -> set[int]:
    """The segments that hold one of *values*, compared after percent-decoding.

    The decoded segments are joined with ``/``, so a value that holds a ``/``
    marks every segment it covers. The compare ignores letter case on both
    sides: the client follows a redirect to the same path in another letter
    case, so ``/keys/DemoKey`` can come back as ``/keys/demokey``.
    """
    decoded = [unquote(segment) for segment in segments]
    joined = "/".join(decoded)
    masked: set[int] = set()
    for value in values:
        # A look-ahead finds each place, also places that overlap.
        for found in re.finditer(f"(?=({re.escape(value)}))", joined, re.IGNORECASE):
            start, end = found.span(1)
            if len(value) >= _MIN_VALUE_INSIDE_SEGMENT or _stands_alone(joined, start, end):
                masked |= _segment_indexes(decoded, start, end)
    return masked


def _stands_alone(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]`` has no letter or digit right before or after it."""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return not before.isalnum() and not after.isalnum()


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


def transport_fault_phase(exc: BaseException) -> str:
    """Name the phase a transport fault happened in.

    A timeout maps to ``connect`` / ``read`` / ``write`` / ``pool``. Any other
    fault (connection refused, network error, protocol error) has no truthful
    phase, so it is ``transport``.
    """
    for timeout_type, phase in _TIMEOUT_PHASES:
        if isinstance(exc, timeout_type):
            return phase
    return "transport"
