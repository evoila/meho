# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SDDC Manager read-only core — curated operator-enabled subset (browse breadth).

This module names the **4 read-only SDDC Manager operations** left as
ingested-row curation after the audited 12-read lab-audit set (domains
list + status, clusters, hosts, vcenters, nsxt-clusters, credentials,
tasks, system, vcf-services, sddc-managers, license-keys) was promoted to
first-class **typed** ops in
:mod:`meho_backplane.connectors.sddc_manager.typed_ops` (#2306). The
curated set here keeps the wider ingested breadth browsable (the SDDC
Manager software release, per-domain detail, network pools, and LCM
bundles) out of the much larger VCF API corpus the G0.7 spec-ingestion
pipeline lands under ``connector_id="sddc-rest-9.0"``. The curation is
two-layered:

* :data:`SDDC_CORE_GROUPS` — the operator-reviewed ``when_to_use``
  hint per LLM-grouping pass output group. Each entry's ``group_key``
  is the deterministic slug the path-prefix classifier assigns to SDDC
  Manager ops (see :func:`classify_sddc_op` below); the ``when_to_use``
  is what the agent reads verbatim through
  :func:`~meho_backplane.operations.meta_tools.list_operation_groups`
  to pick a group to search within.
* :data:`SDDC_CORE_OPS` — the 4 ``EndpointDescriptor.op_id`` strings
  that flip to ``is_enabled=True`` at operator-review time, paired
  with the per-op ``llm_instructions`` blob the agent inlines into
  the reasoning context when it sees the op in
  :func:`~meho_backplane.operations.meta_tools.search_operations`
  hits. Every other op under the same connector triple stays
  ``is_enabled=False`` (the G0.7 ingestion default for
  ``source_kind='ingested'`` rows).

Per Initiative #368 and CLAUDE.md postulates 1-2, SDDC Manager is **fully
generic-ingested**: the underlying ops are not registered in code, they
live in the ``endpoint_descriptor`` table. This module only carries the
**operator-review metadata** the substrate uses at the review step — the
actual curation is applied through :func:`apply_sddc_core_curation`
against an existing ingested connector.

``SDDC_PRODUCT`` / ``SDDC_VERSION`` / ``SDDC_IMPL_ID`` note
------------------------------------------------------------

``SDDC_PRODUCT = "sddc"`` is the value
:func:`~meho_backplane.operations._lookup.parse_connector_id` extracts
from ``SDDC_CONNECTOR_ID = "sddc-rest-9.0"`` (``head.split("-", 1)[0]``).
Since #1814 (Initiative #1810) realigned the registry, it is **also**
:attr:`SddcManagerConnector.product` — the connector registers under the
short, dispatch-canonical ``"sddc"`` token, so the registry key, the
resolver target, and the parser-derived spelling all agree. All
``endpoint_descriptor`` and ``operation_group`` rows for this connector
triple carry ``product="sddc"``; operators pass ``--product sddc`` when driving
``meho connector ingest``. Review-time helpers (including
:class:`~meho_backplane.operations.ingest.service.ReviewService`) derive
the product via ``parse_connector_id`` so the same constant is consistent
across the ingestion, review, and dispatch legs.

The 4 ops (paths cross-checked against VCF / SDDC Manager API 9.0 at
https://developer.broadcom.com/xapis/vmware-cloud-foundation-api/latest/):

1. ``GET:/v1/releases/system`` — ``sddc.about`` — SDDC Manager software
   release (version, build date, component BOM). The same surface the
   operator reads to confirm the VCF build under management; exposing it
   as an agent-callable op lets the agent run a sanity probe before heavier
   inventory reads.
2. ``GET:/v1/domains/{id}`` — ``sddc.domain.info`` — full domain detail
   including associated vCenter(s), NSX-T cluster, clusters, and hosts.
3. ``GET:/v1/network-pools`` — ``sddc.network_pool.list`` — network pools
   defining the IP ranges and VLANs provisioned to new hosts at commission
   time.
4. ``GET:/v1/bundles`` — ``sddc.bundle.list`` — LCM bundle inventory (VCF
   update packages, async patches). Read-only; lifecycle writes stay
   ``staged`` per Initiative #368.

The audited operational reads — domains list + status, clusters, hosts,
vcenters, nsxt-clusters, credentials, tasks, system, vcf-services,
sddc-managers, and license-keys — are **not** curated here: #2306 promoted
them to first-class typed ops (``source_kind="typed"``,
:mod:`meho_backplane.connectors.sddc_manager.typed_ops`) that dispatch on a
fresh boot with zero catalog state. Their ingested rows still exist and
stay browsable; this module simply no longer flips them to
``is_enabled=True``. :data:`SDDC_PATH_RULES` retains the full ``/v1/``
taxonomy so the ingested breadth keeps its group organisation.

Path families and group_keys
-----------------------------

Every SDDC Manager REST path begins with ``/v1/``. :data:`SDDC_PATH_RULES`
retains the full taxonomy (``releases``, ``sddc-managers``, ``domains``,
``clusters``, ``hosts``, ``network-pools``, ``bundles``, ``tasks``) so the
ingested breadth keeps its group organisation even though only 4 of those
families carry a curated read now. The 4 curated ops span 4 groups
(``sddc-releases``, ``sddc-domains``, ``sddc-network-pools``,
``sddc-bundles``); the ``sddc-domains`` group carries the domain-detail
read (its list companion moved to the typed surface).

Curation application
--------------------

:func:`apply_sddc_core_curation` is the operator-review-time substrate
call that makes exactly the 4 curated ops dispatchable and leaves every
other ingested op disabled. The substrate has no "enable only ops X, Y, Z
under group G" verb — :meth:`ReviewService.enable_group` cascades
``is_enabled=True`` to every child op. The helper threads this needle via
the audit-log-driven operator-override exclusion (see
:func:`~meho_backplane.operations.ingest._internals.operator_disabled_op_ids`),
exactly matching the pattern :func:`apply_nsx_core_curation` in
:mod:`meho_backplane.connectors.nsx.core_ops` established.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog

from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "SDDC_CONNECTOR_ID",
    "SDDC_CORE_GROUPS",
    "SDDC_CORE_OPS",
    "SDDC_IMPL_ID",
    "SDDC_PATH_RULES",
    "SDDC_PRODUCT",
    "SDDC_VERSION",
    "SddcCoreGroup",
    "SddcCoreOp",
    "apply_sddc_core_curation",
    "classify_sddc_op",
]

_log = structlog.get_logger(__name__)

#: Endpoint-descriptor product key — what
#: :func:`~meho_backplane.operations._lookup.parse_connector_id`
#: extracts from ``"sddc-rest-9.0"`` (first hyphen-segment of
#: impl_id ``"sddc-rest"``).
#:
#: Since #1814 (Initiative #1810) this equals
#: :attr:`SddcManagerConnector.product` (``"sddc"``) — the v2 registry
#: key and resolver target. All ``endpoint_descriptor`` +
#: ``operation_group`` rows carry ``product="sddc"``; see the module
#: docstring for details.
SDDC_PRODUCT: Final[str] = "sddc"
SDDC_VERSION: Final[str] = "9.0"
SDDC_IMPL_ID: Final[str] = "sddc-rest"

#: Connector-id slug the G0.6 dispatcher's ``parse_connector_id``
#: round-trips back to the triple above: ``"sddc-rest-9.0"``.
SDDC_CONNECTOR_ID: Final[str] = f"{SDDC_IMPL_ID}-{SDDC_VERSION}"


@dataclass(frozen=True, slots=True)
class SddcCoreGroup:
    """One curated operator-review entry for an SDDC Manager operation group.

    ``group_key`` is the slug the path-prefix classifier emits (see
    :data:`SDDC_PATH_RULES`). ``name`` is the operator-readable label
    ``meho connector review`` renders. ``when_to_use`` is the agent-facing
    hint :func:`list_operation_groups` returns verbatim; every entry is a
    single complete sentence so the agent's group-selection step has
    unambiguous guidance.
    """

    group_key: str
    name: str
    when_to_use: str


@dataclass(frozen=True, slots=True)
class SddcCoreOp:
    """One curated operator-review entry for an SDDC Manager operation.

    ``op_id`` follows the ``METHOD:path`` shape every
    ``source_kind='ingested'`` row uses; the path matches an entry
    in the VCF / SDDC Manager 9.0 API spec.

    ``llm_instructions`` is the per-op JSON blob the meta-tools inline
    verbatim when the op surfaces. The shape (``when_to_call`` /
    ``output_shape`` / ``next_step``) mirrors the typed-connector convention
    from :mod:`meho_backplane.connectors.bind9.ops_zone` and
    :mod:`meho_backplane.connectors.nsx.core_ops` — same agent reads both
    surfaces, so the structure stays uniform.
    """

    op_id: str
    group_key: str
    llm_instructions: dict[str, object]


#: Path-prefix → group_key classifier rules for SDDC Manager.
#:
#: First match wins. Order: more specific prefixes first (``/v1/sddc-managers``
#: before any hypothetical ``/v1/sddc-*`` catch-all). Every SDDC Manager
#: path begins with ``/v1/`` so there is no manager-vs-policy split like
#: NSX; the top-level resource name is the natural grouping key.
SDDC_PATH_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("/v1/releases", "sddc-releases"),
    ("/v1/sddc-managers", "sddc-managers"),
    ("/v1/domains", "sddc-domains"),
    ("/v1/clusters", "sddc-clusters"),
    ("/v1/hosts", "sddc-hosts"),
    ("/v1/network-pools", "sddc-network-pools"),
    ("/v1/bundles", "sddc-bundles"),
    ("/v1/tasks", "sddc-tasks"),
)


