# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.host.datastores`` typed read op (#3373).

Lists the datastores mounted on one ESXi host: per datastore its moid,
``name``, ``type`` (``VMFS`` / ``NFS`` / ``vsan`` ...), capacity, free
space and whether **this host** can reach it. Works on a vCenter target and on a
**standalone ESXi** target, so a mount made by
``vmware.composite.host.datastore_mount_nfs`` can be checked by a separate
read, not only by the mount op's own ``result_status``.

The other datastore reads (the ingested ``GET:/vcenter/datastore`` and
``vmware.composite.datastore.usage``) use the vCenter Automation REST
surface, which a standalone ESXi host does not serve. This op reads over
the vmomi seam (:meth:`VmwareRestConnector._post_vmomi_json`: VI-JSON on
vCenter, SOAP ``/sdk`` on ESXi), the same seam ``vmware.host.storage_devices``
uses:

1. ``HostSystem.datastore`` -> the host's ``Datastore`` MoRefs. When the
   collector could not read that property (a ``missingSet`` entry, for
   example no permission), the read fails with ``datastores_unreadable``
   instead of answering ``ok`` with no datastores; a host that truly has no
   datastore sends an empty array and no ``missingSet`` entry.
2. ``Datastore.summary.{name,type,capacity,freeSpace}`` plus
   ``Datastore.host`` (the per-host mount list) over those moids, mapped
   **per object** (one row per datastore). This read goes through
   :func:`~meho_backplane.connectors.vmware_rest.vim_retrieve.retrieve_all_pages`,
   so a split answer is read whole and a lost page fails the read.

``accessible`` is the selected host's ``mountInfo.accessible`` from
``Datastore.host``, not ``summary.accessible``: on vCenter the summary flag
is true when *any* host reaches the datastore, which says nothing about
the host that was asked for.

Host resolution branches on the probe fingerprint
(:func:`~meho_backplane.connectors.vmware_rest.host_target.classify_host_target`):
standalone ESXi -> the well-known ``ha-host`` (``host`` ignored); vCenter
-> the ``host`` param (name, then moid) via ``GET:/vcenter/host``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.host_target import (
    HOST_FLAVOR_ESXI,
    STANDALONE_ESXI_HOST_MOID,
    classify_host_target,
)
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.typed_ops_host_storage_devices import (
    _int_or_none,
    _list_host_moids,
    _moref_value,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    fault_type_name,
    retrieve_properties_body,
    unwrap_vim_value,
)
from meho_backplane.connectors.vmware_rest.vim_retrieve import retrieve_all_pages

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "HOST_DATASTORES_GROUP_KEY",
    "HOST_DATASTORES_WHEN_TO_USE",
    "VMWARE_HOST_DATASTORES_OP",
    "build_datastore_summary_retrieve_params",
    "build_host_datastores_retrieve_params",
    "host_datastores_impl",
]

_log = structlog.get_logger(__name__)

_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_HOST_SYSTEM_MO_TYPE = "HostSystem"
_DATASTORE_MO_TYPE = "Datastore"
_PROP_HOST_DATASTORE = "datastore"
_PROP_NAME = "summary.name"
_PROP_TYPE = "summary.type"
_PROP_CAPACITY = "summary.capacity"
_PROP_FREE_SPACE = "summary.freeSpace"
_PROP_HOST_MOUNTS = "host"
_SUMMARY_PATH_SET = (_PROP_NAME, _PROP_TYPE, _PROP_CAPACITY, _PROP_FREE_SPACE, _PROP_HOST_MOUNTS)

HOST_DATASTORES_GROUP_KEY = "vmware-host-datastores"


def _empty(status: str, host: str | None, **extra: Any) -> dict[str, Any]:
    """A non-``ok`` envelope: *status*, the *host* input, an empty datastore set."""
    return {"status": status, "host": host, "datastores": [], "datastore_count": 0, **extra}


