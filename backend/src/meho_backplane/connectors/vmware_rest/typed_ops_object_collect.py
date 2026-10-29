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

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.vim_body import (
    fault_type_name,
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "OBJECT_COLLECT_GROUP_KEY",
    "OBJECT_COLLECT_WHEN_TO_USE",
    "VMWARE_OBJECT_COLLECT_OP",
    "build_object_collect_retrieve_params",
    "object_collect_impl",
]


def _log() -> Any:
    """Get the logger per call, so a cached proxy never hides events from
    ``structlog.testing.capture_logs`` (same as ``checks/investigate.py``)."""
    return structlog.get_logger(__name__)


# PropertyCollector RetrievePropertiesEx against the singleton
# ``propertyCollector`` moId (carried in the path, so the body is only
# the method arguments). Spec-relative; mounted per-target by
# mount_op_path.
_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"

#: The per-property ``missingSet`` fault types that, when they cover EVERY
#: requested property on a resolved object, mark a stale-but-live vSphere/ESXi
#: SOAP session rather than a genuine read outcome (#3773). ``NotAuthenticated``
#: is the classic server-side-expired-session symptom (the host still answers
#: the unauthenticated ``RetrieveServiceContent`` on the dead cookie but faults
#: every property read). ``NoPermission`` is included ONLY for the all-object
#: case — a session whose principal lost its view faults every property alike;
#: a *partial* per-field ``NoPermission`` (some properties readable) is a real
#: permission outcome and is excluded structurally by the "no property came
#: back" guard in :func:`_all_properties_missing_with_auth_fault`.
#:
#: Known tradeoff of including ``NoPermission``: on a target whose principal
#: really lost all permissions, every such read costs one extra login and
#: evicts the shared per-target session. We accept that cost, because the
#: reset happens at most once per read and the same symptom also marks a
#: stale session that the reset recovers.
_AUTH_CLASS_MISSING_FAULT_TYPES = frozenset({"NotAuthenticated", "NoPermission"})

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
    ``specSet`` + ``options`` method arguments, ``_typeName``-annotated
    via the shared trio helper (#3103).
    """
    return retrieve_properties_body(mo_type, [moid], properties)


def _extract_object_content(
    retrieve_result: Any,
) -> tuple[dict[str, Any], list[str], list[dict[str, str]], dict[str, int]]:
    """Flatten the first ``ObjectContent`` to props + missing detail.

    ``RetrievePropertiesEx`` returns a ``RetrieveResult`` whose ``objects``
    list carries one ``ObjectContent`` per queried object. For the single
    object queried here the first entry's ``propSet`` holds the readable
    ``{name, val}`` pairs and its ``missingSet`` names the paths the
    collector could not read (permission / not-applicable). A bare list
    (legacy ``RetrieveProperties`` shape) is tolerated.

    Returns ``(props, missing, missing_properties, fault_counts)``:

    * ``missing`` -- the bare missing property paths (unchanged shape).
    * ``missing_properties`` -- one ``{"path", "fault_type"?}`` entry per
      ``missingSet`` member: the property ``path`` plus, when the collector
      returned a per-property fault, its concrete vim fault **type name**
      (:func:`fault_type_name` -- e.g. ``NoPermission`` / ``InvalidProperty``
      / ``ManagedObjectNotFound``). vCenter sends a fault with every
      ``missingSet`` entry; an entry with no usable fault class gets no
      ``fault_type``. Requested paths that are in neither list are added
      later by :func:`_add_unreturned_paths` (#5131).
    * ``fault_counts`` -- a per-fault-type count summary (``{"NoPermission":
      12}``); a ``missingSet`` with no faulted entries yields ``{}``.

    Type **names and counts only** -- never fault text (``faultstring`` /
    ``faultMessage`` / ``localizedMessage``) or fault payload fields, which
    can echo a property value or a private identifier (#3708).
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
    missing_properties: list[dict[str, str]] = []
    fault_counts: dict[str, int] = {}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and isinstance(prop.get("name"), str):
                props[prop["name"]] = unwrap_vim_value(prop.get("val"))
        for miss in obj.get("missingSet", []) or []:
            if not isinstance(miss, dict) or not isinstance(miss.get("path"), str):
                continue
            path = miss["path"]
            missing.append(path)
            entry: dict[str, str] = {"path": path}
            type_name = fault_type_name(miss.get("fault"))
            if type_name is not None:
                entry["fault_type"] = type_name
                fault_counts[type_name] = fault_counts.get(type_name, 0) + 1
            missing_properties.append(entry)
    return props, missing, missing_properties, fault_counts


def _all_properties_missing_with_auth_fault(
    requested: list[str],
    read_props: dict[str, Any],
    missing_properties: list[dict[str, str]],
) -> bool:
    """Return ``True`` iff a resolved object came back all-properties-missing with auth faults.

    The stale-but-live session symptom (#3773): the object resolves (so this is
    a well-formed ``RetrieveResult``, not a ``ManagedObjectNotFound`` — that is
    promoted upstream) but **every requested property** landed in ``missingSet``
    each carrying an **auth-class** fault
    (:data:`_AUTH_CLASS_MISSING_FAULT_TYPES`). That is the signature of a
    server-side-expired-but-locally-cached SOAP session, whatever killed it
    (idle expiry, hostd restart, …); the caller invalidates + re-reads once to
    recover it, exactly as a top-level auth fault already recovers.

    ``True`` requires all three, so the fix never perturbs a legitimate read:

    * **no property came back** (``read_props`` empty) — a partial read (at
      least one property present, e.g. a per-field ``NoPermission`` on one path
      of an otherwise-readable object) is a real outcome, not this symptom;
    * **every requested path is missing** — the object is fully void, not
      merely short a path or two;
    * **every missing entry carries an auth-class fault type** — a
      genuinely-unset property (no ``fault_type``) or a non-auth fault
      (``InvalidProperty`` / ``ManagedObjectNotFound``) makes this ``False``, so
      those still return the annotated envelope unchanged.
    """
    if read_props:
        return False
    if not missing_properties:
        return False
    missing_paths = {entry["path"] for entry in missing_properties}
    if any(path not in missing_paths for path in requested):
        return False
    return all(
        entry.get("fault_type") in _AUTH_CLASS_MISSING_FAULT_TYPES for entry in missing_properties
    )


def _add_unreturned_paths(
    requested: list[str],
    read_props: dict[str, Any],
    missing: list[str],
    missing_properties: list[dict[str, str]],
) -> None:
    """Add every requested path that vCenter left out of the answer to ``missing``.

    vCenter puts a property it could not read in ``missingSet``, always with a
    fault. A property that is not set, or whose value is a link to an object the
    login cannot see, is in neither ``propSet`` nor ``missingSet`` (#5131). Each
    such path is added to ``missing`` and ``missing_properties``, without a
    ``fault_type``, in request order. ``missing_fault_summary`` is not changed:
    it counts real faults only.
    """
    seen = set(read_props) | set(missing)
    for path in requested:
        if path in seen:
            continue
        seen.add(path)
        missing.append(path)
        missing_properties.append({"path": path})


async def _collect_with_auth_health_reset(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    mo_type: str,
    moid: str,
    properties: list[str],
    *,
    on_reset: Callable[[list[str]], None] | None = None,
) -> tuple[dict[str, Any], list[str], list[dict[str, str]], dict[str, int], list[str]]:
    """Read the object's properties, self-healing a stale-but-live session once (#3773b).

    Issues the ``RetrievePropertiesEx`` read and flattens it. If the object
    resolves but **every** requested property is missing under an auth-class
    fault (:func:`_all_properties_missing_with_auth_fault`), the cached session
    is a server-side-expired-but-locally-cached cookie: the host still answers
    the unauthenticated bootstrap so no top-level SOAP ``<Fault>`` fires the
    connector's existing re-login, and the read silently returns a "void".
    Invalidate the session (the same recovery hook the dispatcher's #2067
    mid-session-401 path uses: evict → the next read cold-re-logs-in) and
    re-read **once** on the fresh session.

    The reset happens at most once per dispatch — structurally, by re-reading
    exactly once and returning whatever the retried read yields: recovered
    properties, or, if it is STILL all-missing, the fault-annotated result
    (status ok) that the caller turns into the normal envelope — never a second
    reset, no loop. Independent of *why* the session died (idle expiry, hostd
    restart, …) — it recovers on the symptom. A partial read (at least one
    property back) or non-auth faults (``InvalidProperty`` /
    ``ManagedObjectNotFound``) never trigger it and are returned unchanged.

    Returns the ``(props, missing, missing_properties, fault_counts)`` tuple of
    :func:`_extract_object_content` for the read that stands, plus
    ``reset_fault_types``: the sorted fault class names that triggered the
    reset, or an empty list when no reset ran (#3777). Callers use it to mark
    a recovery; it holds class names only, never fault text.

    ``on_reset`` is called with those fault class names right before the
    retried read. The retry can raise (re-login refused, transport error), and
    then nothing is returned; a caller that must record every reset even in
    that case passes ``on_reset`` to learn about it.
    """
    body = build_object_collect_retrieve_params(mo_type, moid, properties)

    async def _read() -> Any:
        return await connector._post_vmomi_json(
            target,
            _RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=body,
            promote_managed_object_not_found=True,
        )

    result = await _read()
    read_props, missing, missing_properties, fault_counts = _extract_object_content(result)
    if not _all_properties_missing_with_auth_fault(properties, read_props, missing_properties):
        return read_props, missing, missing_properties, fault_counts, []

    reset_fault_types = sorted(fault_counts)
    _log().info(
        "vmware_object_collect_auth_reset",
        target=target.name,
        mo_type=mo_type,
        moid=moid,
        requested=len(properties),
        missing_fault_types=reset_fault_types,
    )
    if on_reset is not None:
        on_reset(reset_fault_types)
    await connector.invalidate_session(target)
    result = await _read()
    return (*_extract_object_content(result), reset_fault_types)


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

    2. **Auth health-reset (#3773).** If that read comes back with the object
       resolved but **every** requested property missing under an auth-class
       fault (:func:`_all_properties_missing_with_auth_fault`), the cached
       session is a server-side-expired-but-locally-cached cookie that answers
       the unauthenticated bootstrap yet faults every property — a "void" no
       top-level SOAP ``<Fault>`` would surface. Invalidate the session (the
       same recovery hook the dispatcher's #2067 mid-session-401 path uses) and
       re-read **once** on the fresh session. At most one reset per dispatch: a
       retried read that is still all-missing returns the fault-annotated
       envelope normally, never a second reset. A partial read or a non-auth
       fault never triggers it.

    The size / depth bound is enforced upstream by ``parameter_schema``
    validation in the dispatcher, so by the time this runs ``params`` is
    already within :data:`_MAX_PROPERTIES` / :data:`_MAX_PATH_DEPTH`.

    Returns ``{"type", "moid", "properties": {name: val, ...}, "missing":
    [path, ...], "missing_properties": [{path, fault_type?}, ...],
    "missing_fault_summary": {fault_type: count}}`` -- ``missing`` names every
    requested path that came back without a value, so each requested path is
    either in ``properties`` or in ``missing``. ``missing_properties`` adds the
    per-property vim fault **type name** where vCenter sent one (names only,
    never fault text) and ``missing_fault_summary`` counts them by type
    (#3708). An entry without ``fault_type`` is a path vCenter left out of the
    answer with no fault: the property is not set, or its value is a link to
    an object this login cannot see (#5131).
    """
    mo_type = params["type"]
    moid = params["moid"]
    properties: list[str] = list(params["properties"])

    (
        read_props,
        missing,
        missing_properties,
        fault_counts,
        reset_fault_types,
    ) = await _collect_with_auth_health_reset(
        connector, operator, target, mo_type, moid, properties
    )
    # Here, not in the shared helper: the session reset check (#3773) and the
    # ESXi probe judge the raw answer and must not change.
    _add_unreturned_paths(properties, read_props, missing, missing_properties)

    _log().info(
        "vmware_object_collect_read",
        target=target.name,
        mo_type=mo_type,
        moid=moid,
        requested=len(properties),
        returned=len(read_props),
        missing=len(missing),
        missing_fault_types=sorted(fault_counts),
    )
    out: dict[str, Any] = {
        "type": mo_type,
        "moid": moid,
        "properties": read_props,
        "missing": missing,
        "missing_properties": missing_properties,
        "missing_fault_summary": fault_counts,
    }
    if reset_fault_types:
        # Present only when the session was reset and the read ran again
        # (#3777), so an operator can see and count recoveries.
        out["session_health_reset"] = 1
    return out


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
    "examples": [
        {
            "type": "Datastore",
            "moid": "datastore-12",
            "properties": ["summary.freeSpace", "summary.capacity"],
        }
    ],
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
                "Requested paths that came back without a value. Every requested "
                "path is either in 'properties' or here. Empty means every "
                "requested path came back."
            ),
        },
        "missing_properties": {
            "type": "array",
            "description": (
                "One entry per path in 'missing': the ``path`` plus, when "
                "vCenter sent a fault for it, its vim fault ``fault_type`` (the "
                "concrete fault class name only -- e.g. 'NoPermission', "
                "'InvalidProperty', 'ManagedObjectNotFound', "
                "'NotAuthenticated' -- never fault text, which can echo a "
                "property value or a private identifier). An entry with no "
                "``fault_type`` means vCenter sent no value and no fault: the "
                "property is not set, or its value is a link to an object "
                "this login cannot see."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The missing property path."},
                    "fault_type": {
                        "type": "string",
                        "description": (
                            "Concrete vim fault type name for this property "
                            "(names only, no fault text); absent when vCenter "
                            "sent no value and no fault (not set, or a link "
                            "to an object this login cannot see)."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        "missing_fault_summary": {
            "type": "object",
            "description": (
                "Count of ``missingSet`` faults by vim fault type name (e.g. "
                "{'NoPermission': 12}); empty when no missing property carried "
                "a fault. Type names and counts only -- never fault text."
            ),
            "additionalProperties": {"type": "integer"},
        },
        "session_health_reset": {
            "type": "integer",
            "enum": [1],
            "description": (
                "Present (value 1) only when MEHO found the cached session "
                "dead, logged in again and read the object a second time. "
                "Absent on a normal read."
            ),
        },
    },
    # ``missing_properties`` / ``missing_fault_summary`` are deliberately NOT
    # required: they are additive (#3708), always emitted by the current
    # handler but kept optional so historically-captured / older-connector
    # responses validate unchanged. ``session_health_reset`` (#3777) is only
    # present after a session reset, so a clean read must validate without it.
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
    summary="Read chosen properties of one vSphere managed object, by type and moid.",
    description=(
        "Reads up to 64 property paths of one managed object. Returns type, moid, properties, "
        "missing, missing_properties, missing_fault_summary and, after a session reset, "
        "session_health_reset. vCenter 8.0.x refuses 'configurationEx.<field>': request "
        "'configurationEx' whole. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=OBJECT_COLLECT_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "property-collector", "generic"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read some vim25 property paths of one managed object when no "
            "dedicated typed op fits. Give the object 'type' and 'moid' and the exact "
            "'properties' list. It reads one object, follows no links and takes at most "
            "64 paths. Prefer vm.info, host.usage or host.vsan_health when they cover "
            "what you need. Use it for distributed switch and distributed portgroup "
            "settings (type 'DistributedVirtualPortgroup' or "
            "'VmwareDistributedVirtualSwitch')."
        ),
        "parameter_hints": {
            "type": "MO type, e.g. 'VirtualMachine' / 'HostSystem' / 'Datastore'.",
            "moid": "MO id, e.g. 'vm-42' / 'host-12' / 'datastore-5'.",
            "properties": (
                "1-64 dotted property paths, e.g. ['summary.freeSpace']. For a "
                "cluster's DRS rules, groups or DRS settings, request "
                "['configurationEx'] and pick 'rule', 'group' or 'drsConfig' "
                "from the result: vCenter 8.0.x rejects 'configurationEx.<field>' "
                "with InvalidProperty. Read a polymorphic field whole: for a "
                "distributed portgroup's VLAN or security policy, request "
                "['config.defaultPortConfig'], not "
                "'config.defaultPortConfig.vlan'. The field's declared type is "
                "the base DVPortSetting, so a path into a subclass-only field "
                "fails with InvalidProperty."
            ),
        },
        "output_shape": (
            "{type, moid, properties: {path: raw_value}, missing: [path], "
            "missing_properties: [{path, fault_type}], missing_fault_summary: "
            "{fault_type: count}, session_health_reset}. Values are raw VI-JSON. "
            "Every requested path is in properties or in missing. fault_type is a vim "
            "fault name only (never fault text). An entry with no fault_type means "
            "vCenter sent no value and no fault: the property is not set, or its value "
            "is a link to an object this login cannot see. session_health_reset is "
            "there only when MEHO logged in again and read a second time."
        ),
        "next_step": (
            "For a path in missing with fault_type InvalidProperty, read the parent path "
            "whole. With NoPermission, the MEHO account cannot see it; report that. With "
            "no fault_type, if you expected a value, report that the login may not have "
            "the right to see the linked object. A moref value in properties "
            "({type, value}) can be read next with this op."
        ),
    },
)
