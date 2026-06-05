# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""NSX read-only v0.2 core — curated operator-enabled subset.

This module names the **9 read-only NSX operations** the G3.5 NSX
v0.2 ship enables out of the much larger ``policy.yaml`` +
``manager.yaml`` corpus that the G0.7 spec-ingestion pipeline lands
under the NSX connector triple. NSX-T 4.x was renumbered onto the
VCF train at VCF 9.0 (#1530), so :data:`NSX_VERSION` tracks the
VCF-9-aligned ``"9.0"`` line and :data:`NSX_CONNECTOR_ID` is the
default ``"nsx-rest-9.0"`` slug; :func:`apply_nsx_core_curation`
accepts a ``connector_id`` override so an operator who ingested
under a finer 9.x label (e.g. ``"nsx-rest-9.1.0.0"``) can still
curate the ops the ingest actually landed. The curation is
two-layered:

* :data:`NSX_CORE_GROUPS` — the operator-reviewed ``when_to_use``
  hint per LLM-grouping pass output group. Each entry's
  ``group_key`` is the deterministic slug the path-prefix classifier
  assigns to NSX ops (see :func:`classify_nsx_op` below); the
  ``when_to_use`` is what the agent reads verbatim through
  :func:`~meho_backplane.operations.meta_tools.list_operation_groups`
  to pick a group to search within.
* :data:`NSX_CORE_OPS` — the 9 ``EndpointDescriptor.op_id`` strings
  that flip to ``is_enabled=True`` at operator-review time, paired
  with the per-op ``llm_instructions`` blob the agent inlines into
  the reasoning context when it sees the op in
  :func:`~meho_backplane.operations.meta_tools.search_operations`
  hits. Every other op under the same connector triple stays
  ``is_enabled=False`` (the G0.7 ingestion default for
  ``source_kind='ingested'`` rows).

Per Initiative #368 and CLAUDE.md postulates 1-2, NSX is **fully
generic-ingested**: the underlying ops are not registered in code,
they live in the ``endpoint_descriptor`` table. This module only
carries the **operator-review metadata** the substrate uses at the
review step — the actual curation is applied through
:func:`apply_nsx_core_curation` against an existing ingested
connector.

The 9 ops (paths cross-checked against the NSX REST API guide,
2026-04 snapshot at https://developer.broadcom.com/xapis/nsx-data-center-rest-api/latest/;
the manager (``/api/v1/...``) and policy (``/policy/api/v1/...``)
path families are stable across the 4.x and VCF-9-aligned 9.x lines):

1. ``GET:/api/v1/node`` — ``nsx.about`` — manager version / build /
   node UUID (the same surface :meth:`NsxConnector.fingerprint`
   already consumes; exposing it as an operator-callable op lets
   the agent run a sanity probe before any heavier read).
2. ``GET:/api/v1/transport-nodes`` — ``nsx.node.list`` — list of
   transport nodes (ESXi / edge) the NSX manager is aware of.
3. ``GET:/api/v1/cluster/status`` — ``nsx.cluster.status`` —
   manager cluster mode + node membership; mirrors the connector's
   probe target.
4. ``GET:/policy/api/v1/infra/segments`` — ``nsx.segment.list`` —
   policy-API segments (logical-port + DVS-backed portgroup
   surface).
5. ``GET:/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones``
   — ``nsx.transport_zone.list`` — transport-zone inventory under
   the default enforcement point.
6. ``GET:/policy/api/v1/infra/tier-0s`` — ``nsx.tier0.list`` —
   policy-API tier-0 gateway inventory (BGP / NAT summaries via
   nested ``children`` field when present).
7. ``GET:/policy/api/v1/infra/tier-1s`` — ``nsx.tier1.list`` —
   tier-1 gateway inventory (the per-tenant routing surface).
8. ``GET:/policy/api/v1/infra/domains/{domain-id}/security-policies``
   — ``nsx.firewall.policy.list`` — distributed-firewall policy
   listing scoped by domain (``--scope <domain>`` in the CLI verb,
   default ``default``).
9. ``GET:/policy/api/v1/infra/domains/{domain-id}/security-policies/{security-policy-id}/rules``
   — ``nsx.firewall.rule.list`` — per-policy rule listing; the
   final-grain firewall inspection surface.

Path families and group_keys
----------------------------

NSX surfaces split cleanly into the **manager API** (under
``/api/v1/...``) and the **policy API** (under ``/policy/api/v1/...``).
LLM-grouping output for NSX tends to produce one group per top-level
family — :data:`NSX_PATH_RULES` mirrors that taxonomy so an operator
reviewing the connector sees the read-core ops fall into the
expected groups (``manager-node``, ``manager-cluster``,
``policy-segments``, ``policy-transport-zones``, ``policy-tier0``,
``policy-tier1``, ``policy-firewall``). The path-prefix classifier
is the same shape :class:`_PathPrefixStubLlmClient` in
``test_g07_vsphere_canary.py`` uses for vSphere — stable, deterministic,
operator-reviewable.

Curation application
--------------------

:func:`apply_nsx_core_curation` is the operator-review-time
substrate call that makes exactly the 9 curated ops dispatchable
and leaves every other ingested op disabled. The substrate has no
"enable only ops X, Y, Z under group G" verb —
:meth:`ReviewService.enable_group` cascades ``is_enabled=True`` to
every child op. The helper threads this needle by using the
audit-log-driven operator-override exclusion (see
:func:`~meho_backplane.operations.ingest._internals.operator_disabled_op_ids`):

1. Read the current state of each curated group via
   :meth:`ReviewService.get_review_payload`.
2. For every non-curated op in a curated group, call
   :meth:`ReviewService.edit_op` with ``is_enabled=False`` —
   this writes the operator-override audit row.
3. For each curated group, :meth:`edit_group` lands the
   operator-reviewed ``name`` / ``when_to_use``, then
   :meth:`enable_group` flips ``review_status='enabled'``. The
   cascade detects the prior override rows from step 2 and skips
   those ops; only the curated ops get ``is_enabled=True``.
4. For each entry in :data:`NSX_CORE_OPS`, :meth:`edit_op`
   lands the curated ``llm_instructions`` blob.

Re-running :func:`apply_nsx_core_curation` is **safe** but not
fully idempotent at the audit layer: :meth:`enable_group` is a
no-op on a group already in ``review_status='enabled'`` (no audit
row written), but :meth:`edit_group` and :meth:`edit_op` always
emit one audit row per call — even when every field already
carried the incoming value. The intended posture is a one-shot
curation step after ingest; re-runs during a rollout or test
rerun produce redundant ``meho.connector.edit_*`` audit rows but
never corrupt state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog

from meho_backplane.operations.ingest.payload import ConnectorReviewPayload
from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "NSX_CONNECTOR_ID",
    "NSX_CORE_GROUPS",
    "NSX_CORE_OPS",
    "NSX_IMPL_ID",
    "NSX_PATH_RULES",
    "NSX_PRODUCT",
    "NSX_VERSION",
    "NsxCoreGroup",
    "NsxCoreOp",
    "apply_nsx_core_curation",
    "classify_nsx_op",
]