async def _resolve_host_moid(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    host: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve the target's host to a ``HostSystem`` moid, or a refusal envelope.

    Same branches as ``vmware.host.storage_devices``, with refusals in this
    op's ``datastores`` / ``datastore_count`` shape.
    """
    flavor, refusal = classify_host_target(target)
    if refusal is not None:
        return None, {**refusal, "host": host, "datastores": [], "datastore_count": 0}
    if flavor == HOST_FLAVOR_ESXI:
        return STANDALONE_ESXI_HOST_MOID, None
    if not host:
        return None, _empty(
            "host_required",
            host,
            guidance="a vCenter target requires a host display name or moref",
        )
    by_name = await _list_host_moids(connector, operator, target, {"filter.names": [host]})
    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        return None, _empty("ambiguous_host", host, candidate_hosts=by_name)
    by_moref = await _list_host_moids(connector, operator, target, {"filter.hosts": [host]})
    if len(by_moref) == 1:
        return by_moref[0], None
    return None, _empty("host_not_found", host)


def build_host_datastores_retrieve_params(host_moid: str) -> dict[str, Any]:
    """RetrievePropertiesEx body for ``HostSystem.datastore`` on one host."""
    return retrieve_properties_body(_HOST_SYSTEM_MO_TYPE, [host_moid], (_PROP_HOST_DATASTORE,))


def build_datastore_summary_retrieve_params(datastore_moids: list[str]) -> dict[str, Any]:
    """RetrievePropertiesEx body for ``summary.*`` + ``host`` over *datastore_moids*."""
    return retrieve_properties_body(_DATASTORE_MO_TYPE, datastore_moids, _SUMMARY_PATH_SET)


def _result_objects(retrieve_result: Any) -> list[dict[str, Any]]:
    """The ``objects`` list of a ``RetrievePropertiesEx`` result (dict rows only)."""
    payload = _unwrap_value(retrieve_result)
    if isinstance(payload, dict):
        objects = payload.get("objects", [])
    elif isinstance(payload, list):
        objects = payload
    else:
        objects = []
    return [obj for obj in objects or [] if isinstance(obj, dict)]


def _object_props(obj: dict[str, Any]) -> dict[str, Any]:
    """``{name: val}`` for one result object (val un-boxed)."""
    props: dict[str, Any] = {}
    for prop in obj.get("propSet", []) or []:
        if isinstance(prop, dict) and isinstance(prop.get("name"), str):
            props[prop["name"]] = unwrap_vim_value(prop.get("val"))
    return props


def _host_datastore_moids(retrieve_result: Any) -> list[str]:
    """The ``Datastore`` moids from a single-host ``HostSystem.datastore`` read."""
    moids: list[str] = []
    for obj in _result_objects(retrieve_result):
        refs = _object_props(obj).get(_PROP_HOST_DATASTORE)
        if isinstance(refs, dict):
            refs = [refs]
        if not isinstance(refs, list):
            continue
        for ref in refs:
            moid = _moref_value(ref)
            if moid is not None and moid not in moids:
                moids.append(moid)
    return moids


def _host_datastore_read_fault(retrieve_result: Any) -> str | None:
    """The fault type name when ``HostSystem.datastore`` landed in ``missingSet``.

    The HTTP call succeeds while the collector still could not read the
    property (no permission, a system error): the property is then absent
    from ``propSet`` and named in the object's ``missingSet`` with a fault.
    Only the fault class name is kept, never its text, which can echo a
    private identifier. ``None`` when the property was read, also when it
    is an empty array.
    """
    for obj in _result_objects(retrieve_result):
        for entry in obj.get("missingSet") or []:
            if isinstance(entry, dict) and entry.get("path") == _PROP_HOST_DATASTORE:
                return fault_type_name(entry.get("fault")) or "unknown fault"
    return None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _host_mount_accessible(mounts: Any, host_moid: str) -> bool | None:
    """``mountInfo.accessible`` of the *host_moid* entry in ``Datastore.host``.

    ``None`` when the datastore has no mount entry for that host or the flag
    is missing / not a bool.
    """
    if isinstance(mounts, dict):
        mounts = [mounts]
    if not isinstance(mounts, list):
        return None
    for mount in mounts:
        if not isinstance(mount, dict) or _moref_value(mount.get("key")) != host_moid:
            continue
        info = mount.get("mountInfo")
        return _bool_or_none(info.get("accessible")) if isinstance(info, dict) else None
    return None


def _map_datastore_summaries(retrieve_result: Any, host_moid: str) -> dict[str, dict[str, Any]]:
    """Map a multi-datastore ``summary`` + ``host`` read to one row per datastore moid.

    ``accessible`` is taken from *host_moid*'s own mount entry.
    """
    rows: dict[str, dict[str, Any]] = {}
    for obj in _result_objects(retrieve_result):
        moid = _moref_value(obj.get("obj"))
        if moid is None:
            continue
        props = _object_props(obj)
        rows[moid] = {
            "datastore": moid,
            "name": _str_or_none(props.get(_PROP_NAME)),
            "type": _str_or_none(props.get(_PROP_TYPE)),
            "capacity_bytes": _int_or_none(props.get(_PROP_CAPACITY)),
            "free_space_bytes": _int_or_none(props.get(_PROP_FREE_SPACE)),
            "accessible": _host_mount_accessible(props.get(_PROP_HOST_MOUNTS), host_moid),
        }
    return rows


def _unreadable(host_moid: str, step: str, exc: Exception) -> dict[str, Any]:
    return _empty(
        "datastores_unreadable",
        host_moid,
        read_note=(
            f"datastore read failed: {step} for host {host_moid!r} failed with "
            f"{type(exc).__name__}: {exc}"
        ),
    )


async def host_datastores_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.datastores`` -- the datastores mounted on one host.

    Resolves the host, reads ``HostSystem.datastore``, then the summary and
    mount list of each datastore (every page). Fail-closed: a failed read,
    including a lost continuation page or a ``HostSystem.datastore`` the
    collector could not read (``missingSet``), returns
    ``datastores_unreadable`` with an empty set.
    """
    host_moid, refusal = await _resolve_host_moid(connector, operator, target, params.get("host"))
    if refusal is not None:
        return refusal
    assert host_moid is not None  # refusal is None => the host resolved (mypy narrowing).
    try:
        host_result = await connector._post_vmomi_json(
            target,
            _RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=build_host_datastores_retrieve_params(host_moid),
            promote_managed_object_not_found=True,
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        return _unreadable(host_moid, "HostSystem.datastore read", exc)
    fault = _host_datastore_read_fault(host_result)
    if fault is not None:
        return _empty(
            "datastores_unreadable",
            host_moid,
            read_note=(
                f"datastore read failed: HostSystem.datastore for host {host_moid!r} "
                f"could not be read ({fault})"
            ),
        )
    moids = _host_datastore_moids(host_result)
    summaries: dict[str, dict[str, Any]] = {}
    if moids:
        try:
            summary_result = await retrieve_all_pages(
                connector, target, operator, build_datastore_summary_retrieve_params(moids)
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            return _unreadable(host_moid, "Datastore.summary read", exc)
        summaries = _map_datastore_summaries(summary_result, host_moid)
    # One row per mounted datastore, in host order; a datastore the summary
    # read did not return keeps its moid with null fields.
    datastores = [
        summaries.get(moid)
        or {
            "datastore": moid,
            "name": None,
            "type": None,
            "capacity_bytes": None,
            "free_space_bytes": None,
            "accessible": None,
        }
        for moid in moids
    ]
    _log.info(
        "vmware_host_datastores_read",
        target=target.name,
        host=host_moid,
        datastore_count=len(datastores),
    )
    return {
        "status": "ok",
        "host": host_moid,
        "datastores": datastores,
        "datastore_count": len(datastores),
    }


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Host display name or moref (e.g. 'esxi-01' / 'host-15'). "
                "Resolved via GET:/vcenter/host (name first, moref fallback; "
                "ambiguous -> status='ambiguous_host'). Required on a vCenter "
                "target (else status='host_required'); optional and ignored on "
                "a standalone-ESXi target (fingerprint product=esxi), whose one "
                "host is the well-known ha-host."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
    "examples": [{"host": "host-21"}],
}

_DATASTORE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "datastore": {
            "type": "string",
            "description": (
                "Datastore moid (e.g. 'datastore-12' on vCenter; on a standalone "
                "ESXi host often the NFS 'server:/export' id or a volume UUID)."
            ),
        },
        "name": {"type": ["string", "null"], "description": "Datastore.summary.name."},
        "type": {
            "type": ["string", "null"],
            "description": "Datastore.summary.type (e.g. 'VMFS', 'NFS', 'NFS41', 'vsan').",
        },
        "capacity_bytes": {
            "type": ["integer", "null"],
            "description": "Datastore.summary.capacity in bytes.",
        },
        "free_space_bytes": {
            "type": ["integer", "null"],
            "description": "Datastore.summary.freeSpace in bytes.",
        },
        "accessible": {
            "type": ["boolean", "null"],
            "description": (
                "Whether this host can reach the datastore: the host's own entry in "
                "Datastore.host (mountInfo.accessible). Null when the datastore has no "
                "mount entry for the host."
            ),
        },
    },
    "required": ["datastore", "name"],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "ok",
                "host_required",
                "host_not_found",
                "ambiguous_host",
                "unsupported_host_target",
                "datastores_unreadable",
            ],
            "description": (
                "'ok' — datastores listed; the other statuses are host "
                "resolution refusals or a failed read, each with an empty list."
            ),
        },
        "host": {
            "type": ["string", "null"],
            "description": (
                "Resolved host moid (ha-host on standalone ESXi), or the input on refusal."
            ),
        },
        "datastores": {
            "type": "array",
            "items": _DATASTORE_ITEM_SCHEMA,
            "description": (
                "One row per datastore mounted on the host (empty on any non-'ok' status)."
            ),
        },
        "datastore_count": {
            "type": "integer",
            "minimum": 0,
            "description": "Length of ``datastores`` (true count; array may be JSONFlux-reduced).",
        },
        "candidate_hosts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Matched host moids on 'ambiguous_host'.",
        },
        "target_product": {
            "type": ["string", "null"],
            "description": "The fingerprint product on 'unsupported_host_target'.",
        },
        "read_note": {
            "type": "string",
            "description": (
                "On 'datastores_unreadable', the failing read + error (a transport "
                "error, a lost page, or the fault class when vSphere could not read "
                "HostSystem.datastore)."
            ),
        },
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["status", "host", "datastores", "datastore_count"],
}

