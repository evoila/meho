# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Copy-size preview for the VM moves that copy disks (#4749).

The VMware floor (``ingest_safety.py``) makes the generic relocate
(``POST /vcenter/vm/{vm}?action=relocate``, also with ``&vmw-task=true``) and
the older-API method ``POST /VirtualMachine/{moId}/RelocateVM_Task`` wait for
approval. When such a call names a datastore for the VM or for one of its
disks, vCenter copies the disks there. In a cold move the VM is off for the
whole copy, and the copy fills the target datastore. The person who approves
sees how much data the call copies before deciding.

The builder makes two read-only VI-JSON property reads:

* the VM: name, power state, the datastores its files are on, and
  ``summary.storage`` -- ``committed`` is the data copied now, ``committed +
  uncommitted`` is the most the disks can grow to (a thin disk uses only the
  space written so far);
* the datastores: name, capacity, and free space before the copy.

A failed read never blocks the approval: the preview keeps what it has and
says why the size is missing. That covers a failed call and a property
vCenter could not read inside a successful answer (a ``missingSet`` entry,
for example no permission on the storage data): the unreadable property and
its fault type land in the preview too. :func:`read_vm_copy_size` and
:func:`read_datastore_space` are the shared size reads, so the typed
datastore move can show the same numbers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest.composites._read import (
    _extract_props_by_moid,
    _unwrap_value,
)
from meho_backplane.connectors.vmware_rest.typed_ops import _int_or_none
from meho_backplane.connectors.vmware_rest.vim_body import (
    fault_type_name,
    retrieve_properties_body,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "VM_RELOCATE_OP_IDS",
    "DatastoreSpace",
    "VmCopySize",
    "read_datastore_space",
    "read_vm_copy_size",
    "register_vm_relocate_previews",
    "relocate_target_datastores",
    "vm_relocate_preview",
]

_log = structlog.get_logger(__name__)

_RETRIEVE_PROPERTIES_PATH: Final = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_VM_MO_TYPE: Final = "VirtualMachine"
_DATASTORE_MO_TYPE: Final = "Datastore"
_PROP_NAME: Final = "name"
_PROP_POWER_STATE: Final = "runtime.powerState"
_PROP_VM_DATASTORE: Final = "datastore"
_PROP_STORAGE_SUMMARY: Final = "summary.storage"
_VM_PATHS: Final = (_PROP_NAME, _PROP_POWER_STATE, _PROP_VM_DATASTORE, _PROP_STORAGE_SUMMARY)
_PROP_DATASTORE_SUMMARY: Final = "summary"

#: The VM id rides the path: ``{vm}`` on the REST relocate, ``{moId}`` on the
#: older-API method. An appliance-served spec can spell them ``{vm_id}`` and
#: ``{mo_id}``. The safety rule (``is_vmware_vm_relocate``) accepts every
#: spelling, so the preview is registered under every one of them.
_REST_VM_PARAM_NAMES: Final = ("vm", "vm_id")
_VIM_VM_PARAM_NAMES: Final = ("moId", "mo_id")
_VM_PARAM_NAMES: Final = _REST_VM_PARAM_NAMES + _VIM_VM_PARAM_NAMES
_MOUNT_PREFIXES: Final = ("", "/api")
_REST_QUERIES: Final = ("action=relocate", "action=relocate&vmw-task=true")

#: Every op_id spelling of the VM moves that copy disks: the generic relocate,
#: with and without the task form, and the older-API method, each with and
#: without the ``/api`` mount and with each spelling of the VM id parameter.
VM_RELOCATE_OP_IDS: Final[frozenset[str]] = frozenset(
    f"POST:{prefix}/vcenter/vm/{{{name}}}?{query}"
    for prefix, name, query in product(_MOUNT_PREFIXES, _REST_VM_PARAM_NAMES, _REST_QUERIES)
) | frozenset(
    f"POST:{prefix}/VirtualMachine/{{{name}}}/RelocateVM_Task"
    for prefix, name in product(_MOUNT_PREFIXES, _VIM_VM_PARAM_NAMES)
)

_GIB: Final = 1024**3
_POWER_STATE_WORDS: Final = {
    "poweredOn": "running",
    "poweredOff": "stopped",
    "suspended": "suspended",
}


