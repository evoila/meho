# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The upload of a CSV file to the link Azure Migrate gives for an import.

The link is a signed blob link. It is a secret and carries its own access, so
this module never sends MEHO's Azure token to it, and never puts the link
into an error text. :func:`upload_host` refuses a link that is not a plain
https link to a blob host of the target's cloud. :func:`upload_file` sends the
file through the connector's own ``send`` (the same retry rule, the same
trace), with a caller object that gives no ``Authorization`` header.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._errors import (
    AzureApiError,
    AzureRequestError,
    AzureServerError,
    AzureUrlRefusedError,
)
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config
from meho_backplane.connectors.azure.storage_parse import BLOB_HOST_SUFFIXES

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = ["upload_file", "upload_host"]


class _UploadCaller:
    """The connector as :func:`send` sees it, but with no token.

    ``send`` asks its connector for the request headers before every try.
    This object gives it the login (only for the limit per login) and the
    headers of the upload, and never an ``Authorization`` header. The client
    and the trace stay the connector's own.
    """

    def __init__(self, connector: AzureConnector, headers: Mapping[str, str]) -> None:
        self._connector = connector
        self._headers = dict(headers)

    async def authorize(
        self, target: AzureTargetLike, operator: Operator
    ) -> tuple[dict[str, str], tuple[str, str]]:
        _token_header, login = await self._connector.authorize(target, operator)
        return dict(self._headers), login

    async def invalidate_session(self, target: AzureTargetLike) -> None:
        """A link has no session. Nothing to drop."""

    async def _http_client(self, target: AzureTargetLike) -> httpx.AsyncClient:
        return await self._connector._http_client(target)

    def _request_extensions(self, target: AzureTargetLike) -> dict[str, Any]:
        return self._connector._request_extensions(target)

    def _trace_response_body(self, start: Any, response: httpx.Response) -> Any:
        return self._connector._trace_response_body(start, response)


def upload_host(target: AzureTargetLike, link: object) -> str:
    """The host of the upload link, when the link is a plain https blob link.

    Raises :class:`AzureUrlRefusedError`, with no text of the link in it,
    when it is anything else.
    """
    cloud = resolve_target_config(target).cloud.name
    suffix = f".{BLOB_HOST_SUFFIXES[cloud]}"
    try:
        parts = urlsplit(link) if isinstance(link, str) else None
        host = (parts.hostname or "").lower() if parts else ""
        port = parts.port if parts else None
    except ValueError:
        parts, host, port = None, "", None
    if (
        parts is None
        or parts.scheme != "https"
        or parts.username
        or parts.password
        or port not in (None, 443)
        or not host.endswith(suffix)
        or len(host) == len(suffix)
    ):
        raise AzureUrlRefusedError(
            f"Azure target {target.name!r}: the upload link from Azure is not a plain https "
            f"link to a *{suffix} host. MEHO uploaded nothing."
        )
    return host


async def upload_file(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    link: str,
    content: bytes,
) -> bool:
    """Upload *content* to *link*. Returns false when Azure's answer is not known.

    A lost connection or a 5xx can come after the file arrived, so they are
    not errors: the caller follows the job to see. Any other refusal is
    raised with a fixed text that never holds the link.
    """
    host = upload_host(target, link)
    caller = _UploadCaller(
        connector, {"x-ms-blob-type": "BlockBlob", "Content-Type": "text/csv; charset=utf-8"}
    )
    try:
        await send(
            caller,  # type: ignore[arg-type]
            target,
            "PUT",
            link,
            operator=operator,
            content=content,
            repeatable=False,
            extra_hosts=(host,),
        )
    except (httpx.HTTPError, AzureServerError):
        return False
    except AzureApiError as exc:
        raise AzureRequestError(
            f"Azure target {target.name!r}: Azure Storage refused the upload (HTTP "
            f"{exc.http_status}). The upload link may have run out; send the same call "
            "again for a new link.",
            http_status=exc.http_status,
            azure_error_code=exc.azure_error_code,
            azure_message=None,
            remediation="Send the same call again. It asks for a new upload link.",
        ) from None
    return True
