# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vrli.api_file.get`` -- read one API reference page or file a Logs system serves.

Some calls that VMware's own tools send to Aria Operations for Logs 8.18
and VCF Operations for Logs 9.0 appear in no vendor reference and not in
the pinned 9.0 API file. Only the reference that a running system serves
can show them: the API reference at ``/rest-api`` and the Tech Preview API
pages at ``/rest-api/supported``, plus the Swagger or OpenAPI files those
pages load. This read fetches one such page or file and returns its path,
HTTP status, content type, size, SHA-256 and the system version and build,
with the text in parts behind a result handle. The text is the checked
copy of the file: MEHO parses the file (JSON, YAML, XML or HTML) and
replaces every value under a secret name with a placeholder before it is
cut into parts (:mod:`meho_backplane.connectors._shared.api_file_secrets`).
Joining the parts in order gives that copy, so the vendor API files can be
pinned and compared.

The read uses the target's own address. The vendor writes the reference
as ``http://{host}/rest-api`` (the web address, not the API port 9543).
If the target's address is the API port and the page is not served there,
the status in the answer shows it.

What the read accepts
---------------------

Only two kinds of path, checked before any vendor call:

1. the documentation page ``/rest-api`` and every path under the folder
   ``/rest-api/`` (for example ``/rest-api/supported``);
2. a Swagger or OpenAPI file at its usual place: a path with a part named
   ``api-docs``, ``swagger-resources`` or ``swagger-config``, or a path
   that ends with ``swagger.json``, ``swagger.yaml``, ``openapi.json`` or
   ``openapi.yaml``.

Every other path is refused. So is a path with ``..``, ``//``, ``\\``, an
encoded dot or slash, a semicolon, a scheme, a host name, a fragment or a
control character. A query string may hold only ``group`` and ``apiVersion``.

The read sends one ``GET`` to the target's own address with the target's
own login (the session token). It does not follow a redirect: the answer
carries the redirect's path in ``redirect_to``, and the caller reads that
path next. The fetch, the bounded decoding of a compressed body, the size
limit and the parts are the shared steps of
:mod:`meho_backplane.connectors._shared.api_file_read`. Only UTF-8 text
comes back. A file whose stored record does not fit in one result handle
stops with an error that names its size -- the read never returns a cut
file. A file that does not parse, or whose format is unknown (JavaScript,
CSS, plain text), is refused with the reason. The checked copy is what
``size`` and ``sha256`` describe, and ``secrets_replaced`` says how many
values were replaced. MEHO's general secret-redaction rules also run on
every parsed text value before the file is written back
(:func:`~meho_backplane.connectors._shared.api_file_read.text_redactor`):
the vendor's Logs 9.0 API file has ``Authorization: Bearer ...`` examples
in its description text, and these come back as a placeholder too. The
count includes them. A copy that the rules would still change stops; the
rules run on the whole text and on the answer, so a secret that spans
two parts is found as well, and ``sha256`` always names what the caller
gets.

The flight recorder gets the checked copy, never the body as it came. The
body of a ``401`` or ``440`` answer goes through the same check before the
read raises it for the login retry; a body that is not text, or does not
parse, is withheld. So the error result and the recorder see only a
checked body.

Version and build come from the connector's own version detection
(``GET /api/v2/version``, which needs no login). If that read fails, the
file still comes back: ``version`` and ``build`` are empty and
``version_error`` says why.

The same module serves both Logs implementations (``vrli-rest-9.0`` and
``vrli-vrli8-8.0``): the 8.x connector is a subclass of the 9.0 one and
reuses the handler.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.api_file_read import (
    ApiFileNotTextError,
    assert_fits_result_handle,
    assert_not_redacted,
    decode_text,
    fetch_without_redirects,
    redirect_target,
    text_items,
    text_redactor,
)
from meho_backplane.connectors._shared.api_file_secrets import (
    ApiFileFormatError,
    check_api_file,
    scrub_query_string,
)
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.connectors.vcf_logs.session import VcfLogsTargetLike
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.vcf_logs.connector import VcfLogsConnector