@dataclass(frozen=True)
class VmCopySize:
    """What a disk copy of one VM moves: its identity, state, and storage size."""

    vm: str
    name: str | None
    power_state: str | None
    #: Datastores the VM's files are on today (``VirtualMachine.datastore``).
    source_datastores: tuple[str, ...]
    #: ``summary.storage.committed``: the data copied now.
    copied_now_bytes: int | None
    #: ``committed + uncommitted``: the most the disks can grow to.
    copy_max_bytes: int | None
    #: ``"<property> (<fault type>)"`` for each property vCenter could not read.
    unreadable_properties: tuple[str, ...] = ()


@dataclass(frozen=True)
class DatastoreSpace:
    """A datastore's name, capacity and free space (``Datastore.summary``)."""

    id: str
    name: str | None
    capacity_bytes: int | None
    free_space_bytes: int | None
    #: ``"<property> (<fault type>)"`` for each property vCenter could not read.
    unreadable_properties: tuple[str, ...] = ()


def _datastore_id(value: Any) -> str | None:
    """A datastore id from a REST string or an older-API ``ManagedObjectReference``."""
    if isinstance(value, Mapping):
        value = value.get("value")
    return value if isinstance(value, str) and value else None


def _unique(values: Iterable[str | None]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value is not None:
            seen.setdefault(value, None)
    return list(seen)


def relocate_target_datastores(body: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    """Return ``(datastore for the VM home, datastores named per disk)`` from a relocate body.

    Reads the REST body (``placement.datastore``, ``disks.{disk}.datastore``) and
    the older-API body (``spec.datastore``, ``spec.disk[].datastore``). A
    datastore for the VM home moves every disk that is not named on its own.
    """
    spec = body.get("spec")
    if isinstance(spec, Mapping):
        disks = spec.get("disk")
        entries: Iterable[Any] = disks if isinstance(disks, list) else ()
        home = _datastore_id(spec.get("datastore"))
    else:
        placement = body.get("placement")
        disks = body.get("disks")
        entries = disks.values() if isinstance(disks, Mapping) else ()
        home = _datastore_id(placement.get("datastore")) if isinstance(placement, Mapping) else None
    per_disk = _unique(
        _datastore_id(entry.get("datastore")) for entry in entries if isinstance(entry, Mapping)
    )
    return home, per_disk


def _unreadable_by_moid(retrieve_result: Any) -> dict[str, tuple[str, ...]]:
    """``{moid: ("<property> (<fault type>)", ...)}`` from the ``missingSet`` entries.

    The HTTP call succeeds while vCenter still could not read a property of
    one object (no permission, a system error). Such a property is not in
    ``propSet``; it is in the object's ``missingSet`` with a fault. A property
    that is simply unset has no ``missingSet`` entry and is not reported. Only
    the fault type name is kept, never the fault text, which can echo a
    private identifier (#3708).
    """
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    unreadable: dict[str, tuple[str, ...]] = {}
    for obj in objects if isinstance(objects, list) else []:
        if not isinstance(obj, dict):
            continue
        ref = obj.get("obj")
        moid = ref.get("value") if isinstance(ref, dict) else None
        if not isinstance(moid, str):
            continue
        entries = [
            f"{entry['path']} ({fault_type_name(entry.get('fault')) or 'unknown fault'})"
            for entry in obj.get("missingSet") or []
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        ]
        if entries:
            unreadable[moid] = tuple(entries)
    return unreadable


async def _retrieve(
    connector: VmwareRestConnector, target: Any, operator: Operator, body: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, ...]]]:
    """One ``RetrievePropertiesEx`` read: ``({moid: {property: value}}, unreadable)``.

    *unreadable* names, per object, the properties vCenter could not read
    (:func:`_unreadable_by_moid`).
    """
    result = await connector._post_vmomi_json(
        target,
        _RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=body,
        promote_managed_object_not_found=True,
    )
    return _extract_props_by_moid(result), _unreadable_by_moid(result)


