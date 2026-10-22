# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``fleet.api_file.get`` -- read one API description page or file the ``/lcm`` appliance serves.

Aria Suite Lifecycle 8.18 (and the VCF 9.0 fleet appliance that keeps its
``/lcm`` surface) publishes no vendor file of its ``/lcm`` API. Only a
running appliance serves the API descriptions: the Lifecycle APIs under
``/api/swagger-ui/`` and the Content Management APIs under
``/lcm/cms/api/swagger-ui/``, plus the files those pages load. This read
fetches one such page or file and returns its path, HTTP status, content
type, size, SHA-256 and the appliance version and build, with the text in
parts behind a result handle. Joining the parts in order gives the exact
file, so the vendor API files can be pinned and compared.

What the read accepts
---------------------

Only two kinds of path, checked before any vendor call:

1. the documentation pages and folders: ``/api/swagger-ui.html`` and
   ``/lcm/cms/api/swagger-ui.html`` (the pages before 8.18 Patch 3),
   ``/api/swagger-ui/`` and ``/lcm/cms/api/swagger-ui/`` (a folder covers
   every path under it, for example ``index.html``);
2. a Swagger or OpenAPI file at its usual place: a path with a part named
   ``api-docs``, ``swagger-resources`` or ``swagger-config``, or a path
   that ends with ``swagger.json``, ``swagger.yaml``, ``openapi.json`` or
   ``openapi.yaml``.

Every other path is refused. So is a path with ``..``, ``//``, ``\\``, an
encoded dot or slash, a semicolon, a scheme, a host name, a fragment or a
control character. A query string may hold only ``group`` and ``apiVersion``.

The read sends one ``GET`` to the target's own address with the target's
own login. It does not follow a redirect: the answer carries the
redirect's path in ``redirect_to``, resolved against the path that was
read, and the caller reads that path next. Only UTF-8 text comes back;
other content is refused with its content type. A compressed body
(``gzip`` or ``deflate``; any other encoding is refused) is decoded in
bounded steps and measured as it decodes, so the decoded bytes never
grow past the limit. A body whose compressed stream ends early, or that
carries bytes after a ``deflate`` stream, is refused; a ``gzip`` body
made of several members is decoded whole, under the same limit. A file
whose stored record does not fit in one result handle stops with an
error that names its size -- the read never returns a cut file. A file
that the secret-redaction rules would change stops too, so the read never
claims an exact file it cannot return; the rules run on the whole text
and on the answer, so a secret that spans two parts is found as well.

