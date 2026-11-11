# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared pieces of the Azure VM metric reads: window, VM IDs, batch call, memory size.

The two reads :mod:`.monitor_reads` (one VM) and :mod:`.monitor_summary` (many
VMs) use these. The batch read needs a token for another API
(:data:`METRICS_SCOPE`), which the connector's token cache keeps apart. MEHO
sends that token only to ``{region}.metrics.monitor.azure.com``, for a region
in which the target's own subscription has a VM, and only for the public cloud.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._errors import AzureApiError, AzureForbiddenError
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paths import METRICS_BATCH_API_VERSION, METRICS_BATCH_PATH
from meho_backplane.connectors.azure.compute_reads import NAME_PATTERN, vm_size_list_impl
from meho_backplane.connectors.azure.monitor_rows import CPU_METRIC, MEMORY_METRIC, time_text
from meho_backplane.connectors.azure.session import (
    AzureTargetConfig,
    AzureTargetConfigError,
    AzureTargetLike,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "BATCH_SIZE",
    "INTERVAL",
    "MAX_DAYS",
    "MAX_VMS",
    "METRICS_SCOPE",
    "REGION",
    "VM_ID_PATTERN",
    "Ctx",
    "days_param",
    "floor_hour",
    "list_window",
    "memory_by_size",
    "memory_gb",
    "read_batch",
    "require_public_cloud",
    "utcnow",
    "vm_ref",
]

#: The token scope of the Azure Monitor metrics host. It is a further audience.
METRICS_SCOPE: Final[str] = "https://metrics.monitor.azure.com/.default"

#: The metric namespace of VMs, which the batch read needs.
_VM_NAMESPACE: Final[str] = "Microsoft.Compute/virtualMachines"

#: Most VMs in one batch call. Azure's limit.
BATCH_SIZE: Final[int] = 50

#: Most VMs one summary reads. More are left out and the result says so.
MAX_VMS: Final[int] = 1000

#: Longest window. Azure keeps platform metrics for 93 days.
MAX_DAYS: Final[int] = 93

#: The size of one sample: one hour.
INTERVAL: Final[str] = "PT1H"

_DEFAULT_LIST_DAYS: Final[int] = 7
DEFAULT_SUMMARY_DAYS: Final[int] = 30

#: A region name as the metrics host uses it in its name.
REGION: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]{1,64}")

#: The resource ID of a VM.
VM_ID_PATTERN: Final[str] = (
    r"^/subscriptions/[0-9A-Fa-f-]{36}/resourceGroups/(?!\.+/)[^/\\?#%\s]+"
    r"/providers/Microsoft\.Compute/virtualMachines/(?!\.+$)[^/\\?#%\s]+$"
)
_VM_ID: Final[re.Pattern[str]] = re.compile(
    r"/subscriptions/(?P<sub>[^/]+)/resourceGroups/(?P<group>[^/]+)"
    r"/providers/Microsoft\.Compute/virtualMachines/(?P<name>[^/]+)",
    re.IGNORECASE,
)
_NAME: Final[re.Pattern[str]] = re.compile(NAME_PATTERN)


@dataclass(frozen=True)
class Ctx:
    """The connector, the caller and the target of one read."""

    connector: AzureConnector
    operator: Operator
    target: AzureTargetLike


# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------


def utcnow() -> datetime:
    """The clock. Tests replace it."""
    return datetime.now(UTC)


def floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _time_param(params: dict[str, Any], key: str) -> datetime | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Parameter {key!r} must be a date and time such as 2026-10-01T08:00:00Z.")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"Parameter {key!r} must be a date and time such as 2026-10-01T08:00:00Z."
        ) from None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def list_window(params: dict[str, Any], now: datetime) -> tuple[datetime, datetime]:
    """The window of ``azure.vm.metric.list``: whole hours, at most :data:`MAX_DAYS`.

    ``to`` is cut down to the hour and may not be in the future; without it
    the window ends at the last full hour. ``from`` is cut down to the hour;
    without it the window is seven days.
    """
    last_full_hour = floor_hour(now)
    given_end = _time_param(params, "to")
    end = floor_hour(given_end) if given_end is not None else last_full_hour
    if end > last_full_hour:
        raise ValueError("Parameter 'to' must not be in the future.")
    given_start = _time_param(params, "from")
    start = (
        floor_hour(given_start)
        if given_start is not None
        else end - timedelta(days=_DEFAULT_LIST_DAYS)
    )
    if start >= end:
        raise ValueError("Parameter 'from' must be earlier than 'to' by at least one full hour.")
    if end - start > timedelta(days=MAX_DAYS):
        raise ValueError(
            f"The window may not be longer than {MAX_DAYS} days: Azure keeps platform "
            "metrics for 93 days."
        )
    return start, end