__all__ = [
    "DOCS_FOLDERS",
    "DOCS_PAGES",
    "VrliApiFilePathError",
    "check_path",
    "vrli_api_file_get_impl",
]

#: The documentation page a running Logs system serves (``GET``): the API
#: reference, as the vendor's developer resources for 8.18 name it.
DOCS_PAGES: tuple[str, ...] = ("/rest-api",)

#: The documentation folder (``GET``): every path under the API reference,
#: for example ``/rest-api/supported`` (the Tech Preview API pages). A
#: folder covers every path under it.
DOCS_FOLDERS: tuple[str, ...] = ("/rest-api/",)

#: Path parts that name a Swagger or OpenAPI description, not data.
_API_FILE_SEGMENTS = frozenset({"api-docs", "swagger-resources", "swagger-config"})
_API_FILE_SUFFIXES = ("swagger.json", "swagger.yaml", "openapi.json", "openapi.yaml")

#: The only query keys a documentation path may carry.
_ALLOWED_QUERY_KEYS = frozenset({"group", "apiVersion"})

#: Characters and encoded forms that have no place in a documentation or
#: API file path. Checked on the raw string, before any decoding.
_REFUSED_FRAGMENTS = ("..", "//", "\\", "://", "%2e", "%2f", "%5c", "#", ";", "%3b")

#: The operation id the dispatcher labels this read with. The redaction
#: check and the stored-record estimate use the same label.
_OP_ID = "vrli.api_file.get"

#: The statuses that mean "the session is gone": ``401`` and the system's
#: own ``440``. Raised so the dispatcher re-logs-in and dispatches once more.
_SESSION_EXPIRED_STATUSES = frozenset({httpx.codes.UNAUTHORIZED, 440})


class VrliApiFilePathError(ValueError):
    """The path is outside the rule; no HTTP request was sent."""


