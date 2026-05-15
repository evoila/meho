# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: 8 protocol-driven composite handlers for the
# vSphere REST write surface ship in one module per the issue body's
# design; splitting them by group would scatter the shared sub-op_id
# constants + helpers across files for no readability gain. Each
# handler's body is the documented orchestration workflow from
# #509's spec.

"""Write-shaped ``vmware.composite.*`` handler functions (8 composites).

Companion to :mod:`._read`. Same handler-shape contract -- each handler
is a module-level ``async def`` taking the dispatcher's composite-branch
keyword args ``(operator, target, params, dispatch_child)`` and
returning a single aggregated dict via ``dispatch_child`` calls to
2-N typed sub-ops (or, for :func:`host_evacuate_composite`, recursive
calls into another vmware.composite).

The 8 composites this module ships (G3.1-T6 / #509):

* :func:`vm_create_composite` -- folder lookup -> ``POST:/vcenter/vm``
  -> NIC attach loop -> optional power-on. Partial-failure rollback
  via ``DELETE:/vcenter/vm/{vm}``.
* :func:`vm_clone_composite` -- content-library deploy with task
  polling. Long-running; ``wait_for_completion=False`` returns the
  task id immediately.
* :func:`vm_snapshot_revert_composite` -- list -> match by name ->
  revert. Idempotent; ambiguity-rejection on name collision.
* :func:`vm_migrate_composite` -- DRS recommendation lookup ->
  relocate. ``target_host`` overrides DRS.
* :func:`vm_power_bulk_composite` -- filter -> fan-out power action.
  Per-VM partial-failure tolerated by default.
* :func:`host_evacuate_composite` -- list VMs on host -> recursive
  :func:`vm_migrate_composite` per VM -> maintenance-enter. First
  production composite that calls another composite via
  ``dispatch_child``.
* :func:`host_detach_from_vds_composite` -- per-VM NIC migration to
  a fallback network -> DVS host-detach. Refuses detach when any NIC
  migration failed.
* :func:`cluster_patch_composite` -- sequential per-host maintenance
  + patch + exit. Stops the loop on the first per-host failure.

Every sub-op_id below is the canonical ``METHOD:/path`` key produced
by :func:`~meho_backplane.operations.ingest.openapi.parse_openapi`
from the ingested ``vcenter.yaml`` (G3.1-T2 / #408) and
``vi-json.yaml`` (G3.1-T3 / #503). The path strings come from
inspecting the canonical ``GOVC_PARITY_BENCHMARK`` tuple at
``backend/tests/acceptance/test_g07_vsphere_canary.py`` and the
vSphere REST URL anchors in #509's issue body -- never guessed.

Each composite returns a structured ``{"status": ...}`` envelope so
callers can branch on ``status`` without parsing free-form prose. The
status enums are listed on each composite's ``response_schema`` in
:mod:`.schemas`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.operations.composite import DispatchChild

__all__ = [
    "cluster_patch_composite",
    "host_detach_from_vds_composite",
    "host_evacuate_composite",
    "vm_clone_composite",
    "vm_create_composite",
    "vm_migrate_composite",
    "vm_power_bulk_composite",
    "vm_snapshot_revert_composite",
]


# Connector_id every write composite dispatches sub-ops against.
_CONNECTOR_ID = "vmware-rest-9.0"

# vCenter REST op_ids (canonical METHOD:/path keys from vcenter.yaml).
#
# Action-bearing endpoints. vCenter's OpenAPI spec models POST-with-side-effect
# endpoints as ``/<path>?action=<verb>`` — i.e. the action verb is part of the
# path key, not a body parameter. The ingestion pipeline preserves the query
# verbatim, so the canonical ``op_id`` for "power on a VM" is
# ``POST:/vcenter/vm/{vm}/power?action=start`` (not ``POST:/vcenter/vm/{vm}/power``
# with ``action=start`` in body params — that op_id is not a descriptor row).
# These constants therefore embed the action verb. Endpoints whose action verb
# is operator-chosen (power start/stop, maintenance enter/exit) build the op_id
# per-call via :func:`_power_vm_op_id` / :func:`_host_maintenance_op_id`.
_OP_LIST_FOLDERS = "GET:/vcenter/folder"
_OP_LIST_VMS = "GET:/vcenter/vm"
_OP_GET_VM = "GET:/vcenter/vm/{vm}"
_OP_CREATE_VM = "POST:/vcenter/vm"
_OP_DELETE_VM = "DELETE:/vcenter/vm/{vm}"
_OP_ATTACH_VM_NIC = "PATCH:/vcenter/vm/{vm}/network"
_OP_RELOCATE_VM = "POST:/vcenter/vm/{vm}?action=relocate"
_OP_LIST_VM_SNAPSHOTS = "GET:/vcenter/vm/{vm}/snapshot"
_OP_REVERT_VM_SNAPSHOT = "POST:/vcenter/vm/{vm}/snapshot/{snap}?action=revert"
_OP_LIST_CLUSTER_HOSTS = "GET:/vcenter/cluster/{cluster}/host"
_OP_GET_DRS_RECOMMENDATIONS = "GET:/vcenter/cluster/{cluster}/drs/recommendations"
_OP_DEPLOY_LIBRARY_VM = "POST:/vcenter/vm-template/library-items?action=deploy"
_OP_GET_TASK = "GET:/cis/tasks/{task}"
_OP_HOST_PATCH = "POST:/vcenter/host/{host}?action=patch"
_OP_LIST_PORTGROUPS = "GET:/vcenter/network/distributed-portgroup"
_OP_REMOVE_DVS_HOST = "POST:/vcenter/network/dvs/{dvs}?action=remove_host"


def _power_vm_op_id(action: str) -> str:
    """Build the per-action canonical op_id for ``POST:/vcenter/vm/{vm}/power``.

    vCenter exposes ``start`` / ``stop`` / ``suspend`` / ``reset`` as four
    distinct descriptor rows under the ``?action=<verb>`` discriminator. The
    composite picks the row at call time so each action lands on its own
    audit row + policy evaluation, not as a free-form ``action`` parameter
    on a single shared op_id (which wouldn't resolve against an ingested row).
    """
    return f"POST:/vcenter/vm/{{vm}}/power?action={action}"


def _host_maintenance_op_id(action: str) -> str:
    """Build the per-action canonical op_id for ``PATCH:/vcenter/host/{host}/maintenance``.

    Maintenance enter / exit are two descriptor rows under ``?action=enter`` /
    ``?action=exit``; same reasoning as :func:`_power_vm_op_id`.
    """
    return f"PATCH:/vcenter/host/{{host}}/maintenance?action={action}"


# Recursive composite sub-op_id (host.evacuate -> vm.migrate).
_OP_COMPOSITE_VM_MIGRATE = "vmware.composite.vm.migrate"


def _unwrap_value(payload: Any) -> Any:
    """Return the inner ``value`` field on a pre-7 envelope, else *payload*."""
    if isinstance(payload, dict) and set(payload.keys()) == {"value"}:
        return payload["value"]
    return payload


def _require_ok(result: OperationResult) -> Any:
    """Return :attr:`OperationResult.result` or raise on a non-OK status."""
    if result.status != "ok":
        raise RuntimeError(
            f"composite sub-op {result.op_id!r} returned status="
            f"{result.status!r}: {result.error or '<no error message>'}"
        )
    return result.result


def _rolled_back(
    *,
    steps: list[str],
    failed_step: str,
    reason: str,
) -> dict[str, Any]:
    """Build the canonical rolled_back response envelope for :func:`vm_create_composite`."""
    return {
        "status": "rolled_back",
        "vm_id": None,
        "steps_succeeded": steps,
        "failed_step": failed_step,
        "rollback_reason": reason,
    }


# ===========================================================================
# vm.create
# ===========================================================================


async def _resolve_folder_moid(
    *, dispatch_child: DispatchChild, folder_name: str
) -> tuple[str | None, str | None]:
    """Look up a folder moid by display name.

    Returns ``(moid, None)`` on success or ``(None, reason)`` on
    failure -- the caller folds the reason into a ``rolled_back``
    envelope. Failure modes: empty match list, listing row missing
    the ``folder`` key.
    """
    folder_listing = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_FOLDERS,
            params={"filter.names": [folder_name]},
        )
    )
    folder_entries = _unwrap_value(folder_listing)
    if not isinstance(folder_entries, list) or not folder_entries:
        return None, f"folder name {folder_name!r} did not resolve to any moid"
    first_entry = folder_entries[0]
    folder_moid_raw = first_entry.get("folder") if isinstance(first_entry, dict) else None
    if not isinstance(folder_moid_raw, str):
        return None, "folder listing row missing ``folder`` key"
    return folder_moid_raw, None


async def _dispatch_create_vm(
    *,
    dispatch_child: DispatchChild,
    folder_moid: str,
    name: str,
    guest_os: str,
    cpu_count: int,
    memory_mib: int,
) -> tuple[str | None, str | None]:
    """POST:/vcenter/vm; return ``(vm_id, None)`` or ``(None, error_reason)``."""
    create_result = await dispatch_child(
        connector_id=_CONNECTOR_ID,
        op_id=_OP_CREATE_VM,
        params={
            "spec": {
                "name": name,
                "guest_OS": guest_os,
                "placement": {"folder": folder_moid},
                "cpu": {"count": cpu_count},
                "memory": {"size_MiB": memory_mib},
            },
        },
    )
    if create_result.status != "ok":
        return None, (
            f"create returned status={create_result.status!r}: "
            f"{create_result.error or '<no error message>'}"
        )
    vm_id_raw = _unwrap_value(create_result.result)
    if not isinstance(vm_id_raw, str):
        return None, f"create returned non-string vm id payload: {type(vm_id_raw).__name__}"
    return vm_id_raw, None


async def _attach_vm_nics(
    *,
    dispatch_child: DispatchChild,
    vm_id: str,
    nics: list[dict[str, Any]],
) -> str | None:
    """Attach NICs one-by-one; return ``None`` on success or a failure reason."""
    for nic in nics:
        nic_result = await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ATTACH_VM_NIC,
            params={"vm": vm_id, "spec": nic},
        )
        if nic_result.status != "ok":
            return (
                f"nic attach for network={nic.get('network')!r} failed: "
                f"{nic_result.error or '<no error message>'}"
            )
    return None


async def _rollback_created_vm(*, dispatch_child: DispatchChild, vm_id: str) -> None:
    """Issue ``DELETE:/vcenter/vm/{vm}`` to remove a half-created VM.

    Best-effort: rollback failures are not surfaced -- the operator
    already knows the create flow failed. If the DELETE returns
    non-ok, the audit row of that sub-op records it for forensic
    review.
    """
    await dispatch_child(
        connector_id=_CONNECTOR_ID,
        op_id=_OP_DELETE_VM,
        params={"vm": vm_id},
    )


async def vm_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Create a VM with NIC attach + optional power-on; rollback on failure.

    Op-id: ``vmware.composite.vm.create``. See module docstring for the
    sub-op chain and rollback semantics.
    """
    folder_name = params["folder_name"]
    name = params["name"]
    guest_os = params["guest_os"]
    cpu_count = int(params.get("cpu_count", 1))
    memory_mib = int(params.get("memory_mib", 1024))
    nics: list[dict[str, Any]] = list(params.get("nics") or [])
    power_on = bool(params.get("power_on_after_create", False))

    steps: list[str] = []

    folder_moid, folder_err = await _resolve_folder_moid(
        dispatch_child=dispatch_child, folder_name=folder_name
    )
    if folder_moid is None:
        return _rolled_back(steps=steps, failed_step="folder_lookup", reason=folder_err or "")
    steps.append("folder_lookup")

    vm_id, create_err = await _dispatch_create_vm(
        dispatch_child=dispatch_child,
        folder_moid=folder_moid,
        name=name,
        guest_os=guest_os,
        cpu_count=cpu_count,
        memory_mib=memory_mib,
    )
    if vm_id is None:
        # Create failed; nothing to roll back.
        return _rolled_back(steps=steps, failed_step="create", reason=create_err or "")
    steps.append("create")

    nic_err = await _attach_vm_nics(dispatch_child=dispatch_child, vm_id=vm_id, nics=nics)
    if nic_err is not None:
        await _rollback_created_vm(dispatch_child=dispatch_child, vm_id=vm_id)
        return _rolled_back(steps=steps, failed_step="nic_attach", reason=nic_err)
    if nics:
        steps.append("nic_attach")

    if power_on:
        power_result = await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_power_vm_op_id("start"),
            params={"vm": vm_id},
        )
        if power_result.status != "ok":
            await _rollback_created_vm(dispatch_child=dispatch_child, vm_id=vm_id)
            return _rolled_back(
                steps=steps,
                failed_step="power_on",
                reason=f"power_on failed: {power_result.error or '<no error message>'}",
            )
        steps.append("power_on")

    return {
        "status": "created",
        "vm_id": vm_id,
        "steps_succeeded": steps,
        "failed_step": None,
        "rollback_reason": None,
    }


