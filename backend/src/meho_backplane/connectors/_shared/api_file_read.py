# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared steps for reading one API description file an appliance serves.

Some vendors publish no file of their API; only a running appliance
serves it. An ``*.api_file.get`` read fetches such a file and returns its
size, SHA-256 and the text in parts behind a result handle. Joining the
parts in order gives the exact file, so the vendor file can be pinned.

This module holds the steps that do not depend on the vendor:

* :func:`fetch_without_redirects` sends one ``GET``, never follows a
  redirect and reads the body in a stream. A ``gzip`` or ``deflate``
  body is decoded here in bounded steps, so the decoded bytes never grow
  past the limit (:class:`ApiFileTooLargeError`). A compressed stream
  that ends early, or bytes after a ``deflate`` stream, are refused. A
  ``401`` whose body cannot be read comes back with no body, so the
  dispatcher still sees the ``401``.
* :func:`fetch_checked_api_file` is the step every read starts with: the
  fetch and the file check. A refused file is recorded with no body. A
  ``401`` is recorded and raised with a checked body (or none), so the
  dispatcher still logs in again and retries once.
* :func:`finish_api_file_read` is the step every read ends with: the
  stored-record check, the redaction check, and only then the flight
  recorder call with the checked copy of the file. The recorder never
  gets the body as it came, and no body when a check refuses the file.
* :func:`decode_text` returns the body as UTF-8 text or refuses it with
  its content type (:class:`ApiFileNotTextError`).
* :func:`text_items` and :func:`split_parts` cut the text into parts of
  at most :data:`PART_BYTES`, on character boundaries.
* :func:`assert_fits_result_handle` stops when the stored record would
  not fit in one result handle: the read returns every part or none.
* :func:`text_redactor` gives MEHO's general secret-redaction rules as a
  rule for one text value. A read passes it to
  :func:`~meho_backplane.connectors._shared.api_file_secrets.check_api_file`
  as ``text_rule``, so a documentation example such as
  ``Authorization: Basic ...`` comes back as a placeholder instead of
  stopping the read. The rules run on each parsed value before the file
  is written back, so the written file keeps its format and every value
  keeps its type.
* :func:`assert_not_redacted` stops when the rules would still change the
  answer, so ``size`` and ``sha256`` always describe the returned copy.
  The error names the rules, never the matched text.
* :func:`redirect_target` turns a ``Location`` header into the path the
  caller should read next.

The ``fleet.api_file.get``, ``hcx.api_file.get``, ``srm.api_file.get``
and ``vr.api_file.get`` reads use these steps; each adds only its path
rule, its version read and its answer.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from typing import Any, NoReturn

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.api_file_secrets import (
    ApiFileFormatError,
    CheckedApiFile,
    check_api_file,
)
from meho_backplane.connectors._shared.api_file_values import TextRule
from meho_backplane.connectors.result_handle_store import rows_record_bytes
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.redaction.middleware import apply_connector_boundary_redaction
from meho_backplane.settings import get_settings

__all__ = [
    "PART_BYTES",
    "ApiFileNotTextError",
    "ApiFileRedactedError",
    "ApiFileTooLargeError",
    "FetchedApiFile",
    "assert_fits_result_handle",
    "assert_not_redacted",
    "decode_text",
    "fetch_checked_api_file",
    "fetch_without_redirects",
    "finish_api_file_read",
    "redirect_target",
    "split_parts",
    "text_items",
    "text_redactor",
]


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


class ApiFileTooLargeError(ValueError):
    """The file does not fit in one result handle; the read stopped."""


class ApiFileNotTextError(ValueError):
    """The body is not UTF-8 text."""


class ApiFileRedactedError(ValueError):
    """The secret-redaction rules would change the file; the read stopped."""


#: Headers that describe the wire body, not the decoded file. Dropped from the
#: answer that is built from the decoded bytes, so httpx does not decode twice.
_WIRE_BODY_HEADERS = frozenset({"content-encoding", "content-length"})

#: The only content encodings the read asks for and decodes. Both go through
#: :mod:`zlib`, whose ``max_length`` bounds the decoded bytes of every step.
_ACCEPT_ENCODING = "gzip, deflate"
_IDENTITY_ENCODINGS = frozenset({"", "identity"})

#: Largest wire chunk read from the stream before it is decoded.
_RAW_CHUNK_BYTES = 64 * 1024

