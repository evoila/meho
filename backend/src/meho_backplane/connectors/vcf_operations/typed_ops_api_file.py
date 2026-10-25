# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vrops.api_file.get`` typed op.

Reads one documentation page or API description file that a VCF
Operations 9.x or Aria Operations 8.x appliance serves, so the file can
be pinned and the vROps calls checked against it. The vendor publishes no
API file for Aria Operations 8.18, so a running appliance is the only
source; on 9.x the same read lets the pinned files be compared with what
an appliance serves.

The read is narrow on purpose:

* It accepts only the API start page ``/suite-api/``, a path under the
  documentation folder ``/suite-api/doc/`` (the Swagger page
  ``/suite-api/doc/swagger-ui.html`` and the files under it), or a Swagger
  or OpenAPI file at its usual place: a path with a part named
  ``api-docs``, ``swagger-resources`` or ``swagger-config``, or a path
  that ends with ``swagger.json``, ``swagger.yaml``, ``openapi.json`` or
  ``openapi.yaml``. No ``..``, ``//``, backslash, percent sign, scheme or
  host; the query may hold only ``group`` and ``apiVersion``. Any other
  path is refused before a request goes out.
* It sends one ``GET`` to the target's own address with the target's own
  login (the connector's token session), on the pooled client, through
  the shared
  :func:`~meho_backplane.connectors._shared.api_file_read.fetch_without_redirects`:
  the body is read up to the stored-result limit, also a ``gzip`` or
  ``deflate`` body, measured after decoding.
* A ``401`` is raised, so the dispatcher renews the login and retries
  once, like every other read of this connector. Its body goes through
  the file check below first, and a body that cannot be checked is left
  out, so the error result sees only a checked body. Every other status
  comes back in ``status``.
* It does not follow a redirect. The answer names where it points: a
  path on the target, or ``scheme://host/path`` for another host,
  without its query. A secret-named query setting in a path on the
  target (``?licenseKey=...``, also with an encoded name) comes back
  with its value replaced by the placeholder.
* The read never returns a secret. The shared check
  (:mod:`meho_backplane.connectors._shared.api_file_secrets`) parses the
  whole file (JSON, YAML, XML or HTML) and replaces every value under a
  secret name with ``[REDACTED:param_name]`` before the file is cut into
  parts. MEHO's general secret-redaction rules run on each parsed text
  value too
  (:func:`~meho_backplane.connectors._shared.api_file_read.text_redactor`),
  so an ``Authorization: ...`` example in a description comes back as a
  placeholder instead of stopping the read. ``secrets_replaced`` counts
  both. A file that does not parse, or whose format is unknown
  (JavaScript, CSS, plain text), is refused.
* The checked copy comes back as text parts of at most 64 KiB each,
  measured as JSON text, so a part always fits one ``result_query``
  answer. Joining the parts in order gives the checked copy; ``size`` and
  ``sha256`` describe it. A file that does not fit in one stored result is
  refused with its size, never cut.
* The read then runs the redaction rules the dispatcher will apply, on
  the whole copy and on the answer, and refuses a copy whose text they
  would still change
  (:func:`~meho_backplane.connectors._shared.api_file_read.assert_not_redacted`).
  So the stored parts always match ``sha256``.
* The flight recorder gets the checked copy of the body, never the body
  as it came. When the file is refused, it gets no body.

The appliance version and build come from
``GET /suite-api/api/versions/current`` (``releaseName``, ``buildNumber``),
the call MEHO's version detection already makes. When that read fails,
the file still comes back and ``version_error`` says why.
"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from itertools import accumulate
from typing import TYPE_CHECKING, Any

import httpx
import msgspec
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
from meho_backplane.connectors._shared.credential_backend import CredentialsReadError
from meho_backplane.connectors.vcf_operations.connector import _VERSIONS_CURRENT_PATH
from meho_backplane.connectors.vcf_operations.session import VcfOperationsTargetLike
from meho_backplane.connectors.vcf_operations.typed_ops import VropsTypedOp
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.vcf_operations.connector import VcfOperationsConnector

__all__ = [
    "API_FILE_GROUP_KEY",
    "API_FILE_WHEN_TO_USE",
    "API_START_PAGE",
    "DOC_FOLDER",
    "OP_ID",
    "PART_MAX_BYTES",
    "PATH_PATTERN",
    "VROPS_API_FILE_GET_OP",
    "VropsApiFileError",
    "api_file_get_impl",
    "check_api_file_path",
    "split_text_parts",
]

_log = structlog.get_logger(__name__)

OP_ID = "vrops.api_file.get"

#: The API start page the vendor documents.
API_START_PAGE = "/suite-api/"

#: The documentation folder: the Swagger page ``swagger-ui.html`` and the
#: files under it. A folder covers every path under it.
DOC_FOLDER = "/suite-api/doc/"

#: Largest text part, in bytes of JSON text (the part as a JSON string,
#: without the quotes). One ``result_query`` answer holds at most 256 KiB
#: of JSON, so a part always fits, whatever it escapes.
PART_MAX_BYTES = 64 * 1024

#: How many bytes one byte of UTF-8 takes as JSON text: ``"`` and ``\``
#: take 2, the controls with a short escape (``\b \t \n \f \r``) take
#: 2, the other controls take 6 (``\u00XX``), every other byte takes 1.
_JSON_BYTE_COST = bytes(
    2 if byte in (0x22, 0x5C, 0x08, 0x09, 0x0A, 0x0C, 0x0D) else 6 if byte < 0x20 else 1
    for byte in range(256)
)

#: Room kept free in one stored result for the record's own fields.
_RECORD_OVERHEAD_BYTES = 4096

#: One path part: letters, digits and ``. _ ~ -``. It leaves out ``/``, so
#: ``//`` cannot occur, and ``\``, ``%``, ``:``, ``@``, ``#`` and spaces,
#: so no encoded dot or slash, scheme or host can get through.
_PART = r"[A-Za-z0-9._~-]+"
#: One query setting the Swagger page uses.
_QUERY = r"(?:group|apiVersion)=[A-Za-z0-9._~-]+"

#: The path rule, as one pattern. It is also the ``pattern`` of the
#: ``path`` parameter, so the dispatcher refuses a bad path before the
#: handler runs. The look-aheads refuse ``..`` and ``//`` anywhere.
PATH_PATTERN = (
    r"^(?!.*\.\.)(?!.*//)(?:"
    rf"{API_START_PAGE}"
    rf"|{DOC_FOLDER}(?:{_PART}/)*(?:{_PART})?"
    rf"|(?:/{_PART})*/(?:api-docs|swagger-resources|swagger-config)(?:/{_PART})*/?"
    rf"|(?:/{_PART})*/(?:swagger|openapi)\.(?:json|yaml)"
    rf")(?:\?{_QUERY}(?:&{_QUERY})?)?$"
)
_PATH_RE = re.compile(PATH_PATTERN)

_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/javascript",
    }
)

