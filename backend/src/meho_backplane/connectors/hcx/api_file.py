# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``hcx.api_file.get`` -- read one API documentation file the HCX Manager serves.

Broadcom publishes no file of the main HCX API (``/hybridity/api/...``).
Only a running HCX Manager serves its API documentation, at
``https://{server}/hybridity/docs`` and the files that page loads. This
read fetches one such page or file and returns its path, HTTP status,
content type, size, SHA-256 and the Manager version, with the text in
parts behind a result handle. Joining the parts in order gives the exact
file, so the vendor API files can be pinned and compared.

What the read accepts
---------------------

Only two kinds of path, checked before any vendor call:

1. the documentation page and folder: ``/hybridity/docs`` and every
   path under ``/hybridity/docs/``;
2. a Swagger or OpenAPI file at its usual place: a path with a part named
   ``api-docs``, ``swagger-resources`` or ``swagger-config``, or a path
   that ends with ``swagger.json``, ``swagger.yaml``, ``openapi.json`` or
   ``openapi.yaml``.

Every other path is refused. So is a path with ``..``, ``//``, ``\\``, an
encoded dot or slash, a scheme, a host name, a fragment or a control
character. A query string may hold only ``group`` and ``apiVersion``.

The read sends one ``GET`` to the target's own address with the target's
own login. It does not follow a redirect: the answer carries the
redirect's path in ``redirect_to`` and the caller reads that path next.
Only UTF-8 text comes back; other content is refused with its content
type. A file that does not fit in one result handle stops with an error
that names its size -- the read never returns a cut file.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = [
    "PART_BYTES",
    "HcxApiFileNotTextError",
    "HcxApiFilePathError",
    "HcxApiFileTooLargeError",
    "check_path",
    "hcx_api_file_get_impl",
    "split_parts",
]

#: The documentation page a running HCX Manager serves (``GET``). Every
#: path under ``/hybridity/docs/`` is the page's own folder.
_DOCS_PATH = "/hybridity/docs"

#: Path parts that name a Swagger or OpenAPI description, not data.
_API_FILE_SEGMENTS = frozenset({"api-docs", "swagger-resources", "swagger-config"})
_API_FILE_SUFFIXES = ("swagger.json", "swagger.yaml", "openapi.json", "openapi.yaml")

#: The only query keys a documentation path may carry.
_ALLOWED_QUERY_KEYS = frozenset({"group", "apiVersion"})

#: Encoded dot / slash forms that would change meaning after decoding.
_REFUSED_FRAGMENTS = ("..", "//", "\\", "://", "%2e", "%2f", "%5c", "#")

#: Largest text part. Small enough that one ``result_query`` answer (256 KiB)
#: holds several parts; large enough that a few-MB file is tens of rows.
PART_BYTES = 64 * 1024

#: Content types whose body is text. ``text/*`` is always accepted.
_TEXT_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/yaml",
        "application/x-yaml",
        "application/xml",
        "application/javascript",
        "application/x-javascript",
    }
)
_TEXT_CONTENT_SUFFIXES = ("+json", "+yaml", "+xml")

_UTF8_CONTINUATION_MASK = 0xC0
_UTF8_CONTINUATION_BITS = 0x80


class HcxApiFilePathError(ValueError):
    """The path is outside the rule; no HTTP request was sent."""


class HcxApiFileTooLargeError(ValueError):
    """The file does not fit in one result handle; the read stopped."""


class HcxApiFileNotTextError(ValueError):
    """The body is not UTF-8 text."""


def check_path(raw: Any) -> tuple[str, str]:
    """Check *raw* against the path rule. Return ``(path, query)`` to send.

    Raises :exc:`HcxApiFilePathError` for everything the rule refuses. The
    check runs on the raw string before any decoding, so an encoded dot or
    slash cannot slip through.
    """
    if not isinstance(raw, str) or not raw:
        raise HcxApiFilePathError("path must be a non-empty string")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in raw):
        raise HcxApiFilePathError("path must not contain whitespace or control characters")
    lowered = raw.lower()
    for fragment in _REFUSED_FRAGMENTS:
        if fragment in lowered:
            raise HcxApiFilePathError(f"path must not contain {fragment!r}")
    if not raw.startswith("/"):
        raise HcxApiFilePathError("path must start with '/' and name no scheme or host")
    path, _, query = raw.partition("?")
    if query:
        keys = {key for key, _ in parse_qsl(query, keep_blank_values=True)}
        unexpected = sorted(keys - _ALLOWED_QUERY_KEYS)
        if unexpected or not keys:
            raise HcxApiFilePathError(
                f"query string may hold only {sorted(_ALLOWED_QUERY_KEYS)}; got {unexpected}"
            )
    if _is_docs_path(path) or _is_api_description_path(path):
        return path, query
    raise HcxApiFilePathError(
        f"path {path!r} is not the documentation page ({_DOCS_PATH}, or a path under "
        f"{_DOCS_PATH}/) and not a Swagger or OpenAPI file at its usual place "
        f"(a part named {sorted(_API_FILE_SEGMENTS)}, or a name ending in "
        f"{list(_API_FILE_SUFFIXES)})"
    )


def _is_docs_path(path: str) -> bool:
    return path == _DOCS_PATH or path.startswith(_DOCS_PATH + "/")


def _is_api_description_path(path: str) -> bool:
    segments = [segment for segment in path.split("/") if segment]
    if any(segment in _API_FILE_SEGMENTS for segment in segments):
        return True
    return path.endswith(_API_FILE_SUFFIXES)


