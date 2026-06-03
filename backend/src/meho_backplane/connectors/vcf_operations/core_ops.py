# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vROps 9.0 read-only v0.5 core — curated operator-enabled subset.

This module names the **8 read-only vROps operations** the G3.6
vcf-operations v0.5 ship enables out of the much larger vROps
``/suite-api`` corpus the G0.7 spec-ingestion pipeline lands under
``connector_id="vrops-rest-9.0"``. The curation is two-layered:

* :data:`VROPS_CORE_GROUPS` — the operator-reviewed ``when_to_use``
  hint per LLM-grouping pass output group. Each entry's ``group_key``
  is the deterministic slug :func:`classify_vrops_op` assigns to vROps
  ops; the ``when_to_use`` is what the agent reads verbatim through
  :func:`~meho_backplane.operations.meta_tools.list_operation_groups`
  to pick a group to search within.
* :data:`VROPS_CORE_OPS` — the 8 ``EndpointDescriptor.op_id`` strings
  that flip to ``is_enabled=True`` at operator-review time, paired
  with the per-op ``llm_instructions`` blob the agent inlines into
  the reasoning context when it sees the op in
  :func:`~meho_backplane.operations.meta_tools.search_operations`
  hits. Every other op under the same connector triple stays
  ``is_enabled=False`` (the G0.7 ingestion default for
  ``source_kind='ingested'`` rows).

Per Initiative #369 and CLAUDE.md postulates 1-2, vROps is **fully
generic-ingested**: the underlying ops are not registered in code,
they live in the ``endpoint_descriptor`` table. This module only
carries the **operator-review metadata** the substrate uses at the
review step — the actual curation is applied through
:func:`apply_vrops_core_curation` against an existing ingested
connector.

The 8 ops (paths cross-checked against the Broadcom vROps suite-api
docs at https://developer.broadcom.com/xapis/vrealize-operations-manager-api/latest/):

1. ``GET:/suite-api/api/versions/current`` — ``vrops.about`` — appliance
   version + build (the same surface :meth:`VcfOperationsConnector.fingerprint`
   already consumes; exposing it as an operator-callable op lets the agent
   run a sanity probe before any heavier read).
2. ``GET:/suite-api/api/resources`` — ``vrops.resource.list`` — resource
   inventory filterable by ``resourceKind``, ``name``, or ``adapterKind``.
3. ``GET:/suite-api/api/resources/{id}`` — ``vrops.resource.get`` — full
   detail for one resource by id, including identifiers and credential
   refs (no secret values).
4. ``GET:/suite-api/api/alerts`` — ``vrops.alert.list`` — current and
   historical alerts (filter by ``activeOnly``, ``alertCriticality``).
5. ``GET:/suite-api/api/alertdefinitions`` — ``vrops.alertdefinition.list``
   — the configured alert definitions (the policy surface the alert
   list rolls up against).
6. ``GET:/suite-api/api/symptoms`` — ``vrops.symptom.list`` — currently
   firing symptoms; the per-condition signal that rolls up into alerts.
7. ``GET:/suite-api/api/recommendations`` — ``vrops.recommendation.list``
   — operator-facing remediation hints attached to alerts / symptoms.
8. ``GET:/suite-api/api/supermetrics`` — ``vrops.supermetric.list`` —
   user-defined metric formulae, useful when answering "which metric
   formula was used to compute X".

Path families and group_keys
-----------------------------

vROps' suite-api is flat — every path is under ``/suite-api/api/<noun>``
with no deep hierarchy. :data:`VROPS_PATH_RULES` lists the curated
prefixes in most-specific-first order so the ``startswith`` loop in
:func:`classify_vrops_op` terminates at the right group:

* ``/suite-api/api/versions`` → ``vrops-system``.
* ``/suite-api/api/resources`` → ``vrops-resources``.
* ``/suite-api/api/alertdefinitions`` → ``vrops-alert-definitions``.
  (Must precede ``/suite-api/api/alerts`` to avoid the broader prefix
  consuming it.)
* ``/suite-api/api/alerts`` → ``vrops-alerts``.
* ``/suite-api/api/symptoms`` → ``vrops-symptoms``.
* ``/suite-api/api/recommendations`` → ``vrops-recommendations``.
* ``/suite-api/api/supermetrics`` → ``vrops-supermetrics``.

The ``alertdefinitions`` rule must precede the ``alerts`` rule because
``startswith("/suite-api/api/alerts")`` would otherwise eat the
``/suite-api/api/alertdefinitions`` path. The rule ordering is
load-bearing; :func:`classify_vrops_op` documents the loop contract.

Curation application
--------------------

:func:`apply_vrops_core_curation` is the operator-review-time
substrate call that makes exactly the 8 curated ops dispatchable.
Mirrors :func:`apply_nsx_core_curation` and
:func:`apply_harbor_core_curation` verbatim, threading the
"enable group but pin non-core ops disabled" needle via the
audit-log-driven operator-override exclusion.

Write-ops are deliberately excluded
------------------------------------

vROps' write surface (custom-group create / maintenance-mode set /
alert-acknowledge) stays ``is_enabled=False`` in v0.5 per the
Initiative #369 out-of-scope list. The curation helper's
"enable group but pin non-core ops disabled" pattern keeps those
rows on the connector for future enablement without exposing them
to the agent until an explicit follow-up Task lands their
``llm_instructions`` + safety review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog

from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "VROPS_CONNECTOR_ID",
    "VROPS_CORE_GROUPS",
    "VROPS_CORE_OPS",
    "VROPS_IMPL_ID",
    "VROPS_PATH_RULES",
    "VROPS_PRODUCT",
    "VROPS_VERSION",
    "VropsCoreGroup",
    "VropsCoreOp",
    "apply_vrops_core_curation",
    "classify_vrops_op",
]

_log = structlog.get_logger(__name__)

#: ``VROPS_PRODUCT`` / ``VROPS_VERSION`` / ``VROPS_IMPL_ID`` note
#: ---------------------------------------------------------------
#:
#: ``VROPS_PRODUCT = "vrops"`` is the value
#: :func:`~meho_backplane.operations._lookup.parse_connector_id`
#: extracts from ``VROPS_CONNECTOR_ID = "vrops-rest-9.0"``
#: (``head.split("-", 1)[0]`` where head is ``"vrops-rest"``).
#:
#: Distinct from :attr:`VcfOperationsConnector.product` (``"vcf-operations"``)
#: — same shape as the SDDC Manager case where
#: ``SddcManagerConnector.product="sddc-manager"`` but rows carry
#: ``product="sddc"``. The connector class's product field drives the
#: v2-registry triple; ``endpoint_descriptor.product`` / ``operation_group.product``
#: column values are derived from the connector_id via
#: :func:`parse_connector_id` at ingest + review time, so the curation
#: helper must reference the parser's derived value here, not the
#: registry's.
VROPS_PRODUCT: Final[str] = "vrops"
VROPS_VERSION: Final[str] = "9.0"
VROPS_IMPL_ID: Final[str] = "vrops-rest"

#: Connector-id slug the G0.6 dispatcher's ``parse_connector_id``
#: round-trips back to the triple above: ``"vrops-rest-9.0"``.
VROPS_CONNECTOR_ID: Final[str] = f"{VROPS_IMPL_ID}-{VROPS_VERSION}"


@dataclass(frozen=True, slots=True)
class VropsCoreGroup:
    """One curated operator-review entry for a vROps operation group.

    ``group_key`` is the slug :func:`classify_vrops_op` emits.
    ``name`` is the operator-readable label ``meho connector review``
    renders. ``when_to_use`` is the agent-facing hint
    :func:`list_operation_groups` returns verbatim; every entry is a
    single complete sentence so the agent's group-selection step has
    unambiguous guidance.
    """

    group_key: str
    name: str
    when_to_use: str


@dataclass(frozen=True, slots=True)
class VropsCoreOp:
    """One curated operator-review entry for a vROps operation.

    ``op_id`` follows the ``METHOD:path`` shape every
    ``source_kind='ingested'`` row uses; the path matches an entry
    in the vROps suite-api OpenAPI spec.

    ``llm_instructions`` is the per-op JSON blob the meta-tools inline
    verbatim when the op surfaces. The shape (``when_to_call`` /
    ``output_shape`` / ``next_step``) mirrors the typed-connector
    convention from :mod:`meho_backplane.connectors.bind9.ops_zone`
    and :mod:`meho_backplane.connectors.nsx.core_ops` — same agent
    reads both surfaces, so the structure stays uniform.
    """

    op_id: str
    group_key: str
    llm_instructions: dict[str, object]


