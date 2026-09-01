# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed ``HttpNfcLease`` OVF import orchestrator (#3229).

The durable, transfer-window-decoupled counterpart to the synchronous
``vmware.composite.vm.deploy_from_library`` REST deploy. That deploy holds
one HTTP POST open for the whole server-side disk copy, so completion is
bounded by the client read-timeout rather than the operation's real
duration; a multi-GB installer OVA to an NFS datastore outran even the 3 h
mitigation ceiling live (#3176). The stopgap PR #3234 raised that ceiling +
established the async-dispatch convention; this is the durable fix it
deferred.

This module ties the pieces together; the mechanics live beside it:

* :mod:`.ovf_import_control` -- the vim25 control-plane calls (over the
  VI-JSON seam ``_post_vmomi_json`` / #2466): ``RetrieveServiceContent`` ->
  ``CreateImportSpec`` -> the governed ``ImportVApp`` write -> lease-ready
  poll -> ``Complete`` / ``Abort``.
* :mod:`.ovf_transfer` -- the net-new streaming machinery: each disk streams
  from the source straight to its lease device URL with a background
  ``HttpNfcLeaseProgress`` heartbeat, so the transfer is bounded only by its
  own duration (AC1) and reports percent (AC2).

The flow: resolve the ``OvfManager`` + ``rootFolder`` morefs, read the OVF
descriptor from the source, ``CreateImportSpec`` (a descriptor error short-
circuits to ``import_failed`` before any mutation), gate + ``ImportVApp``
(the one governed write; a parked / denied gate returns the
``OperationResult`` verbatim), poll the lease to ``ready``, stream every disk
matched to its device URL, then ``Complete``. Any failure after the lease
exists aborts it, so vCenter removes the half-created inventory objects and a
failed import never leaves a partial VM behind.

The engine is governance-agnostic: the ``ImportVApp`` gate is injected, so
the composite layer owns the ``enforce_subop_policy`` posture and the engine
stays a pure mechanism (unit-testable with a fake source + recording
connector). The byte source is abstracted behind :class:`.ovf_transfer.OvfSource`;
:mod:`.library_download` supplies the content-library implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors.vmware_rest import ovf_import_control as control
from meho_backplane.connectors.vmware_rest import ovf_transfer as transfer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.ovf_transfer import OvfSource
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "ImportPlacement",
    "LeaseImportResult",
    "import_ovf_from_source",
]

# Default heartbeat cadence forwarded to the transfer (module-global so a test
# can shrink it without reaching into the transfer module).
_PROGRESS_HEARTBEAT_INTERVAL = 30.0


@dataclass(frozen=True)
class ImportPlacement:
    """Resolved placement + descriptor-mapping inputs for one import."""

    resource_pool: str
    datastore: str
    entity_name: str
    host: str | None = None
    folder: str | None = None
    network_mappings: dict[str, str] = field(default_factory=dict)
    ovf_properties: dict[str, str] = field(default_factory=dict)
    disk_provisioning: str | None = None


@dataclass(frozen=True)
class LeaseImportResult:
    """Terminal outcome of a lease import, mapped to the deploy envelope family.

    ``status`` is one of ``imported`` / ``import_failed`` (the OVF descriptor
    was rejected by ``CreateImportSpec``) / ``import_error`` (a vim control-
    plane or disk-upload call faulted) / ``lease_error`` (the lease reached the
    ``error`` state) / ``lease_timeout`` (the lease never reached ``ready``).
    ``vm_id`` / ``resource_type`` are set only on ``imported``. ``issues``
    carries the ``{category, severity, message}`` projections; ``transfer`` the
    per-disk manifest.
    """

    status: str
    vm_id: str | None = None
    resource_type: str | None = None
    issues: list[dict[str, Any]] = field(default_factory=list)
    transfer: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _ImportSpec:
    """The CreateImportSpec output the rest of the import needs."""

    root_folder_moid: str
    import_spec: Any
    warnings: list[dict[str, Any]]
    file_items: list[dict[str, Any]]


async def _create_import_spec(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    source: OvfSource,
    placement: ImportPlacement,
) -> _ImportSpec | LeaseImportResult:
    """Resolve ServiceContent + descriptor + CreateImportSpec.

    Returns the :class:`_ImportSpec` to proceed with, or an ``import_failed``
    :class:`LeaseImportResult` when the descriptor is rejected (no mutation yet).
    """
    ovf_manager_moid, root_folder_moid = await control.retrieve_service_content(
        connector, target, operator
    )
    descriptor = await source.read_descriptor()
    spec_result = await control.create_import_spec(
        connector,
        target,
        operator,
        ovf_manager_moid=ovf_manager_moid,
        descriptor=descriptor,
        placement=placement,
    )
    errors = control.spec_errors(spec_result)
    if errors:
        return LeaseImportResult(status="import_failed", issues=errors)
    return _ImportSpec(
        root_folder_moid=root_folder_moid,
        import_spec=spec_result.get("importSpec"),
        warnings=control.spec_warnings(spec_result),
        file_items=control.file_items(spec_result),
    )


async def import_ovf_from_source(
    *,
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    source: OvfSource,
    placement: ImportPlacement,
    gate: Callable[[dict[str, Any]], Awaitable[OperationResult | None]],
    lease_ready_timeout: float | None = None,
    heartbeat_interval: float = _PROGRESS_HEARTBEAT_INTERVAL,
) -> LeaseImportResult | OperationResult:
    """Drive a full ``HttpNfcLease`` OVF import from *source* to a new VM.

    Returns a :class:`LeaseImportResult` (the composite maps it to the deploy
    envelope) or, when the ``ImportVApp`` gate parks / denies, the
    :class:`OperationResult` verbatim.
    """
    spec = await _create_import_spec(
        connector, target, operator, source=source, placement=placement
    )
    if isinstance(spec, LeaseImportResult):
        return spec

    parked, lease = await control.import_vapp(
        connector,
        target,
        operator,
        resource_pool=placement.resource_pool,
        folder_moid=placement.folder or spec.root_folder_moid,
        host=placement.host,
        import_spec=spec.import_spec,
        gate=gate,
    )
    if parked is not None:
        return parked
    lease_moid = control.lease_moid(lease)
    if lease_moid is None:
        return LeaseImportResult(
            status="import_error",
            issues=[
                *spec.warnings,
                control.issue("lease", "error", "ImportVApp returned no lease"),
            ],
        )

    ready_kwargs = {} if lease_ready_timeout is None else {"timeout_seconds": lease_ready_timeout}
    outcome, info = await control.poll_lease_ready(
        connector, target, operator, lease_moid=lease_moid, **ready_kwargs
    )
    if outcome != control.LEASE_STATE_READY:
        return await _finalize_non_ready(
            connector,
            target,
            operator,
            lease_moid=lease_moid,
            outcome=outcome,
            info=info,
            warnings=spec.warnings,
        )
    return await _run_transfer(
        connector,
        target,
        operator,
        lease_moid=lease_moid,
        info=info,
        file_items=spec.file_items,
        source=source,
        warnings=spec.warnings,
        heartbeat_interval=heartbeat_interval,
    )


async def _finalize_non_ready(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    lease_moid: str,
    outcome: str,
    info: dict[str, Any],
    warnings: list[dict[str, Any]],
) -> LeaseImportResult:
    """Map a lease that reached ``error`` / timed out to a structured result."""
    if outcome == control.LEASE_STATE_ERROR:
        message = control.fault_text(info.get("error"))
        return LeaseImportResult(
            status="lease_error", issues=[*warnings, control.issue("lease", "error", message)]
        )
    await control.abort_lease(connector, target, operator, lease_moid=lease_moid)
    return LeaseImportResult(
        status="lease_timeout",
        issues=[*warnings, control.issue("lease", "error", "lease never reached the ready state")],
    )


async def _run_transfer(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    lease_moid: str,
    info: dict[str, Any],
    file_items: list[dict[str, Any]],
    source: OvfSource,
    warnings: list[dict[str, Any]],
    heartbeat_interval: float,
) -> LeaseImportResult:
    """Stream the disks, complete the lease, and build the ``imported`` result.

    The disk plan matches ``CreateImportSpec``'s ``fileItem`` list to the
    lease ``deviceUrl`` map (by import key). Any upload / control-plane fault
    aborts the lease and returns a structured ``import_error`` -- vCenter then
    removes the half-created VM, so a failed transfer never leaves a partial
    import behind.
    """
    raw_device_urls = info.get("deviceUrl")
    device_urls: list[dict[str, Any]] = raw_device_urls if isinstance(raw_device_urls, list) else []
    connect_host = str(getattr(target, "host", "") or "")
    plan = transfer.plan_transfers(file_items, device_urls, connect_host)
    try:
        manifest = await transfer.transfer_all(
            connector,
            target,
            operator,
            lease_moid=lease_moid,
            plan=plan,
            source=source,
            heartbeat_interval=heartbeat_interval,
        )
        await control.lease_complete(connector, target, operator, lease_moid=lease_moid)
    except httpx.HTTPError as exc:
        await control.abort_lease(connector, target, operator, lease_moid=lease_moid)
        return LeaseImportResult(
            status="import_error",
            issues=[*warnings, control.issue("transfer", "error", f"disk upload faulted: {exc}")],
        )
    vm_id, resource_type = control.entity_from_info(info)
    return LeaseImportResult(
        status="imported",
        vm_id=vm_id,
        resource_type=resource_type,
        issues=warnings,
        transfer=manifest,
    )