def days_param(params: dict[str, Any]) -> int:
    value = params.get("days", DEFAULT_SUMMARY_DAYS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_DAYS:
        raise ValueError(f"Parameter 'days' must be a whole number from 1 to {MAX_DAYS}.")
    return value


def vm_ref(value: object, subscription_id: str, key: str) -> tuple[str, str]:
    """The resource group and name of a VM ID. The ID must be in the target's subscription."""
    match = _VM_ID.fullmatch(value) if isinstance(value, str) else None
    if (
        match is None
        or match["sub"].lower() != subscription_id
        or not _NAME.fullmatch(match["group"])
        or not _NAME.fullmatch(match["name"])
    ):
        raise ValueError(
            f"{key} must be the resource ID of a VM in this target's subscription, as "
            "azure.vm.list shows it: /subscriptions/<id>/resourceGroups/<group>/providers/"
            "Microsoft.Compute/virtualMachines/<name>."
        )
    return match["group"], match["name"]


# ----------------------------------------------------------------------
# Calls
# ----------------------------------------------------------------------


class MetricsCaller:
    """The connector as :func:`send` sees it, but with a token for the metrics host.

    ``send`` asks its connector for the request headers before every try, so a
    try after a 401 gets a new token. This object answers with the connector's
    token for :data:`METRICS_SCOPE`. The client, the trace and the 401
    handling stay the connector's own.
    """

    def __init__(self, connector: AzureConnector) -> None:
        self._connector = connector

    async def authorize(
        self, target: AzureTargetLike, operator: Operator
    ) -> tuple[dict[str, str], tuple[str, str]]:
        return await self._connector.authorize(target, operator, scope=METRICS_SCOPE)

    async def invalidate_session(self, target: AzureTargetLike) -> None:
        await self._connector.invalidate_session(target)

    async def _http_client(self, target: AzureTargetLike) -> httpx.AsyncClient:
        return await self._connector._http_client(target)

    def _request_extensions(self, target: AzureTargetLike) -> dict[str, Any]:
        return self._connector._request_extensions(target)

    def _trace_response_body(self, start: Any, response: httpx.Response) -> Any:
        return self._connector._trace_response_body(start, response)


def require_public_cloud(target: AzureTargetLike, config: AzureTargetConfig) -> None:
    """The regional metrics host is proven for the public cloud only."""
    if config.cloud.name != "AzureCloud":
        raise AzureTargetConfigError(
            f"Azure target {target.name!r}: the batch metrics read is available for the "
            "AzureCloud cloud only. Use azure.vm.metric.list for one VM at a time."
        )


async def memory_by_size(ctx: Ctx, region: str) -> tuple[dict[str, float], str | None]:
    """``({lower-case size name: memory in GB}, note)`` for one region.

    When the size list cannot be read, the map is empty and the note says why,
    so memory use is unknown and not wrong. A missing login or role is not
    hidden: it passes up like every other read.
    """
    try:
        sizes = await vm_size_list_impl(
            ctx.connector, ctx.operator, ctx.target, {"location": region}
        )
    except AzureForbiddenError:
        raise
    except AzureApiError as exc:
        return {}, (
            f"The VM sizes of region {region} could not be read (HTTP {exc.http_status}), "
            "so the memory size is unknown."
        )
    note = (
        "The VM size list of this region was cut at a limit, so some memory sizes may be missing."
        if sizes.get("truncated")
        else None
    )
    memory = {
        row["name"].lower(): float(row["memory_gb"])
        for row in sizes["value"]
        if isinstance(row.get("name"), str) and isinstance(row.get("memory_gb"), (int, float))
    }
    return memory, note


def memory_gb(sizes: dict[str, float], size: object) -> float | None:
    return sizes.get(size.lower()) if isinstance(size, str) else None


async def read_batch(
    ctx: Ctx,
    subscription_id: str,
    region: str,
    ids: list[str],
    window: tuple[datetime, datetime],
) -> dict[str, list[Any]]:
    """One batch call for up to :data:`BATCH_SIZE` VMs of one region.

    Returns ``{lower-case VM ID: metrics}`` for the VMs Azure answered for.
    """
    host = f"{region}.metrics.monitor.azure.com"
    response = await send(
        cast("AzureConnector", MetricsCaller(ctx.connector)),
        ctx.target,
        "POST",
        f"https://{host}{METRICS_BATCH_PATH.format(subscription_id=subscription_id)}",
        operator=ctx.operator,
        params={
            "api-version": METRICS_BATCH_API_VERSION,
            "metricnamespace": _VM_NAMESPACE,
            "metricnames": f"{CPU_METRIC},{MEMORY_METRIC}",
            "starttime": time_text(window[0]),
            "endtime": time_text(window[1]),
            "interval": INTERVAL,
            "aggregation": "average,minimum,maximum",
        },
        json={"resourceids": ids},
        repeatable=True,
        extra_hosts=(host,),
    )
    answered: dict[str, list[Any]] = {}
    items = json_payload_or_empty(response).get("values")
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("resourceid"), str):
            value = item.get("value")
            answered[item["resourceid"].lower()] = value if isinstance(value, list) else []
    return answered
