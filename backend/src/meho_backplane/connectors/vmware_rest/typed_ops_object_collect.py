# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.object.collect`` typed op (#2300).

A **bounded generic** PropertyCollector read: given a managed-object type,
its moid, and a caller-specified list of property paths, return those
properties as typed rows. One typed op absorbs the long tail of one-off
managed-object reads (the adopter audit counted ~18 wrapper mentions)
without adding a hand-coded op per property set.

Bounded, by construction (keeping the #1177 "dumb substrate" line -- no
traversal DSL, no weighting, no arbitrary spec):

* **Single object.** The op reads properties on exactly one
  ``(type, moid)``. It builds a ``PropertyFilterSpec`` with a single
  ``objectSet`` entry and **no** ``TraversalSpec`` / ``ContainerView``,
  so it cannot walk the inventory graph -- the request is structurally
  incapable of an unbounded traversal.
* **Size + shape caps in the schema.** ``properties`` is capped at
  :data:`_MAX_PROPERTIES` paths, each a simple dotted identifier of at
  most :data:`_MAX_PATH_DEPTH` segments (the ``pattern`` rejects
  wildcards, array indices, and pathological depth). An oversized or
  malformed request fails ``parameter_schema`` validation in the
  dispatcher and comes back as a structured ``invalid_params`` error --
  the read is never issued.

It is a ``source_kind="typed"`` bound method on
:class:`VmwareRestConnector` in the
:mod:`~meho_backplane.connectors.vmware_rest.typed_ops` mould: it reads
directly on the connector session (no ``dispatch_child``, no ingested
descriptor), so it works on a fresh boot with **zero catalog ingest**.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "OBJECT_COLLECT_GROUP_KEY",
    "OBJECT_COLLECT_WHEN_TO_USE",
    "VMWARE_OBJECT_COLLECT_OP",
    "build_object_collect_retrieve_params",
    "object_collect_impl",
]

_log = structlog.get_logger(__name__)

# PropertyCollector RetrievePropertiesEx against the singleton
# ``propertyCollector`` moId (carried in the path, so the body is only
# the method arguments). Spec-relative; mounted per-target by
# mount_op_path.
_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"

#: Maximum number of property paths accepted in one request. Caps the
#: read size; enforced declaratively via ``parameter_schema.maxItems``.
_MAX_PROPERTIES = 64
#: Maximum number of dotted segments in a single property path. Caps the
#: path depth; enforced declaratively via the item ``pattern``.
_MAX_PATH_DEPTH = 16

# A property path is a dotted chain of vim identifiers -- no wildcards, no
# array indices, no traversal syntax. ``{0,N}`` bounds the depth at
# ``_MAX_PATH_DEPTH`` segments.
_PROPERTY_PATH_PATTERN = (
    r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*){0," + str(_MAX_PATH_DEPTH - 1) + r"}$"
)
# A managed-object type is a single vim identifier (e.g. 'VirtualMachine',
# 'HostSystem', 'Datastore').
_MO_TYPE_PATTERN = r"^[A-Za-z][A-Za-z0-9]*$"

OBJECT_COLLECT_GROUP_KEY = "vmware-object-collect"


