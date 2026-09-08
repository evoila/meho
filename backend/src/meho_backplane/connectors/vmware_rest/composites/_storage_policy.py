# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed NFS tag-based SPBM storage-policy composites (#3494).

NFS-principal datastores have no default VM storage policy (``vSAN Default
Storage Policy`` is vSAN-only), so a **tag-based** policy must be minted before
a vSphere Supervisor can be enabled (its ``EnableSpec`` requires
``master_storage_policy`` / ``ephemeral_storage_policy`` / ``image_storage``,
all policy ids) and before a vSphere Namespace / VKS guest cluster can bind
storage. Creating it out of band (``govc`` / PowerCLI ``New-SpbmStoragePolicy``)
is exactly the escape from policy / audit / approval the dogfooding story
cannot have — hence these governed ops.

Two transports, both first-class (the postulate): the **tag substrate** (tag
category + tag + tag-association) rides the vCenter REST paths
(``POST /cis/tagging/category`` / ``/tag`` / ``/tag-association/{tagId}?action=attach``,
present in the pinned ``vcenter.yaml``) through the connector's REST session;
the **policy create/delete** itself has *no* vCenter REST equivalent — the spec
exposes only ``GET /vcenter/storage/policies`` (read) — so it rides the **PBM
SOAP** API (``PbmProfileProfileManager.PbmCreate`` / ``PbmDelete`` on ``/pbm``,
``urn:pbm``) through the connector's :meth:`~VmwareRestConnector.pbm_create_tag_profile`
/ :meth:`~VmwareRestConnector.pbm_delete_profiles` seam. The agent sees one
op either way.

Governance mirrors the destructive-delete family: ``storage_policy.create`` is
``safety_level="caution"`` + ``requires_approval=True`` (the dispatcher parks it
for a human before the handler runs), ``storage_policy.delete`` is
``safety_level="destructive"`` + ``requires_approval=True``. Every child write —
each REST tag op and the PBM create/delete — flows through the shared
:func:`~meho_backplane.operations.composite.enforce_subop_policy` seam (the
``dangerous`` / ``requires_approval=False`` posture the composite's own human
approval sits above), so each carries its own synchronous audit row and a
per-``(principal, op, target)`` grant point.

The wire shape of the PBM tag rule is grounded on govmomi ``pbm`` +
``govc storage.policy.create`` + the community ``vmware_vm_storage_policy``
Ansible module (see :mod:`~meho_backplane.connectors.vmware_rest.soap`). The
PBM SOAP path is mock-validated (respx fixtures replaying the documented wire
shapes); live-appliance validation is deferred (no lab access at build time).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._write import (
    _CONNECTOR_ID,
    _WRITE_REQUIRES_APPROVAL,
    _WRITE_SAFETY_LEVEL,
    _read_sub_op,
    _write_sub_op,
)
from meho_backplane.operations.composite import enforce_subop_policy

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

# --- Child op-id governance keys -------------------------------------------
# These are the canonical METHOD:/path keys fed to enforce_subop_policy (the
# grant / audit vocabulary), matching the ingested vcenter.yaml rows so a
# per-(principal, op, target) grant lines up. The tagging rows ride the
# generic REST sub-op seam; the two PBM keys are synthetic (PBM has no REST
# path — the dispatch is the typed SOAP seam) but follow the same METHOD:/path
# shape the vim sub-ops use (e.g. POST:/VirtualMachine/{moId}/ReconfigVM_Task).

_OP_CATEGORY_CREATE = "POST:/cis/tagging/category"
_OP_TAG_CREATE = "POST:/cis/tagging/tag"
_OP_TAG_ATTACH = "POST:/cis/tagging/tag-association/{tagId}?action=attach"
_OP_DATASTORE_LIST = "GET:/vcenter/datastore"
_OP_STORAGE_POLICIES_LIST = "GET:/vcenter/storage/policies"
_OP_PBM_CREATE = "POST:/pbm/ProfileManager/PbmCreate"
_OP_PBM_DELETE = "POST:/pbm/ProfileManager/PbmDelete"

