# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: protocol-driven composite handlers for the
# vSphere REST + VI-JSON write surface ship in one module per the issue
# body's design; splitting them by group would scatter the shared
# sub-op_id constants + helpers across files for no readability gain. Each
# handler's body is the documented orchestration workflow from #509's spec
# (plus the mutating VI-JSON disk-grow from #2893 and folder-template clone
# from #2894).

"""Write-shaped ``vmware.composite.*`` handler functions (11 composites).

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
from meho_backplane.connectors.vmware_rest.vim_task import poll_vim_task
from meho_backplane.operations.composite import DispatchChild, enforce_subop_policy

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "cluster_patch_composite",
    "host_detach_from_vds_composite",
    "host_evacuate_composite",
    "vm_clone_composite",
    "vm_clone_from_template_composite",
    "vm_create_composite",
    "vm_disk_grow_composite",
    "vm_migrate_composite",
    "vm_power_bulk_composite",
    "vm_power_composite",
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
# REST Disk.Info read — the disk-grow park-time preview reads label +
# current capacity (bytes) off this; the disk id is the vim device key.
_OP_GET_VM_DISK = "GET:/vcenter/vm/{vm}/hardware/disk/{disk}"

# vim (VI-JSON) op_ids for the disk-grow write path. These are the
# canonical ``METHOD:/path`` keys the ingest parser emits from
# ``vi-json.yaml`` (the moId rides the path as ``{moId}``, mirroring the
# read composites' ``POST:/EventManager/{moId}/QueryEvents``). They are
# the *governance* op_ids fed to :func:`enforce_subop_policy`; the
# concrete path (moId substituted) is what
# :meth:`VmwareRestConnector._post_vmomi_json` POSTs to. Kept out of the
# ``_SUB_OPS_*`` namespace so the vCenter-REST ingest-reconcile sweep does
# not treat a vi-json path as a ``vcenter.yaml`` row; the pinned
# ``vi-json.yaml`` reconcile asserts them instead.
#
# ``ReconfigVM_Task`` is the only route to change a virtual disk's
# capacity: the pinned 9.0 REST spec's ``Disk.UpdateSpec`` carries only
# ``backing`` (no capacity field), so vim is the sole write path
# (spec-verified). ``RetrievePropertiesEx`` reads the VM's
# ``config.hardware.device`` to obtain the full ``VirtualDisk`` device
# (its ``key`` + current ``capacityInBytes``) the reconfigure edits, and
# is re-read by the shared Task-poll helper to drive the returned
# ``*_Task`` MoRef to a terminal state.
# Canonical vi-json.yaml path keys (moId as the ``{moId}`` template — the
# form the ingest parser emits and the pinned-spec reconcile asserts).
_OP_RECONFIG_VM_TASK = "POST:/VirtualMachine/{moId}/ReconfigVM_Task"
_OP_RETRIEVE_PROPERTIES = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
# Concrete runtime path for the config read: the PropertyCollector is a
# singleton whose moId is the literal ``propertyCollector`` (the same
# concrete path the typed reads + read composites POST). ``ReconfigVM_Task``
# substitutes the VM moid inline at call time.
_VMOMI_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"

#: vi-json sub-op manifest for ``vm.disk.grow`` (parallel to the REST
#: ``_SUB_OPS_*`` tuples, but named out of that namespace so the
#: vcenter.yaml ingest-reconcile sweep skips it). The pinned
#: ``vi-json.yaml`` reconcile lane introspects this to assert every
#: declared vim path exists in the spec.
_VIM_SUB_OPS_VM_DISK_GROW: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_RECONFIG_VM_TASK,
)

# VI-JSON polymorphic-type discriminator (``Any._typeName`` in
# vi-json.yaml) + the VirtualDisk data-object type name. A device read
# from ``config.hardware.device`` carries ``_typeName`` so the handler can
# pick the VirtualDisk out of the mixed device list.
_VMOMI_TYPE_NAME_KEY = "_typeName"
_VIRTUAL_DISK_TYPE = "VirtualDisk"
_VIRTUAL_MACHINE_MO_TYPE = "VirtualMachine"
_PROP_CONFIG_HARDWARE_DEVICE = "config.hardware.device"

# Default wall-clock bound for the ReconfigVM_Task poll — mirrors the 600s
# ``vm.clone`` convention.
_DISK_GROW_TASK_TIMEOUT_SECONDS = 600.0

# vim (VI-JSON) op_ids for the folder-template clone write path (#2894).
# Same ``METHOD:/path`` shape the ingest parser emits from ``vi-json.yaml``
# (moId rides the path as ``{moId}``); kept out of the ``_SUB_OPS_*``
# namespace so the vcenter.yaml reconcile sweep skips them (the pinned
# ``vi-json.yaml`` reconcile lane asserts them instead). ``CloneVM_Task`` is
# the canonical folder-template deploy API (what govc/terraform use) — the
# REST ``vm.clone`` content-library path cannot deploy a marked-as-template
# folder VM, and only the vim path supports inline customization at clone
# time. ``GetCustomizationSpec`` resolves a stored GOSC spec (by name) to the
# full ``CustomizationSpec`` embedded inline in the clone; the config read
# (``RetrievePropertiesEx``, shared with disk-grow) asserts the source is a
# template.
_OP_CLONE_VM_TASK = "POST:/VirtualMachine/{moId}/CloneVM_Task"
_OP_GET_CUSTOMIZATION_SPEC = "POST:/CustomizationSpecManager/{moId}/GetCustomizationSpec"

#: vi-json sub-op manifest for ``vm.clone_from_template`` (parallel to
#: ``_VIM_SUB_OPS_VM_DISK_GROW``; named out of the ``_SUB_OPS_*`` namespace so
#: the vcenter.yaml sweep skips it). The pinned ``vi-json.yaml`` reconcile
#: lane introspects this to assert every declared vim path exists in the spec.
_VIM_SUB_OPS_VM_CLONE_FROM_TEMPLATE: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_GET_CUSTOMIZATION_SPEC,
    _OP_CLONE_VM_TASK,
)

# vim ManagedObjectReference type discriminators for the clone placement
# MoRefs. A vim MoRef serialises as ``{"type": <T>, "value": <moid>}`` — the
# handler wraps the operator-supplied placement moids into these.
_FOLDER_MO_TYPE = "Folder"
_RESOURCE_POOL_MO_TYPE = "ResourcePool"
_HOST_SYSTEM_MO_TYPE = "HostSystem"
_DATASTORE_MO_TYPE = "Datastore"

# The vim property the template assert reads off the resolved source VM.
_PROP_CONFIG_TEMPLATE = "config.template"

# Standard ``ServiceContent.customizationSpecManager`` singleton moid;
# overridable via the ``customization_spec_manager_moid`` param (mirrors the
# performance composite's ``perf_manager_moid`` default of ``"PerfMgr"``).
_DEFAULT_CUSTOMIZATION_SPEC_MANAGER_MOID = "CustomizationSpecManager"

# Default wall-clock bound for the CloneVM_Task poll — mirrors the 600s
# ``vm.clone`` convention.
_CLONE_FROM_TEMPLATE_TASK_TIMEOUT_SECONDS = 600.0


def _power_vm_op_id(action: str) -> str:
    """Build the per-action canonical op_id for ``POST:/vcenter/vm/{vm}/power``.

    vCenter exposes ``start`` / ``stop`` / ``suspend`` / ``reset`` as four
    distinct ``?action=<verb>`` keys. The composite picks the key at call time
    so each action lands on its own governance evaluation, not as a free-form
    ``action`` parameter on a single shared op_id.
    """
    return f"POST:/vcenter/vm/{{vm}}/power?action={action}"


def _guest_power_vm_op_id(action: str) -> str:
    """Build the per-action canonical op_id for ``POST:/vcenter/vm/{vm}/guest/power``.

    The vAPI guest-power endpoint mediates a *soft* power transition through
    VMware Tools: ``?action=shutdown`` asks the guest OS for a clean
    shutdown, ``?action=reboot`` for a clean restart. Same action-in-the-path
    keying vCenter uses for hard power (:func:`_power_vm_op_id`), so the op_id
    round-trips through the ingest parser identically. Distinct from the hard
    ``/power`` endpoint: guest verbs require running Tools and return
    ``ServiceUnavailable`` (HTTP 503) when Tools is absent -- surfaced by the
    single-VM handler as a typed ``tools_unavailable`` status rather than a
    hang.
    """
    return f"POST:/vcenter/vm/{{vm}}/guest/power?action={action}"


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
_COMPOSITE_OP_ID_VM_POWER = "vmware.composite.vm.power"
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

# Single-VM ``vm.power`` operator-facing verb -> raw sub-op_id. Hard verbs
# hit the ``/power`` endpoint (immediate, may lose in-guest state); the two
# ``guest_*`` verbs hit ``/guest/power`` (Tools-mediated soft transition).
# ``reset`` is the hard cycle; ``guest_reboot`` the clean one -- both are
# offered so the approver picks the blast radius they intend.
_SINGLE_POWER_VERB_OP_IDS: dict[str, str] = {
    "on": _power_vm_op_id("start"),
    "off": _power_vm_op_id("stop"),
    "reset": _power_vm_op_id("reset"),
    "guest_shutdown": _guest_power_vm_op_id("shutdown"),
    "guest_reboot": _guest_power_vm_op_id("reboot"),
}

#: The ``vm.power`` verbs routed through VMware Tools (``/guest/power``). A
#: failure on one of these is classified against the Tools state; a hard-power
#: failure never is (a 503 there is not a Tools signal).
_GUEST_POWER_VERBS: frozenset[str] = frozenset({"guest_shutdown", "guest_reboot"})
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
_SUB_OPS_VM_POWER: tuple[str, ...] = tuple(dict.fromkeys(_SINGLE_POWER_VERB_OP_IDS.values()))
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


async def _write_vmomi_sub_op(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    vmomi_path: str,
    body: dict[str, Any],
    params: dict[str, Any],
) -> tuple[OperationResult | None, Any]:
    """Gate then issue one *mutating* vim (VI-JSON) sub-call on the connector session.

    The VI-JSON counterpart of :func:`_write_sub_op`: a vmomi POST that
    *mutates* (``ReconfigVM_Task``, ``CloneVM_Task``,
    ``ReconfigureComputeResource_Task`` …) is a write sub-op, not a
    transport detail, so it flows through the **same** #2254 governance
    seam the REST write sub-ops do. Runs
    :func:`~meho_backplane.operations.composite.enforce_subop_policy` with
    the vim method's canonical governance *op_id* (the ``METHOD:/path`` key
    the ingest parser emits from ``vi-json.yaml``, e.g.
    ``POST:/VirtualMachine/{moId}/ReconfigVM_Task``) and the sub-op's full
    logical *params* (so the durable
    :class:`~meho_backplane.db.models.ApprovalRequest` names the entity the
    write touches) under the shared ``dangerous`` /
    ``requires_approval=False`` posture.

    When the seam returns a result (``awaiting_approval`` / ``denied``) the
    vmomi write is **not** issued -- the ``(gate, None)`` tuple signals the
    caller to return that :class:`OperationResult` verbatim, so a
    policy-denied vmomi write never reaches the wire. When the seam clears
    the gate (``None``), the method is POSTed through
    :meth:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector._post_vmomi_json`
    (which mounts it on the documented VI-JSON base ``/sdk/vim25/{release}``,
    single ``/api`` fallback) and the parsed payload -- for a ``*_Task``
    method, the returned Task :class:`ManagedObjectReference` -- rides back
    as ``(None, payload)``. Transport failures raise :exc:`httpx.HTTPError`
    for the caller.

    ``vmomi_path`` is the concrete spec-relative method path (moId
    substituted, e.g. ``/VirtualMachine/vm-1/ReconfigVM_Task``); ``op_id``
    is the placeholder governance key so a per-``(principal, op, target)``
    grant matches regardless of which VM the write targets.
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
    payload = await connector._post_vmomi_json(target, vmomi_path, operator=operator, json=body)
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
# vm.power (single VM)
# ===========================================================================


def _parse_vsphere_error(exc: httpx.HTTPError) -> tuple[int | None, str | None]:
    """Best-effort ``(http_status, vsphere_error_type)`` from a sub-op fault.

    vCenter error bodies carry a machine ``error_type`` (e.g.
    ``SERVICE_UNAVAILABLE`` when Tools is down) alongside the HTTP status.
    Only :class:`httpx.HTTPStatusError` carries a response; transport-level
    faults (connect/read) surface ``(None, None)`` and are reported as a
    generic error. Body parsing is defensive -- a non-JSON or
    unexpectedly-shaped body yields a ``None`` error_type, never a raise.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return None, None
    status = exc.response.status_code
    try:
        body = exc.response.json()
    except (ValueError, httpx.HTTPError):
        return status, None
    error_type = body.get("error_type") if isinstance(body, dict) else None
    return status, error_type if isinstance(error_type, str) else None


def _guest_tools_unavailable(status: int | None, error_type: str | None) -> bool:
    """Whether a guest-power fault means VMware Tools is not running.

    The vAPI guest-power operation returns ``ServiceUnavailable`` -- HTTP
    503, ``error_type == "SERVICE_UNAVAILABLE"`` -- specifically when Tools
    is not running, which is the state the operator needs surfaced (a soft
    shutdown cannot proceed). Matching either signal keeps the classifier
    robust to a deployment that returns the typed body without the 503, or
    the 503 without a parseable body.
    """
    return status == 503 or error_type == "SERVICE_UNAVAILABLE"


async def vm_power_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Apply one power verb to a single VM; approval-gated, typed failures.

    Op-id: ``vmware.composite.vm.power``. Unlike the fan-out
    :func:`vm_power_bulk_composite`, this acts on one operator-named VM moid
    -- the ergonomics a one-VM incident action (a hung appliance) wants.
    Five verbs: ``on`` / ``off`` / ``reset`` hit the hard ``/power``
    endpoint; ``guest_shutdown`` / ``guest_reboot`` hit the Tools-mediated
    ``/guest/power`` endpoint for a clean in-guest transition.

    The single write is routed through the #2254 governance seam
    (:func:`_write_sub_op`); a parked/denied gate returns the
    :class:`OperationResult` verbatim and no power op fires. A soft verb
    against a VM whose Tools are down fails *typed* -- ``status =
    "tools_unavailable"`` with the Tools state echoed -- rather than hanging
    or surfacing an opaque transport error, so the operator learns why the
    clean shutdown could not run and can fall back to a hard ``off``.
    """
    vm_moid = params["vm"]
    verb = params["verb"]
    op_id = _SINGLE_POWER_VERB_OP_IDS[verb]
    is_guest_verb = verb in _GUEST_POWER_VERBS

    try:
        gate, _ = await _write_sub_op(connector, target, operator, op_id, {"vm": vm_moid})
    except httpx.HTTPError as exc:
        status, error_type = _parse_vsphere_error(exc)
        if is_guest_verb and _guest_tools_unavailable(status, error_type):
            return {
                "vm": vm_moid,
                "verb": verb,
                "status": "tools_unavailable",
                "error": str(exc),
                "error_type": error_type,
                "guest_tools": "unavailable",
            }
        return {
            "vm": vm_moid,
            "verb": verb,
            "status": "error",
            "error": str(exc),
            "error_type": error_type,
            "guest_tools": "unavailable" if is_guest_verb else None,
        }
    if gate is not None:
        return gate
    return {
        "vm": vm_moid,
        "verb": verb,
        "status": "ok",
        "error": None,
        "error_type": None,
        "guest_tools": "ok" if is_guest_verb else None,
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


# ===========================================================================
# vm.disk.grow (VI-JSON write substrate — keystone 2, #2893)
# ===========================================================================
#
# The first *mutating* VI-JSON composite. Growing a virtual disk has no
# REST path — the pinned 9.0 spec's ``Disk.UpdateSpec`` carries only
# ``backing`` (no capacity field, spec-verified), so ``ReconfigVM_Task``
# is the sole route. The write rides the same governed direct-session seam
# the REST writes do (:func:`_write_vmomi_sub_op` → the #2254 gate), and
# the returned ``*_Task`` MoRef is driven to a terminal state via the
# shared :func:`~meho_backplane.connectors.vmware_rest.vim_task.poll_vim_task`
# helper before success is reported. Tasks D (#2894 clone) and E (#2895
# DRS-rule) ride the same two seams.


def _coerce_int(value: Any) -> int | None:
    """Coerce a JSON number / numeric-string to int; reject bools + junk.

    ``VirtualDisk.capacityInBytes`` is ``xsd:long``; some intermediaries
    render a 64-bit value as a JSON string. ``True`` is an ``int`` subclass
    and must not read as ``1``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _build_vm_devices_retrieve_params(vm_moid: str) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` body reading a VM's device list.

    A single ``PropertyFilterSpec`` scoped directly to the VirtualMachine
    object requesting ``config.hardware.device`` -- the list of
    ``VirtualDevice`` objects (disks, controllers, NICs, …), each carrying
    its VI-JSON ``_typeName`` discriminator so the disk-grow handler can
    pick the ``VirtualDisk`` out. The singleton ``propertyCollector`` moId
    rides the path, so the body is only the method args (the shape the
    typed reads send).
    """
    return {
        "specSet": [
            {
                "propSet": [
                    {"type": _VIRTUAL_MACHINE_MO_TYPE, "pathSet": [_PROP_CONFIG_HARDWARE_DEVICE]}
                ],
                "objectSet": [{"obj": {"type": _VIRTUAL_MACHINE_MO_TYPE, "value": vm_moid}}],
            }
        ],
        "options": {},
    }


def _extract_vm_devices(retrieve_result: Any) -> list[Any]:
    """Pull the ``config.hardware.device`` list off a RetrievePropertiesEx result."""
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    if not isinstance(objects, list):
        return []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and prop.get("name") == _PROP_CONFIG_HARDWARE_DEVICE:
                val = prop.get("val")
                return val if isinstance(val, list) else []
    return []


def _find_virtual_disk(devices: list[Any], disk_id: str) -> dict[str, Any] | None:
    """Return the ``VirtualDisk`` device whose key matches *disk_id*, else ``None``.

    The REST disk id (resource type ``com.vmware.vcenter.vm.hardware.Disk``)
    is the string form of the vim ``VirtualDevice.key``, so the disk-grow
    ``disk`` param selects the device by ``str(key)`` match. The
    ``_typeName == "VirtualDisk"`` guard skips the controllers / NICs /
    CD-ROMs sharing the same ``config.hardware.device`` list.
    """
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        if dev.get(_VMOMI_TYPE_NAME_KEY) != _VIRTUAL_DISK_TYPE:
            continue
        if str(dev.get("key")) == disk_id:
            return dev
    return None


async def _resolve_disk_info(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm: str,
    disk: str,
) -> dict[str, Any]:
    """Resolve the REST ``Disk.Info`` (label + current capacity bytes) for a disk.

    Read-only single ``GET:/vcenter/vm/{vm}/hardware/disk/{disk}`` on the
    connector session, shared by the disk-grow park-time preview
    (:mod:`._write_preview`). ``Vcenter.Vm.Hardware.Disk.Info`` carries
    ``label`` + ``capacity`` (bytes) -- the current-capacity half of the
    from→to delta the approver decides on. Raises :exc:`httpx.HTTPError` on
    a transport fault; the preview builder lets it propagate into the #1628
    ``preview_unavailable`` marker (the delta is unknowable, so the park is
    honest about it).
    """
    info = await _read_sub_op(
        connector, target, operator, _OP_GET_VM_DISK, {"vm": vm, "disk": disk}
    )
    payload = _unwrap_value(info)
    return payload if isinstance(payload, dict) else {}


async def _resolve_vm_name(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm: str,
) -> str | None:
    """Best-effort VM display name via ``GET:/vcenter/vm/{vm}``.

    Cosmetic for the disk-grow preview -- a transport fault nulls the name
    rather than sinking the preview, since the current→requested delta (not
    the name) is the decision. The disk read is the load-bearing one.
    """
    try:
        info = await _read_sub_op(connector, target, operator, _OP_GET_VM, {"vm": vm})
    except httpx.HTTPError:
        return None
    payload = _unwrap_value(info)
    name = payload.get("name") if isinstance(payload, dict) else None
    return name if isinstance(name, str) else None


async def vm_disk_grow_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Grow a VM's virtual disk to ``capacity_bytes`` via ``ReconfigVM_Task``.

    Op-id: ``vmware.composite.vm.disk.grow``. The proving op for the
    mutating VI-JSON substrate (#2893): grow has no REST path
    (``Disk.UpdateSpec`` has only ``backing``), so the capacity change goes
    through vim ``VirtualMachine.ReconfigVM_Task`` — a single-device
    ``VirtualDeviceConfigSpec`` with ``operation="edit"`` and the target
    ``VirtualDisk``'s ``capacityInBytes`` raised (spec-verified against
    ``vi-json.yaml``).

    Flow:

    1. Read the VM's ``config.hardware.device`` (vmomi ``RetrievePropertiesEx``,
       a *read* — no governance gate) to obtain the full ``VirtualDisk``
       device (the spec requires the edited device "fully specified") + its
       current ``capacityInBytes``.
    2. **Grow-only by contract**: a request ``<=`` the current capacity is
       refused with ``status="invalid_shrink"`` *before* any write —
       vSphere rejects a shrink anyway; fail early and legibly.
    3. Issue the single ``ReconfigVM_Task`` edit through the governed vmomi
       write seam (:func:`_write_vmomi_sub_op` → the #2254 gate); a
       parked/denied gate returns the :class:`OperationResult` verbatim and
       no reconfigure fires.
    4. Poll the returned ``*_Task`` MoRef to a terminal state via the shared
       :func:`~meho_backplane.connectors.vmware_rest.vim_task.poll_vim_task`
       helper before reporting success. A task fault raises (the dispatcher
       wraps it ``connector_error``, mirroring ``vm.clone``); a poll timeout
       returns ``status="timeout"`` with the task id for the operator.
    """
    vm_moid = params["vm"]
    disk_id = params["disk"]
    requested = int(params["capacity_bytes"])

    devices_result = await connector._post_vmomi_json(
        target,
        _VMOMI_RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=_build_vm_devices_retrieve_params(vm_moid),
    )
    device = _find_virtual_disk(_extract_vm_devices(devices_result), disk_id)
    current = _coerce_int(device.get("capacityInBytes")) if device is not None else None
    if device is None or current is None:
        return {
            "status": "disk_not_found",
            "vm": vm_moid,
            "disk": disk_id,
            "task": None,
            "from_capacity_bytes": current,
            "to_capacity_bytes": requested,
            "delta_bytes": None,
            "guidance": (
                f"no VirtualDisk with key {disk_id!r} (or no readable capacity) on "
                f"vm {vm_moid!r}; list the VM's disks to confirm the disk id"
            ),
        }

    delta = requested - current
    if requested <= current:
        return {
            "status": "invalid_shrink",
            "vm": vm_moid,
            "disk": disk_id,
            "task": None,
            "from_capacity_bytes": current,
            "to_capacity_bytes": requested,
            "delta_bytes": delta,
            "guidance": (
                "vm.disk.grow is grow-only: requested capacity "
                f"{requested} <= current {current} (delta {delta}). vSphere rejects "
                "a disk shrink; re-issue with a capacity greater than the current size"
            ),
        }

    reconfig_spec = {
        "spec": {
            "deviceChange": [
                {"operation": "edit", "device": {**device, "capacityInBytes": requested}}
            ]
        }
    }
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIG_VM_TASK,
        vmomi_path=f"/VirtualMachine/{vm_moid}/ReconfigVM_Task",
        body=reconfig_spec,
        params={"vm": vm_moid, "disk": disk_id, "capacity_bytes": requested},
    )
    if gate is not None:
        return gate

    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_DISK_GROW_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == "error":
        raise RuntimeError(
            f"vm.disk.grow: ReconfigVM_Task on vm {vm_moid!r} disk {disk_id!r} faulted: "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return {
            "status": "timeout",
            "vm": vm_moid,
            "disk": disk_id,
            "task": outcome.task,
            "from_capacity_bytes": current,
            "to_capacity_bytes": requested,
            "delta_bytes": delta,
            "guidance": (
                f"ReconfigVM_Task {outcome.task} did not reach a terminal state within "
                f"{int(_DISK_GROW_TASK_TIMEOUT_SECONDS)}s; poll the task or re-read the disk "
                "capacity — the grow may still complete in the background"
            ),
        }
    return {
        "status": "grown",
        "vm": vm_moid,
        "disk": disk_id,
        "task": outcome.task,
        "from_capacity_bytes": current,
        "to_capacity_bytes": requested,
        "delta_bytes": delta,
        "guidance": None,
    }


# ===========================================================================
# vm.clone_from_template (folder-template CloneVM_Task — Task D, #2894)
# ===========================================================================
#
# Clones a *folder VM template* (a marked-as-template VM in a VM folder) via
# vim ``VirtualMachine.CloneVM_Task``. The existing ``vm.clone`` composite is
# content-library-only (``POST:/vcenter/vm-template/library-items?action=deploy``)
# and a folder template has no clone path on the REST surface at all;
# ``CloneVM_Task`` is the canonical folder-template deploy API (what
# govc/terraform use) and uniquely supports inline guest customization at
# clone time. Rides the #2893 substrate: the mutating CloneVM_Task flows
# through the governed vmomi write seam (:func:`_write_vmomi_sub_op` → the
# #2254 gate) and the returned ``*_Task`` MoRef is driven to a terminal state
# via the shared :func:`~...vim_task.poll_vim_task` helper.


def _moref(mo_type: str, moid: str) -> dict[str, str]:
    """A vim ``ManagedObjectReference`` JSON object (``{type, value}``)."""
    return {"type": mo_type, "value": moid}


def _build_config_template_retrieve_params(vm_moid: str) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` body reading a VM's ``config.template``.

    A single ``PropertyFilterSpec`` scoped to the VirtualMachine requesting
    ``config.template`` -- the boolean that distinguishes a marked-as-template
    VM from a regular one. The singleton ``propertyCollector`` moId rides the
    path, so the body is only the method args (the shape the typed reads +
    the disk-grow config read send).
    """
    return {
        "specSet": [
            {
                "propSet": [{"type": _VIRTUAL_MACHINE_MO_TYPE, "pathSet": [_PROP_CONFIG_TEMPLATE]}],
                "objectSet": [{"obj": {"type": _VIRTUAL_MACHINE_MO_TYPE, "value": vm_moid}}],
            }
        ],
        "options": {},
    }


def _extract_config_template(retrieve_result: Any, vm_moid: str) -> bool | None:
    """Pull the ``config.template`` bool off a RetrievePropertiesEx result.

    Returns the boolean when present, else ``None`` (the caller treats an
    absent / non-bool value as "cannot confirm a template" and refuses the
    clone). Matches the object by moid when the ``obj`` MoRef is present.
    """
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    if not isinstance(objects, list):
        return None
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        obj_moid = obj.get("obj", {}).get("value") if isinstance(obj.get("obj"), dict) else None
        if obj_moid is not None and obj_moid != vm_moid:
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and prop.get("name") == _PROP_CONFIG_TEMPLATE:
                val = prop.get("val")
                return val if isinstance(val, bool) else None
    return None


def _extract_cloned_vm_moid(task_result: Any) -> str | None:
    """Pull the new VM moid out of a SUCCEEDED CloneVM_Task ``result`` MoRef.

    ``TaskInfo.result`` for ``CloneVM_Task`` is the new VirtualMachine
    ``ManagedObjectReference`` (``{"type": "VirtualMachine", "value":
    "vm-99"}``); a bare moid string is tolerated. ``None`` when the shape is
    neither -- the clone still succeeded, the moid is just unreadable.
    """
    if isinstance(task_result, dict):
        value = task_result.get("value")
        return value if isinstance(value, str) else None
    return task_result if isinstance(task_result, str) else None


async def _resolve_template_moid(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    name: str,
) -> tuple[str | None, str | None, list[str]]:
    """Resolve a source-template display name to a unique VM moid.

    Reads ``GET:/vcenter/vm?filter.names=<name>`` (a read sub-op, un-gated)
    and returns ``(moid, error_status, candidates)``:

    * ``(moid, None, [])`` -- exactly one match.
    * ``(None, "template_not_found", [])`` -- no match.
    * ``(None, "ambiguous_template", [moids])`` -- more than one match; the
      operator re-issues against an unambiguous moid.

    Name resolution (not a moid param) is the flow the #2894 body prescribes,
    mirroring the ``vmware.vm.info`` typed read; the ``config.template``
    assert that follows is what proves the resolved VM is actually a template.
    """
    listing = await _read_sub_op(
        connector, target, operator, _OP_LIST_VMS, {"filter.names": [name]}
    )
    rows = _unwrap_value(listing)
    if not isinstance(rows, list):
        return None, "template_not_found", []
    moids = [row["vm"] for row in rows if isinstance(row, dict) and isinstance(row.get("vm"), str)]
    if not moids:
        return None, "template_not_found", []
    if len(moids) > 1:
        return None, "ambiguous_template", moids
    return moids[0], None, []


async def _resolve_customization_spec(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    spec_name: str,
    manager_moid: str,
) -> dict[str, Any]:
    """Resolve a stored GOSC spec by name to its inline vim ``CustomizationSpec``.

    vim ``CloneSpec.customization`` takes a full ``CustomizationSpec`` inline,
    not a by-name reference, so a stored spec (e.g. one created by #2892's
    ``guest.customization_spec.create``) is resolved to its object form via
    ``CustomizationSpecManager.GetCustomizationSpec`` (a *read* -- un-gated,
    routed straight through ``_post_vmomi_json``) and its ``.spec`` field is
    embedded in the clone. Composing here means the clone yields a customized
    VM without a separate ``vm.customize`` dispatch. Raises when the spec name
    does not resolve to a usable ``CustomizationSpecItem`` -- the operator
    named a customization that vCenter cannot supply, so the clone must not
    proceed with a silently-uncustomized VM.
    """
    item = await connector._post_vmomi_json(
        target,
        f"/CustomizationSpecManager/{manager_moid}/GetCustomizationSpec",
        operator=operator,
        json={"name": spec_name},
    )
    payload = _unwrap_value(item)
    spec = payload.get("spec") if isinstance(payload, dict) else None
    if not isinstance(spec, dict):
        raise RuntimeError(
            f"vm.clone_from_template: customization spec {spec_name!r} did not resolve to a "
            f"CustomizationSpecItem with a spec (got {type(spec).__name__}); confirm the GOSC "
            "spec name exists on this vCenter"
        )
    return spec


def _build_clone_body(
    *,
    new_vm_name: str,
    folder_moid: str,
    pool_moid: str,
    datastore_moid: str,
    host_moid: str | None,
    power_on: bool,
    customization: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the ``CloneVM_Task`` request body from the placement params.

    Shape (spec-verified against ``vi-json.yaml``): ``CloneVMRequestType`` =
    ``{folder: Folder-MoRef, name, spec: VirtualMachineCloneSpec}`` where the
    ``CloneSpec`` carries a ``VirtualMachineRelocateSpec`` ``location``
    (``pool`` + ``datastore`` MoRefs, optional ``host`` pin), ``template:
    false`` (clone to a VM, never a template) and ``powerOn``. ``customization``
    (an inline ``CustomizationSpec``) is added only when a GOSC spec was
    requested.
    """
    location: dict[str, Any] = {
        "pool": _moref(_RESOURCE_POOL_MO_TYPE, pool_moid),
        "datastore": _moref(_DATASTORE_MO_TYPE, datastore_moid),
    }
    if host_moid is not None:
        location["host"] = _moref(_HOST_SYSTEM_MO_TYPE, host_moid)
    clone_spec: dict[str, Any] = {"location": location, "template": False, "powerOn": power_on}
    if customization is not None:
        clone_spec["customization"] = customization
    return {"folder": _moref(_FOLDER_MO_TYPE, folder_moid), "name": new_vm_name, "spec": clone_spec}


def _clone_result(
    status: str,
    *,
    source_template: str,
    new_vm_name: str,
    folder: str,
    source_template_id: str | None = None,
    new_vm_id: str | None = None,
    task: str | None = None,
    customization_spec_name: str | None = None,
    candidates: list[str] | None = None,
    guidance: str | None = None,
) -> dict[str, Any]:
    """Build the uniform ``vm.clone_from_template`` response envelope."""
    return {
        "status": status,
        "source_template": source_template,
        "source_template_id": source_template_id,
        "new_vm_name": new_vm_name,
        "new_vm_id": new_vm_id,
        "folder": folder,
        "task": task,
        "customization_spec_name": customization_spec_name,
        "candidates": candidates,
        "guidance": guidance,
    }


async def vm_clone_from_template_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Clone a folder VM template via ``CloneVM_Task``, with optional GOSC.

    Op-id: ``vmware.composite.vm.clone_from_template``. The folder-template
    counterpart to ``vm.clone`` (content-library only). Flow:

    1. Resolve ``source_template`` (a display name) to a unique VM moid via
       ``GET:/vcenter/vm?filter.names=`` -- refuse on no / multiple matches.
    2. Assert the resolved VM is a template (``config.template`` via a vmomi
       ``RetrievePropertiesEx`` *read*) -- refuse a non-template source with
       ``status='not_a_template'`` before any clone.
    3. When ``customization_spec_name`` is set, resolve the stored GOSC spec
       to its inline ``CustomizationSpec`` (``CustomizationSpecManager.
       GetCustomizationSpec``) so the clone customizes in one dispatch.
    4. Issue ``CloneVM_Task`` through the governed vmomi write seam
       (:func:`_write_vmomi_sub_op` → the #2254 gate); a parked/denied gate
       returns the :class:`OperationResult` verbatim and no clone fires.
    5. Poll the returned Task to terminal via
       :func:`~...vim_task.poll_vim_task`. A fault raises (dispatcher wraps
       ``connector_error``, mirroring ``vm.disk.grow``); a timeout returns
       ``status='timeout'`` with the task id; success returns
       ``status='cloned'`` with the new VM moid from ``TaskInfo.result``.
    """
    source_template = params["source_template"]
    new_vm_name = params["new_vm_name"]
    folder_moid = params["folder"]
    pool_moid = params["resource_pool"]
    datastore_moid = params["datastore"]
    host_moid = params.get("host")
    power_on = bool(params.get("power_on", False))
    customization_spec_name = params.get("customization_spec_name")
    manager_moid = params.get(
        "customization_spec_manager_moid", _DEFAULT_CUSTOMIZATION_SPEC_MANAGER_MOID
    )

    template_moid, resolve_error, candidates = await _resolve_template_moid(
        connector, target, operator, name=source_template
    )
    if resolve_error is not None:
        return _clone_result(
            resolve_error,
            source_template=source_template,
            new_vm_name=new_vm_name,
            folder=folder_moid,
            candidates=candidates or None,
            guidance=(
                f"source_template {source_template!r} matched "
                f"{'no VM' if resolve_error == 'template_not_found' else 'more than one VM'}; "
                "re-issue against an unambiguous marked-as-template VM"
            ),
        )
    # ``resolve_error is None`` implies a unique moid resolved (the two are
    # correlated in ``_resolve_template_moid``'s return contract).
    assert template_moid is not None

    template_check = await connector._post_vmomi_json(
        target,
        _VMOMI_RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=_build_config_template_retrieve_params(template_moid),
    )
    if _extract_config_template(template_check, template_moid) is not True:
        return _clone_result(
            "not_a_template",
            source_template=source_template,
            source_template_id=template_moid,
            new_vm_name=new_vm_name,
            folder=folder_moid,
            guidance=(
                f"{source_template!r} ({template_moid}) is not a marked-as-template VM "
                "(config.template is not true); mark it as a template or clone it with "
                "vmware.composite.vm.clone (content-library) instead"
            ),
        )

    customization = None
    if customization_spec_name is not None:
        customization = await _resolve_customization_spec(
            connector,
            target,
            operator,
            spec_name=customization_spec_name,
            manager_moid=manager_moid,
        )

    clone_body = _build_clone_body(
        new_vm_name=new_vm_name,
        folder_moid=folder_moid,
        pool_moid=pool_moid,
        datastore_moid=datastore_moid,
        host_moid=host_moid,
        power_on=power_on,
        customization=customization,
    )
    # Identity-only gate params: they name the blast radius (source, target,
    # placement, spec *name*) for the durable ApprovalRequest without ever
    # serialising the resolved CustomizationSpec's secret-bearing fields
    # (GOSC secret hygiene, #1503).
    gate_params = {
        "source_template": source_template,
        "source_template_id": template_moid,
        "new_vm_name": new_vm_name,
        "folder": folder_moid,
        "resource_pool": pool_moid,
        "datastore": datastore_moid,
        "host": host_moid,
        "power_on": power_on,
        "customization_spec_name": customization_spec_name,
    }
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_CLONE_VM_TASK,
        vmomi_path=f"/VirtualMachine/{template_moid}/CloneVM_Task",
        body=clone_body,
        params=gate_params,
    )
    if gate is not None:
        return gate

    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_CLONE_FROM_TEMPLATE_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == "error":
        raise RuntimeError(
            f"vm.clone_from_template: CloneVM_Task cloning {source_template!r} ({template_moid}) "
            f"to {new_vm_name!r} faulted: {outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return _clone_result(
            "timeout",
            source_template=source_template,
            source_template_id=template_moid,
            new_vm_name=new_vm_name,
            folder=folder_moid,
            task=outcome.task,
            customization_spec_name=customization_spec_name,
            guidance=(
                f"CloneVM_Task {outcome.task} did not reach a terminal state within "
                f"{int(_CLONE_FROM_TEMPLATE_TASK_TIMEOUT_SECONDS)}s; poll the task or list the "
                f"folder — the clone {new_vm_name!r} may still complete in the background"
            ),
        )
    return _clone_result(
        "cloned",
        source_template=source_template,
        source_template_id=template_moid,
        new_vm_name=new_vm_name,
        new_vm_id=_extract_cloned_vm_moid(outcome.result),
        folder=folder_moid,
        task=outcome.task,
        customization_spec_name=customization_spec_name,
    )