#: What reading a body can fail with: over the limit, an encoding the read
#: cannot bound, or a compressed body that does not decode. A ``401`` with
#: such a body is still a ``401``.
_BODY_READ_ERRORS = (ApiFileTooLargeError, ApiFileNotTextError, httpx.DecodingError)


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
            raise ApiFileTooLargeError(
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


def redirect_target(location: str | None, request_url: httpx.URL) -> str | None:
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


async def fetch_without_redirects(
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

    A ``401`` comes back whatever its body holds. When the body is over
    the limit, in an encoding the read cannot bound, or compressed in a
    way that does not decode, the ``401`` comes back with no body instead
    of a body-read error: the dispatcher must see the ``401`` to log in
    again and retry. Only a transport fault raises.
    """
    request = client.build_request(
        "GET", url, headers={**headers, "accept-encoding": _ACCEPT_ENCODING}, extensions=extensions
    )
    response = await httpx.AsyncClient.send(client, request, stream=True, follow_redirects=False)
    try:
        chunks = await _read_body_bounded(response, url, max_bytes=max_bytes)
    except _BODY_READ_ERRORS:
        if response.status_code != httpx.codes.UNAUTHORIZED:
            raise
        chunks = []
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


class FetchedApiFile:
    """One fetched and checked API file whose flight recorder call is still to make.

    ``response`` is the answer as :func:`fetch_without_redirects` built it
    and ``checked`` the checked copy of the file. The read builds its
    answer from both and ends with :func:`finish_api_file_read`, which
    makes the recorder call. :meth:`record` makes it with *body* in place
    of the body as it came.
    """

    def __init__(
        self,
        span: flight_recorder_capture.SpanStart | None,
        headers: dict[str, str],
        response: httpx.Response,
        checked: CheckedApiFile,
    ) -> None:
        self._span = span
        self._headers = headers
        self.response = response
        self.checked = checked

    def record(self, body: bytes) -> None:
        """Record the vendor call with *body* as its body."""
        _record(self._span, self._headers, self.response, body)


async def fetch_checked_api_file(
    client: httpx.AsyncClient,
    url: str,
    *,
    path: str,
    headers: dict[str, str],
    extensions: dict[str, Any],
    max_bytes: int,
    text_rule: TextRule | None,
    error_text_rule: TextRule,
) -> FetchedApiFile:
    """Fetch one API file and check it; the flight recorder call waits for the last checks.

    One ``GET`` through :func:`fetch_without_redirects`, then the file
    check (:func:`check_api_file`) with *text_rule* on the body of every
    answer that is not a ``401``. *path* is the path without its query:
    the check reads the file ending from it. The answer and the checked
    copy come back together as a :class:`FetchedApiFile`; the read ends
    with :func:`finish_api_file_read`, which records the call once the
    stored-record and redaction checks pass.

    The flight recorder never gets the body as it came:

    * a file the check refuses (not text, does not parse) is recorded
      with no body, and the refusal is raised;
    * a ``401`` is raised as :exc:`httpx.HTTPStatusError`, so the
      dispatcher drops the login, logs in again and retries once. The
      raised answer and the recorded call carry the ``401`` body as the
      file check writes it with *error_text_rule*, or no body when it
      cannot be checked or could not be read. The status and the headers
      stay, so the retry still runs. *error_text_rule* is MEHO's general
      redaction rules (:func:`text_redactor`): the ``401`` body is never
      returned as a file, so nothing stops it from being redacted;
    * a transport fault is recorded without a body, and raised.

    A body over *max_bytes* on any other status stops inside the fetch,
    before an answer exists; nothing is recorded for it.
    """
    span = flight_recorder_capture.span_start()
    try:
        response = await fetch_without_redirects(
            client, url, headers=headers, extensions=extensions, max_bytes=max_bytes
        )
    except httpx.TransportError as exc:
        flight_recorder_capture.record_vendor_call_error(span, method="GET", exc=exc)
        raise
    if response.status_code == httpx.codes.UNAUTHORIZED:
        _raise_unauthorized(span, headers, response, path, error_text_rule)
    checked = _check_or_record_refusal(span, headers, response, path, text_rule)
    return FetchedApiFile(span, headers, response, checked)


def finish_api_file_read(
    fetched: FetchedApiFile,
    answer: dict[str, Any],
    items: list[dict[str, Any]],
    operator: Operator,
    *,
    connector_id: str,
    op_id: str,
    max_bytes: int,
) -> dict[str, Any]:
    """Run the last two checks, record the call, and return *answer* with its parts.

    :func:`assert_fits_result_handle` runs on *answer* and *items*, then
    *items* go into the answer and :func:`assert_not_redacted` runs on
    it. Only when both pass does the flight recorder get the call, with
    the checked copy of the file as its body. A file one of the checks
    refuses is recorded with no body, and the refusal is raised. So the
    recorder holds a body only for a file the read returned.
    """
    try:
        assert_fits_result_handle(
            answer, items, operator_sub=operator.sub, op_id=op_id, max_bytes=max_bytes
        )
        answer["items"] = items
        assert_not_redacted(
            answer, fetched.checked.text, operator, connector_id=connector_id, op_id=op_id
        )
    except (ApiFileTooLargeError, ApiFileRedactedError):
        fetched.record(b"")
        raise
    fetched.record(fetched.checked.text.encode("utf-8"))
    return answer


def _raise_unauthorized(
    span: flight_recorder_capture.SpanStart | None,
    headers: dict[str, str],
    response: httpx.Response,
    path: str,
    text_rule: TextRule,
) -> NoReturn:
    """Record the ``401`` with a checked body (or none) and raise it with that body."""
    expired = _with_checked_body(response, path, text_rule)
    _record(span, headers, expired, expired.content)
    expired.raise_for_status()
    raise AssertionError("a 401 answer always raises")  # pragma: no cover


def _check_or_record_refusal(
    span: flight_recorder_capture.SpanStart | None,
    headers: dict[str, str],
    response: httpx.Response,
    path: str,
    text_rule: TextRule | None,
) -> CheckedApiFile:
    """Check the file; a refused file is recorded with no body before the refusal is raised."""
    try:
        return check_api_file(
            decode_text(response, path),
            content_type=response.headers.get("content-type"),
            path=path,
            text_rule=text_rule,
        )
    except (ApiFileNotTextError, ApiFileFormatError):
        _record(span, headers, response, b"")
        raise


def _record(
    span: flight_recorder_capture.SpanStart | None,
    headers: dict[str, str],
    response: httpx.Response,
    body: bytes,
) -> None:
    """Record the vendor call with *body* in place of the body as it came."""
    flight_recorder_capture.record_vendor_call(
        span, method="GET", request_headers=headers, response=response, response_body=body
    )


def _checked_body(response: httpx.Response, path: str, text_rule: TextRule) -> bytes:
    """The body as the file check writes it back, or no body when it cannot be checked.

    Whatever stops the check or the encoding of the checked copy (a refused
    file, a number too long to parse, a lone surrogate the copy cannot
    encode) gives no body. The ``401`` must still reach the dispatcher.
    """
    try:
        checked = check_api_file(
            decode_text(response, path),
            content_type=response.headers.get("content-type"),
            path=path,
            text_rule=text_rule,
        )
        return checked.text.encode("utf-8")
    except Exception:
        return b""


def _with_checked_body(response: httpx.Response, path: str, text_rule: TextRule) -> httpx.Response:
    """The same answer with a checked body (:func:`_checked_body`); status and headers stay."""
    return httpx.Response(
        status_code=response.status_code,
        headers=[
            (k, v) for k, v in response.headers.multi_items() if k.lower() != "content-length"
        ],
        content=_checked_body(response, path, text_rule),
        request=response.request,
        extensions=response.extensions,
    )


async def _read_body_bounded(response: httpx.Response, url: str, *, max_bytes: int) -> list[bytes]:
    """Read and decode the streamed body; stop as soon as it passes *max_bytes*."""
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    declared = response.headers.get("content-length")
    if encoding in _IDENTITY_ENCODINGS:
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise ApiFileTooLargeError(
                f"file of {declared} bytes does not fit in one result handle "
                f"(limit {max_bytes} bytes)"
            )
        decoder: _BoundedDecoder | None = None
    elif encoding in _ACCEPT_ENCODING.split(", "):
        decoder = _BoundedDecoder(encoding, max_bytes)
    else:
        raise ApiFileNotTextError(
            f"GET {url} returned content encoding {encoding!r}; only {_ACCEPT_ENCODING} or "
            "no encoding can be decoded"
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_raw(_RAW_CHUNK_BYTES):
        if decoder is None:
            total += len(chunk)
            if total > max_bytes:
                raise ApiFileTooLargeError(
                    f"file of more than {max_bytes} bytes does not fit in one result handle"
                )
            chunks.append(chunk)
            continue
        chunks.extend(decoder.feed(chunk))
    if decoder is not None:
        chunks.append(decoder.finish())
    return chunks


def assert_fits_result_handle(
    answer: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    operator_sub: str,
    op_id: str,
    max_bytes: int,
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
        raise ApiFileTooLargeError(
            f"file of {size} bytes needs {len(items)} parts; one result handle keeps at "
            f"most {settings.result_handle_max_spill_rows}"
        )
    record = rows_record_bytes(items, operator_sub=operator_sub, op_id=op_id, document=answer)
    if record > max_bytes:
        raise ApiFileTooLargeError(
            f"file of {size} bytes ({record} bytes as a stored result record) does not fit in "
            f"one result handle (limit {max_bytes} bytes)"
        )


def text_redactor(operator: Operator, *, connector_id: str, op_id: str) -> TextRule:
    """Return a rule that runs the general secret-redaction rules on one text value.

    The rule returns the changed value and how many places changed. The
    rules and labels are the ones the dispatcher uses on the answer, and
    the value sits under the same ``text`` key as a part does, so the
    dispatcher finds nothing more to change in a value the rule has seen.
    Give the rule to ``check_api_file`` as ``text_rule``: it then runs on
    every parsed text value before the file is written back, and the
    writer quotes the changed value as the format needs. Build ``size``,
    ``sha256`` and the parts from the written copy, so they describe what
    the caller gets.
    """
    tenant = str(operator.tenant_id) if operator.tenant_id is not None else None

    def rule(value: str) -> tuple[str, int]:
        outcome = apply_connector_boundary_redaction(
            {"text": value}, connector_id=connector_id, tenant=tenant, op=op_id
        )
        if outcome.redacted == outcome.raw:
            return value, 0
        return outcome.redacted["text"], sum(entry.count for entry in outcome.manifest)

    return rule


def assert_not_redacted(
    answer: dict[str, Any],
    text: str,
    operator: Operator,
    *,
    connector_id: str,
    op_id: str,
) -> None:
    """Stop when the shared redaction step would change the file's text.

    The dispatcher redacts every answer before the caller sees it. If it
    changed the text, ``sha256`` would no longer name what the caller
    gets. A read that ran :func:`redact_text` first passes this check; it
    stays as a guard. Running the same rules here, with the same labels,
    finds such a change before the answer leaves the read. Redaction stays
    on; the error names the rules and the places, never the matched text.

    The rules run twice: on the whole *text*, because a secret whose label
    ends one part and whose value starts the next is not seen in either
    part alone; and on the *answer* itself, the document the dispatcher
    will redact.
    """
    tenant = str(operator.tenant_id) if operator.tenant_id is not None else None
    for document in ({"path": answer["path"], "text": text}, answer):
        outcome = apply_connector_boundary_redaction(
            document, connector_id=connector_id, tenant=tenant, op=op_id
        )
        if outcome.redacted == outcome.raw:
            continue
        rules = sorted({entry.rule for entry in outcome.manifest})
        places = sum(entry.count for entry in outcome.manifest)
        raise ApiFileRedactedError(
            f"GET {answer['path']}: the secret-redaction rules {rules} would change the text in "
            f"{places} place(s), so the exact file cannot be returned; the read stopped"
        )


def decode_text(response: httpx.Response, path: str) -> str:
    """Return the body as text; refuse anything that is not UTF-8 text."""
    content_type = response.headers.get("content-type")
    if not _is_text_content_type(content_type):
        raise ApiFileNotTextError(
            f"GET {path} returned content type {content_type!r}; only text comes back"
        )
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApiFileNotTextError(
            f"GET {path} returned content type {content_type!r} that is not UTF-8 text"
        ) from exc


def text_items(body: bytes) -> list[dict[str, Any]]:
    """Split a UTF-8 *body* into text parts of at most :data:`PART_BYTES`."""
    return [
        {"index": index, "text": part.decode("utf-8")}
        for index, part in enumerate(split_parts(body))
    ]
