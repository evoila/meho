# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCF Automation 9.x read-only v0.5 core -- curated operator-enabled subset.

This module names the **11 read-only VCF Automation operations** the
G3.6 dual-plane v0.5 ship enables out of the much larger
``vcf-automation-9.0/cloudapi.yaml`` + ``vcf-automation-9.0/iaas.yaml``
corpus that the G0.7 spec-ingestion pipeline lands under
``connector_id="vcfa-rest-9.0"``. The curation is two-layered:

* :data:`VCFA_CORE_GROUPS` -- the operator-reviewed ``when_to_use``
  hint per LLM-grouping pass output group. Every entry names its
  **plane** (provider or tenant) up front so the agent's group
  selection step routes correctly across the dual-plane surface;
  generic hints would let a tenant question collapse onto a
  provider-only group and vice versa.
* :data:`VCFA_CORE_OPS` -- the 11 ``EndpointDescriptor.op_id`` strings
  (6 provider + 5 tenant) that flip to ``is_enabled=True`` at
  operator-review time, paired with the per-op ``llm_instructions``
  blob the agent inlines into the reasoning context when it sees
  the op in
  :func:`~meho_backplane.operations.meta_tools.search_operations`
  hits. Every other op under the same connector triple stays
  ``is_enabled=False`` (the G0.7 ingestion default for
  ``source_kind='ingested'`` rows).

The literal data tables (VCFA_CORE_GROUPS + VCFA_CORE_OPS +
VCFA_PATH_RULES + classify_vcfa_op) live in :mod:`._core_data`. This
module re-exports them and adds the import-time invariant checks +
the operator-review-time :func:`apply_vcfa_core_curation` helper.

