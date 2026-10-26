# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``hcx.api_file.get`` -- read one API documentation file the HCX Manager serves.

Broadcom publishes no file of the main HCX API (``/hybridity/api/...``).
Only a running HCX Manager serves its API documentation, at
``https://{server}/hybridity/docs`` and the files that page loads. This
read fetches one such page or file and returns its path, HTTP status,
content type, size, SHA-256 and the Manager version, with the text in
parts behind a result handle. The text is the checked copy of the file:
MEHO parses the file (JSON, YAML, XML or HTML) and replaces every value
under a secret name with a placeholder before it is cut into parts
(:mod:`meho_backplane.connectors._shared.api_file_secrets`). Joining the
parts in order gives that copy, so the vendor API files can be pinned
and compared.

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
that does not parse, or whose format is unknown (JavaScript, CSS, plain
text), is refused with the reason. The checked copy is what ``size`` and
``sha256`` describe, and ``secrets_replaced`` says how many values were
replaced. A copy that the general secret-redaction rules would still
change stops too; the rules run on the whole text and on the answer, so
a secret that spans two parts is found as well.

The fetch, the file check and the flight recorder call are the shared
steps in :mod:`meho_backplane.connectors._shared.api_file_read`
(:func:`~meho_backplane.connectors._shared.api_file_read.fetch_checked_api_file`).
The flight recorder gets the checked copy of the file, never the body as
it came, and no body when the file is refused. A ``401`` is raised with
its body checked the same way (or with no body), so the error result
never carries a secret and the dispatcher still logs in again and
retries once.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.api_file_read import (
    assert_fits_result_handle,
    assert_not_redacted,
    fetch_checked_api_file,
    redirect_target,
    text_items,
    text_redactor,
)
from meho_backplane.connectors._shared.api_file_secrets import scrub_query_string
from meho_backplane.connectors.hcx import HCX_CONNECTOR_ID
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = [
    "HcxApiFilePathError",
    "check_path",
    "hcx_api_file_get_impl",
]

#: The documentation page a running HCX Manager serves (``GET``). Every
#: path under ``/hybridity/docs/`` is the page's own folder.
_DOCS_PATH = "/hybridity/docs"

#: Path parts that name a Swagger or OpenAPI description, not data.
_API_FILE_SEGMENTS = frozenset({"api-docs", "swagger-resources", "swagger-config"})
_API_FILE_SUFFIXES = ("swagger.json", "swagger.yaml", "openapi.json", "openapi.yaml")

#: The only query keys a documentation path may carry.
_ALLOWED_QUERY_KEYS = frozenset({"group", "apiVersion"})

#: Characters and encoded forms that have no place in a documentation or
#: API file path. Checked on the raw string, before any decoding.
_REFUSED_FRAGMENTS = ("..", "//", "\\", "://", "%2e", "%2f", "%5c", "#", ";", "%3b")


class HcxApiFilePathError(ValueError):
    """The path is outside the rule; no HTTP request was sent."""


#: The operation id the dispatcher labels this read with. The redaction
#: check and the stored-record estimate use the same label.
_OP_ID = "hcx.api_file.get"


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


async def hcx_api_file_get_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.api_file.get`` -- ``GET {path}`` on the HCX Manager, text in parts.

    A ``401`` is raised as :exc:`httpx.HTTPStatusError`, with a checked
    body or none, so the dispatcher drops the session and dispatches once
    more. Every other status is reported in ``status``.
    """
    path, query = check_path(params.get("path"))
    url = path if not query else f"{path}?{query}"
    max_bytes = get_settings().result_handle_max_record_bytes
    client = await connector._http_client(target)
    headers = await connector.auth_headers(target, operator)
    response, checked = await fetch_checked_api_file(
        client,
        url,
        path=path,
        headers=headers,
        extensions=connector._request_extensions(target),
        max_bytes=max_bytes,
        text_rule=None,
        error_text_rule=text_redactor(operator, connector_id=HCX_CONNECTOR_ID, op_id=_OP_ID),
    )
    body = checked.text.encode("utf-8")
    items = text_items(body)
    redirect_to, redirect_replaced = _redirect(response)
    version = await connector.manager_version(target, operator)
    answer: dict[str, Any] = {
        "path": url,
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "secrets_replaced": checked.secrets_replaced + redirect_replaced,
        "version": version.version,
        "build": version.build,
        "version_error": version.error,
        "redirect_to": redirect_to,
        "parts": len(items),
    }
    assert_fits_result_handle(
        answer, items, operator_sub=operator.sub, op_id=_OP_ID, max_bytes=max_bytes
    )
    answer["items"] = items
    assert_not_redacted(answer, checked.text, operator, connector_id=HCX_CONNECTOR_ID, op_id=_OP_ID)
    return answer


def _redirect(response: httpx.Response) -> tuple[str | None, int]:
    """The redirect path with any secret-named query value replaced, and the count."""
    target = redirect_target(response.headers.get("location"), response.request.url)
    if target is None:
        return None, 0
    return scrub_query_string(target)
