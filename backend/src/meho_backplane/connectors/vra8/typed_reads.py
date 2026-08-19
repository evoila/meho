# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) read implementations for :class:`Vra8Connector`.

The vRA 8 migration-inventory read core (#3058, initiative
`evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_) is registered as
typed ops (``source_kind="typed"``) so it dispatches on a fresh boot with **zero
catalog state** — no operator ``meho connector ingest`` + ``enable_reads`` step.
Each function here is the body a thin bound-method shim on
:class:`~meho_backplane.connectors.vra8.connector.Vra8Connector` delegates to; the
metadata + registrar live in :mod:`meho_backplane.connectors.vra8.typed_ops`, the
per-op ``llm_instructions`` in
:mod:`meho_backplane.connectors.vra8._llm_instructions`. This mirrors the ``vcd``
(#3057) and ``fleet-lcm`` (#3047) read cores under the Task #2358 convention: typed
ops own the curated read core.

Every read is issued via :meth:`Vra8Connector._vra_get`, which applies the bearer
header (minting the two-step session on first use) and ``Accept:
application/json``. The one bearer authenticates every vRA 8 service, so the reads
span ``/iaas`` (projects, about), ``/deployment`` (deployments), ``/blueprint``
(blueprints), and ``/catalog`` (catalog items) uniformly.

The 6-read set (each path pinned by :mod:`tests.test_connectors_vra8_spec_reconcile`):

* ``vcfa.vra8.project.list`` — ``GET /iaas/api/projects``.
* ``vcfa.vra8.deployment.list`` — ``GET /deployment/api/deployments``.
* ``vcfa.vra8.deployment.get`` — ``GET /deployment/api/deployments/{deployment_id}``.
* ``vcfa.vra8.blueprint.list`` — ``GET /blueprint/api/blueprints``.
* ``vcfa.vra8.catalog-item.list`` — ``GET /catalog/api/admin/items``.
* ``vcfa.vra8.about`` — ``GET /iaas/api/about``.

**Pagination note.** The list surfaces return a ``{content: [], totalElements,
numberOfElements, ...}`` wrapper that self-describes whether more pages exist (so a
default call is not silent truncation); the read forwards the caller's ``$top`` /
``$skip`` (:func:`._paths.odata_list_query`) and the agent narrows / sorts a large
result through the dispatcher's JSONFlux handle (MEHO postulate 6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vra8._paths import (
    _ABOUT_PATH,
    _BLUEPRINTS_PATH,
    _CATALOG_ITEMS_PATH,
    _DEPLOYMENT_DETAIL_PATH,
    _DEPLOYMENTS_PATH,
    _PROJECTS_PATH,
    odata_list_query,
)
from meho_backplane.connectors.vra8.session import Vra8TargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.vra8.connector import Vra8Connector

__all__ = [
    "vra8_about_impl",
    "vra8_blueprint_list_impl",
    "vra8_catalog_item_list_impl",
    "vra8_deployment_get_impl",
    "vra8_deployment_list_impl",
    "vra8_project_list_impl",
]


async def vra8_project_list_impl(
    connector: Vra8Connector,
    operator: Operator,
    target: Vra8TargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.vra8.project.list`` — ``GET /iaas/api/projects``.

    Lists vRA 8 projects (the tenancy/quota boundary). Returns the IaaS paged
    ``{content: [Project, ...], totalElements, numberOfElements}`` wrapper; forwards
    the caller's ``$top`` / ``$skip``.
    """
    return await connector._vra_get(
        target, _PROJECTS_PATH, operator=operator, params=odata_list_query(params)
    )


async def vra8_deployment_list_impl(
    connector: Vra8Connector,
    operator: Operator,
    target: Vra8TargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.vra8.deployment.list`` — ``GET /deployment/api/deployments``.

    Lists vRA 8 deployments (the running workloads, Service Broker surface). Returns
    the paged ``{content: [Deployment, ...], totalElements, totalPages}`` wrapper;
    forwards the caller's ``$top`` / ``$skip``. A large list is
    reduced to a JSONFlux handle.
    """
    return await connector._vra_get(
        target, _DEPLOYMENTS_PATH, operator=operator, params=odata_list_query(params)
    )


async def vra8_deployment_get_impl(
    connector: Vra8Connector,
    operator: Operator,
    target: Vra8TargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.vra8.deployment.get`` — ``GET /deployment/api/deployments/{deployment_id}``.

    Reads one deployment's full detail — resources[] and request state
    (``lastRequest`` / ``inprogressRequests``, the pre-cutover quiescence signal).
    ``params["deployment_id"]`` is guaranteed present by the dispatcher's JSON Schema
    validation (``required: ["deployment_id"]``); it is percent-encoded with an empty
    safe set (OpenAPI ``style: simple`` semantics, the ``vcf-automation``
    ``deployment.get`` precedent) so an id carrying a reserved char can never
    traverse out of the ``/deployment/api/deployments/`` segment.
    """
    path = _DEPLOYMENT_DETAIL_PATH.format(
        deployment_id=quote(str(params["deployment_id"]), safe="")
    )
    return await connector._vra_get(target, path, operator=operator)


async def vra8_blueprint_list_impl(
    connector: Vra8Connector,
    operator: Operator,
    target: Vra8TargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.vra8.blueprint.list`` — ``GET /blueprint/api/blueprints``.

    Lists the Cloud Assembly blueprints (the IaC definitions to recreate). Returns
    the paged ``{content: [Blueprint, ...], totalElements}`` wrapper; forwards the
    caller's ``$top`` / ``$skip``.
    """
    return await connector._vra_get(
        target, _BLUEPRINTS_PATH, operator=operator, params=odata_list_query(params)
    )


async def vra8_catalog_item_list_impl(
    connector: Vra8Connector,
    operator: Operator,
    target: Vra8TargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.vra8.catalog-item.list`` — ``GET /catalog/api/admin/items``.

    Lists the Service Broker catalog items (admin / whole-estate view — the
    self-service offerings to republish). Returns the paged ``{content:
    [CatalogItem, ...], totalElements}`` wrapper; forwards the caller's ``$top`` /
    ``$skip``.
    """
    return await connector._vra_get(
        target, _CATALOG_ITEMS_PATH, operator=operator, params=odata_list_query(params)
    )


async def vra8_about_impl(
    connector: Vra8Connector,
    operator: Operator,
    target: Vra8TargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.vra8.about`` — ``GET /iaas/api/about``.

    Reads the IaaS API capabilities ``{latestApiVersion, supportedApis}``. A
    no-argument read; ``latestApiVersion`` is an API date label, not the product
    build.
    """
    del params  # schema declares the param object empty
    return await connector._vra_get(target, _ABOUT_PATH, operator=operator)
