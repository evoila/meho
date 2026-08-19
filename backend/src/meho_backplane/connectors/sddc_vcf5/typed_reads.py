# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) read implementations for :class:`SddcVcf5Connector`.

The vRealize / VCF 5.x SDDC Manager migration-inventory read set (#3059, initiative
`evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_) is registered as
typed ops (``source_kind="typed"``) so it dispatches on a fresh boot with **zero
catalog state** — no operator ``meho connector ingest`` + ``enable_reads`` step.
Each function here is the body a thin bound-method shim on
:class:`~meho_backplane.connectors.sddc_vcf5.connector.SddcVcf5Connector` delegates
to; the metadata + registrar live in
:mod:`meho_backplane.connectors.sddc_vcf5.typed_ops`.

This is the **legacy dual-impl** of ``product="sddc"`` — the migration-source VCF
5.x estate evoila brings customers *off* (the modern 9.x sibling is
:mod:`meho_backplane.connectors.sddc_manager`, ``sddc-rest``). SDDC Manager's REST
path surface is **stable since VCF 4.0**, so these ``/v1/*`` paths are the same
shapes the modern connector reads; the 5.x split exists because 5.x publishes only
Swagger 2.0 (OA3 is VCF-9.0-net-new — not ingestable) and its response *schemas*
drift by point release. The read core is therefore **typed** (hand-coded), the
``vcd`` / ``vcfa-vra8`` migration-source posture.

Every read is issued directly on the connector's own authenticated token session
via :meth:`HttpConnector._get_json` — no ``dispatch_child``, no ingested descriptor.
A raw ``401`` from a downstream call (SDDC Manager's expired-token signal)
propagates as :class:`httpx.HTTPStatusError` to the dispatcher's #2067 recovery arm,
which evicts the cached session token via the connector's public
:meth:`SddcVcf5Connector.invalidate_session` hook and re-dispatches once. The
handlers therefore do **not** wrap calls in the connector's internal
``_get_json_with_session_retry`` helper (that helper serves the fingerprint/probe
path, which the dispatcher does not drive) — the modern ``sddc_manager`` posture.

The 7-read migration-inventory surface (each path pinned by
:mod:`tests.test_connectors_sddc_vcf5_spec_reconcile`), all ``{elements: [...],
pageMetadata}`` envelopes:

* ``sddc.vcf5.domain.list`` — ``GET /v1/domains`` (management + workload domains).
* ``sddc.vcf5.cluster.list`` — ``GET /v1/clusters`` (optional ``domainId``).
* ``sddc.vcf5.host.list`` — ``GET /v1/hosts`` (optional ``domainId`` / ``clusterId`` / ``status``).
* ``sddc.vcf5.vcenter.list`` — ``GET /v1/vcenters`` (optional ``domainId``).
* ``sddc.vcf5.nsxt_cluster.list`` — ``GET /v1/nsxt-clusters``.
* ``sddc.vcf5.manager.list`` — ``GET /v1/sddc-managers`` (also the fingerprint surface).
* ``sddc.vcf5.task.list`` — ``GET /v1/tasks`` (optional ``status``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.sddc_manager.session import SddcTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.sddc_vcf5.connector import SddcVcf5Connector

__all__ = [
    "sddc5_cluster_list_impl",
    "sddc5_domain_list_impl",
    "sddc5_host_list_impl",
    "sddc5_manager_list_impl",
    "sddc5_nsxt_cluster_list_impl",
    "sddc5_task_list_impl",
    "sddc5_vcenter_list_impl",
]

# Spec-relative SDDC Manager endpoints. Every SDDC Manager REST path begins with
# ``/v1/`` and is stable since VCF 4.0; the connector's pooled per-target client
# carries the ``Authorization: Bearer <accessToken>`` header the token session
# primed. The reconcile lane introspects these ``_*_PATH`` constants.
# ``_TOKENS_PATH`` is the POST session-mint endpoint (see ``.connector``); the rest
# are GET reads.
_TOKENS_PATH = "/v1/tokens"
_DOMAINS_PATH = "/v1/domains"
_CLUSTERS_PATH = "/v1/clusters"
_HOSTS_PATH = "/v1/hosts"
_VCENTERS_PATH = "/v1/vcenters"
_NSXT_CLUSTERS_PATH = "/v1/nsxt-clusters"
_TASKS_PATH = "/v1/tasks"
_SDDC_MANAGERS_PATH = "/v1/sddc-managers"


def _optional_query(params: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any] | None:
    """Build a query dict from the truthy *keys* present in *params*, or ``None``.

    Mirrors the modern ``sddc_manager`` filter-forwarding shape: only keys the
    caller supplied a truthy value for are forwarded, and an empty result collapses
    to ``None`` so :meth:`HttpConnector._get_json` omits the query string entirely.
    """
    query: dict[str, Any] = {}
    for key in keys:
        val = params.get(key)
        if val:
            query[key] = val
    return query or None


async def sddc5_domain_list_impl(
    connector: SddcVcf5Connector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.vcf5.domain.list`` — ``GET /v1/domains``.

    Lists every VCF domain the SDDC Manager governs — the management domain plus any
    workload domains — as a paginated ``{elements: [...], pageMetadata}`` envelope.
    The entry point for a migration inventory: the top of the VCF tenancy tree and
    the anchor for domain-scoped cluster / host / vCenter reads.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _DOMAINS_PATH, operator=operator)


async def sddc5_cluster_list_impl(
    connector: SddcVcf5Connector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.vcf5.cluster.list`` — ``GET /v1/clusters``.

    Lists vSphere clusters across all VCF domains, or narrowed to one domain via the
    optional ``domainId`` filter. The primary inventory read for cluster count,
    datastore type, and host membership a migration must re-home.
    """
    query = _optional_query(params, ("domainId",))
    return await connector._get_json(target, _CLUSTERS_PATH, operator=operator, params=query)


async def sddc5_host_list_impl(
    connector: SddcVcf5Connector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.vcf5.host.list`` — ``GET /v1/hosts``.

    Enumerates ESXi hosts across all VCF domains, or narrowed by the optional
    ``domainId`` / ``clusterId`` / ``status`` filters — the physical footprint (host
    count, FQDN, ESXi version, assignment status) a migration must size. Large VCF
    estates return dozens or hundreds of hosts; a big list is reduced to a JSONFlux
    handle.
    """
    query = _optional_query(params, ("domainId", "clusterId", "status"))
    return await connector._get_json(target, _HOSTS_PATH, operator=operator, params=query)


async def sddc5_vcenter_list_impl(
    connector: SddcVcf5Connector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.vcf5.vcenter.list`` — ``GET /v1/vcenters``.

    Lists the vCenter Server instances SDDC Manager manages, or narrowed to one
    domain via the optional ``domainId`` filter. Cross the returned ``fqdn`` against
    the vSphere connector for VM-level migration inventory.
    """
    query = _optional_query(params, ("domainId",))
    return await connector._get_json(target, _VCENTERS_PATH, operator=operator, params=query)


async def sddc5_nsxt_cluster_list_impl(
    connector: SddcVcf5Connector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.vcf5.nsxt_cluster.list`` — ``GET /v1/nsxt-clusters``.

    Lists the NSX-T manager clusters SDDC Manager manages and the domains each one
    backs — the network fabric a migration must re-home. ``{elements: [{id, vipFqdn,
    domainIds}, ...], pageMetadata}``.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _NSXT_CLUSTERS_PATH, operator=operator)


async def sddc5_manager_list_impl(
    connector: SddcVcf5Connector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.vcf5.manager.list`` — ``GET /v1/sddc-managers``.

    Lists the SDDC Manager appliances — their FQDN, IP, version, and management
    domain. The primary 'which SDDC Manager fronts this VCF stack, and what version'
    read, and the same surface :meth:`SddcVcf5Connector.fingerprint` reads, exposed
    as an operator-callable typed op.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _SDDC_MANAGERS_PATH, operator=operator)


async def sddc5_task_list_impl(
    connector: SddcVcf5Connector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.vcf5.task.list`` — ``GET /v1/tasks``.

    Lists in-flight or recently completed VCF workflow tasks, optionally narrowed by
    the ``status`` filter. The read for confirming the source estate is quiescent
    before a migration cutover, or triaging a workflow that failed.
    """
    query = _optional_query(params, ("status",))
    return await connector._get_json(target, _TASKS_PATH, operator=operator, params=query)