def split_parts(data: bytes, part_bytes: int = PART_BYTES) -> list[bytes]:
    """Split *data* into parts of at most *part_bytes*, on UTF-8 character boundaries.

    Joining the parts in order gives *data* back byte for byte; each part
    decodes on its own, so no character is cut in half.
    """
    parts: list[bytes] = []
    start = 0
    while start < len(data):
        end = min(start + part_bytes, len(data))
        while end < len(data) and end > start + 1:
            if data[end] & _UTF8_CONTINUATION_MASK != _UTF8_CONTINUATION_BITS:
                break
            end -= 1
        parts.append(data[start:end])
        start = end
    return parts


def _is_text_content_type(content_type: str | None) -> bool:
    if not content_type:
        return True
    media = content_type.split(";", 1)[0].strip().lower()
    if media.startswith("text/") or media in _TEXT_CONTENT_TYPES:
        return True
    return media.endswith(_TEXT_CONTENT_SUFFIXES)


def _redirect_target(location: str | None, request_url: httpx.URL) -> str | None:
    """Reduce a ``Location`` header to the path the caller should read next.

    A relative ``Location``, or an absolute one on the target's own host,
    gives its path (plus query). An absolute ``Location`` on another host
    gives ``scheme://host/path`` so the caller sees it cannot be read
    through this target. Fragments and user info are dropped.
    """
    if not location:
        return None
    try:
        url = httpx.URL(location)
    except httpx.InvalidURL:
        return None
    if not url.host or url.host == request_url.host:
        return url.copy_with(fragment=None).raw_path.decode("ascii", "replace")
    return str(url.copy_with(query=None, fragment=None, username=None, password=None))


async def _fetch_without_redirects(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    extensions: dict[str, Any],
    max_bytes: int,
) -> httpx.Response:
    """Send one ``GET`` and read the body, capped at *max_bytes*.

    The request goes through :meth:`httpx.AsyncClient.send` on the base
    class on purpose: the pooled client follows same-site redirects, and
    this read must not follow any. The body is streamed so an oversized
    file is refused before it is held in memory.
    """
    request = client.build_request("GET", url, headers=headers, extensions=extensions)
    response = await httpx.AsyncClient.send(client, request, stream=True, follow_redirects=False)
    try:
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise HcxApiFileTooLargeError(
                f"file of {declared} bytes does not fit in one result handle "
                f"(limit {max_bytes} bytes)"
            )
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_raw():
            total += len(chunk)
            if total > max_bytes:
                raise HcxApiFileTooLargeError(
                    f"file of more than {max_bytes} bytes does not fit in one result handle"
                )
            chunks.append(chunk)
    finally:
        await response.aclose()
    return httpx.Response(
        status_code=response.status_code,
        headers=response.headers,
        content=b"".join(chunks),
        request=request,
        extensions=response.extensions,
    )


def _assert_fits_result_handle(size: int, items: list[dict[str, Any]], max_bytes: int) -> None:
    """Stop with the file's size when its parts would not fit in one result handle."""
    settings = get_settings()
    if len(items) > settings.result_handle_max_spill_rows:
        raise HcxApiFileTooLargeError(
            f"file of {size} bytes needs {len(items)} parts; one result handle keeps at "
            f"most {settings.result_handle_max_spill_rows}"
        )
    encoded = len(json.dumps(items, ensure_ascii=False).encode("utf-8"))
    if encoded > max_bytes:
        raise HcxApiFileTooLargeError(
            f"file of {size} bytes ({encoded} bytes as result rows) does not fit in one "
            f"result handle (limit {max_bytes} bytes)"
        )


def _text_items(response: httpx.Response, path: str) -> list[dict[str, Any]]:
    """Split the body into text parts; refuse anything that is not UTF-8 text."""
    content_type = response.headers.get("content-type")
    if not _is_text_content_type(content_type):
        raise HcxApiFileNotTextError(
            f"GET {path} returned content type {content_type!r}; only text comes back"
        )
    try:
        parts = [part.decode("utf-8") for part in split_parts(response.content)]
    except UnicodeDecodeError as exc:
        raise HcxApiFileNotTextError(
            f"GET {path} returned content type {content_type!r} that is not UTF-8 text"
        ) from exc
    return [{"index": index, "text": text} for index, text in enumerate(parts)]


async def hcx_api_file_get_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.api_file.get`` -- ``GET {path}`` on the HCX Manager, text in parts.

    A ``401`` is raised as :exc:`httpx.HTTPStatusError` so the dispatcher
    drops the session and dispatches once more. Every other status is
    reported in ``status``.
    """
    path, query = check_path(params.get("path"))
    url = path if not query else f"{path}?{query}"
    max_bytes = get_settings().result_handle_max_record_bytes
    client = await connector._http_client(target)
    headers = await connector.auth_headers(target, operator)
    span = flight_recorder_capture.span_start()
    try:
        response = await _fetch_without_redirects(
            client,
            url,
            headers=headers,
            extensions=connector._request_extensions(target),
            max_bytes=max_bytes,
        )
    except httpx.TransportError as exc:
        flight_recorder_capture.record_vendor_call_error(span, method="GET", exc=exc)
        raise
    flight_recorder_capture.record_vendor_call(
        span, method="GET", request_headers=headers, response=response
    )
    if response.status_code == httpx.codes.UNAUTHORIZED:
        response.raise_for_status()

    items = _text_items(response, path)
    body = response.content
    _assert_fits_result_handle(len(body), items, max_bytes)
    version = await connector.manager_version(target, operator)
    return {
        "path": url,
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "version": version.version,
        "build": version.build,
        "version_error": version.error,
        "redirect_to": _redirect_target(response.headers.get("location"), response.request.url),
        "parts": len(items),
        "items": items,
    }
