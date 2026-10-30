# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.datacenter.list`` typed op -- the datacenters of one vCenter.

Reads the vSphere Automation REST API on the connector's own session, so it
works on a fresh install with no catalog ingest:

1. ``GET /vcenter/datacenter`` lists every datacenter (moid and name). This
   call must work; a failure fails the operation.
2. For each datacenter that matches the optional ``name`` pattern, three
   counts: ``GET /vcenter/cluster``, ``GET /vcenter/host`` and
   ``GET /vcenter/datastore``, each with the ``datacenters`` filter. A count
   that fails is ``null`` and ``read_note`` says why; the other rows and counts
   stay.

The rows are flat, so a long list rides a result handle like any other list.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "DATACENTER_COUNT_PATHS",
    "DATACENTER_FILTER_PARAM",
    "DATACENTER_LIST_GROUP_KEY",
    "DATACENTER_LIST_PATH",
    "DATACENTER_LIST_WHEN_TO_USE",
    "VMWARE_DATACENTER_LIST_OP",
    "datacenter_list_impl",
]

#: Spec-relative REST paths; ``mount_op_path`` puts them on ``/api`` or ``/rest``.
DATACENTER_LIST_PATH = "/vcenter/datacenter"

#: Row field -> the REST list it counts, filtered to one datacenter.
DATACENTER_COUNT_PATHS: dict[str, str] = {
    "cluster_count": "/vcenter/cluster",
    "host_count": "/vcenter/host",
    "datastore_count": "/vcenter/datastore",
}

#: The FilterSpec field the three count calls use. Written in the legacy
#: ``filter.`` form; ``adapt_op_query`` drops the prefix on ``/api``.
DATACENTER_FILTER_PARAM = "datacenters"

DATACENTER_LIST_GROUP_KEY = "vmware-datacenters"

DATACENTER_LIST_WHEN_TO_USE = (
    "Use to see the datacenters of a vCenter: their names and ids, and how many "
    "clusters, hosts and datastores each one holds. Start here when you need a "
    "datacenter id for another call, or a first overview of a vCenter. Read-only."
)


def _log() -> Any:
    """Get the logger per call, so ``structlog.testing.capture_logs`` sees every event."""
    return structlog.get_logger(__name__)


def _name_matches(name: Any, pattern: str | None) -> bool:
    """Match *name* against a ``*`` / ``?`` pattern; letter case does not matter."""
    if pattern is None:
        return True
    if not isinstance(name, str):
        return False
    return fnmatch.fnmatchcase(name.casefold(), pattern.casefold())


async def _get_list(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    path: str,
    query: dict[str, Any] | None,
) -> list[Any]:
    """GET one REST list on the target's mount; raise when the answer is not a list."""
    mounted = await connector.mount_op_path(target, path, operator)
    wire_query = await connector.adapt_op_query(target, query, operator)
    answer = _unwrap_value(
        await connector._get_json(target, mounted, operator=operator, params=wire_query)
    )
    if not isinstance(answer, list):
        raise RuntimeError(
            f"vmware.datacenter.list: expected a list from GET {path!r}, "
            f"got {type(answer).__name__}"
        )
    return answer


async def _datacenter_row(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    moid: str,
    name: Any,
) -> dict[str, Any]:
    """One row: id, name and the three counts. A failed count is ``None`` with a note."""
    row: dict[str, Any] = {"id": moid, "name": name if isinstance(name, str) else None}
    notes: list[str] = []
    query = {f"filter.{DATACENTER_FILTER_PARAM}": [moid]}
    for field, path in DATACENTER_COUNT_PATHS.items():
        try:
            row[field] = len(await _get_list(connector, operator, target, path, query))
        except (httpx.HTTPError, RuntimeError) as exc:
            row[field] = None
            notes.append(f"{field} not read: GET {path} failed with {type(exc).__name__}: {exc}")
    row["read_note"] = "; ".join(notes) if notes else None
    return row


async def datacenter_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.datacenter.list``. Returns ``{"datacenters": [...]}``."""
    raw_pattern = params.get("name")
    pattern = raw_pattern if isinstance(raw_pattern, str) else None
    entries = await _get_list(connector, operator, target, DATACENTER_LIST_PATH, None)
    rows: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        moid = entry.get("datacenter")
        if not isinstance(moid, str) or not _name_matches(entry.get("name"), pattern):
            continue
        rows.append(await _datacenter_row(connector, operator, target, moid, entry.get("name")))
    _log().info(
        "vmware_datacenter_list_read",
        target=target.name,
        datacenter_count=len(rows),
        filtered=pattern is not None,
    )
    return {"datacenters": rows}


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_COUNT_DESCRIPTION = (
    "Number of {what} in this datacenter. Null when the count could not be read; "
    "read_note says why."
)

_DATACENTER_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "Datacenter moid, for example 'datacenter-3'.",
        },
        "name": {"type": ["string", "null"], "description": "Datacenter name."},
        "cluster_count": {
            "type": ["integer", "null"],
            "description": _COUNT_DESCRIPTION.format(what="clusters"),
        },
        "host_count": {
            "type": ["integer", "null"],
            "description": _COUNT_DESCRIPTION.format(
                what="ESXi hosts, in a cluster or standalone,"
            ),
        },
        "datastore_count": {
            "type": ["integer", "null"],
            "description": _COUNT_DESCRIPTION.format(what="datastores"),
        },
        "read_note": {
            "type": ["string", "null"],
            "description": "Why a count is null. Null when every count was read.",
        },
    },
    "required": ["id", "name", "cluster_count", "host_count", "datastore_count", "read_note"],
}

VMWARE_DATACENTER_LIST_OP = VmwareTypedOp(
    op_id="vmware.datacenter.list",
    handler_attr="datacenter_list",
    summary="List the datacenters of a vCenter, with the number of clusters, hosts and datastores.",
    description=(
        "Reads GET /vcenter/datacenter, then counts the clusters, hosts and datastores of "
        "each datacenter. Returns one row per datacenter: id (moid), name and the three "
        "counts. Optional name pattern. vCenter only. Needs no catalog ingest. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": (
                    "Optional. Show only datacenters whose name matches this pattern. "
                    "Use * for any text and ? for one character. Letter case does not "
                    "matter. Omit it to list every datacenter."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{}, {"name": "<datacenter-name>*"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "datacenters": {
                "type": "array",
                "description": "One row per datacenter that matches the name pattern.",
                "items": _DATACENTER_ROW_SCHEMA,
            }
        },
        "required": ["datacenters"],
    },
    group_key=DATACENTER_LIST_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "datacenter", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which datacenters a vCenter has, to get a datacenter id for "
            "another call, or for a first overview: how many clusters, hosts and "
            "datastores each datacenter holds."
        ),
        "parameter_hints": {
            "name": "Omit it for every datacenter, or pass a pattern such as 'prod*'.",
        },
        "output_shape": (
            "{datacenters: [{id, name, cluster_count, host_count, datastore_count, "
            "read_note}]}. id is the moid, for example 'datacenter-3'. A count is null "
            "when its read failed; read_note says why."
        ),
        "next_step": (
            "Use id where another call needs a datacenter moid. For the load of the "
            "hosts, call vmware.host.usage. For the space on the datastores, call "
            "vmware.composite.datastore.usage."
        ),
    },
)