# ===========================================================================
# vm.clone
# ===========================================================================


def _extract_clone_task_id(deploy_payload: Any) -> str | None:
    """Pull the task id out of a deploy response in either canonical shape."""
    unwrapped = _unwrap_value(deploy_payload)
    if isinstance(unwrapped, dict):
        candidate = unwrapped.get("task") or unwrapped.get("value")
        if isinstance(candidate, str):
            return candidate
    elif isinstance(unwrapped, str):
        return unwrapped
    return None


def _extract_clone_vm_id(task_result_payload: Any) -> str | None:
    """Pull the new VM id out of a SUCCEEDED clone task's ``result`` field."""
    if isinstance(task_result_payload, str):
        return task_result_payload
    if isinstance(task_result_payload, dict):
        candidate = task_result_payload.get("vm") or task_result_payload.get("id")
        if isinstance(candidate, str):
            return candidate
    return None


async def _poll_clone_task(
    *,
    dispatch_child: DispatchChild,
    task_id: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Poll ``GET:/cis/tasks/{task}`` until SUCCEEDED/FAILED/timeout.

    Returns the completed-or-timeout response envelope directly (the
    composite's outer status enum). Raises on FAILED so the dispatcher
    can wrap as ``connector_error``.
    """
    deadline = time.monotonic() + timeout_seconds
    poll_interval = 1.0
    while time.monotonic() < deadline:
        task_payload = _require_ok(
            await dispatch_child(
                connector_id=_CONNECTOR_ID,
                op_id=_OP_GET_TASK,
                params={"task": task_id},
            )
        )
        task = _unwrap_value(task_payload)
        if isinstance(task, dict):
            status = task.get("status")
            if status == "SUCCEEDED":
                return {
                    "status": "completed",
                    "task_id": task_id,
                    "vm_id": _extract_clone_vm_id(task.get("result")),
                    "guidance": None,
                }
            if status == "FAILED":
                raise RuntimeError(
                    f"vm.clone: deploy task {task_id!r} reported FAILED: "
                    f"{task.get('error') or '<no error reported>'}"
                )
        await asyncio.sleep(poll_interval)

    return {
        "status": "timeout",
        "task_id": task_id,
        "vm_id": None,
        "guidance": (
            f"poll GET:/cis/tasks/{task_id} for final state -- the "
            f"composite gave up after {timeout_seconds}s"
        ),
    }


async def vm_clone_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Clone a VM from a content-library template; poll the deploy task.

    Op-id: ``vmware.composite.vm.clone``. Long-running -- blocks for
    up to ``timeout_seconds`` (default 600) when
    ``wait_for_completion=True``.
    """
    source_vm = params["source_vm"]
    target_name = params["target_name"]
    library_item = params["library_item"]
    wait_for_completion = bool(params.get("wait_for_completion", True))
    timeout_seconds = int(params.get("timeout_seconds", 600))

    # Source config drives CloneSpec; the read is a no-op when the
    # source VM lookup fails (RuntimeError surfaces upstream).
    _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_GET_VM,
            params={"vm": source_vm},
        )
    )

    deploy_payload = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_DEPLOY_LIBRARY_VM,
            params={
                "library_item": library_item,
                "spec": {"name": target_name},
            },
        )
    )
    task_id = _extract_clone_task_id(deploy_payload)
    if task_id is None:
        raise RuntimeError(f"vm.clone: deploy returned no task id (payload={deploy_payload!r})")

    if not wait_for_completion:
        return {
            "status": "pending",
            "task_id": task_id,
            "vm_id": None,
            "guidance": "poll GET:/cis/tasks/{task} for final state",
        }

    return await _poll_clone_task(
        dispatch_child=dispatch_child,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
    )