async def read_vm_copy_size(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str
) -> VmCopySize:
    """Read one VM's name, power state, datastores and storage size (read-only).

    Raises when the VM cannot be read; the caller decides what that means. A
    property vCenter could not read inside a successful answer is reported in
    ``unreadable_properties`` and its value is ``None``.
    """
    body = retrieve_properties_body(_VM_MO_TYPE, [vm], _VM_PATHS)
    by_moid, unreadable = await _retrieve(connector, target, operator, body)
    props = by_moid.get(vm)
    if props is None:
        raise ConnectorResourceNotFoundError([vm], f"VM {vm!r} was not in the vCenter answer")
    storage = props.get(_PROP_STORAGE_SUMMARY)
    storage = storage if isinstance(storage, Mapping) else {}
    committed = _int_or_none(storage.get("committed"))
    uncommitted = _int_or_none(storage.get("uncommitted"))
    datastores = props.get(_PROP_VM_DATASTORE)
    name = props.get(_PROP_NAME)
    power_state = props.get(_PROP_POWER_STATE)
    return VmCopySize(
        vm=vm,
        name=name if isinstance(name, str) else None,
        power_state=power_state if isinstance(power_state, str) else None,
        source_datastores=tuple(
            _unique(_datastore_id(ref) for ref in datastores)
            if isinstance(datastores, list)
            else ()
        ),
        copied_now_bytes=committed,
        copy_max_bytes=(
            committed + uncommitted if committed is not None and uncommitted is not None else None
        ),
        unreadable_properties=unreadable.get(vm, ()),
    )


async def read_datastore_space(
    connector: VmwareRestConnector, target: Any, operator: Operator, datastores: Sequence[str]
) -> dict[str, DatastoreSpace]:
    """Read name, capacity and free space of *datastores* in one read (read-only).

    A datastore whose summary vCenter could not read keeps ``None`` values and
    names the property in ``unreadable_properties``.
    """
    if not datastores:
        return {}
    body = retrieve_properties_body(_DATASTORE_MO_TYPE, datastores, (_PROP_DATASTORE_SUMMARY,))
    by_moid, unreadable = await _retrieve(connector, target, operator, body)
    spaces: dict[str, DatastoreSpace] = {}
    for moid in datastores:
        summary = by_moid.get(moid, {}).get(_PROP_DATASTORE_SUMMARY)
        summary = summary if isinstance(summary, Mapping) else {}
        name = summary.get("name")
        spaces[moid] = DatastoreSpace(
            id=moid,
            name=name if isinstance(name, str) else None,
            capacity_bytes=_int_or_none(summary.get("capacity")),
            free_space_bytes=_int_or_none(summary.get("freeSpace")),
            unreadable_properties=unreadable.get(moid, ()),
        )
    return spaces


def _gib(size: int | None) -> str:
    return "an unknown size" if size is None else f"{size / _GIB:.1f} GiB"


def _after(free: int | None, size: int | None) -> int | None:
    return None if free is None or size is None else free - size


def _datastore_entry(moid: str, spaces: Mapping[str, DatastoreSpace]) -> dict[str, Any]:
    space = spaces.get(moid)
    return {"id": moid, "name": space.name if space is not None else None}


def _target_entry(
    moid: str, spaces: Mapping[str, DatastoreSpace], size: VmCopySize | None
) -> dict[str, Any]:
    space = spaces.get(moid)
    free = space.free_space_bytes if space is not None else None
    return {
        **_datastore_entry(moid, spaces),
        "capacity_bytes": space.capacity_bytes if space is not None else None,
        "free_space_bytes": free,
        "free_space_after_copy_bytes": _after(free, size.copied_now_bytes if size else None),
        "free_space_after_max_copy_bytes": _after(free, size.copy_max_bytes if size else None),
    }


def _label(entry: Mapping[str, Any]) -> str:
    name = entry.get("name")
    return f"{name} ({entry['id']})" if isinstance(name, str) else str(entry["id"])


def _summary_text(preview: Mapping[str, Any], size: VmCopySize | None) -> str:
    """One or two plain sentences: the size first, then where, then the VM state."""
    targets = preview["target_datastores"]
    at_most = "at most " if preview["copy_size_is_upper_bound"] else ""
    if size is None or size.copied_now_bytes is None:
        parts = [f"The copy size is missing: {preview['size_unavailable_reason']}."]
    else:
        parts = [
            f"Copies {at_most}{_gib(size.copied_now_bytes)} now, "
            f"up to {_gib(size.copy_max_bytes)} if the disks fill up"
        ]
        sources = ", ".join(_label(s) for s in preview["source_datastores"]) or "unknown datastores"
        parts[0] += f", from {sources} to {', '.join(_label(t) for t in targets)}."
    if preview["copy_scope"] == "some_disks":
        parts.append("Only some disks move, so the real copy can be smaller.")
    if len(targets) > 1:
        parts.append("The disks go to more than one datastore, so each one gets at most this much.")
    for target in targets:
        free = target["free_space_bytes"]
        after = target["free_space_after_copy_bytes"]
        if free is None:
            parts.append(f"The free space of {_label(target)} is unknown.")
        elif after is None:
            parts.append(f"{_label(target)} has {_gib(free)} free before the copy.")
        else:
            parts.append(
                f"{_label(target)} has {_gib(free)} free before the copy "
                f"and {_gib(after)} after it."
            )
    if size is not None:
        state = _POWER_STATE_WORDS.get(size.power_state or "", size.power_state or "unknown")
        parts.append(f"The VM is {state}.")
    return " ".join(parts)