Per Initiative #369 and CLAUDE.md postulates 1-2, VCF Automation is
**generic-ingested** (Layer-2) and **dual-plane**: the provider plane
(``/cloudapi/*`` and the classic ``/api/*`` family) and the tenant
plane (``/iaas/api/*``) are two OpenAPI specs ingested under one
``connector_id`` with ``spec_source`` tags so the dispatcher routes
each op to the correct auth plane (see
:func:`~meho_backplane.connectors.vcf_automation._routing.plane_for_path`).
The vSphere two-spec merge (``vcenter.yaml`` + ``vi-json.yaml``,
ingested via #408) is the precedent.

The 11 ops:

Provider plane (6 ops, paths under ``/cloudapi/1.0.0/*``):

* ``GET:/cloudapi/1.0.0/site`` -- ``vcfa.provider.about``
* ``GET:/cloudapi/1.0.0/orgs`` -- ``vcfa.provider.org.list``
* ``GET:/cloudapi/1.0.0/orgs/{id}`` -- ``vcfa.provider.org.get``
* ``GET:/cloudapi/1.0.0/regions`` -- ``vcfa.provider.vdc.list``
  (VCFA 9 evolution of the vCD provider VDC concept)
* ``GET:/cloudapi/1.0.0/regions/{id}`` -- ``vcfa.provider.vdc.get``
* ``GET:/cloudapi/1.0.0/users`` -- ``vcfa.provider.user.list``

Tenant plane (5 ops, paths under ``/iaas/api/*``):

* ``GET:/iaas/api/about`` -- ``vcfa.tenant.about``
* ``GET:/iaas/api/projects`` -- ``vcfa.tenant.project.list``
* ``GET:/iaas/api/deployments`` -- ``vcfa.tenant.deployment.list``
  (largest tenant-side payload; trips the dispatcher's JSONFlux
  seam on large tenants)
* ``GET:/iaas/api/deployments/{id}`` -- ``vcfa.tenant.deployment.get``
* ``GET:/iaas/api/blueprints`` -- ``vcfa.tenant.blueprint.list``

Curation application
--------------------

:func:`apply_vcfa_core_curation` is the operator-review-time substrate
call that makes exactly the 11 curated ops dispatchable. Mirrors
:func:`apply_nsx_core_curation` /
:func:`apply_harbor_core_curation` verbatim: per-group ``edit_op``
override pass to mark non-core ops disabled, then ``edit_group`` +
``enable_group`` per curated group, then ``edit_op(llm_instructions=...)``
per curated op. Re-running is safe but emits redundant audit rows
(same posture every prior core-curation helper established).

Plane awareness is load-bearing in the ``when_to_use`` strings: without
it the agent's grouping step risks calling a tenant op through the
provider auth path (or vice versa), which surfaces as HTTP 401 from
VCFA -- both planes share the same ``Bearer <token>`` header shape
but reject the wrong plane's token. The connector's :meth:`auth_headers`
selects the plane by ``plane_for_path(path)``, so a misrouted call
fails fast at dispatch, but the agent UX is cleaner if the grouping
step picks the right plane first. Every :data:`VCFA_CORE_GROUPS`
``when_to_use`` names its plane explicitly to keep that step honest.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from meho_backplane.connectors.vcf_automation._core_data import (
    VCFA_CONNECTOR_ID,
    VCFA_CORE_GROUPS,
    VCFA_CORE_OPS,
    VCFA_IMPL_ID,
    VCFA_PATH_RULES,
    VCFA_PRODUCT,
    VCFA_VERSION,
    VcfaCoreGroup,
    VcfaCoreOp,
    classify_vcfa_op,
)
from meho_backplane.connectors.vcf_automation._routing import plane_for_path
from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "VCFA_CONNECTOR_ID",
    "VCFA_CORE_GROUPS",
    "VCFA_CORE_OPS",
    "VCFA_IMPL_ID",
    "VCFA_PATH_RULES",
    "VCFA_PRODUCT",
    "VCFA_VERSION",
    "VcfaCoreGroup",
    "VcfaCoreOp",
    "apply_vcfa_core_curation",
    "classify_vcfa_op",
]

_log = structlog.get_logger(__name__)


def _validate_module_invariants() -> None:
    """Assert internal plane / classifier invariants at import time.

    Three load-bearing invariants tied to the dual-plane shape:

    1. Every :data:`VCFA_CORE_OPS` entry's ``plane`` field matches
       :func:`~meho_backplane.connectors.vcf_automation._routing.plane_for_path`
       applied to the op's path. A drift here would mean a tenant op
       carries a provider group hint (or vice versa) and the agent's
       grouping step would misroute the call -- surfacing as HTTP 401
       at dispatch time. Failing fast at import is cheaper.
    2. Every :data:`VCFA_CORE_OPS` entry's ``group_key`` resolves
       through :func:`classify_vcfa_op` to the same group_key (no
       silent reassignment).
    3. Every :data:`VCFA_CORE_OPS` entry's ``group_key`` matches an
       entry in :data:`VCFA_CORE_GROUPS`, and the entry's plane
       matches its group's plane.
    """
    group_keys = {group.group_key for group in VCFA_CORE_GROUPS}
    group_planes = {group.group_key: group.plane for group in VCFA_CORE_GROUPS}
    for op in VCFA_CORE_OPS:
        _, path = op.op_id.split(":", 1)
        derived_plane = plane_for_path(path)
        if derived_plane != op.plane:
            raise AssertionError(
                f"VCFA_CORE_OPS entry {op.op_id!r} declares plane={op.plane!r} "
                f"but plane_for_path() returns {derived_plane!r}"
            )
        classified = classify_vcfa_op(op.op_id)
        if classified != op.group_key:
            raise AssertionError(
                f"VCFA_CORE_OPS entry {op.op_id!r} declares "
                f"group_key={op.group_key!r} but classify_vcfa_op() returns "
                f"{classified!r}"
            )
        if op.group_key not in group_keys:
            raise AssertionError(
                f"VCFA_CORE_OPS entry {op.op_id!r} references unknown "
                f"group_key={op.group_key!r} (not in VCFA_CORE_GROUPS)"
            )
        if group_planes[op.group_key] != op.plane:
            raise AssertionError(
                f"VCFA_CORE_OPS entry {op.op_id!r} declares plane={op.plane!r} "
                f"but its group {op.group_key!r} declares "
                f"plane={group_planes[op.group_key]!r}"
            )


_validate_module_invariants()


def _core_op_ids_by_group() -> dict[str, set[str]]:
    """Index :data:`VCFA_CORE_OPS` by ``group_key`` for the curation pass."""
    index: dict[str, set[str]] = {}
    for op in VCFA_CORE_OPS:
        index.setdefault(op.group_key, set()).add(op.op_id)
    return index


async def _disable_non_core_ops(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
) -> None:
    """Write the operator-override audit rows for every non-core op in a curated group.

    The follow-on :meth:`ReviewService.enable_group` cascade reads the
    audit log via
    :func:`~meho_backplane.operations.ingest._internals.operator_disabled_op_ids`
    and skips any op with a prior ``is_enabled=False`` override row.
    The override must be written even when the op already appears
    ``is_enabled=False`` -- a freshly-ingested op has no audit history,
    so the cascade would re-enable it.

    Non-curated groups (whose ``group_key`` isn't in
    :data:`VCFA_CORE_OPS`) are left entirely alone; their
    ``review_status`` stays at whatever the ingest pass set it to.
    """
    payload = await review_service.get_review_payload(VCFA_CONNECTOR_ID, tenant_id)
    core_op_ids = _core_op_ids_by_group()
    for group_payload in payload.groups:
        allow_list = core_op_ids.get(group_payload.group_key)
        if allow_list is None:
            continue
        for review_op in group_payload.ops:
            if review_op.op_id in allow_list:
                continue
            await review_service.edit_op(
                VCFA_CONNECTOR_ID,
                review_op.op_id,
                tenant_id=tenant_id,
                is_enabled=False,
            )
            _log.info(
                "vcfa_non_core_op_disabled",
                connector_id=VCFA_CONNECTOR_ID,
                op_id=review_op.op_id,
                group_key=group_payload.group_key,
            )


async def _enable_curated_groups(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
) -> None:
    """Land curated ``name`` + ``when_to_use`` and flip every curated group to enabled.

    ``ReviewService.edit_group`` lands the operator-reviewed text;
    ``ReviewService.enable_group`` then flips
    ``review_status='enabled'`` and cascades ``is_enabled=True`` to
    the curated child ops. The cascade respects the operator-override
    rows written by :func:`_disable_non_core_ops`, so non-core ops in
    a curated group stay disabled.
    """
    for group in VCFA_CORE_GROUPS:
        await review_service.edit_group(
            VCFA_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
            name=group.name,
            when_to_use=group.when_to_use,
        )
        await review_service.enable_group(
            VCFA_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
        )
        _log.info(
            "vcfa_core_group_enabled",
            connector_id=VCFA_CONNECTOR_ID,
            group_key=group.group_key,
            plane=group.plane,
        )


async def _land_op_llm_instructions(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
) -> None:
    """Write the operator-reviewed ``llm_instructions`` blob onto each curated op."""
    for op in VCFA_CORE_OPS:
        await review_service.edit_op(
            VCFA_CONNECTOR_ID,
            op.op_id,
            tenant_id=tenant_id,
            llm_instructions=op.llm_instructions,
        )
        _log.info(
            "vcfa_core_op_curated",
            connector_id=VCFA_CONNECTOR_ID,
            op_id=op.op_id,
            plane=op.plane,
        )


async def apply_vcfa_core_curation(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
) -> None:
    """Apply the curated 11-op read core against an ingested VCFA connector.

    Drives the substrate so that, after this call returns, exactly
    the 11 ops in :data:`VCFA_CORE_OPS` are dispatchable
    (``is_enabled=True``) and every other ingested op stays
    ``is_enabled=False``. The 8 curated groups land
    ``review_status='enabled'`` so the agent's
    :func:`~meho_backplane.operations.meta_tools.search_operations`
    surfaces the core ops; non-curated groups are left untouched
    (``review_status='staged'`` from the G0.7 ingest default).

    The substrate doesn't expose "enable only ops X, Y, Z under
    group G": :meth:`ReviewService.enable_group`'s cascade flips
    ``is_enabled=True`` on every child op in the group. The helper
    works around this via the audit-log-driven operator-override
    exclusion -- same mechanism the NSX and Harbor precedents
    established. Implementation is split into three sequential
    phases:

    1. :func:`_disable_non_core_ops` writes
       ``edit_op(is_enabled=False)`` for every non-core op in a
       curated group.
    2. :func:`_enable_curated_groups` lands ``edit_group(name, when_to_use)``
       and then ``enable_group(...)`` per curated group; the cascade
       honours the override rows from step 1.
    3. :func:`_land_op_llm_instructions` writes
       ``edit_op(llm_instructions=...)`` per curated op.

    Re-running is safe but not idempotent at the audit layer: each
    ``edit_group`` / ``edit_op`` writes a fresh audit row even on
    no-op values. ``enable_group`` short-circuits on a group already
    in ``review_status='enabled'`` (no audit row). The intended
    posture is a one-shot curation step after ingest; re-runs during
    a rollout produce redundant ``meho.connector.edit_*`` audit rows
    but never corrupt state.

    Raises :class:`~meho_backplane.operations.ingest.ConnectorNotFoundError`
    if no groups exist for ``vcfa-rest-9.0`` under *tenant_id* (the
    operator must run ``meho connector ingest`` against both VCFA
    specs before this helper applies). The operator runbook at
    ``docs/cross-repo/g36-vcfa-canary.md`` documents the end-to-end
    procedure.
    """
    await _disable_non_core_ops(review_service, tenant_id=tenant_id)
    await _enable_curated_groups(review_service, tenant_id=tenant_id)
    await _land_op_llm_instructions(review_service, tenant_id=tenant_id)
