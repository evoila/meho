# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vRLI 9.x read-only v0.5 core — curated operator-enabled subset.

This module names the **7 read-only vRLI operations** the G3.6 vRLI
v0.5 ship enables out of the much larger ``vcf-logs-9.0/<api-v2>.yaml``
OpenAPI corpus that the G0.7 spec-ingestion pipeline lands under
``connector_id="vrli-rest-9.0"``. The curation is two-layered:

* :data:`VRLI_CORE_GROUPS` — the operator-reviewed ``when_to_use``
  hint per LLM-grouping pass output group. Each entry's ``group_key``
  is the deterministic slug :func:`classify_vrli_op` assigns to vRLI
  ops; the ``when_to_use`` is what the agent reads verbatim through
  :func:`~meho_backplane.operations.meta_tools.list_operation_groups`
  to pick a group to search within.
* :data:`VRLI_CORE_OPS` — the 7 ``EndpointDescriptor.op_id`` strings
  that flip to ``is_enabled=True`` at operator-review time, paired
  with the per-op ``llm_instructions`` blob the agent inlines into
  the reasoning context when it sees the op in
  :func:`~meho_backplane.operations.meta_tools.search_operations`
  hits. Every other op under the same connector triple stays
  ``is_enabled=False`` (the G0.7 ingestion default for
  ``source_kind='ingested'`` rows).

Per Initiative #369 and CLAUDE.md postulates 1-2, vRLI is **fully
generic-ingested**: the underlying ops are not registered in code,
they live in the ``endpoint_descriptor`` table. This module only
carries the **operator-review metadata** the substrate uses at the
review step — the actual curation is applied through
:func:`apply_vrli_core_curation` against an existing ingested
connector.

vRLI is a **log-query** product (event search + aggregation are the
headline ops); the curated read core is biased toward those two
plus the small set of inventory / catalog ops an operator needs to
compose a useful query (fields known to the indexer, hosts
reporting, content packs governing field extraction, alert
definitions running).

The 7 ops (paths sourced against the vRLI 9.x REST API documented at
https://developer.broadcom.com/xapis/vrealize-log-insight-api/latest/
and the v1 reference at https://vmw-loginsight.github.io/, both of
which describe the same shape under the ``/api/v2/`` family in 9.x):

1. ``GET:/api/v2/version`` — ``vrli.about`` — appliance version /
   release-name / build. The same surface :meth:`VcfLogsConnector.fingerprint`
   already consumes; exposing it as an operator-callable op lets the
   agent run a sanity probe before any heavier read.
2. ``GET:/api/v2/events/{constraints}`` — ``vrli.event.query`` —
   constraint + time-range filtered raw-event search. Returns a
   :class:`~meho_backplane.connectors.schemas.ResultHandle` for
   large result sets -- a bounded inline sample plus a ``fetch_more``
   envelope -- rather than the full payload inline. To act on more than
   the sample the agent re-runs with a narrower constraint / time range.
3. ``GET:/api/v2/aggregated-events/{constraints}`` — ``vrli.aggregated.query``
   — group-by aggregation over the same constraint set as the event
   query. Useful for "how many events per host / severity / source
   in the last 24h" reductions.
4. ``GET:/api/v2/fields`` — ``vrli.field.list`` — catalog of fields
   the indexer knows about (static + extracted). The agent reads
   this before composing a non-trivial event-query constraint to
   confirm a field name exists.
5. ``GET:/api/v2/hosts`` — ``vrli.host.list`` — hosts currently
   reporting events to this vRLI cluster. Combined with the field
   catalog, this is the minimum agent-side composer-context for a
   useful event query.
6. ``GET:/api/v2/content/contentpack/list`` — ``vrli.content.pack.list``
   — installed content packs (each governs a set of extracted
   fields, dashboards, and alerts). Read when the operator asks
   "which integrations are configured on this vRLI".
7. ``GET:/api/v2/alerts`` — ``vrli.alert.list`` — alert definitions
   currently configured. Read-only; create / mutate are intentionally
   excluded from v0.5 per #369 DoD (write ops stay ``staged``).

Path families and group_keys
-----------------------------

vRLI's REST paths split cleanly into five families:

* ``/api/v2/version`` + ``/api/v2/fields`` → ``vrli-system``
  (appliance identity + indexer catalog metadata).
* ``/api/v2/events`` + ``/api/v2/aggregated-events`` → ``vrli-events``
  (the headline query surfaces).
* ``/api/v2/hosts`` → ``vrli-inventory`` (host reporting inventory).
* ``/api/v2/content`` → ``vrli-content`` (content packs).
* ``/api/v2/alerts`` → ``vrli-alerts`` (alert definitions).

:data:`VRLI_PATH_RULES` orders the rules most-specific-first so the
``startswith`` loop in :func:`classify_vrli_op` terminates at the
right group. Path-prefix matching mirrors the pattern
:mod:`meho_backplane.connectors.harbor.core_ops` and
:mod:`meho_backplane.connectors.nsx.core_ops` established —
stable, deterministic, operator-reviewable.

Curation application
--------------------

:func:`apply_vrli_core_curation` is the operator-review-time
substrate call that makes exactly the 7 curated ops dispatchable.
Mirrors :func:`~meho_backplane.connectors.harbor.core_ops.apply_harbor_core_curation`
verbatim, threading the "enable group but pin non-core ops disabled"
needle via the audit-log-driven operator-override exclusion. See
:func:`~meho_backplane.operations.ingest._internals.operator_disabled_op_ids`
for the cascade's exclusion-list source.

Write ops invariant
-------------------

The v0.5 core is **read-only**. Write ops on vRLI (alert create,
content-pack import, query result export) stay ``is_enabled=False``
under their respective groups; the path-prefix classifier in
:data:`VRLI_PATH_RULES` is permissive ("any op under
``/api/v2/alerts``" hits ``vrli-alerts``), but :func:`classify_vrli_op`
restricts the group assignment to ``GET`` operations only — non-GET
methods classify as ``"none"`` and never appear under a curated
group. Acceptance tests
(:mod:`tests.test_connectors_vcf_logs_core_ops`) assert this
invariant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog

from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "VRLI_CONNECTOR_ID",
    "VRLI_CORE_GROUPS",
    "VRLI_CORE_OPS",
    "VRLI_IMPL_ID",
    "VRLI_PATH_RULES",
    "VRLI_PRODUCT",
    "VRLI_VERSION",
    "VrliCoreGroup",
    "VrliCoreOp",
    "apply_vrli_core_curation",
    "classify_vrli_op",
]

_log = structlog.get_logger(__name__)

#: Endpoint-descriptor product key — what
#: :func:`~meho_backplane.operations._lookup.parse_connector_id`
#: extracts from ``"vrli-rest-9.0"`` (first hyphen-segment of impl_id
#: ``"vrli-rest"``).
#:
#: Note the discrepancy: :attr:`VcfLogsConnector.product` is
#: ``"vcf-logs"`` (the v2 registry triple key) but ingested rows
#: carry ``product="vrli"`` because the dispatcher's
#: :func:`parse_connector_id` reads the first hyphen-segment of the
#: ``connector_id`` slug. Same shape SDDC Manager uses
#: (``product="sddc"`` on rows vs ``product="sddc-manager"`` on the
#: connector class) — documented under
#: :data:`~meho_backplane.connectors.sddc_manager.core_ops.SDDC_PRODUCT`.
VRLI_PRODUCT: Final[str] = "vrli"
VRLI_VERSION: Final[str] = "9.0"
VRLI_IMPL_ID: Final[str] = "vrli-rest"

#: Connector-id slug the G0.6 dispatcher's ``parse_connector_id``
#: round-trips back to the triple above: ``"vrli-rest-9.0"``.
VRLI_CONNECTOR_ID: Final[str] = f"{VRLI_IMPL_ID}-{VRLI_VERSION}"


@dataclass(frozen=True, slots=True)
class VrliCoreGroup:
    """One curated operator-review entry for a vRLI operation group.

    ``group_key`` is the slug :func:`classify_vrli_op` emits.
    ``name`` is the operator-readable label ``meho connector review``
    renders. ``when_to_use`` is the agent-facing hint
    :func:`list_operation_groups` returns verbatim; every entry is a
    single complete sentence so the agent's group-selection step has
    unambiguous guidance.
    """

    group_key: str
    name: str
    when_to_use: str


