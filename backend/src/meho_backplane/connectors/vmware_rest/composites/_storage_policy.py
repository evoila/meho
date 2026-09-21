# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — the three cohesive storage-policy composites
# (list / create / delete) plus the #3826 resolve-before-create adopt machinery
# (category / tag / policy resolution + conflict checks) that keeps the create
# path idempotent. The adopt helpers are tightly coupled to the create handler's
# envelope; splitting them into a sibling module would fragment one composite's
# logic across two files for no readability gain. Sibling composite handlers
# (_read.py, _write.py) take the same allow for the same reason.

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
from meho_backplane.connectors.vmware_rest.soap_pbm import pbm_tag_property_id
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
_OP_CATEGORY_LIST = "GET:/cis/tagging/category"
_OP_CATEGORY_GET = "GET:/cis/tagging/category/{categoryId}"
_OP_TAG_CREATE = "POST:/cis/tagging/tag"
_OP_TAG_LIST = "GET:/cis/tagging/tag"
_OP_TAG_GET = "GET:/cis/tagging/tag/{tagId}"
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


def _issue(category: str, severity: str, message: str) -> dict[str, Any]:
    """Build one structured ``issues[]`` entry (#3812 convention).

    ``severity == "error"`` is the author-controlled marker that routes the
    envelope through the dispatcher's error-audit path (#3809 /
    :func:`~meho_backplane.operations.dispatcher._composite_terminal_error_code`)
    instead of recording a failed mutation as ``ok``; ``warning`` / ``info``
    stay on the success path (an adopted sub-step is a ``warning``, not a
    failure).
    """
    return {"category": category, "severity": severity, "message": message}


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
    adopted: dict[str, bool] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the ``storage_policy.create`` response envelope (one shape, all paths).

    ``adopted`` reports, per sub-step (``category`` / ``tag`` / ``policy``),
    whether an existing object was reused rather than created — the
    resolve-before-create idempotency of #3826. ``issues`` carries the #3812
    structured entries: a ``warning`` per adopted sub-step on a success
    envelope, or a single ``error`` on a terminal ``*_conflict`` /
    ``policy_create_failed`` envelope (which the dispatcher then surfaces as a
    dispatch error, not a false ``ok``).
    """
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
        "adopted": adopted or {"category": False, "tag": False, "policy": False},
        "issues": list(issues or []),
    }


async def _resolve_category(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    name: str,
) -> tuple[str, dict[str, Any]] | None:
    """Find an existing tag category by display *name* (#3826 resolve-before-create).

    The tagging REST API is id-first: ``GET /cis/tagging/category`` lists ids
    only, so each is fetched (``GET /cis/tagging/category/{categoryId}``) and
    matched on ``name``. Returns ``(category_id, detail)`` for the first match —
    ``detail`` carries ``cardinality`` / ``associable_types`` for the adopt
    compatibility check — or ``None`` when no category of that name exists.
    """
    ids = await _read_sub_op(connector, target, operator, _OP_CATEGORY_LIST, {})
    for category_id in ids or []:
        if not isinstance(category_id, str):
            continue
        detail = await _read_sub_op(
            connector, target, operator, _OP_CATEGORY_GET, {"categoryId": category_id}
        )
        if isinstance(detail, dict) and detail.get("name") == name:
            return category_id, detail
    return None


def _category_conflict_reason(detail: dict[str, Any]) -> str | None:
    """Why an existing category of the right name still cannot be adopted, else ``None``.

    An adopt is only safe when the existing category can carry the datastore tag
    the composite mints: its ``cardinality`` must match the ``MULTIPLE`` the
    fresh path creates (a ``SINGLE`` category would cap a datastore at one policy
    tag), and its ``associable_types`` must permit ``Datastore`` (an empty set
    means "any type", so it is permissive). A mismatch is a terminal
    ``category_conflict`` — the operator must pick a different category name or
    reconcile the existing one by hand.
    """
    cardinality = detail.get("cardinality")
    if cardinality != _TAG_CATEGORY_CARDINALITY:
        return (
            f"existing category cardinality {cardinality!r} != required "
            f"{_TAG_CATEGORY_CARDINALITY!r}"
        )
    associable = detail.get("associable_types") or []
    if associable and _DATASTORE_OBJECT_TYPE not in associable:
        return (
            f"existing category associable_types {sorted(associable)!r} does not "
            f"permit {_DATASTORE_OBJECT_TYPE!r}"
        )
    return None


async def _resolve_tag_in_category(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    tag_name: str,
    category_id: str,
) -> str | None:
    """Find an existing tag by *tag_name* **within** *category_id* (#3826).

    Same id-first walk as :func:`_resolve_category` over
    ``GET /cis/tagging/tag`` + ``GET /cis/tagging/tag/{tagId}``, matched on both
    ``name`` and ``category_id`` (a tag name is only unique inside its category).
    Returns the tag id, or ``None`` when no such tag exists yet.
    """
    ids = await _read_sub_op(connector, target, operator, _OP_TAG_LIST, {})
    for tag_id in ids or []:
        if not isinstance(tag_id, str):
            continue
        detail = await _read_sub_op(connector, target, operator, _OP_TAG_GET, {"tagId": tag_id})
        if (
            isinstance(detail, dict)
            and detail.get("name") == tag_name
            and detail.get("category_id") == category_id
        ):
            return tag_id
    return None


async def _ensure_category(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    category_name: str,
    description: str,
) -> tuple[OperationResult | None, str | None, str | None, bool]:
    """Adopt an existing tag category of *category_name*, or create it.

    Returns ``(gate, conflict_reason, category_id, adopted)``. ``gate`` is a
    parked / denied :class:`OperationResult` the caller bubbles verbatim (from
    the create sub-op's governance seam); ``conflict_reason`` is set (with the
    existing ``category_id``) when a same-named category cannot be adopted
    (:func:`_category_conflict_reason`); otherwise ``category_id`` is the adopted
    or freshly-created id and ``adopted`` says which. Adoption issues **no**
    write, so it carries no gate.
    """
    existing = await _resolve_category(connector, target, operator, category_name)
    if existing is not None:
        category_id, detail = existing
        reason = _category_conflict_reason(detail)
        if reason is not None:
            return None, reason, category_id, False
        return None, None, category_id, True
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
        return gate, None, None, False
    return None, None, str(category_id), False


async def _ensure_tag(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    tag_name: str,
    category_id: str,
    description: str,
) -> tuple[OperationResult | None, str | None, bool]:
    """Adopt an existing tag of *tag_name* in *category_id*, or create it.

    Returns ``(gate, tag_id, adopted)`` — the tag counterpart of
    :func:`_ensure_category`. Adoption issues no write (no gate); the create
    flows through the shared sub-op governance seam.
    """
    existing_tag_id = await _resolve_tag_in_category(
        connector, target, operator, tag_name, category_id
    )
    if existing_tag_id is not None:
        return None, existing_tag_id, True
    gate, tag_id = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_TAG_CREATE,
        {"name": tag_name, "description": description, "category_id": category_id},
    )
    if gate is not None:
        return gate, None, False
    return None, str(tag_id), False


async def _attach_tag_to_datastores(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    tag_id: str,
    resolved: list[dict[str, str]],
) -> OperationResult | None:
    """Attach *tag_id* to every resolved datastore; ``None`` on success.

    ``TagAssociation.attach`` is idempotent — re-attaching an already-attached
    tag is a no-op that returns success, not an error (grounded on the vSphere
    Automation API) — so this needs no read-before-write: a retry after a
    partial run safely re-attaches. Returns a parked / denied
    :class:`OperationResult` (the caller bubbles it) if a per-datastore gate does
    not clear, else ``None``.
    """
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
            return gate
    return None


async def _resolve_policy_by_name(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    policy_name: str,
) -> dict[str, Any] | None:
    """Find an existing storage policy by display *name* via the REST list (#3826).

    ``GET /vcenter/storage/policies`` returns ``{policy, name, ...}`` rows for
    every visible policy (tag-based ones included), so a name lookup needs no
    PBM round-trip. Returns the first matching row (carrying the ``policy`` id),
    or ``None`` when no policy of that name exists.
    """
    rows = await _read_sub_op(connector, target, operator, _OP_STORAGE_POLICIES_LIST, {})
    return next(
        (r for r in (rows or []) if isinstance(r, dict) and r.get("name") == policy_name),
        None,
    )


async def _create_pbm_policy(
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
    adopted: dict[str, bool],
    issues: list[dict[str, Any]],
) -> dict[str, Any] | OperationResult:
    """Gate + ``PbmCreate`` a fresh policy for an ensured tag substrate, then verify.

    The PBM tag rule references the category by its **display name**
    (``com.vmware.storage.tag.<category_name>.property``, per the govc / Ansible
    reference), not the ``category_id`` the REST create returned. On a
    ``PbmCreate`` that yields no id, returns a terminal ``policy_create_failed``
    error envelope (#3812 ``error``-severity issue) whose guidance says to
    **retry** — the ensured category / tag will simply be adopted, so there is
    no orphan to clean up (#3826, the "no orphan narrative" contract).
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
            adopted=adopted,
            issues=[
                *issues,
                _issue(
                    "connector",
                    "error",
                    f"PbmCreate returned no policy id for {policy_name!r}",
                ),
            ],
            guidance=(
                "PbmCreate returned no policy id after the tag substrate was "
                f"ensured (category {category_id!r}, tag {tag_id!r}); no policy "
                "exists — retry the same call: the existing category / tag are "
                "adopted, so no manual cleanup is needed"
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
        adopted=adopted,
        issues=issues,
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


async def _mint_or_adopt_pbm_policy(
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
    adopted: dict[str, bool],
    issues: list[dict[str, Any]],
) -> dict[str, Any] | OperationResult:
    """Adopt an existing same-named policy with a matching rule set, else create it.

    Resolve-before-create for the policy (#3826): if a policy of *policy_name*
    already exists, compare its tag-rule signature
    (:meth:`~VmwareRestConnector.pbm_retrieve_profile_tag_rules`) against the one
    rule this op would mint — the category property constrained to ``[tag_name]``.
    Equal → **adopt** (status ``adopted``, an ``issues[]`` warning, no
    ``PbmCreate``); different → terminal ``policy_conflict`` (#3812
    ``error``-severity issue). No same-named policy → :func:`_create_pbm_policy`.
    """
    intended = {(pbm_tag_property_id(category_name), frozenset({tag_name}))}
    existing = await _resolve_policy_by_name(connector, target, operator, policy_name)
    if existing is not None:
        existing_id = str(existing.get("policy"))
        signatures = await connector.pbm_retrieve_profile_tag_rules(
            target, operator, profile_ids=[existing_id]
        )
        if signatures.get(existing_id) == intended:
            adopted["policy"] = True
            return _create_result(
                status="adopted",
                policy_name=policy_name,
                tag_name=tag_name,
                resolved=resolved,
                policy_id=existing_id,
                category_id=category_id,
                tag_id=tag_id,
                listed=True,
                adopted=adopted,
                issues=[
                    *issues,
                    _issue(
                        "state",
                        "warning",
                        f"adopted existing storage policy {policy_name!r} "
                        f"({existing_id}) with a matching tag rule set",
                    ),
                ],
            )
        return _create_result(
            status="policy_conflict",
            policy_name=policy_name,
            tag_name=tag_name,
            resolved=resolved,
            policy_id=existing_id,
            category_id=category_id,
            tag_id=tag_id,
            adopted=adopted,
            issues=[
                *issues,
                _issue(
                    "state",
                    "error",
                    f"storage policy {policy_name!r} already exists ({existing_id}) "
                    "with a different rule set",
                ),
            ],
            guidance=(
                f"a storage policy named {policy_name!r} already exists but "
                "constrains a different category / tag set; no policy was created "
                "— pick a different policy name or delete the existing policy first"
            ),
        )
    return await _create_pbm_policy(
        connector,
        target,
        operator,
        policy_name=policy_name,
        description=description,
        category_name=category_name,
        category_id=category_id,
        tag_id=tag_id,
        tag_name=tag_name,
        resolved=resolved,
        adopted=adopted,
        issues=issues,
    )


async def storage_policy_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create a tag-based NFS storage policy end to end, idempotently (#3494, #3826).

    Op-id ``vmware.composite.storage_policy.create``. ``safety_level="caution"``
    + ``requires_approval=True`` — the dispatcher parks it for a human before
    the handler runs; each child write flows through the shared sub-op gate.

    **Resolve-before-create (#3826):** a partial run that created the tag
    substrate then failed (e.g. a ``PbmCreate`` fault) used to make the retry
    die at the category create with ``ALREADY_EXISTS``. Each sub-step now adopts
    an existing object keyed by name — the tag **category**
    (:func:`_ensure_category`, cardinality / associable-types checked, else a
    terminal ``category_conflict``), the **tag** in it (:func:`_ensure_tag`),
    the datastore **attachments** (idempotent ``attach``), and the **policy**
    (:func:`_mint_or_adopt_pbm_policy`, same rule set → adopt, different →
    ``policy_conflict``). Adopted sub-steps are reported in the ``adopted`` map
    and as ``issues[]`` warnings; the fresh path is unchanged. Datastore names
    resolve to moids first (fail-closed ``datastore_not_found`` before any
    write).
    """
    policy_name = params["policy_name"]
    category_name = params["category_name"]
    tag_name = params["tag_name"]
    description = params.get("description", "") or ""
    adopted: dict[str, bool] = {"category": False, "tag": False, "policy": False}
    issues: list[dict[str, Any]] = []

    resolved, missing = await _resolve_datastore_moids(
        connector, target, operator, list(params["datastore_names"])
    )
    if missing is not None:
        return _create_result(
            status="datastore_not_found",
            policy_name=policy_name,
            tag_name=tag_name,
            resolved=resolved,
            issues=[
                _issue(
                    "input",
                    "error",
                    f"datastore {missing!r} resolved to zero or several datastores",
                )
            ],
            guidance=(
                f"datastore {missing!r} resolved to zero or several datastores; "
                "no tag substrate or policy was created — pass a name that "
                "matches exactly one datastore"
            ),
        )

    gate, conflict, category_id, cat_adopted = await _ensure_category(
        connector,
        target,
        operator,
        category_name=category_name,
        description=description,
    )
    if gate is not None:
        return gate
    if conflict is not None:
        return _create_result(
            status="category_conflict",
            policy_name=policy_name,
            tag_name=tag_name,
            resolved=resolved,
            category_id=category_id,
            adopted=adopted,
            issues=[
                _issue(
                    "state",
                    "error",
                    f"tag category {category_name!r} exists but cannot be adopted: {conflict}",
                )
            ],
            guidance=(
                f"tag category {category_name!r} already exists with an "
                f"incompatible shape ({conflict}); no tag substrate or policy "
                "was created — pick a different category name or reconcile the "
                "existing category"
            ),
        )
    adopted["category"] = cat_adopted
    if cat_adopted:
        issues.append(
            _issue(
                "state",
                "warning",
                f"adopted existing tag category {category_name!r} ({category_id})",
            )
        )

    gate, tag_id, tag_adopted = await _ensure_tag(
        connector,
        target,
        operator,
        tag_name=tag_name,
        category_id=str(category_id),
        description=description,
    )
    if gate is not None:
        return gate
    adopted["tag"] = tag_adopted
    if tag_adopted:
        issues.append(
            _issue(
                "state",
                "warning",
                f"adopted existing tag {tag_name!r} ({tag_id}) in category {category_name!r}",
            )
        )

    gate = await _attach_tag_to_datastores(
        connector, target, operator, tag_id=str(tag_id), resolved=resolved
    )
    if gate is not None:
        return gate

    return await _mint_or_adopt_pbm_policy(
        connector,
        target,
        operator,
        policy_name=policy_name,
        description=description,
        category_name=category_name,
        category_id=str(category_id),
        tag_id=str(tag_id),
        tag_name=tag_name,
        resolved=resolved,
        adopted=adopted,
        issues=issues,
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
