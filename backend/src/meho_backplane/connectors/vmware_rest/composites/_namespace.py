# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed vSphere Namespace ``vmware.composite.namespace.*`` handlers (#3502).

A vSphere Namespace on an enabled Supervisor (#3281) is the container a VKS
guest cluster is created into; it binds an SPBM storage policy (#3494), VM
classes, and a TKr/VKr content library (#3495). VKS guest-cluster lifecycle
then runs entirely from *inside* the Supervisor via Kubernetes CRDs -- there
is no ``/tkg/*`` vCenter REST family -- so the Namespace create is the last
vCenter-REST step before the flow moves to ``k8s.apply`` of a ``Cluster`` CR
on the Supervisor target. Creating the namespace out of band (``kubectl`` /
raw REST) escapes the governed policy / audit / approval path the demo is
built to show; these two composites close that gap:

* ``vmware.composite.namespace.create`` --
  ``POST /vcenter/namespaces/instances/v2`` with the
  ``Namespaces.Instances.CreateSpecV2`` body (``supervisor`` + ``namespace``
  + optional ``access_list`` / ``storage_specs`` / ``vm_service_spec``) -> 204.
  ``safety_level="caution"`` + ``requires_approval=True`` (a write that always
  parks for a human decision, but not the intrinsic destruction tier). Reads
  the created namespace back (``GET /vcenter/namespaces/instances/{namespace}``)
  to surface ``config_status``.
* ``vmware.composite.namespace.delete`` --
  ``DELETE /vcenter/namespaces/instances/{namespace}`` -> 204.
  ``safety_level="destructive"`` + ``requires_approval=True`` (mandatory human
  approval; a ``DELETE`` child is never grant-clearable, so this always parks).
  Read-back verifies absence (``GET`` 404) -> ``status="deleted"``.
* ``vmware.composite.namespace.status`` --
  ``GET /vcenter/namespaces/instances/{namespace}`` -> the scalar
  ``config_status`` (``CONFIGURING`` -> ``RUNNING`` / ``ERROR`` / ``REMOVING``)
  + a derived ``ready`` flag + ``stats`` + capped ``messages``.
  ``safety_level="safe"`` + ``requires_approval=False``. The **governed,
  boot-enabled** poll op ``namespace.create`` / ``.delete`` hand the caller for
  the asynchronous convergence poll -- symmetric with
  ``vmware.composite.supervisor.status``.

The **list** read (``GET /vcenter/namespaces/instances`` / ``.../v2``) is left
to the already-enabled ingested rows; the dispatcher JSONFlux-reduces the
set-shaped list automatically. The single-namespace **get** is wrapped as the
boot-enabled ``namespace.status`` composite above -- *not* left to the ingested
``GET /vcenter/namespaces/instances/{namespace}`` row, because that row lands
``is_enabled=False`` behind per-deployment operator review, so a caller polling
create/delete convergence on a fresh boot would have no governed read. A typed
read composite is dispatchable at connector import on every deployment
(symmetric with ``supervisor.status``); the create / delete read-backs share
the same un-gated ``_read_sub_op`` seam directly.

Generic-vs-typed
----------------

The POST / DELETE paths are present in the pinned ``vcenter.yaml`` and land as
ingested ``endpoint_descriptor`` rows, and ``vmware_rest`` is a **real**
connector (it overrides
:meth:`~...connector.VmwareRestConnector.mount_op_path`), so a raw ingested
row *could* dispatch through ``httpx``. Generic dispatch is nonetheless
insufficient here, for the four reasons the DAG-sibling deps (#3281 supervisor,
#3495 content-library) already documented:

1. **Dispatchable at merge.** Ingested rows land ``is_enabled=False`` behind
   the per-deployment operator review state machine; enabling them is a runtime
   ``ReviewService.edit_op`` DB mutation, not a code artifact a PR merges. A
   merged generic-only change would leave the write disabled on every
   deployment until an operator vets it. A typed composite lands
   ``is_enabled=True`` at connector import (the author vouches at code-review
   time) and is dispatchable immediately -- load-bearing for the demo freeze.
2. **Zero-catalog-ingest boot.** Every governed ``vmware_rest`` write ships as
   a composite so the governed surface does not depend on a runtime spec-ingest
   + operator review having happened; a raw ingested row reintroduces that
   dependency.
3. **Delete read-back verify.** A raw ingested ``DELETE`` returns 204 and
   stops; it cannot read-back-verify absence (the DoD's "delete -> absent"). A
   composite verifies via the namespace ``GET``.
4. **Destructive blast-radius.** The destructive park gate is fail-closed on a
   missing ``blast_radius`` block; only a composite with a ``_write_preview``
   builder can populate it.

Governance seam
---------------

Both writes ride the same #2254 governed REST sub-op seam the other REST write
composites use
(:func:`~meho_backplane.connectors.vmware_rest.composites._write._write_sub_op`
-> :func:`~meho_backplane.operations.composite.enforce_subop_policy` ->
``_post_json``): the top-level composite's own approval posture parks for a
human at dispatch, and the governed child POST / DELETE re-applies policy under
the shared ``dangerous`` / ``requires_approval=False`` sub-op posture. The
read-backs ride the un-gated read sub-op seam
(:func:`~...composites._write._read_sub_op`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._write import (
    _read_sub_op,
    _unwrap_value,
    _write_sub_op,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "namespace_create_composite",
    "namespace_delete_composite",
    "namespace_status_composite",
]


# Canonical ``METHOD:/path`` op_ids -- byte-for-byte the strings the ingest
# parser emits from the pinned ``vcenter.yaml``. The ``{namespace}`` path var
# is the namespace name (the instances API keys a single namespace by name; it
# has no ``/v2/`` GET-by-name variant -- the v2 form is create/list only).
# These are the governance op_ids fed to ``enforce_subop_policy`` via
# ``_write_sub_op`` and the read seam via ``_read_sub_op``; the reconcile lane
# (``tests/test_connectors_vmware_rest_namespace_reconcile.py``) pins each
# against the canonical pinned spec.
_OP_CREATE_NAMESPACE: Final = "POST:/vcenter/namespaces/instances/v2"
_OP_DELETE_NAMESPACE: Final = "DELETE:/vcenter/namespaces/instances/{namespace}"
_OP_GET_NAMESPACE: Final = "GET:/vcenter/namespaces/instances/{namespace}"

#: REST governed-child manifests (parallel to ``_write._SUB_OPS_*``).
#: Referenced by :mod:`._governed_subops` so the discovery surface publishes
#: each write composite's grant set, and asserted by the reconcile lane. The
#: read-back ``GET`` is NOT a governed child (it rides the un-gated read seam),
#: so it is deliberately absent here -- mirroring ``_supervisor``.
_SUB_OPS_NAMESPACE_CREATE: tuple[str, ...] = (_OP_CREATE_NAMESPACE,)
_SUB_OPS_NAMESPACE_DELETE: tuple[str, ...] = (_OP_DELETE_NAMESPACE,)

#: The v2 create-spec pass-through fields beyond the two required scalars.
_OPTIONAL_CREATE_SPEC_FIELDS: Final[tuple[str, ...]] = (
    "access_list",
    "storage_specs",
    "vm_service_spec",
)

#: ``Namespaces.Instances.Info.config_status`` value that means the namespace
#: is being asynchronously torn down (delete accepted, workloads draining).
_CONFIG_STATUS_REMOVING: Final = "REMOVING"

#: ``Namespaces.Instances.Info.config_status`` value that means the namespace
#: has finished configuring and is ready for VKS guest clusters -- the single
#: poll predicate ``namespace.status`` exposes as ``ready``.
_CONFIG_STATUS_RUNNING: Final = "RUNNING"

#: Default inline cap on the ``namespace.status`` ``messages`` array (mirrors
#: ``supervisor.status``): keeps the status envelope pollable without a
#: JSONFlux handle. ``message_count`` always carries the uncapped size.
_STATUS_MESSAGES_DEFAULT_CAP: Final = 25


async def _read_namespace_info(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    namespace: str,
) -> dict[str, Any] | None:
    """Read a namespace's ``Info`` back; return the dict, or ``None`` when absent.

    Issues the un-gated ``GET /vcenter/namespaces/instances/{namespace}`` read
    sub-op. A vCenter 404 (the namespace does not exist) is the load-bearing
    read-back-absence signal -- it returns ``None`` rather than raising. Any
    other transport / status fault propagates for the dispatcher to wrap
    ``connector_error``.
    """
    try:
        payload = await _read_sub_op(
            connector, target, operator, _OP_GET_NAMESPACE, {"namespace": namespace}
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    info = _unwrap_value(payload)
    return info if isinstance(info, dict) else {}


async def namespace_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create a vSphere Namespace on an enabled Supervisor (#3502).

    Op-id: ``vmware.composite.namespace.create``. ``safety_level="caution"`` +
    ``requires_approval=True`` -- the dispatcher parks it for a human before
    the handler runs. Assembles the ``Namespaces.Instances.CreateSpecV2`` body
    from ``supervisor`` + ``namespace`` plus the optional pass-through
    ``access_list`` / ``storage_specs`` (the #3494 SPBM policy ids) /
    ``vm_service_spec`` (the #3495 content-library ids + VM classes), and issues
    ``POST /vcenter/namespaces/instances/v2`` through the governed REST write
    seam. A parked / denied gate returns the :class:`OperationResult` verbatim
    and no create fires.

    The create is asynchronous vCenter-side (the namespace moves
    ``CONFIGURING`` -> ``RUNNING``); the composite read-backs the namespace
    ``GET`` best-effort to surface ``config_status`` and returns
    ``status="created"``. A read-back that 404s (not yet visible) or faults
    leaves ``config_status=None`` -- the create was still accepted.
    """
    supervisor = params["supervisor"]
    namespace = params["namespace"]

    body: dict[str, Any] = {"supervisor": supervisor, "namespace": namespace}
    for field in _OPTIONAL_CREATE_SPEC_FIELDS:
        value = params.get(field)
        if value is not None:
            body[field] = value

    gate, _payload = await _write_sub_op(connector, target, operator, _OP_CREATE_NAMESPACE, body)
    if gate is not None:
        return gate

    config_status: str | None = None
    try:
        info = await _read_namespace_info(connector, target, operator, namespace)
    except httpx.HTTPError:
        info = None
    if isinstance(info, dict):
        status_value = info.get("config_status")
        config_status = status_value if isinstance(status_value, str) else None

    return {
        "status": "created",
        "namespace": namespace,
        "supervisor": supervisor,
        "config_status": config_status,
        "guidance": (
            "vSphere Namespace create accepted (asynchronous). The namespace "
            "moves config_status CONFIGURING -> RUNNING; poll "
            "vmware.composite.namespace.status (the governed, boot-enabled read "
            "composite -- it returns ready==true once config_status is RUNNING) "
            "before creating a VKS guest cluster into it. config_status == "
            "'ERROR' needs operator intervention."
        ),
    }


async def namespace_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete a vSphere Namespace and read-back verify absence (#3502).

    Op-id: ``vmware.composite.namespace.delete``. ``safety_level="destructive"``
    + ``requires_approval=True`` -- mandatory human approval, no standing grant
    (the ``DELETE`` child is never grant-clearable, so this always parks).
    Issues ``DELETE /vcenter/namespaces/instances/{namespace}`` through the
    governed REST write seam; a parked / denied gate returns the
    :class:`OperationResult` verbatim and no teardown fires.

    Deleting a namespace cascades -- it destroys every workload inside it (VKS
    guest clusters, pods, PVCs). The delete is asynchronous, so the read-back
    resolves to one of three statuses:

    - the ``GET`` 404s (namespace gone) -> ``status="deleted"`` (the verified
      happy path);
    - the ``GET`` reports ``config_status="REMOVING"`` -> ``status="removing"``
      (delete accepted, teardown draining -- re-read until it 404s);
    - the ``GET`` still reports a non-REMOVING status -> ``status="still_present"``
      (accepted but nothing changed -- re-check the target).
    """
    namespace = params["namespace"]

    gate, _payload = await _write_sub_op(
        connector, target, operator, _OP_DELETE_NAMESPACE, {"namespace": namespace}
    )
    if gate is not None:
        return gate

    info = await _read_namespace_info(connector, target, operator, namespace)
    if info is None:
        return {
            "status": "deleted",
            "namespace": namespace,
            "config_status": None,
            "guidance": None,
        }

    status_value = info.get("config_status")
    config_status = status_value if isinstance(status_value, str) else None
    if config_status == _CONFIG_STATUS_REMOVING:
        return {
            "status": "removing",
            "namespace": namespace,
            "config_status": config_status,
            "guidance": (
                "delete accepted; the namespace is asynchronously REMOVING "
                "(workloads inside are being torn down). Poll "
                "vmware.composite.namespace.status until it reports "
                "exists==false to confirm removal."
            ),
        }
    return {
        "status": "still_present",
        "namespace": namespace,
        "config_status": config_status,
        "guidance": (
            f"DELETE was accepted but the namespace still reports config_status "
            f"{config_status!r} (not REMOVING); re-check the target."
        ),
    }


def _project_messages(items: Any, cap: int) -> tuple[list[dict[str, Any]], int]:
    """Cap + project a namespace ``messages`` array; return ``(capped, total)``.

    Keeps the array small + inline so ``namespace.status`` stays pollable
    without a JSONFlux handle. Non-dict rows are dropped; ``total`` is the
    uncapped count so a caller sees the true size even when the list is
    truncated. Mirrors ``_supervisor._project_messages``.
    """
    if not isinstance(items, list):
        return [], 0
    rows = [row for row in items if isinstance(row, dict)]
    return rows[:cap], len(rows)


async def namespace_status_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Read a vSphere Namespace's config status -- poll-friendly + boot-enabled (#3502).

    Op-id: ``vmware.composite.namespace.status``. ``safety_level="safe"`` +
    ``requires_approval=False``. Issues the un-gated
    ``GET /vcenter/namespaces/instances/{namespace}`` read and reshapes
    ``Namespaces.Instances.Info`` into a compact, inline-pollable envelope: the
    scalar ``config_status`` (``CONFIGURING`` / ``REMOVING`` / ``RUNNING`` /
    ``ERROR``) and the derived ``ready`` flag stay top-level so a runbook
    ``OperationCallVerify`` step or a Sensor assertion can poll
    ``config_status == 'RUNNING'`` / ``ready == true`` directly -- never behind
    a set-shaped handle.

    This is the **governed, boot-enabled** poll op ``namespace.create`` /
    ``.delete`` hand the caller. Unlike the raw ingested
    ``GET /vcenter/namespaces/instances/{namespace}`` row -- which lands
    ``is_enabled=False`` behind per-deployment operator review -- a typed read
    composite is dispatchable at connector import on every deployment,
    symmetric with ``vmware.composite.supervisor.status``.

    A ``GET`` 404 (the namespace does not exist -- not yet visible mid-create,
    or gone after delete) is a normal poll answer, returned as ``exists=False``
    / ``config_status=None`` / ``ready=False`` rather than a fault; any other
    transport / status fault propagates for the dispatcher to wrap
    ``connector_error``. The ``messages`` array is capped inline to
    :data:`_STATUS_MESSAGES_DEFAULT_CAP` (override with ``messages_limit``);
    ``message_count`` carries the uncapped size. Read-only.
    """
    namespace = params["namespace"]
    cap = params.get("messages_limit", _STATUS_MESSAGES_DEFAULT_CAP)

    info = await _read_namespace_info(connector, target, operator, namespace)
    if info is None:
        return {
            "namespace": namespace,
            "exists": False,
            "config_status": None,
            "ready": False,
            "stats": None,
            "description": None,
            "messages": [],
            "message_count": 0,
        }

    status_value = info.get("config_status")
    config_status = status_value if isinstance(status_value, str) else None
    stats = info.get("stats")
    description = info.get("description")
    messages, message_count = _project_messages(info.get("messages"), cap)
    return {
        "namespace": namespace,
        "exists": True,
        "config_status": config_status,
        "ready": config_status == _CONFIG_STATUS_RUNNING,
        "stats": stats if isinstance(stats, dict) else None,
        "description": description if isinstance(description, str) else None,
        "messages": messages,
        "message_count": message_count,
    }
