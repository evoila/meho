# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Utilization for ``vmware.migration.inventory``: vCenter history or VCF Operations.

Two sources, one answer shape (:class:`~...migration.contracts.Utilization`):

* **vCenter performance history** -- one ``QueryPerf`` per batch of VMs on the
  connector session, for four counters (CPU and memory use in percent,
  network received and sent). The counter ids come from the
  ``PerformanceManager.perfCounter`` list, read once. The sample interval
  follows the window: 5 minutes up to a day, 30 minutes up to a week,
  2 hours up to a month, 1 day above (vCenter's historical intervals).
* **VCF Operations** -- per VM, the ``vrops.resource.query`` and
  ``vrops.resource.stats`` operations, run as child operations on the named
  VCF Operations target. That read gives the latest values only, so the
  window is the time of those samples and ``samples`` says how few they are.
  The sample interval is not reported, so it stays unknown.
  A child answer that came back as a stored result (a handle, because it
  was large) is read back from the result store for the caller's tenant.

A VM with no samples gets ``None`` and a reason: ``powered_off``,
``not_measured``, ``no_permission`` or ``collection_failed``. Never zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import httpx
import structlog

from meho_backplane.connectors.migration.contracts import MissingReason, Stat, Utilization
from meho_backplane.connectors.result_handle_store import (
    ResultHandleStore,
    ResultRecordKindError,
    get_result_handle_store,
)
from meho_backplane.connectors.vmware_rest.composites._migration_inventory_map import (
    as_dict,
    nearest_rank_stat,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    retrieve_properties_body,
    unwrap_vim_value,
    vim_moref,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations.composite import DispatchChild

__all__ = [
    "VCENTER_SOURCE",
    "VROPS_CHILD_OP_IDS",
    "VROPS_CONNECTOR_ID",
    "VROPS_SOURCE",
    "UsageResult",
    "VcenterPerf",
    "VmIdentity",
    "VropsUsage",
    "interval_for_days",
]

_log = structlog.get_logger(__name__)

VCENTER_SOURCE: Final = "vcenter-performance"
VROPS_SOURCE: Final = "vcf-operations-latest"
VROPS_CONNECTOR_ID: Final = "vrops-rest-9.0"
_VROPS_RESOURCE_QUERY: Final = "vrops.resource.query"
_VROPS_RESOURCE_STATS: Final = "vrops.resource.stats"
#: The child operations this source runs, declared for the start-up check.
VROPS_CHILD_OP_IDS: Final[tuple[str, ...]] = (_VROPS_RESOURCE_QUERY, _VROPS_RESOURCE_STATS)

_RETRIEVE_PROPERTIES_PATH: Final = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_PERF_COUNTER_PROPERTY: Final = "perfCounter"

#: vCenter counter name -> (Utilization field, factor to the field's unit).
#: CPU and memory come in 1/100 percent, network in KB/s.
_VCENTER_COUNTERS: Final[dict[str, tuple[str, float]]] = {
    "cpu.usage.average": ("cpu_percent", 0.01),
    "mem.usage.average": ("memory_percent", 0.01),
    "net.received.average": ("network_in_mb_per_s", 1 / 1024),
    "net.transmitted.average": ("network_out_mb_per_s", 1 / 1024),
}
#: VCF Operations stat key -> (Utilization field, factor to the field's unit).
_VROPS_STATS: Final[dict[str, tuple[str, float]]] = {
    "cpu|usage_average": ("cpu_percent", 1.0),
    "mem|usage_average": ("memory_percent", 1.0),
    "net|received_average": ("network_in_mb_per_s", 1 / 1024),
    "net|transmitted_average": ("network_out_mb_per_s", 1 / 1024),
}
#: (most days, sample interval in seconds): vCenter's historical intervals.
_INTERVALS: Final[tuple[tuple[int, int], ...]] = ((1, 300), (7, 1800), (30, 7200))
_YEAR_INTERVAL: Final = 86400


def interval_for_days(days: int) -> int:
    """The vCenter sample interval (seconds) that covers *days* of history."""
    for most_days, seconds in _INTERVALS:
        if days <= most_days:
            return seconds
    return _YEAR_INTERVAL


@dataclass
class UsageResult:
    """Utilization per VM moid, and the reason for each VM without it."""

    by_vm: dict[str, Utilization] = field(default_factory=dict)
    reasons: dict[str, MissingReason] = field(default_factory=dict)


def _no_data_reason(power_state: str | None) -> MissingReason:
    return "powered_off" if power_state == "poweredOff" else "not_measured"


def _utilization(
    series: Mapping[str, list[float]],
    *,
    start: datetime,
    end: datetime,
    samples: int,
    source: str,
    interval_minutes: float | None = None,
) -> Utilization | None:
    stats: dict[str, Stat | None] = {
        name: nearest_rank_stat(values) for name, values in series.items()
    }
    if samples < 1 or all(stat is None for stat in stats.values()):
        return None
    return Utilization(
        window_start=start,
        window_end=end,
        samples=samples,
        sample_interval_minutes=interval_minutes,
        source=source,
        **stats,
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class VcenterPerf:
    """Reads vCenter's performance history for batches of VMs."""

    def __init__(
        self,
        connector: VmwareRestConnector,
        target: Any,
        operator: Operator,
        *,
        perf_manager: str,
        days: int,
        now: datetime,
    ) -> None:
        self._connector = connector
        self._target = target
        self._operator = operator
        self._perf_manager = perf_manager
        self._interval = interval_for_days(days)
        self._start = now - timedelta(days=days)
        self._end = now
        self._counters: dict[int, tuple[str, float]] | None = None
        self.problem: str | None = None

    async def _load_counters(self) -> dict[int, tuple[str, float]]:
        if self._counters is not None:
            return self._counters
        raw = await self._connector._post_vmomi_json(
            self._target,
            _RETRIEVE_PROPERTIES_PATH,
            operator=self._operator,
            json=retrieve_properties_body(
                "PerformanceManager", [self._perf_manager], [_PERF_COUNTER_PROPERTY]
            ),
        )
        counters: dict[int, tuple[str, float]] = {}
        for info in _perf_counter_infos(raw):
            name = _counter_name(info)
            key = info.get("key")
            if name in _VCENTER_COUNTERS and isinstance(key, int):
                counters[key] = _VCENTER_COUNTERS[name]
        self._counters = counters
        return counters

    async def read(self, vms: Mapping[str, str | None]) -> UsageResult:
        """Utilization for *vms* (``{moid: runtime.powerState}``); never raises a vCenter error."""
        result = UsageResult()
        if not vms:
            return result
        try:
            counters = await self._load_counters()
            if not counters:
                raise RuntimeError("vCenter lists none of the CPU, memory or network counters")
            raw = await self._connector._post_vmomi_json(
                self._target,
                f"/PerformanceManager/{self._perf_manager}/QueryPerf",
                operator=self._operator,
                json={"querySpec": [self._spec(moid, counters) for moid in vms]},
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            self.problem = f"{type(exc).__name__} while reading vCenter performance data"
            _log.info("migration_inventory_perf_failed", error=type(exc).__name__, vms=len(vms))
            result.reasons = dict.fromkeys(vms, "collection_failed")
            return result
        metrics = _entity_metrics(raw)
        for moid, power_state in vms.items():
            usage = self._usage(metrics.get(moid), counters)
            if usage is None:
                result.reasons[moid] = _no_data_reason(power_state)
            else:
                result.by_vm[moid] = usage
        return result

    def _spec(self, moid: str, counters: Mapping[int, tuple[str, float]]) -> dict[str, Any]:
        return {
            VIM_TYPE_NAME_KEY: "PerfQuerySpec",
            "entity": vim_moref("VirtualMachine", moid),
            "startTime": _iso(self._start),
            "endTime": _iso(self._end),
            "intervalId": self._interval,
            "metricId": [
                {VIM_TYPE_NAME_KEY: "PerfMetricId", "counterId": key, "instance": ""}
                for key in counters
            ],
        }

    def _usage(
        self, metric: Mapping[str, Any] | None, counters: Mapping[int, tuple[str, float]]
    ) -> Utilization | None:
        if metric is None:
            return None
        sample_info = metric.get("sampleInfo")
        samples = len(sample_info) if isinstance(sample_info, list) else 0
        series: dict[str, list[float]] = {name: [] for name, _ in _VCENTER_COUNTERS.values()}
        for entry in metric.get("value") or []:
            if not isinstance(entry, dict):
                continue
            ident = as_dict(entry.get("id"))
            counter = ident.get("counterId")
            if ident.get("instance") not in ("", None) or not isinstance(counter, int):
                continue
            if counter not in counters:
                continue
            name, factor = counters[counter]
            # vCenter writes -1 for a sample it does not have.
            series[name] = [v * factor for v in _numbers(entry.get("value")) if v >= 0]
        return _utilization(
            series,
            start=self._start,
            end=self._end,
            samples=samples,
            source=VCENTER_SOURCE,
            interval_minutes=self._interval / 60,
        )


def _numbers(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []
    return [float(v) for v in values if isinstance(v, int | float) and not isinstance(v, bool)]


def _perf_counter_infos(raw: Any) -> Iterable[dict[str, Any]]:
    payload = unwrap_vim_value(raw)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    for obj in objects if isinstance(objects, list) else []:
        for prop in (obj.get("propSet") or []) if isinstance(obj, dict) else []:
            if isinstance(prop, dict) and prop.get("name") == _PERF_COUNTER_PROPERTY:
                values = unwrap_vim_value(prop.get("val"))
                yield from (
                    (v for v in values if isinstance(v, dict)) if isinstance(values, list) else ()
                )


def _counter_name(info: Mapping[str, Any]) -> str | None:
    group = as_dict(info.get("groupInfo"))
    name = as_dict(info.get("nameInfo"))
    rollup = info.get("rollupType")
    parts = [group.get("key"), name.get("key"), rollup]
    names = [part for part in parts if isinstance(part, str) and part]
    return ".".join(names) if len(names) == len(parts) else None


def _entity_metrics(raw: Any) -> dict[str, dict[str, Any]]:
    payload = unwrap_vim_value(raw)
    if isinstance(payload, dict) and isinstance(payload.get("returnval"), list):
        payload = payload["returnval"]
    found: dict[str, dict[str, Any]] = {}
    for metric in payload if isinstance(payload, list) else []:
        if not isinstance(metric, dict):
            continue
        entity = metric.get("entity")
        moid = entity.get("value") if isinstance(entity, dict) else None
        if isinstance(moid, str):
            found[moid] = metric
    return found


@dataclass(frozen=True)
class VmIdentity:
    """What VCF Operations needs to find one VM."""

    moid: str
    name: str | None
    instance_uuid: str | None
    power_state: str | None


class VropsUsage:
    """Reads the latest utilization of VMs from VCF Operations, as child operations.

    The first answer that is ``denied`` or waits for approval stops the
    source for the rest of the run: a missing permission does not change from
    one VM to the next, and each further call would only add a refusal.
    """

    def __init__(
        self,
        dispatch_child: DispatchChild,
        vrops_target: Any,
        *,
        operator: Operator,
        vcenter_uuid: str | None,
        store: ResultHandleStore | None = None,
    ) -> None:
        self._dispatch_child = dispatch_child
        self._target = vrops_target
        self._operator = operator
        self._store = store
        self._vcenter_uuid = (vcenter_uuid or "").lower() or None
        self.stopped: MissingReason | None = None
        self.problem: str | None = None

    async def read(self, vms: Iterable[VmIdentity]) -> UsageResult:
        result = UsageResult()
        for vm in vms:
            if self.stopped is not None:
                result.reasons[vm.moid] = self.stopped
                continue
            usage, reason = await self._one(vm)
            if usage is not None:
                result.by_vm[vm.moid] = usage
            else:
                result.reasons[vm.moid] = reason
        return result

    async def _call(
        self, op_id: str, params: dict[str, Any], *, collection: str
    ) -> dict[str, Any] | MissingReason:
        """The child's answer, or the reason there is none.

        *collection* names the list the answer carries. When the answer was
        large, the child came back as a stored result and the list is read
        back from the result store under that name.
        """
        child: OperationResult = await self._dispatch_child(
            connector_id=VROPS_CONNECTOR_ID, op_id=op_id, params=params, target=self._target
        )
        if child.status in ("denied", "pending"):
            self.stopped = "no_permission"
            self.problem = f"{op_id} was {child.status} on the VCF Operations target"
            return "no_permission"
        if child.status != "ok":
            return "collection_failed"
        if child.handle is not None:
            stored = await self._stored(child.handle.handle_id, collection)
            if stored is None:
                return "collection_failed"
            return {collection: stored}
        if not isinstance(child.result, dict):
            return "collection_failed"
        return child.result

    async def _stored(self, handle_id: UUID, collection: str) -> list[Any] | None:
        """The *collection* list of a stored child result; ``None`` when it cannot be read."""
        store = self._store if self._store is not None else get_result_handle_store()
        operator = self._operator
        try:
            rows = await store.fetch_rows(
                tenant_id=operator.tenant_id, operator_sub=operator.sub, handle_id=handle_id
            )
        except ResultRecordKindError:
            document = await store.fetch_document(
                tenant_id=operator.tenant_id, operator_sub=operator.sub, handle_id=handle_id
            )
            value = as_dict(document.document).get(collection) if document is not None else None
            return value if isinstance(value, list) else None
        if rows is None:
            _log.info("migration_inventory_vrops_result_missing", handle_id=str(handle_id))
            return None
        return rows.rows

    async def _one(self, vm: VmIdentity) -> tuple[Utilization | None, MissingReason]:
        if vm.name is None:
            return None, "not_measured"
        found = await self._call(
            _VROPS_RESOURCE_QUERY,
            {"adapterKind": ["VMWARE"], "resourceKind": ["VirtualMachine"], "name": [vm.name]},
            collection="resourceList",
        )
        if isinstance(found, str):
            return None, found
        resource_id = self._match(found, vm)
        if resource_id is None:
            return None, "not_measured"
        stats = await self._call(
            _VROPS_RESOURCE_STATS,
            {"resourceId": [resource_id], "statKey": list(_VROPS_STATS)},
            collection="values",
        )
        if isinstance(stats, str):
            return None, stats
        usage = _vrops_utilization(stats)
        return usage, _no_data_reason(vm.power_state)

    def _match(self, found: Mapping[str, Any], vm: VmIdentity) -> str | None:
        """The resource id whose vCenter identifiers name this VM, else ``None``."""
        resources = found.get("resourceList")
        for resource in resources if isinstance(resources, list) else []:
            if not isinstance(resource, dict) or not isinstance(resource.get("identifier"), str):
                continue
            ids = _resource_identifiers(resource)
            instance = ids.get("VMEntityInstanceUUID")
            if instance and vm.instance_uuid and instance.lower() == vm.instance_uuid.lower():
                return str(resource["identifier"])
            vcenter = ids.get("VMEntityVCID")
            same_vcenter = vcenter is None or vcenter.lower() == self._vcenter_uuid
            if ids.get("VMEntityObjectID") == vm.moid and same_vcenter:
                return str(resource["identifier"])
        return None


def _resource_identifiers(resource: Mapping[str, Any]) -> dict[str, str]:
    key = as_dict(resource.get("resourceKey"))
    found: dict[str, str] = {}
    for entry in key.get("resourceIdentifiers") or []:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("identifierType")
        name = kind.get("name") if isinstance(kind, dict) else None
        value = entry.get("value")
        if isinstance(name, str) and isinstance(value, str) and value:
            found[name] = value
    return found


def _vrops_utilization(stats: Mapping[str, Any]) -> Utilization | None:
    series: dict[str, list[float]] = {name: [] for name, _ in _VROPS_STATS.values()}
    stamps: list[int] = []
    samples = 0
    values = stats.get("values")
    for entry in values if isinstance(values, list) else []:
        stat_list = entry.get("stat-list") if isinstance(entry, dict) else None
        for stat in (stat_list.get("stat") or []) if isinstance(stat_list, dict) else []:
            if not isinstance(stat, dict):
                continue
            key = (
                stat.get("statKey", {}).get("key")
                if isinstance(stat.get("statKey"), dict)
                else None
            )
            if key not in _VROPS_STATS:
                continue
            name, factor = _VROPS_STATS[key]
            data = [v * factor for v in _numbers(stat.get("data")) if v >= 0]
            series[name] = data
            samples = max(samples, len(data))
            stamps.extend(int(t) for t in _numbers(stat.get("timestamps")))
    if not stamps:
        return None
    start = datetime.fromtimestamp(min(stamps) / 1000, tz=UTC)
    end = datetime.fromtimestamp(max(stamps) / 1000, tz=UTC)
    return _utilization(series, start=start, end=end, samples=samples, source=VROPS_SOURCE)
