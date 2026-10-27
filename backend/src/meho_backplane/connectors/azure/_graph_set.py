# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One Resource Graph query over a checked set of subscriptions.

:func:`read_over_set` is the shared engine of every Resource Graph read that
can cover more than the calling target's subscription. It:

1. checks the subscription set
   (:func:`~meho_backplane.connectors.azure._subscription_set.resolve_subscription_set`);
2. sends ``POST /providers/Microsoft.ResourceGraph/resources`` with
   ``subscriptions`` filled in by MEHO, at most :data:`GRAPH_BATCH_SIZE`
   subscriptions per query (Microsoft advises fewer than 300), and never with
   ``managementGroups``;
3. follows ``$skipToken`` page by page, inside one limit of pages, rows and
   seconds for the whole read (the defaults of
   :class:`~meho_backplane.connectors.azure._paging.PageLimits`);
4. drops a row that names a subscription outside its batch. Azure should never
   send one; the check keeps the boundary even then;
5. returns the rows under ``value``, the ``subscriptions`` block, and every gap
   as a named omission in ``omissions: {count, items}``.

The omission kinds are ``subscriptions_denied``, ``page_failed``,
``subscription_limit`` (Azure's ``x-ms-tenant-subscription-limit-hit``: it
searched only the first 10,000 subscriptions), ``result_truncated`` (Resource
Graph cut the result and gave no ``$skipToken``), ``row_limit`` (the most rows
one stored result holds), ``page_limit``, ``time_limit`` and ``outside_set``.

Both blocks are objects, so the dispatcher's result reduction keeps them next
to the link to the stored result when the rows go behind it; an operation that
uses this engine names them in its ``result_objects`` hint.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._errors import AzureApiError, AzureNotFoundError
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paging import PageLimits
from meho_backplane.connectors.azure._paths import RESOURCE_GRAPH_API_VERSION, RESOURCE_GRAPH_PATH
from meho_backplane.connectors.azure._subscription_set import (
    SetMember,
    SubscriptionSet,
    resolve_subscription_set,
)
from meho_backplane.connectors.azure.session import AzureTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = ["GRAPH_BATCH_SIZE", "SUBSCRIPTION_LIMIT_HEADER", "read_over_set"]

#: Most subscriptions in one Resource Graph query. Microsoft advises fewer than 300.
GRAPH_BATCH_SIZE: Final[int] = 250

#: Azure's answer header when it searched only the first 10,000 subscriptions.
SUBSCRIPTION_LIMIT_HEADER: Final[str] = "x-ms-tenant-subscription-limit-hit"

#: Errors after which one batch counts as failed and the read goes on with the
#: next batch. A login or target error stops the whole read instead.
_BATCH_ERRORS: Final[tuple[type[Exception], ...]] = (
    AzureApiError,
    AzureNotFoundError,
    httpx.HTTPError,
)

_NARROW: Final[str] = (
    "Narrow the query (fewer subscriptions, a filter, or summarize) and ask again."
)

#: How the row limit is named in the omission: it is the stored-result cap.
_ROW_LIMIT_UNIT: Final[str] = "rows, the most one stored result holds"

#: Clock. Tests replace it to move time forward.
_monotonic = time.monotonic


async def read_over_set(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    names: Sequence[str],
    *,
    op_id: str,
    query: str,
    page_size: int | None = None,
    limits: PageLimits | None = None,
) -> dict[str, Any]:
    """Check the set named by *names*, run *query* over it, and return the result.

    A batch that Azure answers with an error is listed under ``failed``, and
    the read goes on with the next batch. When nothing could be read at all,
    the first such error is raised, so the caller gets Azure's reason (for
    example a query Resource Graph cannot parse) instead of an empty list.
    """
    subscription_set = await resolve_subscription_set(
        connector, operator, target, op_id=op_id, names=names
    )
    read = _GraphRead(
        connector, operator, target, query, page_size, limits or PageLimits(), _monotonic()
    )
    for ids, members in _batches(subscription_set.members):
        reason = await read.read_batch(ids)
        if reason is None:
            subscription_set.mark_completed(members)
        else:
            subscription_set.mark_failed(members, reason)
    subscription_set.bind_audit()
    if read.first_error is not None and not read.rows and not subscription_set.completed:
        raise read.first_error
    return read.result(subscription_set)


