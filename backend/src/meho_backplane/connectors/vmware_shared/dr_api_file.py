# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``<product>.api_file.get`` for the Live Recovery REST API gateways (SRM, vSphere Replication).

Broadcom does not publish the 8.8 and 9.0 API files of Site Recovery
Manager and vSphere Replication as downloads; a running appliance serves
them. This read fetches one file named in the connector's
:attr:`~meho_backplane.connectors.vmware_shared.dr_rest.DrRestConnector.api_files`
and returns its path, HTTP status, content type, size, SHA-256 and the
server version and build, with the text in parts behind a result handle.
Joining the parts in order gives the checked copy of the file, so the
vendor API files can be pinned and compared.

No other path can be read: the input is a name, not a path. The read
sends one ``GET`` to the target's own address with the target's own
login and does not follow a redirect (``redirect_to`` carries the path).
Only UTF-8 text comes back. The body is decoded and measured in bounded
steps; a file too big for one result handle stops with its size.

The file is parsed and written back by
:func:`~meho_backplane.connectors._shared.api_file_secrets.check_api_file`,
which replaces every value under a secret name (``[REDACTED:param_name]``)
and refuses a file that does not parse. MEHO's general secret-redaction
rules run on every parsed text value before the file is written back
(:func:`~meho_backplane.connectors._shared.api_file_read.text_redactor`):
the vendor files have ``Authorization: Basic ...`` examples in their
description text, and these come back as
``[REDACTED:authorization_header]``. Because the rules see the values and
not the written file, the copy keeps its format and every value keeps its
type. ``size``, ``sha256`` and the parts describe this final copy;
``secrets_replaced`` counts every replaced value. A copy that the rules
would still change stops, so ``sha256`` always names what the caller gets.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.api_file_read import (
    fetch_checked_api_file,
    finish_api_file_read,
    redirect_target,
    text_items,
    text_redactor,
)
from meho_backplane.connectors._shared.api_file_secrets import scrub_query_string
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_shared.dr_rest import DrRestConnector, DrTargetLike

__all__ = ["ApiFileNameError", "dr_api_file_get"]


class ApiFileNameError(ValueError):
    """The ``file`` input names no known file; no HTTP request was sent."""


def _file_path(files: dict[str, str], params: dict[str, Any]) -> tuple[str, str]:
    name = params.get("file")
    if not isinstance(name, str) or name not in files:
        raise ApiFileNameError(f"file must be one of {sorted(files)}; got {name!r}")
    return name, files[name]


async def dr_api_file_get(
    connector: DrRestConnector,
    operator: Operator,
    target: DrTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``GET`` one API file on the appliance and return it checked, text in parts.

    The fetch and the file check are the shared
    :func:`fetch_checked_api_file`; the stored-record check, the
    redaction check and the flight recorder call are the shared
    :func:`finish_api_file_read`. The recorder gets the checked copy, and
    only for a file the read returns. A ``401`` is raised as
    :exc:`httpx.HTTPStatusError`, with a checked body or none, so the
    dispatcher drops this target's session and dispatches once more.
    Every other status is reported in ``status``.
    """
    op_id = f"{connector.product}.api_file.get"
    connector_id = connector.connector_id
    name, path = _file_path(connector.api_files, params)
    max_bytes = get_settings().result_handle_max_record_bytes
    client = await connector._http_client(target)
    headers = await connector.auth_headers(target, operator)
    text_rule = text_redactor(operator, connector_id=connector_id, op_id=op_id)
    fetched = await fetch_checked_api_file(
        client,
        path,
        path=path,
        headers=headers,
        extensions=connector._request_extensions(target),
        max_bytes=max_bytes,
        text_rule=text_rule,
        error_text_rule=text_rule,
    )
    response, checked = fetched.response, fetched.checked
    content_type = response.headers.get("content-type")
    body = checked.text.encode("utf-8")
    items = text_items(body)
    redirect_to, redirect_replaced = _redirect(response)
    version = await connector.server_version(target, operator)
    answer: dict[str, Any] = {
        "file": name,
        "path": path,
        "status": response.status_code,
        "content_type": content_type,
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "secrets_replaced": checked.secrets_replaced + redirect_replaced,
        "version": version.version,
        "build": version.build,
        "version_error": version.error,
        "redirect_to": redirect_to,
        "parts": len(items),
    }
    return finish_api_file_read(
        fetched,
        answer,
        items,
        operator,
        connector_id=connector_id,
        op_id=op_id,
        max_bytes=max_bytes,
    )


def _redirect(response: httpx.Response) -> tuple[str | None, int]:
    """The redirect path with any secret-named query value replaced, and the count."""
    target = redirect_target(response.headers.get("location"), response.request.url)
    if target is None:
        return None, 0
    return scrub_query_string(target)
