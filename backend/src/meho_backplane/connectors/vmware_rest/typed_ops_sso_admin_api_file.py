# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vcenter.sso_admin.api_file.get`` typed op.

Reads one API description file (a WSDL or an XSD file) that the vCenter
SSO Admin service serves, so the file can be pinned and the SSO Admin
calls checked against it. No vendor download ships the SSO Admin WSDL,
so a running vCenter is the only source.

The read is narrow on purpose:

* It accepts only a path under the SSO Admin service folder
  ``/sso-adminserver/``, with no ``..``, ``//``, backslash, percent
  sign, scheme or host, and an optional query of ``wsdl``, ``xsd`` or
  ``xsd=<number>``. Every SSO Admin call is a SOAP ``POST``, so a
  ``GET`` there changes nothing. Any other path is refused before a
  request goes out.
* It sends one ``GET`` on a new client with an empty cookie jar and no
  login header: a WSDL holds no data, and the SSO Admin service has its
  own login (a SAML token) that MEHO does not have.
* It does not follow a redirect. The answer names the redirect's path.
* The file is checked before it is cut into parts: MEHO parses it (XML
  for a WSDL or XSD file; JSON, YAML or HTML as well) and replaces every
  value under a secret name with a placeholder
  (:mod:`meho_backplane.connectors._shared.api_file_secrets`). A file
  that does not parse, or whose format is unknown, is refused with the
  reason. The checked copy comes back as text parts of at most 64 KiB
  each. Joining the parts in order gives the copy; ``sha256`` is the
  hash of the copy and ``secrets_replaced`` counts the replaced values.
  A file that does not fit in one stored result is refused with its
  size, never cut.
* MEHO redacts secret-shaped text in every stored result. The read runs
  the same redaction rules the dispatcher will apply, on the whole copy
  and on each part, and refuses a copy whose text they would still
  change, naming the rule. So the stored parts always match ``sha256``,
  no secret that a part cut would hide gets through, and redaction
  stays active.

The vCenter version and build come from ``GET /api/appliance/system/version``
on the connector's normal login. When that read fails (HTTP error, login
error, or the credential store cannot serve the login), the file still
comes back and ``version_error`` says why.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

import defusedxml.ElementTree as DefusedET
import httpx
import msgspec
import structlog
from defusedxml import DefusedXmlException

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.api_file_secrets import (
    ApiFileFormatError,
    check_api_file,
    scrub_query_string,
)
from meho_backplane.connectors._shared.credential_backend import CredentialsReadError
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.redaction import apply_connector_boundary_redaction
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "OP_ID",
    "PART_MAX_BYTES",
    "SSO_ADMIN_API_FILE_GROUP_KEY",
    "SSO_ADMIN_API_FILE_WHEN_TO_USE",
    "TYPED_OPS",
    "VCENTER_SSO_ADMIN_API_FILE_GET_OP",
    "WHEN_TO_USE_BY_GROUP",
    "SsoAdminApiFileError",
    "check_api_file_path",
    "find_imports",
    "split_text_parts",
    "sso_admin_api_file_get_impl",
]

_log = structlog.get_logger(__name__)

OP_ID = "vcenter.sso_admin.api_file.get"

#: The SSO Admin service folder. The documented service address is
#: ``/sso-adminserver/sdk/{domain}``.
SSO_ADMIN_FOLDER = "/sso-adminserver/"

#: The vCenter version read MEHO's version detection already makes.
_APPLIANCE_VERSION_PATH = "/api/appliance/system/version"

#: Largest text part, in UTF-8 bytes. One ``result_query`` answer holds at
#: most 256 KiB, so a 64 KiB part fits even after JSON escaping.
PART_MAX_BYTES = 64 * 1024

#: Room kept free in one stored result for the record's own fields.
_RECORD_OVERHEAD_BYTES = 4096

#: The path rule, as one pattern. It is also the ``pattern`` of the
#: ``path`` parameter, so the dispatcher refuses a bad path before the
#: handler runs. The allowed characters leave out ``\``, ``%``, ``:``,
#: ``@``, ``#`` and spaces, so no encoded dot or slash, scheme or host
#: can get through. The two look-aheads refuse ``..`` and ``//``.
PATH_PATTERN = (
    r"^(?!.*\.\.)(?!.*//)/sso-adminserver/[A-Za-z0-9._~/-]*(\?(wsdl|xsd(=[0-9]{1,4})?))?$"
)
_PATH_RE = re.compile(PATH_PATTERN)

