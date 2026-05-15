# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``register_vmware_composite_operations`` -- registrar for the 5 read composites.

Module-level async function called from the lifespan-driven
:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
after the registrar list is populated by the
``meho_backplane.connectors.vmware_rest.composites`` package's
``__init__`` (which appends this function via
:func:`register_typed_op_registrar`).

Per-composite arguments (summary / description / group_key / tags /
``parameter_schema``) live here so a future shape change (e.g.
``llm_instructions`` polish) only touches one file. The
:func:`~meho_backplane.operations.typed_register.register_composite_operation`
helper handles the upsert, body-hash dedupe, embedding pipeline, and
the source_kind="composite" persistence.

Every composite passes ``safety_level="safe"`` +
``requires_approval=False`` -- overrides of T4's
``dangerous`` / ``True`` defaults (which target the typical write
composite). All 5 composites in this Task are inherently read-only;
operator-facing dispatch should not pop the approval queue for them.
"""

from __future__ import annotations

from meho_backplane.connectors.vmware_rest.composites._read import (
    cluster_drs_recommendations_composite,
    datastore_usage_composite,
    event_tail_composite,
    network_portgroup_audit_composite,
    performance_summary_composite,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA,
    DATASTORE_USAGE_PARAMETER_SCHEMA,
    EVENT_TAIL_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA,
    PERFORMANCE_SUMMARY_PARAMETER_SCHEMA,
)
from meho_backplane.operations.typed_register import register_composite_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = ["register_vmware_composite_operations"]


# Natural-key shorthand. Every composite registers against
# ``(product="vmware", version="9.0", impl_id="vmware-rest")`` -- the
# same triple :class:`VmwareRestConnector` advertises -- so the
# dispatcher's ``connector_id="vmware-rest-9.0"`` lookup resolves
# every read composite alongside the ~3,470 ingested ops.
_PRODUCT = "vmware"
_VERSION = "9.0"
_IMPL_ID = "vmware-rest"


async def register_vmware_composite_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every vmware-rest read composite into ``endpoint_descriptor``.

    Idempotent: a second invocation against unchanged descriptions is a
    no-op for the embedding pipeline (the body-hash skip path in
    :func:`_register_in_session`). The runner
    (:func:`run_typed_op_registrars`) calls every registered registrar
    on every lifespan startup; the skip-re-embed branch keeps that
    cheap.

    Scope: 5 read composites (cluster.drs_recommendations + event.tail
    + performance.summary + datastore.usage + network.portgroup.audit).
    The 8 write composites are scope of #509 (G3.1-T6).

    Test seam: ``embedding_service`` lets test fixtures inject a stub
    so unit tests don't load the ONNX model. Production callers leave
    it ``None`` and each registration resolves the process-wide
    singleton.
    """
    await register_composite_operation(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id="vmware.composite.cluster.drs_recommendations",
        handler=cluster_drs_recommendations_composite,
        summary="Read DRS state + active recommendations for a cluster.",
        description=(
            "Orchestrates a cluster summary read plus a DRS-config read, "
            "returning a single aggregated payload. Equivalent of "
            "'govc cluster.recommendations' for the operator-facing "
            "workflow: one composite call replaces two raw vCenter REST "
            "GETs while preserving the audit-tree linkage between the "
            "parent composite row and each sub-op row. Read-only -- "
            "never mutates cluster state."
        ),
        parameter_schema=CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA,
        group_key="cluster",
        tags=["composite", "read-only", "cluster", "drs"],
        safety_level="safe",
        requires_approval=False,
        embedding_service=embedding_service,
    )

    await register_composite_operation(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id="vmware.composite.event.tail",
        handler=event_tail_composite,
        summary="Tail recent vCenter events via EventManager.QueryEvents.",
        description=(
            "Calls EventManager.QueryEvents (vi-json) against the "
            "EventManager singleton, optionally narrowed by a per-call "
            "moId override, and caps the returned array client-side. "
            "Equivalent of 'govc events' for the operator-facing "
            "workflow. Read-only -- never mutates the event store."
        ),
        parameter_schema=EVENT_TAIL_PARAMETER_SCHEMA,
        group_key="events",
        tags=["composite", "read-only", "events", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        embedding_service=embedding_service,
    )

    await register_composite_operation(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id="vmware.composite.performance.summary",
        handler=performance_summary_composite,
        summary="Summarise performance metrics for one entity via PerformanceManager.",
        description=(
            "Discovers available counters for the target entity via "
            "PerformanceManager.QueryAvailablePerfMetric, then fetches "
            "sample values via PerformanceManager.QueryPerf (both "
            "vi-json). Returns the available-counter list plus the "
            "capped sample list; the caller can post-filter to whichever "
            "metric they need. Read-only -- never mutates counter "
            "configuration."
        ),
        parameter_schema=PERFORMANCE_SUMMARY_PARAMETER_SCHEMA,
        group_key="performance",
        tags=["composite", "read-only", "performance", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        embedding_service=embedding_service,
    )

    await register_composite_operation(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id="vmware.composite.datastore.usage",
        handler=datastore_usage_composite,
        summary="List datastores with capacity, free space, and VM placement.",
        description=(
            "Reads the datastore listing, then per-datastore detail "
            "(capacity, free space, type) plus the VM-placement filter "
            "via 'GET:/vcenter/vm?filter.datastores=...'. Aggregates "
            "into one row per datastore including vm_count + vm_names. "
            "Equivalent of an operator-facing 'storage usage report' "
            "that would otherwise require 1 + N sub-calls. Read-only -- "
            "never mutates storage state."
        ),
        parameter_schema=DATASTORE_USAGE_PARAMETER_SCHEMA,
        group_key="storage",
        tags=["composite", "read-only", "storage", "datastore"],
        safety_level="safe",
        requires_approval=False,
        embedding_service=embedding_service,
    )

    await register_composite_operation(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id="vmware.composite.network.portgroup.audit",
        handler=network_portgroup_audit_composite,
        summary="Audit distributed portgroups with parent DVS + connected VMs.",
        description=(
            "Reads the distributed-switch listing (for parent-DVS name "
            "enrichment) plus the distributed-portgroup listing, then "
            "per-portgroup queries the VM list via "
            "'GET:/vcenter/vm?filter.networks=...'. Aggregates one row "
            "per portgroup with its parent DVS + connected VM names. "
            "Equivalent of 'govc dvs.portgroup.info' rolled up across "
            "every portgroup. Read-only -- never mutates network "
            "configuration."
        ),
        parameter_schema=NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA,
        group_key="networking",
        tags=["composite", "read-only", "networking", "portgroup"],
        safety_level="safe",
        requires_approval=False,
        embedding_service=embedding_service,
    )
