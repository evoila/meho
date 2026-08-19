# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) read implementations for :class:`VcloudDirectorConnector`.

The migration-inventory read core (#3057, initiative
`evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_) is registered
as typed ops (``source_kind="typed"``) so it dispatches on a fresh boot with
**zero catalog state** — no operator ``meho connector ingest`` + ``enable_reads``
step. Each function here is the body a thin bound-method shim on
:class:`~meho_backplane.connectors.vcd.connector.VcloudDirectorConnector`
delegates to; the metadata + registrar live in
:mod:`meho_backplane.connectors.vcd.typed_ops`, the per-op ``llm_instructions``
in :mod:`meho_backplane.connectors.vcd._llm_instructions`. This mirrors the
``fleet-lcm`` (#3047) and Harbor (#2856) read cores under the Task #2358
convention: typed ops own the curated read core.

Every read is issued via :meth:`VcloudDirectorConnector._vcd_get`, which applies
the provider Bearer header (minting the session on first use) plus the
per-surface ``Accept`` derived from the path. The provider/System session sees
the whole estate, so the classic-query reads use the ``admin*`` query types.

The 7-read set (each path pinned by :mod:`tests.test_connectors_vcd_spec_reconcile`):

* ``vcd.org.list`` — ``GET /cloudapi/1.0.0/orgs`` (modern cloudapi org collection).
* ``vcd.vdc.list`` / ``.vapp.list`` / ``.vm.list`` / ``.catalog.list`` — the
  classic query service ``GET /api/query`` with ``type=adminOrgVdc`` /
  ``adminVApp`` / ``adminVM`` / ``adminCatalog`` and ``format=records``.
* ``vcd.edge-gateway.list`` — ``GET /api/query?type=edgeGateway``.
* ``vcd.task.list`` — ``GET /api/query?type=adminTask``.

**Pagination note.** The cloudapi collection returns ``{resultTotal, pageCount,
page, pageSize, values: []}``; the query service returns ``{...QueryResultRecords,
record: [], total, page, pageSize}`` — both self-describe whether more pages
exist (so this is not silent truncation). Forwarding server-side ``page`` /
``pageSize`` is a deferred enrichment; today the agent narrows a large result
through the dispatcher's JSONFlux handle (``result_query``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vcd._paths import (
    _ORGS_PATH,
    _QT_ADMIN_CATALOG,
    _QT_ADMIN_ORG_VDC,
    _QT_ADMIN_TASK,
    _QT_ADMIN_VAPP,
    _QT_ADMIN_VM,
    _QT_EDGE_GATEWAY,
    _QUERY_PATH,
    QUERY_RECORDS_FORMAT,
)
from meho_backplane.connectors.vcd.session import VcloudDirectorTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.vcd.connector import VcloudDirectorConnector

__all__ = [
    "vcd_catalog_list_impl",
    "vcd_edge_gateway_list_impl",
    "vcd_org_list_impl",
    "vcd_task_list_impl",
    "vcd_vapp_list_impl",
    "vcd_vdc_list_impl",
    "vcd_vm_list_impl",
]


def _query_params(query_type: str) -> dict[str, str]:
    """Build the query-service params for *query_type* (``type`` + ``format=records``)."""
    return {"type": query_type, "format": QUERY_RECORDS_FORMAT}


async def vcd_org_list_impl(
    connector: VcloudDirectorConnector,
    operator: Operator,
    target: VcloudDirectorTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcd.org.list`` — ``GET /cloudapi/1.0.0/orgs``.

    Lists the organizations in the vCD instance (a provider session sees all).
    Returns the cloudapi paged envelope ``{resultTotal, pageCount, page,
    pageSize, values: [Org, ...]}``; each ``Org`` carries ``id`` (a
    ``urn:vcloud:org:`` URN), ``name``, ``displayName``, and ``isEnabled``.
    """
    del params  # schema declares the param object empty
    return await connector._vcd_get(target, _ORGS_PATH, operator=operator)


async def vcd_vdc_list_impl(
    connector: VcloudDirectorConnector,
    operator: Operator,
    target: VcloudDirectorTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcd.vdc.list`` — ``GET /api/query?type=adminOrgVdc&format=records``.

    Lists the organization VDCs (the per-org compute/storage allocations) across
    every org. Returns the query-service record envelope; each
    ``AdminOrgVdcRecord`` carries ``name``, ``orgName``, ``providerVdcName``,
    ``numberOfVApps`` / ``numberOfVMs``, and allocation figures.
    """
    del params  # schema declares the param object empty
    return await connector._vcd_get(
        target, _QUERY_PATH, operator=operator, params=_query_params(_QT_ADMIN_ORG_VDC)
    )


async def vcd_vapp_list_impl(
    connector: VcloudDirectorConnector,
    operator: Operator,
    target: VcloudDirectorTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcd.vapp.list`` — ``GET /api/query?type=adminVApp&format=records``.

    Lists the vApps (the deployable multi-VM units) across every org. Returns the
    query-service record envelope; each ``AdminVAppRecord`` carries ``name``,
    ``orgName``, ``vdcName``, ``status``, ``numberOfVMs``, and ``isDeployed``.
    """
    del params  # schema declares the param object empty
    return await connector._vcd_get(
        target, _QUERY_PATH, operator=operator, params=_query_params(_QT_ADMIN_VAPP)
    )


async def vcd_vm_list_impl(
    connector: VcloudDirectorConnector,
    operator: Operator,
    target: VcloudDirectorTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcd.vm.list`` — ``GET /api/query?type=adminVM&format=records``.

    Lists the VMs across every org — the core migration-workload inventory.
    Returns the query-service record envelope; each ``AdminVMRecord`` carries
    ``name``, ``containerName`` (the vApp), ``vdc``, ``org``, ``status``,
    ``numberOfCpus``, ``memoryMB``, and ``guestOs``. A large inventory is reduced
    to a JSONFlux handle.
    """
    del params  # schema declares the param object empty
    return await connector._vcd_get(
        target, _QUERY_PATH, operator=operator, params=_query_params(_QT_ADMIN_VM)
    )


async def vcd_catalog_list_impl(
    connector: VcloudDirectorConnector,
    operator: Operator,
    target: VcloudDirectorTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcd.catalog.list`` — ``GET /api/query?type=adminCatalog&format=records``.

    Lists the catalogs (vApp-template + media libraries) across every org.
    Returns the query-service record envelope; each ``AdminCatalogRecord``
    carries ``name``, ``orgName``, ``isShared`` / ``isPublished``, and
    ``numberOfVAppTemplates`` / ``numberOfMedia``.
    """
    del params  # schema declares the param object empty
    return await connector._vcd_get(
        target, _QUERY_PATH, operator=operator, params=_query_params(_QT_ADMIN_CATALOG)
    )


async def vcd_edge_gateway_list_impl(
    connector: VcloudDirectorConnector,
    operator: Operator,
    target: VcloudDirectorTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcd.edge-gateway.list`` — ``GET /api/query?type=edgeGateway&format=records``.

    Lists the edge gateways (the org-VDC network egress + services boundary).
    Returns the query-service record envelope; each ``EdgeGatewayRecord`` carries
    ``name``, ``vdc``, ``orgName``, ``numberOfExtNetworks``, and
    ``numberOfOrgNetworks`` — the networking surface a migration must re-home.
    """
    del params  # schema declares the param object empty
    return await connector._vcd_get(
        target, _QUERY_PATH, operator=operator, params=_query_params(_QT_EDGE_GATEWAY)
    )


async def vcd_task_list_impl(
    connector: VcloudDirectorConnector,
    operator: Operator,
    target: VcloudDirectorTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcd.task.list`` — ``GET /api/query?type=adminTask&format=records``.

    Lists recent asynchronous tasks across the instance — 'what is vCD doing /
    just did'. Uses the system-admin ``adminTask`` type (not the org-scoped
    ``task``) so a provider session sees tasks across every org — the whole-estate
    view the migration-quiescence check needs. Returns the query-service record
    envelope; each ``AdminTaskRecord`` carries ``name``, ``status``,
    ``objectName``, ``orgName``, ``startDate`` / ``endDate``, and ``ownerName``.
    A large list is reduced to a JSONFlux handle.
    """
    del params  # schema declares the param object empty
    return await connector._vcd_get(
        target, _QUERY_PATH, operator=operator, params=_query_params(_QT_ADMIN_TASK)
    )