# ===========================================================================
# vm.snapshot.revert
# ===========================================================================


async def vm_snapshot_revert_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Revert a VM to a named snapshot. Idempotent; ambiguity-rejecting.

    Op-id: ``vmware.composite.vm.snapshot.revert``. Multiple snapshots
    sharing the name -> ``status='ambiguous'``; missing -> ``not_found``.
    Revert never dispatches on either.
    """
    vm_moid = params["vm"]
    snapshot_name = params["snapshot_name"]

    listing = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_VM_SNAPSHOTS,
            params={"vm": vm_moid},
        )
    )
    entries = _unwrap_value(listing)
    if not isinstance(entries, list):
        raise RuntimeError(
            f"vm.snapshot.revert: expected list from {_OP_LIST_VM_SNAPSHOTS!r}, "
            f"got {type(entries).__name__}"
        )
    matches = [e for e in entries if isinstance(e, dict) and e.get("name") == snapshot_name]
    if not matches:
        return {
            "status": "not_found",
            "snapshot_id": None,
            "candidates": [],
            "guidance": f"no snapshot named {snapshot_name!r} on vm {vm_moid!r}",
        }
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "snapshot_id": None,
            "candidates": matches,
            "guidance": (
                "multiple snapshots share the requested name -- pass "
                "``snapshot_id`` explicitly to disambiguate"
            ),
        }
    snapshot_moid = matches[0].get("snapshot")
    if not isinstance(snapshot_moid, str):
        return {
            "status": "not_found",
            "snapshot_id": None,
            "candidates": matches,
            "guidance": "matched snapshot row missing ``snapshot`` key",
        }
    _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_REVERT_VM_SNAPSHOT,
            params={"vm": vm_moid, "snap": snapshot_moid},
        )
    )
    return {
        "status": "reverted",
        "snapshot_id": snapshot_moid,
        "candidates": [],
        "guidance": None,
    }


# ===========================================================================
# vm.migrate
# ===========================================================================


def _pick_drs_target_host(recs: Any, vm_moid: str) -> str | None:
    """Walk a DRS recommendations payload for ``vm_moid``'s target host."""
    if not isinstance(recs, list):
        return None
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        if rec.get("vm") != vm_moid:
            continue
        candidate = rec.get("target_host") or rec.get("host")
        if isinstance(candidate, str):
            return candidate
    return None


