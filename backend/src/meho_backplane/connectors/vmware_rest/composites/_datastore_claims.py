# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which registered VMs use files on a datastore (#3339).

Used by ``datastore.file.delete`` (refuse a path that holds any file a VM
uses) and by the content-library deletes (refuse while a VM mounts an item).

A VM "uses" a file when it is:

* in ``layoutEx.file`` -- disks, linked-clone parents, ``.vmx``, ``.nvram``,
  snapshots, swap, logs;
* the ``fileName`` of a device backing in ``config.hardware.device`` -- disks,
  and the files ``layoutEx`` does not list: ISO / floppy images in a CD-ROM or
  floppy drive (also on a powered-off VM or a template) and serial-port files;
* the ``.vmx`` (``config.files.vmPathName`` / ``summary.config.vmPathName``).

The VMs are the ones in ``Datastore.vm`` (every VM with any file on the
datastore, mounted images included). The read is strict: a paged or partial
answer, or a VM whose files cannot be known, makes the check "incomplete" and
the caller refuses -- it never reads as "nothing uses it". Comparisons ignore
case, so they refuse more, never less.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._teardown_reads import read_many
from meho_backplane.connectors.vmware_rest.composites._write import _VIRTUAL_MACHINE_MO_TYPE
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, unwrap_vim_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.composites._datastore_browse import Datastore
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = ["media_users", "vm_claims"]

_PROP_LAYOUT: Final = "layoutEx.file"
_PROP_DEVICES: Final = "config.hardware.device"
_PROP_VMX: Final = "config.files.vmPathName"
_PROP_SUMMARY_VMX: Final = "summary.config.vmPathName"
_CLAIM_PROPS: Final = ["name", _PROP_LAYOUT, _PROP_DEVICES, _PROP_VMX, _PROP_SUMMARY_VMX]
#: Removable-media devices a content-library ISO / image can be mounted in.
_MEDIA_DEVICE_TYPES: Final = frozenset({"VirtualCdrom", "VirtualFloppy"})


def _device_files(devices: Any, *, media_only: bool = False) -> list[str]:
    """``backing.fileName`` of every device (or only CD-ROM / floppy drives)."""
    out: list[str] = []
    for device in devices if isinstance(devices, list) else []:
        if not isinstance(device, dict):
            continue
        if media_only and device.get(VIM_TYPE_NAME_KEY) not in _MEDIA_DEVICE_TYPES:
            continue
        backing = device.get("backing")
        name = backing.get("fileName") if isinstance(backing, dict) else None
        if isinstance(name, str) and name:
            out.append(name)
    return out


def _within(child: str, parent: str) -> bool:
    """``True`` when *child* is *parent* or lies below it (case-insensitive)."""
    child, parent = child.casefold(), parent.casefold()
    return child == parent or child.startswith(parent + "/")


def _claim_hits(ds: Datastore, props: dict[str, Any], path: str) -> list[str] | None:
    """The VM's files on *ds* that a delete of *path* would remove; ``None`` = unknowable.

    Without ``layoutEx`` the VM's whole home folder counts as used (its logs and
    swap live there). A VM with neither ``layoutEx`` nor devices, whose home is
    on another datastore, is unknowable -- it is in ``Datastore.vm``, so it has
    something here -- and the caller refuses.
    """
    layout = unwrap_vim_value(props.get(_PROP_LAYOUT))
    devices = unwrap_vim_value(props.get(_PROP_DEVICES))
    vmx = props.get(_PROP_VMX) or props.get(_PROP_SUMMARY_VMX)
    names = [
        entry["name"]
        for entry in (layout if isinstance(layout, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]
    names += _device_files(devices)
    if isinstance(vmx, str):
        names.append(vmx)
    hits = {rel for name in names if (rel := ds.relative(name)) and _within(rel, path)}
    if not isinstance(layout, list) or not layout:
        home_vmx = ds.relative(vmx) if isinstance(vmx, str) else None
        if home_vmx:
            home = home_vmx.rpartition("/")[0]
            if home and _within(path, home):
                hits.add(home + "/")
        elif not isinstance(devices, list):
            return None
    return sorted(hits)


async def vm_claims(
    connector: VmwareRestConnector, target: Any, operator: Operator, ds: Datastore, path: str
) -> tuple[list[dict[str, Any]], bool]:
    """Registered VMs using a file a delete of *path* would remove: ``(rows, complete)``.

    ``complete`` is ``False`` when vCenter paged or left out part of the
    answer, or a VM's files could not be known -- the caller then refuses.
    """
    by_moid, complete = await read_many(
        connector,
        target,
        operator,
        mo_type=_VIRTUAL_MACHINE_MO_TYPE,
        moids=ds.vm_moids,
        props=_CLAIM_PROPS,
    )
    rows: list[dict[str, Any]] = []
    for moid, props in by_moid.items():
        hits = _claim_hits(ds, props, path)
        if hits is None:
            complete = False
        elif hits:
            rows.append({"kind": "vm", "moid": moid, "name": props.get("name"), "files": hits[:10]})
    return rows, complete


async def media_users(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm_moids: list[str],
    needles: list[str],
) -> tuple[list[dict[str, Any]], bool]:
    """VMs whose CD-ROM / floppy image path contains one of *needles*: ``(rows, complete)``.

    The content-library check: an item's files live in a folder named by the
    item id, so a mounted ISO's ``backing.fileName`` contains ``/<item id>/``.
    Matching on the id (not the datastore folder name) also works where the
    library folder is shown by UUID.
    """
    by_moid, complete = await read_many(
        connector,
        target,
        operator,
        mo_type=_VIRTUAL_MACHINE_MO_TYPE,
        moids=vm_moids,
        props=["name", _PROP_DEVICES],
    )
    wanted = [needle.casefold() for needle in needles]
    rows: list[dict[str, Any]] = []
    for moid, props in by_moid.items():
        files = [
            name
            for name in _device_files(unwrap_vim_value(props.get(_PROP_DEVICES)), media_only=True)
            if any(needle in name.casefold() for needle in wanted)
        ]
        if files:
            rows.append(
                {"kind": "vm", "moid": moid, "name": props.get("name"), "files": files[:10]}
            )
    return rows, complete
