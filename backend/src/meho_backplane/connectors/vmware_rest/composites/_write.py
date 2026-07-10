# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: 8 protocol-driven composite handlers for the
# vSphere REST write surface ship in one module per the issue body's
# design; splitting them by group would scatter the shared sub-op_id
# constants + helpers across files for no readability gain. Each
# handler's body is the documented orchestration workflow from
# #509's spec.

"""Write-shaped ``vmware.composite.*`` handler functions (8 composites).

Companion to :mod:`._read`. Post-#2256 each handler is a module-level
``async def`` taking the dispatcher's composite-branch keyword args
``(operator, target, params, connector)`` -- the resolved connector
instance the #2251 substrate injects -- and issues every raw-REST sub-op
**directly on the connector's own authenticated session**
(``connector._get_json`` / ``connector._post_json`` mounted through
``connector.mount_op_path``) with no ``endpoint_descriptor`` lookup, so
the composite works on a fresh boot with **zero catalog ingest**
(Initiative #2249 / Goal #2247, the I-A write migration).

The one exception is :func:`host_evacuate_composite`, which additionally
declares ``dispatch_child`` for its recursive call into
``vmware.composite.vm.migrate`` -- a ``source_kind="composite"`` sub-op
routed through a registrar-guaranteed row (never an ingested primitive),
so that recursion keeps its ``dispatch_child`` path per #2248.

Preserving write governance on the direct path
----------------------------------------------

``dispatch_child`` re-ran the dispatcher's per-sub-op policy/approval
gate (property 3 of #508's four guarantees); a direct session call
bypasses :func:`~meho_backplane.operations.dispatcher.dispatch`, so a
now-internal *write* sub-op would otherwise execute un-gated. Every
mutating sub-call therefore routes through
:func:`~meho_backplane.operations.composite.enforce_subop_policy`
(Task #2254) **before** the direct ``connector._post_json`` fires: the
seam re-runs the same ``policy_gate`` against an in-memory descriptor
carrying the sub-op's declared governance and returns an
``awaiting_approval`` / ``denied`` :class:`OperationResult` when the gate
does not clear. The handler returns that verbatim -- the dispatcher
passes a handler-returned :class:`OperationResult` straight through, so an
internal write **queues** (or is denied) instead of silently running.

Sub-op governance posture
-------------------------

Each write sub-op declares ``safety_level="dangerous"`` +
``requires_approval=False``. The ``dangerous`` label is the honest
intrinsic-risk classification (create / delete / power / relocate /
patch / maintenance are all state-mutating); ``requires_approval=False``
keeps the **top-level composite** (``requires_approval=True`` in
:mod:`._register`) the single primary approval gate. Flooring a sub-op to
``requires_approval=True`` would double-gate: the approval-resume path
re-runs the handler with the top-level gate already satisfied, but
:func:`enforce_subop_policy` is not resume-aware, so it would re-queue the
first internal write forever. With ``requires_approval=False`` the seam
still (a) auto-executes for a human/service operator whose composite was
already approved, and (b) denies -- or, with an explicit
per-``(principal, op, target)`` grant, queues -- a ``dangerous`` write for
an agent principal, so no internal write drops below the governance it had
under ``dispatch_child``.

Every sub-op_id below is the canonical ``METHOD:/path`` key produced
by :func:`~meho_backplane.operations.ingest.openapi.parse_openapi`
from the ingested ``vcenter.yaml`` (G3.1-T2 / #408) and
``vi-json.yaml`` (G3.1-T3 / #503). The path strings come from
inspecting the canonical ``GOVC_PARITY_BENCHMARK`` tuple at
``backend/tests/acceptance/test_g07_vsphere_canary.py`` and the
vSphere REST URL anchors in #509's issue body -- never guessed. Post-#2256
they no longer resolve an ``endpoint_descriptor`` row; each handler splits
the ``METHOD:/path`` into its verb + spec-relative path, substitutes the
``{var}`` path params, and mounts the remainder onto the target's live
``/api`` (modern) / ``/rest`` (legacy/vcsim) prefix for the direct call
(see :func:`_read_sub_op` / :func:`_write_sub_op`).

Each composite returns a structured ``{"status": ...}`` envelope so
callers can branch on ``status`` without parsing free-form prose. The
status enums are listed on each composite's ``response_schema`` in
:mod:`.schemas`.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.operations.composite import DispatchChild, enforce_subop_policy

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

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


# Connector_id every write composite governs its sub-ops against (fed to
# :func:`enforce_subop_policy` for the in-memory descriptor + the
# ``vmware.composite.vm.migrate`` recursion routed through ``dispatch_child``).
_CONNECTOR_ID = "vmware-rest-9.0"

# Declared governance for every raw-REST *write* sub-op. ``dangerous`` is
# the intrinsic-risk label; ``requires_approval=False`` keeps the top-level
# composite the single approval gate (see the module docstring on why
# flooring a sub-op to True would double-gate the resume path).
_WRITE_SAFETY_LEVEL = "dangerous"
_WRITE_REQUIRES_APPROVAL = False

# ``{var}`` path-template placeholder pattern. vCenter moids are bare
# ``[A-Za-z0-9-]`` tokens, so a plain ``str.format`` matches the RFC6570
# simple-expansion the ingested path did.
_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")

# vCenter REST op_ids (canonical METHOD:/path keys from vcenter.yaml).
#
# Action-bearing endpoints. vCenter's OpenAPI spec models POST-with-side-effect
# endpoints as ``/<path>?action=<verb>`` — i.e. the action verb is part of the
# path key, not a body parameter. The canonical ``op_id`` for "power on a VM" is
# ``POST:/vcenter/vm/{vm}/power?action=start``; the ``?action=<verb>`` rides on
# the mounted path verbatim (httpx sends it as the request query string), so no
# ``action`` body param is ever constructed. Endpoints whose action verb is
# operator-chosen (power start/stop, maintenance enter/exit) build the op_id
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
    distinct ``?action=<verb>`` keys. The composite picks the key at call time
    so each action lands on its own governance evaluation, not as a free-form
    ``action`` parameter on a single shared op_id.
    """
    return f"POST:/vcenter/vm/{{vm}}/power?action={action}"