def _batches(members: list[SetMember]) -> Iterator[tuple[list[str], list[SetMember]]]:
    """The members grouped by subscription ID, :data:`GRAPH_BATCH_SIZE` IDs per batch."""
    by_id: dict[str, list[SetMember]] = {}
    for member in members:
        by_id.setdefault(member.subscription_id, []).append(member)
    ids = list(by_id)
    for start in range(0, len(ids), GRAPH_BATCH_SIZE):
        batch = ids[start : start + GRAPH_BATCH_SIZE]
        yield batch, [member for sub_id in batch for member in by_id[sub_id]]


def _outside(row: object, allowed: set[str]) -> bool:
    """True when *row* names a subscription that is not in *allowed*."""
    value = row.get("subscriptionId") if isinstance(row, dict) else None
    return isinstance(value, str) and bool(value) and value.lower() not in allowed


@dataclass
class _GraphRead:
    """One Resource Graph read over the set: the rows, the pages and the gaps."""

    connector: AzureConnector
    operator: Operator
    target: AzureTargetLike
    query: str
    page_size: int | None
    limits: PageLimits
    #: When the read started, from :data:`_monotonic` (the clock tests replace).
    started: float
    rows: list[Any] = field(default_factory=list)
    pages: int = 0
    omissions: list[dict[str, Any]] = field(default_factory=list)
    stopped: str | None = None
    outside_rows: int = 0
    first_error: Exception | None = None

    async def read_batch(self, ids: list[str]) -> str | None:
        """Read every page of one batch. Returns why it is not complete, or ``None``."""
        options: dict[str, Any] = {"resultFormat": "objectArray"}
        if self.page_size is not None:
            options["$top"] = self.page_size
        body: dict[str, Any] = {"query": self.query, "subscriptions": ids, "options": options}
        allowed = set(ids)
        received = 0
        page = 0
        while not self._limit_reached():
            page += 1
            try:
                response = await self._send(body)
            except _BATCH_ERRORS as exc:
                return self._page_failed(exc, page, len(ids))
            answer = self._take_page(response)
            data = answer.get("data")
            rows = data if isinstance(data, list) else []
            received += len(rows)
            if not self._add(rows, allowed):
                break
            token = answer.get("$skipToken")
            if not isinstance(token, str) or not token:
                self._note_cut(answer, received)
                return None
            body = {**body, "options": {**options, "$skipToken": token}}
        return self.stopped

    async def _send(self, body: dict[str, Any]) -> httpx.Response:
        """One page: a read-only POST, sent again after a 5xx like a GET."""
        return await send(
            self.connector,
            self.target,
            "POST",
            RESOURCE_GRAPH_PATH,
            operator=self.operator,
            params={"api-version": RESOURCE_GRAPH_API_VERSION},
            json=body,
            repeatable=True,
            resource_graph=True,
        )

    def _take_page(self, response: httpx.Response) -> dict[str, Any]:
        """Count the page, note Azure's subscription cut, and return the answer."""
        self.pages += 1
        cut = response.headers.get(SUBSCRIPTION_LIMIT_HEADER, "").strip().lower() == "true"
        if cut and not any(item["kind"] == "subscription_limit" for item in self.omissions):
            self.omissions.append(
                {
                    "kind": "subscription_limit",
                    "message": (
                        "Azure searched only the first 10,000 subscriptions the login can "
                        "see (x-ms-tenant-subscription-limit-hit), so rows of the other "
                        "subscriptions are missing. Name fewer subscriptions."
                    ),
                }
            )
        return json_payload_or_empty(response)

    def _limit_reached(self) -> bool:
        """Check the row, page and time limits before the next page."""
        if self.stopped is None:
            if len(self.rows) >= self.limits.max_rows:
                self._stop("row_limit", self.limits.max_rows, _ROW_LIMIT_UNIT)
            elif self.pages >= self.limits.max_pages:
                self._stop("page_limit", self.limits.max_pages, "pages")
            elif _monotonic() - self.started >= self.limits.max_seconds:
                self._stop("time_limit", self.limits.max_seconds, "seconds")
        return self.stopped is not None

    def _stop(self, kind: str, limit: float, unit: str) -> None:
        self.stopped = f"MEHO stopped reading at its limit of {limit:g} {unit}."
        self.omissions.append(
            {
                "kind": kind,
                "limit": limit,
                "message": f"{self.stopped} Rows after that point are missing. {_NARROW}",
            }
        )

    def _add(self, rows: list[Any], allowed: set[str]) -> bool:
        """Keep the rows of the batch. Returns ``False`` when the row limit was reached."""
        for row in rows:
            if _outside(row, allowed):
                self.outside_rows += 1
            elif len(self.rows) >= self.limits.max_rows:
                self._stop("row_limit", self.limits.max_rows, _ROW_LIMIT_UNIT)
                return False
            else:
                self.rows.append(row)
        return True

    def _page_failed(self, exc: Exception, page: int, count: int) -> str:
        """Record a page Azure answered with an error. Returns the reason for ``failed``."""
        if self.first_error is None:
            self.first_error = exc
        detail = (
            str(exc)
            if isinstance(exc, AzureApiError | AzureNotFoundError)
            else f"the connection to Azure failed ({type(exc).__name__})"
        )
        reason = f"Page {page} of the query failed: {detail}"
        self.omissions.append(
            {
                "kind": "page_failed",
                "page": page,
                "subscription_count": count,
                "message": (
                    f"{reason} Rows of these {count} subscriptions from that page on are "
                    "missing; rows read before are kept."
                ),
            }
        )
        return reason

    def _note_cut(self, answer: dict[str, Any], received: int) -> None:
        """Record it when Resource Graph cut the batch's result and gave no token."""
        if str(answer.get("resultTruncated")).lower() != "true":
            return
        total = answer.get("totalRecords")
        if isinstance(total, int) and not isinstance(total, bool):
            if received >= total:
                return
        else:
            total = None
        self.omissions.append(
            {
                "kind": "result_truncated",
                "total_records": total,
                "message": (
                    "Resource Graph cut the result and gave no token for the next page: "
                    "the query uses limit, take or sample, or every output column is "
                    "dynamic or null. Rows are missing. Change the query and ask again."
                ),
            }
        )

    def result(self, subscription_set: SubscriptionSet) -> dict[str, Any]:
        """The result of the read: rows, the ``subscriptions`` block and the omissions."""
        omissions = list(self.omissions)
        if subscription_set.denied:
            count = len(subscription_set.denied)
            omissions.insert(
                0,
                {
                    "kind": "subscriptions_denied",
                    "count": count,
                    "message": (
                        f"MEHO did not read {count} of the requested subscriptions; "
                        "subscriptions.denied names each one and the reason."
                    ),
                },
            )
        if self.outside_rows:
            omissions.append(
                {
                    "kind": "outside_set",
                    "count": self.outside_rows,
                    "message": (
                        "Azure sent rows of subscriptions that were not asked for. MEHO "
                        "dropped them, so they are not in the result."
                    ),
                }
            )
        return {
            "value": self.rows,
            "complete": not omissions,
            "pages_read": self.pages,
            "subscriptions": subscription_set.as_block(),
            "omissions": {"count": len(omissions), "items": omissions},
        }