@dataclass(frozen=True, slots=True)
class VrliCoreOp:
    """One curated operator-review entry for a vRLI operation.

    ``op_id`` follows the ``METHOD:path`` shape every
    ``source_kind='ingested'`` row uses; the path matches an entry
    in the vRLI 9.x OpenAPI spec under the ``/api/v2/`` family.

    ``llm_instructions`` is the per-op JSON blob the meta-tools
    inline verbatim when the op surfaces. The shape (``when_to_call``
    / ``output_shape`` / ``next_step``) mirrors the typed-connector
    convention from :mod:`meho_backplane.connectors.bind9.ops_zone`
    and :mod:`meho_backplane.connectors.nsx.core_ops` — same agent
    reads both surfaces, so the structure stays uniform.
    """

    op_id: str
    group_key: str
    llm_instructions: dict[str, object]


#: Path-prefix → group_key classifier rules for vRLI.
#:
#: **Order is load-bearing.** Each rule is checked via
#: ``path.startswith(prefix)``. More-specific prefixes must precede
#: less-specific ones to avoid a shorter prefix consuming a path
#: that belongs to a deeper group:
#:
#: * ``/api/v2/aggregated-events`` before ``/api/v2/events`` —
#:   the aggregated path is not a prefix of the raw-event path, but
#:   the rule ordering documents the intent ("aggregated is a
#:   distinct surface from raw events") so the deterministic
#:   classifier output reads cleanly during operator review.
#: * ``/api/v2/version`` and ``/api/v2/fields`` both map to
#:   ``vrli-system``; the appliance-identity probe (version) and
#:   the indexer catalog (fields) co-locate because the agent reads
#:   both as part of "is this vRLI ready, and what can I query".
VRLI_PATH_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("/api/v2/version", "vrli-system"),
    ("/api/v2/fields", "vrli-system"),
    ("/api/v2/aggregated-events", "vrli-events"),
    ("/api/v2/events", "vrli-events"),
    ("/api/v2/hosts", "vrli-inventory"),
    ("/api/v2/content", "vrli-content"),
    ("/api/v2/alerts", "vrli-alerts"),
)


def classify_vrli_op(op_id: str) -> str:
    """Return the curated ``group_key`` for a vRLI op_id, or ``"none"``.

    ``op_id`` is the ``METHOD:/path`` form ingested rows carry; the
    helper strips the verb and matches the path against
    :data:`VRLI_PATH_RULES` in order.

    Non-``GET`` methods classify as ``"none"`` — v0.5's curated core
    is read-only; write ops never land under a curated group even
    when their path matches a curated family. Acceptance tests
    enforce this invariant.

    Returns ``"none"`` for paths outside the curated families (e.g.
    ``/api/v2/sessions``, ``/api/v2/notification/webhook``); those
    rows are un-curated and stay ``is_enabled=False`` after
    :func:`apply_vrli_core_curation` runs.
    """
    try:
        method, path = op_id.split(":", 1)
    except ValueError:
        return "none"
    if method != "GET":
        return "none"
    for prefix, group_key in VRLI_PATH_RULES:
        if path.startswith(prefix):
            return group_key
    return "none"