def _host_maintenance_op_id(action: str) -> str:
    """Build the per-action canonical op_id for ``PATCH:/vcenter/host/{host}/maintenance``.

    Maintenance enter / exit are two keys under ``?action=enter`` /
    ``?action=exit``; same reasoning as :func:`_power_vm_op_id`.
    """
    return f"PATCH:/vcenter/host/{{host}}/maintenance?action={action}"


# Recursive composite sub-op_id (host.evacuate -> vm.migrate). Routed
# through ``dispatch_child`` (a registrar-guaranteed ``source_kind="composite"``
# row), not the direct session -- per #2248 the composite->composite recursion
# is out of scope for the ingested-dispatch migration.
_OP_COMPOSITE_VM_MIGRATE = "vmware.composite.vm.migrate"

# Composite op_ids -- retained as the canonical documentation anchor.
_COMPOSITE_OP_ID_VM_CREATE = "vmware.composite.vm.create"
_COMPOSITE_OP_ID_VM_CLONE = "vmware.composite.vm.clone"
_COMPOSITE_OP_ID_VM_SNAPSHOT_REVERT = "vmware.composite.vm.snapshot.revert"
_COMPOSITE_OP_ID_VM_MIGRATE = "vmware.composite.vm.migrate"
_COMPOSITE_OP_ID_VM_POWER_BULK = "vmware.composite.vm.power.bulk"
_COMPOSITE_OP_ID_HOST_EVACUATE = "vmware.composite.host.evacuate"
_COMPOSITE_OP_ID_HOST_DETACH_FROM_VDS = "vmware.composite.host.detach_from_vds"
_COMPOSITE_OP_ID_CLUSTER_PATCH = "vmware.composite.cluster.patch"

