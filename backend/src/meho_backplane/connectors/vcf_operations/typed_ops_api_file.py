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
  login (the connector's token session), on the pooled client.
* It does not follow a redirect. The answer names the redirect's path.
* The file comes back as text parts of at most 64 KiB each, measured as
  JSON text, so a part always fits one ``result_query`` answer. Joining
  the parts in order gives the exact file; ``sha256`` is the hash of the
  file bytes. A file that does not fit in one stored result is refused
  with its size, never cut.
* The read never returns a secret. It runs the redaction rules the
  dispatcher will apply, on the whole file text and on each part, and
  refuses a file whose text they would change, naming the rule. It also
  refuses a file with a password, token, licence key or Authorization
  field with a value, or a PEM private key, shapes the redaction does not
  see (:mod:`.typed_ops_api_file_secrets`). So the stored parts always
  match ``sha256`` and no secret a part cut would hide gets through.

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
from meho_backplane.connectors._shared.credential_backend import CredentialsReadError
from meho_backplane.connectors.vcf_operations.connector import _VERSIONS_CURRENT_PATH
from meho_backplane.connectors.vcf_operations.session import VcfOperationsTargetLike
from meho_backplane.connectors.vcf_operations.typed_ops import VropsTypedOp
from meho_backplane.connectors.vcf_operations.typed_ops_api_file_secrets import find_secret_shape
from meho_backplane.redaction import apply_connector_boundary_redaction
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


def _redirect_path(request_url: httpx.URL, location: str | None) -> str | None:
    """Return the path (and query) a redirect points to, or ``None``."""
    if not location:
        return None
    try:
        target = request_url.join(location)
    except httpx.InvalidURL:
        return None
    return target.raw_path.decode("ascii", "replace")


async def _read_body(response: httpx.Response, max_bytes: int) -> bytes:
    """Read the body, but refuse one over *max_bytes* without keeping it."""
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise VropsApiFileError(
            f"The file is {int(declared)} bytes (Content-Length). One stored result "
            f"holds at most {max_bytes} bytes, so the file was not read."
        )
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise VropsApiFileError(
                f"The file is more than {max_bytes} bytes. One stored result holds at "
                "most that, so the file was not read."
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_file(
    connector: VcfOperationsConnector,
    operator: Operator,
    target: VcfOperationsTargetLike,
    path: str,
    max_bytes: int,
) -> dict[str, Any]:
    """Send the one ``GET`` with the target's login and return status, headers and body.

    The request carries the connector's token header (``OpsToken`` on 9.x,
    ``vRealizeOpsToken`` on 8.x) on the pooled client. The pooled client
    class follows same-origin redirects by itself, so the request goes
    through :meth:`httpx.AsyncClient.send` with ``follow_redirects=False``
    to return a redirect unfollowed.
    """
    client = await connector._http_client(target)
    headers = await connector.auth_headers(target, operator)
    request = client.build_request(
        "GET", path, headers=headers, extensions=connector._request_extensions(target)
    )
    response = await httpx.AsyncClient.send(client, request, stream=True, follow_redirects=False)
    try:
        status = response.status_code
        content_type = response.headers.get("content-type")
        redirect = (
            _redirect_path(request.url, response.headers.get("location"))
            if response.is_redirect
            else None
        )
        body = await _read_body(response, max_bytes) if response.is_success else b""
    finally:
        await response.aclose()
    return {"status": status, "content_type": content_type, "redirect": redirect, "body": body}


async def _read_version(
    connector: VcfOperationsConnector, operator: Operator, target: VcfOperationsTargetLike
) -> dict[str, Any]:
    """Read ``version`` and ``build``; on failure return ``None`` and the reason.

    The read uses the connector's normal login, so it can also fail
    before any request goes out: no credentials for the target, access
    denied, or a credential store that cannot be reached. Those are
    expected failures of this best-effort read, not of the file read.
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


def _file_fields(path: str, content_type: str | None, body: bytes) -> dict[str, Any]:
    """Decode a ``2xx`` body and build the file fields of the answer."""
    if not _is_text(content_type):
        raise VropsApiFileError(
            f"{path!r} is {content_type!r}, not text. This read returns only UTF-8 text."
        )
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VropsApiFileError(
            f"{path!r} ({content_type or 'no content type'}) is not UTF-8 text: {exc.reason}."
        ) from exc
    parts = split_text_parts(body)
    return {
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "parts": len(parts),
        "text_parts": [{"index": index, "text": text} for index, text in enumerate(parts)],
    }


def _check_fits(answer: dict[str, Any], max_record_bytes: int, max_rows: int) -> None:
    """Refuse an answer too big to keep whole in one stored result.

    The stored result keeps the text parts as rows, so both caps apply:
    the row cap and the record byte cap. The reducer would cut the tail
    silently; this read never returns a cut file.
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


def _check_no_secret_shape(answer: dict[str, Any]) -> None:
    """Refuse a file that holds a secret the stored-result redaction would not see.

    The whole file text is checked, so a secret that a part cut splits in
    two is found as well. The error names the shape and field, never the value.
    """
    found = find_secret_shape("".join(part["text"] for part in answer["text_parts"]))
    if found is not None:
        raise VropsApiFileError(
            f"{answer['path']!r} holds {found}. This read never returns a secret, "
            "so the file was not returned."
        )


def _check_redaction_keeps_text(
    answer: dict[str, Any], connector: VcfOperationsConnector, operator: Operator
) -> None:
    """Refuse a file whose text the stored-result redaction would change.

    The dispatcher redacts every result with the policy for this
    (connector, tenant, operation) before it stores the result. A rule
    that fires on the file text would rewrite a part, and the joined parts
    would no longer match ``sha256``. This runs the same middleware with
    the same labels on the text parts, so the check and the real
    redaction cannot disagree, and on the whole file text as well: a
    secret that a part cut splits in two is invisible to the per-part
    check, and the dispatcher would store it in clear text.
    """
    probe = {
        "text_parts": answer["text_parts"],
        "file_text": "".join(part["text"] for part in answer["text_parts"]),
    }
    result = apply_connector_boundary_redaction(
        probe,
        connector_id=f"{connector.impl_id}-{connector.version}",
        tenant=str(operator.tenant_id) if operator.tenant_id is not None else None,
        op=OP_ID,
    )
    if result.redacted == result.raw:
        return
    hit = result.manifest[0] if result.manifest else None
    where = (
        f"rule {hit.rule!r} ({hit.reason}) fires at {hit.path}"
        if hit is not None
        else f"policy {result.policy_id!r} changes the text"
    )
    raise VropsApiFileError(
        f"{answer['path']!r} holds text that MEHO's secret redaction would change: {where}. "
        "The stored parts would no longer match sha256, so the file was not returned. "
        "This read never returns a changed file."
    )


async def api_file_get_impl(
    connector: VcfOperationsConnector,
    operator: Operator,
    target: VcfOperationsTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vrops.api_file.get``.

    1. Check the path (no request goes out for a refused path).
    2. ``GET`` the file with the target's login, without following a redirect.
    3. Read the appliance version and build (best effort).
    4. Refuse a file that does not fit in one stored result, whose text the
       stored-result redaction would change, or that holds a secret shape.

    A non-``2xx`` status comes back in ``status`` with no text. A ``3xx``
    also gives ``redirect_path``.
    """
    path = check_api_file_path(params.get("path"))
    settings = get_settings()
    max_record_bytes = settings.result_handle_max_record_bytes

    fetched = await _fetch_file(connector, operator, target, path, max_record_bytes)
    answer: dict[str, Any] = {
        "path": path,
        "status": fetched["status"],
        "content_type": fetched["content_type"],
        "redirect_path": fetched["redirect"],
        "size": None,
        "sha256": None,
        "parts": 0,
        "text_parts": [],
    }
    if 200 <= fetched["status"] < 300:
        answer.update(_file_fields(path, fetched["content_type"], fetched["body"]))
    answer.update(await _read_version(connector, operator, target))
    if answer["parts"]:
        _check_fits(answer, max_record_bytes, settings.result_handle_max_spill_rows)
        _check_redaction_keeps_text(answer, connector, operator)
        _check_no_secret_shape(answer)

    _log.info(
        "vrops_api_file_read",
        target=target.name,
        path=path,
        status=answer["status"],
        size=answer["size"],
        parts=answer["parts"],
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
        "content_type": {
            "type": ["string", "null"],
            "description": "Content-Type header of the answer.",
        },
        "redirect_path": {
            "type": ["string", "null"],
            "description": "For a 3xx answer: the path the redirect points to (not followed).",
        },
        "size": {
            "type": ["integer", "null"],
            "description": "File size in bytes; null when the status is not 2xx.",
        },
        "sha256": {
            "type": ["string", "null"],
            "description": "SHA-256 (hex) of the file bytes; null when the status is not 2xx.",
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
            "description": "The file text, in order. Joining all parts gives the exact file.",
        },
        "version": {
            "type": ["string", "null"],
            "description": "Appliance releaseName from GET /suite-api/api/versions/current.",
        },
        "build": {
            "type": ["string", "null"],
            "description": "Appliance buildNumber from GET /suite-api/api/versions/current.",
        },
        "version_error": {
            "type": ["string", "null"],
            "description": "Why version or build is empty; null when the read worked.",
        },
    },
    "required": ["path", "status", "parts", "text_parts"],
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
            "{path, status, content_type, redirect_path, size, sha256, parts, "
            "text_parts: [{index, text}], version, build, version_error}. A large answer "
            "comes back as a result handle whose rows are the text parts: page them with "
            "result_query (offset and a small limit such as 2; one answer is at most 256 "
            "KiB). Join the text of all parts in index order to get the exact file; its "
            "SHA-256 equals sha256."
        ),
        "next_step": (
            "On a 3xx status, read redirect_path next. If the page names another file "
            "under /suite-api/doc/, read that path next with this same operation. On 401, "
            "403 or 404 the path is not served for this login; report the status."
        ),
    },
)
