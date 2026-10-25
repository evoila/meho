# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vcfa.vra8.api_file.get`` typed op.

Reads one documentation page or API description file that an Aria
Automation 8 appliance serves: the API landing pages, the Swagger file of
one service, or the reference of the Orchestrator inside the appliance.
No vendor download has the 8.18 files, so a running appliance is the only
source. With this read the files can be pinned, with their size, SHA-256
and the build that served them.

The read is narrow on purpose:

* It accepts only documentation paths (:data:`PATH_PATTERN`): the landing
  pages ``/automation/api-docs`` and ``/automation-ui/api-docs``, the
  folder ``/{service}/api/swagger`` of one service, the Orchestrator
  reference ``/vco/api/docs``, and a Swagger or OpenAPI file at its usual
  place (a path part named ``api-docs``, ``swagger-resources`` or
  ``swagger-config``, or a file named ``swagger.json``, ``swagger.yaml``,
  ``openapi.json`` or ``openapi.yaml``). A folder covers every path under
  it. The query may hold only ``group`` and ``apiVersion``. A path with
  ``..``, ``//``, a backslash, a percent sign, a scheme or a host is
  refused before a request goes out.
* It sends one ``GET`` with the connector's login (the bearer token) to
  the target's own address. It does not follow a redirect: the answer
  names where it points (a path on the target, or ``scheme://host/path``
  for another host, without its query). A ``401`` is raised so the dispatcher
  renews the login and retries once; every other status comes back in
  ``status``. The body of a ``401`` goes through the same file check as a
  returned file, with the same general redaction rules on its text
  values, before the read raises it; a body that cannot be checked is
  left out. So the error result sees only a checked body.
* The file is checked before it comes back
  (:func:`~meho_backplane.connectors._shared.api_file_secrets.check_api_file`).
  MEHO parses it as JSON, YAML, XML or HTML, replaces every value under a
  secret name with ``[REDACTED:param_name]`` and writes the file back
  from the parsed copy. MEHO's general secret-redaction rules run on each
  parsed text value before the copy is written
  (:func:`~meho_backplane.connectors._shared.api_file_read.text_redactor`),
  so a labelled pair such as ``api_key=...`` in a description comes back
  as a placeholder too. ``secrets_replaced`` counts every replaced value.
  A file that does not parse, or whose format is unknown (JavaScript,
  CSS, plain text), is refused with the reason. An example credential in
  a documentation file is replaced like a real one; that is on purpose.
* The checked copy comes back as text parts of at most 64 KiB each.
  Joining the parts in order gives the copy; ``size`` and ``sha256``
  describe the copy. A file that does not fit in one stored result, as
  bytes or as rows, is refused with its size, never cut. Only UTF-8 text
  comes back.
* The stored-result redaction runs once more when the dispatcher stores
  the answer. The read checks that it changes nothing on the copy and on
  the answer, and refuses the file if it would, naming the rule
  (:func:`~meho_backplane.connectors._shared.api_file_read.assert_not_redacted`).
  So the stored parts are always the text the read returns.
* The flight recorder gets the checked copy of the body, never the body
  as it came. When the file is refused, it gets no body.

The version and build come from the Orchestrator inside the appliance
(``GET /vco/api/about``: ``version`` and ``build-number``); the IaaS API
label comes from ``GET /iaas/api/about`` (``latestApiVersion``). When
either read fails, the file still comes back and ``version_error`` says
why.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.api_file_read import (
    ApiFileNotTextError,
    ApiFileTooLargeError,
    assert_not_redacted,
    decode_text,
    fetch_without_redirects,
    redirect_target,
    text_redactor,
)
from meho_backplane.connectors._shared.api_file_secrets import (
    ApiFileFormatError,
    check_api_file,
    scrub_query_string,
)
from meho_backplane.connectors._shared.api_file_values import TextRule
from meho_backplane.connectors._shared.credential_backend import CredentialsReadError
from meho_backplane.connectors.result_handle_store import rows_record_bytes
from meho_backplane.connectors.vra8._paths import _ABOUT_PATH, _VCO_ABOUT_PATH
from meho_backplane.connectors.vra8.session import Vra8TargetLike
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.vra8.connector import Vra8Connector

__all__ = [
    "DOCUMENTATION_PATHS",
    "OP_ID",
    "PART_MAX_BYTES",
    "PATH_PATTERN",
    "SERVICE_SWAGGER_FOLDER",
    "Vra8ApiFileError",
    "check_api_file_path",
    "split_text_parts",
    "vra8_api_file_get_impl",
]

_log = structlog.get_logger(__name__)

OP_ID = "vcfa.vra8.api_file.get"