_WSDL_NS = "http://schemas.xmlsoap.org/wsdl/"
_XSD_NS = "http://www.w3.org/2001/XMLSchema"
#: Element tag -> the attribute that names the file it imports.
_IMPORT_ATTRS: dict[str, str] = {
    f"{{{_WSDL_NS}}}import": "location",
    f"{{{_XSD_NS}}}import": "schemaLocation",
    f"{{{_XSD_NS}}}include": "schemaLocation",
}

_TEXT_MEDIA_TYPES = frozenset({"application/xml", "application/json"})

SSO_ADMIN_API_FILE_GROUP_KEY = "vcenter-sso-admin-api-files"


class SsoAdminApiFileError(ValueError):
    """The read refused a path or a file. The message says why."""


def check_api_file_path(path: Any) -> str:
    """Return *path* when the read accepts it; raise :exc:`SsoAdminApiFileError` if not."""
    if not isinstance(path, str) or not _PATH_RE.fullmatch(path):
        raise SsoAdminApiFileError(
            f"vcenter.sso_admin.api_file.get refused the path {path!r}. The path must "
            f"start with {SSO_ADMIN_FOLDER!r}, use only letters, digits and . _ ~ / -, "
            "have no '..' or '//', and have no query or one of '?wsdl', '?xsd' or "
            "'?xsd=<number>'. No request was sent."
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


def find_imports(data: bytes) -> list[str]:
    """Return the files a WSDL or XSD file names, in order, as written.

    Reads ``location`` on ``wsdl:import`` and ``schemaLocation`` on
    ``xsd:import`` / ``xsd:include``. A file that is not XML (or that
    defusedxml refuses, for example one that declares entities) gives an
    empty list.
    """
    try:
        root = DefusedET.fromstring(data)
    except (DefusedET.ParseError, DefusedXmlException):
        return []
    found: list[str] = []
    for element in root.iter():
        attr = _IMPORT_ATTRS.get(element.tag) if isinstance(element.tag, str) else None
        if attr is None:
            continue
        value = element.get(attr)
        if value and value not in found:
            found.append(value)
    return found


def _is_text(content_type: str | None) -> bool:
    """True for a text or XML/JSON content type, or when the server sent none."""
    if not content_type:
        return True
    media = content_type.split(";", 1)[0].strip().lower()
    return media.startswith("text/") or media in _TEXT_MEDIA_TYPES or media.endswith("+xml")


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
        raise SsoAdminApiFileError(
            f"The file is {int(declared)} bytes (Content-Length). One stored result "
            f"holds at most {max_bytes} bytes, so the file was not read."
        )
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise SsoAdminApiFileError(
                f"The file is more than {max_bytes} bytes. One stored result holds at "
                "most that, so the file was not read."
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_file(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    path: str,
    max_bytes: int,
) -> dict[str, Any]:
    """Send the one unauthenticated ``GET`` and return status, headers and body.

    A new client with its own empty cookie jar, so no session cookie of
    the pooled client goes out, and no login header is added. The
    pooled client class follows same-origin redirects by itself, so the
    request goes through :meth:`httpx.AsyncClient.send` with
    ``follow_redirects=False`` to return a redirect unfollowed.
    """
    client = await connector._ephemeral_http_client(target)
    try:
        request = client.build_request(
            "GET", path, extensions=connector._request_extensions(target)
        )
        response = await httpx.AsyncClient.send(
            client, request, stream=True, follow_redirects=False
        )
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
    finally:
        await client.aclose()
    return {"status": status, "content_type": content_type, "redirect": redirect, "body": body}


async def _read_version(
    connector: VmwareRestConnector, operator: Operator, target: VsphereTargetLike
) -> dict[str, Any]:
    """Read ``version`` and ``build``; on failure return ``None`` and the reason.

    The read logs in with the connector's normal credentials, so it can
    also fail before any request goes out: no credentials for the target,
    access denied, or a credential store that cannot be reached. Those are
    expected failures of this best-effort read, not of the file read.
    """
    try:
        payload = await connector._get_json(target, _APPLIANCE_VERSION_PATH, operator=operator)
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
            "version_error": f"GET {_APPLIANCE_VERSION_PATH} failed: {type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        payload = {}
    version = payload.get("version")
    build = payload.get("build")
    version = version if isinstance(version, str) and version else None
    build = build if isinstance(build, str) and build else None
    error = None if build else f"GET {_APPLIANCE_VERSION_PATH} returned no build"
    return {"version": version, "build": build, "version_error": error}


def _file_fields(path: str, content_type: str | None, body: bytes) -> dict[str, Any]:
    """Decode a ``2xx`` body, check it, and build the file fields of the answer."""
    if not _is_text(content_type):
        raise SsoAdminApiFileError(
            f"{path!r} is {content_type!r}, not text. This read returns only UTF-8 text."
        )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SsoAdminApiFileError(
            f"{path!r} ({content_type or 'no content type'}) is not UTF-8 text: {exc.reason}."
        ) from exc
    try:
        checked = check_api_file(text, content_type=content_type, path=path)
    except ApiFileFormatError as exc:
        raise SsoAdminApiFileError(str(exc)) from exc
    copy = checked.text.encode("utf-8")
    parts = split_text_parts(copy)
    return {
        "size": len(copy),
        "sha256": hashlib.sha256(copy).hexdigest(),
        "secrets_replaced": checked.secrets_replaced,
        "imports": find_imports(copy),
        "parts": len(parts),
        "text_parts": [{"index": index, "text": text} for index, text in enumerate(parts)],
    }


def _check_fits(answer: dict[str, Any], max_record_bytes: int) -> None:
    """Refuse an answer too big to keep whole in one stored result."""
    encoded = len(msgspec.json.encode(answer))
    if encoded + _RECORD_OVERHEAD_BYTES > max_record_bytes:
        raise SsoAdminApiFileError(
            f"{answer['path']!r} is {answer['size']} bytes ({encoded} bytes as a stored "
            f"result). One stored result holds at most {max_record_bytes} bytes, so the "
            "file was not returned. This read never returns a cut file."
        )


def _check_redaction_keeps_text(
    answer: dict[str, Any], connector: VmwareRestConnector, operator: Operator
) -> None:
    """Refuse a checked copy whose text the stored-result redaction would still change.

    The dispatcher redacts every result with the policy for this
    (connector, tenant, operation) before it stores the result. A rule
    that fires on the text would rewrite a part, and the joined parts
    would no longer match ``sha256``. This runs the same middleware with
    the same labels on the text parts, so the check and the real
    redaction cannot disagree, and on the whole text as well: a secret
    that a part cut splits in two is invisible to the per-part check, and
    the dispatcher would store it in clear text. The rules skip the
    placeholder the file check writes, so a replaced value does not set
    them off.
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
    raise SsoAdminApiFileError(
        f"{answer['path']!r} holds text that MEHO's secret redaction would change: {where}. "
        "The stored parts would no longer match sha256, so the file was not returned."
    )


async def sso_admin_api_file_get_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vcenter.sso_admin.api_file.get``.

    1. Check the path (no request goes out for a refused path).
    2. ``GET`` the file without login, without following a redirect.
    3. Parse the file and replace every value under a secret name; refuse
       a file that does not parse.
    4. Read the vCenter version and build (best effort).
    5. Refuse a file that does not fit in one stored result, or whose
       text the stored-result redaction would still change.

    A non-``2xx`` status comes back in ``status`` with no text. A ``3xx``
    also gives ``redirect_path``.
    """
    path = check_api_file_path(params.get("path"))
    max_record_bytes = get_settings().result_handle_max_record_bytes

    fetched = await _fetch_file(connector, target, path, max_record_bytes)
    redirect, redirect_replaced = (
        scrub_query_string(fetched["redirect"]) if fetched["redirect"] else (None, 0)
    )
    answer: dict[str, Any] = {
        "path": path,
        "status": fetched["status"],
        "content_type": fetched["content_type"],
        "redirect_path": redirect,
        "size": None,
        "sha256": None,
        "secrets_replaced": redirect_replaced,
        "imports": [],
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
        "vcenter_sso_admin_api_file_read",
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
            "minLength": len(SSO_ADMIN_FOLDER),
            "maxLength": 512,
            "pattern": PATH_PATTERN,
            "description": (
                "Path of one file under the SSO Admin service folder '/sso-adminserver/' "
                "on the target vCenter. Allowed: letters, digits and . _ ~ / -, no '..' "
                "or '//', and an optional query '?wsdl', '?xsd' or '?xsd=<number>'. "
                "Replace placeholders such as DOMAIN with real values. A name from "
                "'imports' is relative to the folder of the file that names it."
            ),
        },
    },
    "required": ["path"],
    "additionalProperties": False,
    "examples": [{"path": "/sso-adminserver/sdk/DOMAIN?wsdl"}],
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
            "description": "Size of the checked copy in bytes; null when the status is not 2xx.",
        },
        "sha256": {
            "type": ["string", "null"],
            "description": "SHA-256 (hex) of the checked copy; null when the status is not 2xx.",
        },
        "secrets_replaced": {
            "type": "integer",
            "description": (
                "How many values under a secret name (password, token, licence key, ...) "
                "were replaced by [REDACTED:param_name] in the copy."
            ),
        },
        "imports": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "location / schemaLocation values the file names (wsdl:import, "
                "xsd:import, xsd:include), as written. Empty for a file that is not XML."
            ),
        },
        "parts": {"type": "integer", "description": "How many text parts the file has."},
        "text_parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Position of the part."},
                    "text": {"type": "string", "description": "At most 64 KiB of the file."},
                },
                "required": ["index", "text"],
            },
            "description": (
                "The checked copy of the file, in order. Joining all parts gives the copy."
            ),
        },
        "version": {
            "type": ["string", "null"],
            "description": "vCenter version from GET /api/appliance/system/version.",
        },
        "build": {
            "type": ["string", "null"],
            "description": "vCenter build from GET /api/appliance/system/version.",
        },
        "version_error": {
            "type": ["string", "null"],
            "description": "Why version or build is empty; null when the read worked.",
        },
    },
    "required": ["path", "status", "secrets_replaced", "imports", "parts", "text_parts"],
}