def build_object_collect_retrieve_params(
    mo_type: str, moid: str, properties: list[str]
) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` request body for one object.

    A single ``PropertyFilterSpec`` scoped directly to the ``(mo_type,
    moid)`` object requesting exactly the caller's property paths. No
    ``TraversalSpec`` -- the read cannot leave the named object. The
    singleton ``propertyCollector`` moId rides the request path
    (:data:`_RETRIEVE_PROPERTIES_PATH`), so the body is just the
    ``specSet`` + ``options`` method arguments.
    """
    return {
        "specSet": [
            {
                "propSet": [{"type": mo_type, "pathSet": list(properties)}],
                "objectSet": [{"obj": {"type": mo_type, "value": moid}}],
            }
        ],
        "options": {},
    }


def _extract_object_content(retrieve_result: Any) -> tuple[dict[str, Any], list[str]]:
    """Flatten the first ``ObjectContent`` to (props, missing-paths).

    ``RetrievePropertiesEx`` returns a ``RetrieveResult`` whose ``objects``
    list carries one ``ObjectContent`` per queried object. For the single
    object queried here the first entry's ``propSet`` holds the readable
    ``{name, val}`` pairs and its ``missingSet`` names the paths the
    collector could not read (permission / not-applicable). A bare list
    (legacy ``RetrieveProperties`` shape) is tolerated.
    """
    payload = _unwrap_value(retrieve_result)
    if isinstance(payload, dict):
        objects = payload.get("objects", [])
    elif isinstance(payload, list):
        objects = payload
    else:
        objects = []
    props: dict[str, Any] = {}
    missing: list[str] = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and isinstance(prop.get("name"), str):
                props[prop["name"]] = prop.get("val")
        for miss in obj.get("missingSet", []) or []:
            if isinstance(miss, dict) and isinstance(miss.get("path"), str):
                missing.append(miss["path"])
    return props, missing


async def object_collect_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.object.collect`` -- bounded generic read.

    Reads, directly on the connector session (no ``dispatch_child``, no
    ingested descriptor):

    1. ``POST .../PropertyCollector/propertyCollector/RetrievePropertiesEx``
       requesting the caller's ``properties`` on the single ``(type,
       moid)`` object, mounted on the documented VI-JSON base
       ``/sdk/vim25/{release}`` via
       :meth:`VmwareRestConnector._post_vmomi_json` (single ``/api``
       fallback) so it resolves on vCenter 8.0.x instead of 404ing
       (#2466). Load-bearing: a failure raises and the dispatcher records
       it as a ``connector_error``.

    The size / depth bound is enforced upstream by ``parameter_schema``
    validation in the dispatcher, so by the time this runs ``params`` is
    already within :data:`_MAX_PROPERTIES` / :data:`_MAX_PATH_DEPTH`.

    Returns ``{"type", "moid", "properties": {name: val, ...}, "missing":
    [path, ...]}`` -- ``missing`` names any requested path the collector
    could not read.
    """
    mo_type = params["type"]
    moid = params["moid"]
    properties: list[str] = list(params["properties"])

    result = await connector._post_vmomi_json(
        target,
        _RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=build_object_collect_retrieve_params(mo_type, moid, properties),
    )
    read_props, missing = _extract_object_content(result)
    _log.info(
        "vmware_object_collect_read",
        target=target.name,
        mo_type=mo_type,
        moid=moid,
        requested=len(properties),
        returned=len(read_props),
        missing=len(missing),
    )
    return {"type": mo_type, "moid": moid, "properties": read_props, "missing": missing}


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": _MO_TYPE_PATTERN,
            "description": (
                "Managed-object type name (e.g. 'VirtualMachine', "
                "'HostSystem', 'Datastore', 'ClusterComputeResource')."
            ),
        },
        "moid": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": "Managed-object ID of the object to read (e.g. 'vm-42').",
        },
        "properties": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_PROPERTIES,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "pattern": _PROPERTY_PATH_PATTERN,
            },
            "description": (
                f"Property paths to read (1-{_MAX_PROPERTIES}), each a dotted "
                f"vim identifier chain up to {_MAX_PATH_DEPTH} segments deep "
                "(e.g. 'runtime.powerState'). Wildcards, array indices, and "
                "traversal syntax are rejected -- oversized or malformed "
                "requests fail as a structured invalid_params error."
            ),
        },
    },
    "required": ["type", "moid", "properties"],
    "additionalProperties": False,
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "description": "Managed-object type read."},
        "moid": {"type": "string", "description": "Managed-object ID read."},
        "properties": {
            "type": "object",
            "description": (
                "Readable property paths mapped to their raw VI-JSON values "
                "(shape depends on the property)."
            ),
            "additionalProperties": True,
        },
        "missing": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Requested paths the collector could not read (permission or "
                "not-applicable), from the ObjectContent ``missingSet``."
            ),
        },
    },
    "required": ["type", "moid", "properties", "missing"],
}

#: Curated ``when_to_use`` blurb for the object-collect group.
OBJECT_COLLECT_WHEN_TO_USE = (
    "Use as the bounded generic escape hatch to read arbitrary "
    "PropertyCollector properties off one managed object by (type, moid) "
    "when no purpose-built typed op covers the exact property set -- e.g. "
    "reading a Datastore's summary.freeSpace, a ResourcePool's "
    "runtime.memory, or any vim25 property path. Bounded by construction: "
    "one object, no traversal, at most 64 property paths each up to 16 "
    "segments deep; an oversized or malformed request is a structured "
    "invalid_params error. Prefer the specific typed op (vm.info, "
    "host.usage, ...) when one exists -- it returns parsed rows with "
    "curated guidance. Reads directly on the connector session, so it "
    "works with zero catalog ingest. Read-only."
)

VMWARE_OBJECT_COLLECT_OP = VmwareTypedOp(
    op_id="vmware.object.collect",
    handler_attr="object_collect",
    summary="Bounded generic PropertyCollector read of one managed object.",
    description=(
        "Reads a caller-specified set of property paths off a single "
        "managed object addressed by 'type' + 'moid', returning "
        "{properties: {path: value}, missing: [path]}. The bounded generic "
        "escape hatch for the long tail of one-off managed-object reads: "
        "no purpose-built op needed. Bounded by construction to keep it a "
        "dumb substrate -- exactly one object, no TraversalSpec (cannot "
        "walk the inventory), at most 64 property paths each a dotted vim "
        "identifier up to 16 segments deep; an oversized / malformed "
        "request fails parameter validation as a structured invalid_params "
        "error before any read is issued. Reads via PropertyCollector "
        "directly on the connector session, so it works with zero catalog "
        "ingest. Prefer a purpose-built typed op when one covers the "
        "property set. safety_level=safe, read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=OBJECT_COLLECT_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "property-collector", "generic"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to read specific vim25 property paths off one managed "
            "object when no dedicated typed op fits. Supply the object 'type' "
            "and 'moid' and the exact 'properties' list. Bounded: one object, "
            "no traversal, <=64 paths. Prefer vm.info / host.usage / "
            "host.vsan_health when they cover what you need."
        ),
        "parameter_hints": {
            "type": "MO type, e.g. 'VirtualMachine' / 'HostSystem' / 'Datastore'.",
            "moid": "MO id, e.g. 'vm-42' / 'host-12' / 'datastore-5'.",
            "properties": "1-64 dotted property paths, e.g. ['summary.freeSpace'].",
        },
        "output_shape": (
            "{type, moid, properties: {path: raw_value, ...}, missing: "
            "[path, ...]}. 'missing' names paths the collector could not read."
        ),
    },
)