def check_path(raw: Any) -> tuple[str, str]:
    """Check *raw* against the path rule. Return ``(path, query)`` to send.

    Raises :exc:`VrliApiFilePathError` for everything the rule refuses. The
    check runs on the raw string before any decoding, so an encoded dot or
    slash cannot slip through.
    """
    if not isinstance(raw, str) or not raw:
        raise VrliApiFilePathError("path must be a non-empty string")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in raw):
        raise VrliApiFilePathError("path must not contain whitespace or control characters")
    lowered = raw.lower()
    for fragment in _REFUSED_FRAGMENTS:
        if fragment in lowered:
            raise VrliApiFilePathError(f"path must not contain {fragment!r}")
    if not raw.startswith("/"):
        raise VrliApiFilePathError("path must start with '/' and name no scheme or host")
    path, _, query = raw.partition("?")
    if query:
        keys = {key for key, _ in parse_qsl(query, keep_blank_values=True)}
        unexpected = sorted(keys - _ALLOWED_QUERY_KEYS)
        if unexpected or not keys:
            raise VrliApiFilePathError(
                f"query string may hold only {sorted(_ALLOWED_QUERY_KEYS)}; got {unexpected}"
            )
    if _is_docs_path(path) or _is_api_description_path(path):
        return path, query
    raise VrliApiFilePathError(
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


def _system_version(fingerprint: FingerprintResult) -> tuple[str | None, str | None, str | None]:
    """Reduce the connector's fingerprint to ``(version, build, error)``.

    The fingerprint never raises: an unreachable system reports
    ``reachable=False`` with the reason in ``extras["error"]``. A reachable
    system whose answer names no version is reported the same way, so the
    caller always learns why ``version`` is empty.
    """
    if not fingerprint.reachable:
        return None, None, str(fingerprint.extras.get("error") or "version read failed")
    if fingerprint.version is None:
        return None, None, "version answer names no version"
    return fingerprint.version, fingerprint.build, None


async def vrli_api_file_get_impl(
    connector: VcfLogsConnector,
    operator: Operator,
    target: VcfLogsTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vrli.api_file.get`` -- ``GET {path}`` on the Logs system, text in parts.

    A ``401`` or a ``440`` (the system's own "session expired" status) is
    raised as :exc:`httpx.HTTPStatusError` so the dispatcher drops the
    cached session and dispatches once more; its body is the checked copy,
    or empty (:func:`_session_expired_response`). Every other status is
    reported in ``status``. The flight recorder gets the checked copy only.
    """
    path, query = check_path(params.get("path"))
    url = path if not query else f"{path}?{query}"
    max_bytes = get_settings().result_handle_max_record_bytes
    client = await connector._http_client(target)
    headers = await connector.auth_headers(target, operator)
    span = flight_recorder_capture.span_start()
    try:
        response = await fetch_without_redirects(
            client,
            url,
            headers=headers,
            extensions=connector._request_extensions(target),
            max_bytes=max_bytes,
        )
    except httpx.TransportError as exc:
        flight_recorder_capture.record_vendor_call_error(span, method="GET", exc=exc)
        raise
    if response.status_code in _SESSION_EXPIRED_STATUSES:
        expired = _session_expired_response(response, path)
        _record(span, headers, expired, expired.content)
        expired.raise_for_status()

    content_type = response.headers.get("content-type")
    connector_id = f"{connector.impl_id}-{connector.version}"
    try:
        checked = check_api_file(
            decode_text(response, path),
            content_type=content_type,
            path=path,
            text_rule=text_redactor(operator, connector_id=connector_id, op_id=_OP_ID),
        )
    except (ApiFileNotTextError, ApiFileFormatError):
        _record(span, headers, response, b"")
        raise
    text = checked.text
    body = text.encode("utf-8")
    _record(span, headers, response, body)
    items = text_items(body)
    redirect_to, redirect_replaced = _redirect(response)
    version, build, version_error = _system_version(await connector.fingerprint(target))
    answer: dict[str, Any] = {
        "path": url,
        "status": response.status_code,
        "content_type": content_type,
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "secrets_replaced": checked.secrets_replaced + redirect_replaced,
        "version": version,
        "build": build,
        "version_error": version_error,
        "redirect_to": redirect_to,
        "parts": len(items),
    }
    assert_fits_result_handle(
        answer, items, operator_sub=operator.sub, op_id=_OP_ID, max_bytes=max_bytes
    )
    answer["items"] = items
    assert_not_redacted(answer, text, operator, connector_id=connector_id, op_id=_OP_ID)
    return answer


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


def _session_expired_response(response: httpx.Response, path: str) -> httpx.Response:
    """The ``401`` or ``440`` answer to raise: the same status, with a checked body.

    The dispatcher drops the cached session and asks once more. When that
    fails too, it copies the body of the second answer into the error
    result. So the body goes through the same file check as a page first:
    a secret-named value comes back as the placeholder, and a body that is
    not text or does not parse is withheld. The status and the headers
    stay, so the dispatcher still sees the session as expired and the
    login retry still runs.
    """
    try:
        content = check_api_file(
            decode_text(response, path),
            content_type=response.headers.get("content-type"),
            path=path,
        ).text.encode("utf-8")
    except (ApiFileNotTextError, ApiFileFormatError):
        content = b""
    return httpx.Response(
        status_code=response.status_code,
        headers=[
            (k, v) for k, v in response.headers.multi_items() if k.lower() != "content-length"
        ],
        content=content,
        request=response.request,
        extensions=response.extensions,
    )


def _redirect(response: httpx.Response) -> tuple[str | None, int]:
    """The redirect path with any secret-named query value replaced, and the count."""
    target = redirect_target(response.headers.get("location"), response.request.url)
    if target is None:
        return None, 0
    return scrub_query_string(target)