#: The documentation pages the vendor names: the two API landing pages of
#: Aria Automation 8 and the reference of the Orchestrator inside the
#: appliance. Each is also a folder (every path under it is accepted).
DOCUMENTATION_PATHS: tuple[str, ...] = (
    "/automation/api-docs",
    "/automation-ui/api-docs",
    "/vco/api/docs",
)

#: The Swagger folder of one service, where ``{service}`` is one path part
#: of small letters and hyphens (for example the IaaS or deployment
#: service). Every path under it is accepted.
SERVICE_SWAGGER_FOLDER = "/{service}/api/swagger"

#: Largest text part, in UTF-8 bytes. One ``result_query`` answer holds at
#: most 256 KiB, so a 64 KiB part fits even after JSON escaping.
PART_MAX_BYTES = 64 * 1024

# The path rule, built from named pieces. One path part holds letters,
# digits and . _ ~ -, so no backslash, percent sign (an encoded dot or
# slash), colon (a scheme), at sign (a host), hash or space can get
# through, and two slashes in a row never match. The look-ahead refuses
# '..' anywhere. A folder form accepts every path under it, with or
# without a trailing slash.
_PART = r"[A-Za-z0-9._~-]+"
_UNDER = rf"(?:/{_PART})*/?"
_QUERY_VALUE = r"[A-Za-z0-9._-]+"
_QUERY = rf"(?:\?(?:group|apiVersion)={_QUERY_VALUE}(?:&(?:group|apiVersion)={_QUERY_VALUE})?)?"

#: The path rule, as one pattern. It is also the ``pattern`` of the
#: ``path`` parameter, so the dispatcher refuses a bad path before the
#: handler runs.
PATH_PATTERN = (
    r"^(?!.*\.\.)(?:"
    rf"/automation(?:-ui)?/api-docs{_UNDER}"
    rf"|/[a-z][a-z-]*/api/swagger{_UNDER}"
    rf"|/vco/api/docs{_UNDER}"
    rf"|(?:/{_PART})*/(?:api-docs|swagger-resources|swagger-config){_UNDER}"
    rf"|(?:/{_PART})*/(?:swagger|openapi)\.(?:json|yaml)"
    rf"){_QUERY}$"
)
_PATH_RE = re.compile(PATH_PATTERN)

_TEXT_MEDIA_TYPES = frozenset(
    {"application/json", "application/xml", "application/yaml", "application/x-yaml"}
)


class Vra8ApiFileError(ValueError):
    """The read refused a path or a file. The message says why."""


def check_api_file_path(path: Any) -> str:
    """Return *path* when the read accepts it; raise :exc:`Vra8ApiFileError` if not."""
    if not isinstance(path, str) or not _PATH_RE.fullmatch(path):
        raise Vra8ApiFileError(
            f"vcfa.vra8.api_file.get refused the path {path!r}. The path must be a "
            "documentation path: /automation/api-docs or /automation-ui/api-docs, "
            "/<service>/api/swagger, /vco/api/docs, or a path with a part named "
            "api-docs, swagger-resources or swagger-config, or a file named "
            "swagger.json, swagger.yaml, openapi.json or openapi.yaml (each folder "
            "covers every path under it). It may use only letters, digits and "
            ". _ ~ / -, no '..', and a query of only 'group' and 'apiVersion'. "
            "No request was sent."
        )
    return path


def split_text_parts(data: bytes) -> list[str]:
    """Split UTF-8 *data* into text parts of at most :data:`PART_MAX_BYTES` bytes.

    A cut never falls inside a multi-byte character, so each part decodes
    on its own and ``"".join(parts).encode("utf-8") == data``.
    """
    parts: list[str] = []
    start = 0
    total = len(data)
    while start < total:
        end = min(start + PART_MAX_BYTES, total)
        # Step back off UTF-8 continuation bytes (10xxxxxx).
        while end < total and (data[end] & 0xC0) == 0x80:
            end -= 1
        parts.append(data[start:end].decode("utf-8"))
        start = end
    return parts


def _is_text(content_type: str | None) -> bool:
    """True for a text, JSON, XML or YAML content type, or when the server sent none."""
    if not content_type:
        return True
    media = content_type.split(";", 1)[0].strip().lower()
    return (
        media.startswith("text/")
        or media in _TEXT_MEDIA_TYPES
        or media.endswith(("+json", "+xml", "+yaml"))
    )


def _redirect(response: httpx.Response) -> str | None:
    """Where a ``3xx`` points: a path on this target, or ``scheme://host/path`` elsewhere."""
    if not response.is_redirect:
        return None
    return redirect_target(response.headers.get("location"), response.request.url)


