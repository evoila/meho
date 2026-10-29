# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reading every page of a Microsoft Graph list, with limits.

Graph answers a list with ``value`` and, when more rows exist, a full URL in
``@odata.nextLink``. The link already holds the query of the first page and a
page token, so it is sent as it is. :func:`list_all_pages` follows it until
the list ends or a limit stops it. The limits are the Azure connector's
(``connectors/azure/_paging.py``), imported from there:

* 50 pages,
* 120 seconds,
* 10,000 rows (the most rows one stored result holds).

When a limit stops the list, the result says so: ``truncated`` is ``True``
and ``omission`` says which limit, how far MEHO got, and how to narrow the
request. Nothing is cut silently. The result has the same shape as an Azure
list (:class:`PagedRows`), so the dispatcher keeps ``truncated``,
``omission``, ``omission_kind`` and ``pages_read`` inline when the rows go
behind a stored-result link.

Every next-page link goes through
:func:`~meho_backplane.connectors.entra._http.check_next_link` before the
token is sent to it.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._paging import PagedRows, PageLimits
from meho_backplane.connectors.entra._http import check_next_link, send
from meho_backplane.connectors.entra.session import EntraTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.entra.connector import EntraConnector

__all__ = ["PageLimits", "PagedRows", "list_all_pages"]

#: Clock. Tests replace it to move time forward.
_monotonic = time.monotonic

_NARROW_HINT: Final[str] = (
    "Narrow the request (for example with a filter of this operation) and ask again, "
    "or read the rest in the Microsoft Entra admin center."
)


class _Pager:
    """Counts pages, rows and time, and records what stopped the list."""

    def __init__(self, limits: PageLimits) -> None:
        self.limits = limits
        self.result = PagedRows()
        self._started = _monotonic()

    def add(self, rows: object) -> bool:
        """Add one page. Returns ``False`` when the row limit was reached."""
        self.result.pages += 1
        page = rows if isinstance(rows, list) else []
        room = self.limits.max_rows - len(self.result.rows)
        self.result.rows.extend(page[:room])
        if len(page) > room:
            self._stop("row_limit", self.limits.max_rows, "rows")
            return False
        return True

    def may_read_more(self) -> bool:
        """Check the page, row and time limits before the next page."""
        if len(self.result.rows) >= self.limits.max_rows:
            self._stop("row_limit", self.limits.max_rows, "rows")
            return False
        if self.result.pages >= self.limits.max_pages:
            self._stop("page_limit", self.limits.max_pages, "pages")
            return False
        if _monotonic() - self._started >= self.limits.max_seconds:
            self._stop("time_limit", self.limits.max_seconds, "seconds")
            return False
        return True

    def _stop(self, kind: str, limit: float, unit: str) -> None:
        self.result.truncated = True
        self.result.omission = {
            "kind": kind,
            "pages_read": self.result.pages,
            "rows_returned": len(self.result.rows),
            "limit": limit,
            "message": (
                f"MEHO stopped at its limit of {limit:g} {unit}. Microsoft Graph has more "
                f"rows that are not in this result. {_NARROW_HINT}"
            ),
        }


async def list_all_pages(
    connector: EntraConnector,
    target: EntraTargetLike,
    path: str,
    *,
    operator: Operator,
    permission: str,
    params: dict[str, str] | None = None,
    limits: PageLimits | None = None,
) -> PagedRows:
    """GET *path* and follow ``@odata.nextLink`` until the list ends or a limit stops it.

    *path* is one of the hand-written paths. *permission* is the application
    permission the list needs; a refusal names it. A next-page link that does
    not continue the same list on the target's Graph host fails the call
    before a request is made.
    """
    pager = _Pager(limits or PageLimits())
    response = await send(
        connector, target, path, operator=operator, permission=permission, params=params
    )
    while True:
        body: Any = json_payload_or_empty(response)
        page = body if isinstance(body, dict) else {}
        if not pager.add(page.get("value")):
            return pager.result
        next_link = page.get("@odata.nextLink")
        if not isinstance(next_link, str) or not next_link:
            return pager.result
        check_next_link(target, next_link, path=path)
        if not pager.may_read_more():
            return pager.result
        response = await send(
            connector,
            target,
            next_link,
            operator=operator,
            permission=permission,
            list_path=path,
        )