# Per-composite sub-op-id tuples. Pre-#2256 these fed the L2 pre-flight
# check that guarded a missing catalog ingest; the direct-session migration
# removed that coupling, so the tuples now serve as the canonical sub-op-path
# manifest the ingest-reconcile acceptance guard
# (``tests/test_connectors_vmware_rest_composites_l2_ingest_reconcile.py``)
# checks against the vCenter spec. Composite-to-composite sub-ops
# (``vmware.composite.*``) are listed for host.evacuate but are routed through
# ``dispatch_child``, not the direct session.
_POWER_ACTIONS: tuple[str, ...] = ("start", "stop", "suspend", "reset")
_SUB_OPS_VM_CREATE: tuple[str, ...] = (
    _OP_LIST_FOLDERS,
    _OP_CREATE_VM,
    _OP_DELETE_VM,
    _OP_ATTACH_VM_NIC,
    _power_vm_op_id("start"),
)
_SUB_OPS_VM_CLONE: tuple[str, ...] = (
    _OP_GET_VM,
    _OP_DEPLOY_LIBRARY_VM,
    _OP_GET_TASK,
)
_SUB_OPS_VM_SNAPSHOT_REVERT: tuple[str, ...] = (
    _OP_LIST_VM_SNAPSHOTS,
    _OP_REVERT_VM_SNAPSHOT,
)
_SUB_OPS_VM_MIGRATE: tuple[str, ...] = (
    _OP_GET_DRS_RECOMMENDATIONS,
    _OP_RELOCATE_VM,
)
_SUB_OPS_VM_POWER_BULK: tuple[str, ...] = (
    _OP_LIST_VMS,
    *(_power_vm_op_id(action) for action in _POWER_ACTIONS),
)
_SUB_OPS_HOST_EVACUATE: tuple[str, ...] = (
    _OP_LIST_VMS,
    _OP_COMPOSITE_VM_MIGRATE,
    _host_maintenance_op_id("enter"),
)
_SUB_OPS_HOST_DETACH_FROM_VDS: tuple[str, ...] = (
    _OP_LIST_PORTGROUPS,
    _OP_LIST_VMS,
    _OP_ATTACH_VM_NIC,
    _OP_REMOVE_DVS_HOST,
)
_SUB_OPS_CLUSTER_PATCH: tuple[str, ...] = (
    _OP_LIST_CLUSTER_HOSTS,
    _host_maintenance_op_id("enter"),
    _host_maintenance_op_id("exit"),
    _OP_HOST_PATCH,
)


def _unwrap_value(payload: Any) -> Any:
    """Return the inner ``value`` field on a pre-7 envelope, else *payload*."""
    if isinstance(payload, dict) and set(payload.keys()) == {"value"}:
        return payload["value"]
    return payload


