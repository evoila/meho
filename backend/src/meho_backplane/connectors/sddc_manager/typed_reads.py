# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) read implementations for :class:`SddcManagerConnector`.

The audited SDDC Manager read set (#2306) is converted from the
ingested-row curation in
:mod:`meho_backplane.connectors.sddc_manager.core_ops` to typed ops
(``source_kind="typed"``) so it dispatches on a fresh boot with **zero
catalog state** -- the #2247 failure class the ingested curation was
subject to (per-deploy catalog state). Each function here is the body a
thin bound-method shim on
:class:`~meho_backplane.connectors.sddc_manager.connector.SddcManagerConnector`
delegates to; the metadata + registrar live in
:mod:`meho_backplane.connectors.sddc_manager.typed_ops`.

Every read is issued directly on the connector's own authenticated token
session via :meth:`HttpConnector._get_json` -- no ``dispatch_child``, no
ingested descriptor. A raw ``401`` from a downstream call (SDDC Manager's
expired-token signal) propagates as :class:`httpx.HTTPStatusError` up to
the dispatcher's G0.29-T2 (#2067) recovery arm, which evicts the cached
session token via the connector's public
:meth:`SddcManagerConnector.invalidate_session` hook (established by
#2290) and re-dispatches once. The handlers therefore do **not** wrap
calls in the connector's internal ``_get_json_with_session_retry``
helper (that helper serves the fingerprint/probe path, which the
dispatcher does not drive).

The audited 12-read set (paths from the #2294 lab audit, cross-checked
against the VCF / SDDC Manager 9.0 API at
https://developer.broadcom.com/xapis/vmware-cloud-foundation-api/latest/):

* ``sddc.domain.list`` -- ``GET /v1/domains`` (management + workload
  domain inventory).
* ``sddc.domain.status`` -- ``GET /v1/domains/{id}/status`` (per-domain
  status: the READY / ACTIVATING / ERROR lifecycle state).
* ``sddc.cluster.list`` -- ``GET /v1/clusters`` (vSphere cluster
  inventory; optional ``domainId`` filter).
* ``sddc.host.list`` -- ``GET /v1/hosts`` (ESXi host inventory; optional
  ``domainId`` / ``clusterId`` / ``status`` filters).
* ``sddc.vcenter.list`` -- ``GET /v1/vcenters`` (managed vCenter
  inventory; optional ``domainId`` filter).
* ``sddc.nsxt_cluster.list`` -- ``GET /v1/nsxt-clusters`` (NSX-T manager
  cluster inventory).
* ``sddc.credential.list`` -- ``GET /v1/credentials`` (nested-infra
  credential inventory). **Credential-read gated** (#2306): dispatch
  routes through the approval queue (``requires_approval=True``) and the
  op-id is classified ``credential_read`` so audit/broadcast rows collapse
  to aggregate-only; the handler additionally scrubs every secret-keyed
  value (``password`` / ``privateKey`` / ...) so no credential material
  ever rides the ``OperationResult``.
* ``sddc.task.list`` -- ``GET /v1/tasks`` (in-flight / recent VCF
  workflow tasks; optional ``status`` filter).
* ``sddc.system.info`` -- ``GET /v1/system`` (SDDC Manager system-level
  settings summary).
* ``sddc.vcf_service.list`` -- ``GET /v1/vcf-services`` (the running VCF
  micro-service inventory + health).
* ``sddc.manager.list`` -- ``GET /v1/sddc-managers`` (SDDC Manager
  appliance inventory).
* ``sddc.license.list`` -- ``GET /v1/license-keys`` (the license-key
  inventory).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.sddc_manager.session import SddcTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.sddc_manager.connector import SddcManagerConnector

__all__ = [
    "REDACTED",
    "sddc_cluster_list_impl",
    "sddc_credential_list_impl",
    "sddc_domain_list_impl",
    "sddc_domain_status_impl",
    "sddc_host_list_impl",
    "sddc_license_list_impl",
    "sddc_manager_list_impl",
    "sddc_nsxt_cluster_list_impl",
    "sddc_system_info_impl",
    "sddc_task_list_impl",
    "sddc_vcenter_list_impl",
    "sddc_vcf_service_list_impl",
]

_log = structlog.get_logger(__name__)

# Spec-relative SDDC Manager endpoints. Every SDDC Manager REST path
# begins with ``/v1/``; the connector's pooled per-target client carries
# the ``Authorization: Bearer <accessToken>`` header the token session
# primed (#2290).
_DOMAINS_PATH = "/v1/domains"
_DOMAIN_STATUS_PATH = "/v1/domains/{id}/status"
_CLUSTERS_PATH = "/v1/clusters"
_HOSTS_PATH = "/v1/hosts"
_VCENTERS_PATH = "/v1/vcenters"
_NSXT_CLUSTERS_PATH = "/v1/nsxt-clusters"
_CREDENTIALS_PATH = "/v1/credentials"
_TASKS_PATH = "/v1/tasks"
_SYSTEM_PATH = "/v1/system"
_VCF_SERVICES_PATH = "/v1/vcf-services"
_SDDC_MANAGERS_PATH = "/v1/sddc-managers"
_LICENSE_KEYS_PATH = "/v1/license-keys"

#: Sentinel written in place of a scrubbed secret value. A non-empty
#: marker (rather than dropping the key) keeps the shape stable so the
#: agent can see a credential *existed* without learning its value. Same
#: convention :mod:`meho_backplane.connectors.nsx.typed_reads` uses.
REDACTED = "***REDACTED***"

#: Key names (matched case-insensitively) whose value -- scalar or
#: subtree -- is secret material scrubbed wherever it appears in a
#: ``sddc.credential.list`` response. SDDC Manager is the system of record
#: for nested-infra credentials, so ``GET /v1/credentials`` returns the
#: live ``password`` (and, for certificate/SSH accounts, private-key
#: material) alongside the credential's identity. The handler masks every
#: known secret spelling so the returned inventory shows *which* accounts
#: exist (username, resource, accountType) without leaking their secrets.
_SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "passphrase",
        "secret",
        "client_secret",
        "token",
        "private_key",
        "privatekey",
        "sshprivatekey",
        "certificate",
    }
)


def _redact_secrets(value: Any) -> Any:
    """Return *value* with secret-keyed material scrubbed, recursively.

    Walks dicts and lists. A key matching :data:`_SECRET_FIELDS`
    (case-insensitively) has its whole value replaced with
    :data:`REDACTED`; every other value is walked so a credential nested
    inside ``elements[].password`` -- or a private key under a nested
    certificate object -- is still caught. Scalars pass through unchanged.
    The input is not mutated; a new structure is returned.
    """
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and key.lower() in _SECRET_FIELDS
                else _redact_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def _optional_query(params: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any] | None:
    """Build a query dict from the truthy *keys* present in *params*, or ``None``.

    Mirrors the NSX ``nsx.alarm.list`` filter-forwarding shape: only keys
    the caller supplied a truthy value for are forwarded, and an empty
    result collapses to ``None`` so :meth:`HttpConnector._get_json` omits
    the query string entirely.
    """
    query: dict[str, Any] = {}
    for key in keys:
        val = params.get(key)
        if val:
            query[key] = val
    return query or None


async def sddc_domain_list_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.domain.list`` -- ``GET /v1/domains``.

    Lists every VCF domain the SDDC Manager governs -- the management
    domain plus any workload domains -- as a paginated ``{elements: [...]}``
    envelope. The entry point for domain-scoped cluster / host / vCenter
    reads and for mapping a workload domain to its vCenter and NSX-T
    cluster.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _DOMAINS_PATH, operator=operator)


async def sddc_domain_status_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.domain.status`` -- ``GET /v1/domains/{id}/status``.

    Reads the lifecycle status of one VCF domain (its ACTIVE / ACTIVATING
    / ERROR state and the last status transition). Requires a domain
    ``id`` from ``sddc.domain.list``; the read an operator runs when a
    domain-create or expand workflow is in flight or a domain is reported
    unhealthy.
    """
    domain_id = params["id"]
    path = _DOMAIN_STATUS_PATH.format(id=domain_id)
    return await connector._get_json(target, path, operator=operator)


async def sddc_cluster_list_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.cluster.list`` -- ``GET /v1/clusters``.

    Lists vSphere clusters across all VCF domains, or narrowed to one
    domain via the optional ``domainId`` filter. The primary inventory
    read for cluster count, datastore type, and host membership.
    ``{elements: [...]}`` with ``id`` / ``name`` / ``primaryDatastoreType``
    / ``domainId`` per cluster.
    """
    query = _optional_query(params, ("domainId",))
    return await connector._get_json(target, _CLUSTERS_PATH, operator=operator, params=query)


async def sddc_host_list_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.host.list`` -- ``GET /v1/hosts``.

    Enumerates ESXi hosts across all VCF domains, or narrowed by the
    optional ``domainId`` / ``clusterId`` / ``status`` filters. The
    primary read for host count, FQDN, ESXi version, and assignment
    status. Large VCF deployments return dozens or hundreds of hosts.
    """
    query = _optional_query(params, ("domainId", "clusterId", "status"))
    return await connector._get_json(target, _HOSTS_PATH, operator=operator, params=query)


async def sddc_vcenter_list_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.vcenter.list`` -- ``GET /v1/vcenters``.

    Lists the vCenter Server instances the SDDC Manager manages, or
    narrowed to one domain via the optional ``domainId`` filter. Cross
    the returned ``fqdn`` against the vSphere connector for VM-level
    reads. ``{elements: [...]}`` with ``id`` / ``fqdn`` / ``domain`` per
    vCenter.
    """
    query = _optional_query(params, ("domainId",))
    return await connector._get_json(target, _VCENTERS_PATH, operator=operator, params=query)


async def sddc_nsxt_cluster_list_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.nsxt_cluster.list`` -- ``GET /v1/nsxt-clusters``.

    Lists the NSX-T manager clusters SDDC Manager manages and the domains
    each one backs. ``{elements: [...]}`` with ``id`` / ``vipFqdn`` /
    ``domainIds`` per NSX-T cluster -- the read for mapping which NSX-T
    cluster fronts a given workload domain.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _NSXT_CLUSTERS_PATH, operator=operator)