#: Group text: says what the group holds, makes no claim for other ops.
SSO_ADMIN_API_FILE_WHEN_TO_USE = (
    "This group holds one read: vcenter.sso_admin.api_file.get. It reads one SSO "
    "Admin API description file (the WSDL or one of its XSD files) that a vCenter "
    "serves under /sso-adminserver/, with its size, SHA-256, the files it imports, "
    "and the vCenter version and build. Use it to get the SSO Admin WSDL, which no "
    "vendor download contains."
)

VCENTER_SSO_ADMIN_API_FILE_GET_OP = VmwareTypedOp(
    op_id=OP_ID,
    handler_attr="sso_admin_api_file_get",
    summary="Read one SSO Admin API description file (WSDL or XSD) that a vCenter serves.",
    description=(
        "Reads one file under /sso-adminserver/ with one GET, without login, and "
        "returns its status, content type, size, SHA-256, imported files, the vCenter "
        "version and build, and the text in parts. Does not follow redirects. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=SSO_ADMIN_API_FILE_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "sso", "api-description", "wsdl"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to fetch the SSO Admin WSDL or one of its XSD files from a vCenter, "
            "for example to pin it or to check SSO Admin calls against it. The file "
            "paths are not documented; the documented service address is "
            "'/sso-adminserver/sdk/<SSO domain>', so try that address with '?wsdl' first."
        ),
        "parameter_hints": {
            "path": (
                "A path under '/sso-adminserver/'. Any other path, '..', '//', a "
                "percent sign, a scheme or a host is refused before a request is sent."
            ),
        },
        "output_shape": (
            "{path, status, content_type, redirect_path, size, sha256, secrets_replaced, "
            "imports: [str], parts, text_parts: [{index, text}], version, build, "
            "version_error}. text_parts hold the checked copy of the file: the parsed "
            "file written back with every value under a secret name replaced by "
            "[REDACTED:param_name]; secrets_replaced counts them. A large answer comes "
            "back as a result handle: read '/imports' and '/text_parts' with result_query "
            "(query {'path': '/text_parts'}, a small limit such as 2). Join the text of "
            "all parts in index order to get the copy; its SHA-256 equals sha256."
        ),
        "next_step": (
            "Read each name in 'imports' next with this same operation (a relative name "
            "is relative to the folder of the current file). On a 3xx status, read "
            "redirect_path next. On 401, 403 or 404 the file is not served at that path "
            "without login; report the status."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VCENTER_SSO_ADMIN_API_FILE_GET_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    SSO_ADMIN_API_FILE_GROUP_KEY: SSO_ADMIN_API_FILE_WHEN_TO_USE
}