def _split_sub_op(op_id: str, params: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Split *op_id* + *params* into ``(method, spec-relative path, remainder)``.

    Extracts the ``{var}`` names from the ``METHOD:/path`` template,
    substitutes the matching *params* entries into the path (any
    ``?action=<verb>`` query segment rides along verbatim), and returns the
    remaining params -- the query bucket for a ``GET`` or the JSON body for a
    write. Mirrors the ``x-meho-param-loc`` path/query/body split the ingested
    dispatch performed, without any descriptor lookup.
    """
    method, _, path_template = op_id.partition(":")
    var_names = _PATH_VAR_RE.findall(path_template)
    path_params = {name: params[name] for name in var_names}
    path = path_template.format(**path_params) if path_params else path_template
    remainder = {k: v for k, v in params.items() if k not in path_params}
    return method, path, remainder


async def _read_sub_op(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    op_id: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """Issue one read sub-call (``GET``) directly on the connector session.

    Splits the canonical ``METHOD:/path`` *op_id*, mounts the substituted
    path onto the target's live ``/api`` / ``/rest`` prefix, and dispatches
    through :meth:`~meho_backplane.connectors.adapters.http.HttpConnector._get_json`
    (tenacity-retried, idempotent) with the remainder params as the query
    bucket. The query bucket is authored in the legacy ``filter.*`` style
    and keyed off the mount flavor by
    :meth:`VmwareRestConnector.adapt_op_query` (#2298) — bare param names
    on modern ``/api`` (which 400s the prefixed form), ``filter.*`` on
    legacy ``/rest`` — so the write composites' resolution listings behave
    the same as the read composites'. Read sub-ops carry no governance
    gate -- they are the safe resolution reads the write composites build
    their request bodies from. Transport / status failures raise
    :exc:`httpx.HTTPError`; load-bearing callers let it propagate (the
    dispatcher wraps it as ``connector_error`` for the composite parent).
    """
    method, path, query = _split_sub_op(op_id, params or {})
    if method != "GET":
        raise RuntimeError(f"_read_sub_op called with non-GET op_id {op_id!r}")
    mounted = await connector.mount_op_path(target, path, operator)
    adapted = await connector.adapt_op_query(target, query, operator)
    return await connector._get_json(target, mounted, operator=operator, params=adapted)


async def _write_sub_op(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    op_id: str,
    params: dict[str, Any],
) -> tuple[OperationResult | None, Any]:
    """Gate then issue one *write* sub-call directly on the connector session.

    Runs :func:`~meho_backplane.operations.composite.enforce_subop_policy`
    with the sub-op's full logical *params* (so the durable
    :class:`~meho_backplane.db.models.ApprovalRequest` names the entity the
    write would touch) and the declared ``dangerous`` /
    ``requires_approval=False`` governance. When the seam returns a result
    (``awaiting_approval`` / ``denied``) the write is **not** issued -- the
    ``(gate, None)`` tuple signals the caller to return that
    :class:`OperationResult` verbatim. When the seam clears the gate
    (``None``), the sub-op is split into ``(verb, path, body)`` and dispatched
    through
    :meth:`~meho_backplane.connectors.adapters.http.HttpConnector._post_json`
    (honouring the actual ``POST`` / ``PATCH`` / ``DELETE`` verb); the parsed
    JSON payload rides back as ``(None, payload)``. Transport failures raise
    :exc:`httpx.HTTPError` for the caller to catch (partial-failure legs) or
    let propagate (load-bearing legs).
    """
    gate = await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        safety_level=_WRITE_SAFETY_LEVEL,
        requires_approval=_WRITE_REQUIRES_APPROVAL,
        target=target,
        params=params,
    )
    if gate is not None:
        return gate, None
    method, path, body = _split_sub_op(op_id, params)
    mounted = await connector.mount_op_path(target, path, operator)
    payload = await connector._post_json(
        target, mounted, operator=operator, verb=method, json=body or None
    )
    return None, payload


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


async def _resolve_vm_list(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    filter_dict: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve a VM filter to its listing rows via ``GET:/vcenter/vm``.

    Read-only: issues exactly one listing GET directly on the connector
    session, never a mutation. Filter keys are forwarded as ``filter.<key>``
    query params per the vCenter listing contract.

    Shared seam (#1608): the write handlers that fan out over a filtered
    VM set (:func:`vm_power_bulk_composite`, :func:`host_evacuate_composite`,
    :func:`host_detach_from_vds_composite`) call this at dispatch time, and
    their park-time preview builders (:mod:`._write_preview`) call the same
    function against the resolved connector at approval-park time — so the
    entity set the reviewer sees in ``proposed_effect`` is resolved by the
    same code path the approved dispatch will use.

    Raises :class:`RuntimeError` when the listing returns a non-list
    payload and :exc:`httpx.HTTPError` when the sub-op transport fails.
    Non-dict rows are dropped (the listing contract yields summary objects);
    per-row key validation stays with callers.
    """
    listing = await _read_sub_op(
        connector,
        target,
        operator,
        _OP_LIST_VMS,
        {f"filter.{k}": v for k, v in filter_dict.items()},
    )
    vms = _unwrap_value(listing)
    if not isinstance(vms, list):
        raise RuntimeError(f"expected list from {_OP_LIST_VMS!r}, got {type(vms).__name__}")
    return [entry for entry in vms if isinstance(entry, dict)]


async def _resolve_cluster_hosts(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    cluster_moid: str,
) -> list[dict[str, Any]]:
    """Resolve a cluster's host listing rows via ``GET:/vcenter/cluster/{cluster}/host``.

    Read-only single GET directly on the connector session. Shared between
    :func:`cluster_patch_composite` (dispatch time) and its park-time preview
    builder in :mod:`._write_preview` (#1608) — same rationale as
    :func:`_resolve_vm_list`.

    Raises :class:`RuntimeError` on a non-list payload and
    :exc:`httpx.HTTPError` on a transport fault. Non-dict rows are dropped.
    """
    listing = await _read_sub_op(
        connector, target, operator, _OP_LIST_CLUSTER_HOSTS, {"cluster": cluster_moid}
    )
    entries = _unwrap_value(listing)
    if not isinstance(entries, list):
        raise RuntimeError(
            f"expected list from {_OP_LIST_CLUSTER_HOSTS!r}, got {type(entries).__name__}"
        )
    return [entry for entry in entries if isinstance(entry, dict)]


# ===========================================================================
# vm.create
# ===========================================================================


async def _resolve_folder_moid(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    folder_name: str,
) -> tuple[str | None, str | None]:
    """Look up a folder moid by display name.

    Returns ``(moid, None)`` on success or ``(None, reason)`` on
    failure -- the caller folds the reason into a ``rolled_back``
    envelope. Failure modes: empty match list, listing row missing
    the ``folder`` key.
    """
    folder_listing = await _read_sub_op(
        connector, target, operator, _OP_LIST_FOLDERS, {"filter.names": [folder_name]}
    )
    folder_entries = _unwrap_value(folder_listing)
    if not isinstance(folder_entries, list) or not folder_entries:
        return None, f"folder name {folder_name!r} did not resolve to any moid"
    first_entry = folder_entries[0]
    folder_moid_raw = first_entry.get("folder") if isinstance(first_entry, dict) else None
    if not isinstance(folder_moid_raw, str):
        return None, "folder listing row missing ``folder`` key"
    return folder_moid_raw, None


async def _rollback_created_vm(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm_id: str,
) -> None:
    """Issue ``DELETE:/vcenter/vm/{vm}`` to remove a half-created VM.

    Best-effort: rollback faults are swallowed -- the operator already knows
    the create flow failed, and a denied/queued rollback sub-op or a
    transport error must not mask the original failure. A gate result from
    the seam is ignored here for the same reason (the rollback DELETE is a
    cleanup, not an operator-requested action).
    """
    try:
        await _write_sub_op(connector, target, operator, _OP_DELETE_VM, {"vm": vm_id})
    except httpx.HTTPError:
        return


async def vm_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create a VM with NIC attach + optional power-on; rollback on failure.

    Op-id: ``vmware.composite.vm.create``. See module docstring for the
    sub-op chain, the direct-session governance seam, and rollback semantics.
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
        connector=connector, target=target, operator=operator, folder_name=folder_name
    )
    if folder_moid is None:
        return _rolled_back(steps=steps, failed_step="folder_lookup", reason=folder_err or "")
    steps.append("folder_lookup")

    create_spec = {
        "spec": {
            "name": name,
            "guest_OS": guest_os,
            "placement": {"folder": folder_moid},
            "cpu": {"count": cpu_count},
            "memory": {"size_MiB": memory_mib},
        },
    }
    try:
        gate, create_payload = await _write_sub_op(
            connector, target, operator, _OP_CREATE_VM, create_spec
        )
    except httpx.HTTPError as exc:
        # Create failed; nothing to roll back.
        return _rolled_back(steps=steps, failed_step="create", reason=f"create failed: {exc}")
    if gate is not None:
        return gate
    vm_id = _unwrap_value(create_payload)
    if not isinstance(vm_id, str):
        return _rolled_back(
            steps=steps,
            failed_step="create",
            reason=f"create returned non-string vm id payload: {type(vm_id).__name__}",
        )
    steps.append("create")

    for nic in nics:
        try:
            gate, _ = await _write_sub_op(
                connector, target, operator, _OP_ATTACH_VM_NIC, {"vm": vm_id, "spec": nic}
            )
        except httpx.HTTPError as exc:
            await _rollback_created_vm(
                connector=connector, target=target, operator=operator, vm_id=vm_id
            )
            return _rolled_back(
                steps=steps,
                failed_step="nic_attach",
                reason=f"nic attach for network={nic.get('network')!r} failed: {exc}",
            )
        if gate is not None:
            return gate
    if nics:
        steps.append("nic_attach")

    if power_on:
        try:
            gate, _ = await _write_sub_op(
                connector, target, operator, _power_vm_op_id("start"), {"vm": vm_id}
            )
        except httpx.HTTPError as exc:
            await _rollback_created_vm(
                connector=connector, target=target, operator=operator, vm_id=vm_id
            )
            return _rolled_back(
                steps=steps,
                failed_step="power_on",
                reason=f"power_on failed: {exc}",
            )
        if gate is not None:
            return gate
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
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
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
        task_payload = await _read_sub_op(
            connector, target, operator, _OP_GET_TASK, {"task": task_id}
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
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
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
    # source VM lookup fails (httpx.HTTPError surfaces upstream).
    await _read_sub_op(connector, target, operator, _OP_GET_VM, {"vm": source_vm})

    gate, deploy_payload = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_DEPLOY_LIBRARY_VM,
        {"library_item": library_item, "spec": {"name": target_name}},
    )
    if gate is not None:
        return gate
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
        connector=connector,
        target=target,
        operator=operator,
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
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Revert a VM to a named snapshot. Idempotent; ambiguity-rejecting.

    Op-id: ``vmware.composite.vm.snapshot.revert``. Multiple snapshots
    sharing the name -> ``status='ambiguous'``; missing -> ``not_found``.
    Revert never dispatches on either.
    """
    vm_moid = params["vm"]
    snapshot_name = params["snapshot_name"]

    listing = await _read_sub_op(
        connector, target, operator, _OP_LIST_VM_SNAPSHOTS, {"vm": vm_moid}
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
    gate, _ = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_REVERT_VM_SNAPSHOT,
        {"vm": vm_moid, "snap": snapshot_moid},
    )
    if gate is not None:
        return gate
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
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Migrate a VM via DRS recommendation or explicit ``target_host``.

    Op-id: ``vmware.composite.vm.migrate``. ``target_host`` overrides
    the DRS lookup. No-recommendation path returns
    ``status='no_recommendation'`` so the caller can re-dispatch.

    Also the recursion target of :func:`host_evacuate_composite`: invoked
    through ``dispatch_child`` there, the dispatcher resolves this composite
    and injects ``connector`` so its own relocate write runs on the direct
    session under the governance seam, exactly as a top-level dispatch does.
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
        recs_payload = await _read_sub_op(
            connector,
            target,
            operator,
            _OP_GET_DRS_RECOMMENDATIONS,
            {"cluster": cluster_moid},
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

    gate, _ = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_RELOCATE_VM,
        {"vm": vm_moid, "spec": {"placement": {"host": target_host}}},
    )
    if gate is not None:
        return gate
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
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Apply a power action to every VM matching a filter; aggregate results.

    Op-id: ``vmware.composite.vm.power.bulk``. ``fail_fast=True``
    aborts on first failure; default tolerates per-VM failures. A governance
    verdict is identical across the fan-out (same op_id + target), so a
    gated/denied per-VM power short-circuits the whole composite with the
    seam's result rather than half-executing the batch.
    """
    filter_dict: dict[str, Any] = dict(params.get("filter") or {})
    action = params["action"]
    fail_fast = bool(params.get("fail_fast", False))
    power_op_id = _power_vm_op_id(action)

    vms = await _resolve_vm_list(
        connector=connector, target=target, operator=operator, filter_dict=filter_dict
    )

    results: list[dict[str, Any]] = []
    ok_count = 0
    err_count = 0
    aborted = False
    for vm_entry in vms:
        vm_moid = vm_entry.get("vm")
        if not isinstance(vm_moid, str):
            continue
        try:
            gate, _ = await _write_sub_op(connector, target, operator, power_op_id, {"vm": vm_moid})
        except httpx.HTTPError as exc:
            results.append({"vm": vm_moid, "status": "error", "error": str(exc)})
            err_count += 1
            if fail_fast:
                aborted = True
                break
            continue
        if gate is not None:
            return gate
        results.append({"vm": vm_moid, "status": "ok", "error": None})
        ok_count += 1
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
    connector: VmwareRestConnector,
    dispatch_child: DispatchChild,
) -> dict[str, Any] | OperationResult:
    """Migrate every VM off a host (via recursive vm.migrate) then enter maintenance.

    Op-id: ``vmware.composite.host.evacuate``. First production composite
    that calls another composite via ``dispatch_child`` -- that recursion
    into ``vmware.composite.vm.migrate`` stays on the ``dispatch_child`` path
    (a registrar-guaranteed ``source_kind="composite"`` row, #2248), while
    the host's own VM listing read and the maintenance-enter write run
    directly on the injected ``connector`` session.
    """
    host_moid = params["host"]
    tolerate_partial = bool(params.get("tolerate_partial_failure", False))

    vms = await _resolve_vm_list(
        connector=connector, target=target, operator=operator, filter_dict={"hosts": [host_moid]}
    )

    migrated: list[str] = []
    failed: list[dict[str, str]] = []
    for vm_entry in vms:
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

    gate, _ = await _write_sub_op(
        connector, target, operator, _host_maintenance_op_id("enter"), {"host": host_moid}
    )
    if gate is not None:
        return gate
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
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Migrate host VM NICs off a DVS to a standard switch, then remove host from DVS.

    Op-id: ``vmware.composite.host.detach_from_vds``. Refuses the DVS
    detach when any NIC migration failed -- vSphere would reject the
    step-4 detach anyway.
    """
    host_moid = params["host"]
    dvs_moid = params["dvs"]
    fallback_network = params["fallback_network"]

    await _read_sub_op(
        connector, target, operator, _OP_LIST_PORTGROUPS, {"filter.hosts": [host_moid]}
    )
    vms = await _resolve_vm_list(
        connector=connector, target=target, operator=operator, filter_dict={"hosts": [host_moid]}
    )

    vms_migrated: list[str] = []
    migration_failures: list[dict[str, str]] = []
    for vm_entry in vms:
        vm_moid = vm_entry.get("vm")
        if not isinstance(vm_moid, str):
            continue
        try:
            gate, _ = await _write_sub_op(
                connector,
                target,
                operator,
                _OP_ATTACH_VM_NIC,
                {"vm": vm_moid, "spec": {"network": fallback_network}},
            )
        except httpx.HTTPError as exc:
            migration_failures.append({"vm": vm_moid, "error": str(exc)})
            continue
        if gate is not None:
            return gate
        vms_migrated.append(vm_moid)

    if migration_failures:
        return {
            "status": "incomplete",
            "host": host_moid,
            "vm_migration_failures": migration_failures,
            "vms_migrated": vms_migrated,
        }

    gate, _ = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_REMOVE_DVS_HOST,
        {"dvs": dvs_moid, "host": host_moid},
    )
    if gate is not None:
        return gate
    return {
        "status": "detached",
        "host": host_moid,
        "vm_migration_failures": [],
        "vms_migrated": vms_migrated,
    }


# ===========================================================================
# cluster.patch
# ===========================================================================


# Per-step (step-name, op_id) tuples. The op_id yields the concrete
# ``?action=<verb>`` key per step; the patch step additionally carries a
# ``method`` body field (the patch verb has a non-trivial body schema; the
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
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    host_moid: str,
    patch_method: str,
) -> tuple[OperationResult | None, str | None]:
    """Sequential maintenance + patch + exit on a single host.

    Returns ``(gate, None)`` when a step's governance seam parks/denies the
    write (the caller returns the :class:`OperationResult` verbatim),
    ``(None, error_reason)`` when a step's transport fails, or
    ``(None, None)`` on full success.
    """
    for step, op_id in _CLUSTER_PATCH_STEPS:
        step_params = _cluster_patch_step_params(
            step=step, host_moid=host_moid, patch_method=patch_method
        )
        try:
            gate, _ = await _write_sub_op(connector, target, operator, op_id, step_params)
        except httpx.HTTPError as exc:
            return None, f"{step} on {host_moid!r} failed: {exc}"
        if gate is not None:
            return gate, None
    return None, None


async def cluster_patch_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Sequentially patch every host in a cluster: maintenance + patch + exit.

    Op-id: ``vmware.composite.cluster.patch``. Sequential by design --
    concurrent host patches would force every cluster VM to vMotion
    at once.
    """
    cluster_moid = params["cluster"]
    patch_method = params.get("patch_method", "default")

    entries = await _resolve_cluster_hosts(
        connector=connector, target=target, operator=operator, cluster_moid=cluster_moid
    )
    host_moids: list[str] = []
    for entry in entries:
        host_moid = entry.get("host")
        if isinstance(host_moid, str):
            host_moids.append(host_moid)

    patched: list[str] = []
    for i, host_moid in enumerate(host_moids):
        gate, failure_reason = await _patch_one_host(
            connector=connector,
            target=target,
            operator=operator,
            host_moid=host_moid,
            patch_method=patch_method,
        )
        if gate is not None:
            return gate
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