#: Tag categories that constrain datastores use MULTIPLE cardinality so a
#: datastore can carry several policy tags at once (the common lab shape).
_TAG_CATEGORY_CARDINALITY = "MULTIPLE"
#: The vim managed-object type a datastore tag-association DynamicID names.
_DATASTORE_OBJECT_TYPE = "Datastore"


async def _gate_pbm_sub_op(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    params: dict[str, Any],
) -> OperationResult | None:
    """Run the shared sub-op governance gate for a PBM SOAP write.

    The PBM twin of ``_write_sub_op``'s gate leg: the PBM create/delete has no
    ingested REST descriptor, so there is nothing to dispatch generically — but
    it is still a governed write, so it flows through the same #2254
    :func:`enforce_subop_policy` seam under the shared ``dangerous`` /
    ``requires_approval=False`` posture (the composite's own human approval sits
    above it). Returns the parked / denied :class:`OperationResult` for the
    caller to bubble verbatim, or ``None`` when the gate clears and the caller
    may issue the SOAP call.
    """
    return await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        safety_level=_WRITE_SAFETY_LEVEL,
        requires_approval=_WRITE_REQUIRES_APPROVAL,
        target=target,
        params=params,
    )


async def _resolve_datastore_moids(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    datastore_names: list[str],
) -> tuple[list[dict[str, str]], str | None]:
    """Resolve each datastore *name* to its moid via ``GET /vcenter/datastore``.

    Returns ``(resolved, error_name)``: ``resolved`` is ``[{name, moid}]`` in
    input order; ``error_name`` names the first datastore that resolved to zero
    or many matches (a fail-closed refuse before any write), else ``None``.
    """
    resolved: list[dict[str, str]] = []
    for name in datastore_names:
        rows = await _read_sub_op(
            connector, target, operator, _OP_DATASTORE_LIST, {"names": [name]}
        )
        matches = [r for r in (rows or []) if isinstance(r, dict) and r.get("name") == name]
        if len(matches) != 1:
            return resolved, name
        resolved.append({"name": name, "moid": str(matches[0]["datastore"])})
    return resolved, None


async def storage_policy_list_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """List the visible storage policies (``GET /vcenter/storage/policies``).

    Op-id ``vmware.composite.storage_policy.list``. Read-only; the set-shaped
    ``policies`` list is JSONFlux-reduced to a result handle by the dispatcher
    when it crosses the byte threshold (postulate 6). Optional ``policy_ids``
    narrows the scan.
    """
    policy_ids = params.get("policy_ids") or []
    rows = await _read_sub_op(connector, target, operator, _OP_STORAGE_POLICIES_LIST, {})
    policies = [r for r in (rows or []) if isinstance(r, dict)]
    if policy_ids:
        wanted = set(policy_ids)
        policies = [p for p in policies if p.get("policy") in wanted]
    return {"policies": policies}


async def _storage_policy_listed(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    policy_id: str,
) -> bool:
    """Read-back: is *policy_id* visible in ``GET /vcenter/storage/policies``?

    Scans the full (uncapped-by-filter) list so a picky filter can never
    mask presence; the list is small in practice (capped at 1024 server-side).
    """
    rows = await _read_sub_op(connector, target, operator, _OP_STORAGE_POLICIES_LIST, {})
    return any(isinstance(r, dict) and r.get("policy") == policy_id for r in (rows or []))


def _create_result(
    *,
    status: str,
    policy_name: str,
    tag_name: str,
    resolved: list[dict[str, str]],
    policy_id: str | None = None,
    category_id: str | None = None,
    tag_id: str | None = None,
    listed: bool = False,
    guidance: str | None = None,
) -> dict[str, Any]:
    """Build the ``storage_policy.create`` response envelope (one shape, all paths)."""
    return {
        "status": status,
        "policy_id": policy_id,
        "policy_name": policy_name,
        "category_id": category_id,
        "tag_id": tag_id,
        "tag_name": tag_name,
        "datastores": resolved,
        "listed": listed,
        "guidance": guidance,
    }