async def vm_migrate_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Migrate a VM via DRS recommendation or explicit ``target_host``.

    Op-id: ``vmware.composite.vm.migrate``. ``target_host`` overrides
    the DRS lookup. No-recommendation path returns
    ``status='no_recommendation'`` so the caller can re-dispatch.
    """
    vm_moid = params["vm"]
    cluster_moid = params["cluster"]
    explicit_target = params.get("target_host")
    target_host: str | None = None
    source = "none"

    if isinstance(explicit_target, str):
        target_host = explicit_target
        source = "operator"
    else:
        recs_payload = _require_ok(
            await dispatch_child(
                connector_id=_CONNECTOR_ID,
                op_id=_OP_GET_DRS_RECOMMENDATIONS,
                params={"cluster": cluster_moid},
            )
        )
        target_host = _pick_drs_target_host(_unwrap_value(recs_payload), vm_moid)
        if target_host is not None:
            source = "drs"

    if target_host is None:
        return {
            "status": "no_recommendation",
            "target_host": None,
            "source": "none",
            "guidance": (
                "DRS produced no recommendation for the VM; pass "
                "``target_host`` explicitly to bypass the DRS lookup"
            ),
        }

    _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_RELOCATE_VM,
            params={
                "vm": vm_moid,
                "spec": {"placement": {"host": target_host}},
            },
        )
    )
    return {
        "status": "migrated",
        "target_host": target_host,
        "source": source,
        "guidance": None,
    }


# ===========================================================================
# vm.power.bulk
# ===========================================================================


async def vm_power_bulk_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Apply a power action to every VM matching a filter; aggregate results.

    Op-id: ``vmware.composite.vm.power.bulk``. ``fail_fast=True``
    aborts on first failure; default tolerates per-VM failures.
    """
    filter_dict: dict[str, Any] = dict(params.get("filter") or {})
    action = params["action"]
    fail_fast = bool(params.get("fail_fast", False))
    # Resolve the action verb to a concrete descriptor op_id once before the
    # fan-out loop. Each ``?action=<verb>`` is a distinct descriptor row, so
    # every per-VM dispatch must target the matching one.
    power_op_id = _power_vm_op_id(action)

    listing = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_VMS,
            params={f"filter.{k}": v for k, v in filter_dict.items()},
        )
    )
    vms = _unwrap_value(listing)
    if not isinstance(vms, list):
        raise RuntimeError(
            f"vm.power.bulk: expected list from {_OP_LIST_VMS!r}, got {type(vms).__name__}"
        )

    results: list[dict[str, Any]] = []
    ok_count = 0
    err_count = 0
    aborted = False
    for vm_entry in vms:
        if not isinstance(vm_entry, dict):
            continue
        vm_moid = vm_entry.get("vm")
        if not isinstance(vm_moid, str):
            continue
        power_result = await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=power_op_id,
            params={"vm": vm_moid},
        )
        if power_result.status == "ok":
            results.append({"vm": vm_moid, "status": "ok", "error": None})
            ok_count += 1
        else:
            results.append(
                {
                    "vm": vm_moid,
                    "status": "error",
                    "error": power_result.error or "<no error message>",
                }
            )
            err_count += 1
            if fail_fast:
                aborted = True
                break
    return {
        "results": results,
        "summary": {"ok": ok_count, "error": err_count},
        "aborted_on_failure": aborted,
    }


