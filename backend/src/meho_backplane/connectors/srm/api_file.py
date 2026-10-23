# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``srm.api_file.get`` -- read one API description file the SRM appliance serves.

Broadcom does not publish the SRM 8.8 and Live Site Recovery 9.0 API
files as downloads; a running appliance serves them. This read fetches
one of two files and returns its path, HTTP status, content type, size,
SHA-256 and the SRM version and build, with the text in parts behind a
result handle. Joining the parts in order gives the exact file, so the
vendor API files can be pinned and compared.

The two files (input ``file``):

* ``srm`` -- ``GET /api/rest/srm/v2/open-api.yaml``, the Site Recovery
  API;
* ``configure`` -- ``GET /api/rest/configure/v2/open-api.yaml``, the
  appliance setup API.

No other path can be read: the input is a name, not a path. The read
sends one ``GET`` to the target's own address with the target's own
login and does not follow a redirect (``redirect_to`` carries the path).
Only UTF-8 text comes back. The body is decoded and measured in bounded
steps; a file too big for one result handle stops with its size; a file
that MEHO's secret-redaction rules would change stops too, so the read
never claims an exact file it cannot return. These steps live in
:mod:`meho_backplane.connectors._shared.api_file_read`.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.api_file_read import (
    assert_fits_result_handle,
    assert_not_redacted,
    decode_text,
    fetch_without_redirects,
    redirect_target,
    text_items,
)
from meho_backplane.connectors.srm.session import SrmTargetLike
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.srm.connector import SrmConnector

__all__ = ["API_FILES", "SrmApiFileNameError", "srm_api_file_get_impl"]

#: The files the read can fetch, by input name.
API_FILES: dict[str, str] = {
    "srm": "/api/rest/srm/v2/open-api.yaml",
    "configure": "/api/rest/configure/v2/open-api.yaml",
}

#: The operation id the dispatcher labels this read with. The redaction
#: check and the stored-record estimate use the same label.
_OP_ID = "srm.api_file.get"


class SrmApiFileNameError(ValueError):
    """The ``file`` input names no known file; no HTTP request was sent."""


def _file_path(params: dict[str, Any]) -> tuple[str, str]:
    name = params.get("file")
    if not isinstance(name, str) or name not in API_FILES:
        raise SrmApiFileNameError(f"file must be one of {sorted(API_FILES)}; got {name!r}")
    return name, API_FILES[name]


async def srm_api_file_get_impl(
    connector: SrmConnector,
    operator: Operator,
    target: SrmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``srm.api_file.get`` -- ``GET`` one API file on the appliance, text in parts.

    A ``401`` is raised as :exc:`httpx.HTTPStatusError` so the dispatcher
    drops this target's session and dispatches once more. Every other
    status is reported in ``status``.
    """
    from meho_backplane.connectors.srm import SRM_CONNECTOR_ID

    name, path = _file_path(params)
    max_bytes = get_settings().result_handle_max_record_bytes
    client = await connector._http_client(target)
    headers = await connector.auth_headers(target, operator)
    span = flight_recorder_capture.span_start()
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
    flight_recorder_capture.record_vendor_call(
        span, method="GET", request_headers=headers, response=response
    )
    if response.status_code == httpx.codes.UNAUTHORIZED:
        response.raise_for_status()

    text = decode_text(response, path)
    body = response.content
    items = text_items(body)
    version = await connector.server_version(target, operator)
    answer: dict[str, Any] = {
        "file": name,
        "path": path,
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "version": version.version,
        "build": version.build,
        "version_error": version.error,
        "redirect_to": redirect_target(response.headers.get("location"), response.request.url),
        "parts": len(items),
    }
    assert_fits_result_handle(
        answer, items, operator_sub=operator.sub, op_id=_OP_ID, max_bytes=max_bytes
    )
    answer["items"] = items
    assert_not_redacted(answer, text, operator, connector_id=SRM_CONNECTOR_ID, op_id=_OP_ID)
    return answer