#: Operator-reviewed ``when_to_use`` hints for the 5 vRLI groups the
#: read-only v0.5 core spans. Every hint is one complete sentence
#: the agent reads verbatim — vague hints poison
#: ``search_operations`` ranking, per the ai_engineering pack.
VRLI_CORE_GROUPS: Final[tuple[VrliCoreGroup, ...]] = (
    VrliCoreGroup(
        group_key="vrli-system",
        name="vRLI (system + indexer catalog)",
        when_to_use=(
            "Use this group to read vRLI appliance-level information and "
            "indexer metadata: the software version and release name "
            "(version), and the catalog of static + extracted fields the "
            "indexer knows about (fields). The agent reads version as a "
            "pre-flight probe before any heavier query, and reads the "
            "field catalog before composing a non-trivial event-query "
            "constraint to confirm a field name exists on this cluster."
        ),
    ),
    VrliCoreGroup(
        group_key="vrli-events",
        name="vRLI Event Queries",
        when_to_use=(
            "Use this group to query vRLI log events — both raw event "
            "search and group-by aggregation. The headline read surface "
            "of vRLI: event queries return constraint-filtered, "
            "time-range-bounded log lines; aggregated queries return "
            "numeric counts grouped by one or more fields. Result sets "
            "are JSONFlux-handle-shaped (typically large): a bounded "
            "inline sample plus a ``fetch_more`` envelope rather than the "
            "full payload. Re-run with a narrower constraint / time range "
            "to act on more than the sample."
        ),
    ),
    VrliCoreGroup(
        group_key="vrli-inventory",
        name="vRLI Hosts",
        when_to_use=(
            "Use this group to enumerate hosts currently reporting log "
            "events to this vRLI cluster. Returns each host's hostname, "
            "source-type, and last-seen timestamp. Combined with the "
            "field catalog (vrli-system), this is the minimum "
            "agent-side composer-context for a useful event query."
        ),
    ),
    VrliCoreGroup(
        group_key="vrli-content",
        name="vRLI Content Packs",
        when_to_use=(
            "Use this group to list installed vRLI content packs. Each "
            "pack governs a set of extracted fields, dashboards, and "
            "alert templates for a specific product integration (NSX, "
            "vSAN, vCenter, etc.). Read when the operator asks 'which "
            "integrations are configured on this vRLI' or 'why is field "
            "X missing from the catalog'."
        ),
    ),
    VrliCoreGroup(
        group_key="vrli-alerts",
        name="vRLI Alert Definitions",
        when_to_use=(
            "Use this group to list alert definitions configured on "
            "this vRLI cluster. Each alert carries its name, search "
            "constraint, time window, hit threshold, and notification "
            "channels. Read-only in v0.5 — alert create / update / "
            "delete are deliberately excluded from the curated core."
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

    Same three-field shape :mod:`meho_backplane.connectors.nsx.core_ops`
    and :mod:`meho_backplane.connectors.harbor.core_ops` use so an
    agent crossing connector boundaries sees a stable convention.
    """
    return {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
    }


#: The 7 curated read-only vRLI core ops. Each entry carries the
#: op_id (``GET:/path`` form), the curated group assignment, and the
#: operator-reviewed ``llm_instructions`` blob.
#:
#: Paths cross-checked against the vRLI 9.x REST API at
#: https://developer.broadcom.com/xapis/vrealize-log-insight-api/latest/
#: and the v1 reference at https://vmw-loginsight.github.io/ (which
#: documents the same shape under the ``/api/v2/`` family in 9.x).
VRLI_CORE_OPS: Final[tuple[VrliCoreOp, ...]] = (
    VrliCoreOp(
        op_id="GET:/api/v2/version",
        group_key="vrli-system",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to identify the vRLI appliance — its software "
                "version, build, and release name. Useful as a "
                "pre-flight probe before any heavier query, or to "
                "confirm which vRLI cluster the target points at."
            ),
            output_shape=(
                "Object with version (e.g. '9.0.0'), releaseName "
                "(e.g. 'VMware Aria Operations for Logs 9.0'), and "
                "build (the appliance build number)."
            ),
            next_step=(
                "If the appliance looks healthy and the version is in "
                "the supported range, proceed to vrli.field.list to "
                "confirm the catalog of queryable fields, then compose "
                "the event-query constraint."
            ),
        ),
    ),
    VrliCoreOp(
        op_id="GET:/api/v2/events/{constraints}",
        group_key="vrli-events",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to query raw vRLI log events. The {constraints} "
                "path segment carries the URL-encoded constraint set "
                "(field/value pairs, time range, limit) — compose it "
                "with field names obtained from vrli.field.list. The "
                "headline read surface of vRLI; use when answering "
                "'show me events where source contains nsx and "
                "severity is error in the last 1h'."
            ),
            output_shape=(
                "Result-handle-shaped: events[] array of raw event "
                "rows (each with timestamp, text, fields), plus "
                "complete (bool) indicating whether the constraint "
                "exhausted the index or hit the limit. Large result "
                "sets return a JSONFlux ResultHandle with a bounded "
                "inline sample plus a ``fetch_more`` envelope; re-run "
                "with a narrower constraint / time range to act on more "
                "than the sample."
            ),
            next_step=(
                "If complete=false, surface the truncation to the "
                "operator. For drill-down, pick an event timestamp + "
                "host and re-compose a tighter constraint, or switch "
                "to vrli.aggregated.query for group-by counts."
            ),
        ),
    ),
    VrliCoreOp(
        op_id="GET:/api/v2/aggregated-events/{constraints}",
        group_key="vrli-events",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to run a group-by aggregation over the same "
                "constraint shape vrli.event.query accepts, plus a "
                "bin-by / group-by clause and an aggregation function "
                "(count, sum, avg, min, max). Use when answering "
                "'how many error events per host in the last 24h' or "
                "'top 10 sources by event volume since midnight'."
            ),
            output_shape=(
                "Object with bins[] array of (group_key -> value) "
                "tuples, each carrying the aggregated metric value "
                "plus the bucket boundary (time-bucket or field-value "
                "bucket depending on the group-by clause). Numeric "
                "sequence safe to inline directly into agent context."
            ),
            next_step=(
                "Surface the top-N bins to the operator. For drill "
                "into a specific bin, pass the bin's group_key as "
                "an extra constraint to vrli.event.query."
            ),
        ),
    ),
    VrliCoreOp(
        op_id="GET:/api/v2/fields",
        group_key="vrli-system",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list the catalog of fields the vRLI indexer "
                "knows about — both static (source, hostname, "
                "timestamp) and extracted (parsed from log content "
                "via content-pack rules). Read this before composing "
                "a non-trivial vrli.event.query constraint to confirm "
                "the field name actually exists on this cluster."
            ),
            output_shape=(
                "Array of field entries; each carries name, type "
                "(string / numeric / timestamp), source (static or "
                "the content pack that defined it), and a brief "
                "description when documented."
            ),
            next_step=(
                "Pick the field names that match the operator's "
                "intent and compose them into the constraint for "
                "vrli.event.query or vrli.aggregated.query."
            ),
        ),
    ),
    VrliCoreOp(
        op_id="GET:/api/v2/hosts",
        group_key="vrli-inventory",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list hosts currently reporting log events "
                "to this vRLI cluster. Useful when answering 'which "
                "hosts are sending logs', 'is host X reporting', or "
                "as the input list for a per-host aggregation."
            ),
            output_shape=(
                "Array of host entries; each carries hostname, "
                "source-type (the agent / forwarder that ships the "
                "events), and last_seen timestamp."
            ),
            next_step=(
                "Pick a hostname for a constraint on vrli.event.query "
                "(field='hostname', operator='=', value='<host>'), "
                "or aggregate across hosts via vrli.aggregated.query."
            ),
        ),
    ),
    VrliCoreOp(
        op_id="GET:/api/v2/content/contentpack/list",
        group_key="vrli-content",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list installed vRLI content packs. Each "
                "pack governs a set of extracted fields, dashboards, "
                "and alert templates for a specific product "
                "integration (NSX, vSAN, vCenter, etc.). Read when "
                "answering 'which integrations are configured on "
                "this vRLI', 'why is field X missing from the "
                "catalog', or auditing the cluster's content surface."
            ),
            output_shape=(
                "Array of content-pack entries; each carries name, "
                "namespace, version, and a brief description of the "
                "fields / dashboards / alerts it contributes."
            ),
            next_step=(
                "If a content pack the operator expects is missing, "
                "surface the gap. Cross-reference pack-contributed "
                "fields with vrli.field.list when investigating "
                "extraction issues."
            ),
        ),
    ),
    VrliCoreOp(
        op_id="GET:/api/v2/alerts",
        group_key="vrli-alerts",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list alert definitions configured on this "
                "vRLI cluster. Use when auditing which alerts exist, "
                "checking which fire on a given constraint, or "
                "confirming a recent alert configuration change. "
                "Read-only in v0.5 — alert create / update / delete "
                "are deliberately excluded from the curated core."
            ),
            output_shape=(
                "Array of alert entries; each carries id, name, "
                "enabled (bool), constraint (the search clause "
                "vRLI evaluates), search_period (the rolling time "
                "window), hit_count threshold, and notification "
                "channels (email, webhook, vROps target)."
            ),
            next_step=(
                "Surface enabled alerts whose constraint overlaps "
                "the operator's investigation. For drill into "
                "what an alert would match, lift its constraint "
                "into vrli.event.query against the same time range."
            ),
        ),
    ),
)


async def apply_vrli_core_curation(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
) -> None:
    """Apply the curated 7-op read core against an ingested vRLI connector.

    Drives the substrate so that, after this call returns, exactly
    the 7 ops in :data:`VRLI_CORE_OPS` are dispatchable
    (``is_enabled=True``) and every other ingested op stays
    ``is_enabled=False``. The 5 curated groups land
    ``review_status='enabled'`` so the agent's
    :func:`~meho_backplane.operations.meta_tools.search_operations`
    surfaces the core ops; non-curated groups are left untouched
    (``review_status='staged'`` from the G0.7 ingest default).

    The substrate doesn't expose "enable only ops X, Y, Z under
    group G": :meth:`ReviewService.enable_group`'s cascade flips
    ``is_enabled=True`` on every child op in the group. The helper
    works around this via the audit-log-driven operator-override
    exclusion — the same mechanism
    :func:`~meho_backplane.connectors.harbor.core_ops.apply_harbor_core_curation`
    and :func:`~meho_backplane.connectors.nsx.core_ops.apply_nsx_core_curation`
    established:

    1. :meth:`ReviewService.get_review_payload` loads the current
       state of every curated group and its child ops.
    2. For each child op in a curated group that **isn't** in the
       :data:`VRLI_CORE_OPS` allow-list,
       :meth:`ReviewService.edit_op` with ``is_enabled=False``
       writes the operator-override audit row. The follow-on
       :meth:`enable_group` cascade detects these rows and skips
       them.
    3. :meth:`ReviewService.edit_group` lands the operator-reviewed
       ``name`` + ``when_to_use`` on each curated group.
    4. :meth:`ReviewService.enable_group` flips
       ``review_status='enabled'`` and cascades ``is_enabled=True``
       to the curated child ops (operator-overridden non-core ops
       are skipped).
    5. :meth:`ReviewService.edit_op` lands the curated
       ``llm_instructions`` blob per entry in :data:`VRLI_CORE_OPS`.

    Raises :class:`~meho_backplane.operations.ingest.ConnectorNotFoundError`
    if no groups exist for ``vrli-rest-9.0`` under *tenant_id* (the
    operator must run ``meho connector ingest`` against the vRLI
    spec before this helper applies).
    """
    payload = await review_service.get_review_payload(
        VRLI_CONNECTOR_ID,
        tenant_id,
    )

    core_op_ids_by_group: dict[str, set[str]] = {}
    for op in VRLI_CORE_OPS:
        core_op_ids_by_group.setdefault(op.group_key, set()).add(op.op_id)

    for group_payload in payload.groups:
        allow_list = core_op_ids_by_group.get(group_payload.group_key)
        if allow_list is None:
            continue
        for review_op in group_payload.ops:
            if review_op.op_id in allow_list:
                continue
            await review_service.edit_op(
                VRLI_CONNECTOR_ID,
                review_op.op_id,
                tenant_id=tenant_id,
                is_enabled=False,
            )
            _log.info(
                "vrli_non_core_op_disabled",
                connector_id=VRLI_CONNECTOR_ID,
                op_id=review_op.op_id,
                group_key=group_payload.group_key,
            )

    for group in VRLI_CORE_GROUPS:
        await review_service.edit_group(
            VRLI_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
            name=group.name,
            when_to_use=group.when_to_use,
        )
        await review_service.enable_group(
            VRLI_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
        )
        _log.info(
            "vrli_core_group_enabled",
            connector_id=VRLI_CONNECTOR_ID,
            group_key=group.group_key,
        )

    for op in VRLI_CORE_OPS:
        await review_service.edit_op(
            VRLI_CONNECTOR_ID,
            op.op_id,
            tenant_id=tenant_id,
            llm_instructions=op.llm_instructions,
        )
        _log.info(
            "vrli_core_op_curated",
            connector_id=VRLI_CONNECTOR_ID,
            op_id=op.op_id,
        )