async def _read_vm(
    connector: VmwareRestConnector, ctx: PreviewContext, vm: str
) -> tuple[VmCopySize | None, list[str]]:
    """The VM read for the preview: ``(size or None, why the size is missing)``.

    A failed call and a property vCenter could not read inside a successful
    answer both land in the reasons; the park must go on either way.
    """
    try:
        size = await read_vm_copy_size(connector, ctx.target, ctx.operator, vm)
    except Exception as exc:
        return None, [f"the VM read failed ({preview_failure_reason(exc)})"]
    if size.unreadable_properties:
        return size, [f"vCenter could not read {', '.join(size.unreadable_properties)} of the VM"]
    if size.copied_now_bytes is None:
        return size, ["vCenter did not report the storage size of the VM"]
    return size, []


async def _read_datastores(
    connector: VmwareRestConnector, ctx: PreviewContext, datastores: Sequence[str]
) -> tuple[dict[str, DatastoreSpace], list[str]]:
    """The datastore read for the preview: ``(spaces, why free space is missing)``.

    Same rule as :func:`_read_vm`: a failed call keeps the VM size and says
    the free space is unknown; an unreadable summary names the datastore.
    """
    try:
        spaces = await read_datastore_space(connector, ctx.target, ctx.operator, datastores)
    except Exception as exc:
        return {}, [f"the datastore read failed ({preview_failure_reason(exc)})"]
    unreadable = [
        f"{', '.join(space.unreadable_properties)} of datastore {moid}"
        for moid, space in spaces.items()
        if space.unreadable_properties
    ]
    return spaces, [f"vCenter could not read {'; '.join(unreadable)}"] if unreadable else []


async def vm_relocate_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview for the VM moves: whether the call copies disks, how much, and where.

    No datastore in the call: no disks are copied, and nothing is read. With a
    datastore, the VM and the datastores are read; a failed read, and a
    property vCenter could not read inside a successful answer, are reported
    in ``size_unavailable_reason`` and the call still waits for approval.
    """
    if ctx.connector_instance is None:
        return None
    vm = next((ctx.params[n] for n in _VM_PARAM_NAMES if isinstance(ctx.params.get(n), str)), None)
    if vm is None:
        return None
    body = ctx.params.get("body")
    home, per_disk = relocate_target_datastores(body if isinstance(body, Mapping) else {})
    targets = _unique([home, *per_disk])
    if not targets:
        return {
            "summary": "No disks are copied: the call names no datastore.",
            "vm": vm,
            "copies_disks": False,
            "copy_scope": "none",
            "target_datastores": [],
        }
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    size, reasons = await _read_vm(connector, ctx, vm)
    sources = list(size.source_datastores) if size is not None else []
    spaces, datastore_reasons = await _read_datastores(
        connector, ctx, _unique([*sources, *targets])
    )
    reasons += datastore_reasons
    if reasons:
        _log.warning("vm_relocate_preview_read_failed", vm=vm, reasons=reasons)
    preview: dict[str, Any] = {
        "vm": vm,
        "name": size.name if size is not None else None,
        "power_state": size.power_state if size is not None else None,
        "copies_disks": True,
        "copy_scope": "whole_vm" if home is not None else "some_disks",
        "copy_size_is_upper_bound": home is None or len(targets) > 1,
        "copied_now_bytes": size.copied_now_bytes if size is not None else None,
        "copy_max_bytes": size.copy_max_bytes if size is not None else None,
        "source_datastores": [_datastore_entry(moid, spaces) for moid in sources],
        "target_datastores": [_target_entry(moid, spaces, size) for moid in targets],
    }
    if reasons:
        preview["size_unavailable_reason"] = "; ".join(reasons)
    return {"summary": _summary_text(preview, size), **preview}


def register_vm_relocate_previews() -> None:
    """Register the builder under every op_id spelling. Import-time."""
    for op_id in VM_RELOCATE_OP_IDS:
        register_preview_builder(op_id, vm_relocate_preview)


register_vm_relocate_previews()
