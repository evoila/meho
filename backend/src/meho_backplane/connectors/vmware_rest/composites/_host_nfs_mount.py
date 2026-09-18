# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Idempotence pre-check + race resolution for ``host.datastore_mount_nfs``.

Split out of :mod:`._host` (file-size discipline) so the mount composite's
read-before-write idempotence — the piece that makes a re-run of the NFS
mount step converge instead of surfacing a raw vim ``DuplicateName`` — lives
in one focused unit. Everything here is a *read* (``RetrievePropertiesEx`` on
the host's mounted datastores) plus pure matching / envelope shaping; the
gated ``CreateNasDatastore`` write stays in
:func:`._host.datastore_mount_nfs_composite`.

The read reuses the same ungated ``PropertyCollector.RetrievePropertiesEx``
seam the config-manager read uses (declared already as
:data:`._host._OP_RETRIEVE_PROPERTIES` on the mount composite's sub-op
manifest, so no new vim op_id is introduced) and works on a vCenter VI-JSON
target and a standalone-ESXi SOAP target alike, because
:meth:`~...connector.VmwareRestConnector._post_vmomi_json` routes both.

The matcher keys on the export ``(remoteHost, remotePath)`` — the true
identity of an NFS mount — with the obvious normalisation (case-insensitive
host, trailing-slash-insensitive path). A matching export → ``already_mounted``
(the existing datastore moid, no write). A datastore whose *name* is taken by
a **different** export → ``name_conflict`` (a structured refusal, no write).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.soap import parse_soap_fault
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "STATUS_ALREADY_MOUNTED",
    "STATUS_NAME_CONFLICT",
    "NasDatastore",
    "build_mounted_result",
    "build_nas_volume_spec",
    "is_already_exists_fault",
    "read_mounted_nas_datastores",
    "resolve_mount_precheck",
]

# Result-envelope ``status`` values the pre-check / race arm return.
STATUS_ALREADY_MOUNTED: Final = "already_mounted"
STATUS_NAME_CONFLICT: Final = "name_conflict"

# vim MO types + property paths the pre-check reads.
_HOST_SYSTEM_MO_TYPE: Final = "HostSystem"
_DATASTORE_MO_TYPE: Final = "Datastore"
_HOST_NAS_VOLUME_SPEC_TYPE: Final = "HostNasVolumeSpec"
#: ``HostSystem.datastore`` — the host's mounted Datastore MoRefs.
_PROP_HOST_DATASTORES: Final = "datastore"
#: ``Datastore.info`` — a ``NasDatastoreInfo`` for a NAS mount, carrying
#: ``name`` + the ``nas`` HostNasVolume (``remoteHost`` / ``remotePath`` /
#: ``type``).
_PROP_DATASTORE_INFO: Final = "info"

# Concrete runtime path for the config-manager / datastore reads: the
# PropertyCollector is a singleton whose moId is the literal
# ``propertyCollector`` (mirrors ``_host._VMOMI_RETRIEVE_PROPERTIES_PATH``).
_VMOMI_RETRIEVE_PROPERTIES_PATH: Final = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"

# vim fault-type localNames CreateNasDatastore raises when the export is
# already mounted (documented already-exists class: ``DuplicateName`` for a
# name clash, ``AlreadyExists`` for the volume — handle BOTH). Consulted only
# on the race arm; the common re-run resolves in the read pre-check.
_ALREADY_EXISTS_FAULT_TYPES: Final[frozenset[str]] = frozenset({"DuplicateName", "AlreadyExists"})


@dataclass(frozen=True, slots=True)
class NasDatastore:
    """One mounted NAS datastore, projected from ``Datastore.info`` (NasDatastoreInfo)."""

    moid: str
    name: str | None
    remote_host: str | None
    remote_path: str | None
    nas_type: str | None


# ---------------------------------------------------------------------------
# Read side: project the host's mounted NAS datastores off two reads.
# ---------------------------------------------------------------------------


async def read_mounted_nas_datastores(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    host_moid: str,
) -> list[NasDatastore]:
    """Read the host's mounted NAS datastores (``[]`` when it has none).

    Two ungated ``RetrievePropertiesEx`` reads: the host's ``datastore`` MoRef
    list, then each ``Datastore.info`` (batched into one call). Non-NAS
    datastores (VMFS / vSAN — no ``info.nas``) are dropped, so the result is
    exactly the NFS mounts the export matcher compares against.
    """
    listing = await connector._post_vmomi_json(
        target,
        _VMOMI_RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(_HOST_SYSTEM_MO_TYPE, [host_moid], [_PROP_HOST_DATASTORES]),
    )
    ds_moids = _extract_host_datastore_moids(listing)
    if not ds_moids:
        return []
    infos = await connector._post_vmomi_json(
        target,
        _VMOMI_RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(_DATASTORE_MO_TYPE, ds_moids, [_PROP_DATASTORE_INFO]),
    )
    return _extract_nas_datastores(infos)


