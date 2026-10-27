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
and ``omissions`` names the limit, how far the helper got, and how to narrow
the request. Nothing is cut silently.

Every ``nextLink`` is checked with
:func:`~meho_backplane.connectors.azure._http.check_azure_url` before the
token is sent to it, so a link to another host or another subscription
fails the call and nothing is sent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._http import send
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


@dataclass(frozen=True)
class PageLimits:
    """The limits of one list. The defaults are the module constants."""

    max_pages: int = MAX_PAGES
    max_seconds: float = MAX_SECONDS
    max_rows: int = MAX_ROWS


@dataclass
class PagedRows:
    """The rows of a list, and what a limit left out."""

    rows: list[Any] = field(default_factory=list)
    pages: int = 0
    truncated: bool = False
    omissions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """The shape an operation returns: ``rows``, ``truncated``, ``omissions``."""
        return {"rows": self.rows, "truncated": self.truncated, "omissions": self.omissions}


class _Pager:
    """Counts pages, rows and time, and records the limit that stopped the list."""

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
        self.result.omissions.append(
            {
                "kind": kind,
                "limit": limit,
                "pages_read": self.result.pages,
                "rows_returned": len(self.result.rows),
                "message": (
                    f"MEHO stopped at its limit of {limit:g} {unit}. Azure has more "
                    f"rows that are not in this result. {_NARROW_HINT}"
                ),
            }
        )


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
    Every ``nextLink`` is a full URL from Azure; it already holds the
    ``api-version`` and the page token, so it is sent as it is.
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
    like a GET, and it is paced to Resource Graph's limit per login.
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
            return pager.result
        if not pager.may_read_more():
            return pager.result
        options = query.get("options")
        given: dict[str, Any] = options if isinstance(options, dict) else {}
        body = {**query, "options": {**given, "$skipToken": token}}