_log = structlog.get_logger(__name__)

#: Connector triple. Matches :class:`NsxConnector.product` /
#: ``version`` / ``impl_id``; lifted here so review-time callers
#: don't import the connector class (avoids re-running the registry
#: side-effects in the ``__init__``).
NSX_PRODUCT: Final[str] = "nsx"
NSX_VERSION: Final[str] = "9.0"
NSX_IMPL_ID: Final[str] = "nsx-rest"

#: Connector-id slug the G0.6 dispatcher's ``parse_connector_id``
#: round-trips back to the triple above: ``"nsx-rest-9.0"``. This is
#: the *default* curation target; :func:`apply_nsx_core_curation`
#: takes a ``connector_id`` override for ingests landed under a
#: finer 9.x label.
NSX_CONNECTOR_ID: Final[str] = f"{NSX_IMPL_ID}-{NSX_VERSION}"


@dataclass(frozen=True, slots=True)
class NsxCoreGroup:
    """One curated operator-review entry for an NSX operation group.

    ``group_key`` is the slug the path-prefix classifier emits (see
    :data:`NSX_PATH_RULES`). ``name`` is the operator-readable label
    ``meho connector review`` renders. ``when_to_use`` is the
    agent-facing hint :func:`list_operation_groups` returns verbatim;
    every entry is a single complete sentence so the agent's
    group-selection step has unambiguous guidance.
    """

    group_key: str
    name: str
    when_to_use: str