def _retrieve_objects(result: Any) -> list[dict[str, Any]]:
    """The ``objects`` list of a ``RetrievePropertiesEx`` result (tolerant of a value box)."""
    payload = result
    if isinstance(payload, dict) and set(payload.keys()) == {"value"}:
        payload = payload["value"]
    objects = payload.get("objects") if isinstance(payload, dict) else payload
    return [obj for obj in objects if isinstance(obj, dict)] if isinstance(objects, list) else []


def _prop_value(obj: dict[str, Any], name: str) -> Any:
    """Unwrapped ``propSet`` value for *name* on one RetrieveResult object, else ``None``."""
    for entry in obj.get("propSet", []) or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return unwrap_vim_value(entry.get("val"))
    return None


def _object_moid(obj: dict[str, Any]) -> str | None:
    """The ``obj`` MoRef's ``value`` (the object's moid) off one RetrieveResult object."""
    ref = obj.get("obj")
    value = ref.get("value") if isinstance(ref, dict) else None
    return value if isinstance(value, str) and value else None


def _as_moref_list(refs: Any) -> list[dict[str, Any]]:
    """Normalise an unwrapped ``ArrayOfManagedObjectReference`` (or a lone MoRef) to a list."""
    if isinstance(refs, list):
        return [ref for ref in refs if isinstance(ref, dict)]
    if isinstance(refs, dict) and "value" in refs:
        return [refs]
    return []


def _extract_host_datastore_moids(result: Any) -> list[str]:
    """Datastore moids off a ``HostSystem.datastore`` RetrievePropertiesEx result."""
    moids: list[str] = []
    for obj in _retrieve_objects(result):
        for ref in _as_moref_list(_prop_value(obj, _PROP_HOST_DATASTORES)):
            value = ref.get("value")
            if isinstance(value, str) and value:
                moids.append(value)
    return moids


def _extract_nas_datastores(result: Any) -> list[NasDatastore]:
    """Project a ``Datastore.info`` RetrievePropertiesEx result to the NAS mounts only."""
    datastores: list[NasDatastore] = []
    for obj in _retrieve_objects(result):
        moid = _object_moid(obj)
        info = _prop_value(obj, _PROP_DATASTORE_INFO)
        if moid is None or not isinstance(info, dict):
            continue
        nas = info.get("nas")
        if not isinstance(nas, dict):
            continue  # non-NAS datastore (VMFS / vSAN) — no export to match
        datastores.append(
            NasDatastore(
                moid=moid,
                name=_str_or_none(info.get("name")),
                remote_host=_str_or_none(nas.get("remoteHost")),
                remote_path=_str_or_none(nas.get("remotePath")),
                nas_type=_str_or_none(nas.get("type")),
            )
        )
    return datastores


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


# ---------------------------------------------------------------------------
# Matching + envelope shaping (pure).
# ---------------------------------------------------------------------------


def _norm_host(host: str) -> str:
    """Case-insensitive NFS server identity (hostnames / IPs are case-insensitive)."""
    return host.strip().lower()


def _norm_path(path: str) -> str:
    """Trailing-slash-insensitive export path identity (``/export/`` == ``/export``)."""
    return path.strip().rstrip("/") or "/"


def _matches_export(ds: NasDatastore, nfs_server: str, remote_path: str) -> bool:
    """True when *ds* mounts exactly the ``(nfs_server, remote_path)`` export."""
    if ds.remote_host is None or ds.remote_path is None:
        return False
    return _norm_host(ds.remote_host) == _norm_host(nfs_server) and _norm_path(
        ds.remote_path
    ) == _norm_path(remote_path)