API_FILE_GROUP_KEY = "vrops-api-files"


class VropsApiFileError(ValueError):
    """The read refused a path or a file. The message says why."""


def check_api_file_path(path: Any) -> str:
    """Return *path* when the read accepts it; raise :exc:`VropsApiFileError` if not."""
    if not isinstance(path, str) or not _PATH_RE.fullmatch(path):
        raise VropsApiFileError(
            f"vrops.api_file.get refused the path {path!r}. The path must be "
            f"{API_START_PAGE!r}, a path under {DOC_FOLDER!r}, a path with a part named "
            "'api-docs', 'swagger-resources' or 'swagger-config', or a path that ends with "
            "'swagger.json', 'swagger.yaml', 'openapi.json' or 'openapi.yaml'. It may use "
            "only letters, digits and . _ ~ / -, no '..' or '//', and a query of 'group' "
            "and 'apiVersion' only. No request was sent."
        )
    return path


def _character_start(data: bytes, end: int) -> int:
    """Step *end* back off UTF-8 continuation bytes (``10xxxxxx``)."""
    while end < len(data) and (data[end] & 0xC0) == 0x80:
        end -= 1
    return end


def _json_fit(chunk: bytes) -> int:
    """How many leading bytes of *chunk* take at most :data:`PART_MAX_BYTES` as JSON."""
    sizes = list(accumulate(chunk.translate(_JSON_BYTE_COST)))
    return bisect_right(sizes, PART_MAX_BYTES)


def split_text_parts(data: bytes) -> list[str]:
    """Split UTF-8 *data* into text parts of at most :data:`PART_MAX_BYTES` bytes.

    The bound is on the part as JSON text, so a part full of quotes,
    backslashes or control characters stays small enough for one
    ``result_query`` answer. A cut never falls inside a multi-byte
    character, so each part decodes on its own and
    ``"".join(parts).encode("utf-8") == data``.
    """
    parts: list[str] = []
    start = 0
    total = len(data)
    while start < total:
        end = _character_start(data, min(start + PART_MAX_BYTES, total))
        if len(msgspec.json.encode(data[start:end].decode("utf-8"))) - 2 > PART_MAX_BYTES:
            end = _character_start(data, start + _json_fit(data[start:end]))
        parts.append(data[start:end].decode("utf-8"))
        start = end
    return parts


