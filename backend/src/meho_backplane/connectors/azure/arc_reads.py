# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure Arc reads and of the two stored patching lists.

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.arc.ArcOps`. The operation table and
the guidance for the AI are in :mod:`meho_backplane.connectors.azure.arc`, the
rows are built in :mod:`meho_backplane.connectors.azure.arc_rows`.

* ``azure.arc.machine.list`` reads every Arc machine of the target's
  subscription with
  :func:`~meho_backplane.connectors.azure._paging.list_all_pages`, which
  follows ``nextLink`` within the paging limits.
* ``azure.arc.machine.show`` reads one Arc machine by resource group and name.
  Both go into the URL path only after a check against the same rule as the
  parameter schema; they are then percent-encoded (a special character such
  as ``(`` is written as a ``%`` code), so they stay one part of the path.
* ``azure.patching.assessment.list`` and ``azure.patching.installation.list``
  send a fixed Resource Graph query over the target's own subscription only,
  with :func:`~meho_backplane.connectors.azure._paging.query_resource_graph`,
  which follows ``$skipToken`` within the same limits. The only value MEHO
  puts into a query is the start of the time window, a time MEHO computes
  itself from a checked number of days.

Every list returns its rows under ``value``, a ``summary`` object with counts
over every row read, and the paging fields of
:meth:`~meho_backplane.connectors.azure._paging.PagedRows.as_dict`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._paging import PagedRows, list_all_pages, query_resource_graph
from meho_backplane.connectors.azure._paths import (
    ARC_API_VERSION,
    ARC_MACHINE_PATH,
    ARC_MACHINES_PATH,
)
from meho_backplane.connectors.azure.arc_rows import (
    Row,
    assessment_row,
    assessment_summary,
    installation_row,
    installation_summary,
    machine_detail,
    machine_row,
    machine_summary,
)
from meho_backplane.connectors.azure.arc_schemas import (
    ARC_MACHINE_NAME_CHARS,
    ARC_MACHINE_NAME_MAX_LENGTH,
    INSTALLATION_DEFAULT_DAYS,
    INSTALLATION_MAX_DAYS,
    RESOURCE_GROUP_CHARS,
    RESOURCE_GROUP_MAX_LENGTH,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "ASSESSMENT_QUERY",
    "arc_machine_list_impl",
    "arc_machine_show_impl",
    "installation_query",
    "patching_assessment_list_impl",
    "patching_installation_list_impl",
]

#: The stored result of the last update check of each machine. Resource Graph
#: also keeps one row per missing update (the ``softwarepatches`` rows); they
#: are left out.
ASSESSMENT_QUERY: Final[str] = (
    "patchassessmentresources"
    + " | where type !has 'softwarepatches'"
    + " | project id, type, location, properties"
)

_RESOURCE_GROUP: Final[re.Pattern[str]] = re.compile(RESOURCE_GROUP_CHARS)
_ARC_MACHINE_NAME: Final[re.Pattern[str]] = re.compile(ARC_MACHINE_NAME_CHARS)


def _now() -> datetime:
    """The current time. Tests replace it."""
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    """*moment* as ISO 8601 in UTC, to the second, for example 2026-10-27T12:00:00Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def installation_query(since: datetime) -> str:
    """The installation runs whose record changed at *since* or later, newest first.

    Resource Graph also keeps one row per update of a run (the
    ``softwarepatches`` rows); they are left out. *since* is a time MEHO
    computes, never text from a caller.
    """
    return (
        "patchinstallationresources"
        + " | where type !has 'softwarepatches'"
        + " | extend lastModified = todatetime(properties.lastModifiedDateTime)"
        + f" | where lastModified >= datetime({_iso(since)})"
        + " | order by lastModified desc"
        + " | project id, type, location, properties"
    )


def _checked(params: Mapping[str, Any], key: str, pattern: re.Pattern[str], max_length: int) -> str:
    """Return ``params[key]`` checked, then percent-encoded.

    The parameter schema already checks the same rule; this check keeps a
    direct call safe too. Raises ``ValueError`` without repeating the value.
    """
    value = params.get(key)
    if not isinstance(value, str) or len(value) > max_length or not pattern.fullmatch(value):
        raise ValueError(
            f"{key} is not a valid Azure name. Pass the name exactly as Azure shows it."
        )
    return quote(value, safe="")


def _days(value: object) -> int:
    """The ``days`` parameter: a whole number from 1 to 30, or the default."""
    if value is None:
        return INSTALLATION_DEFAULT_DAYS
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("days must be a whole number from 1 to 30.")
    if not 1 <= value <= INSTALLATION_MAX_DAYS:
        raise ValueError("days must be a whole number from 1 to 30.")
    return value


def _answer(paged: PagedRows, rows: list[Row], **objects: Row) -> Row:
    """The answer of a list: the rows, the named objects, and how the paging went."""
    paging = paged.as_dict()
    del paging["value"]
    return {"value": rows, **objects, **paging}


async def _graph_rows(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    query: str,
    build: Callable[[Mapping[str, Any], str], Row | None],
) -> tuple[PagedRows, list[Row]]:
    """Run *query* over the target's own subscription; build one row per result.

    A result that does not belong to a machine of the target's subscription
    is left out (:func:`~.arc_rows.machine_of_result`).
    """
    subscription_id = resolve_target_config(target).subscription_id
    paged = await query_resource_graph(
        connector,
        target,
        {
            "query": query,
            "subscriptions": [subscription_id],
            "options": {"resultFormat": "objectArray"},
        },
        operator=operator,
    )
    rows: list[Row] = []
    for item in paged.rows:
        row = build(item, subscription_id) if isinstance(item, Mapping) else None
        if row is not None:
            rows.append(row)
    return paged, rows


async def arc_machine_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.arc.machine.list``: every Azure Arc machine of the subscription."""
    del params  # the operation takes no parameters
    config = resolve_target_config(target)
    paged = await list_all_pages(
        connector,
        target,
        ARC_MACHINES_PATH.format(subscription_id=config.subscription_id),
        operator=operator,
        api_version=ARC_API_VERSION,
    )
    rows = [machine_row(item) for item in paged.rows if isinstance(item, Mapping)]
    return _answer(paged, rows, summary=machine_summary(rows))


async def arc_machine_show_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.arc.machine.show``: one Azure Arc machine by resource group and name."""
    path = ARC_MACHINE_PATH.format(
        subscription_id=resolve_target_config(target).subscription_id,
        resource_group=_checked(
            params, "resource_group", _RESOURCE_GROUP, RESOURCE_GROUP_MAX_LENGTH
        ),
        name=_checked(params, "name", _ARC_MACHINE_NAME, ARC_MACHINE_NAME_MAX_LENGTH),
    )
    payload = await connector.arm_get(target, path, operator=operator, api_version=ARC_API_VERSION)
    return machine_detail(payload)


async def patching_assessment_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.patching.assessment.list``: the last stored update check of each machine."""
    del params  # the operation takes no parameters
    paged, rows = await _graph_rows(connector, operator, target, ASSESSMENT_QUERY, assessment_row)
    return _answer(paged, rows, summary=assessment_summary(rows))


async def patching_installation_list_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.patching.installation.list``: the installation runs of the last *days* days."""
    days = _days(params.get("days"))
    until = _now()
    since = until - timedelta(days=days)
    paged, rows = await _graph_rows(
        connector, operator, target, installation_query(since), installation_row
    )
    window: Row = {"days": days, "from": _iso(since), "to": _iso(until)}
    return _answer(paged, rows, window=window, summary=installation_summary(rows))
