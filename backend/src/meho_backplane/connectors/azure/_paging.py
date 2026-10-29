# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reading every page of an Azure list, with limits.

Azure answers a list with ``value`` and, when more rows exist, a full URL in
``nextLink``. Resource Graph answers with ``data`` and a ``$skipToken``; the
next page is the same query with the token in ``options``. The two helpers
here follow those links until the list ends or a limit stops them:

* :data:`MAX_PAGES` pages,
* :data:`MAX_SECONDS` seconds,
* :data:`MAX_ROWS` rows (the most rows one stored result holds).

When a limit stops a helper, the result says so: ``truncated`` is ``True``
and ``omission`` names the limit, how far the helper got, and how to narrow
the request. Resource Graph can also cut a result itself and give no token
for the next page (``resultTruncated``); that is reported the same way.
Nothing is cut silently.

The result of :meth:`PagedRows.as_dict` keeps the rows under ``value`` and
the rest as plain values, so the dispatcher's result reduction recognises the
rows and keeps ``truncated`` and the omission next to the stored-result link.

Every ``nextLink`` is checked with
:func:`~meho_backplane.connectors.azure._http.check_azure_url` before the
token is sent to it, so a link to another host or another subscription, or
a link that is not a full https URL, fails the call and nothing is sent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._http import check_azure_url, send
from meho_backplane.connectors.azure._paths import RESOURCE_GRAPH_API_VERSION, RESOURCE_GRAPH_PATH
from meho_backplane.connectors.azure.session import AzureTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "MAX_PAGES",
    "MAX_ROWS",
    "MAX_SECONDS",
    "PageLimits",
    "PagedRows",
    "list_all_pages",
    "query_resource_graph",
]

#: Most pages one list reads.
MAX_PAGES: Final[int] = 50

#: Most seconds one list reads pages for.
MAX_SECONDS: Final[float] = 120.0

#: Most rows one list returns. The same as the most rows of a stored result.
MAX_ROWS: Final[int] = 10_000

#: Clock. Tests replace it to move time forward.
_monotonic = time.monotonic

_NARROW_HINT: Final[str] = (
    "Narrow the request (for example one resource group, one resource type, a "
    "filter or a shorter time range) and ask again."
)

_GRAPH_CUT_MESSAGE: Final[str] = (
    "Azure Resource Graph cut the result and gave no token for the next page, "
    "so MEHO could not read more. Resource Graph does this when the query uses "
    "limit, sample or take, or when every output column is dynamic or null. "
    "Change the query so it has neither, or narrow it, and ask again."
)


@dataclass(frozen=True)
class PageLimits:
    """The limits of one list. The defaults are the module constants."""

    max_pages: int = MAX_PAGES
    max_seconds: float = MAX_SECONDS
    max_rows: int = MAX_ROWS


@dataclass
class PagedRows:
    """The rows of a list, and what was left out."""

    rows: list[Any] = field(default_factory=list)
    pages: int = 0
    truncated: bool = False
    omission: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """The shape an operation returns.

        ``value`` holds the rows, like Azure's own answers. ``truncated``,
        ``omission`` (the message), ``omission_kind`` and ``pages_read`` are
        plain values, so the result reduction keeps them inline when the rows
        go behind a stored-result link.
        """
        omission = self.omission or {}
        return {
            "value": self.rows,
            "truncated": self.truncated,
            "omission": omission.get("message"),
            "omission_kind": omission.get("kind"),
            "pages_read": self.pages,
        }


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

    def note_resource_graph_cut(self, answer: dict[str, Any]) -> None:
        """Record it when Resource Graph says the result is cut and gives no token.

        ``resultTruncated`` is the string ``"true"`` then. When the answer
        also says how many rows match (``totalRecords``) and every one of
        them was read, nothing is missing and nothing is recorded.
        """
        flag = answer.get("resultTruncated")
        if flag is not True and flag != "true":
            return
        total = answer.get("totalRecords")
        if isinstance(total, int) and not isinstance(total, bool):
            if len(self.result.rows) >= total:
                return
        else:
            total = None
        self._record("result_truncated", _GRAPH_CUT_MESSAGE, limit=None, total_records=total)

    def _stop(self, kind: str, limit: float, unit: str) -> None:
        message = (
            f"MEHO stopped at its limit of {limit:g} {unit}. Azure has more "
            f"rows that are not in this result. {_NARROW_HINT}"
        )
        self._record(kind, message, limit=limit)

    def _record(self, kind: str, message: str, **details: Any) -> None:
        self.result.truncated = True
        self.result.omission = {
            "kind": kind,
            "pages_read": self.result.pages,
            "rows_returned": len(self.result.rows),
            "message": message,
            **details,
        }


async def list_all_pages(
    connector: AzureConnector,
    target: AzureTargetLike,
    path: str,
    *,
    operator: Operator,
    api_version: str,
    params: dict[str, Any] | None = None,
    limits: PageLimits | None = None,
) -> PagedRows:
    """GET *path* and follow ``nextLink`` until the list ends or a limit stops it.

    *path* is a hand-written path on the target's Resource Manager host.
    Every ``nextLink`` must be a full URL from Azure inside the target's
    subscription; it already holds the ``api-version`` and the page token,
    so it is sent as it is. Any other link (another host, another
    subscription, a relative path) fails the call before a request is made.
    """
    pager = _Pager(limits or PageLimits())
    response = await send(
        connector,
        target,
        "GET",
        path,
        operator=operator,
        params={**(params or {}), "api-version": api_version},
    )
    while True:
        body = json_payload_or_empty(response)
        if not pager.add(body.get("value")):
            return pager.result
        next_link = body.get("nextLink")
        if not isinstance(next_link, str) or not next_link:
            return pager.result
        check_azure_url(target, next_link)
        if not pager.may_read_more():
            return pager.result
        response = await send(connector, target, "GET", next_link, operator=operator)


async def query_resource_graph(
    connector: AzureConnector,
    target: AzureTargetLike,
    query: dict[str, Any],
    *,
    operator: Operator,
    limits: PageLimits | None = None,
) -> PagedRows:
    """Run a Resource Graph query and follow ``$skipToken`` until the end or a limit.

    *query* is the request body (``query``, ``subscriptions``, ``options``).
    The query is a read, so it is repeated after a 5xx or a lost connection
    like a GET, and it is paced to Resource Graph's limit per login. When
    Resource Graph cuts the result itself and gives no token, the result
    says so (``omission_kind`` is ``result_truncated``).
    """
    pager = _Pager(limits or PageLimits())
    body = query
    while True:
        response = await send(
            connector,
            target,
            "POST",
            RESOURCE_GRAPH_PATH,
            operator=operator,
            params={"api-version": RESOURCE_GRAPH_API_VERSION},
            json=body,
            repeatable=True,
            resource_graph=True,
        )
        answer = json_payload_or_empty(response)
        if not pager.add(answer.get("data")):
            return pager.result
        token = answer.get("$skipToken")
        if not isinstance(token, str) or not token:
            pager.note_resource_graph_cut(answer)
            return pager.result
        if not pager.may_read_more():
            return pager.result
        options = query.get("options")
        given: dict[str, Any] = options if isinstance(options, dict) else {}
        body = {**query, "options": {**given, "$skipToken": token}}