def _is_text(content_type: str | None) -> bool:
    """True for a text, JSON, XML, YAML or script content type, or when none was sent."""
    if not content_type:
        return True
    media = content_type.split(";", 1)[0].strip().lower()
    return (
        media.startswith("text/")
        or media in _TEXT_MEDIA_TYPES
        or media.endswith("+json")
        or media.endswith("+xml")
    )


async def _fetch_file(
    connector: VcfOperationsConnector,
    target: VcfOperationsTargetLike,
    path: str,
    headers: dict[str, str],
    span: flight_recorder_capture.SpanStart | None,
    max_bytes: int,
) -> httpx.Response:
    """Send the one ``GET`` with the target's login and return the answer with its body.

    The request carries the connector's token header (``OpsToken`` on 9.x,
    ``vRealizeOpsToken`` on 8.x). The shared :func:`fetch_without_redirects`
    sends it without following a redirect and reads the body up to
    *max_bytes*, also for a compressed body. A ``401`` is raised as
    :class:`httpx.HTTPStatusError` with a checked body
    (:func:`_with_checked_body`): the dispatcher then drops the cached
    token, logs in again and retries once. The caller records every other
    answer once the body is checked.
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
        raise VropsApiFileError(f"{path!r}: {exc}. The file was not read.") from exc
    if response.status_code == httpx.codes.UNAUTHORIZED:
        expired = _with_checked_body(response, path)
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


def _checked_body(response: httpx.Response, path: str) -> bytes:
    """The body as the file check writes it back, or no body when it cannot be checked.

    For an answer the read does not return as a file: a ``401`` or another
    error status. A secret-named value comes back as the placeholder; a
    body that is not text, or does not parse, is left out.
    """
    try:
        checked = check_api_file(
            decode_text(response, path),
            content_type=response.headers.get("content-type"),
            path=path,
        )
    except (ApiFileNotTextError, ApiFileFormatError):
        return b""
    return checked.text.encode("utf-8")


def _with_checked_body(response: httpx.Response, path: str) -> httpx.Response:
    """The same answer with a checked body (:func:`_checked_body`).

    The status and the headers stay, so the dispatcher still sees the
    session as expired and the retry still runs. When the retry fails too,
    the dispatcher copies this body into the error result.
    """
    return httpx.Response(
        status_code=response.status_code,
        headers=[
            (k, v) for k, v in response.headers.multi_items() if k.lower() != "content-length"
        ],
        content=_checked_body(response, path),
        request=response.request,
        extensions=response.extensions,
    )


async def _read_version(
    connector: VcfOperationsConnector, operator: Operator, target: VcfOperationsTargetLike
) -> dict[str, Any]:
    """Read ``version`` and ``build``; on failure return ``None`` and the reason.

    The read uses the connector's normal login, so it can also fail before
    any request goes out (no credentials, access denied, a credential store
    out of reach). Those are expected failures of this best-effort read.
    """
    try:
        payload = await connector._get_json(target, _VERSIONS_CURRENT_PATH, operator=operator)
    except (
        httpx.HTTPError,
        OSError,
        RuntimeError,
        ValueError,
        CredentialsReadError,
        VaultClientError,
    ) as exc:
        return {
            "version": None,
            "build": None,
            "version_error": f"GET {_VERSIONS_CURRENT_PATH} failed: {type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        payload = {}
    version = payload.get("releaseName")
    build = payload.get("buildNumber")
    version = version if isinstance(version, str) and version else None
    build = str(build) if isinstance(build, int | str) and build != "" else None
    error = None if build else f"GET {_VERSIONS_CURRENT_PATH} returned no buildNumber"
    return {"version": version, "build": build, "version_error": error}


def _file_fields(
    path: str, content_type: str | None, body: bytes, operator: Operator, connector_id: str
) -> tuple[dict[str, Any], bytes]:
    """Decode a ``2xx`` body, check the whole file and build the file fields of the answer.

    The shared check runs on the whole text before it is cut into parts,
    so a part cut cannot hide a secret. MEHO's general redaction rules run
    on each parsed text value too, with the labels the dispatcher uses
    (:func:`text_redactor`), so an example such as ``Authorization: ...``
    in a description comes back as a placeholder instead of stopping the
    read. ``size``, ``sha256`` and the parts describe the checked copy,
    which comes back as bytes too, for the flight recorder.
    """
    if not _is_text(content_type):
        raise VropsApiFileError(
            f"{path!r} is {content_type!r}, not text. This read returns only UTF-8 text."
        )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VropsApiFileError(
            f"{path!r} ({content_type or 'no content type'}) is not UTF-8 text: {exc.reason}."
        ) from exc
    try:
        checked = check_api_file(
            text,
            content_type=content_type,
            path=path,
            text_rule=text_redactor(operator, connector_id=connector_id, op_id=OP_ID),
        )
    except ApiFileFormatError as exc:
        raise VropsApiFileError(str(exc)) from None
    copy = checked.text.encode("utf-8")
    parts = split_text_parts(copy)
    fields = {
        "size": len(copy),
        "sha256": hashlib.sha256(copy).hexdigest(),
        "secrets_replaced": checked.secrets_replaced,
        "parts": len(parts),
        "text_parts": [{"index": index, "text": text} for index, text in enumerate(parts)],
    }
    return fields, copy


def _check_fits(answer: dict[str, Any], max_record_bytes: int, max_rows: int) -> None:
    """Refuse an answer too big to keep whole in one stored result.

    The text parts are rows, so the row cap and the record byte cap both
    apply. The reducer would cut the tail silently; this read never does.
    """
    if answer["parts"] > max_rows:
        raise VropsApiFileError(
            f"{answer['path']!r} is {answer['size']} bytes ({answer['parts']} text parts). "
            f"One stored result holds at most {max_rows} parts, so the file was not "
            "returned. This read never returns a cut file."
        )
    encoded = len(msgspec.json.encode(answer))
    if encoded + _RECORD_OVERHEAD_BYTES > max_record_bytes:
        raise VropsApiFileError(
            f"{answer['path']!r} is {answer['size']} bytes ({encoded} bytes as a stored "
            f"result). One stored result holds at most {max_record_bytes} bytes, so the "
            "file was not returned. This read never returns a cut file."
        )


async def api_file_get_impl(
    connector: VcfOperationsConnector,
    operator: Operator,
    target: VcfOperationsTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vrops.api_file.get``.

    1. Check the path (no request goes out for a refused path).
    2. ``GET`` the file with the target's login, without following a
       redirect. A ``401`` is raised for the login retry.
    3. Read the appliance version and build (best effort).
    4. Check the whole file and replace every secret value (see
       :func:`_file_fields`); replace a secret-named query value in
       ``redirect_path``.
    5. Refuse a copy that does not fit in one stored result, or whose
       text the stored-result redaction would still change.

    A non-``2xx`` status comes back in ``status`` with no text. A ``3xx``
    also gives ``redirect_path``.
    """
    path = check_api_file_path(params.get("path"))
    settings = get_settings()
    max_record_bytes = settings.result_handle_max_record_bytes
    connector_id = f"{connector.impl_id}-{connector.version}"

    headers = await connector.auth_headers(target, operator)
    span = flight_recorder_capture.span_start()
    response = await _fetch_file(connector, target, path, headers, span, max_record_bytes)
    content_type = response.headers.get("content-type")
    redirect = (
        redirect_target(response.headers.get("location"), response.request.url)
        if response.is_redirect
        else None
    )
    redirect_path, redirect_replaced = scrub_query_string(redirect) if redirect else (None, 0)
    answer: dict[str, Any] = {
        "path": path,
        "status": response.status_code,
        "content_type": content_type,
        "redirect_path": redirect_path,
        "size": None,
        "sha256": None,
        "secrets_replaced": 0,
        "parts": 0,
        "text_parts": [],
    }
    if response.is_success:
        try:
            fields, copy = _file_fields(
                path, content_type, response.content, operator, connector_id
            )
        except VropsApiFileError:
            _record(span, headers, response, b"")
            raise
        _record(span, headers, response, copy)
        answer.update(fields)
    else:
        _record(span, headers, response, _checked_body(response, path))
    answer["secrets_replaced"] += redirect_replaced
    answer.update(await _read_version(connector, operator, target))
    if answer["parts"]:
        _check_fits(answer, max_record_bytes, settings.result_handle_max_spill_rows)
        file_text = "".join(part["text"] for part in answer["text_parts"])
        assert_not_redacted(answer, file_text, operator, connector_id=connector_id, op_id=OP_ID)

    _log.info(
        "vrops_api_file_read",
        target=target.name,
        path=path,
        status=answer["status"],
        size=answer["size"],
        parts=answer["parts"],
        secrets_replaced=answer["secrets_replaced"],
        has_build=answer["build"] is not None,
    )
    return answer


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "maxLength": 512,
            "pattern": PATH_PATTERN,
            "description": (
                "Path of one documentation page or API description file on the target "
                "appliance: '/suite-api/' (the API start page), a path under "
                "'/suite-api/doc/' (the Swagger page 'swagger-ui.html' and the files under "
                "it), a path with a part named 'api-docs', 'swagger-resources' or "
                "'swagger-config', or a path that ends with 'swagger.json', 'swagger.yaml', "
                "'openapi.json' or 'openapi.yaml'. Allowed characters are letters, digits "
                "and . _ ~ / -, no '..' or '//'; the query may hold only 'group' and "
                "'apiVersion'. Replace FILE with the real page or file name."
            ),
        },
    },
    "required": ["path"],
    "additionalProperties": False,
    "examples": [{"path": "/suite-api/doc/FILE"}],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The path that was read."},
        "status": {"type": "integer", "description": "HTTP status of the GET."},
        "content_type": {"type": ["string", "null"], "description": "Content-Type of the answer."},
        "redirect_path": {
            "type": ["string", "null"],
            "description": "For a 3xx answer: where the redirect points (not followed): a "
            "path on the target, or scheme://host/path for another host. A secret-named "
            "query value in a path on the target is replaced by [REDACTED:param_name].",
        },
        "size": {
            "type": ["integer", "null"],
            "description": "Size in bytes of the checked copy; null unless 2xx.",
        },
        "sha256": {
            "type": ["string", "null"],
            "description": "SHA-256 (hex) of the checked copy; null unless 2xx.",
        },
        "secrets_replaced": {
            "type": "integer",
            "description": "How many secret values the read replaced with "
            "[REDACTED:param_name], in the file and in redirect_path.",
        },
        "parts": {"type": "integer", "description": "How many text parts the file has."},
        "text_parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Position of the part."},
                    "text": {
                        "type": "string",
                        "description": "At most 64 KiB of the file (as JSON text).",
                    },
                },
                "required": ["index", "text"],
            },
            "description": "The checked copy, in order. Joining all parts gives the copy.",
        },
        "version": {"type": ["string", "null"], "description": "releaseName of the appliance."},
        "build": {"type": ["string", "null"], "description": "buildNumber of the appliance."},
        "version_error": {"type": ["string", "null"], "description": "Why the version is null."},
    },
    "required": ["path", "status", "secrets_replaced", "parts", "text_parts"],
}