# ===========================================================================
# host.evacuate (recursive composite)
# ===========================================================================


def _classify_vm_migrate_outcome(
    migrate_result: OperationResult,
) -> tuple[bool, str]:
    """Return ``(succeeded, error_text)`` for a recursive vm.migrate result."""
    if migrate_result.status != "ok":
        return False, migrate_result.error or "unknown"
    inner = migrate_result.result
    if isinstance(inner, dict) and inner.get("status") == "migrated":
        return True, ""
    inner_status = inner.get("status") if isinstance(inner, dict) else None
    return False, str(inner_status or "unknown")


async def host_evacuate_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Migrate every VM off a host (via recursive vm.migrate) then enter maintenance.

    Op-id: ``vmware.composite.host.evacuate``. First production
    composite that calls another composite via ``dispatch_child``.
    """
    host_moid = params["host"]
    tolerate_partial = bool(params.get("tolerate_partial_failure", False))

    listing = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_VMS,
            params={"filter.hosts": [host_moid]},
        )
    )
    vms = _unwrap_value(listing)
    if not isinstance(vms, list):
        raise RuntimeError(
            f"host.evacuate: expected list from {_OP_LIST_VMS!r}, got {type(vms).__name__}"
        )

    migrated: list[str] = []
    failed: list[dict[str, str]] = []
    for vm_entry in vms:
        if not isinstance(vm_entry, dict):
            continue
        vm_moid = vm_entry.get("vm")
        if not isinstance(vm_moid, str):
            continue
        # Resolve the cluster per-VM rather than once from ``vms[0]``. The
        # vCenter listing row reports each VM's containing cluster; VMs on
        # the same host can belong to different clusters when the host
        # straddles a federation (and the listing payload may simply omit
        # the field for a row mid-flight). Treat a missing cluster as a
        # per-VM failure so the recursive migrate doesn't fire against an
        # empty target — that would short-circuit to ``no_recommendation``
        # without surfacing the underlying data gap.
        vm_cluster = vm_entry.get("cluster")
        if not isinstance(vm_cluster, str) or not vm_cluster:
            failed.append({"vm": vm_moid, "error": "missing_cluster"})
            if not tolerate_partial:
                return {
                    "status": "aborted",
                    "host": host_moid,
                    "migrated_vms": migrated,
                    "failed_vms": failed,
                    "maintenance_entered": False,
                }
            continue
        migrate_result = await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_COMPOSITE_VM_MIGRATE,
            params={"vm": vm_moid, "cluster": vm_cluster},
        )
        succeeded, err_text = _classify_vm_migrate_outcome(migrate_result)
        if succeeded:
            migrated.append(vm_moid)
            continue
        failed.append({"vm": vm_moid, "error": err_text})
        if not tolerate_partial:
            return {
                "status": "aborted",
                "host": host_moid,
                "migrated_vms": migrated,
                "failed_vms": failed,
                "maintenance_entered": False,
            }

    _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_host_maintenance_op_id("enter"),
            params={"host": host_moid},
        )
    )
    return {
        "status": "partial" if failed else "evacuated",
        "host": host_moid,
        "migrated_vms": migrated,
        "failed_vms": failed,
        "maintenance_entered": True,
    }


# ===========================================================================
# host.detach_from_vds
# ===========================================================================


async def host_detach_from_vds_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Migrate host VM NICs off a DVS to a standard switch, then remove host from DVS.

    Op-id: ``vmware.composite.host.detach_from_vds``. Refuses the DVS
    detach when any NIC migration failed -- vSphere would reject the
    step-4 detach anyway.
    """
    host_moid = params["host"]
    dvs_moid = params["dvs"]
    fallback_network = params["fallback_network"]

    _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_PORTGROUPS,
            params={"filter.hosts": [host_moid]},
        )
    )
    vm_listing = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_VMS,
            params={"filter.hosts": [host_moid]},
        )
    )
    vms = _unwrap_value(vm_listing)
    if not isinstance(vms, list):
        raise RuntimeError(
            f"host.detach_from_vds: expected list from {_OP_LIST_VMS!r}, got {type(vms).__name__}"
        )

    vms_migrated: list[str] = []
    migration_failures: list[dict[str, str]] = []
    for vm_entry in vms:
        if not isinstance(vm_entry, dict):
            continue
        vm_moid = vm_entry.get("vm")
        if not isinstance(vm_moid, str):
            continue
        nic_result = await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ATTACH_VM_NIC,
            params={"vm": vm_moid, "spec": {"network": fallback_network}},
        )
        if nic_result.status == "ok":
            vms_migrated.append(vm_moid)
        else:
            migration_failures.append(
                {"vm": vm_moid, "error": nic_result.error or "<no error message>"}
            )

    if migration_failures:
        return {
            "status": "incomplete",
            "host": host_moid,
            "vm_migration_failures": migration_failures,
            "vms_migrated": vms_migrated,
        }

    _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_REMOVE_DVS_HOST,
            params={"dvs": dvs_moid, "host": host_moid},
        )
    )
    return {
        "status": "detached",
        "host": host_moid,
        "vm_migration_failures": [],
        "vms_migrated": vms_migrated,
    }


