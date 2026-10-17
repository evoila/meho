# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Payload profiles and the reduce step for the capacity benchmark (#3642).

Each profile builds rows shaped like one real vendor list response. The
builders depend only on the row index, so the same call always returns the
same payload. :func:`reduce_into_store` runs a payload through the
production reducer into a given store. Kept apart from
:mod:`scripts.result_handle_capacity_benchmark` so that module stays small.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.jsonflux.query.result_catalog import AdmissionGuard
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer

__all__ = ["PROFILES", "PayloadProfile", "reduce_into_store"]

#: A work guard wide enough that the benchmark never refuses a payload.
_OPEN_GUARD = AdmissionGuard(max_decoded_bytes=2**40, max_depth=10_000, max_nodes=2**40)


def _vm_row(i: int) -> dict[str, Any]:
    return {
        "vm": f"vm-{1000 + i}",
        "name": f"app-{i:05d}-{('web', 'db', 'cache', 'batch')[i % 4]}",
        "power_state": ("POWERED_ON", "POWERED_OFF", "SUSPENDED")[i % 3],
        "cpu_count": 2 + i % 7,
        "memory_size_MiB": 2048 * (1 + i % 8),
        "guest_OS": ("UBUNTU_64", "RHEL_9_64", "WINDOWS_2022_SERVER")[i % 3],
        "host": f"host-{i % 48}",
        "resource_pool": f"resgroup-{i % 12}",
        "tools_running_status": "guestToolsRunning" if i % 5 else "guestToolsNotRunning",
        "ip_address": f"10.{i // 65536 % 256}.{i // 256 % 256}.{i % 256}",
        "annotation": f"owner=team-{i % 30}; backup=daily; ticket=CHG{100000 + i}",
    }


def _pod_row(i: int) -> dict[str, Any]:
    labels = {"app": f"svc-{i % 40}", "tier": ("web", "api", "worker")[i % 3], "rev": str(i % 9)}
    container = {
        "name": f"svc-{i % 40}",
        "image": f"registry.example/team/svc-{i % 40}:1.{i % 30}.{i % 7}",
        "ports": [{"containerPort": 8080, "protocol": "TCP"}],
        "resources": {"limits": {"cpu": "500m", "memory": "512Mi"}},
        "env": [{"name": f"FEATURE_{n}", "value": "on"} for n in range(4)],
    }
    return {
        "metadata": {
            "name": f"svc-{i % 40}-{i:06x}",
            "namespace": f"ns-{i % 16}",
            "uid": f"{i:08x}-0000-4000-8000-{i:012x}",
            "labels": labels,
            "ownerReferences": [{"kind": "ReplicaSet", "name": f"svc-{i % 40}-rs"}],
        },
        "spec": {"nodeName": f"node-{i % 24}", "containers": [container]},
        "status": {
            "phase": "Running" if i % 11 else "Pending",
            "podIP": f"10.244.{i // 256 % 256}.{i % 256}",
            "conditions": [{"type": t, "status": "True"} for t in ("Ready", "Initialized")],
            "containerStatuses": [{"name": container["name"], "restartCount": i % 4}],
        },
    }


def _segment_row(i: int) -> dict[str, Any]:
    return {
        "id": f"seg-{i:05d}",
        "display_name": f"tenant-{i % 50}-net-{i}",
        "path": f"/infra/segments/seg-{i:05d}",
        "type": "ROUTED" if i % 2 else "EXTENDED",
        "transport_zone_path": f"/infra/sites/default/enforcement-points/default/tz-{i % 4}",
        "connectivity_path": f"/infra/tier-1s/t1-{i % 20}",
        "subnets": [{"gateway_address": f"172.{16 + i % 16}.{i % 256}.1/24", "network": "x"}],
        "advanced_config": {"address_pool_paths": [], "hybrid": False, "local_egress": False},
        "tags": [{"scope": "tenant", "tag": f"t-{i % 50}"}],
        "_revision": i % 13,
    }


def _metric_row(i: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "identifier": f"{i:08x}-1111-4222-8333-{i:012x}",
        "resourceKind": ("VirtualMachine", "HostSystem", "Datastore")[i % 3],
        "name": f"resource-{i:05d}",
    }
    for n in range(120):
        row[f"stat_{n:03d}"] = round((i * 31 + n * 17) % 10_000 / 7.0, 3)
    return row


def _log_row(i: int) -> dict[str, Any]:
    return {
        "timestamp": f"2026-10-01T12:{i // 60 % 60:02d}:{i % 60:02d}.{i % 1000:03d}Z",
        "level": ("info", "warn", "error")[i % 3],
        "source": f"host-{i % 32}",
        "message": f'Bestätigung für Benutzer „müller-{i}“ fehlgeschlagen — 認証エラー "{i}"\\n',
    }


@dataclass(frozen=True, slots=True)
class PayloadProfile:
    """One representative vendor list response shape."""

    name: str
    description: str
    row: Callable[[int], dict[str, Any]]
    collection_key: str

    def payload(self, rows: int) -> dict[str, Any]:
        """Return the vendor response with *rows* rows under its list key."""
        return {self.collection_key: [self.row(i) for i in range(rows)]}


PROFILES: tuple[PayloadProfile, ...] = (
    PayloadProfile("vm-inventory", "vCenter VM list with summary fields", _vm_row, "value"),
    PayloadProfile("k8s-pods", "Kubernetes pod list (nested spec and status)", _pod_row, "items"),
    PayloadProfile("nsx-segments", "NSX policy segment list", _segment_row, "results"),
    PayloadProfile(
        "metric-stats", "Operations-style resource with 120 stats", _metric_row, "values"
    ),
    PayloadProfile("log-lines", "Log lines with non-ASCII text and quotes", _log_row, "logs"),
)


async def reduce_into_store(
    profile: PayloadProfile,
    rows: int,
    store: ResultHandleStore,
    *,
    max_spill_rows: int,
    sample_byte_budget: int,
) -> tuple[Any, Any]:
    """Reduce *profile*'s payload with the production reducer into *store*.

    Returns ``(summary, handle)`` as the reducer returns them. The work
    guard is wide open, so every payload is stored (up to the store's caps).
    """
    reducer = JsonFluxReducer(
        store=store,
        max_spill_rows=max_spill_rows,
        sample_byte_budget=sample_byte_budget,
        admission_guard=_OPEN_GUARD,
    )
    context = {"tenant_id": str(uuid4()), "operator_sub": "benchmark", "op_id": profile.name}
    return await reducer.reduce(profile.payload(rows), None, context=context)