#: Path-prefix → group_key classifier rules for vROps.
#:
#: **Order is load-bearing.** Each rule is checked via
#: ``path.startswith(prefix)``. More-specific prefixes must precede
#: less-specific ones to avoid a shorter prefix consuming a path that
#: belongs to a deeper group:
#:
#: * ``/alertdefinitions`` before ``/alerts`` — ``/alerts`` is a prefix
#:   of ``/alertdefinitions``.
#:
#: Every other curated prefix is disjoint from the rest, so the order
#: between them only documents the operator's mental model.
VROPS_PATH_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("/suite-api/api/versions", "vrops-system"),
    ("/suite-api/api/resources", "vrops-resources"),
    # alertdefinitions must precede alerts — startswith("/suite-api/api/alerts")
    # would otherwise eat the longer path.
    ("/suite-api/api/alertdefinitions", "vrops-alert-definitions"),
    ("/suite-api/api/alerts", "vrops-alerts"),
    ("/suite-api/api/symptoms", "vrops-symptoms"),
    ("/suite-api/api/recommendations", "vrops-recommendations"),
    ("/suite-api/api/supermetrics", "vrops-supermetrics"),
)


def classify_vrops_op(op_id: str) -> str:
    """Return the curated ``group_key`` for a vROps op_id, or ``"none"``.

    ``op_id`` is the ``METHOD:/path`` form ingested rows carry; the
    helper strips the verb and matches the path against
    :data:`VROPS_PATH_RULES` in order.

    Returns ``"none"`` for non-GET methods (vROps' v0.5 read core
    rejects writes outright) and for paths outside the curated
    families (e.g. ``/suite-api/api/credentialkinds``,
    ``/suite-api/api/auth/sources``); those rows are un-curated and
    stay ``is_enabled=False`` after :func:`apply_vrops_core_curation`
    runs.
    """
    try:
        method, path = op_id.split(":", 1)
    except ValueError:
        return "none"
    if method != "GET":
        return "none"
    for prefix, group_key in VROPS_PATH_RULES:
        if path.startswith(prefix):
            return group_key
    return "none"


#: Operator-reviewed ``when_to_use`` hints for the 7 vROps groups
#: the read-only v0.5 core spans. Every hint is one complete sentence
#: the agent reads verbatim — vague hints poison
#: ``search_operations`` ranking, per the ai_engineering pack.
VROPS_CORE_GROUPS: Final[tuple[VropsCoreGroup, ...]] = (
    VropsCoreGroup(
        group_key="vrops-system",
        name="vROps (system / about)",
        when_to_use=(
            "Use this group to read the vROps appliance's own identity — "
            "release name and build number. The probe surface the agent "
            "calls before any heavier inventory read, or when confirming "
            "which vROps instance the target points at."
        ),
    ),
    VropsCoreGroup(
        group_key="vrops-resources",
        name="vROps Resources",
        when_to_use=(
            "Use this group to list or inspect vROps resources — every "
            "monitored object across vCenter, ESXi hosts, VMs, datastores, "
            "and cloud adapters. The primary inventory entry point for "
            "any vROps workflow; filter by resourceKind, adapterKind, or "
            "name to narrow large fleets. Drill into one resource by id "
            "for credential references and full identifiers."
        ),
    ),
    VropsCoreGroup(
        group_key="vrops-alerts",
        name="vROps Alerts",
        when_to_use=(
            "Use this group to list currently firing or recently resolved "
            "alerts. Filter by activeOnly to focus on the open set, or by "
            "alertCriticality for severity-scoped views. Each alert ties "
            "back to an alert definition via alertDefinitionId and to a "
            "resource via resourceId."
        ),
    ),
    VropsCoreGroup(
        group_key="vrops-alert-definitions",
        name="vROps Alert Definitions",
        when_to_use=(
            "Use this group to list the configured alert definitions — "
            "the policy surface the alert list rolls up against. Each "
            "definition names the triggering symptom set, the impact "
            "category, and the attached recommendations. Useful when "
            "answering 'why did this alert fire' or 'which definition "
            "owns this signal'."
        ),
    ),
    VropsCoreGroup(
        group_key="vrops-symptoms",
        name="vROps Symptoms",
        when_to_use=(
            "Use this group to list currently firing symptoms — the "
            "per-condition signals (metric breach, property change, "
            "message event) that roll up into alerts. Useful when "
            "debugging which underlying condition triggered an alert "
            "or when surveying the raw signal layer beneath the alert "
            "definitions."
        ),
    ),
    VropsCoreGroup(
        group_key="vrops-recommendations",
        name="vROps Recommendations",
        when_to_use=(
            "Use this group to list the recommendations vROps surfaces "
            "alongside its alerts and symptoms — operator-facing "
            "remediation hints, often parameterised against the "
            "affected resource. Useful when the agent needs to suggest "
            "next-step actions for an active alert."
        ),
    ),
    VropsCoreGroup(
        group_key="vrops-supermetrics",
        name="vROps Super Metrics",
        when_to_use=(
            "Use this group to list user-defined super metrics — derived "
            "metric formulae built from existing vROps metrics. Useful "
            "when answering 'which formula computes X' or auditing "
            "custom metric definitions across the deployment."
        ),
    ),
)