@dataclass(frozen=True, slots=True)
class NsxCoreOp:
    """One curated operator-review entry for an NSX operation.

    ``op_id`` follows the ``METHOD:path`` shape every
    ``source_kind='ingested'`` row uses; the path matches an entry
    in NSX's ``policy.yaml`` (``/policy/api/v1/...``) or
    ``manager.yaml`` (``/api/v1/...``).

    ``llm_instructions`` is the per-op JSON blob the meta-tools
    inline verbatim when the op surfaces. The shape (``when_to_call``
    / ``output_shape`` / ``next_step``) mirrors the typed-connector
    convention from :mod:`meho_backplane.connectors.bind9.ops_zone`
    and :mod:`meho_backplane.connectors.vault.ops` — same agent reads
    both surfaces, so the structure stays uniform.
    """

    op_id: str
    group_key: str
    llm_instructions: dict[str, object]


#: Path-prefix → group_key classifier rules for NSX.
#:
#: First match wins. Order matters: the more specific prefixes are
#: listed first so e.g. ``/policy/api/v1/infra/tier-0s`` doesn't fall
#: into a broader ``/policy/api/v1/infra/`` catch-all rule (no such
#: rule exists today; the ordering is defensive against future
#: additions). The NSX manager surface (``/api/v1/...``) and the
#: policy surface (``/policy/api/v1/...``) never overlap because the
#: ``/policy/`` prefix is unique to the policy API.
NSX_PATH_RULES: Final[tuple[tuple[str, str], ...]] = (
    # Policy-API surfaces — the modern, declarative NSX path family.
    ("/policy/api/v1/infra/segments", "policy-segments"),
    (
        "/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones",
        "policy-transport-zones",
    ),
    ("/policy/api/v1/infra/tier-0s", "policy-tier0"),
    ("/policy/api/v1/infra/tier-1s", "policy-tier1"),
    ("/policy/api/v1/infra/domains/", "policy-firewall"),
    # Manager-API surfaces — the legacy / lower-level NSX path family
    # the policy API delegates to. The core read set uses two
    # specific manager endpoints; everything else under /api/v1/
    # falls through to ``manager-misc``.
    ("/api/v1/cluster/status", "manager-cluster"),
    ("/api/v1/transport-nodes", "manager-transport-nodes"),
    ("/api/v1/node", "manager-node"),
)


def classify_nsx_op(op_id: str) -> str:
    """Return the curated ``group_key`` for an NSX op_id, or ``"none"``.

    ``op_id`` is the ``METHOD:/path`` form ingested rows carry;
    the helper strips the verb (NSX read ops are uniformly ``GET``)
    and matches the path against :data:`NSX_PATH_RULES` in order.

    Returns ``"none"`` for paths outside the curated families
    (manager-misc / policy-misc); operators reviewing the ingested
    connector see those rows as unassigned in the review payload
    and can either tighten the rules in this module on the next
    release or accept them as un-curated. The G0.7 canary's
    ``operations_unassigned < 50%`` bar tolerates a long tail of
    un-curated paths.
    """
    try:
        _, path = op_id.split(":", 1)
    except ValueError:
        return "none"
    for prefix, group_key in NSX_PATH_RULES:
        if path.startswith(prefix):
            return group_key
    return "none"


