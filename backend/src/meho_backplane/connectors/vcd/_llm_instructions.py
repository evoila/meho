# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-op ``llm_instructions`` blobs for the ``vcd`` typed read core.

Split out of :mod:`~meho_backplane.connectors.vcd.typed_reads` (which holds the
op bodies) to keep each module focused, the ``fleet-lcm`` #3047 layout. Consumed
by :mod:`meho_backplane.connectors.vcd.typed_ops`, which keys each op's metadata
row to :data:`VCD_READ_LLM_INSTRUCTIONS`.

The canonical three-key shape (``when_to_call`` / ``output_shape`` /
``next_step``) is the same convention the harbor / nsx / fleet-lcm read cores
use, so an agent crossing connector boundaries sees a stable structure. The
prose references the dot-form op ids the reads register under and the sibling
ops an agent drills to next. The framing is migration inventory: these reads
enumerate a *source* vCD estate an operator is bringing onto the platform.
"""

from __future__ import annotations

__all__ = ["VCD_READ_LLM_INSTRUCTIONS"]


_ORG_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call first when inventorying a VMware Cloud Director source estate — "
        "the organizations are the top of the vCD tenancy tree. A provider "
        "(System) session sees every org. Use to answer 'which tenants live on "
        "this vCD' before drilling into their VDCs, vApps, and VMs."
    ),
    "output_shape": (
        "Modern cloudapi paged envelope {resultTotal, pageCount, page, "
        "pageSize, values: [Org, ...]}. Each Org carries id (a "
        "urn:vcloud:org: URN), name, displayName, and isEnabled."
    ),
    "next_step": (
        "Cross-reference an org's name against vcd.vdc.list (orgName) to see its "
        "compute allocations, or vcd.catalog.list to see its templates. vApps "
        "and VMs are reached the same way via vcd.vapp.list / vcd.vm.list."
    ),
}

_VDC_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the organization VDCs — the per-org compute/storage "
        "allocations backed by a provider VDC. The unit of capacity a migration "
        "must re-home. Use to answer 'how much is allocated where' or to map "
        "orgs to their backing provider VDCs."
    ),
    "output_shape": (
        "Query-service record envelope {record: [AdminOrgVdcRecord, ...], total, "
        "page, pageSize}. Each record carries name, orgName, providerVdcName, "
        "numberOfVApps / numberOfVMs, and CPU/memory/storage allocation figures."
    ),
    "next_step": (
        "Filter vcd.vapp.list / vcd.vm.list by vdcName to enumerate the "
        "workloads in a given VDC, or group by providerVdcName to size the "
        "underlying vSphere resource the migration lands on."
    ),
}

_VAPP_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the vApps — the deployable multi-VM units vCD groups "
        "workloads into. Use when planning migration waves (a vApp is a natural "
        "move-together boundary) or to find which vApp contains a given VM."
    ),
    "output_shape": (
        "Query-service record envelope {record: [AdminVAppRecord, ...], total, "
        "page, pageSize}. Each record carries name, orgName, vdcName, status "
        "(e.g. POWERED_ON / POWERED_OFF), numberOfVMs, and isDeployed. A large "
        "list is reduced to a JSONFlux handle."
    ),
    "next_step": (
        "Drill into a vApp's VMs by filtering vcd.vm.list on containerName (the "
        "vApp name). Correlate status against vcd.task.list to see recent "
        "power/deploy operations."
    ),
}

_VM_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the virtual machines across the estate — the core "
        "migration-workload inventory. This is usually the highest-value read: "
        "the count, sizing, and guest OS of every VM the migration will move. A "
        "provider session sees every org's VMs."
    ),
    "output_shape": (
        "Query-service record envelope {record: [AdminVMRecord, ...], total, "
        "page, pageSize}. Each record carries name, containerName (the vApp), "
        "vdc, org, status, numberOfCpus, memoryMB, and guestOs. A large "
        "inventory is reduced to a JSONFlux handle — narrow it with result_query "
        "/ result_aggregate (e.g. sum memoryMB by org)."
    ),
    "next_step": (
        "Aggregate over the handle for capacity planning (count / vCPU / memory "
        "by org or vdc), or group by guestOs to flag OSes needing special "
        "migration handling. Map containerName back to vcd.vapp.list for the "
        "move-together boundary."
    ),
}

_CATALOG_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the catalogs — the vApp-template and media (ISO) libraries "
        "each org publishes. Use to inventory the templates a migration must "
        "recreate on the target, and to spot shared/published catalogs that "
        "cross org boundaries."
    ),
    "output_shape": (
        "Query-service record envelope {record: [AdminCatalogRecord, ...], "
        "total, page, pageSize}. Each record carries name, orgName, isShared / "
        "isPublished, and numberOfVAppTemplates / numberOfMedia."
    ),
    "next_step": (
        "Flag isPublished / isShared catalogs as cross-tenant dependencies for "
        "the migration plan. Correlate an org's catalogs with its vApps "
        "(vcd.vapp.list) to see which templates are in active use."
    ),
}

_EDGE_GATEWAY_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the edge gateways — the org-VDC network egress + services "
        "(NAT, firewall, VPN, load-balancing) boundary. The networking surface a "
        "migration must re-home. Use to answer 'what network edges exist and "
        "which VDCs do they serve'."
    ),
    "output_shape": (
        "Query-service record envelope {record: [EdgeGatewayRecord, ...], total, "
        "page, pageSize}. Each record carries name, vdc, orgName, "
        "numberOfExtNetworks, and numberOfOrgNetworks."
    ),
    "next_step": (
        "Map each edge gateway's vdc back to vcd.vdc.list to associate network "
        "boundaries with compute allocations, so the target-side network design "
        "covers every VDC's egress."
    ),
}

_TASK_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list recent asynchronous tasks — 'what is vCD doing / did it "
        "just do'. Use to confirm the estate is quiescent before a migration "
        "cutover, or to find the task behind a resource in an unexpected state."
    ),
    "output_shape": (
        "Query-service record envelope {record: [AdminTaskRecord, ...], total, page, "
        "pageSize}. Each record carries name (the operation), status (running / "
        "success / error), objectName (the affected entity), orgName, startDate "
        "/ endDate, and ownerName. A large list is reduced to a JSONFlux handle."
    ),
    "next_step": (
        "Filter the handle to status=error to surface recent failures, or to "
        "status=running to confirm no operation is in flight before a cutover. "
        "Correlate objectName with the matching inventory read."
    ),
}


#: Per-op ``llm_instructions`` keyed by the dot-form op id, consumed by
#: :mod:`meho_backplane.connectors.vcd.typed_ops`.
VCD_READ_LLM_INSTRUCTIONS: dict[str, dict[str, object]] = {
    "vcd.org.list": _ORG_LIST_INSTRUCTIONS,
    "vcd.vdc.list": _VDC_LIST_INSTRUCTIONS,
    "vcd.vapp.list": _VAPP_LIST_INSTRUCTIONS,
    "vcd.vm.list": _VM_LIST_INSTRUCTIONS,
    "vcd.catalog.list": _CATALOG_LIST_INSTRUCTIONS,
    "vcd.edge-gateway.list": _EDGE_GATEWAY_LIST_INSTRUCTIONS,
    "vcd.task.list": _TASK_LIST_INSTRUCTIONS,
}