async def sddc_credential_list_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.credential.list`` -- ``GET /v1/credentials`` (credential-read, redacted).

    Lists the nested-infra credentials SDDC Manager is the system of
    record for -- the ESXi / vCenter / NSX-T / backup service accounts it
    rotates and stores. SDDC Manager returns the live ``password`` (and,
    for certificate/SSH accounts, private-key material) on this endpoint,
    so this handler scrubs every secret-keyed value at the boundary via
    :func:`_redact_secrets`: the returned inventory shows *which* accounts
    exist (``username`` / ``resource`` / ``accountType`` / ``credentialType``)
    with each secret replaced by :data:`REDACTED`.

    The op is additionally credential-read *gated*: its typed registration
    carries ``requires_approval=True`` (so the dispatcher's policy gate
    routes it to the approval queue -- not dispatchable without operator
    approval) and its op-id ``sddc.credential.list`` is on the
    ``_CREDENTIAL_READ_OPS`` allowlist so ``classify_op`` collapses its
    audit + broadcast payload to aggregate-only. The boundary scrub here
    is the third, belt-and-suspenders layer: even the approved
    ``OperationResult`` the operator receives carries no secret value.
    """
    del params  # schema declares the param object empty
    _log.info("sddc_credential_list_read", target=target.name)
    raw = await connector._get_json(target, _CREDENTIALS_PATH, operator=operator)
    return cast("dict[str, Any]", _redact_secrets(raw))


async def sddc_task_list_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.task.list`` -- ``GET /v1/tasks``.

    Lists in-flight or recently completed VCF workflow tasks, optionally
    narrowed by the ``status`` filter (Successful / Failed / In_Progress /
    Pending / Cancelled). The read for monitoring a domain-expand,
    host-commission, or update-apply workflow, or triaging one that
    failed. ``{elements: [...]}`` with ``id`` / ``name`` / ``status`` /
    ``type`` / ``subtasks`` / ``errors`` per task.
    """
    query = _optional_query(params, ("status",))
    return await connector._get_json(target, _TASKS_PATH, operator=operator, params=query)