#: Group text: says what the group holds, makes no claim for other ops.
API_FILE_WHEN_TO_USE = (
    "This group holds one read: vrops.api_file.get. It reads one documentation page or "
    "API description file that a VCF Operations or Aria Operations appliance serves "
    "(the Swagger page under /suite-api/doc/ and the files it loads), with its size, "
    "SHA-256 and the appliance version and build. Use it to get the API description of "
    "a version that has no vendor download, or to compare a vendor file with what an "
    "appliance serves."
)

VROPS_API_FILE_GET_OP = VropsTypedOp(
    op_id=OP_ID,
    handler_attr="api_file_get",
    summary=(
        "Read one API description page or file that a VCF Operations or Aria Operations "
        "appliance serves."
    ),
    description=(
        "Sends one GET, with the target's login, to the Swagger page under /suite-api/doc/ "
        "or a usual Swagger or OpenAPI file path, and returns its status, content type, "
        "size, SHA-256, the appliance version and build, and the text in parts. Does not "
        "follow redirects. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=API_FILE_GROUP_KEY,
    tags=("read-only", "vrops", "vmware", "api-description", "swagger", "openapi"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to fetch the API description that an appliance serves, for example to "
            "pin it or to check vROps calls against it. The documented page is "
            "'/suite-api/doc/swagger-ui.html'; read it first, then the file the page "
            "loads (its name is in the page text)."
        ),
        "parameter_hints": {
            "path": (
                "'/suite-api/', a path under '/suite-api/doc/', or a usual Swagger or "
                "OpenAPI file path. Any other path, '..', '//', a percent sign, a scheme "
                "or a host is refused before a request is sent."
            ),
        },
        "output_shape": (
            "{path, status, content_type, redirect_path, size, sha256, secrets_replaced, "
            "parts, text_parts: [{index, text}], version, build, version_error}. A large "
            "answer comes back as a result handle whose rows are the text parts: page them "
            "with result_query (offset and a small limit such as 2; one answer is at most "
            "256 KiB). Join the text of all parts in index order to get the checked copy; "
            "its SHA-256 equals sha256. The copy is the file with every value under a "
            "secret name replaced by [REDACTED:param_name]; secrets_replaced counts them."
        ),
        "next_step": (
            "On a 3xx status, read redirect_path next if it is a path on the target. If "
            "the page names another file under /suite-api/doc/, read that path next with "
            "this same operation. On 403 or 404 the path is not served for this login; "
            "report the status."
        ),
    },
)
