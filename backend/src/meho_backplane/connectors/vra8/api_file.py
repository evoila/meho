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
  names the redirect's path. A ``401`` is raised so the dispatcher
  renews the login and retries once; every other status comes back in
  ``status``.
* The file comes back as text parts of at most 64 KiB each. Joining the
  parts in order gives the exact file; ``sha256`` is the hash of the
  file bytes. A file that does not fit in one stored result is refused
  with its size, never cut. Only UTF-8 text comes back.
* MEHO redacts secret-shaped text in every stored result. The read runs
  the same redaction rules the dispatcher will apply, on the whole file
  text and on each part, and refuses a file whose text they would
  change, naming the rule. So the stored parts always match ``sha256``.

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
import msgspec
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.credential_backend import CredentialsReadError
from meho_backplane.connectors.vra8._paths import _ABOUT_PATH, _VCO_ABOUT_PATH
from meho_backplane.connectors.vra8.session import Vra8TargetLike
from meho_backplane.redaction import apply_connector_boundary_redaction
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

#: Room kept free in one stored result for the record's own fields.
_RECORD_OVERHEAD_BYTES = 4096

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
        raise Vra8ApiFileError(
            f"The file is {int(declared)} bytes (Content-Length). One stored result "
            f"holds at most {max_bytes} bytes, so the file was not read."
        )
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise Vra8ApiFileError(
                f"The file is more than {max_bytes} bytes. One stored result holds at "
                "most that, so the file was not read."
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_file(
    connector: Vra8Connector,
    operator: Operator,
    target: Vra8TargetLike,
    path: str,
    max_bytes: int,
) -> dict[str, Any]:
    """Send the one ``GET`` with the bearer and return status, headers and body.

    The pooled client class follows a same-origin redirect by itself, so
    the request goes through :meth:`httpx.AsyncClient.send` with
    ``follow_redirects=False`` to return a redirect unfollowed. A ``401``
    is raised as :class:`httpx.HTTPStatusError`: the dispatcher then drops
    the cached bearer, logs in again and retries once, like every other
    read of this connector.
    """
    client = await connector._http_client(target)
    headers = await connector.auth_headers(target, operator)
    request = client.build_request(
        "GET", path, headers=headers, extensions=connector._request_extensions(target)
    )
    response = await httpx.AsyncClient.send(client, request, stream=True, follow_redirects=False)
    try:
        if response.status_code == 401:
            response.raise_for_status()
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


def _file_fields(path: str, content_type: str | None, body: bytes) -> dict[str, Any]:
    """Decode a ``2xx`` body and build the file fields of the answer."""
    if not _is_text(content_type):
        raise Vra8ApiFileError(
            f"{path!r} is {content_type!r}, not text. This read returns only UTF-8 text."
        )
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Vra8ApiFileError(
            f"{path!r} ({content_type or 'no content type'}) is not UTF-8 text: {exc.reason}."
        ) from exc
    parts = split_text_parts(body)
    return {
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "parts": len(parts),
        "text_parts": [{"index": index, "text": text} for index, text in enumerate(parts)],
    }


def _check_fits(answer: dict[str, Any], max_record_bytes: int) -> None:
    """Refuse an answer too big to keep whole in one stored result."""
    encoded = len(msgspec.json.encode(answer))
    if encoded + _RECORD_OVERHEAD_BYTES > max_record_bytes:
        raise Vra8ApiFileError(
            f"{answer['path']!r} is {answer['size']} bytes ({encoded} bytes as a stored "
            f"result). One stored result holds at most {max_record_bytes} bytes, so the "
            "file was not returned. This read never returns a cut file."
        )


def _check_redaction_keeps_text(
    answer: dict[str, Any], connector: Vra8Connector, operator: Operator
) -> None:
    """Refuse a file whose text the stored-result redaction would change.

    The dispatcher redacts every result with the policy for this
    (connector, tenant, operation) before it stores the result. A rule
    that fires on the file text would rewrite a part, and the joined parts
    would no longer match ``sha256``. This runs the same middleware with
    the same labels on the text parts and on the whole file text: a
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
    raise Vra8ApiFileError(
        f"{answer['path']!r} holds text that MEHO's secret redaction would change: {where}. "
        "The stored parts would no longer match sha256, so the file was not returned. "
        "This read never returns a changed file."
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
    4. Refuse a file that does not fit in one stored result, or whose
       text the stored-result redaction would change.

    A non-``2xx`` status comes back in ``status`` with no text. A ``3xx``
    also gives ``redirect_path``.
    """
    path = check_api_file_path(params.get("path"))
    max_record_bytes = get_settings().result_handle_max_record_bytes

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
        _check_fits(answer, max_record_bytes)
        _check_redaction_keeps_text(answer, connector, operator)

    _log.info(
        "vra8_api_file_read",
        target=target.name,
        path=path,
        status=answer["status"],
        size=answer["size"],
        parts=answer["parts"],
        has_build=answer["build"] is not None,
    )
    return answer
