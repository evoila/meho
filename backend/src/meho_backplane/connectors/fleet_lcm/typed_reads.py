# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) read implementations for :class:`FleetLcmConnector`.

The modern VCF 9 Fleet LCM Service ``/v1/*`` read core (#3047, initiative
`evoila/meho#3033 <https://github.com/evoila/meho/issues/3033>`_) is
registered as typed ops (``source_kind="typed"``) so it dispatches on a
fresh boot with **zero catalog state** — the read core is live the moment
the connector loads, no operator ``meho connector ingest`` +
``enable_reads`` step. This is what restores 9.0 fleet dispatch: a VCF
Fleet 9.0 target resolves to ``fleet-lcm`` by the resolver's
most-specific-version tie-break (``>=9.0,<10.0`` vs the legacy
``fleet-rest`` ``>=8.0,<10.0``), and these ops give it a dispatchable
modern read surface.

Each function here is the body a thin bound-method shim on
:class:`~meho_backplane.connectors.fleet_lcm.connector.FleetLcmConnector`
delegates to; the metadata + registrar live in
:mod:`meho_backplane.connectors.fleet_lcm.typed_ops`, and the per-op
``llm_instructions`` blobs in
:mod:`meho_backplane.connectors.fleet_lcm._llm_instructions` (split out to
keep this module under the file-size budget — the same three-key shape the
harbor #2856 read core co-locates). This mirrors the Harbor (#2856) and NSX
(#2302) conversions under Task #2358: typed ops own the curated read core,
generic review (``ReviewService.enable_reads`` over an operator ingest of
the full 51-op spec) owns the wider write / breadth surface as a follow-up.

Every read is issued directly on the connector's own authenticated client
via :meth:`HttpConnector._get_json`, which applies
:meth:`FleetLcmConnector.auth_headers` per request — Bearer primary
(``Authorization: Bearer <token>`` when the loaded credentials carry a
``token``), HTTP-Basic fallback otherwise. A raw ``401`` propagates as
:class:`httpx.HTTPStatusError` to the dispatcher's credential-recovery arm,
which evicts the cached credentials via
:meth:`FleetLcmConnector.invalidate_credentials` (#2396) and re-dispatches
once.

The 13-read set (each ``GET:/path`` reconciled against the pinned
Apache-2.0 ``fleet-lcm-openapi.yaml`` on the operator's shelf by
:mod:`tests.test_connectors_fleet_lcm_spec_reconcile`; the path templates
live in :mod:`~meho_backplane.connectors.fleet_lcm._paths`):

* ``fleet-lcm.health`` — ``GET /v1/health`` (service liveness; the same
  surface :meth:`FleetLcmConnector.probe` reads, exposed as an
  operator-callable typed op).
* ``fleet-lcm.system.info`` — ``GET /v1/system`` (fleet system summary:
  pending upgrade + available bundles).
* ``fleet-lcm.config.info`` — ``GET /v1/config`` (Fleet LCM service
  configuration).
* ``fleet-lcm.sddc-lcm.list`` / ``.info`` — ``GET /v1/sddc-lcms`` and
  ``/v1/sddc-lcms/{sddcLcmId}`` (registered SDDC lifecycle managers).
* ``fleet-lcm.component.list`` / ``.info`` / ``.status`` —
  ``GET /v1/components``, ``/v1/components/{componentId}`` and
  ``/v1/components/{componentId}/status`` (managed component inventory +
  per-component status).
* ``fleet-lcm.task.list`` / ``.info`` — ``GET /v1/tasks`` and
  ``/v1/tasks/{taskId}`` (async operation tracking).
* ``fleet-lcm.upgrade-plan.list`` / ``.info`` — ``GET /v1/upgrade-plans``
  and ``/v1/upgrade-plans/{planId}`` (planned upgrades).
* ``fleet-lcm.release-version.list`` — ``GET /v1/release-versions``
  (available target releases).

**Pagination note.** ``sddc-lcms`` / ``tasks`` / ``upgrade-plans`` /
``release-versions`` are server-paged: an un-parameterised call returns the
first page under an ``sddcLcms`` / ``elements`` array plus a
``pageMetadata`` object carrying ``totalElements`` / ``totalPages`` — the
agent reads that to know whether more exist (so this is self-describing,
not silent truncation). Forwarding server-side ``page`` / ``size`` / filter
query params is a deferred enrichment; today the agent narrows large
results through the dispatcher's JSONFlux handle (``result_query``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.fleet_lcm._paths import (
    _COMPONENT_PATH,
    _COMPONENT_STATUS_PATH,
    _COMPONENTS_PATH,
    _CONFIG_PATH,
    _HEALTH_PATH,
    _RELEASE_VERSIONS_PATH,
    _SDDC_LCM_PATH,
    _SDDC_LCMS_PATH,
    _SYSTEM_PATH,
    _TASK_PATH,
    _TASKS_PATH,
    _UPGRADE_PLAN_PATH,
    _UPGRADE_PLANS_PATH,
    encode_segment,
    fill_path,
)
from meho_backplane.connectors.fleet_lcm.session import FleetLcmTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.fleet_lcm.connector import FleetLcmConnector

__all__ = [
    "fleet_lcm_component_info_impl",
    "fleet_lcm_component_list_impl",
    "fleet_lcm_component_status_impl",
    "fleet_lcm_config_info_impl",
    "fleet_lcm_health_impl",
    "fleet_lcm_release_version_list_impl",
    "fleet_lcm_sddc_lcm_info_impl",
    "fleet_lcm_sddc_lcm_list_impl",
    "fleet_lcm_system_info_impl",
    "fleet_lcm_task_info_impl",
    "fleet_lcm_task_list_impl",
    "fleet_lcm_upgrade_plan_info_impl",
    "fleet_lcm_upgrade_plan_list_impl",
]


# ---------------------------------------------------------------------------
# System / service surface
# ---------------------------------------------------------------------------


async def fleet_lcm_health_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.health`` — ``GET /v1/health``.

    Returns the Fleet LCM Service liveness object (``{"up": bool}``). The
    same reachability surface :meth:`FleetLcmConnector.probe` /
    :meth:`~FleetLcmConnector.fingerprint` read, exposed as an
    operator-callable typed op. The spec marks the endpoint
    ``security: []`` (unauthenticated), but the connector sends its auth
    headers anyway so probe and dispatch stay on one auth path.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _HEALTH_PATH, operator=operator)


async def fleet_lcm_system_info_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.system.info`` — ``GET /v1/system``.

    Returns the fleet system summary: any in-flight system ``upgrade``
    (``taskId`` + ``status``) and the ``bundles[]`` available to the fleet
    (each with component type, version, and required/optional status).
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _SYSTEM_PATH, operator=operator)


async def fleet_lcm_config_info_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.config.info`` — ``GET /v1/config``.

    Returns the Fleet LCM service configuration: ``deploymentFlavor`` (e.g.
    ``VCF``), the management ``vspCluster`` it is bound to, the config
    ``id`` / ``version``, and an overall ``status`` (``HEALTHY`` /
    ``OFFLINE``).
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _CONFIG_PATH, operator=operator)


# ---------------------------------------------------------------------------
# SDDC LCM inventory
# ---------------------------------------------------------------------------


async def fleet_lcm_sddc_lcm_list_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.sddc-lcm.list`` — ``GET /v1/sddc-lcms``.

    Lists the SDDC lifecycle managers the fleet manages. Returns a paged
    envelope: ``{"pageMetadata": {...}, "sddcLcms": [SddcLcm, ...]}``. Each
    entry carries ``fqdn``, ``sddcGroupFqdn``, ``isPrimary``, and its
    ``vspClusters[]``. ``pageMetadata`` (``totalElements`` / ``totalPages``)
    tells you whether more pages exist.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _SDDC_LCMS_PATH, operator=operator)


async def fleet_lcm_sddc_lcm_info_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.sddc-lcm.info`` — ``GET /v1/sddc-lcms/{sddcLcmId}``.

    Reads one SDDC lifecycle manager by id. Requires ``sddcLcmId`` (a UUID
    from ``fleet-lcm.sddc-lcm.list``). Returns the full ``SddcLcm`` object.
    """
    path = fill_path(_SDDC_LCM_PATH, {"sddcLcmId": encode_segment(params["sddcLcmId"])})
    return await connector._get_json(target, path, operator=operator)


# ---------------------------------------------------------------------------
# Managed components
# ---------------------------------------------------------------------------


async def fleet_lcm_component_list_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.component.list`` — ``GET /v1/components``.

    Lists the components the fleet manages ("what's deployed"). Returns
    ``{"components": [Component, ...]}``; each entry carries
    ``componentType``, ``fqdn``, ``version``, ``sddcLcmId``,
    ``deploymentType``, ``nodes[]``, and its ``vspCluster``.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _COMPONENTS_PATH, operator=operator)


async def fleet_lcm_component_info_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.component.info`` — ``GET /v1/components/{componentId}``.

    Reads one managed component by id. Requires ``componentId`` (a UUID
    from ``fleet-lcm.component.list``). Returns the full ``Component``
    object.
    """
    path = fill_path(_COMPONENT_PATH, {"componentId": encode_segment(params["componentId"])})
    return await connector._get_json(target, path, operator=operator)


async def fleet_lcm_component_status_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.component.status`` — ``GET /v1/components/{componentId}/status``.

    Reads the runtime status of one managed component. Requires
    ``componentId``. Returns ``{"id": <uuid>, "status": <Unknown |
    NotRunning | Running>}``.
    """
    path = fill_path(
        _COMPONENT_STATUS_PATH,
        {"componentId": encode_segment(params["componentId"])},
    )
    return await connector._get_json(target, path, operator=operator)


# ---------------------------------------------------------------------------
# Async tasks
# ---------------------------------------------------------------------------


async def fleet_lcm_task_list_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.task.list`` — ``GET /v1/tasks``.

    Lists the fleet's async operation tasks. Returns a paged envelope
    ``{"elements": [TaskSummary, ...], "pageMetadata": {...}}``; each entry
    carries ``resourceId``, ``type``, ``createTime`` / ``updateTime``,
    ``cancellable`` / ``retriable``, ``parentTaskId``, and a
    ``description`` with a ``localizedMessage``.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _TASKS_PATH, operator=operator)


async def fleet_lcm_task_info_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.task.info`` — ``GET /v1/tasks/{taskId}``.

    Reads one async task by id. Requires ``taskId`` (a UUID from
    ``fleet-lcm.task.list``). Returns the full ``Task`` object, including
    its current status and any sub-task detail.
    """
    path = fill_path(_TASK_PATH, {"taskId": encode_segment(params["taskId"])})
    return await connector._get_json(target, path, operator=operator)


# ---------------------------------------------------------------------------
# Lifecycle — upgrade plans + release versions
# ---------------------------------------------------------------------------


async def fleet_lcm_upgrade_plan_list_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.upgrade-plan.list`` — ``GET /v1/upgrade-plans``.

    Lists the fleet's upgrade plans. Returns a paged envelope
    ``{"elements": [UpgradePlanSummary, ...], "pageMetadata": {...}}``; each
    entry carries ``id``, ``createTime`` / ``updateTime``, and a ``spec``
    with the ``componentsFilter`` and ``desiredSoftware``.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _UPGRADE_PLANS_PATH, operator=operator)


async def fleet_lcm_upgrade_plan_info_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.upgrade-plan.info`` — ``GET /v1/upgrade-plans/{planId}``.

    Reads one upgrade plan by id. Requires ``planId`` (a UUID from
    ``fleet-lcm.upgrade-plan.list``). Returns the full ``UpgradePlan``
    object.
    """
    path = fill_path(_UPGRADE_PLAN_PATH, {"planId": encode_segment(params["planId"])})
    return await connector._get_json(target, path, operator=operator)


async def fleet_lcm_release_version_list_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.release-version.list`` — ``GET /v1/release-versions``.

    Lists the release versions the fleet can target. Returns a paged
    envelope ``{"elements": [ReleaseVersion, ...], "pageMetadata": {...}}``;
    each entry carries a ``version`` and the per-``components[]`` public
    names and available versions.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _RELEASE_VERSIONS_PATH, operator=operator)