def _instructions(
    *,
    when_to_call: str,
    output_shape: str,
    next_step: str,
) -> dict[str, object]:
    """Build the per-op ``llm_instructions`` blob with the canonical keys.

    Same three-field shape :mod:`meho_backplane.connectors.nsx.core_ops`
    and :mod:`meho_backplane.connectors.harbor.core_ops` use so an
    agent crossing connector boundaries sees a stable convention.
    """
    return {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
    }


#: The 8 curated read-only vROps core ops. Each entry carries the
#: op_id (``GET:/path`` form), the curated group assignment, and the
#: operator-reviewed ``llm_instructions`` blob.
#:
#: Paths cross-checked against the vROps suite-api docs at
#: https://developer.broadcom.com/xapis/vrealize-operations-manager-api/latest/.
VROPS_CORE_OPS: Final[tuple[VropsCoreOp, ...]] = (
    VropsCoreOp(
        op_id="GET:/suite-api/api/versions/current",
        group_key="vrops-system",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the vROps appliance identity: release name "
                "and build number. Useful as a pre-flight probe before "
                "heavier inventory reads, or to confirm which vROps "
                "instance the target points at. The same endpoint the "
                "connector's fingerprint surface consumes."
            ),
            output_shape=(
                "Object with releaseName (e.g. '9.0.0.1.23456789'), "
                "buildNumber (int), and optionally "
                "humanlyReadableReleaseName when the appliance emits it."
            ),
            next_step=(
                "If the version looks healthy, proceed to "
                "vrops.resource.list for the inventory entry point, or "
                "vrops.alert.list for the operational signal surface."
            ),
        ),
    ),
    VropsCoreOp(
        op_id="GET:/suite-api/api/resources",
        group_key="vrops-resources",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list vROps-monitored resources. Accepts "
                "resourceKind (e.g. 'VirtualMachine', 'HostSystem', "
                "'Datastore'), adapterKind (e.g. 'VMWARE'), and name "
                "query parameters to narrow large fleets; supports "
                "pagination via page + pageSize. A large list is reduced "
                "to a JSONFlux handle with a bounded inline sample plus a "
                "``fetch_more`` envelope; page through with page + pageSize "
                "to read beyond the sample."
            ),
            output_shape=(
                "Object with resourceList[]; each entry carries identifier "
                "(UUID), resourceKey (name + resourceKindKey + "
                "adapterKindKey + resourceIdentifiers[]), creationTime, "
                "resourceStatusStates[] (DATA_RECEIVING / NOT_EXISTING / "
                "etc.), and credentialInstanceId. Also returns pageInfo "
                "for pagination and links for HATEOAS navigation."
            ),
            next_step=(
                "Pick a resource by identifier (UUID) for "
                "vrops.resource.get to read full detail, or cross-reference "
                "a resource's identifier against vrops.alert.list "
                "(filter by resourceId) to find alerts firing on it."
            ),
        ),
    ),
    VropsCoreOp(
        op_id="GET:/suite-api/api/resources/{id}",
        group_key="vrops-resources",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the full detail of one vROps resource by "
                "its identifier (UUID). Returns the same shape as the "
                "list entry plus extended adapterKindKey-specific "
                "identifiers. Requires an id obtained from "
                "vrops.resource.list."
            ),
            output_shape=(
                "Object with identifier (UUID), resourceKey (full "
                "key with all resourceIdentifiers), creationTime, "
                "resourceStatusStates[], credentialInstanceId, "
                "geoLocation (lat/long when set), and dtEnabled flag."
            ),
            next_step=(
                "Cross-reference the resource's identifier with "
                "vrops.alert.list (?resourceId=<uuid>) to find "
                "alerts firing on it, or with vrops.symptom.list to "
                "see the underlying signal layer."
            ),
        ),
    ),
    VropsCoreOp(
        op_id="GET:/suite-api/api/alerts",
        group_key="vrops-alerts",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list currently firing or recently resolved "
                "alerts. Accepts activeOnly (bool, default false), "
                "alertCriticality (CRITICAL / IMMEDIATE / WARNING / "
                "INFORMATION / UNKNOWN), alertStatus (ACTIVE / CANCELED "
                "/ SUSPENDED / UPDATED), and resourceId (UUID) query "
                "parameters. Supports pagination via page + pageSize; "
                "large alert sets return a JSONFlux handle."
            ),
            output_shape=(
                "Object with alerts[]; each entry carries alertId (UUID), "
                "alertDefinitionId, alertDefinitionName, alertLevel "
                "(0..5), alertImpact, resourceId, startTimeUTC, "
                "cancelTimeUTC, status, and controlState. Also returns "
                "pageInfo and links."
            ),
            next_step=(
                "Surface the high-criticality alerts to the operator; "
                "cross-reference alertDefinitionId with "
                "vrops.alertdefinition.list for policy context, "
                "resourceId with vrops.resource.get for affected-object "
                "detail."
            ),
        ),
    ),
    VropsCoreOp(
        op_id="GET:/suite-api/api/alertdefinitions",
        group_key="vrops-alert-definitions",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list configured alert definitions — the "
                "policy surface alerts roll up against. Accepts id "
                "(repeatable), adapterKind, resourceKind, and name "
                "query parameters; supports pagination via page + "
                "pageSize."
            ),
            output_shape=(
                "Object with alertDefinitions[]; each entry carries id, "
                "name, description, adapterKindKey, resourceKindKey, "
                "waitCycles, cancelCycles, type, subType, states[] "
                "(each with severity, base-symptom-set, "
                "recommendation-priority-map), and links to attached "
                "recommendations."
            ),
            next_step=(
                "Pick an alertDefinitionId of interest, then call "
                "vrops.alert.list to see which alerts currently fire "
                "under that definition, or vrops.symptom.list to "
                "inspect the underlying signal layer."
            ),
        ),
    ),
    VropsCoreOp(
        op_id="GET:/suite-api/api/symptoms",
        group_key="vrops-symptoms",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list currently firing symptoms — the per-condition "
                "signals (metric breach, property change, message event) "
                "that roll up into alerts. Accepts id (repeatable), "
                "resourceId, activeOnly, and statusType query parameters; "
                "supports pagination via page + pageSize."
            ),
            output_shape=(
                "Object with symptoms[]; each entry carries id, "
                "symptomDefinitionId, symptomDefinitionName, resourceId, "
                "alertId (when rolled up into an alert), startTimeUTC, "
                "cancelTimeUTC, severity, statusType, and "
                "controlState."
            ),
            next_step=(
                "Cross-reference symptomDefinitionId for policy context, "
                "resourceId with vrops.resource.get for affected-object "
                "detail, or alertId with vrops.alert.list for the "
                "rolled-up alert."
            ),
        ),
    ),
    VropsCoreOp(
        op_id="GET:/suite-api/api/recommendations",
        group_key="vrops-recommendations",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list the recommendations vROps surfaces "
                "alongside its alerts and symptoms — operator-facing "
                "remediation hints. Accepts id (repeatable) and page + "
                "pageSize query parameters."
            ),
            output_shape=(
                "Object with recommendations[]; each entry carries id, "
                "description (the operator-facing remediation text), and "
                "actionId when an automated remediation action is linked."
            ),
            next_step=(
                "Surface the recommendation description to the operator; "
                "if actionId is set, the recommendation is "
                "machine-actionable via the vROps action framework "
                "(out of scope for v0.5 read core)."
            ),
        ),
    ),
    VropsCoreOp(
        op_id="GET:/suite-api/api/supermetrics",
        group_key="vrops-supermetrics",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list user-defined super metrics — derived "
                "metric formulae built from existing vROps metrics. "
                "Accepts id (repeatable) and name query parameters; "
                "supports pagination via page + pageSize."
            ),
            output_shape=(
                "Object with superMetrics[]; each entry carries id, "
                "name, description, formula (the metric expression), "
                "and modificationTime."
            ),
            next_step=(
                "Surface the formula and name to the operator; cross-"
                "reference modificationTime when auditing recent "
                "definition changes."
            ),
        ),
    ),
)