# ===========================================================================
# cluster.patch
# ===========================================================================


# Per-step (step-name, op_id_builder, extra-params-builder) tuples. The op_id
# builder yields the concrete ``?action=<verb>`` descriptor key per step;
# extra-params adds the ``method`` body field that ``POST:/vcenter/host/{host}
# ?action=patch`` consumes (the patch verb has a non-trivial body schema; the
# maintenance verbs do not).
_CLUSTER_PATCH_STEPS: tuple[tuple[str, str], ...] = (
    ("maintenance_enter", _host_maintenance_op_id("enter")),
    ("patch", _OP_HOST_PATCH),
    ("maintenance_exit", _host_maintenance_op_id("exit")),
)


def _cluster_patch_step_params(
    *,
    step: str,
    host_moid: str,
    patch_method: str,
) -> dict[str, Any]:
    """Build the per-step params dict for a cluster.patch sub-op.

    Action verbs live on the op_id (``?action=enter`` / ``?action=patch`` /
    ``?action=exit``); only the patch step adds a body-shaped ``method``.
    """
    if step == "patch":
        return {"host": host_moid, "method": patch_method}
    return {"host": host_moid}


async def _patch_one_host(
    *,
    dispatch_child: DispatchChild,
    host_moid: str,
    patch_method: str,
) -> str | None:
    """Sequential maintenance + patch + exit on a single host; return error reason or None."""
    for step, op_id in _CLUSTER_PATCH_STEPS:
        step_result = await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            params=_cluster_patch_step_params(
                step=step, host_moid=host_moid, patch_method=patch_method
            ),
        )
        if step_result.status != "ok":
            return f"{step} on {host_moid!r} failed: {step_result.error or '<no error message>'}"
    return None