Version and build come from ``GET /lcm/lcops/api/v2/about`` (the call of
``fleet.about``). On VCF 9.0 builds that call answers HTTP 500; then
``version`` and ``build`` are empty, ``version_error`` says why, and
``lcm_api_version`` still carries the ``Lcm-API-Version`` header the
appliance sent with the file.
"""

from __future__ import annotations

import hashlib
import zlib
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.result_handle_store import rows_record_bytes
from meho_backplane.connectors.vcf_fleet import FLEET_CONNECTOR_ID
from meho_backplane.connectors.vcf_fleet.connector import _FLEET_LCM_API_VERSION_HEADER
from meho_backplane.connectors.vcf_fleet.session import VcfFleetTargetLike
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.redaction.middleware import apply_connector_boundary_redaction
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.vcf_fleet.connector import VcfFleetConnector

__all__ = [
    "DOCS_FOLDERS",
    "DOCS_PAGES",
    "PART_BYTES",
    "FleetApiFileNotTextError",
    "FleetApiFilePathError",
    "FleetApiFileRedactedError",
    "FleetApiFileTooLargeError",
    "check_path",
    "fleet_api_file_get_impl",
    "split_parts",
]

#: The documentation pages a running appliance serves (``GET``): the
#: Lifecycle APIs page and the Content Management APIs page as the 8.12 to
#: 8.18 programming guides name them, before 8.18 Patch 3.
DOCS_PAGES: tuple[str, ...] = ("/api/swagger-ui.html", "/lcm/cms/api/swagger-ui.html")

#: The documentation folders (``GET``): the Lifecycle APIs and the Content
#: Management APIs from 8.18 Patch 3 on (``index.html`` under each). A
#: folder covers every path under it.
DOCS_FOLDERS: tuple[str, ...] = ("/api/swagger-ui/", "/lcm/cms/api/swagger-ui/")

#: Path parts that name a Swagger or OpenAPI description, not data.
_API_FILE_SEGMENTS = frozenset({"api-docs", "swagger-resources", "swagger-config"})
_API_FILE_SUFFIXES = ("swagger.json", "swagger.yaml", "openapi.json", "openapi.yaml")

#: The only query keys a documentation path may carry.
_ALLOWED_QUERY_KEYS = frozenset({"group", "apiVersion"})

#: Characters and encoded forms that have no place in a documentation or
#: API file path. Checked on the raw string, before any decoding.
_REFUSED_FRAGMENTS = ("..", "//", "\\", "://", "%2e", "%2f", "%5c", "#", ";", "%3b")

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


class FleetApiFilePathError(ValueError):
    """The path is outside the rule; no HTTP request was sent."""


class FleetApiFileTooLargeError(ValueError):
    """The file does not fit in one result handle; the read stopped."""


class FleetApiFileNotTextError(ValueError):
    """The body is not UTF-8 text."""


class FleetApiFileRedactedError(ValueError):
    """The secret-redaction rules would change the file; the read stopped."""


#: The operation id the dispatcher labels this read with. The redaction
#: check and the stored-record estimate use the same label.
_OP_ID = "fleet.api_file.get"

#: Headers that describe the wire body, not the decoded file. Dropped from the
#: answer that is built from the decoded bytes, so httpx does not decode twice.
_WIRE_BODY_HEADERS = frozenset({"content-encoding", "content-length"})

#: The only content encodings the read asks for and decodes. Both go through
#: :mod:`zlib`, whose ``max_length`` bounds the decoded bytes of every step.
_ACCEPT_ENCODING = "gzip, deflate"
_IDENTITY_ENCODINGS = frozenset({"", "identity"})

#: Largest wire chunk read from the stream before it is decoded.
_RAW_CHUNK_BYTES = 64 * 1024


class _BoundedDecoder:
    """Decode a ``gzip`` or ``deflate`` body in steps of at most the room left.

    :meth:`zlib.Decompress.decompress` is called with ``max_length`` set to
    the bytes still allowed plus one, so no step allocates more than that,
    however small the compressed input is. Input that did not fit in a step
    waits in ``unconsumed_tail`` and is decoded on the next loop. A step
    that crosses the limit stops the read; the one extra byte is what
    shows the file is over the limit.

    The decoder returns the whole file or fails. A ``gzip`` body may hold
    several members one after the other (RFC 1952); each member after the
    first is decoded with a fresh stream, under the same limit. Bytes
    after a ``deflate`` stream are refused, as is a body that ends before
    its stream does. Only an empty body (no compressed bytes at all, as on
    a redirect) decodes to nothing without an error.
    """

    def __init__(self, encoding: str, max_bytes: int) -> None:
        self._encoding = encoding
        self._max_bytes = max_bytes
        self._total = 0
        self._fed = 0
        self._first_step = True
        self._raw_deflate_retry = encoding == "deflate"
        self._zlib = zlib.decompressobj(self._wbits())

    def _wbits(self) -> int:
        return zlib.MAX_WBITS | 16 if self._encoding == "gzip" else zlib.MAX_WBITS

    @property
    def total(self) -> int:
        return self._total

    def _count(self, out: bytes) -> bytes:
        self._total += len(out)
        if self._total > self._max_bytes:
            raise FleetApiFileTooLargeError(
                f"file of more than {self._max_bytes} bytes does not fit in one result handle"
            )
        return out

    def _step(self, data: bytes) -> bytes:
        room = self._max_bytes - self._total + 1
        try:
            return self._zlib.decompress(data, max_length=room)
        except zlib.error as exc:
            if self._first_step and self._raw_deflate_retry:
                # Some servers send raw deflate under ``Content-Encoding: deflate``.
                self._raw_deflate_retry = False
                self._zlib = zlib.decompressobj(-zlib.MAX_WBITS)
                return self._step(data)
            raise httpx.DecodingError(str(exc)) from exc
        finally:
            self._first_step = False

    def feed(self, data: bytes) -> Iterator[bytes]:
        self._fed += len(data)
        while data:
            yield self._count(self._step(data))
            data = self._next_input()

    def _next_input(self) -> bytes:
        """The bytes the next step decodes: the step's leftover, or the next member."""
        if not self._zlib.eof:
            return self._zlib.unconsumed_tail
        # At the end of a stream zlib puts every later byte in ``unused_data``
        # (and in ``unconsumed_tail`` too), so the tail must not be fed again.
        rest = self._zlib.unused_data
        if not rest:
            return b""
        if self._encoding != "gzip":
            raise httpx.DecodingError(
                "compressed body carries data after the end of the deflate stream"
            )
        self._zlib = zlib.decompressobj(self._wbits())
        return rest

    def finish(self) -> bytes:
        try:
            out = self._count(self._zlib.flush())
        except zlib.error as exc:  # pragma: no cover - zlib flush does not fail on short input
            raise httpx.DecodingError(str(exc)) from exc
        if self._fed and not self._zlib.eof:
            raise httpx.DecodingError("compressed body ended before the compressed stream finished")
        return out