async def _fetch_file(
    connector: Vra8Connector,
    target: Vra8TargetLike,
    path: str,
    headers: dict[str, str],
    span: flight_recorder_capture.SpanStart | None,
    max_bytes: int,
    text_rule: TextRule,
) -> httpx.Response:
    """Send the one ``GET`` with the bearer and return the answer with its body.

    The shared :func:`fetch_without_redirects` sends the request without
    following a redirect and reads the body up to *max_bytes*, also for a
    compressed body. A ``401`` is raised as :class:`httpx.HTTPStatusError`
    with a checked body (:func:`_with_checked_body`): the dispatcher then
    drops the cached bearer, logs in again and retries once, like every
    other read of this connector. The caller records every other answer
    once the body is checked.
    """
    client = await connector._http_client(target)
    try:
        response = await fetch_without_redirects(
            client,
            path,
            headers=headers,
            extensions=connector._request_extensions(target),
            max_bytes=max_bytes,
        )
    except httpx.TransportError as exc:
        flight_recorder_capture.record_vendor_call_error(span, method="GET", exc=exc)
        raise
    except (ApiFileTooLargeError, ApiFileNotTextError) as exc:
        raise Vra8ApiFileError(f"{path!r}: {exc}. The file was not returned.") from exc
    if response.status_code == httpx.codes.UNAUTHORIZED:
        expired = _with_checked_body(response, path, text_rule)
        _record(span, headers, expired, expired.content)
        expired.raise_for_status()
    return response


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

    For an answer the read does not return as a file: a ``401`` or another
    error status. The check is the one a returned file gets: a
    secret-named value comes back as the placeholder, and *text_rule*
    (the general redaction rules) runs on every other text value, so a
    credential inside an error message is replaced too. A body that is
    not text, or does not parse, is left out.
    """
    try:
        checked = check_api_file(
            decode_text(response, path),
            content_type=response.headers.get("content-type"),
            path=path,
            text_rule=text_rule,
        )
    except (ApiFileNotTextError, ApiFileFormatError):
        return b""
    return checked.text.encode("utf-8")


def _with_checked_body(response: httpx.Response, path: str, text_rule: TextRule) -> httpx.Response:
    """The same answer with a checked body (:func:`_checked_body`).

    The status and the headers stay, so the dispatcher still sees the
    login as expired and the retry still runs. When the retry fails too,
    the dispatcher copies this body into the error result.
    """
    return httpx.Response(
        status_code=response.status_code,
        headers=[
            (k, v) for k, v in response.headers.multi_items() if k.lower() != "content-length"
        ],
        content=_checked_body(response, path, text_rule),
        request=response.request,
        extensions=response.extensions,
    )


def _text_field(payload: Any, key: str) -> str | None:
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value else None


async def _read_version(
    connector: Vra8Connector, operator: Operator, target: Vra8TargetLike
) -> dict[str, Any]:
    """Read the version, build and IaaS API label; name every read that failed.

    Both reads use the connector's login. A failure of either one is an
    expected failure of this best-effort read, not of the file read: an
    HTTP error, a login error, or a credential store that cannot serve
    the login.
    """
    failures = (
        httpx.HTTPError,
        OSError,
        RuntimeError,
        ValueError,
        CredentialsReadError,
        VaultClientError,
    )
    answer: dict[str, Any] = {
        "version": None,
        "build": None,
        "iaas_api_version": None,
        "version_error": None,
    }
    errors: list[str] = []
    try:
        about = await connector._vra_get(target, _VCO_ABOUT_PATH, operator=operator)
    except failures as exc:
        errors.append(f"GET {_VCO_ABOUT_PATH} failed: {type(exc).__name__}: {exc}")
    else:
        answer["version"] = _text_field(about, "version")
        answer["build"] = _text_field(about, "build-number")
        if answer["build"] is None:
            errors.append(f"GET {_VCO_ABOUT_PATH} returned no build-number")
    try:
        iaas_about = await connector._vra_get(target, _ABOUT_PATH, operator=operator)
    except failures as exc:
        errors.append(f"GET {_ABOUT_PATH} failed: {type(exc).__name__}: {exc}")
    else:
        answer["iaas_api_version"] = _text_field(iaas_about, "latestApiVersion")
    answer["version_error"] = "; ".join(errors) or None
    return answer


def _decode_text(path: str, content_type: str | None, body: bytes) -> str:
    """Decode a ``2xx`` body as UTF-8 text; refuse other content."""
    if not _is_text(content_type):
        raise Vra8ApiFileError(
            f"{path!r} is {content_type!r}, not text. This read returns only UTF-8 text."
        )
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Vra8ApiFileError(
            f"{path!r} ({content_type or 'no content type'}) is not UTF-8 text: {exc.reason}."
        ) from exc


def _checked_fields(
    path: str, content_type: str | None, text: str, text_rule: TextRule
) -> tuple[dict[str, Any], bytes]:
    """Check the file and build its fields: ``size`` and ``sha256`` of the copy, the parts.

    The shared check parses the file, replaces every value under a secret
    name and runs *text_rule* (MEHO's general redaction rules, with the
    labels the dispatcher uses) on each text value, then writes the copy
    back. The parts, ``size`` and ``sha256`` all describe that copy, which
    comes back as bytes too, for the flight recorder.
    """
    checked = check_api_file(text, content_type=content_type, path=path, text_rule=text_rule)
    body = checked.text.encode("utf-8")
    parts = split_text_parts(body)
    fields = {
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "secrets_replaced": checked.secrets_replaced,
        "parts": len(parts),
        "text_parts": [{"index": index, "text": part} for index, part in enumerate(parts)],
    }
    return fields, body


def _check_fits(answer: dict[str, Any], operator: Operator) -> None:
    """Refuse an answer too big to keep whole in one stored result.

    The store keeps the text parts as rows and the other fields beside
    them. It keeps at most ``result_handle_max_spill_rows`` rows and one
    record of at most ``result_handle_max_record_bytes``; the record is
    measured as the store writes it (:func:`rows_record_bytes`), with the
    record's own fields, so a file that passes here is stored whole.
    """
    settings = get_settings()
    rows = answer["text_parts"]
    max_rows = settings.result_handle_max_spill_rows
    if len(rows) > max_rows:
        raise Vra8ApiFileError(
            f"{answer['path']!r} is {answer['size']} bytes in {len(rows)} parts. One stored "
            f"result keeps at most {max_rows} parts (result_handle_max_spill_rows), so the "
            "file was not returned. This read never returns a cut file."
        )
    document = {key: value for key, value in answer.items() if key != "text_parts"}
    record = rows_record_bytes(rows, operator_sub=operator.sub, op_id=OP_ID, document=document)
    max_record_bytes = settings.result_handle_max_record_bytes
    if record > max_record_bytes:
        raise Vra8ApiFileError(
            f"{answer['path']!r} is {answer['size']} bytes ({record} bytes as a stored "
            f"result). One stored result holds at most {max_record_bytes} bytes, so the "
            "file was not returned. This read never returns a cut file."
        )


async def vra8_api_file_get_impl(
    connector: Vra8Connector,
    operator: Operator,
    target: Vra8TargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vcfa.vra8.api_file.get``.

    1. Check the path (no request goes out for a refused path).
    2. ``GET`` the file with the connector's login, without following a
       redirect.
    3. Read the version, build and IaaS API label (best effort).
    4. Check the file: parse it, replace every secret value with a
       placeholder and count the replacements (``secrets_replaced``).
       Refuse a file that does not parse, does not fit in one stored
       result, or whose copy the stored-result redaction would still
       change.

    A non-``2xx`` status comes back in ``status`` with no text. A ``3xx``
    also gives ``redirect_path``.
    """
    path = check_api_file_path(params.get("path"))
    max_record_bytes = get_settings().result_handle_max_record_bytes
    connector_id = f"{connector.impl_id}-{connector.version}"

    text_rule = text_redactor(operator, connector_id=connector_id, op_id=OP_ID)

    headers = await connector.auth_headers(target, operator)
    span = flight_recorder_capture.span_start()
    response = await _fetch_file(
        connector, target, path, headers, span, max_record_bytes, text_rule
    )
    status = response.status_code
    content_type = response.headers.get("content-type")
    redirect_to = _redirect(response)
    redirect, redirect_replaced = scrub_query_string(redirect_to) if redirect_to else (None, 0)
    answer: dict[str, Any] = {
        "path": path,
        "status": status,
        "content_type": content_type,
        "redirect_path": redirect,
        "size": None,
        "sha256": None,
        "secrets_replaced": redirect_replaced,
        "parts": 0,
        "text_parts": [],
    }
    if response.is_success:
        try:
            text = _decode_text(path, content_type, response.content)
            fields, body = _checked_fields(path, content_type, text, text_rule)
        except (Vra8ApiFileError, ApiFileFormatError):
            _record(span, headers, response, b"")
            raise
        _record(span, headers, response, body)
        answer.update(fields)
    else:
        _record(span, headers, response, _checked_body(response, path, text_rule))
    answer.update(await _read_version(connector, operator, target))
    if answer["parts"]:
        file_text = "".join(part["text"] for part in answer["text_parts"])
        _check_fits(answer, operator)
        assert_not_redacted(answer, file_text, operator, connector_id=connector_id, op_id=OP_ID)

    _log.info(
        "vra8_api_file_read",
        target=target.name,
        path=path,
        status=answer["status"],
        size=answer["size"],
        parts=answer["parts"],
        secrets_replaced=answer["secrets_replaced"],
        has_build=answer["build"] is not None,
    )
    return answer