async def _create_tag_substrate(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    category_name: str,
    tag_name: str,
    description: str,
    resolved: list[dict[str, str]],
) -> tuple[OperationResult | None, str | None, str | None]:
    """Create the tag category + tag and attach the tag to each datastore.

    Returns ``(gate, category_id, tag_id)``: ``gate`` is a parked / denied
    :class:`OperationResult` the caller bubbles verbatim (``category_id`` /
    ``tag_id`` then ``None``); on success ``gate`` is ``None`` and both ids are
    populated. Each write flows through the shared sub-op governance seam.
    """
    gate, category_id = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_CATEGORY_CREATE,
        {
            "name": category_name,
            "description": description,
            "cardinality": _TAG_CATEGORY_CARDINALITY,
            "associable_types": [_DATASTORE_OBJECT_TYPE],
        },
    )
    if gate is not None:
        return gate, None, None
    gate, tag_id = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_TAG_CREATE,
        {"name": tag_name, "description": description, "category_id": category_id},
    )
    if gate is not None:
        return gate, category_id, None
    for datastore in resolved:
        gate, _payload = await _write_sub_op(
            connector,
            target,
            operator,
            _OP_TAG_ATTACH,
            {
                "tagId": tag_id,
                "object_id": {"id": datastore["moid"], "type": _DATASTORE_OBJECT_TYPE},
            },
        )
        if gate is not None:
            return gate, category_id, tag_id
    return None, category_id, tag_id


async def _mint_pbm_policy(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    policy_name: str,
    description: str,
    category_name: str,
    category_id: str,
    tag_id: str,
    tag_name: str,
    resolved: list[dict[str, str]],
) -> dict[str, Any] | OperationResult:
    """Gate + create the PBM policy for an already-created tag substrate, then verify.

    Runs the PBM sub-op gate (bubbled verbatim on park / deny), issues
    ``PbmCreate``, and read-backs ``GET /vcenter/storage/policies``. Returns the
    ``policy_create_failed`` result when PbmCreate yields no id (reporting the
    created category / tag for cleanup), else the ``created`` result.

    The PBM tag rule references the category by its **display name**
    (``com.vmware.storage.tag.<category_name>.property``, per the govc / Ansible
    reference), not the ``category_id`` the REST create returned — the id is
    kept only for the response envelope + cleanup guidance.
    """
    gate = await _gate_pbm_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_PBM_CREATE,
        params={"policy_name": policy_name, "category_name": category_name, "tag_name": tag_name},
    )
    if gate is not None:
        return gate
    policy_id = await connector.pbm_create_tag_profile(
        target,
        operator,
        name=policy_name,
        description=description,
        category_name=category_name,
        tag_names=[tag_name],
    )
    if not policy_id:
        return _create_result(
            status="policy_create_failed",
            policy_name=policy_name,
            tag_name=tag_name,
            resolved=resolved,
            category_id=category_id,
            tag_id=tag_id,
            guidance=(
                "PbmCreate returned no policy id after the tag substrate was "
                f"created (category {category_id!r}, tag {tag_id!r}); no policy "
                "exists — clean up the tag/category or retry"
            ),
        )
    listed = await _storage_policy_listed(connector, target, operator, policy_id)
    return _create_result(
        status="created",
        policy_name=policy_name,
        tag_name=tag_name,
        resolved=resolved,
        policy_id=policy_id,
        category_id=category_id,
        tag_id=tag_id,
        listed=listed,
        guidance=(
            None
            if listed
            else (
                f"policy {policy_id!r} was created but did not appear in "
                "GET /vcenter/storage/policies on the immediate read-back; it "
                "may take a moment to propagate — re-list to confirm"
            )
        ),
    )