async def cluster_patch_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Sequentially patch every host in a cluster: maintenance + patch + exit.

    Op-id: ``vmware.composite.cluster.patch``. Sequential by design --
    concurrent host patches would force every cluster VM to vMotion
    at once.
    """
    cluster_moid = params["cluster"]
    patch_method = params.get("patch_method", "default")

    listing = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_CLUSTER_HOSTS,
            params={"cluster": cluster_moid},
        )
    )
    entries = _unwrap_value(listing)
    if not isinstance(entries, list):
        raise RuntimeError(
            f"cluster.patch: expected list from {_OP_LIST_CLUSTER_HOSTS!r}, "
            f"got {type(entries).__name__}"
        )
    host_moids: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            host_moid = entry.get("host")
            if isinstance(host_moid, str):
                host_moids.append(host_moid)

    patched: list[str] = []
    for i, host_moid in enumerate(host_moids):
        failure_reason = await _patch_one_host(
            dispatch_child=dispatch_child, host_moid=host_moid, patch_method=patch_method
        )
        if failure_reason is not None:
            return {
                "status": "stopped",
                "cluster": cluster_moid,
                "patched_hosts": patched,
                "failed_host": host_moid,
                "remaining_hosts": host_moids[i + 1 :],
                "failure_reason": failure_reason,
            }
        patched.append(host_moid)

    return {
        "status": "completed",
        "cluster": cluster_moid,
        "patched_hosts": patched,
        "failed_host": None,
        "remaining_hosts": [],
        "failure_reason": None,
    }