#: Operator-reviewed ``when_to_use`` hints for the seven NSX groups
#: the read-only v0.2 core spans. Two groups carry one op each
#: (``manager-node`` for nsx.about, ``manager-cluster`` for
#: nsx.cluster.status); the rest carry one or two. Every hint is
#: one complete sentence the agent reads verbatim — vague hints
#: poison ``search_operations`` ranking, per the ai_engineering pack.
NSX_CORE_GROUPS: Final[tuple[NsxCoreGroup, ...]] = (
    NsxCoreGroup(
        group_key="manager-node",
        name="NSX Manager (node)",
        when_to_use=(
            "Use this group to read the NSX Manager's own identity — "
            "build, version, node UUID, hostname. The probe / sanity "
            "surface the agent calls before any heavier inventory "
            "read."
        ),
    ),
    NsxCoreGroup(
        group_key="manager-cluster",
        name="NSX Manager (cluster)",
        when_to_use=(
            "Use this group to check whether the NSX management plane "
            "is healthy — cluster mode (1-node / 3-node), per-member "
            "status, control-plane availability."
        ),
    ),
    NsxCoreGroup(
        group_key="manager-transport-nodes",
        name="NSX Transport Nodes",
        when_to_use=(
            "Use this group to enumerate the transport nodes (ESXi "
            "hypervisors and edge nodes) NSX has prepared for "
            "overlay / VLAN traffic."
        ),
    ),
    NsxCoreGroup(
        group_key="policy-segments",
        name="NSX Segments",
        when_to_use=(
            "Use this group to list NSX logical segments and DVS-backed "
            "portgroups under the policy API. The primary read surface "
            "for 'what virtual networks exist' questions."
        ),
    ),
    NsxCoreGroup(
        group_key="policy-transport-zones",
        name="NSX Transport Zones",
        when_to_use=(
            "Use this group to list the transport zones segments and "
            "tier-routers attach to. Read-only inventory under the "
            "default enforcement point."
        ),
    ),
    NsxCoreGroup(
        group_key="policy-tier0",
        name="NSX Tier-0 Gateways",
        when_to_use=(
            "Use this group to inspect provider tier-0 gateways — "
            "north-south edge routing, BGP peers, NAT summaries."
        ),
    ),
    NsxCoreGroup(
        group_key="policy-tier1",
        name="NSX Tier-1 Gateways",
        when_to_use=(
            "Use this group to inspect per-tenant tier-1 gateways — "
            "the east-west routing surface attached under a tier-0."
        ),
    ),
    NsxCoreGroup(
        group_key="policy-firewall",
        name="NSX Distributed Firewall",
        when_to_use=(
            "Use this group to read distributed-firewall security "
            "policies and their per-policy rules. The agent asks "
            "'which rules govern traffic in domain X' questions here."
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

    Lifted to a helper so every op's blob has the same three
    fields in the same order — the agent's grammar checks read
    ``when_to_call`` / ``output_shape`` / ``next_step`` and a typo
    in any single op would silently degrade retrieval. The shape
    mirrors :mod:`meho_backplane.connectors.bind9.ops_zone` (typed
    bind9 ops) so an agent crossing connector boundaries sees a
    stable convention.
    """
    return {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
    }


#: The 9 curated read-only NSX core ops. Each entry carries the
#: op_id (``GET:/path`` form ingested ops use), the curated group
#: assignment, and the operator-reviewed ``llm_instructions`` blob.
#:
#: Sourced against the NSX REST API docs at
#: https://developer.broadcom.com/xapis/nsx-data-center-rest-api/latest/
#: (path families unchanged across 4.x and the VCF-9-aligned 9.x line).
NSX_CORE_OPS: Final[tuple[NsxCoreOp, ...]] = (
    NsxCoreOp(
        op_id="GET:/api/v1/node",
        group_key="manager-node",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to identify the NSX Manager — its build, version, "
                "node UUID, hostname. Useful as a probe before heavier "
                "policy reads, or to confirm which NSX cluster the "
                "target points at."
            ),
            output_shape=(
                "Object with node_version, kernel_version, node_uuid, hostname, external_id keys."
            ),
            next_step=(
                "If the manager looks healthy, move on to cluster-status or transport-node listing."
            ),
        ),
    ),
    NsxCoreOp(
        op_id="GET:/api/v1/transport-nodes",
        group_key="manager-transport-nodes",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to enumerate transport nodes — ESXi hypervisors "
                "and edge nodes prepared for NSX overlay or VLAN "
                "traffic. Useful when answering 'which hosts has NSX "
                "claimed'."
            ),
            output_shape=(
                "Object with a `results` array of transport node "
                "entries; each entry carries id, display_name, "
                "node_deployment_info, and host_switch_spec."
            ),
            next_step=(
                "Drill into a specific node by id when you need its "
                "uplink configuration or tunnel state."
            ),
        ),
    ),
    NsxCoreOp(
        op_id="GET:/api/v1/cluster/status",
        group_key="manager-cluster",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to confirm whether the NSX management plane is "
                "healthy and which members make up the cluster. "
                "Useful when an operator suspects a control-plane "
                "outage."
            ),
            output_shape=(
                "Object with mgmt_cluster_status (overall health), "
                "control_cluster_status, and per-member detail."
            ),
            next_step=(
                "If unhealthy, surface the failing member's id; "
                "otherwise proceed to the inventory / policy reads."
            ),
        ),
    ),
    NsxCoreOp(
        op_id="GET:/policy/api/v1/infra/segments",
        group_key="policy-segments",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list NSX segments — both pure logical-port "
                "overlay segments and DVS-backed portgroups exposed "
                "via the policy API. The primary read for 'what "
                "virtual networks exist' questions."
            ),
            output_shape=(
                "Object with a `results` array; each segment carries "
                "id, display_name, transport_zone_path, "
                "subnets/gateway_addresses for overlay segments, and "
                "vlan_ids for VLAN-backed ones. Large NSX deployments "
                "may return hundreds of rows — drill into one segment "
                "by id rather than re-listing for follow-ups."
            ),
            next_step=(
                "Pick a segment by id for status / port reads, or "
                "cross-reference its transport_zone_path against the "
                "transport-zone listing."
            ),
        ),
    ),
    NsxCoreOp(
        op_id=("GET:/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones"),
        group_key="policy-transport-zones",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list transport zones under the default "
                "enforcement point — the scope segments and "
                "tier-gateways attach to. Read-only inventory."
            ),
            output_shape=(
                "Object with a `results` array of transport-zone "
                "entries; each carries id, display_name, tz_type "
                "(OVERLAY / VLAN), and host_switch_name."
            ),
            next_step=(
                "Cross-reference a zone's id when answering 'where "
                "does this segment live' or 'what zones does this "
                "edge see'."
            ),
        ),
    ),
    NsxCoreOp(
        op_id="GET:/policy/api/v1/infra/tier-0s",
        group_key="policy-tier0",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to inspect provider tier-0 gateways — the "
                "north-south edge routing surface. Includes BGP peers "
                "and NAT summaries via the nested `children` field "
                "when configured."
            ),
            output_shape=(
                "Object with a `results` array; each tier-0 carries "
                "id, display_name, ha_mode (ACTIVE_ACTIVE / "
                "ACTIVE_STANDBY), failover_mode, and optionally a "
                "`children` array with BgpRoutingConfig and Nat "
                "subtrees."
            ),
            next_step=(
                "Drill into a tier-0 by id for its locale-services / "
                "interfaces, or follow up with tier-1 listing for "
                "downstream consumers."
            ),
        ),
    ),
    NsxCoreOp(
        op_id="GET:/policy/api/v1/infra/tier-1s",
        group_key="policy-tier1",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to inspect per-tenant tier-1 gateways — the "
                "east-west routing surface attached under a tier-0. "
                "Useful when mapping which tier-1 fronts a given "
                "segment."
            ),
            output_shape=(
                "Object with a `results` array; each tier-1 carries "
                "id, display_name, tier0_path (its parent), "
                "route_advertisement_types, and ha_mode."
            ),
            next_step=(
                "Cross-reference tier0_path against the tier-0 "
                "listing, or drill into a tier-1 for its segment "
                "attachments."
            ),
        ),
    ),
    NsxCoreOp(
        op_id="GET:/policy/api/v1/infra/domains/{domain-id}/security-policies",
        group_key="policy-firewall",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list distributed-firewall security policies "
                "in a domain. The default domain is `default`; "
                "operators on multi-tenant NSX deployments may use "
                "tenant-specific domain ids."
            ),
            output_shape=(
                "Object with a `results` array of security-policy "
                "entries; each carries id, display_name, category "
                "(Ethernet / Emergency / Infrastructure / Environment "
                "/ Application), sequence_number, and scope."
            ),
            next_step=(
                "Pick a policy by id and call the rule listing op "
                "to inspect individual firewall rules."
            ),
        ),
    ),
    NsxCoreOp(
        op_id=(
            "GET:/policy/api/v1/infra/domains/{domain-id}/security-policies/"
            "{security-policy-id}/rules"
        ),
        group_key="policy-firewall",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to enumerate firewall rules inside one "
                "security-policy. The final-grain inspection surface "
                "when answering 'what rule allows / blocks traffic "
                "X'."
            ),
            output_shape=(
                "Object with a `results` array; each rule carries id, "
                "display_name, action (ALLOW / DROP / REJECT), "
                "sources, destinations, services, and "
                "applied_to. Large policies may return hundreds of "
                "rules — drill into one rule by id for follow-ups."
            ),
            next_step=(
                "Surface the rule action + sources/destinations to "
                "the operator; cross-reference applied_to against "
                "segment ids when context demands."
            ),
        ),
    ),
)


async def apply_nsx_core_curation(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
    connector_id: str = NSX_CONNECTOR_ID,
) -> None:
    """Apply the curated 9-op read core against an ingested NSX connector.

    Drives the substrate so that, after this call returns, exactly
    the 9 ops in :data:`NSX_CORE_OPS` are dispatchable
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
    exclusion (see
    :func:`~meho_backplane.operations.ingest._internals.operator_disabled_op_ids`):

    1. :meth:`ReviewService.get_review_payload` loads the current
       state of every curated group and its child ops.
    2. For each child op in a curated group that **isn't** in the
       :data:`NSX_CORE_OPS` allow-list,
       :meth:`ReviewService.edit_op` with ``is_enabled=False``
       writes the operator-override audit row. The follow-on
       :meth:`enable_group` cascade detects these rows and skips
       them, so non-core ops stay disabled even though their
       group is being enabled.
    3. :meth:`ReviewService.edit_group` lands the operator-reviewed
       ``name`` + ``when_to_use`` on each curated group.
    4. :meth:`ReviewService.enable_group` flips
       ``review_status='enabled'`` and cascades ``is_enabled=True``
       to the curated child ops (operator-overridden non-core ops
       are skipped).
    5. :meth:`ReviewService.edit_op` lands the curated
       ``llm_instructions`` blob per entry in :data:`NSX_CORE_OPS`.

    Caller's *review_service* must be constructed against a
    ``tenant_admin``-role operator (matching the substrate's auth
    contract); built-in scope (``tenant_id=None``) is the default
    for NSX content, same convention vSphere uses.

    *connector_id* defaults to :data:`NSX_CONNECTOR_ID`
    (``"nsx-rest-9.0"``) but accepts an override for the case the
    operator ingested under a finer 9.x label than the class pin
    — ingested ops land under the **operator-supplied** ``version``
    label (e.g. a VCF-9 appliance spec ingested as
    ``version="9.1.0.0"`` lands ``connector_id="nsx-rest-9.1.0.0"``),
    while :data:`NSX_CONNECTOR_ID` only mirrors the registered
    class pin. Pass the connector_id the ingest actually produced
    (#1530).

    Re-running is safe but not idempotent at the audit layer.
    :meth:`enable_group` short-circuits on groups already in
    ``review_status='enabled'`` (no audit row), but
    :meth:`edit_group` and :meth:`edit_op` always emit one audit
    row per call — even when the incoming value equals the
    persisted one. The intended posture is a one-shot curation
    step after ingest; re-runs produce redundant
    ``meho.connector.edit_*`` audit rows but never corrupt state.

    Raises :class:`~meho_backplane.operations.ingest.ConnectorNotFoundError`
    if no groups exist for *connector_id* under *tenant_id* (the
    operator must run ``meho connector ingest`` against the NSX
    specs before this helper applies).
    """
    # Step 1 — read the current state so we can compute the
    # per-group non-core op set.
    payload = await review_service.get_review_payload(connector_id, tenant_id)

    # Step 2 — disable non-core ops in each curated group so the
    # subsequent enable_group cascade skips them.
    await _disable_non_core_ops(
        review_service, payload, tenant_id=tenant_id, connector_id=connector_id
    )
    # Steps 3 + 4 — land the reviewed group metadata, then enable each
    # curated group (cascade respects the step-2 exclusion list).
    await _enable_curated_groups(review_service, tenant_id=tenant_id, connector_id=connector_id)
    # Step 5 — land the curated llm_instructions blob per core op.
    await _land_core_op_instructions(review_service, tenant_id=tenant_id, connector_id=connector_id)


def _core_op_ids_by_group() -> dict[str, set[str]]:
    """Map each curated ``group_key`` to its allow-list of core op_ids.

    ``policy-firewall`` carries two entries (security-policy.list +
    rule.list); every other curated group carries exactly one.
    """
    by_group: dict[str, set[str]] = {}
    for op in NSX_CORE_OPS:
        by_group.setdefault(op.group_key, set()).add(op.op_id)
    return by_group


async def _disable_non_core_ops(
    review_service: ReviewService,
    payload: ConnectorReviewPayload,
    *,
    tenant_id: UUID | None,
    connector_id: str,
) -> None:
    """Write an operator-override disable row for every non-core op.

    Walks each curated group's ops and disables the ones outside
    :data:`NSX_CORE_OPS` via the audit-log override path so the
    follow-on :meth:`ReviewService.enable_group` cascade skips them.
    The override row is always written — even when the op already
    appears ``is_enabled=False`` — because the cascade consults the
    audit log (see
    :func:`~meho_backplane.operations.ingest._internals.operator_disabled_op_ids`),
    not the live column value, and a freshly-ingested op carries no
    ``edit_op`` audit history.
    """
    allow_by_group = _core_op_ids_by_group()
    for group_payload in payload.groups:
        allow_list = allow_by_group.get(group_payload.group_key)
        if allow_list is None:
            # Non-curated group — left entirely alone; its
            # review_status stays at whatever the ingest pass set it
            # to (typically 'staged').
            continue
        for review_op in group_payload.ops:
            if review_op.op_id in allow_list:
                continue
            await review_service.edit_op(
                connector_id,
                review_op.op_id,
                tenant_id=tenant_id,
                is_enabled=False,
            )
            _log.info(
                "nsx_non_core_op_disabled",
                connector_id=connector_id,
                op_id=review_op.op_id,
                group_key=group_payload.group_key,
            )


async def _enable_curated_groups(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
    connector_id: str,
) -> None:
    """Land the reviewed group metadata + enable each curated group."""
    for group in NSX_CORE_GROUPS:
        await review_service.edit_group(
            connector_id,
            group.group_key,
            tenant_id=tenant_id,
            name=group.name,
            when_to_use=group.when_to_use,
        )
        await review_service.enable_group(
            connector_id,
            group.group_key,
            tenant_id=tenant_id,
        )
        _log.info(
            "nsx_core_group_enabled",
            connector_id=connector_id,
            group_key=group.group_key,
        )


async def _land_core_op_instructions(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
    connector_id: str,
) -> None:
    """Land the curated ``llm_instructions`` blob on each core op."""
    for op in NSX_CORE_OPS:
        await review_service.edit_op(
            connector_id,
            op.op_id,
            tenant_id=tenant_id,
            llm_instructions=op.llm_instructions,
        )
        _log.info(
            "nsx_core_op_curated",
            connector_id=connector_id,
            op_id=op.op_id,
        )