def classify_sddc_op(op_id: str) -> str:
    """Return the curated ``group_key`` for an SDDC Manager op_id, or ``"none"``.

    ``op_id`` is the ``METHOD:/path`` form ingested rows carry; the
    helper strips the verb and matches the path against
    :data:`SDDC_PATH_RULES` in order.

    Returns ``"none"`` for paths outside the curated families (e.g.
    ``/v1/vcenters``, ``/v1/nsxt-clusters``); operators reviewing
    the ingested connector see those rows as unassigned and can either
    tighten the rules in this module on the next release or accept them
    as un-curated. The G0.7 canary's ``operations_unassigned < 50%`` bar
    tolerates a long tail of un-curated paths.
    """
    try:
        _, path = op_id.split(":", 1)
    except ValueError:
        return "none"
    for prefix, group_key in SDDC_PATH_RULES:
        if path.startswith(prefix):
            return group_key
    return "none"


#: Operator-reviewed ``when_to_use`` hints for the 4 SDDC Manager groups
#: the browse-breadth curated core spans (the audited operational groups
#: moved to typed ops in #2306). Each group carries exactly one op. Every
#: hint is one complete sentence the agent reads verbatim — vague hints
#: poison ``search_operations`` ranking, per the ai_engineering pack.
SDDC_CORE_GROUPS: Final[tuple[SddcCoreGroup, ...]] = (
    SddcCoreGroup(
        group_key="sddc-releases",
        name="SDDC Manager (release)",
        when_to_use=(
            "Use this group to read the SDDC Manager's own software release — "
            "VCF version, build date, and component BOM. The probe surface the "
            "agent calls before any heavier inventory read or when confirming "
            "the VCF build under management."
        ),
    ),
    SddcCoreGroup(
        group_key="sddc-domains",
        name="VCF Domains",
        when_to_use=(
            "Use this group to inspect a single VCF domain in detail — its "
            "associated vCenter(s), NSX-T cluster, clusters, and hosts. The "
            "domain listing itself is the typed sddc.domain.list op; this "
            "group's curated read is the per-domain detail."
        ),
    ),
    SddcCoreGroup(
        group_key="sddc-network-pools",
        name="VCF Network Pools",
        when_to_use=(
            "Use this group to list network pools configured in SDDC Manager. "
            "Network pools define the IP ranges and VLANs provisioned to new "
            "ESXi hosts during domain expansion or host commission operations."
        ),
    ),
    SddcCoreGroup(
        group_key="sddc-bundles",
        name="VCF LCM Bundles",
        when_to_use=(
            "Use this group to list LCM bundles available on this SDDC Manager — "
            "VCF update packages, component updates, and async patches. Read-only; "
            "use when answering 'which updates are available' or 'is the VCF stack "
            "compliant with the latest release'."
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

    Same three-field shape :mod:`meho_backplane.connectors.nsx.core_ops` and
    :mod:`meho_backplane.connectors.bind9.ops_zone` use so an agent crossing
    connector boundaries sees a stable convention.
    """
    return {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
    }


#: The 4 curated read-only SDDC Manager core ops. Each entry carries the
#: op_id (``GET:/path`` form), the curated group assignment, and the
#: operator-reviewed ``llm_instructions`` blob. The audited operational
#: reads moved to typed ops in #2306
#: (:mod:`meho_backplane.connectors.sddc_manager.typed_ops`); these four
#: keep the wider ingested breadth browsable.
#:
#: Paths sourced against VCF / SDDC Manager API 9.0 at
#: https://developer.broadcom.com/xapis/vmware-cloud-foundation-api/latest/.
SDDC_CORE_OPS: Final[tuple[SddcCoreOp, ...]] = (
    SddcCoreOp(
        op_id="GET:/v1/releases/system",
        group_key="sddc-releases",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the SDDC Manager's own software release — VCF "
                "version string, release date, and the component BOM listing "
                "every bundled product's version. Useful as a pre-flight probe "
                "before heavier inventory reads or to confirm the VCF build "
                "under management."
            ),
            output_shape=(
                "Object with version, releaseDate, description, and bom[] "
                "(each entry carries componentType and componentVersion)."
            ),
            next_step=(
                "If the release looks current, proceed to the typed domain / "
                "cluster reads; if behind, surface the version to the operator "
                "for an LCM update check via sddc.bundle.list."
            ),
        ),
    ),
    SddcCoreOp(
        op_id="GET:/v1/domains/{id}",
        group_key="sddc-domains",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the full detail of one VCF domain — its associated "
                "vCenter(s), NSX-T cluster (with VIP FQDN), cluster list, and host "
                "references. Requires a domain id from sddc.domain.list; use after "
                "listing domains to answer 'which vCenter manages domain X' or "
                "'what NSX-T cluster backs this workload domain'."
            ),
            output_shape=(
                "Domain object with vcenters[] (id, fqdn), nsxtCluster (id, "
                "vipFqdn), clusters[] (id, name), and ssoId/ssoName fields."
            ),
            next_step=(
                "Cross-reference vcenters[].fqdn against the vSphere connector "
                "for VM reads, or use cluster ids for targeted sddc.cluster.list "
                "or sddc.host.list queries."
            ),
        ),
    ),
    SddcCoreOp(
        op_id="GET:/v1/network-pools",
        group_key="sddc-network-pools",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list network pools configured in SDDC Manager. Network "
                "pools define the IP ranges, VLANs, and subnets provisioned to "
                "new ESXi hosts during domain expansion or host commission. Use "
                "when answering 'what network pool will be used for the next "
                "host commission' or verifying VLAN assignments."
            ),
            output_shape=(
                "Paginated envelope with elements[] and pageMetadata; each pool "
                "carries id, name, and networks[] (each network has type, vlanId, "
                "subnet, mask, gateway, and ipPools[] with start/end ranges)."
            ),
            next_step=(
                "Cross-reference a pool's id against cluster or host records when "
                "answering 'which network pool covers this host' or confirming "
                "pre-commission networking requirements."
            ),
        ),
    ),
    SddcCoreOp(
        op_id="GET:/v1/bundles",
        group_key="sddc-bundles",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list LCM bundles available on this SDDC Manager — VCF "
                "update packages, component patches, and async updates. Read-only; "
                "use when answering 'which updates are available', 'is the VCF "
                "stack compliant with the latest release', or 'what version does "
                "bundle X contain'."
            ),
            output_shape=(
                "Array of bundle objects; each carries id, type "
                "(VMWARE_SOFTWARE), version, description, sizeMB, "
                "downloadStatus, isCumulative, isCompliant, "
                "applicabilityStatus, and components[] (component name + "
                "version pairs)."
            ),
            next_step=(
                "Surface bundle isCompliant and applicabilityStatus to the "
                "operator; cross-reference the VCF release version from "
                "sddc.about to identify applicable update bundles."
            ),
        ),
    ),
)


async def apply_sddc_core_curation(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
) -> None:
    """Apply the curated 4-op read core against an ingested SDDC Manager connector.

    Drives the substrate so that, after this call returns, exactly
    the 4 ops in :data:`SDDC_CORE_OPS` are dispatchable
    (``is_enabled=True``) and every other ingested op stays
    ``is_enabled=False``. The 4 curated groups land
    ``review_status='enabled'`` so the agent's
    :func:`~meho_backplane.operations.meta_tools.search_operations`
    surfaces the core ops; non-curated groups are left untouched
    (``review_status='staged'`` from the G0.7 ingest default).

    The substrate doesn't expose "enable only ops X, Y, Z under group G":
    :meth:`ReviewService.enable_group`'s cascade flips ``is_enabled=True``
    on every child op. The helper works around this via the
    audit-log-driven operator-override exclusion: (1) ``get_review_payload``
    loads each curated group's ops; (2) every non-core op in a curated group
    gets an ``edit_op(is_enabled=False)`` override row the ``enable_group``
    cascade then skips; (3) ``edit_group`` lands the reviewed name +
    when_to_use; (4) ``enable_group`` flips ``review_status='enabled'`` and
    cascades ``is_enabled=True`` to the curated ops; (5) ``edit_op`` lands
    each curated ``llm_instructions`` blob. Same pattern as
    :func:`~meho_backplane.connectors.nsx.core_ops.apply_nsx_core_curation`.

    Re-running is safe but not idempotent at the audit layer:
    ``enable_group`` short-circuits on already-enabled groups, but
    ``edit_group`` / ``edit_op`` always emit one audit row per call.

    Raises :class:`~meho_backplane.operations.ingest.ConnectorNotFoundError`
    if no groups exist for ``sddc-rest-9.0`` under *tenant_id* (the
    operator must run ``meho connector ingest`` against the VCF spec
    before this helper applies).
    """
    payload = await review_service.get_review_payload(
        SDDC_CONNECTOR_ID,
        tenant_id,
    )

    core_op_ids_by_group: dict[str, set[str]] = {}
    for op in SDDC_CORE_OPS:
        core_op_ids_by_group.setdefault(op.group_key, set()).add(op.op_id)

    for group_payload in payload.groups:
        allow_list = core_op_ids_by_group.get(group_payload.group_key)
        if allow_list is None:
            continue
        for review_op in group_payload.ops:
            if review_op.op_id in allow_list:
                continue
            await review_service.edit_op(
                SDDC_CONNECTOR_ID,
                review_op.op_id,
                tenant_id=tenant_id,
                is_enabled=False,
            )
            _log.info(
                "sddc_non_core_op_disabled",
                connector_id=SDDC_CONNECTOR_ID,
                op_id=review_op.op_id,
                group_key=group_payload.group_key,
            )

    for group in SDDC_CORE_GROUPS:
        await review_service.edit_group(
            SDDC_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
            name=group.name,
            when_to_use=group.when_to_use,
        )
        await review_service.enable_group(
            SDDC_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
        )
        _log.info(
            "sddc_core_group_enabled",
            connector_id=SDDC_CONNECTOR_ID,
            group_key=group.group_key,
        )

    for op in SDDC_CORE_OPS:
        await review_service.edit_op(
            SDDC_CONNECTOR_ID,
            op.op_id,
            tenant_id=tenant_id,
            llm_instructions=op.llm_instructions,
        )
        _log.info(
            "sddc_core_op_curated",
            connector_id=SDDC_CONNECTOR_ID,
            op_id=op.op_id,
        )
