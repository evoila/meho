# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) workload-domain write implementations for the connector.

The curated SDDC Manager workload-domain (WLD) lifecycle *submit* primitives
(#3497). #368 shipped the connector read-only and deferred writes; this module
lands the four-step governed build path an operator (or the deploy-automation
add-on) drives to stand a workload domain up **through the backplane**:

1. ``sddc.network_pool.create`` -- ``POST /v1/network-pools`` with a
   ``NetworkPool`` carrying the vMotion + **NFS** networks the domain draws
   from. ``safety_level="caution"`` + ``requires_approval=True``.
2. ``sddc.host.validate`` -- ``POST /v1/hosts/validations`` with the list of
   ``HostCommissionSpec`` (``storageType="NFS"``); a non-mutating pre-flight
   returning the vendor ``Validation`` to poll. ``safety_level="caution"``,
   no approval.
3. ``sddc.host.commission`` -- ``POST /v1/hosts`` with the same list; the
   estate mutation that adds the hosts. ``safety_level="dangerous"`` +
   ``requires_approval=True``; returns the ``Task`` to poll.
4. ``sddc.domain.validate`` -- ``POST /v1/domains/validations`` with the
   ``DomainCreationSpec`` (NFS shape:
   ``computeSpec.clusterSpecs[].datastoreSpec.nfsDatastoreSpecs[].nasVolume``,
   never ``vsanDatastoreSpec``); a non-mutating pre-flight. ``caution``, no
   approval.
5. ``sddc.domain.create`` -- ``POST /v1/domains`` with the same spec; the
   ``202``-async domain build. ``safety_level="dangerous"`` +
   ``requires_approval=True``; returns the ``Task`` to poll.

The create calls are natively asynchronous -- each returns its tracking
``Task`` in seconds and the multi-hour build runs on the appliance -- so every
call here is short, the standard park -> approve -> resume machinery governs
each write as-is, and no long-blocking loop lives in this module. Sequencing
(validate -> create -> poll ``sddc.task.get`` / ``sddc.domain.status`` to
``ACTIVE``) is the caller's responsibility (runbook / automation add-on), not
the dispatcher's. There is deliberately **no** bulk enable path: each write is
a distinct approval-gated typed op.

The optional NSX edge-cluster create/validate ops are out of scope: the
Envision estate's Supervisor path is VDS + Foundation LB, so no edge cluster
is needed (issue #3497 lists them for completeness only).

Each write is issued directly on the connector's own authenticated token
session via :meth:`HttpConnector._post_json`. A raw ``401`` (SDDC Manager's
expired-token signal) propagates as :class:`httpx.HTTPStatusError` up to the
dispatcher's #2067 recovery arm, which evicts the cached session token via the
connector's public :meth:`SddcManagerConnector.invalidate_session` hook and
re-dispatches once. That single re-dispatch is safe even for these
non-idempotent POSTs: a ``401`` means the request was rejected at auth
*before* the appliance processed it, so the first attempt had no effect --
the same argument :mod:`meho_backplane.connectors.vcf_installer.typed_writes`
documents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.sddc_manager.session import SddcTargetLike

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.sddc_manager.connector import SddcManagerConnector

__all__ = [
    "SDDC_DOMAIN_CREATE_OP_ID",
    "SDDC_DOMAIN_VALIDATE_OP_ID",
    "SDDC_HOST_COMMISSION_OP_ID",
    "SDDC_HOST_VALIDATE_OP_ID",
    "SDDC_NETWORK_POOL_CREATE_OP_ID",
    "TYPED_WRITE_DECLARED_OP_IDS",
    "sddc_domain_create_impl",
    "sddc_domain_validate_impl",
    "sddc_host_commission_impl",
    "sddc_host_validate_impl",
    "sddc_network_pool_create_impl",
]

#: ``sddc.network_pool.create`` -- create the network pool a WLD draws from.
SDDC_NETWORK_POOL_CREATE_OP_ID: Final[str] = "sddc.network_pool.create"
#: ``sddc.host.validate`` -- pre-flight a host-commission spec (non-mutating).
SDDC_HOST_VALIDATE_OP_ID: Final[str] = "sddc.host.validate"
#: ``sddc.host.commission`` -- add ESXi hosts to the free pool (estate mutation).
SDDC_HOST_COMMISSION_OP_ID: Final[str] = "sddc.host.commission"
#: ``sddc.domain.validate`` -- pre-flight a DomainCreationSpec (non-mutating).
SDDC_DOMAIN_VALIDATE_OP_ID: Final[str] = "sddc.domain.validate"
#: ``sddc.domain.create`` -- create the workload domain (202-async build).
SDDC_DOMAIN_CREATE_OP_ID: Final[str] = "sddc.domain.create"

# --- Hand-coded wire paths — the spec-reconcile lane's introspection source
# (module constants introspected by value, the #2944 pattern). ---
#: ``POST`` — create a network pool (the vendor ``createNetworkPool``).
_NETWORK_POOLS_PATH = "/v1/network-pools"
#: ``POST`` — validate a host-commission spec (``validateHostCommissionSpec``).
_HOSTS_VALIDATIONS_PATH = "/v1/hosts/validations"
#: ``POST`` — commission hosts (the vendor ``commissionHosts``).
_HOSTS_PATH = "/v1/hosts"
#: ``POST`` — validate a DomainCreationSpec (``validateDomainCreationSpec``).
_DOMAINS_VALIDATIONS_PATH = "/v1/domains/validations"
#: ``POST`` — create a workload domain (the vendor ``createDomain``).
_DOMAINS_PATH = "/v1/domains"

#: The exact ``METHOD:/path`` set these WLD write primitives hand-code. The
#: 9.1 reconcile lane
#: (:mod:`tests.test_connectors_sddc_manager_91_spec_reconcile`) asserts each
#: is served by the pinned ``sddc-manager-9.1`` OpenAPI, and pins this set so
#: it can't go vacuous. Reads stay guarded against ``sddc-manager-9.0`` by the
#: sibling lane.
TYPED_WRITE_DECLARED_OP_IDS: frozenset[str] = frozenset(
    {
        f"POST:{_NETWORK_POOLS_PATH}",
        f"POST:{_HOSTS_VALIDATIONS_PATH}",
        f"POST:{_HOSTS_PATH}",
        f"POST:{_DOMAINS_VALIDATIONS_PATH}",
        f"POST:{_DOMAINS_PATH}",
    }
)


async def sddc_network_pool_create_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.network_pool.create`` -- ``POST /v1/network-pools``.

    Submits ``params["spec"]`` (a vendor ``NetworkPool`` — ``name`` plus the
    ``networks[]`` the domain's hosts draw from, which for an NFS-principal
    workload domain must include the vMotion and NFS networks) verbatim and
    returns the created pool (``201``). ``requires_approval=True``: the
    dispatcher parks the call for approval before this handler runs.
    """
    return await connector._post_json(
        target, _NETWORK_POOLS_PATH, operator=operator, json=params["spec"]
    )


async def sddc_host_validate_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.host.validate`` -- ``POST /v1/hosts/validations``.

    Submits ``params["spec"]`` (a JSON array of vendor ``HostCommissionSpec``,
    each with ``fqdn`` / ``username`` / ``password`` / ``networkPoolId`` and
    ``storageType`` — ``NFS`` for the Envision estate) and returns the vendor
    ``Validation`` (``202`` with ``executionStatus="IN_PROGRESS"`` and the
    ``id`` to poll via ``sddc.task.get`` — validations resolve to a task).
    Mutates no estate; ``safety_level="caution"``, no approval.
    """
    return await connector._post_json(
        target, _HOSTS_VALIDATIONS_PATH, operator=operator, json=params["spec"]
    )


async def sddc_host_commission_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.host.commission`` -- ``POST /v1/hosts``.

    Submits ``params["spec"]`` (the same array of ``HostCommissionSpec``,
    validated first via ``sddc.host.validate``) and returns the vendor
    ``Task`` the moment the commission is accepted (``202``); poll
    ``sddc.task.get`` with the returned ``id`` to a terminal ``status``. The
    estate mutation that moves ESXi hosts into the free pool —
    ``safety_level="dangerous"`` + ``requires_approval=True``, so the
    dispatcher has already parked and an approver already resumed by the time
    this handler runs.
    """
    return await connector._post_json(target, _HOSTS_PATH, operator=operator, json=params["spec"])


async def sddc_domain_validate_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.domain.validate`` -- ``POST /v1/domains/validations``.

    Submits ``params["spec"]`` (a vendor ``DomainCreationSpec``) for a
    non-mutating pre-flight and returns the vendor ``Validation`` to poll. Run
    this to a passing ``resultStatus`` before ``sddc.domain.create`` — the
    ``DomainCreationSpec`` is trap-rich (NFS-vs-vSAN datastore shape,
    ``clusterImageId``), and the validation is the vendor's own answer to
    "will this build". Mutates no estate; ``safety_level="caution"``, no
    approval.
    """
    return await connector._post_json(
        target, _DOMAINS_VALIDATIONS_PATH, operator=operator, json=params["spec"]
    )


async def sddc_domain_create_impl(
    connector: SddcManagerConnector,
    operator: Operator,
    target: SddcTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``sddc.domain.create`` -- ``POST /v1/domains``.

    Submits ``params["spec"]`` (an NFS-shaped ``DomainCreationSpec`` —
    ``computeSpec.clusterSpecs[].datastoreSpec.nfsDatastoreSpecs[].nasVolume``,
    not ``vsanDatastoreSpec``) verbatim and returns the vendor ``Task`` the
    moment the build is accepted (``202``); the domain build itself runs for
    hours on the appliance — poll ``sddc.task.get`` with the returned ``id``
    (and ``sddc.domain.status`` once the domain object exists) to ``ACTIVE``.
    The estate mutation — ``safety_level="dangerous"`` + ``requires_approval``,
    so the dispatcher has already parked and an approver already resumed by
    the time this handler runs. The reviewer context surfaces the op + target
    + subject identity only, never the hidden ``params`` body
    (:func:`~meho_backplane.operations.approval_context.resolve_reviewer_context`),
    so the spec's plaintext passwords never reach the approver.
    """
    return await connector._post_json(target, _DOMAINS_PATH, operator=operator, json=params["spec"])