def check_path(raw: Any) -> tuple[str, str]:
    """Check *raw* against the path rule. Return ``(path, query)`` to send.

    Raises :exc:`FleetApiFilePathError` for everything the rule refuses. The
    check runs on the raw string before any decoding, so an encoded dot or
    slash cannot slip through.
    """
    if not isinstance(raw, str) or not raw:
        raise FleetApiFilePathError("path must be a non-empty string")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in raw):
        raise FleetApiFilePathError("path must not contain whitespace or control characters")
    lowered = raw.lower()
    for fragment in _REFUSED_FRAGMENTS:
        if fragment in lowered:
            raise FleetApiFilePathError(f"path must not contain {fragment!r}")
    if not raw.startswith("/"):
        raise FleetApiFilePathError("path must start with '/' and name no scheme or host")
    path, _, query = raw.partition("?")
    if query:
        keys = {key for key, _ in parse_qsl(query, keep_blank_values=True)}
        unexpected = sorted(keys - _ALLOWED_QUERY_KEYS)
        if unexpected or not keys:
            raise FleetApiFilePathError(
                f"query string may hold only {sorted(_ALLOWED_QUERY_KEYS)}; got {unexpected}"
            )
    if _is_docs_path(path) or _is_api_description_path(path):
        return path, query
    raise FleetApiFilePathError(
        f"path {path!r} is not a documentation page ({list(DOCS_PAGES)}), not a path under a "
        f"documentation folder ({list(DOCS_FOLDERS)}) and not a Swagger or OpenAPI file at "
        f"its usual place (a part named {sorted(_API_FILE_SEGMENTS)}, or a name ending in "
        f"{list(_API_FILE_SUFFIXES)})"
    )