async def sddc_system_info_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.system.info`` -- ``GET /v1/system``.

    Reads the SDDC Manager system-level settings summary -- the
    appliance-wide configuration (proxy, CEIP, DNS/NTP posture) SDDC
    Manager exposes at its ``/v1/system`` root. The read for confirming
    the SDDC Manager's own platform configuration before an inventory or
    lifecycle operation.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _SYSTEM_PATH, operator=operator)


async def sddc_vcf_service_list_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.vcf_service.list`` -- ``GET /v1/vcf-services``.

    Lists the VCF platform micro-services running on the SDDC Manager
    appliance and their status. The read for confirming the SDDC Manager
    control plane is healthy -- which service is degraded when an
    operation is failing for no obvious inventory reason.
    ``{elements: [...]}`` with ``id`` / ``name`` / ``status`` /
    ``version`` per service.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _VCF_SERVICES_PATH, operator=operator)


async def sddc_manager_list_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.manager.list`` -- ``GET /v1/sddc-managers``.

    Lists the SDDC Manager appliances -- their FQDN, IP, version, and the
    management domain each belongs to. The primary read for 'which SDDC
    Manager manages this VCF stack', and the same surface
    :meth:`SddcManagerConnector.fingerprint` reads, exposed as an
    operator-callable typed op. ``{elements: [...]}`` with ``id`` /
    ``fqdn`` / ``ipAddress`` / ``version`` / ``domain`` per appliance.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _SDDC_MANAGERS_PATH, operator=operator)


async def sddc_license_list_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.license.list`` -- ``GET /v1/license-keys``.

    Lists the license keys registered with SDDC Manager -- their product
    type, key, description, and usage. The read for a licensing-compliance
    question ('is this VCF stack licensed', 'which keys are near their
    limit'). ``{elements: [...]}`` with ``key`` / ``productType`` /
    ``description`` / ``licenseKeyUsage`` per key.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _LICENSE_KEYS_PATH, operator=operator)