async def storage_policy_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create a tag-based NFS storage policy end to end (#3494).

    Op-id ``vmware.composite.storage_policy.create``. ``safety_level="caution"``
    + ``requires_approval=True`` — the dispatcher parks it for a human before
    the handler runs; each child write flows through the shared sub-op gate.
    Resolves datastore names to moids (fail-closed ``datastore_not_found``
    before any write), creates the tag category + tag and attaches it to each
    datastore (:func:`_create_tag_substrate`), then mints + verifies the PBM
    policy whose one rule requires the tag (:func:`_mint_pbm_policy`).
    """
    policy_name = params["policy_name"]
    tag_name = params["tag_name"]
    description = params.get("description", "") or ""

    resolved, missing = await _resolve_datastore_moids(
        connector, target, operator, list(params["datastore_names"])
    )
    if missing is not None:
        return _create_result(
            status="datastore_not_found",
            policy_name=policy_name,
            tag_name=tag_name,
            resolved=resolved,
            guidance=(
                f"datastore {missing!r} resolved to zero or several datastores; "
                "no tag substrate or policy was created — pass a name that "
                "matches exactly one datastore"
            ),
        )

    gate, category_id, tag_id = await _create_tag_substrate(
        connector,
        target,
        operator,
        category_name=params["category_name"],
        tag_name=tag_name,
        description=description,
        resolved=resolved,
    )
    if gate is not None:
        return gate

    return await _mint_pbm_policy(
        connector,
        target,
        operator,
        policy_name=policy_name,
        description=description,
        category_name=params["category_name"],
        category_id=str(category_id),
        tag_id=str(tag_id),
        tag_name=tag_name,
        resolved=resolved,
    )


async def storage_policy_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete a storage policy by id and read-back verify it is absent (#3494).

    Op-id ``vmware.composite.storage_policy.delete``. ``safety_level="destructive"``
    + ``requires_approval=True`` — the teardown counterpart of
    ``storage_policy.create``. Gates the PBM delete, issues
    ``PbmProfileProfileManager.PbmDelete`` for the id, then read-backs
    ``GET /vcenter/storage/policies``:

    - a per-id ``PbmDelete`` fault (e.g. the policy is still in use by a VM /
      Supervisor) → ``status="delete_failed"`` with the fault type (no retry
      or force — an in-use policy must be freed first);
    - the read-back still lists the id → ``status="still_present"``;
    - otherwise → ``status="deleted"``.

    Deletes only the **policy**; the tag / category the create minted are left
    in place (they may be shared) — remove them separately if orphaned.
    """
    policy_id = params["policy_id"]
    policy_name = params.get("policy_name")

    gate = await _gate_pbm_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_PBM_DELETE,
        params={"policy_id": policy_id, "policy_name": policy_name},
    )
    if gate is not None:
        return gate

    outcomes = await connector.pbm_delete_profiles(target, operator, profile_ids=[policy_id])
    faulted = next(
        (o for o in outcomes if isinstance(o, dict) and o.get("fault") is not None), None
    )
    if faulted is not None:
        fault = faulted.get("fault")
        fault_type = fault.get("_typeName") if isinstance(fault, dict) else None
        return {
            "status": "delete_failed",
            "policy_id": policy_id,
            "fault": fault_type,
            "guidance": (
                f"PbmDelete refused to remove policy {policy_id!r}"
                + (f" ({policy_name!r})" if policy_name else "")
                + f": {fault_type or 'fault'}. An in-use policy must be freed "
                "(detach it from every VM / Supervisor / namespace) before it "
                "can be deleted."
            ),
        }

    still_present = await _storage_policy_listed(connector, target, operator, policy_id)
    if still_present:
        return {
            "status": "still_present",
            "policy_id": policy_id,
            "fault": None,
            "guidance": (
                f"PbmDelete reported success but policy {policy_id!r} still "
                "appears in GET /vcenter/storage/policies; re-check the target"
            ),
        }
    return {
        "status": "deleted",
        "policy_id": policy_id,
        "fault": None,
        "guidance": None,
    }


# Re-export for the governed-subop manifest (#3349): the child op-ids each
# write composite may fan out to, referenced (never re-typed) by
# ``_governed_subops._GOVERNED_SUBOP_MANIFEST``.
_SUB_OPS_STORAGE_POLICY_CREATE: tuple[str, ...] = (
    _OP_CATEGORY_CREATE,
    _OP_TAG_CREATE,
    _OP_TAG_ATTACH,
    _OP_PBM_CREATE,
)
_SUB_OPS_STORAGE_POLICY_DELETE: tuple[str, ...] = (_OP_PBM_DELETE,)


__all__ = [
    "storage_policy_create_composite",
    "storage_policy_delete_composite",
    "storage_policy_list_composite",
]