#: Curated ``when_to_use`` blurb for the host-datastores group.
HOST_DATASTORES_WHEN_TO_USE = (
    "Use to list the datastores mounted on one ESXi host — per datastore its "
    "moid, name, type (VMFS / NFS / vsan), capacity_bytes, free_space_bytes and "
    "whether this host can reach it (accessible, from the host's own mount "
    "entry). The right op for 'which "
    "datastores does this host see?' or 'did the NFS mount work?', including on "
    "a standalone ESXi host no vCenter manages yet (host param ignored), where "
    "the vCenter datastore list and datastore.usage do not work. Read-only."
)

VMWARE_HOST_DATASTORES_OP = VmwareTypedOp(
    op_id="vmware.host.datastores",
    handler_attr="host_datastores",
    summary="Datastores mounted on one ESXi host: name, type, size, free space, reachable.",
    description=(
        "Returns one row per datastore mounted on an ESXi host: moid, name, type, "
        "capacity, free space and accessible (whether this host can reach it). Works "
        "through vCenter (host is required) and on a standalone ESXi host (host is "
        "ignored). Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=HOST_DATASTORES_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "esxi", "host", "storage", "datastore"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to list the datastores one ESXi host has mounted, for example to check "
            "that vmware.composite.host.datastore_mount_nfs really mounted the share. "
            "Works on a standalone ESXi host too."
        ),
        "parameter_hints": {
            "host": (
                "Host display name or moid on a vCenter target; omit on a "
                "standalone-ESXi target (the host is the target)."
            ),
        },
        "output_shape": (
            "{status: ok|host_required|host_not_found|ambiguous_host|"
            "unsupported_host_target|datastores_unreadable, host, datastores: [{datastore, "
            "name, type, capacity_bytes, free_space_bytes, accessible}], datastore_count, "
            "candidate_hosts, read_note}. Every status except ok has an empty datastores list."
        ),
        "next_step": (
            "To check a mount, look for the row whose name is the datastore name you "
            "mounted and check accessible is true. On ambiguous_host, call again with "
            "one of candidate_hosts."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_HOST_DATASTORES_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {HOST_DATASTORES_GROUP_KEY: HOST_DATASTORES_WHEN_TO_USE}