async def apply_vrops_core_curation(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
) -> None:
    """Apply the curated 8-op read core against an ingested vROps connector.

    Drives the substrate so that, after this call returns, exactly
    the 8 ops in :data:`VROPS_CORE_OPS` are dispatchable
    (``is_enabled=True``) and every other ingested op stays
    ``is_enabled=False``. The 7 curated groups land
    ``review_status='enabled'`` so the agent's
    :func:`~meho_backplane.operations.meta_tools.search_operations`
    surfaces the core ops; non-curated groups are left untouched
    (``review_status='staged'`` from the G0.7 ingest default).

    The substrate doesn't expose "enable only ops X, Y, Z under
    group G": :meth:`ReviewService.enable_group`'s cascade flips
    ``is_enabled=True`` on every child op in the group. The helper
    works around this via the audit-log-driven operator-override
    exclusion — the same mechanism
    :func:`~meho_backplane.connectors.nsx.core_ops.apply_nsx_core_curation`
    and :func:`~meho_backplane.connectors.harbor.core_ops.apply_harbor_core_curation`
    established:

    1. :meth:`ReviewService.get_review_payload` loads the current
       state of every curated group and its child ops.
    2. For each child op in a curated group that **isn't** in the
       :data:`VROPS_CORE_OPS` allow-list,
       :meth:`ReviewService.edit_op` with ``is_enabled=False``
       writes the operator-override audit row. The follow-on
       :meth:`enable_group` cascade detects these rows and skips
       them.
    3. :meth:`ReviewService.edit_group` lands the operator-reviewed
       ``name`` + ``when_to_use`` on each curated group.
    4. :meth:`ReviewService.enable_group` flips
       ``review_status='enabled'`` and cascades ``is_enabled=True``
       to the curated child ops (operator-overridden non-core ops
       are skipped).
    5. :meth:`ReviewService.edit_op` lands the curated
       ``llm_instructions`` blob per entry in :data:`VROPS_CORE_OPS`.

    Re-running is safe but not idempotent at the audit layer.
    :meth:`enable_group` short-circuits on groups already in
    ``review_status='enabled'`` (no audit row), but
    :meth:`edit_group` and :meth:`edit_op` always emit one audit
    row per call — even when the incoming value equals the
    persisted one. The intended posture is a one-shot curation step
    after ingest; re-runs produce redundant
    ``meho.connector.edit_*`` audit rows but never corrupt state.

    Raises :class:`~meho_backplane.operations.ingest.ConnectorNotFoundError`
    if no groups exist for ``vrops-rest-9.0`` under *tenant_id* (the
    operator must run ``meho connector ingest`` against the vROps
    suite-api spec before this helper applies).
    """
    payload = await review_service.get_review_payload(
        VROPS_CONNECTOR_ID,
        tenant_id,
    )

    core_op_ids_by_group: dict[str, set[str]] = {}
    for op in VROPS_CORE_OPS:
        core_op_ids_by_group.setdefault(op.group_key, set()).add(op.op_id)

    for group_payload in payload.groups:
        allow_list = core_op_ids_by_group.get(group_payload.group_key)
        if allow_list is None:
            continue
        for review_op in group_payload.ops:
            if review_op.op_id in allow_list:
                continue
            await review_service.edit_op(
                VROPS_CONNECTOR_ID,
                review_op.op_id,
                tenant_id=tenant_id,
                is_enabled=False,
            )
            _log.info(
                "vrops_non_core_op_disabled",
                connector_id=VROPS_CONNECTOR_ID,
                op_id=review_op.op_id,
                group_key=group_payload.group_key,
            )

    for group in VROPS_CORE_GROUPS:
        await review_service.edit_group(
            VROPS_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
            name=group.name,
            when_to_use=group.when_to_use,
        )
        await review_service.enable_group(
            VROPS_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
        )
        _log.info(
            "vrops_core_group_enabled",
            connector_id=VROPS_CONNECTOR_ID,
            group_key=group.group_key,
        )

    for op in VROPS_CORE_OPS:
        await review_service.edit_op(
            VROPS_CONNECTOR_ID,
            op.op_id,
            tenant_id=tenant_id,
            llm_instructions=op.llm_instructions,
        )
        _log.info(
            "vrops_core_op_curated",
            connector_id=VROPS_CONNECTOR_ID,
            op_id=op.op_id,
        )
