# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vim control-plane calls for the ``HttpNfcLease`` OVF import (#3229).

The vim25 methods the import orchestrator (:mod:`.ovf_import`) drives over
the VI-JSON seam ``_post_vmomi_json`` (#2466): resolve the ``OvfManager`` +
``rootFolder`` off ``ServiceContent``, ``CreateImportSpec`` (a read that
validates the descriptor and returns the ``importSpec`` + disk ``fileItem``
list), the governed ``ImportVApp`` write (returns the ``HttpNfcLease``), the
lease-state ready-poll, and the ``Complete`` / ``Abort`` lifecycle.

Every request DataObject carries its ``_typeName`` discriminator (#3103).
The ``importSpec`` from ``CreateImportSpec`` is round-tripped **verbatim**
into ``ImportVApp`` -- it is a server-issued ``Any``-typed DataObject that
already carries its annotations, so it is never passed through
:func:`unwrap_vim_value` (which would strip the tags an ``Any`` request
field needs). Only pre-9.0 fields are read, so the surface is version-
agnostic across the VCF 5.x migration-source fleet (#3056) and 9.0+.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    retrieve_properties_body,
    unwrap_vim_value,
    vim_moref,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.ovf_import import ImportPlacement
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

# Governance op_ids (canonical ``METHOD:/{moId}`` keys the ingest parser emits
# from vi-json.yaml). ``_write`` re-exports these in its
# ``_VIM_SUB_OPS_VM_IMPORT_FROM_LIBRARY`` manifest for the l2 reconcile lane.
OP_RETRIEVE_SERVICE_CONTENT = "POST:/ServiceInstance/{moId}/RetrieveServiceContent"
OP_CREATE_IMPORT_SPEC = "POST:/OvfManager/{moId}/CreateImportSpec"
OP_IMPORT_VAPP = "POST:/ResourcePool/{moId}/ImportVApp"
OP_LEASE_PROGRESS = "POST:/HttpNfcLease/{moId}/HttpNfcLeaseProgress"
OP_LEASE_COMPLETE = "POST:/HttpNfcLease/{moId}/HttpNfcLeaseComplete"
OP_LEASE_ABORT = "POST:/HttpNfcLease/{moId}/HttpNfcLeaseAbort"
OP_RETRIEVE_PROPERTIES = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"

# vim ``_typeName`` discriminators the engine emits in request bodies. Every
# value names a component schema in the pinned vi-json.yaml (grounded by the
# ``_EMITTED_VIM_TYPE_NAMES`` reconcile lane).
OVF_CREATE_IMPORT_SPEC_PARAMS_TYPE = "OvfCreateImportSpecParams"
OVF_NETWORK_MAPPING_TYPE = "OvfNetworkMapping"
KEY_VALUE_TYPE = "KeyValue"

# Well-known bootstrap moid: the ServiceInstance singleton every vim client
# starts from (govmomi's ``vim25.ServiceInstance``). The OvfManager +
# rootFolder morefs are read off its ServiceContent, not guessed.
_SERVICE_INSTANCE_MOID = "ServiceInstance"
# The PropertyCollector singleton moid the lease-state poll reads through.
_PROPERTY_COLLECTOR_MOID = "propertyCollector"

_MO_TYPE_HTTP_NFC_LEASE = "HttpNfcLease"
_MO_TYPE_NETWORK = "Network"
_MO_TYPE_RESOURCE_POOL = "ResourcePool"
_MO_TYPE_FOLDER = "Folder"
_MO_TYPE_HOST_SYSTEM = "HostSystem"
_MO_TYPE_DATASTORE = "Datastore"

# ``HttpNfcLeaseState_enum`` values. ``initializing`` is the non-terminal state
# the ready-poll waits through; ``ready`` unblocks the transfer.
LEASE_STATE_READY = "ready"
LEASE_STATE_ERROR = "error"

# Default lease-ready wall-clock bound + cadence (module-global so tests zero
# them), mirroring the ``poll_vim_task`` 600s / 2s convention.
_LEASE_READY_TIMEOUT_SECONDS = 600.0
_LEASE_READY_POLL_INTERVAL = 2.0


def issue(category: str, severity: str, message: str) -> dict[str, Any]:
    """Build one ``{category, severity, message}`` issue projection."""
    return {"category": category, "severity": severity, "message": message}


def fault_text(fault: Any) -> str:
    """Best human-readable text from a vim ``MethodFault`` / ``LocalizedMethodFault``.

    Mirrors :func:`vim_task._fault_message`'s fallback order:
    ``localizedMessage`` -> joined ``faultMessage[*].message`` -> the fault's
    ``_typeName`` concrete class -- so a genuine placement / OVF fault reaches
    the operator as text, never a bare ``<no message>``.
    """
    if not isinstance(fault, dict):
        return "<no fault reported>"
    localized = fault.get("localizedMessage")
    if isinstance(localized, str) and localized.strip():
        return localized
    body = fault.get("fault") if isinstance(fault.get("fault"), dict) else fault
    messages = body.get("faultMessage") if isinstance(body, dict) else None
    if isinstance(messages, list):
        texts = [
            m
            for entry in messages
            if isinstance(entry, dict) and isinstance(m := entry.get("message"), str) and m.strip()
        ]
        if texts:
            return "; ".join(texts)
    type_name = body.get("_typeName") if isinstance(body, dict) else None
    return type_name if isinstance(type_name, str) and type_name.strip() else "<no fault reported>"


def _cisp_body(placement: ImportPlacement) -> dict[str, Any]:
    """Build the annotated ``OvfCreateImportSpecParams`` (``cisp``) DataObject.

    ``entityName`` is the one required field (empty string lets vCenter fall
    back to the descriptor's product name). ``networkMapping`` /
    ``propertyMapping`` fold the operator's OVF-key maps into annotated
    ``OvfNetworkMapping`` / ``KeyValue`` DataObjects; ``diskProvisioning`` is
    passed through only when set.
    """
    cisp: dict[str, Any] = {
        VIM_TYPE_NAME_KEY: OVF_CREATE_IMPORT_SPEC_PARAMS_TYPE,
        "entityName": placement.entity_name,
    }
    if placement.network_mappings:
        cisp["networkMapping"] = [
            {
                VIM_TYPE_NAME_KEY: OVF_NETWORK_MAPPING_TYPE,
                "name": str(ovf_key),
                "network": vim_moref(_MO_TYPE_NETWORK, str(moid)),
            }
            for ovf_key, moid in placement.network_mappings.items()
        ]
    if placement.ovf_properties:
        cisp["propertyMapping"] = [
            {VIM_TYPE_NAME_KEY: KEY_VALUE_TYPE, "key": str(k), "value": str(v)}
            for k, v in placement.ovf_properties.items()
        ]
    if placement.disk_provisioning:
        cisp["diskProvisioning"] = placement.disk_provisioning
    return cisp


async def retrieve_service_content(
    connector: VmwareRestConnector, target: VsphereTargetLike, operator: Operator
) -> tuple[str, str]:
    """Return ``(ovf_manager_moid, root_folder_moid)`` off the ServiceContent.

    Reads ``ServiceInstance.RetrieveServiceContent`` -- the documented way to
    resolve the singleton managed objects (their moids are not guessed). The
    boxed ``Any`` values normalise through :func:`unwrap_vim_value`.
    """
    path = f"/ServiceInstance/{_SERVICE_INSTANCE_MOID}/RetrieveServiceContent"
    raw = await connector._post_vmomi_json(target, path, operator=operator, json={})
    content = unwrap_vim_value(raw)
    if not isinstance(content, dict):
        raise RuntimeError(f"RetrieveServiceContent returned no ServiceContent ({type(raw)})")
    ovf_ref = content.get("ovfManager")
    folder_ref = content.get("rootFolder")
    ovf_moid = ovf_ref.get("value") if isinstance(ovf_ref, dict) else None
    folder_moid = folder_ref.get("value") if isinstance(folder_ref, dict) else None
    if not isinstance(ovf_moid, str) or not isinstance(folder_moid, str):
        raise RuntimeError("ServiceContent missing ovfManager / rootFolder MoRef")
    return ovf_moid, folder_moid


async def create_import_spec(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    ovf_manager_moid: str,
    descriptor: str,
    placement: ImportPlacement,
) -> dict[str, Any]:
    """Call ``OvfManager.CreateImportSpec`` and return the raw result dict.

    A read (``System.View``) -- validates the descriptor and returns the
    ``importSpec`` (round-tripped verbatim into ``ImportVApp``) plus the
    ``fileItem`` upload list. The result is **not** wholesale-unwrapped:
    ``importSpec`` must keep its ``_typeName`` tags for the round-trip; the
    ``fileItem`` / ``error`` fields are read box-tolerantly at their own sites.
    """
    body = {
        "ovfDescriptor": descriptor,
        "resourcePool": vim_moref(_MO_TYPE_RESOURCE_POOL, placement.resource_pool),
        "datastore": vim_moref(_MO_TYPE_DATASTORE, placement.datastore),
        "cisp": _cisp_body(placement),
    }
    path = f"/OvfManager/{ovf_manager_moid}/CreateImportSpec"
    result = await connector._post_vmomi_json(target, path, operator=operator, json=body)
    if not isinstance(result, dict):
        raise RuntimeError(
            f"CreateImportSpec returned no OvfCreateImportSpecResult ({type(result)})"
        )
    return result


def spec_errors(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``OvfCreateImportSpecResult.error`` into issue projections."""
    errors = unwrap_vim_value(result.get("error"))
    if not isinstance(errors, list):
        return []
    return [issue("descriptor", "error", fault_text(e)) for e in errors if isinstance(e, dict)]


def spec_warnings(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``OvfCreateImportSpecResult.warning`` into issue projections."""
    warnings = unwrap_vim_value(result.get("warning"))
    if not isinstance(warnings, list):
        return []
    return [issue("descriptor", "warning", fault_text(w)) for w in warnings if isinstance(w, dict)]


def file_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ``OvfFileItem`` list (box-tolerant) from a CreateImportSpec result."""
    items = unwrap_vim_value(result.get("fileItem"))
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


async def import_vapp(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    resource_pool: str,
    folder_moid: str,
    host: str | None,
    import_spec: Any,
    gate: Callable[[dict[str, Any]], Awaitable[OperationResult | None]],
) -> tuple[OperationResult | None, Any]:
    """Gate then call ``ResourcePool.ImportVApp``; return ``(gate, lease_moref)``.

    The one governed *write* -- it creates the inventory objects. The gate
    parks / denies for an agent principal like every other composite write;
    when it clears, ``ImportVApp`` returns the ``HttpNfcLease`` MoRef the
    transfer drives. ``import_spec`` is passed **verbatim** (never unwrapped)
    so its server-issued ``_typeName`` annotations survive the round-trip.
    """
    params = {"resource_pool": resource_pool, "folder": folder_moid, "spec": "<import-spec>"}
    parked = await gate(params)
    if parked is not None:
        return parked, None
    body: dict[str, Any] = {
        "spec": import_spec,
        "folder": vim_moref(_MO_TYPE_FOLDER, folder_moid),
    }
    if host:
        body["host"] = vim_moref(_MO_TYPE_HOST_SYSTEM, host)
    path = f"/ResourcePool/{resource_pool}/ImportVApp"
    lease = await connector._post_vmomi_json(target, path, operator=operator, json=body)
    return None, unwrap_vim_value(lease)


async def _read_lease_props(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    lease_moid: str,
    props: list[str],
) -> dict[str, Any]:
    """Read named ``HttpNfcLease`` properties via RetrievePropertiesEx.

    Returns ``{prop_name: value}`` (box-normalised). Absent properties are
    simply missing from the map -- the poll treats that as "not ready yet".
    """
    body = retrieve_properties_body(_MO_TYPE_HTTP_NFC_LEASE, [lease_moid], props)
    path = f"/PropertyCollector/{_PROPERTY_COLLECTOR_MOID}/RetrievePropertiesEx"
    raw = await connector._post_vmomi_json(target, path, operator=operator, json=body)
    payload = unwrap_vim_value(raw)
    objects = payload.get("objects") if isinstance(payload, dict) else None
    if not isinstance(objects, list):
        return {}
    out: dict[str, Any] = {}
    for obj in objects:
        prop_set = obj.get("propSet", []) if isinstance(obj, dict) else []
        for prop in prop_set or []:
            if isinstance(prop, dict) and isinstance(prop.get("name"), str):
                out[prop["name"]] = prop.get("val")
    return out


async def poll_lease_ready(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    lease_moid: str,
    timeout_seconds: float = _LEASE_READY_TIMEOUT_SECONDS,
    poll_interval: float = _LEASE_READY_POLL_INTERVAL,
) -> tuple[str, dict[str, Any]]:
    """Poll ``HttpNfcLease.state`` to ``ready`` / ``error`` / timeout.

    Returns ``(outcome, info)`` where outcome is ``"ready"`` (``info`` is the
    ``HttpNfcLeaseInfo`` carrying ``deviceUrl`` + ``entity``), ``"error"``
    (``info`` carries the lease fault under ``"error"``), or ``"timeout"``
    (``info`` empty). Wall-clock bound, mirroring ``poll_vim_task``.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        props = await _read_lease_props(
            connector, target, operator, lease_moid=lease_moid, props=["state", "info", "error"]
        )
        state = props.get("state")
        if state == LEASE_STATE_READY:
            info = props.get("info")
            return LEASE_STATE_READY, info if isinstance(info, dict) else {}
        if state == LEASE_STATE_ERROR:
            return LEASE_STATE_ERROR, {"error": props.get("error")}
        if time.monotonic() >= deadline:
            return "timeout", {}
        await asyncio.sleep(poll_interval)


async def lease_complete(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    lease_moid: str,
) -> None:
    """``HttpNfcLeaseComplete`` -- releases the lease + marks the import a success."""
    path = f"/HttpNfcLease/{lease_moid}/HttpNfcLeaseComplete"
    await connector._post_vmomi_json(target, path, operator=operator, json={})


async def abort_lease(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    lease_moid: str,
) -> None:
    """Best-effort ``HttpNfcLeaseAbort`` -- vCenter then removes the half-import.

    Swallows every fault: abort runs on the failure path and must never mask
    the original error with a secondary one.
    """
    with contextlib.suppress(Exception):
        path = f"/HttpNfcLease/{lease_moid}/HttpNfcLeaseAbort"
        await connector._post_vmomi_json(target, path, operator=operator, json={})


def lease_moid(lease: Any) -> str | None:
    """Pull the moid out of the ``HttpNfcLease`` MoRef ``ImportVApp`` returned."""
    if isinstance(lease, dict):
        value = lease.get("value")
        return value if isinstance(value, str) else None
    return lease if isinstance(lease, str) else None


def entity_from_info(info: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(vm_moid, resource_type)`` from ``HttpNfcLeaseInfo.entity``."""
    entity = info.get("entity")
    if not isinstance(entity, dict):
        return None, None
    value = entity.get("value")
    kind = entity.get("type")
    return (
        value if isinstance(value, str) else None,
        kind if isinstance(kind, str) else None,
    )
