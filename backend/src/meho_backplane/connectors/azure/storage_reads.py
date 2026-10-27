# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure storage reads.

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.storage.StorageOps`. Every function
here only reads. Nothing changes in Azure, and no account key is ever read.

Two hosts:

* **Resource Manager** (``management.azure.com``): the account and container
  lists, with the target's Resource Manager token, through
  :func:`~meho_backplane.connectors.azure._paging.list_all_pages`.
* **The blob data plane** (``https://<account>.blob.core.windows.net``): the
  blob list and the file read. These calls need a token for Azure Storage
  (scope :data:`STORAGE_SCOPE`), never the Resource Manager token. It comes
  from the same token cache, which keeps one token per scope. MEHO first
  finds the account in the target's subscription (:func:`find_account`), so
  a caller reaches only accounts of that subscription, and the host is built
  from the checked account name, never taken from the caller.

Both hosts go through the shared :func:`~meho_backplane.connectors.azure._http.send`:
one retry budget, a wait when Azure throttles, one new login after a 401,
the limit per login and the URL check, which admits the account's own blob
host and no other. :class:`_StorageCaller` lets ``send`` use the storage
token.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import parse_qsl, quote

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._errors import AzureForbiddenError, AzureNotFoundError
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paging import (
    PagedRows,
    PageLimits,
    _Pager,
    list_all_pages,
)
from meho_backplane.connectors.azure._paths import (
    BLOB_API_VERSION,
    BLOB_CONTAINERS_PATH,
    BLOB_LIST_PATH,
    BLOB_PATH,
    STORAGE_ACCOUNTS_PATH,
    STORAGE_API_VERSION,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config
from meho_backplane.connectors.azure.storage_parse import (
    MAX_TEXT_BYTES,
    FileProperties,
    StorageAccount,
    StorageReadError,
    account_name,
    account_row,
    binary_main_type,
    blob_name,
    blob_page,
    blob_prefix,
    container_name,
    container_row,
    file_properties,
    list_answer,
    max_results,
    resource_group_name,
    storage_account,
    text_content,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "STORAGE_SCOPE",
    "account_list_impl",
    "blob_list_impl",
    "blob_read_impl",
    "container_list_impl",
    "find_account",
]

#: The token scope of Azure Storage. Microsoft documents it as the same in
#: every Azure cloud.
STORAGE_SCOPE: Final[str] = "https://storage.azure.com/.default"

#: The most rows Azure returns in one List Blobs page.
_AZURE_PAGE_SIZE: Final[int] = 5000

_DATA_ROLE_REMEDIATION: Final[str] = (
    "Give the login's service principal the Storage Blob Data Reader role on the "
    "storage account or the container, wait up to 10 minutes for Azure to apply it, "
    "then try again. The Reader role alone gives no access to the files. If the login "
    "has that role, the account's network rules may block MEHO. azure.about shows "
    "which login MEHO uses."
)


class _StorageCaller:
    """The connector as :func:`send` sees it, but with a token for Azure Storage.

    ``send`` asks its connector for the request headers before every try, so
    a try after a 401 gets a new token. This object answers with the
    connector's token for :data:`STORAGE_SCOPE` and the Blob service
    headers. The client, the trace and the 401 handling stay the
    connector's own. ``send`` has no way yet to ask for another scope; this
    object is the bridge until it has one.
    """

    def __init__(self, connector: AzureConnector, headers: Mapping[str, str]) -> None:
        self._connector = connector
        self._headers = dict(headers)

    async def authorize(
        self, target: AzureTargetLike, operator: Operator
    ) -> tuple[dict[str, str], tuple[str, str]]:
        headers, login = await self._connector.authorize(target, operator, scope=STORAGE_SCOPE)
        return {**headers, **self._headers}, login

    async def invalidate_session(self, target: AzureTargetLike) -> None:
        await self._connector.invalidate_session(target)

    async def _http_client(self, target: AzureTargetLike) -> httpx.AsyncClient:
        return await self._connector._http_client(target)

    def _request_extensions(self, target: AzureTargetLike) -> dict[str, Any]:
        return self._connector._request_extensions(target)

    def _trace_response_body(self, start: Any, response: httpx.Response) -> Any:
        return self._connector._trace_response_body(start, response)


async def _blob_call(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    account: StorageAccount,
    method: str,
    path: str,
    *,
    missing: str,
    params: dict[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Send one call to the account's blob host and return the answer.

    *path* is a data-plane path built from checked names. Azure's 403 becomes
    the error that names the data role the login needs; a 404 names
    *missing*, what Azure did not find.
    """
    caller = _StorageCaller(connector, {"x-ms-version": BLOB_API_VERSION, **(headers or {})})
    try:
        return await send(
            cast("AzureConnector", caller),
            target,
            method,
            f"https://{account.blob_host}{path}",
            operator=operator,
            params=params,
            extra_hosts=(account.blob_host,),
        )
    except AzureForbiddenError as exc:
        raise AzureForbiddenError(
            f"Azure target {target.name!r}: Azure Storage refused to read the files of "
            f"storage account {account.name!r} (HTTP 403). The login needs the Storage "
            f"Blob Data Reader role on the account or the container. {_DATA_ROLE_REMEDIATION}",
            http_status=exc.http_status,
            azure_error_code=exc.azure_error_code,
            azure_message=exc.azure_message,
            remediation=_DATA_ROLE_REMEDIATION,
        ) from exc
    except AzureNotFoundError as exc:
        raise AzureNotFoundError(
            [f"{account.name}/{path.lstrip('/')}"],
            f"Azure target {target.name!r}: storage account {account.name!r} has no "
            f"{missing}. Check the name with azure.storage.container.list or "
            "azure.storage.blob.list.",
        ) from exc


async def _account_pages(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike
) -> PagedRows:
    """Every storage account of the target's subscription, from Resource Manager."""
    config = resolve_target_config(target)
    return await list_all_pages(
        connector,
        target,
        STORAGE_ACCOUNTS_PATH.format(subscription_id=config.subscription_id),
        operator=operator,
        api_version=STORAGE_API_VERSION,
    )


async def find_account(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, name: str
) -> StorageAccount:
    """Find the storage account *name* (already checked) in the target's subscription.

    One storage account list call on Resource Manager. The blob reads call
    this before they send anything to a blob host: a caller reaches only an
    account of the target's own subscription, and the host must be the
    endpoint Azure reports for that account.
    """
    paged = await _account_pages(connector, operator, target)
    for item in paged.rows:
        if isinstance(item, dict) and item.get("name") == name:
            cloud = resolve_target_config(target).cloud.name
            return storage_account(item, name, cloud, target.name)
    among = " among the accounts MEHO read before its limit" if paged.truncated else ""
    raise AzureNotFoundError(
        [name],
        f"Azure target {target.name!r}: the subscription has no storage account named "
        f"{name!r}{among}. Check the name with azure.storage.account.list.",
    )


async def account_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.storage.account.list``: the storage accounts of the subscription."""
    del params  # the operation takes no parameters
    paged = await _account_pages(connector, operator, target)
    rows = [account_row(item) for item in paged.rows if isinstance(item, dict)]
    return list_answer(paged, rows)


async def container_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.storage.container.list``: the containers of one account, from Resource Manager."""
    group = resource_group_name(params.get("resource_group"), target.name)
    name = account_name(params.get("account"), target.name)
    config = resolve_target_config(target)
    paged = await list_all_pages(
        connector,
        target,
        BLOB_CONTAINERS_PATH.format(
            subscription_id=config.subscription_id, resource_group=group, account=name
        ),
        operator=operator,
        api_version=STORAGE_API_VERSION,
    )
    rows = [container_row(item) for item in paged.rows if isinstance(item, dict)]
    return list_answer(paged, rows, account={"name": name, "resource_group": group})


async def blob_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.storage.blob.list``: the files of one container, from the blob data plane.

    Follows Azure's ``NextMarker`` page by page until the list ends, the
    caller's ``max_results`` is reached or a paging limit stops it.
    """
    name = account_name(params.get("account"), target.name)
    container = container_name(params.get("container"), target.name)
    prefix = blob_prefix(params.get("prefix"), target.name)
    limit = max_results(params.get("max_results"), target.name)
    account = await find_account(connector, operator, target, name)
    path, _, fixed_query = BLOB_LIST_PATH.format(container=container).partition("?")
    pager = _Pager(PageLimits(max_rows=limit))
    marker: str | None = None
    while True:
        query = dict(parse_qsl(fixed_query))
        query["maxresults"] = str(min(_AZURE_PAGE_SIZE, limit - len(pager.result.rows)))
        if prefix is not None:
            query["prefix"] = prefix
        if marker is not None:
            query["marker"] = marker
        response = await _blob_call(
            connector,
            operator,
            target,
            account,
            "GET",
            path,
            missing=f"container {container!r}",
            params=query,
        )
        rows, marker = blob_page(response.content, target.name)
        if not pager.add(rows) or marker is None or not pager.may_read_more():
            break
    return list_answer(
        pager.result,
        pager.result.rows,
        account=account.as_dict(),
        container=container,
        prefix=prefix,
    )


def _refuse_before_reading(target_name: str, blob: str, found: FileProperties) -> None:
    """Refuse a file that is too big, archived, compressed or not text, before its bytes.

    A stored content encoding (for example gzip) is refused because the HTTP
    client would unpack the bytes, and unpacked they can be far bigger than
    the size limit.
    """
    if found.size is None or found.size > MAX_TEXT_BYTES:
        size = "of unknown size" if found.size is None else f"{found.size:,} bytes"
        raise StorageReadError(
            f"Azure target {target_name!r}: file {blob!r} is {size}. "
            f"azure.storage.blob.read returns files of at most {MAX_TEXT_BYTES:,} bytes "
            "(1 MiB).",
            remediation="Read a smaller file. azure.storage.blob.list shows each file's size.",
        )
    if (found.access_tier or "").lower() == "archive":
        raise StorageReadError(
            f"Azure target {target_name!r}: file {blob!r} is in the Archive tier, so "
            "Azure cannot read it now.",
            remediation=(
                "An Azure administrator must move the file to an online tier (rehydrate "
                "it) first. MEHO does not do that."
            ),
        )
    main_type = binary_main_type(found.content_type)
    if main_type is not None:
        raise _not_text(target_name, blob, f"its content type ({main_type}/...) is never text")
    if (found.content_encoding or "identity").lower() != "identity":
        raise _not_text(target_name, blob, "it is stored with a content encoding, such as gzip")


def _changed(target_name: str, blob: str) -> StorageReadError:
    return StorageReadError(
        f"Azure target {target_name!r}: file {blob!r} changed while MEHO read it.",
        remediation="Ask again.",
    )


def _not_text(target_name: str, blob: str, reason: str) -> StorageReadError:
    return StorageReadError(
        f"Azure target {target_name!r}: file {blob!r} is not a text file ({reason}). "
        "azure.storage.blob.read returns UTF-8 text files only and never returns binary "
        "content as text.",
        remediation="Read a text file instead, for example a checksum file or a log file.",
    )


async def blob_read_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.storage.blob.read``: one small UTF-8 text file, from the blob data plane.

    Reads the file's properties first (HEAD) and refuses a file over
    :data:`MAX_TEXT_BYTES`, an archived file, a file stored with a content
    encoding (for example gzip) or one whose content type is never text,
    without reading its bytes. Then reads the bytes with a range that stops
    at the size limit, checks that the file did not change in between, and
    refuses bytes that are not UTF-8 text.
    """
    name = account_name(params.get("account"), target.name)
    container = container_name(params.get("container"), target.name)
    blob = blob_name(params.get("name"), target.name)
    account = await find_account(connector, operator, target, name)
    path = BLOB_PATH.format(container=container, blob=quote(blob, safe="/"))
    missing = f"file {blob!r} in container {container!r}"
    head = await _blob_call(connector, operator, target, account, "HEAD", path, missing=missing)
    found = file_properties(head.headers)
    _refuse_before_reading(target.name, blob, found)
    data = b""
    if found.size:
        got = await _blob_call(
            connector,
            operator,
            target,
            account,
            "GET",
            path,
            missing=missing,
            headers={"x-ms-range": f"bytes=0-{MAX_TEXT_BYTES - 1}"},
        )
        # The ETag is checked before the body is touched: only the file the
        # first answer described (no content encoding) is ever read.
        if got.headers.get("etag") != head.headers.get("etag"):
            raise _changed(target.name, blob)
        data = got.content
        if len(data) != found.size:
            raise _changed(target.name, blob)
    text = text_content(data)
    if text is None:
        raise _not_text(target.name, blob, "it holds bytes that text never has")
    return {
        "account": account.as_dict(),
        "container": container,
        "name": blob,
        **found.as_dict(),
        "encoding": "utf-8",
        "content": text,
    }