def resolve_mount_precheck(
    datastores: list[NasDatastore],
    *,
    params: dict[str, Any],
    host_moid: str,
) -> dict[str, Any] | None:
    """Resolve an already-mounted export / a name collision, else ``None`` (proceed to write).

    Export identity wins first: if any datastore already mounts this
    ``(nfs_server, remote_path)`` → ``already_mounted`` with its moid (the
    idempotent success). Otherwise, if the requested ``datastore_name`` is
    taken by a **different** export → ``name_conflict``. ``None`` means no
    match — the caller issues ``CreateNasDatastore``.
    """
    nfs_server = params["nfs_server"]
    remote_path = params["remote_path"]
    datastore_name = params["datastore_name"]
    for ds in datastores:
        if _matches_export(ds, nfs_server, remote_path):
            return _already_mounted_result(ds, params=params, host_moid=host_moid)
    for ds in datastores:
        if ds.name is not None and ds.name == datastore_name:
            return _name_conflict_result(ds, datastore_name=datastore_name, host_moid=host_moid)
    return None


def _already_mounted_result(
    ds: NasDatastore, *, params: dict[str, Any], host_moid: str
) -> dict[str, Any]:
    nfs_server = params["nfs_server"]
    remote_path = params["remote_path"]
    return {
        "status": STATUS_ALREADY_MOUNTED,
        "host": host_moid,
        "datastore": ds.moid,
        "summary": {
            "datastore": ds.moid,
            "name": ds.name,
            "nfs_server": nfs_server,
            "remote_path": remote_path,
            "access_mode": params.get("access_mode", "readWrite"),
            "type": ds.nas_type or params.get("nfs_type", "NFS"),
        },
        "guidance": (
            f"NFS export {nfs_server}:{remote_path} is already mounted on host "
            f"{host_moid} as datastore {ds.name!r} ({ds.moid}); no change made "
            "(idempotent re-mount)."
        ),
    }


def _name_conflict_result(
    ds: NasDatastore, *, datastore_name: str, host_moid: str
) -> dict[str, Any]:
    return {
        "status": STATUS_NAME_CONFLICT,
        "host": host_moid,
        "datastore": ds.moid,
        "summary": {
            "datastore": ds.moid,
            "name": ds.name,
            "nfs_server": ds.remote_host,
            "remote_path": ds.remote_path,
            "access_mode": None,
            "type": ds.nas_type,
        },
        "guidance": (
            f"datastore name {datastore_name!r} is already in use on host "
            f"{host_moid} for a different NFS export ({ds.remote_host}:{ds.remote_path}); "
            "pick a different datastore_name or unmount the existing datastore first."
        ),
    }


def build_nas_volume_spec(params: dict[str, Any]) -> dict[str, Any]:
    """The tagged ``HostNasVolumeSpec`` the ``CreateNasDatastore`` write carries."""
    return {
        VIM_TYPE_NAME_KEY: _HOST_NAS_VOLUME_SPEC_TYPE,
        "remoteHost": params["nfs_server"],
        "remotePath": params["remote_path"],
        "localPath": params["datastore_name"],
        "accessMode": params.get("access_mode", "readWrite"),
        "type": params.get("nfs_type", "NFS"),
    }


def build_mounted_result(
    *, host_moid: str, payload: Any, params: dict[str, Any], spec: dict[str, Any]
) -> dict[str, Any]:
    """The ``status="mounted"`` envelope from the write's returned Datastore MoRef."""
    datastore = unwrap_vim_value(payload)
    datastore_moid = datastore.get("value") if isinstance(datastore, dict) else None
    return {
        "status": "mounted",
        "host": host_moid,
        "datastore": datastore_moid,
        "summary": {
            "datastore": datastore_moid,
            "name": params["datastore_name"],
            "nfs_server": params["nfs_server"],
            "remote_path": params["remote_path"],
            "access_mode": spec["accessMode"],
            "type": spec["type"],
        },
        "guidance": None,
    }


def is_already_exists_fault(exc: BaseException) -> bool:
    """True when a ``CreateNasDatastore`` failure is the already-exists fault class.

    Cross-transport, because the two write seams surface a vim fault
    differently: the vCenter VI-JSON write raises :class:`httpx.HTTPStatusError`
    (HTTP 500, SOAP-shaped body → :func:`~...soap.parse_soap_fault`), while the
    standalone-ESXi SOAP write raises :class:`RuntimeError` already carrying the
    fault-type localName in its ``vim fault <Type>`` message
    (``connector._soap_fault_error``). Either ``DuplicateName`` or
    ``AlreadyExists`` (CreateNasDatastore documents both) counts; anything else
    returns ``False`` so the caller re-raises unchanged.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        fault = parse_soap_fault(exc.response.text)
        return fault is not None and fault.fault_type in _ALREADY_EXISTS_FAULT_TYPES
    if isinstance(exc, RuntimeError):
        text = str(exc)
        return any(f"vim fault {fault_type}" in text for fault_type in _ALREADY_EXISTS_FAULT_TYPES)
    return False