def _is_docs_path(path: str) -> bool:
    return path in DOCS_PAGES or path.startswith(DOCS_FOLDERS)


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

    The header is resolved against *request_url* first (RFC 3986), so
    ``swagger.json`` read from ``/api/swagger-ui/index.html`` gives
    ``/api/swagger-ui/swagger.json`` and ``?group=x`` keeps the path that
    was read. A result on the target's own host gives its path (plus
    query). A result on another host gives ``scheme://host/path`` so the
    caller sees it cannot be read through this target. Fragments and user
    info are dropped.
    """
    if not location:
        return None
    try:
        url = request_url.join(location)
    except httpx.InvalidURL:
        return None
    if url.host == request_url.host:
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
    """Send one ``GET`` and read the body, capped at *max_bytes* after decoding.

    The request goes through :meth:`httpx.AsyncClient.send` on the base
    class on purpose: the pooled client follows same-site redirects, and
    this read must not follow any. The wire body is streamed in chunks
    (:meth:`~httpx.Response.aiter_raw`) and decoded here, not by httpx:
    httpx decodes each wire chunk whole, so a few KiB of ``gzip`` could
    expand to many MiB before any count. :class:`_BoundedDecoder` never
    decodes more than the room left, so the decoded bytes stay within the
    cap at every step. The ``Content-Length`` fast reject applies only to
    a body that is not compressed, where the header names the file's own
    size.

    The answer is rebuilt from the decoded bytes, without the wire-body
    headers, so the flight recorder and the caller see the file once.
    """
    request = client.build_request(
        "GET", url, headers={**headers, "accept-encoding": _ACCEPT_ENCODING}, extensions=extensions
    )
    response = await httpx.AsyncClient.send(client, request, stream=True, follow_redirects=False)
    try:
        chunks = await _read_body_bounded(response, url, max_bytes=max_bytes)
    finally:
        await response.aclose()
    kept_headers = [
        (name, value)
        for name, value in response.headers.multi_items()
        if name.lower() not in _WIRE_BODY_HEADERS
    ]
    return httpx.Response(
        status_code=response.status_code,
        headers=kept_headers,
        content=b"".join(chunks),
        request=request,
        extensions=response.extensions,
    )


async def _read_body_bounded(response: httpx.Response, url: str, *, max_bytes: int) -> list[bytes]:
    """Read and decode the streamed body; stop as soon as it passes *max_bytes*."""
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    declared = response.headers.get("content-length")
    if encoding in _IDENTITY_ENCODINGS:
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise FleetApiFileTooLargeError(
                f"file of {declared} bytes does not fit in one result handle "
                f"(limit {max_bytes} bytes)"
            )
        decoder: _BoundedDecoder | None = None
    elif encoding in _ACCEPT_ENCODING.split(", "):
        decoder = _BoundedDecoder(encoding, max_bytes)
    else:
        raise FleetApiFileNotTextError(
            f"GET {url} returned content encoding {encoding!r}; only {_ACCEPT_ENCODING} or "
            "no encoding can be decoded"
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_raw(_RAW_CHUNK_BYTES):
        if decoder is None:
            total += len(chunk)
            if total > max_bytes:
                raise FleetApiFileTooLargeError(
                    f"file of more than {max_bytes} bytes does not fit in one result handle"
                )
            chunks.append(chunk)
            continue
        chunks.extend(decoder.feed(chunk))
    if decoder is not None:
        chunks.append(decoder.finish())
    return chunks


def _assert_fits_result_handle(
    answer: dict[str, Any], items: list[dict[str, Any]], *, operator_sub: str, max_bytes: int
) -> None:
    """Stop with the file's size when its stored record would not fit in one result handle.

    The size is the complete record the result store writes: the parts plus
    the record's own fields and the answer's scalars. Counting only the
    parts lets the store keep all but the last one and still answer; this
    read must return every part or none.
    """
    settings = get_settings()
    size = answer["size"]
    if len(items) > settings.result_handle_max_spill_rows:
        raise FleetApiFileTooLargeError(
            f"file of {size} bytes needs {len(items)} parts; one result handle keeps at "
            f"most {settings.result_handle_max_spill_rows}"
        )
    record = rows_record_bytes(items, operator_sub=operator_sub, op_id=_OP_ID, document=answer)
    if record > max_bytes:
        raise FleetApiFileTooLargeError(
            f"file of {size} bytes ({record} bytes as a stored result record) does not fit in "
            f"one result handle (limit {max_bytes} bytes)"
        )


def _assert_not_redacted(answer: dict[str, Any], text: str, operator: Operator) -> None:
    """Stop when the shared redaction step would change the file's text.

    The dispatcher redacts every answer before the caller sees it. A file
    that holds, for example, an ``Authorization`` header as a documentation
    example would come back changed while ``sha256`` still names the
    original. Running the same rules here, with the same labels, finds that
    case before the answer leaves the read. Redaction stays on; the error
    names the rules and the places, never the matched text.

    The rules run twice: on the whole *text*, because a secret whose label
    ends one part and whose value starts the next is not seen in either
    part alone; and on the *answer* itself, the document the dispatcher
    will redact.
    """
    tenant = str(operator.tenant_id) if operator.tenant_id is not None else None
    for document in ({"path": answer["path"], "text": text}, answer):
        outcome = apply_connector_boundary_redaction(
            document, connector_id=FLEET_CONNECTOR_ID, tenant=tenant, op=_OP_ID
        )
        if outcome.redacted == outcome.raw:
            continue
        rules = sorted({entry.rule for entry in outcome.manifest})
        places = sum(entry.count for entry in outcome.manifest)
        raise FleetApiFileRedactedError(
            f"GET {answer['path']}: the secret-redaction rules {rules} would change the text in "
            f"{places} place(s), so the exact file cannot be returned; the read stopped"
        )


def _decode_text(response: httpx.Response, path: str) -> str:
    """Return the body as text; refuse anything that is not UTF-8 text."""
    content_type = response.headers.get("content-type")
    if not _is_text_content_type(content_type):
        raise FleetApiFileNotTextError(
            f"GET {path} returned content type {content_type!r}; only text comes back"
        )
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FleetApiFileNotTextError(
            f"GET {path} returned content type {content_type!r} that is not UTF-8 text"
        ) from exc


def _text_items(body: bytes) -> list[dict[str, Any]]:
    """Split a UTF-8 *body* into text parts of at most :data:`PART_BYTES`."""
    return [
        {"index": index, "text": part.decode("utf-8")}
        for index, part in enumerate(split_parts(body))
    ]


async def fleet_api_file_get_impl(
    connector: VcfFleetConnector,
    operator: Operator,
    target: VcfFleetTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet.api_file.get`` -- ``GET {path}`` on the ``/lcm`` appliance, text in parts.

    A ``401`` is raised as :exc:`httpx.HTTPStatusError` so the dispatcher
    drops the cached login and dispatches once more. Every other status is
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

    text = _decode_text(response, path)
    body = response.content
    items = _text_items(body)
    version = await connector.appliance_version(target, operator)
    answer: dict[str, Any] = {
        "path": url,
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "version": version.version,
        "build": version.build,
        "version_error": version.error,
        "lcm_api_version": response.headers.get(_FLEET_LCM_API_VERSION_HEADER),
        "redirect_to": _redirect_target(response.headers.get("location"), response.request.url),
        "parts": len(items),
    }
    _assert_fits_result_handle(answer, items, operator_sub=operator.sub, max_bytes=max_bytes)
    answer["items"] = items
    _assert_not_redacted(answer, text, operator)
    return answer
